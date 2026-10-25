"""Deleting a project: confirmation, guards, file removal and retained history."""
import json
import os
from pathlib import Path
import tempfile
import unittest
import uuid
from unittest.mock import patch

from relay.ide import ProjectIDE, WorkspaceConflict
from relay.store import Relay
from relay.workspace import Workspace
import test_local_native_dashboard as native_fixture

CLIENT = 'delete-browser-0123456789'


class ProjectDeletionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory();self.root = Path(self.temp.name).resolve()
        self.env = patch.dict(os.environ, {'RELAY_NATIVE_ROOT': str(self.root / 'native')});self.env.start()
        self.relay = Relay(self.root / 'data');self.ide = ProjectIDE(self.relay)
        self.workspace = self.relay.ws.path('demo')
        self.conversation = self.relay.detail('alice', 'demo')['conversation']['id']

    def tearDown(self):self.relay.close();self.env.stop();self.temp.cleanup()

    def delete(self, user='admin', name='Password toolkit', project='demo'):
        return self.relay.mutate(user, 'projects/delete', {'project_id': project, 'confirm_name': name})

    def event(self, kind, metadata):
        count = self.relay.db.execute('SELECT COUNT(*) FROM events').fetchone()[0]
        self.relay.db.execute('INSERT INTO events VALUES(?,?,?,?,?,?,?,?,?,?,?,?)', (uuid.uuid4().hex, 'orgdemo', 'demo', self.conversation, None, count + 1000, 'now', None, kind, 'system', '', json.dumps(metadata)))

    def test_only_an_administrator_who_types_the_name_can_delete(self):
        with self.assertRaises(PermissionError):self.delete('alice')  # demo creators are engineers
        with self.assertRaises(ValueError):self.delete(name='password toolkit')
        self.assertTrue(self.workspace.is_dir())
        self.assertIn('demo', [p['id'] for p in self.relay.overview('admin', light=True)['projects']])

    def test_deletion_removes_files_and_access_but_keeps_history(self):
        self.ide.acquire('alice', 'demo', CLIENT, 'session');self.ide.draft('alice', 'demo', CLIENT, 'session', 'draft.py', None, 'unsaved')
        queued = self.relay.mutate('alice', 'tasks/assign', {'project_id': 'demo', 'worker_id': 'codexA', 'prompt': 'Build a password validator', 'queue': True})
        events = self.relay.db.execute('SELECT COUNT(*) FROM events WHERE project_id=?', ('demo',)).fetchone()[0]
        self.relay.db.execute("DELETE FROM human_workspace_locks")  # alice's tab closed; her draft remains
        result = self.delete()
        self.assertEqual((result['deleted'], result['cancelled_tasks'], result['files_not_removed']), (True, 1, []))
        self.assertFalse(self.workspace.exists())
        for user in ('admin', 'alice', 'bob'):
            self.assertNotIn('demo', [p['id'] for p in self.relay.overview(user, light=True)['projects']])
            with self.assertRaises(PermissionError):self.relay.detail(user, 'demo')
            with self.assertRaises(PermissionError):self.ide.state(user, 'demo')
        self.assertFalse(self.workspace.exists())  # no read path recreates it
        self.assertEqual(self.relay.one('SELECT state FROM tasks WHERE id=?', (queued['id'],))['state'], 'cancelled')
        self.assertGreater(self.relay.db.execute('SELECT COUNT(*) FROM events WHERE project_id=?', ('demo',)).fetchone()[0], events)
        for table in ('worker_projects', 'editor_drafts', 'human_workspace_locks'):
            self.assertFalse(self.relay.rows(f'SELECT 1 FROM {table} WHERE project_id=?', ('demo',)))
        self.assertTrue(self.relay.rows("SELECT 1 FROM audit_events WHERE action='project_deleted' AND project_id='demo'"))
        self.assertIn('private', [p['id'] for p in self.relay.overview('bob', light=True)['projects']])
        self.relay.close();self.relay = Relay(self.root / 'data')
        self.assertNotIn('demo', [p['id'] for p in self.relay.overview('admin', light=True)['projects']])

    def test_running_agents_open_commands_and_other_editors_block_deletion(self):
        self.relay.db.execute("INSERT INTO workspace_leases VALUES('demo','task','codexA','token','now')")
        with self.assertRaises(WorkspaceConflict):self.delete()
        self.relay.db.execute('DELETE FROM workspace_leases')
        self.relay.human_commands = lambda project: project == 'demo'
        with self.assertRaises(WorkspaceConflict):self.delete()
        self.relay.human_commands = None
        self.ide.acquire('bob', 'demo', CLIENT, 'bob-session')
        with self.assertRaises(WorkspaceConflict):self.delete()
        self.assertTrue(self.workspace.is_dir())
        self.ide.acquire('admin', 'private', CLIENT, 'admin-session')
        self.assertTrue(self.delete(name='Private sandbox', project='private')['deleted'])  # one's own reservation is no obstacle

    def test_agents_private_clones_and_stored_outputs_are_removed(self):
        from relay.native import initialize_profiles, profile_paths
        initialize_profiles()
        mine, other = uuid.uuid4().hex, uuid.uuid4().hex
        for identity in (mine, other):(profile_paths('b')[1] / identity / 'src').mkdir(parents=True)
        self.event('session_started', {'native_profile': 'b', 'workspace_id': mine})
        self.event('session_started', {'native_profile': 'b', 'workspace_id': '../../escape'})
        artifact = uuid.uuid4().hex;(self.root / 'data/artifacts').mkdir(exist_ok=True);(self.root / 'data/artifacts' / artifact).write_text('output')
        self.event('tool_output', {'artifact_id': artifact})
        self.assertEqual(self.delete()['private_clones_removed'], 1)
        self.assertFalse((profile_paths('b')[1] / mine).exists())
        self.assertTrue((profile_paths('b')[1] / other).is_dir())  # another project's clone stays
        self.assertFalse((self.root / 'data/artifacts' / artifact).exists())

    def test_workspace_removal_never_follows_a_symlink(self):
        outside = self.root / 'outside';outside.mkdir();(outside / 'keep.txt').write_text('keep')
        ws = Workspace(self.root / 'links');(ws.root / 'linked').symlink_to(outside, target_is_directory=True)
        ws.remove('linked')
        self.assertFalse((ws.root / 'linked').exists());self.assertEqual((outside / 'keep.txt').read_text(), 'keep')
        with self.assertRaises(ValueError):ws.remove('../outside')


class ProjectDeletionHTTPTests(unittest.TestCase):
    def setUp(self):self.fixture = native_fixture.LocalNativeDashboardTests();self.fixture.setUp();self.client = self.fixture.client
    def tearDown(self):self.fixture.tearDown()

    def test_owner_deletes_through_the_csrf_protected_route(self):
        project = self.fixture.project
        body = {'project_id': project, 'confirm_name': 'Disposable native sample'}
        self.assertEqual(self.client.request('/api/projects/delete', body, {'X-Relay-CSRF': ''})[0], 403)
        self.assertEqual(self.client.request('/api/projects/delete', {**body, 'confirm_name': 'wrong'})[0], 400)
        self.assertTrue(self.client.post('projects/delete', body)['deleted'])
        self.assertNotIn(project, [p['id'] for p in self.client.get('overview?light=1')['projects']])
        self.assertEqual(self.client.request('/api/projects/' + project)[0], 403)


if __name__ == '__main__':
    unittest.main()
