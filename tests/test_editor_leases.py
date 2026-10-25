"""Editor reservation leases: heartbeats, closed tabs, stale recovery and guards."""
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest

from relay.ide import LEASE_SECONDS, ProjectIDE, WorkspaceConflict
from relay.migrations import migrate_reservation_leases
from relay.store import Relay
import test_local_native_dashboard as native_fixture

TAB_A = 'lease-tab-a-0123456789'
TAB_B = 'lease-tab-b-0123456789'
PROMPT = {'project_id': 'demo', 'worker_id': 'codexA', 'prompt': 'Build a password validator'}


class FakeConsole:
    """Stands in for CommandConsole: open command sessions per project."""
    def __init__(self):self.open = set()
    def active(self, project):return project in self.open
    def finish_clean(self, *args):
        if self.open:raise WorkspaceConflict('Stop the command or wait for it to finish before starting the agent')


class LeaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory();self.root = Path(self.temp.name)
        self.relay = Relay(self.root / 'data');self.relay.local_owner_id = 'alice'
        self.ide = ProjectIDE(self.relay, local_owner=True)
        self.console = FakeConsole();self.ide.console = self.console;self.relay.human_commands = self.console.active
        self.c = self.relay.detail('alice', 'demo')['conversation']['id']
        for session in ('session-a', 'session-b'):
            self.relay.db.execute('INSERT INTO sessions VALUES(?,?,?,?,?)', (session, 'alice', time.time(), time.time() + 3600, time.time()))

    def tearDown(self):self.relay.close();self.temp.cleanup()

    def prepare(self, tab=TAB_A, session='session-a'):return self.ide.prepare('alice', 'demo', tab, session, 'Chrome on macOS · tab opened 10:00')
    def lock(self, tab=TAB_B, session='session-b'):return self.ide.state('alice', 'demo', tab, session)['lock']
    def save(self, path, content, tab=TAB_A, session='session-a'):return self.ide.save('alice', 'demo', tab, session, path, None, content, self.c)
    def row(self):return self.relay.one('SELECT * FROM human_workspace_locks WHERE project_id=?', ('demo',))

    def elapse(self, seconds):
        """Age every lease and the process start, as if no browser renewed."""
        self.relay.started -= seconds
        self.relay.db.execute('UPDATE human_workspace_locks SET heartbeat_at=heartbeat_at-? WHERE heartbeat_at>0', (seconds,))

    def test_active_tab_of_same_account_is_never_taken_automatically(self):
        self.prepare()
        with self.assertRaises(WorkspaceConflict):self.prepare(TAB_B, 'session-b')
        with self.assertRaises(WorkspaceConflict):self.prepare(TAB_B, 'session-a')  # duplicated tab, same login
        lock = self.lock()
        self.assertFalse(lock['mine']);self.assertFalse(lock['stale']);self.assertFalse(lock['auto_recoverable'])
        self.assertTrue(lock['recoverable']);self.assertFalse(lock['same_browser'])
        self.assertEqual(lock['label'], 'Chrome on macOS · tab opened 10:00')
        self.assertTrue(self.lock(TAB_B, 'session-a')['same_browser'])
        self.assertLessEqual(lock['expires_in'], LEASE_SECONDS)
        self.assertEqual(self.row()['client_id'], TAB_A)

    def test_heartbeat_keeps_an_idle_tab_alive_and_never_takes_a_lock(self):
        self.prepare();self.elapse(LEASE_SECONDS - 10)
        self.assertTrue(self.ide.heartbeat('alice', 'demo', TAB_A, 'session-a')['held'])
        self.elapse(LEASE_SECONDS - 10)
        with self.assertRaises(WorkspaceConflict):self.prepare(TAB_B, 'session-b')
        other = self.ide.heartbeat('alice', 'demo', TAB_B, 'session-b', 'Other tab')
        self.assertFalse(other['held']);self.assertFalse(other['lock']['mine'])
        self.assertEqual(self.row()['client_id'], TAB_A);self.assertEqual(self.row()['label'], 'Chrome on macOS · tab opened 10:00')

    def test_abandoned_tab_is_recovered_automatically_with_drafts_and_git_state(self):
        self.prepare();self.save('saved.py', 'value=1\n')
        self.assertEqual(self.ide.git('alice', 'demo', TAB_A, 'session-a', ['add', 'saved.py'], self.c)['exit_code'], 0)
        self.ide.draft('alice', 'demo', TAB_A, 'session-a', 'draft.py', None, 'unsaved text')
        self.elapse(LEASE_SECONDS + 1)
        lock = self.lock()
        self.assertTrue(lock['stale']);self.assertTrue(lock['auto_recoverable'])
        recovered = self.prepare(TAB_B, 'session-b')
        self.assertTrue(recovered['lock']['mine'])
        self.assertEqual(recovered['drafts'][0]['content'], 'unsaved text')
        self.assertEqual([(c['path'], c['staged']) for c in recovered['changes'] if c['path'] == 'saved.py'], [('saved.py', True)])
        audit = self.relay.rows("SELECT metadata FROM audit_events WHERE action='editor_lock_recovered'")
        self.assertIn('"automatic": true', audit[-1]['metadata'])
        with self.assertRaises(WorkspaceConflict):self.save('old-tab.py', 'blocked')
        self.assertFalse(self.ide.heartbeat('alice', 'demo', TAB_A, 'session-a')['held'])

    def test_closed_tab_and_ended_login_are_stale_immediately(self):
        self.prepare();self.save('dirty.py', 'value=2\n')
        self.assertEqual(self.ide.detach('alice', 'demo', TAB_A, 'session-a'), {'released': False, 'detached': True})
        lock = self.lock()
        self.assertTrue(lock['stale']);self.assertTrue(lock['closed'])
        self.assertTrue(self.prepare(TAB_B, 'session-b')['lock']['mine'])
        self.relay.db.execute('DELETE FROM sessions WHERE digest=?', ('session-b',))  # signed out elsewhere
        lock = self.lock(TAB_A, 'session-a')
        self.assertTrue(lock['stale']);self.assertTrue(lock['session_ended'])
        self.assertTrue(self.prepare()['lock']['mine'])

    def test_detach_releases_only_clean_reservations(self):
        self.prepare()
        self.assertEqual(self.ide.detach('alice', 'demo', TAB_B, 'session-b'), {'released': False, 'detached': False})
        self.assertEqual(self.ide.detach('alice', 'demo', TAB_A, 'session-a'), {'released': True})
        self.assertIsNone(self.ide.state('alice', 'demo')['lock'])
        self.prepare();self.ide.draft('alice', 'demo', TAB_A, 'session-a', 'draft.py', None, 'kept')
        self.assertTrue(self.ide.detach('alice', 'demo', TAB_A, 'session-a')['detached'])
        self.assertEqual(self.ide.state('alice', 'demo')['drafts'][0]['content'], 'kept')
        self.assertFalse((self.relay.ws.path('demo') / 'draft.py').exists())

    def test_logout_gives_up_only_that_sessions_reservation(self):
        self.prepare();self.save('dirty.py', 'value=3\n')
        self.ide.detach_session('session-b')
        self.assertFalse(self.lock()['stale'])
        self.ide.detach_session('session-a')
        self.assertTrue(self.lock()['closed'])
        self.assertTrue(self.relay.rows('SELECT 1 FROM human_workspace_locks'))

    def test_open_command_session_keeps_the_lease_live(self):
        self.prepare();self.console.open.add('demo');self.elapse(LEASE_SECONDS * 5)
        lock = self.lock()
        self.assertFalse(lock['stale']);self.assertTrue(lock['commands_active']);self.assertFalse(lock['recoverable'])
        with self.assertRaises(WorkspaceConflict):self.prepare(TAB_B, 'session-b')
        with self.assertRaises(WorkspaceConflict):self.ide.acquire('alice', 'demo', TAB_B, 'session-b', recover=True)
        with self.assertRaises(WorkspaceConflict):self.ide.detach('alice', 'demo', TAB_A, 'session-a')
        self.ide.detach_session('session-a')
        self.assertEqual(self.row()['client_id'], TAB_A)
        self.console.open.clear()
        self.assertTrue(self.prepare(TAB_B, 'session-b')['lock']['mine'])

    def test_explicit_takeover_of_live_session_moves_reservation(self):
        self.prepare()
        taken = self.ide.acquire('alice', 'demo', TAB_B, 'session-b', recover=True, label='Safari on macOS')
        self.assertTrue(taken['lock']['mine']);self.assertEqual(self.row()['label'], 'Safari on macOS')
        audit = self.relay.rows("SELECT metadata FROM audit_events WHERE action='editor_lock_recovered'")
        self.assertIn('"automatic": false', audit[-1]['metadata'])
        with self.assertRaises(WorkspaceConflict):self.save('old-tab.py', 'blocked')

    def test_other_users_and_shared_mode_never_auto_recover(self):
        self.prepare();self.elapse(LEASE_SECONDS * 10)
        view = self.ide.state('bob', 'demo', TAB_B, 'bob-session')['lock']
        self.assertFalse(view['recoverable']);self.assertFalse(view['auto_recoverable']);self.assertNotIn('label', view)
        with self.assertRaises(WorkspaceConflict):self.ide.prepare('bob', 'demo', TAB_B, 'bob-session')
        with self.assertRaises(WorkspaceConflict):self.ide.acquire('bob', 'demo', TAB_B, 'bob-session', recover=True)
        shared = ProjectIDE(self.relay)
        with self.assertRaises(WorkspaceConflict):shared.prepare('alice', 'demo', TAB_B, 'session-b')
        self.assertTrue(shared.state('alice', 'demo', TAB_B, 'session-b')['lock']['recoverable'])
        self.assertTrue(shared.acquire('alice', 'demo', TAB_B, 'session-b', recover=True)['lock']['mine'])

    def test_backend_restart_gives_open_tabs_a_lease_before_recovery(self):
        self.prepare();self.elapse(LEASE_SECONDS * 10)
        self.relay.close();self.relay = Relay(self.root / 'data');self.relay.local_owner_id = 'alice'
        self.ide = ProjectIDE(self.relay, local_owner=True)
        self.assertFalse(self.lock()['stale'])
        with self.assertRaises(WorkspaceConflict):self.prepare(TAB_B, 'session-b')
        self.assertTrue(self.prepare()['lock']['mine'])  # the original tab reconnects
        self.elapse(LEASE_SECONDS + 1)
        self.assertTrue(self.prepare(TAB_B, 'session-b')['lock']['mine'])

    def test_stale_reservation_and_open_commands_block_agents_until_recovered(self):
        self.prepare();self.save('human.py', 'human=1\n');self.elapse(LEASE_SECONDS + 1)
        with self.assertRaises(WorkspaceConflict):self.relay.mutate('alice', 'tasks/assign', PROMPT)
        self.prepare(TAB_B, 'session-b')
        finished = self.ide.finish('alice', 'demo', TAB_B, 'session-b', self.c)
        self.assertTrue(finished['released'])
        self.console.open.add('demo')
        with self.assertRaises(WorkspaceConflict):self.relay.mutate('alice', 'tasks/assign', PROMPT)
        self.console.open.clear()
        task = self.relay.mutate('alice', 'tasks/assign', PROMPT)
        self.assertEqual(self.relay.rows('SELECT task_id FROM workspace_leases'), [{'task_id': task['id']}])
        self.assertEqual(self.relay.ws.git(self.relay.ws.path('demo'), 'show', '--name-only', '--format=', finished['checkpoint']), 'human.py')
        with self.assertRaises(WorkspaceConflict):self.prepare(TAB_B, 'session-b')

    def test_upgrade_keeps_existing_reservations_with_unknown_heartbeat(self):
        connection = sqlite3.connect(':memory:', isolation_level=None)
        try:
            connection.execute('CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY,applied_at TEXT)')
            connection.execute('CREATE TABLE human_workspace_locks(project_id TEXT PRIMARY KEY,user_id TEXT NOT NULL,client_id TEXT NOT NULL,session_hash TEXT NOT NULL,created_at TEXT NOT NULL)')
            connection.execute("INSERT INTO human_workspace_locks VALUES('demo','alice','legacy-client-0123456789','legacy','2026-01-01')")
            migrate_reservation_leases(connection);migrate_reservation_leases(connection)
            self.assertEqual(connection.execute('SELECT client_id,heartbeat_at,label FROM human_workspace_locks').fetchone(), ('legacy-client-0123456789', None, None))
        finally:connection.close()


class LeaseHTTPTests(unittest.TestCase):
    def setUp(self):
        self.fixture = native_fixture.LocalNativeDashboardTests();self.fixture.setUp()
        self.first = self.fixture.client
        self.second = native_fixture.Client(self.fixture.server);self.second.login('owner@relay.invalid')
        self.project = self.fixture.project

    def tearDown(self):self.fixture.tearDown()

    def ctx(self, tab):return {'project_id': self.project, 'client_id': tab, 'conversation_id': self.fixture.conversation}

    def test_routes_heartbeat_detach_and_logout_across_browser_sessions(self):
        self.assertEqual(self.first.request('/api/ide/heartbeat', self.ctx(TAB_A), {'X-Relay-CSRF': ''})[0], 403)
        self.assertTrue(self.first.post('ide/prepare', {**self.ctx(TAB_A), 'label': 'Chrome on macOS'})['lock']['mine'])
        self.first.post('ide/save', {**self.ctx(TAB_A), 'path': 'dirty.py', 'content': 'value=4\n', 'version': None})
        self.assertTrue(self.first.post('ide/heartbeat', self.ctx(TAB_A))['held'])
        status, body, _ = self.second.request('/api/ide/prepare', self.ctx(TAB_B))
        self.assertEqual(status, 409)
        lock = self.second.post('ide/heartbeat', self.ctx(TAB_B))['lock']
        self.assertFalse(lock['same_browser']);self.assertEqual(lock['label'], 'Chrome on macOS');self.assertFalse(lock['stale'])
        self.first.post('logout', {})
        lock = self.second.get('ide/state?' + 'project_id=' + self.project + '&client_id=' + TAB_B)['lock']
        self.assertTrue(lock['stale']);self.assertTrue(lock['auto_recoverable'])
        self.assertTrue(self.second.post('ide/prepare', self.ctx(TAB_B))['lock']['mine'])
        self.second.post('ide/git', {**self.ctx(TAB_B), 'arguments': ['add', '.']})
        self.second.post('ide/git', {**self.ctx(TAB_B), 'arguments': ['commit', '-m', 'Recovered work']})
        self.assertEqual(self.second.post('ide/detach', self.ctx(TAB_B)), {'released': True})
        self.assertIsNone(self.second.get('ide/state?project_id=' + self.project)['lock'])


if __name__ == '__main__':
    unittest.main()
