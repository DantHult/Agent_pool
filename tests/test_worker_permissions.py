"""Ownership, capacity, and handoff regressions beyond the original demo suite."""
import concurrent.futures
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from relay.store import Relay

PROMPT = 'Add a password-strength validator and tests to this sample project.'


class WorkerPermissionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.relay = Relay(self.tmp.name)
        self.conversation = self.relay.detail('alice', 'demo')['conversation']['id']

    def tearDown(self):
        self.relay.db.close()
        self.tmp.cleanup()

    def enroll(self, user='alice', **fields):
        data = {
            'display_name': 'Independent demo worker',
            'execution_mode': 'simulation',
            'owner_type': 'user',
            'project_ids': [],
            'opt_in': True,
            'max_parallel_tasks': 1,
            **fields,
        }
        return self.relay.mutate(user, 'workers/enroll', data)

    def start(self, worker_id='codexA', conversation=None):
        return self.relay.mutate('alice', 'prompt', {
            'conversation_id': conversation or self.conversation,
            'prompt': PROMPT,
            'worker_id': worker_id,
        })

    def worker(self, worker_id, user='alice', project='demo'):
        workers = self.relay.workers(user, project=project)['workers']
        return next(w for w in workers if w['id'] == worker_id)

    def test_nonshared_conversation_and_other_owners_reference_are_private(self):
        private = self.relay.detail('bob', 'private')['conversation']['id']
        with self.assertRaises(PermissionError):
            self.relay.detail('alice', 'private')
        with self.assertRaises(PermissionError):
            self.relay.events('alice', private)
        # It is a reference, not a real credential. Even references must not leak.
        self.relay.db.execute(
            'UPDATE workers SET credential_ref=? WHERE id=?',
            ('env:RELAY_SAM_PRIVATE_REFERENCE', 'codexB'),
        )
        public = json.dumps({
            'overview': self.relay.overview('alice'),
            'workers': self.relay.workers('alice', project='demo'),
            'project': self.relay.detail('alice', 'demo'),
        })
        self.assertNotIn('RELAY_SAM_PRIVATE_REFERENCE', public)
        self.assertNotIn('credential_ref', public)
        mine = self.relay.workers('alice', mine=True)['workers']
        self.assertTrue(all(w['owner_type'] == 'user' and w['owner_id'] == 'alice' for w in mine))

    def test_project_membership_does_not_approve_another_members_worker(self):
        project = self.relay.mutate('alice', 'projects', {'name': 'Explicitly approved workspace'})['id']
        self.relay.mutate('admin', 'membership', {'project_id': project, 'user_id': 'bob'})
        conversation = self.relay.detail('alice', project)['conversation']['id']
        candidates = self.relay.workers('alice', project=project)['workers']
        b = next((w for w in candidates if w['id'] == 'codexB'), None)
        self.assertTrue(b is None or not b['eligible'])
        with self.assertRaises((PermissionError, ValueError)):
            self.start('codexB', conversation)
        self.relay.mutate('bob', 'workers/configure', {
            'worker_id': 'codexB', 'project_ids': ['demo', 'private', project],
        })
        approved = self.worker('codexB', project=project)
        self.assertTrue(approved['eligible'], approved.get('eligibility_reason'))
        task = self.start('codexB', conversation)
        self.assertEqual(task['worker_id'], 'codexB')

    def test_only_owner_or_admin_can_change_authorization(self):
        for method, fields in [
            ('workers/configure', {'opt_in': False}),
            ('workers/configure', {'project_ids': []}),
            ('workers/revoke', {}),
            ('availability', {'state': 'offline'}),
        ]:
            with self.assertRaises(PermissionError):
                self.relay.mutate('alice', method, {'worker_id': 'codexB', **fields})
        self.relay.mutate('bob', 'workers/configure', {'worker_id': 'codexB', 'opt_in': False})
        self.assertFalse(self.worker('codexB')['opt_in'])
        self.relay.mutate('admin', 'workers/configure', {'worker_id': 'codexB', 'opt_in': True})
        self.assertTrue(self.worker('codexB')['opt_in'])

    def test_enrollment_does_not_accept_subscription_secrets_or_impersonation(self):
        for secret_field in ('token', 'cookie', 'auth_dir', 'api_key', 'credential_ref'):
            with self.subTest(field=secret_field):
                with self.assertRaises((ValueError, PermissionError)):
                    self.enroll(**{secret_field: 'not-a-real-secret'})
        with self.assertRaises((PermissionError, ValueError)):
            self.enroll(owner_id='bob')
        with self.assertRaises(PermissionError):
            self.enroll(owner_type='org', execution_mode='organization_api')

    def test_explicit_revocation_removes_project_eligibility(self):
        self.relay.mutate('bob', 'workers/revoke', {'worker_id': 'codexB', 'project_id': 'demo'})
        b = self.worker('codexB', user='bob')
        self.assertFalse(b['eligible'])
        visible = self.relay.workers('alice', project='demo')['workers']
        self.assertFalse(any(w['id'] == 'codexB' for w in visible))
        with self.assertRaises((PermissionError, ValueError)):
            self.start('codexB')
        task = self.start()
        pending = self.relay.mutate('alice', 'limit', {'task_id': task['id'], 'generation': task['generation']})
        self.assertEqual(pending['state'], 'handoff_pending')
        self.assertEqual(len(self.relay.rows('SELECT * FROM assignments WHERE task_id=?', (task['id'],))), 1)

    def test_opt_out_handoffs_active_work_and_prevents_new_assignments(self):
        task = self.start()
        self.relay.mutate('alice', 'workers/configure', {'worker_id': 'codexA', 'opt_in': False})
        detail = self.relay.detail('alice', 'demo')
        current = next(t for t in detail['tasks'] if t['id'] == task['id'])
        self.assertEqual(current['conversation_id'], self.conversation)
        self.assertNotEqual(current['worker_id'], 'codexA')
        self.assertTrue(detail['handoffs'])
        self.assertFalse(self.worker('codexA')['opt_in'])
        with self.assertRaises((PermissionError, ValueError)):
            self.start('codexA')

    def test_organization_api_workers_share_labeled_quota_scope(self):
        first = self.enroll('admin', display_name='Organization API one',
                            owner_type='org', execution_mode='organization_api', project_ids=['demo'])
        second = self.enroll('admin', display_name='Organization API two',
                             owner_type='org', execution_mode='organization_api', project_ids=['demo'])
        a = self.worker(first['id'])
        b = self.worker(second['id'])
        self.assertEqual(a['execution_mode'], 'organization_api')
        self.assertEqual(a['capacity']['quota_scope'], 'organization:orgdemo')
        self.assertEqual(a['capacity']['quota_scope'], b['capacity']['quota_scope'])
        self.assertNotEqual(a['capacity']['source'], 'simulated')
        self.assertNotIn('remaining_tokens', a['capacity'])
        self.assertIn('shared', json.dumps(a).lower())

    def test_simulated_limit_cannot_be_applied_to_organization_api_task(self):
        task = self.start()
        # Change only persisted mode to exercise the guard without making a paid
        # request or treating a simulated worker as a real API execution.
        self.relay.db.execute('UPDATE tasks SET execution_mode=? WHERE id=?',
                              ('organization_api', task['id']))
        original = self.relay.events('alice', self.conversation, limit=100)['events']
        with self.assertRaisesRegex(ValueError, 'simulated'):
            self.relay.mutate('alice', 'limit', {
                'task_id': task['id'], 'generation': task['generation'],
            })
        self.assertEqual(self.relay.events('alice', self.conversation, limit=100)['events'], original)
        current = self.relay.task('alice', task['id'])
        self.assertEqual(current['state'], 'running')
        self.assertEqual(current['generation'], task['generation'])

    def test_personal_subscription_fails_closed_with_clear_error(self):
        from relay.workers import UnsupportedExecutionMode
        worker = self.enroll(execution_mode='personal_supported', project_ids=['demo'])
        public = self.worker(worker['id'])
        self.assertFalse(public['eligible'])
        self.assertEqual(public['capacity']['availability'], 'unsupported')
        with self.assertRaises(UnsupportedExecutionMode) as raised:
            self.start(worker['id'])
        self.assertEqual(raised.exception.code, 'unsupported_execution_mode')
        self.assertTrue(str(raised.exception))
        self.assertFalse(self.relay.rows('SELECT * FROM assignments WHERE worker_id=?', (worker['id'],)))

    def test_simulated_handoff_preserves_objective_files_history_and_authorization(self):
        task = self.start()
        original = self.relay.events('alice', self.conversation, limit=100)['events']
        completed = self.relay.mutate('alice', 'limit', {'task_id': task['id'], 'generation': task['generation']})
        detail = self.relay.detail('alice', 'demo')
        self.assertEqual(completed['state'], 'completed')
        self.assertEqual(completed['worker_id'], 'codexB')
        self.assertEqual(completed['conversation_id'], self.conversation)
        full = self.relay.events('alice', self.conversation, limit=100)['events']
        self.assertEqual(full[:len(original)], original)
        self.assertIn('password_strength', detail['workspace']['files']['validator.py'])
        self.assertIn('source_range', detail['workspace']['files']['.relay/HANDOFF.md'])
        b_response = next(e for e in full if e['worker_id'] == 'codexB' and e['event_type'] == 'response')
        context = json.loads(b_response['metadata'])['context_received']
        self.assertEqual(context['objective'], PROMPT)
        self.assertEqual(context['conversation_id'], self.conversation)
        self.assertTrue(context['todos'])
        handoff = detail['handoffs'][0]
        self.assertEqual(handoff['authorized_by'], 'alice')
        self.assertTrue(handoff['reason_code'])
        self.assertTrue(detail['checkpoints'])
        source = self.worker('codexA')
        target = self.worker('codexB')
        self.assertEqual(source['capacity']['availability'], 'quota_blocked')
        self.assertEqual(source['capacity']['source'], 'simulated')
        self.assertEqual(target['capacity']['availability'], 'available')
        self.assertNotEqual(source['capacity']['quota_scope'], target['capacity']['quota_scope'])

    def test_restart_retains_context_checkpoints_and_assignments(self):
        task = self.start()
        self.relay.mutate('alice', 'limit', {'task_id': task['id'], 'generation': task['generation']})
        before = self.relay.detail('alice', 'demo')
        self.relay.db.close()
        self.relay = Relay(self.tmp.name)
        after = self.relay.detail('alice', 'demo')
        self.assertEqual(after['conversation'], before['conversation'])
        self.assertEqual(after['contexts'], before['contexts'])
        self.assertEqual(after['assignments'], before['assignments'])
        self.assertEqual(after['checkpoints'], before['checkpoints'])
        self.assertEqual(after['workspace']['files']['validator.py'], before['workspace']['files']['validator.py'])

    def test_cached_task_response_rechecks_revoked_project_access(self):
        args = {'conversation_id': self.conversation, 'prompt': PROMPT,
                'worker_id': 'codexA', 'request_id': 'before-revocation'}
        self.relay.mutate('alice', 'prompt', args)
        self.relay.db.execute('DELETE FROM memberships WHERE user_id=? AND project_id=?',
                              ('alice', 'demo'))
        with self.assertRaises(PermissionError):
            self.relay.mutate('alice', 'prompt', args)

    def test_same_request_id_cannot_be_reused_for_another_operation(self):
        args = {'conversation_id': self.conversation, 'prompt': PROMPT,
                'worker_id': 'codexA', 'request_id': 'fixed-request-id'}
        task = self.relay.mutate('alice', 'prompt', args)
        with self.assertRaisesRegex(ValueError, 'request_id'):
            self.relay.mutate('alice', 'cancel', {
                'task_id': task['id'], 'request_id': 'fixed-request-id',
            })
        self.assertEqual(self.relay.task('alice', task['id'])['state'], 'running')

    def test_rejected_simulated_limit_does_not_change_worker_capacity(self):
        task = self.start()
        complete = self.relay.mutate('alice', 'limit', {
            'task_id': task['id'], 'generation': task['generation'],
        })
        before = self.worker('codexB')['capacity']
        with self.assertRaises(ValueError):
            self.relay.mutate('alice', 'limit', {
                'task_id': task['id'], 'generation': complete['generation'],
            })
        self.assertEqual(self.worker('codexB')['capacity'], before)

    def test_unavailable_api_worker_preserves_general_objective_and_queued_mode(self):
        objective = 'Refactor this project module and add regression tests for its behavior.'
        # Force the optional live runtime off even if the test runner happens to
        # have organization configuration. This test must never make paid calls.
        with patch.dict('os.environ', {'RELAY_ENABLE_ORG_API': '0'}):
            worker = self.enroll('admin', display_name='Unavailable organization API',
                                 owner_type='org', execution_mode='organization_api',
                                 project_ids=['demo'])
            self.relay.mutate('admin', 'workers/configure', {
                'worker_id': worker['id'], 'state': 'offline',
            })
            for fields in ({'worker_id': worker['id']}, {}, {'execution_mode': 'organization_api'}):
                with self.subTest(selection=fields):
                    task = self.relay.mutate('alice', 'prompt', {
                        'conversation_id': self.conversation, 'prompt': objective, **fields,
                    })
                    self.assertEqual(task['state'], 'queued')
                    self.assertEqual(task['execution_mode'], 'organization_api')
                    self.assertEqual(task['objective'], objective)
                    self.assertIsNone(task['worker_id'])
                    self.assertFalse(self.relay.rows('SELECT * FROM assignments WHERE task_id=?', (task['id'],)))
                    events = self.relay.rows('SELECT * FROM events WHERE task_id=? ORDER BY sequence', (task['id'],))
                    self.assertTrue(any(e['event_type'] == 'prompt' and e['content'] == objective for e in events))
                    self.assertFalse(any(e['event_type'] == 'tool_invocation' for e in events))
                    self.relay.mutate('alice', 'cancel', {'task_id': task['id']})
            with self.assertRaisesRegex(ValueError, 'Simulated'):
                self.relay.mutate('alice', 'prompt', {
                    'conversation_id': self.conversation, 'prompt': objective,
                    'execution_mode': 'simulation',
                })

    def test_worker_owner_removed_from_org_loses_eligibility_despite_project_grant(self):
        self.assertTrue(self.worker('codexB')['eligible'])
        self.relay.db.execute('DELETE FROM organization_memberships WHERE organization_id=? AND user_id=?',
                              ('orgdemo', 'bob'))
        self.assertTrue(self.relay.rows('SELECT 1 FROM worker_projects WHERE worker_id=? AND project_id=?',
                                       ('codexB', 'demo')))
        self.assertTrue(self.relay.rows('SELECT 1 FROM memberships WHERE user_id=? AND project_id=?',
                                       ('bob', 'demo')))
        worker = self.worker('codexB')
        self.assertFalse(worker['eligible'])
        self.assertIn('organization', worker['eligibility_reason'].lower())
        with self.assertRaises(PermissionError):
            self.start('codexB')
        self.assertFalse(self.relay.rows('SELECT * FROM assignments WHERE worker_id=?', ('codexB',)))

    def test_no_candidate_queues_then_starts_when_approved_worker_is_available(self):
        for owner, worker in [('alice', 'codexA'), ('bob', 'codexB')]:
            self.relay.mutate(owner, 'workers/configure', {'worker_id': worker, 'opt_in': False})
        task = self.relay.mutate('alice', 'prompt', {
            'conversation_id': self.conversation, 'prompt': PROMPT,
        })
        self.assertEqual(task['state'], 'queued')
        self.assertIsNone(task['worker_id'])
        self.assertFalse(self.relay.rows('SELECT * FROM assignments WHERE task_id=?', (task['id'],)))
        self.assertFalse((Path(self.tmp.name) / 'workspaces/demo/validator.py').exists())
        self.relay.mutate('bob', 'workers/configure', {'worker_id': 'codexB', 'opt_in': True})
        running = self.relay.mutate('alice', 'switch', {
            'task_id': task['id'], 'generation': task['generation'], 'target_worker_id': 'codexB',
        })
        self.assertEqual(running['state'], 'running')
        self.assertEqual(running['worker_id'], 'codexB')
        self.assertTrue((Path(self.tmp.name) / 'workspaces/demo/validator.py').exists())
        finished = self.relay.mutate('alice', 'continue', {'task_id': task['id']})
        self.assertEqual(finished['state'], 'completed')
        self.assertEqual(finished['conversation_id'], self.conversation)

    def test_checkpoint_failure_leaves_pending_task_and_worktree_for_retry(self):
        task = self.start()
        file = Path(self.tmp.name) / 'workspaces' / 'demo' / 'validator.py'
        before = file.read_text()
        with patch.object(self.relay.ws, 'checkpoint', side_effect=OSError('simulated disk fault')):
            try:
                self.relay.mutate('alice', 'limit', {'task_id': task['id'], 'generation': task['generation']})
            except OSError:
                pass
        recovered = self.relay.task('alice', task['id'])
        self.assertEqual(recovered['state'], 'handoff_pending')
        self.assertEqual(file.read_text(), before)
        full = self.relay.events('alice', self.conversation, limit=100)['events']
        self.assertTrue(any(e['event_type'] == 'error' for e in full))
        resumed = self.relay.mutate('alice', 'switch', {
            'task_id': task['id'], 'generation': recovered['generation'], 'reason_code': 'retry',
        })
        self.assertEqual(resumed['state'], 'completed')
        self.assertIn('password_strength', file.read_text())

    def test_competing_handoffs_never_overlap_checkpoint_writers(self):
        task = self.start()
        guard = threading.Lock()
        active = 0
        maximum = 0
        original = self.relay.ws.checkpoint

        def observed_checkpoint(*args, **kwargs):
            nonlocal active, maximum
            with guard:
                active += 1
                maximum = max(maximum, active)
            try:
                time.sleep(0.02)
                return original(*args, **kwargs)
            finally:
                with guard:
                    active -= 1

        def handoff(_):
            return self.relay.mutate('alice', 'limit', {'task_id': task['id'], 'generation': task['generation']})

        with patch.object(self.relay.ws, 'checkpoint', side_effect=observed_checkpoint):
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                list(pool.map(handoff, range(2)))
        self.assertEqual(maximum, 1)
        self.assertEqual(len(self.relay.rows('SELECT * FROM assignments WHERE task_id=?', (task['id'],))), 2)
        self.assertEqual(len(self.relay.rows('SELECT * FROM handoffs WHERE task_id=?', (task['id'],))), 1)


class LegacyMigrationTests(unittest.TestCase):
    def test_legacy_transcript_identity_and_worker_names_survive_idempotent_migration(self):
        with tempfile.TemporaryDirectory() as directory:
            db = sqlite3.connect(Path(directory) / 'relay.sqlite')
            db.executescript("""
                CREATE TABLE users(id TEXT PRIMARY KEY,name TEXT,role TEXT);
                CREATE TABLE projects(id TEXT PRIMARY KEY,name TEXT,organization_id TEXT);
                CREATE TABLE memberships(user_id TEXT,project_id TEXT,role TEXT,PRIMARY KEY(user_id,project_id));
                CREATE TABLE conversations(id TEXT PRIMARY KEY,project_id TEXT UNIQUE);
                CREATE TABLE workers(id TEXT PRIMARY KEY,name TEXT,state TEXT,owner_id TEXT);
                CREATE TABLE tasks(id TEXT PRIMARY KEY,conversation_id TEXT,state TEXT,worker_id TEXT,objective TEXT,generation INTEGER DEFAULT 0);
                CREATE TABLE events(event_id TEXT PRIMARY KEY,organization_id TEXT,project_id TEXT,conversation_id TEXT,task_id TEXT,sequence INTEGER,timestamp TEXT,worker_id TEXT,event_type TEXT,role TEXT,content TEXT,metadata TEXT,UNIQUE(conversation_id,sequence));
                CREATE TABLE contexts(id TEXT PRIMARY KEY,task_id TEXT,version INTEGER,data TEXT);
                CREATE TABLE handoffs(id TEXT PRIMARY KEY,task_id TEXT,source TEXT,target TEXT,reason TEXT,checkpoint TEXT,context_version INTEGER,timestamp TEXT,status TEXT);
                CREATE TABLE assignments(id TEXT PRIMARY KEY,task_id TEXT,worker_id TEXT,session_id TEXT,created_at TEXT);
                CREATE TABLE requests(key TEXT PRIMARY KEY,result TEXT);
                INSERT INTO users VALUES('alice','Alex Morgan','engineer'),('bob','Sam Rivera','engineer'),('admin','Jordan Lee','admin');
                INSERT INTO projects VALUES('demo','Preserved project','orgdemo'),('private','Private sandbox','orgdemo');
                INSERT INTO memberships VALUES('alice','demo','engineer'),('bob','demo','engineer'),('bob','private','engineer');
                INSERT INTO conversations VALUES('original-conversation','demo'),('private-conversation','private');
                INSERT INTO workers VALUES('codexA','A custom display name','available','alice'),('codexB','Codex Worker B','available','bob');
                INSERT INTO tasks VALUES('original-task','original-conversation','completed','codexA','Preserved objective',0);
                INSERT INTO events VALUES('original-event','orgdemo','demo','original-conversation','original-task',1,'2026-10-24T00:00:00+00:00','codexA','prompt','user','Original visible conversation','{}');
            """)
            db.commit()
            db.close()
            for _ in range(2):
                relay = Relay(directory)
                try:
                    detail = relay.detail('alice', 'demo')
                    self.assertEqual(detail['conversation']['id'], 'original-conversation')
                    events = relay.events('alice', 'original-conversation')['events']
                    self.assertEqual(len(events), 1)
                    self.assertEqual(events[0]['event_id'], 'original-event')
                    self.assertEqual(events[0]['content'], 'Original visible conversation')
                    workers = relay.workers('alice', project='demo')['workers']
                    a = next(w for w in workers if w['id'] == 'codexA')
                    self.assertEqual(a.get('display_name', a.get('name')), 'A custom display name')
                    self.assertTrue(a['eligible'])
                    self.assertTrue(relay.rows('SELECT * FROM schema_migrations'))
                finally:
                    relay.db.close()


if __name__ == '__main__':
    unittest.main()
