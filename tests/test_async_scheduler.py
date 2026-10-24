"""Exercise live-mode scheduling boundaries with controllable, credential-free adapters.

The fake adapters perform real temporary worktree writes, but never call a provider.
They model an in-flight tool which must stop before another worker can write.
"""
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from relay.store import Relay
from relay.workers import ExecutionResult, get_adapter


class WriterTracker:
    def __init__(self):
        self.guard = threading.Lock()
        self.active = 0
        self.maximum = 0
        self.started = []

    def enter(self, worker):
        with self.guard:
            self.active += 1
            self.maximum = max(self.maximum, self.active)
            self.started.append(worker)

    def leave(self):
        with self.guard:
            self.active -= 1


class ControlledAdapter:
    capabilities = {'coding': True, 'simulation': False, 'sandbox': 'test_fake_only'}

    def __init__(self, worker, tracker):
        self.worker = worker
        self.tracker = tracker
        self.entered = threading.Event()
        self.release_tool = threading.Event()
        self.stopped = threading.Event()
        self.cancel_called = threading.Event()
        self.block = False
        self.outcome = 'completed'
        self.error = None
        self.safe_to_release = True
        self.recovery_confirmed = True
        self.recover_calls = []

    def health(self):
        return {'available': True, 'connected': False, 'reason': 'Controllable test adapter; no provider call'}

    def recover(self, task_id):
        self.recover_calls.append(task_id)
        return self.recovery_confirmed

    def checkpoint(self, workspace):
        return {'managed_by': 'test_scheduler'}

    def cancel(self, task_id):
        self.cancel_called.set()

    def start(self, task, workspace, context, emit, cancel):
        self.tracker.enter(self.worker)
        try:
            emit('tool_invocation', f'{self.worker} test tool entered', {'test_fake': True})
            (workspace / f'{self.worker}.txt').write_text('Tool change before safe stop\n')
            self.entered.set()
            if self.block:
                # Cancellation requests don't instantly terminate this simulated
                # external tool. Its explicit safe boundary must be reached.
                if not self.release_tool.wait(30):
                    raise RuntimeError('Test tool did not reach its safe boundary')
            if self.error:
                raise RuntimeError(self.error)
            if cancel.is_set():
                emit('tool_output', 'Test tool stopped safely; file change retained', {'interrupted': True})
                return ExecutionResult('cancelled', 'Stopped at safe boundary', safe_to_release=self.safe_to_release)
            emit('tool_output', f'{self.worker} test tool completed', {'test_fake': True})
            emit('response', f'{self.worker} completed test execution', {'context_received': context})
            return ExecutionResult(self.outcome, 'Controlled adapter outcome', 'native-test-thread', self.safe_to_release)
        finally:
            self.tracker.leave()
            self.stopped.set()

    resume = start


class AsyncSchedulerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tracker = WriterTracker()
        self.adapters = {}

        def factory(worker):
            if worker['execution_mode'] != 'organization_api':
                return get_adapter(worker)
            if worker['id'] not in self.adapters:
                self.adapters[worker['id']] = ControlledAdapter(worker['id'], self.tracker)
            return self.adapters[worker['id']]

        self.adapter_patch = patch('relay.store.get_adapter', side_effect=factory)
        self.adapter_patch.start()
        self.relay = Relay(self.tmp.name)
        self.conversation = self.relay.detail('alice', 'demo')['conversation']['id']
        self.a = self.enroll('Controlled API A')
        self.b = self.enroll('Controlled API B')

    def tearDown(self):
        # Release all fake tools before close so a failing assertion cannot leave
        # a thread writing after its SQLite connection/temp directory is closed.
        for adapter in self.adapters.values():
            adapter.release_tool.set()
        self.relay.close()
        self.adapter_patch.stop()
        self.tmp.cleanup()

    def enroll(self, name):
        result = self.relay.mutate('admin', 'workers/enroll', {
            'display_name': name, 'owner_type': 'org',
            'execution_mode': 'organization_api', 'opt_in': True,
            'project_ids': ['demo'], 'max_parallel_tasks': 1,
        })
        return result['id']

    def start(self):
        return self.relay.mutate('alice', 'prompt', {
            'conversation_id': self.conversation, 'worker_id': self.a,
            'prompt': 'Perform a controlled scheduler test in this approved workspace.',
        })

    def wait_for(self, task, state, timeout=30):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self.relay.lock:
                current = self.relay.task('alice', task['id'])
                if current['state'] == state and task['id'] not in self.relay.jobs:
                    return current
            time.sleep(0.01)
        self.fail(f'Task did not settle in {state}: {self.relay.task("alice", task["id"])}')

    def test_cancel_waits_for_tool_stop_then_releases_writer_lease(self):
        adapter = self.adapters[self.a]
        adapter.block = True
        task = self.start()
        self.assertTrue(adapter.entered.wait(15))
        stopping = self.relay.mutate('alice', 'cancel', {'task_id': task['id']})
        self.assertEqual(stopping['state'], 'pausing')
        self.assertTrue(adapter.cancel_called.is_set())
        self.assertFalse(adapter.stopped.is_set())
        self.assertEqual(len(self.relay.rows('SELECT * FROM workspace_leases WHERE task_id=?', (task['id'],))), 1)
        adapter.release_tool.set()
        cancelled = self.wait_for(task, 'cancelled')
        self.assertTrue(adapter.stopped.is_set())
        self.assertEqual(cancelled['conversation_id'], self.conversation)
        self.assertFalse(self.relay.rows('SELECT * FROM workspace_leases WHERE task_id=?', (task['id'],)))
        self.assertTrue((Path(self.tmp.name) / 'workspaces/demo' / f'{self.a}.txt').exists())
        assignments = self.relay.rows('SELECT * FROM assignments WHERE task_id=?', (task['id'],))
        self.assertEqual(assignments[0]['status'], 'cancelled')
        self.assertEqual(self.tracker.maximum, 1)

    def test_switch_waits_for_safe_boundary_and_retries_do_not_start_two_writers(self):
        first = self.adapters[self.a]
        second = self.adapters[self.b]
        first.block = True
        task = self.start()
        self.assertTrue(first.entered.wait(15))
        args = {'task_id': task['id'], 'generation': task['generation'], 'target_worker_id': self.b}
        pending = self.relay.mutate('alice', 'switch', args)
        repeated = self.relay.mutate('alice', 'switch', args)
        self.assertEqual(pending['state'], 'pausing')
        self.assertEqual(repeated['state'], 'pausing')
        self.assertFalse(second.entered.is_set())
        self.assertEqual(self.tracker.active, 1)
        first.release_tool.set()
        complete = self.wait_for(task, 'completed')
        self.assertEqual(complete['worker_id'], self.b)
        self.assertEqual(complete['conversation_id'], self.conversation)
        self.assertEqual(self.tracker.maximum, 1)
        self.assertEqual(self.tracker.started, [self.a, self.b])
        self.assertEqual(len(self.relay.rows('SELECT * FROM handoffs WHERE task_id=?', (task['id'],))), 1)
        self.assertEqual(len(self.relay.rows('SELECT * FROM assignments WHERE task_id=?', (task['id'],))), 2)
        self.assertFalse(self.relay.rows('SELECT * FROM workspace_leases WHERE task_id=?', (task['id'],)))
        detail = self.relay.detail('alice', 'demo')
        self.assertIn(f'{self.a}.txt', detail['workspace']['files'])
        self.assertIn(f'{self.b}.txt', detail['workspace']['files'])
        self.assertIn('HANDOFF.md', '\n'.join(detail['workspace']['files']))

    def test_owner_opt_out_waits_for_safe_stop_then_reassigns(self):
        first = self.adapters[self.a]
        second = self.adapters[self.b]
        first.block = True
        task = self.start()
        self.assertTrue(first.entered.wait(15))
        self.relay.mutate('admin', 'workers/configure', {
            'worker_id': self.a, 'opt_in': False,
        })
        self.assertEqual(self.relay.task('alice', task['id'])['state'], 'pausing')
        self.assertTrue(first.cancel_called.is_set())
        self.assertFalse(second.entered.is_set())
        first.release_tool.set()
        complete = self.wait_for(task, 'completed')
        self.assertEqual(complete['worker_id'], self.b)
        public = self.relay.workers('admin', project='demo')['workers']
        opted_out = next(w for w in public if w['id'] == self.a)
        self.assertFalse(opted_out['opt_in'])
        self.assertFalse(opted_out['eligible'])
        self.assertEqual(self.tracker.maximum, 1)
        handoff = self.relay.detail('alice', 'demo')['handoffs'][-1]
        self.assertEqual(handoff['authorized_by'], 'admin')
        self.assertEqual(handoff['reason_code'], 'worker_opt_out')

    def test_provider_limit_blocks_peers_with_shared_quota_and_preserves_pending_work(self):
        adapter = self.adapters[self.a]
        adapter.block = True
        adapter.outcome = 'quota_blocked'
        task = self.start()
        self.assertTrue(adapter.entered.wait(15))
        adapter.release_tool.set()
        current = self.wait_for(task, 'handoff_pending')
        self.assertEqual(current['conversation_id'], self.conversation)
        capacity = self.relay.rows('SELECT * FROM capacity_sources WHERE worker_id IN (?,?)', (self.a, self.b))
        self.assertEqual(len(capacity), 2)
        self.assertTrue(all(c['availability'] == 'quota_blocked' for c in capacity))
        self.assertTrue(all(c['source'] == 'provider_reported' for c in capacity))
        self.assertEqual({c['quota_scope'] for c in capacity}, {'organization:orgdemo'})
        self.assertFalse(self.adapters[self.b].entered.is_set())
        self.assertEqual(len(self.relay.rows('SELECT * FROM assignments WHERE task_id=?', (task['id'],))), 1)
        self.assertFalse(self.relay.rows('SELECT * FROM workspace_leases WHERE task_id=?', (task['id'],)))
        self.assertTrue(self.relay.detail('alice', 'demo')['checkpoints'])
        self.assertTrue((Path(self.tmp.name) / 'workspaces/demo' / f'{self.a}.txt').exists())

    def test_shutdown_does_not_spawn_pending_replacement_worker(self):
        first = self.adapters[self.a]
        second = self.adapters[self.b]
        first.block = True
        second.block = True
        task = self.start()
        self.assertTrue(first.entered.wait(15))
        pending = self.relay.mutate('alice', 'switch', {
            'task_id': task['id'], 'generation': task['generation'],
            'target_worker_id': self.b,
        })
        self.assertEqual(pending['state'], 'pausing')
        errors = []

        def shutdown():
            try:
                self.relay.close()
            except Exception as exc:
                errors.append(exc)

        thread = threading.Thread(target=shutdown)
        thread.start()
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and not self.relay.closing:
            time.sleep(0.01)
        self.assertTrue(self.relay.closing)
        first.release_tool.set()
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertFalse(second.entered.is_set())
        self.assertEqual(self.tracker.started, [self.a])
        # Reopening must expose saved pending work, without a replacement
        # assignment that outlived the server which authorized it.
        self.relay = Relay(self.tmp.name)
        current = self.relay.task('alice', task['id'])
        self.assertEqual(current['state'], 'handoff_pending')
        self.assertFalse(self.relay.rows('SELECT * FROM workspace_leases WHERE task_id=?', (task['id'],)))
        self.assertEqual(len(self.relay.rows('SELECT * FROM assignments WHERE task_id=?', (task['id'],))), 1)
        self.assertFalse(second.entered.is_set())
        self.assertTrue((Path(self.tmp.name) / 'workspaces/demo' / f'{self.a}.txt').exists())

    def test_unexpected_adapter_error_does_not_release_unverified_container(self):
        adapter = self.adapters[self.a]
        adapter.block = True
        adapter.error = 'Unexpected adapter failure before a cleanup result'
        adapter.recovery_confirmed = False
        task = self.start()
        self.assertTrue(adapter.entered.wait(15))
        adapter.release_tool.set()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            with self.relay.lock:
                current = self.relay.task('alice', task['id'])
                if current['state'] == 'pausing' and task['id'] in self.relay.unsafe_tasks:
                    break
            time.sleep(0.01)
        else:
            self.fail('Adapter exception released a writer without verified cleanup')
        self.assertIn(task['id'], adapter.recover_calls)
        self.assertEqual(len(self.relay.rows('SELECT * FROM workspace_leases WHERE task_id=?', (task['id'],))), 1)
        self.assertFalse(self.adapters[self.b].entered.is_set())
        with self.assertRaisesRegex(ValueError, '(?i)cleanup'):
            self.relay.mutate('alice', 'switch', {
                'task_id': task['id'], 'generation': current['generation'], 'target_worker_id': self.b,
            })
        self.assertEqual(self.tracker.started, [self.a])

    def test_unverified_cleanup_retains_lease_across_restart_until_recovery_is_confirmed(self):
        first = self.adapters[self.a]
        first.block = True
        first.safe_to_release = False
        first.recovery_confirmed = False
        task = self.start()
        self.assertTrue(first.entered.wait(15))
        first.release_tool.set()
        # A finished adapter thread can still represent a container whose
        # shutdown wasn't confirmed. This differs from a normal safe stop.
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            with self.relay.lock:
                current = self.relay.task('alice', task['id'])
                if current['state'] == 'pausing' and task['id'] in self.relay.unsafe_tasks:
                    break
            time.sleep(0.01)
        else:
            self.fail('Unverified cleanup was not marked unsafe')
        self.assertTrue(first.stopped.is_set())
        original_lease = self.relay.rows('SELECT * FROM workspace_leases WHERE task_id=?', (task['id'],))
        self.assertEqual(len(original_lease), 1)
        self.assertFalse(self.adapters[self.b].entered.is_set())
        for method, fields in [
            ('switch', {'generation': current['generation'], 'target_worker_id': self.b}),
            ('cancel', {}),
        ]:
            with self.assertRaisesRegex(ValueError, '(?i)cleanup'):
                self.relay.mutate('alice', method, {'task_id': task['id'], **fields})
        self.assertEqual(self.tracker.started, [self.a])
        self.relay.close()
        self.relay = Relay(self.tmp.name)
        current = self.relay.task('alice', task['id'])
        self.assertEqual(current['state'], 'pausing')
        self.assertIn(task['id'], self.relay.unsafe_tasks)
        self.assertIn(task['id'], first.recover_calls)
        self.assertEqual(self.relay.rows('SELECT * FROM workspace_leases WHERE task_id=?', (task['id'],)), original_lease)
        self.assertEqual(self.tracker.started, [self.a])
        self.assertFalse(self.adapters[self.b].entered.is_set())
        # Only independently confirmed cleanup on a subsequent startup may
        # release the old lease and permit an explicitly requested new writer.
        first.recovery_confirmed = True
        self.relay.close()
        self.relay = Relay(self.tmp.name)
        current = self.relay.task('alice', task['id'])
        self.assertEqual(current['state'], 'handoff_pending')
        self.assertFalse(self.relay.rows('SELECT * FROM workspace_leases WHERE task_id=?', (task['id'],)))
        resumed = self.relay.mutate('alice', 'switch', {
            'task_id': task['id'], 'generation': current['generation'],
            'target_worker_id': self.b, 'reason_code': 'retry',
        })
        complete = self.wait_for(resumed, 'completed')
        self.assertEqual(complete['worker_id'], self.b)
        self.assertEqual(self.tracker.maximum, 1)
        self.assertEqual(self.tracker.started, [self.a, self.b])

    def test_tool_exception_records_error_and_keeps_partial_files_for_recovery(self):
        adapter = self.adapters[self.a]
        adapter.block = True
        adapter.error = 'controlled provider/tool failure'
        task = self.start()
        self.assertTrue(adapter.entered.wait(15))
        adapter.release_tool.set()
        current = self.wait_for(task, 'handoff_pending')
        self.assertIn('controlled provider/tool failure', current['last_error'])
        full = self.relay.events('alice', self.conversation, limit=100)['events']
        self.assertTrue(any(e['event_type'] == 'error' and 'controlled provider/tool failure' in e['content'] for e in full))
        self.assertFalse(self.relay.rows('SELECT * FROM workspace_leases WHERE task_id=?', (task['id'],)))
        self.assertTrue((Path(self.tmp.name) / 'workspaces/demo' / f'{self.a}.txt').exists())
        resumed = self.relay.mutate('alice', 'switch', {
            'task_id': task['id'], 'generation': current['generation'],
            'target_worker_id': self.b, 'reason_code': 'retry',
        })
        complete = self.wait_for(resumed, 'completed')
        self.assertEqual(complete['worker_id'], self.b)
        self.assertEqual(complete['conversation_id'], self.conversation)
        self.assertEqual(self.tracker.maximum, 1)


if __name__ == '__main__':
    unittest.main()
