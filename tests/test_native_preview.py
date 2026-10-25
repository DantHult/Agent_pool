"""Policy/cleanup regressions; no model calls or implicit network provisioning."""
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from relay import native_preview as preview


class NativePreviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve() / 'native'
        self.workspace = self.root / 'workspaces/b' / ('a' * 32)
        self.workspace.mkdir(parents=True)
        self.workspace.parent.chmod(0o700)
        self.environment = patch.dict(os.environ, {'RELAY_NATIVE_ROOT': str(self.root), 'OPENAI_API_KEY': 'never-forward'})
        self.environment.start()
        self.platform = patch.object(preview.platform, 'system', return_value='Darwin')
        self.platform.start()
        self.port_probe = patch.object(preview, 'ensure_port_available')
        self.mock_port_probe = self.port_probe.start()

    def tearDown(self):
        self.platform.stop()
        self.port_probe.stop()
        self.environment.stop()
        self.tmp.cleanup()

    def test_policy_denies_default_and_only_grants_explicit_loopback_ports(self):
        policy = preview.preview_policy(self.workspace)
        self.assertIn('(deny default)', policy)
        self.assertIn('(allow network-bind (local ip "localhost:8000"))', policy)
        self.assertIn('(allow network-outbound (remote ip "localhost:5173"))', policy)
        self.assertNotIn('localhost:*', policy)
        self.assertNotIn('*:*', policy)
        self.assertNotIn('(allow network-outbound)', policy)
        self.assertNotIn('(allow sysctl-read)', policy)
        self.assertNotIn('kern.procargs', policy)
        self.assertNotIn(json.dumps(str(self.root / 'profiles')), policy)
        self.assertNotIn(json.dumps(str(self.root / 'environments')), policy)
        self.assertIn(json.dumps(str(self.workspace / '.git')), policy)
        self.assertIn(json.dumps(str(self.workspace / '.relay')), policy)
        self.assertIn('[.]env$', policy)

    def test_canonical_or_parent_workspace_cannot_be_granted(self):
        for path in (self.root, self.workspace.parent, self.root / 'profiles/b', Path('/')):
            with self.assertRaises((ValueError, FileNotFoundError)):
                preview.preview_policy(path)

    def test_symlink_workspace_cannot_grant_another_directory(self):
        link = self.workspace.parent / ('b' * 32)
        try:
            link.symlink_to(self.workspace, target_is_directory=True)
        except OSError as exc:
            self.skipTest('Symlink creation requires privileges or Developer Mode on Windows: ' + str(exc))
        with self.assertRaisesRegex(ValueError, 'symlinks'):
            preview.private_workspace(link)

    def test_invalid_ports_and_unapproved_runtime_are_rejected(self):
        for backend, frontend in ((80, 5173), (8000, 8000), (65536, 5173), (True, 5173), ('8000', 5173)):
            with self.assertRaises(ValueError):
                preview.preview_policy(self.workspace, backend, frontend)
        with self.assertRaises(ValueError):
            preview.preview_policy(self.workspace, runtime='/')

    @unittest.skipUnless(hasattr(os, 'killpg') and hasattr(signal, 'SIGKILL'), 'POSIX process-group cleanup')
    def test_child_uses_sandbox_clean_environment_and_group_cleanup(self):
        fake = Mock(pid=12345)
        fake.poll.return_value = None
        with patch.object(preview.subprocess, 'Popen', return_value=fake) as popen, patch.object(preview.os, 'killpg') as kill:
            with preview.Preview(self.workspace) as runner:
                runner._start('test', ['/usr/bin/true'], self.workspace)
                argv = popen.call_args.args[0]
                options = popen.call_args.kwargs
                self.assertEqual(argv[:2], ['/usr/bin/sandbox-exec', '-p'])
                self.assertTrue(options['start_new_session'])
                self.assertEqual(options['stdin'], subprocess.DEVNULL)
                self.assertNotIn('OPENAI_API_KEY', options['env'])
                with self.assertRaisesRegex(ValueError, 'already running'):
                    runner._start('test', ['/usr/bin/true'], self.workspace)
            self.assertEqual(kill.call_args_list[0].args, (12345, signal.SIGTERM))
            self.assertEqual(kill.call_args_list[1].args, (12345, signal.SIGKILL))
            self.assertEqual(fake.wait.call_count, 2)

    @unittest.skipUnless(hasattr(os, 'killpg') and hasattr(signal, 'SIGKILL'), 'POSIX process-group cleanup')
    def test_cleanup_kills_timed_out_process_group(self):
        fake = Mock(pid=12345)
        fake.wait.side_effect = [subprocess.TimeoutExpired('preview', 5), 0]
        with patch.object(preview.os, 'killpg') as kill:
            runner = preview.Preview(self.workspace)
            runner.processes['backend'] = fake
            runner.stop_backend()
            self.assertEqual(kill.call_args_list[0].args, (12345, signal.SIGTERM))
            self.assertEqual(kill.call_args_list[1].args, (12345, signal.SIGKILL))
            self.assertFalse(runner.processes)

    def test_occupied_port_prevents_any_project_process_launch(self):
        self.mock_port_probe.side_effect = ValueError('Preview loopback port 8000 is already occupied or unavailable')
        with patch.object(preview.subprocess, 'Popen') as popen:
            with self.assertRaisesRegex(ValueError, 'occupied'):
                preview.Preview(self.workspace)
            popen.assert_not_called()

    def test_recovery_rechecks_only_backend_port_and_cannot_attach_to_relay(self):
        (self.workspace / 'backend').mkdir()
        (self.workspace / 'backend/manage.py').write_text('raise AssertionError("must not execute")')
        (self.workspace / '.venv/bin').mkdir(parents=True)
        (self.workspace / '.venv/bin/python').write_text('fixture')
        runner = preview.Preview(self.workspace)
        self.mock_port_probe.reset_mock()
        self.mock_port_probe.side_effect = ValueError('occupied')
        runner.processes['frontend'] = Mock()
        with patch.object(preview.subprocess, 'Popen') as popen:
            with self.assertRaisesRegex(ValueError, 'occupied'):
                runner.start_backend()
            self.mock_port_probe.assert_called_once_with(8000)
            popen.assert_not_called()

    @unittest.skipUnless(sys.platform == 'darwin' and os.environ.get('RELAY_RUN_SANDBOX_TESTS') == '1',
                         'Opt-in actual loopback socket test')
    def test_actual_occupied_port_is_rejected_before_project_code(self):
        self.port_probe.stop()
        for address in ('127.0.0.1', '0.0.0.0'):
            with self.subTest(address=address), socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                listener.bind((address, 0))
                listener.listen()
                occupied = listener.getsockname()[1]
                other = 5173 if occupied != 5173 else 8000
                with patch.object(preview.subprocess, 'Popen') as popen:
                    with self.assertRaisesRegex(ValueError, 'occupied'):
                        preview.Preview(self.workspace, occupied, other)
                    popen.assert_not_called()

    @unittest.skipUnless(sys.platform == 'darwin' and os.environ.get('RELAY_RUN_SANDBOX_TESTS') == '1',
                         'Opt-in actual closed-server recovery socket test')
    def test_actual_closed_server_time_wait_allows_recovery(self):
        self.port_probe.stop()
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
            server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server.bind(('127.0.0.1', 0))
            server.listen()
            port = server.getsockname()[1]
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as client:
                client.connect(('127.0.0.1', port))
                connection, _ = server.accept()
                connection.close()
                self.assertEqual(client.recv(1), b'')
        preview.ensure_port_available(port)

    @unittest.skipUnless(sys.platform == 'darwin' and os.environ.get('RELAY_RUN_SANDBOX_TESTS') == '1',
                         'Opt-in actual macOS sandbox execution; no model calls')
    def test_actual_sandbox_executes_python_and_restricts_files_and_network(self):
        canary = self.root / 'nonsecret-canary'
        canary.write_text('nonsecret')
        for name in ('.env', '.git/config', '.relay/HANDOFF.md'):
            path = self.workspace / name
            path.parent.mkdir(exist_ok=True)
            path.write_text('nonsecret')
        policy = preview.preview_policy(self.workspace, 57941, 57942)
        program = '''import pathlib,socket,sys
for name in sys.argv[1:]:
    try: pathlib.Path(name).read_text()
    except PermissionError: pass
    else: raise AssertionError('outside/protected file readable: '+name)
for address in [('127.0.0.1',9000),('1.1.1.1',443)]:
    connection=socket.socket()
    assert connection.connect_ex(address)==1, 'unapproved network reachable'
    connection.close()
listener=socket.socket()
listener.bind(('127.0.0.1',57941))
listener.close()
print('native-preview-isolation-ok')
'''
        result = subprocess.run(['/usr/bin/sandbox-exec', '-p', policy, str(Path(sys.executable).resolve()), '-I', '-c', program,
                                 str(canary), *(str(self.workspace / name) for name in ('.env', '.git/config', '.relay/HANDOFF.md'))],
                                cwd=self.workspace, env=preview.workspace_environment(self.workspace),
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), 'native-preview-isolation-ok')


if __name__ == '__main__':
    unittest.main()
