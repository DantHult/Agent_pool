import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

from relay.workers import (OrganizationApiCodexAdapter,
                           PersonalSubscriptionCodexAdapter, SimulationWorkerAdapter,
                           UnsupportedExecutionMode, get_adapter)
from relay.workspace import IMPLEMENTATION

class WorkerAdapterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)
        self.worker = {'id': 'codexA', 'owner_type': 'user', 'owner_id': 'alice', 'execution_mode': 'simulation'}
        self.task = {'id': 'task1', 'conversation_id': 'conversation1', 'objective': 'Add a password-strength validator and tests'}
        self.context = {'version': 2, 'checkpoint': '123456789abcdef', 'todos': ['Add tests'], 'tests': 'not run'}
        self.events = []
        self.emit = lambda kind, content, metadata: self.events.append((kind, content, metadata))
    def test_simulation_executes_real_fixture_and_carries_context(self):
        first = get_adapter(self.worker)
        result = first.start(self.task, self.path, {}, self.emit, threading.Event())
        self.assertEqual(result.state, 'running')
        self.assertEqual((self.path / 'validator.py').read_text(), IMPLEMENTATION)
        second = SimulationWorkerAdapter({**self.worker, 'id': 'codexB'})
        result = second.resume(self.task, self.path, self.context, self.emit, threading.Event())
        self.assertEqual(result.state, 'completed')
        self.assertTrue(any('Ran 5 tests' in content and 'OK' in content for _, content, _ in self.events))
        self.assertTrue(any('codexB' in content and metadata.get('context_received') == self.context for _, content, metadata in self.events))
    def test_simulation_refuses_generated_code_and_symlinks(self):
        (self.path / 'validator.py').write_text('raise RuntimeError("must never execute")')
        result = get_adapter(self.worker).resume(self.task, self.path, {}, self.emit, threading.Event())
        self.assertEqual(result.state, 'failed')
        self.assertFalse((self.path / 'test_validator.py').exists())
        outside = Path(self.tmp.name) / 'outside-relay-test'
        outside.write_text('outside')
        (self.path / 'validator.py').unlink()
        try:
            (self.path / 'validator.py').symlink_to(outside)
        except (OSError, NotImplementedError):
            self.skipTest('Symlink creation requires privileges or Developer Mode on Windows')
        with self.assertRaises(ValueError):
            get_adapter(self.worker).start(self.task, self.path, {}, self.emit, threading.Event())
    def test_cancellation_prevents_writes(self):
        cancel = threading.Event(); cancel.set()
        result = get_adapter(self.worker).start(self.task, self.path, {}, self.emit, cancel)
        self.assertEqual(result.state, 'cancelled'); self.assertFalse((self.path / 'validator.py').exists())
    def test_personal_mode_fails_closed_without_credentials(self):
        adapter = get_adapter({**self.worker, 'execution_mode': 'personal_supported'})
        self.assertIsInstance(adapter, PersonalSubscriptionCodexAdapter)
        self.assertFalse(adapter.health()['available'])
        with self.assertRaises(UnsupportedExecutionMode) as raised:
            adapter.start(self.task, self.path, {}, self.emit, threading.Event())
        self.assertEqual(raised.exception.code, 'unsupported_execution_mode')
        self.assertEqual(self.events, [])
    def test_org_mode_fails_closed_without_authorized_owner_or_config(self):
        adapter = OrganizationApiCodexAdapter({**self.worker, 'execution_mode': 'organization_api'})
        self.assertFalse(adapter.health()['available'])
        adapter = OrganizationApiCodexAdapter({**self.worker, 'owner_type': 'org', 'credential_ref': 'env:RELAY_ORG_OPENAI_API_KEY'})
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(adapter.health()['available'])
            result = adapter.start(self.task, self.path, {}, self.emit, threading.Event())
        self.assertEqual(result.state, 'failed')
        self.assertEqual(adapter.capabilities['quota_scope'], 'shared_organization_api')
    def test_live_command_uses_outer_sandbox_and_no_secret_argument(self):
        (self.path / '.git').mkdir(); (self.path / '.relay').mkdir()
        adapter = OrganizationApiCodexAdapter({**self.worker, 'owner_type': 'org'})
        config = {'network': 'relay-internal', 'image': 'relay-codex:local', 'proxy_url': 'http://relay-codex-proxy:8080/v1', 'model': 'example-model'}
        command = adapter._command(self.path, config, 'relay-task-test')
        self.assertIn('--read-only', command); self.assertIn('--cap-drop=ALL', command)
        self.assertIn('--security-opt=no-new-privileges', command)
        self.assertIn('--pids-limit=128', command); self.assertIn('--memory=768m', command)
        self.assertIn('--ignore-user-config', command); self.assertIn('--ephemeral', command)
        self.assertIn('RELAY_PROXY_TOKEN', command)
        self.assertNotIn('CODEX_API_KEY', command)
        self.assertTrue(any('dst=/workspace/.git,readonly' in arg for arg in command))
        self.assertFalse(any('docker.sock' in arg or '.codex/auth' in arg for arg in command))
    def test_org_health_refuses_non_internal_network(self):
        adapter = OrganizationApiCodexAdapter({**self.worker, 'owner_type': 'org', 'credential_ref': 'env:RELAY_ORG_OPENAI_API_KEY'})
        config = {'RELAY_ENABLE_ORG_API': '1', 'RELAY_ORG_OPENAI_API_KEY': 'secret-key', 'RELAY_CODEX_PROXY_TOKEN': 'proxy-secret',
                  'RELAY_CODEX_MODEL': 'example-model', 'RELAY_CODEX_NETWORK': 'not-internal'}
        response = subprocess.CompletedProcess([], 0, json.dumps([{'Internal': False}]), '')
        with patch.dict(os.environ, config, clear=True), patch('relay.workers.current_uid',return_value=1000), patch('relay.workers.current_gid',return_value=1000), patch('relay.workers.shutil.which', return_value='/bin/docker'), patch('relay.workers.subprocess.run', return_value=response):
            health = adapter.health()
        self.assertFalse(health['available']); self.assertIn('internal', health['reason'])
    def test_recovery_only_targets_task_labeled_containers(self):
        adapter = OrganizationApiCodexAdapter({**self.worker, 'owner_type': 'org'})
        completed = subprocess.CompletedProcess([], 0, 'abcdef123456\n', '')
        with patch('relay.workers.subprocess.run', return_value=completed) as run, patch.object(adapter, '_remove_container', return_value=True) as remove:
            self.assertTrue(adapter.recover('task1'))
        command = run.call_args.args[0]
        self.assertIn('label=relay.task_id=task1', command)
        self.assertIn('label=relay.managed=true', command)
        remove.assert_called_once_with('abcdef123456')
        with patch('relay.workers.subprocess.run', side_effect=OSError):
            self.assertFalse(adapter.recover('task1'))

    def _run_provider_fixture(self, source, cleanup=True, cancel=None):
        adapter = OrganizationApiCodexAdapter({**self.worker, 'owner_type': 'org'})
        script = self.path / 'provider_fixture.py'; script.write_text(source)
        with patch.dict(os.environ, {'RELAY_CODEX_PROXY_TOKEN': 'fixture-proxy-capability'}), patch.object(adapter, 'health', return_value={'available': True}), patch.object(adapter, '_command', return_value=[__import__('sys').executable, str(script)]), patch.object(adapter, '_remove_container', return_value=cleanup):
            return adapter.start(self.task, self.path, {}, self.emit, cancel or threading.Event())

    def test_provider_requires_real_completed_turn_and_drops_reasoning(self):
        source = "import json\nprint(json.dumps({'type':'thread.started','thread_id':'native-thread'}))\nprint(json.dumps({'type':'item.completed','item':{'type':'reasoning','text':'do not persist'}}))\nprint(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'Finished coding'}}))\nprint(json.dumps({'type':'turn.completed','usage':{'input_tokens':10}}))\n"
        result = self._run_provider_fixture(source)
        self.assertEqual(result.state, 'completed')
        self.assertEqual(result.native_thread_id, 'native-thread')
        self.assertNotIn('do not persist', str(self.events))
        result = self._run_provider_fixture("print('runtime failed')\n")
        self.assertEqual(result.state, 'failed')

    def test_cleanup_failure_keeps_writer_lease_and_cancel_stops_process(self):
        result = self._run_provider_fixture("import json\nprint(json.dumps({'type':'turn.completed'}))\n", cleanup=False)
        self.assertFalse(result.safe_to_release)
        self.assertEqual(result.state, 'failed')
        cancel = threading.Event(); timer = threading.Timer(.1, cancel.set); timer.start()
        try:
            result = self._run_provider_fixture("import time\ntime.sleep(5)\n", cancel=cancel)
        finally:
            timer.cancel()
        self.assertEqual(result.state, 'cancelled')
        self.assertTrue(result.safe_to_release)

    def test_visible_events_redact_secrets_and_drop_reasoning(self):
        adapter = OrganizationApiCodexAdapter({**self.worker, 'owner_type': 'org'})
        adapter._translate({'type': 'item.completed', 'item': {'type': 'reasoning', 'text': 'private internal reasoning'}}, self.emit, ())
        self.assertEqual(self.events, [])
        adapter._translate({'type': 'item.completed', 'item': {'type': 'agent_message', 'text': 'secret-key proxy-secret sk-abcdefghijklmnop'}}, self.emit, ('secret-key', 'proxy-secret'))
        text = self.events[0][1]
        self.assertNotIn('secret-key', text); self.assertNotIn('proxy-secret', text); self.assertNotIn('sk-abcdefghijklmnop', text)
        adapter._translate({'type': 'turn.completed', 'usage': {'input_tokens': 123}}, self.emit, ())
        self.assertEqual(self.events[-1][2]['source'], 'provider_reported')
        self.assertEqual(self.events[-1][2]['quota_scope'], 'shared_organization_api')

class ProxyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location('relay_api_proxy', Path(__file__).resolve().parent.parent / 'deploy/api_proxy.py')
        cls.proxy = importlib.util.module_from_spec(spec); spec.loader.exec_module(cls.proxy)
    def request(self, path, token='proxy-secret', payload=None):
        # Exercise handler in memory: no socket, Docker, credentials, or provider call.
        handler = object.__new__(self.proxy.Proxy)
        handler.path = path; handler.command = 'POST'; handler.headers = {'Authorization': 'Bearer '+token, 'Content-Length': str(len(payload or b'{}'))}
        handler.rfile = io.BytesIO(payload or b'{}'); handler.wfile = io.BytesIO(); handler.responses = []
        handler.send_response = lambda code: handler.responses.append(code)
        handler.send_header = lambda *args: None; handler.end_headers = lambda: None
        with patch.dict(os.environ, {'RELAY_CODEX_PROXY_TOKEN': 'proxy-secret', 'RELAY_ORG_OPENAI_API_KEY': 'organization-secret'}, clear=True):
            handler.forward()
        return handler
    def test_proxy_refuses_arbitrary_hosts_paths_and_missing_auth(self):
        for path in ['http://evil.test/v1/responses', '/v1/responses?target=evil', '/v1/other']:
            handler = self.request(path); self.assertEqual(handler.responses, [403])
        self.assertEqual(self.request('/v1/responses', token='wrong').responses, [403])
    def test_proxy_uses_fixed_upstream_and_keeps_key_out_of_response(self):
        class Upstream(io.BytesIO):
            status = 200; headers = {'Content-Type': 'application/json'}
        upstream = Upstream(b'{"result":"organization-secret"}')
        with patch.object(self.proxy, 'urlopen', return_value=upstream) as opened:
            handler = self.request('/v1/responses', payload=b'{"store":true,"model":"model"}')
        request = opened.call_args.args[0]
        self.assertEqual(request.full_url, 'https://api.openai.com/v1/responses')
        self.assertEqual(request.get_header('Authorization'), 'Bearer organization-secret')
        self.assertFalse(json.loads(request.data)['store'])
        self.assertNotIn(b'organization-secret', handler.wfile.getvalue())

if __name__ == '__main__': unittest.main()
