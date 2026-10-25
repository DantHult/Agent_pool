"""Portable HANDOFF.md export for continuing a conversation with another agent."""
import json
import os
from pathlib import Path
import tempfile
import unittest
import uuid
from unittest.mock import patch

from relay.handoff import export_handoff, export_project_handoff
from relay.ide import ProjectIDE
from relay.store import Relay
import test_local_native_dashboard as native_fixture


class HandoffExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory();self.root = Path(self.temp.name).resolve()
        self.env = patch.dict(os.environ, {'RELAY_NATIVE_ROOT': str(self.root / 'native')});self.env.start()
        self.relay = Relay(self.root / 'data')
        self.c = self.relay.detail('alice', 'demo')['conversation']['id']

    def tearDown(self):self.relay.close();self.env.stop();self.temp.cleanup()

    def export(self, user='alice', conversation=None):return export_handoff(self.relay, user, conversation or self.c)

    def test_handed_off_work_reads_as_a_standalone_brief(self):
        task = self.relay.mutate('alice', 'prompt', {'conversation_id': self.c, 'prompt': 'Build a password validator'})
        self.relay.mutate('alice', 'switch', {'task_id': task['id'], 'generation': task['generation'], 'target_worker_id': 'codexB'})
        handoff = self.export()
        text = handoff['content']
        self.assertEqual(handoff['filename'], 'HANDOFF.md')
        self.assertTrue(text.startswith('# Handoff: Password toolkit\n'))
        for expected in ('Codex Worker A', 'Codex Worker B', '## How to continue', '## Requests in this conversation',
                         '1. Build a password validator', 'latest request', '`validator.py`', 'handed off to Codex Worker B'):
            self.assertIn(expected, text)
        for internal in ('/api/', '.relay/history', 'writer lease', 'older_event_endpoint'):
            self.assertNotIn(internal, text)
        self.assertNotIn('Project files:', text)  # a server path means nothing outside the local owner's machine
        self.assertTrue(self.relay.rows("SELECT 1 FROM audit_events WHERE action='handoff_exported'"))

    def test_agent_progress_is_cleaned_for_use_elsewhere(self):
        task = self.relay.mutate('alice', 'tasks/assign', {'project_id': 'demo', 'conversation_id': self.c, 'worker_id': 'codexA', 'prompt': 'Build a password validator', 'queue': True})
        clone = self.root / 'native/workspaces/c' / uuid.uuid4().hex
        progress = {'summary': 'Built it with key sk-test-secret-value', 'completed_work': ['Wrote [index.html](' + str(clone) + '/index.html)'],
                    'pending_steps': ['Add a login page\nthen style it'], 'completion_state': 'continue', 'test_results': ['```\n3 passed\n```']}
        self.relay.db.execute('UPDATE tasks SET progress_json=? WHERE id=?', (json.dumps(progress), task['id']))
        with patch.dict(os.environ, {'OPENAI_API_KEY': 'sk-test-secret-value'}):text = self.export()['content']
        self.assertIn('Wrote [index.html](index.html)', text);self.assertNotIn(str(clone), text)
        self.assertNotIn('sk-test-secret-value', text);self.assertIn('[REDACTED]', text)
        self.assertIn('- Add a login page\n  then style it', text)
        self.assertIn('agent reported: continue', text)
        self.assertNotIn('.relay/', text)
        self.relay.ws.write_handoff('demo', {'internal': True})  # modified first in git status
        self.assertNotIn('.relay/', self.export()['content'])
        (self.relay.ws.path('demo') / 'notes.txt').write_text('human edit\n')
        self.assertIn('?? notes.txt', self.export()['content'])

    def test_a_conversation_with_no_agent_work_still_exports(self):
        from relay.ide import ProjectIDE
        empty = ProjectIDE(self.relay).create_conversation('alice', 'demo', 'Fresh idea')
        text = self.export(conversation=empty['id'])['content']
        self.assertIn('# Handoff: Fresh idea', text);self.assertIn('No agent has worked in this conversation yet', text)
        self.assertNotIn('## Requests', text)

    def test_access_follows_project_membership_and_deletion(self):
        private = self.relay.detail('bob', 'private')['conversation']['id']
        with self.assertRaises(PermissionError):self.export('alice', private)
        self.relay.mutate('admin', 'projects/delete', {'project_id': 'demo', 'confirm_name': 'Password toolkit'})
        with self.assertRaises(PermissionError):self.export('alice')

    def test_the_local_owner_gets_the_project_folder(self):
        self.relay.local_owner_id = 'alice'
        self.assertIn('- **Project files:** `' + str(self.relay.ws.path('demo')) + '`', self.export()['content'])


class ProjectHandoffTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory();self.root = Path(self.temp.name).resolve()
        self.env = patch.dict(os.environ, {'RELAY_NATIVE_ROOT': str(self.root / 'native')});self.env.start()
        self.relay = Relay(self.root / 'data');self.ide = ProjectIDE(self.relay)
        self.first = self.relay.detail('alice', 'demo')['conversation']['id']

    def tearDown(self):self.relay.close();self.env.stop();self.temp.cleanup()

    def test_every_visible_project_is_listed_with_its_conversations(self):
        second = self.ide.create_conversation('alice', 'demo', 'Second idea')
        listed = {item['id']: [c['title'] for c in item['conversations']] for item in self.ide.all_conversations('alice')['projects']}
        self.assertEqual(set(listed), {'demo'})  # not bob's private project
        self.assertEqual(set(listed['demo']), {'Password toolkit', 'Second idea'})
        self.assertEqual({item['id'] for item in self.ide.all_conversations('bob')['projects']}, {'demo', 'private'})
        self.relay.mutate('admin', 'projects/delete', {'project_id': 'private', 'confirm_name': 'Private sandbox'})
        self.assertEqual({item['id'] for item in self.ide.all_conversations('bob')['projects']}, {'demo'})

    def test_project_handoff_combines_active_conversations(self):
        task = self.relay.mutate('alice', 'prompt', {'conversation_id': self.first, 'prompt': 'Build a password validator'})
        self.relay.mutate('alice', 'switch', {'task_id': task['id'], 'generation': task['generation'], 'target_worker_id': 'codexB'})
        later = self.ide.create_conversation('alice', 'demo', 'Docs pass')
        hidden = self.ide.create_conversation('alice', 'demo', 'Abandoned idea')
        self.ide.update_conversation('alice', 'demo', hidden['id'], {'archived': True, 'confirmed': True})
        text = export_project_handoff(self.relay, 'alice', 'demo')['content']
        self.assertTrue(text.startswith('# Handoff: Password toolkit\n'))
        self.assertIn('- **Conversations:** 2 (1 archived, not included)', text)
        self.assertNotIn('Abandoned idea', text)
        self.assertLess(text.index('## Conversation: Docs pass'), text.index('## Conversation: Password toolkit'))  # most recent first
        self.assertIn('_No agent work yet', text)
        for expected in ('### Requests in this conversation', '1. Build a password validator', 'worked on by Codex Worker A, Codex Worker B',
                         'handed off to Codex Worker B', 'Work so far by Codex Worker A, Codex Worker B'):
            self.assertIn(expected, text)
        self.assertNotIn('/api/', text)
        with self.assertRaises(PermissionError):export_project_handoff(self.relay, 'alice', 'private')
        self.assertTrue(self.relay.rows("SELECT 1 FROM audit_events WHERE action='handoff_exported' AND metadata LIKE '%project%'"))


class HandoffExportHTTPTests(unittest.TestCase):
    def setUp(self):self.fixture = native_fixture.LocalNativeDashboardTests();self.fixture.setUp()
    def tearDown(self):self.fixture.tearDown()

    def test_route_requires_a_session_and_returns_markdown(self):
        path = '/api/conversations/' + self.fixture.conversation + '/handoff'
        self.assertEqual(native_fixture.Client(self.fixture.server).request(path)[0], 401)
        handoff = self.fixture.client.get(path.removeprefix('/api/'))
        self.assertEqual(handoff['filename'], 'HANDOFF.md');self.assertIn('# Handoff: Disposable native sample', handoff['content'])
        self.assertTrue(self.fixture.client.get('overview?light=1')['features']['handoff_export'])
        listed = self.fixture.client.get('conversations')['projects']
        self.assertEqual([item['id'] for item in listed], [self.fixture.project])
        project = self.fixture.client.get('projects/' + self.fixture.project + '/handoff')
        self.assertIn('## Conversation: Disposable native sample', project['content'])
        self.assertEqual(native_fixture.Client(self.fixture.server).request('/api/projects/' + self.fixture.project + '/handoff')[0], 401)


if __name__ == '__main__':
    unittest.main()
