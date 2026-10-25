"""An idle agent within its usage limits can take a task directly."""
from pathlib import Path
import json
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from relay.native import NativeCodexAdapter, close_native_sessions, profile_paths, session_for, usage_reason, usage_snapshot
from relay.store import Relay
import test_local_native_dashboard as native_fixture


def limits(five_hour=10, weekly=20, allowed=None):
    stamp = int(time.time())
    return {'rateLimits': {'primary': {'usedPercent': five_hour, 'windowDurationMins': 300, 'resetsAt': stamp + 3600},
                           'secondary': {'usedPercent': weekly, 'windowDurationMins': 10080, 'resetsAt': stamp + 86400}},
            'ordinaryUsageAllowed': allowed}


class UsageSnapshotTests(unittest.TestCase):
    def test_windows_are_identified_by_duration(self):
        swapped = limits(five_hour=40, weekly=70)
        swapped['rateLimits'] = {'primary': swapped['rateLimits']['secondary'], 'secondary': swapped['rateLimits']['primary']}
        usage = usage_snapshot(swapped)
        self.assertEqual(usage['windows']['five_hour']['used_percent'], 40)
        self.assertEqual(usage['windows']['weekly']['used_percent'], 70)
        self.assertFalse(usage['blocked'])

    def test_provider_verdict_decides_and_exhausted_windows_block_when_unknown(self):
        self.assertTrue(usage_snapshot(limits(allowed=False))['blocked'])
        self.assertFalse(usage_snapshot(limits(five_hour=100, allowed=True))['blocked'])
        unknown = usage_snapshot(limits(weekly=100))
        self.assertTrue(unknown['blocked']);self.assertEqual(unknown['exhausted'], ['weekly'])
        self.assertIn('weekly limit', usage_reason(unknown));self.assertIn('Resets', usage_reason(unknown))
        self.assertFalse(usage_snapshot({'rateLimits': {}})['blocked'])


class NativeHealthTests(unittest.TestCase):
    """The profile session lock is shared by readiness checks and execution."""
    worker = {'native_profile': 'a', 'owner_type': 'user', 'owner_id': 'alice', 'state': 'available',
              '_native_enabled': True, '_native_owner_id': 'alice'}

    def setUp(self):
        close_native_sessions()
        self.session = session_for('a');self.usage = None
        patchers = [patch('relay.native.native_host', return_value=True)]
        for name in ('connect', 'permissions', 'probe', 'auth'):patchers.append(patch.object(self.session, name, return_value=None))
        patchers.append(patch.object(self.session, 'limits', side_effect=lambda fresh=False: self.usage))
        for patcher in patchers:patcher.start();self.addCleanup(patcher.stop)

    def tearDown(self):close_native_sessions()

    def health(self, state='available'):return NativeCodexAdapter({**self.worker, 'state': state}).health()

    def test_a_running_readiness_check_is_waited_for_not_reported_busy(self):
        self.session.lock.acquire()
        threading.Timer(0.3, self.session.lock.release).start()
        self.assertEqual(self.health()['status'], 'available')

    def test_an_executing_task_reports_busy_immediately(self):
        self.session.executing = True
        try:
            started = time.monotonic()
            self.assertEqual(self.health()['status'], 'busy')
            self.assertLess(time.monotonic() - started, 1)
        finally:self.session.executing = False

    def test_usage_limits_gate_and_only_the_provider_lifts_a_block(self):
        self.usage = usage_snapshot(limits(five_hour=100))
        health = self.health()
        self.assertEqual(health['status'], 'quota_blocked');self.assertIn('5-hour limit', health['reason'])
        self.usage = usage_snapshot(limits())
        self.assertTrue(self.health()['available'])
        self.assertEqual(self.health('quota_blocked')['status'], 'quota_blocked')  # provider verdict unknown
        self.usage = None
        self.assertEqual(self.health('quota_blocked')['status'], 'quota_blocked')
        self.usage = usage_snapshot(limits(allowed=True))
        recovered = self.health('quota_blocked')
        self.assertTrue(recovered['available']);self.assertTrue(recovered['quota_recovered'])


class CapacityStateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory();self.root = Path(self.temp.name)
        self.relay = Relay(self.root / 'data')

    def tearDown(self):self.relay.close();self.temp.cleanup()

    def worker(self):return self.relay.one('SELECT * FROM workers WHERE id=?', ('codexA',))

    def test_provider_block_lifts_only_on_provider_recovery_and_owner_block_stays(self):
        self.relay._capacity('codexA', 'quota_blocked', source='provider_reported')
        eligible, reason = self.relay.eligibility(self.worker(), 'demo', 'alice', {'available': True})
        self.assertFalse(eligible);self.assertIn('quota_blocked', reason)
        recovered = {'available': True, 'quota_recovered': True, 'usage': {'allowed': True}}
        self.assertEqual(self.relay.eligibility(self.worker(), 'demo', 'alice', recovered), (True, 'Approved for this project'))
        self.assertEqual(self.worker()['state'], 'available')
        self.assertTrue(self.relay.rows("SELECT 1 FROM audit_events WHERE action='worker_quota_recovered'"))
        self.relay.configure('alice', {'worker_id': 'codexA', 'state': 'quota_blocked'})
        self.assertFalse(self.relay.eligibility(self.worker(), 'demo', 'alice', recovered)[0])
        self.assertEqual(self.worker()['state'], 'quota_blocked')

    def test_dashboard_keeps_the_last_readiness_while_a_check_refreshes(self):
        w = self.worker();release = threading.Event()
        with patch.object(self.relay, 'adapter') as adapter:
            adapter.return_value.health.return_value = {'available': True, 'status': 'available'}
            self.assertEqual(self.relay.worker_health(w, cached=True)['status'], 'checking')  # nothing known yet
            while self.relay.health_jobs:time.sleep(.01)
            key = next(iter(self.relay.health_cache))
            self.relay.health_cache[key] = (time.monotonic() - 60, self.relay.health_cache[key][1])
            adapter.return_value.health.side_effect = lambda: release.wait(5) and {'available': True, 'status': 'available'}
            self.assertEqual(self.relay.worker_health(w, cached=True)['status'], 'available')
            release.set()

    def test_handing_off_leaves_the_previous_agent_available(self):
        c = self.relay.detail('alice', 'demo')['conversation']['id']
        task = self.relay.mutate('alice', 'prompt', {'conversation_id': c, 'prompt': 'Build a password validator'})
        self.assertEqual(task['worker_id'], 'codexA')
        self.relay.mutate('alice', 'switch', {'task_id': task['id'], 'generation': task['generation'], 'target_worker_id': 'codexB'})
        self.assertEqual(self.worker()['state'], 'available')

    def test_restart_restores_agents_relay_paused_but_keeps_owner_pauses(self):
        self.relay._capacity('codexA', 'paused')  # what a handoff used to do
        self.relay.configure('bob', {'worker_id': 'codexB', 'state': 'paused'})
        self.relay.close();self.relay = Relay(self.root / 'data')
        self.assertEqual(self.worker()['state'], 'available')
        self.assertEqual(self.relay.one('SELECT state FROM workers WHERE id=?', ('codexB',))['state'], 'paused')
        self.assertTrue(self.relay.rows("SELECT 1 FROM audit_events WHERE action='worker_unpaused' AND worker_id='codexA'"))

    def test_backend_restart_leaves_the_interrupted_worker_free_for_new_work(self):
        task = self.relay.mutate('alice', 'tasks/assign', {'project_id': 'demo', 'worker_id': 'codexA', 'prompt': 'Build a password validator', 'queue': True})
        self.relay.db.execute("UPDATE tasks SET state='running',worker_id='codexA' WHERE id=?", (task['id'],))
        self.relay.db.execute("INSERT INTO assignments(id,task_id,worker_id,session_id,created_at,authorized_by,status) VALUES('a1',?,'codexA','s','now','alice','active')", (task['id'],))
        self.relay._capacity('codexA', 'busy')
        self.relay.close();self.relay = Relay(self.root / 'data')
        self.assertEqual(self.relay.one('SELECT state FROM tasks WHERE id=?', (task['id'],))['state'], 'handoff_pending')
        self.assertEqual(self.worker()['state'], 'available')
        self.relay.configure('alice', {'worker_id': 'codexA', 'state': 'offline'})
        self.relay.db.execute("UPDATE tasks SET state='running' WHERE id=?", (task['id'],))
        self.relay.close();self.relay = Relay(self.root / 'data')
        self.assertEqual(self.worker()['state'], 'offline')  # an owner's decision survives


class IdleAgentHTTPTests(unittest.TestCase):
    def setUp(self):
        self.fixture = native_fixture.LocalNativeDashboardTests();self.fixture.setUp()
        self.client = self.fixture.client;self.project = self.fixture.project

    def tearDown(self):self.fixture.tearDown()

    def assign(self, worker):
        return self.client.post('tasks/assign', {'project_id': self.project, 'worker_id': worker, 'prompt': native_fixture.OBJECTIVE})

    def test_assignment_during_a_background_readiness_check_starts_directly(self):
        worker = self.fixture.enroll('b');session = session_for('b')
        session.lock.acquire()
        threading.Timer(0.5, session.lock.release).start()
        task = self.assign(worker)
        self.assertEqual(task['worker_id'], worker)
        self.assertEqual(self.fixture.wait(task, 'completed')['worker_id'], worker)

    def test_usage_limited_agent_takes_work_again_once_the_provider_allows_it(self):
        close_native_sessions();home, _ = profile_paths('a')
        (home / 'scenario.json').write_text(json.dumps({'quota_once': True, 'complete': True, 'limits': {}}))
        worker = self.fixture.enroll('a')
        first = self.assign(worker);pending = self.fixture.wait(first, 'handoff_pending')
        status = {p['profile']: p for p in self.client.get('native/profiles')['profiles']}['a']
        self.assertEqual(status['status'], 'quota_blocked');self.assertEqual(status['usage']['windows']['five_hour']['used_percent'], 100)
        self.client.post('cancel', {'task_id': first['id'], 'generation': pending['generation']})
        self.fixture.wait(first, 'cancelled')
        status, body, _ = self.client.request('/api/tasks/assign', {'project_id': self.project, 'worker_id': worker, 'prompt': native_fixture.OBJECTIVE})
        self.assertEqual(status, 409);self.assertIn('quota_blocked', body['error'])
        (home / 'scenario.json').write_text(json.dumps({'quota_once': True, 'complete': True, 'limits': {'allowed': True, 'five_hour': 3}}))
        second = self.assign(worker)
        self.assertEqual(self.fixture.wait(second, 'completed')['worker_id'], worker)
        self.assertEqual(self.fixture.server.relay.one('SELECT state FROM workers WHERE id=?', (worker,))['state'], 'available')


if __name__ == '__main__':
    unittest.main()
