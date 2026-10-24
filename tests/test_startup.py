"""Regression coverage for a second Relay process sharing a data directory."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from relay import server as relay_server
from relay.fs_lock import try_exclusive_lock


class StartupLockTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.servers = []
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.close_servers)

    def close_servers(self):
        for server in self.servers:
            server.server_close()
            server.relay.db.close()
        self.servers.clear()

    def start(self):
        server = relay_server.make_server(self.tmp.name, 0)
        self.servers.append(server)
        return server

    def test_contender_reports_existing_server_without_overwriting_metadata(self):
        server = self.start()
        status = Path(self.tmp.name) / 'server.status.json'
        original = status.read_text(encoding='utf-8')
        metadata = json.loads(original)
        url = f'http://127.0.0.1:{server.server_port}'
        self.assertEqual(metadata['pid'], os.getpid())
        self.assertEqual(metadata['url'], url)

        with self.assertRaises(relay_server.ServerAlreadyRunning) as raised:
            self.start()

        message = str(raised.exception)
        self.assertIn(url, message)
        self.assertRegex(message.lower(), r'stop|ctrl\+c|restart')
        self.assertEqual(status.read_text(encoding='utf-8'), original)

    def test_legacy_empty_lock_reports_friendly_error_without_truncation(self):
        path = Path(self.tmp.name) / 'server.lock'
        with path.open('a+') as lock:
            try_exclusive_lock(lock)
            with self.assertRaises(relay_server.ServerAlreadyRunning) as raised:
                self.start()
            self.assertRegex(str(raised.exception).lower(), r'stop|ctrl\+c|restart')
            # Read through the holding handle: Windows mandatory locks deny other opens.
            lock.seek(0)
            self.assertEqual(lock.read(), '')

    def test_release_allows_restart_with_existing_lock_file(self):
        server = self.start()
        conversation = server.relay.detail('alice', 'demo')['conversation']['id']
        self.close_servers()
        # Metadata left by a closed process must not be mistaken for a live lock.
        self.assertTrue((Path(self.tmp.name) / 'server.lock').exists())
        restarted = self.start()
        self.assertEqual(
            restarted.relay.detail('alice', 'demo')['conversation']['id'],
            conversation,
        )
        metadata = json.loads((Path(self.tmp.name) / 'server.status.json').read_text(encoding='utf-8'))
        self.assertEqual(metadata['url'], f'http://127.0.0.1:{restarted.server_port}')

    def test_second_cli_process_exits_with_guidance_and_no_traceback(self):
        server = self.start()
        result = subprocess.run(
            [sys.executable, '-m', 'relay.server', '--data', self.tmp.name, '--port', '0'],
            capture_output=True, text=True, timeout=10,
        )
        output = result.stdout + result.stderr
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('Traceback', output)
        self.assertNotIn('BlockingIOError', output)
        self.assertIn(f'http://127.0.0.1:{server.server_port}', output)
        self.assertRegex(output.lower(), r'stop|ctrl\+c|restart')


if __name__ == '__main__':
    unittest.main()
