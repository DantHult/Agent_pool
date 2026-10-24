"""Regression coverage for integrating the IDE with portable workspace support."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from relay.ide import ProjectIDE
from relay.server import make_server
from relay.store import Relay


class PortableIDEIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.relay = Relay(Path(self.temporary.name) / 'data')
        self.ide = ProjectIDE(self.relay)
        self.project = self.relay.ws.path('demo')

    def tearDown(self):
        self.relay.close()
        self.temporary.cleanup()

    def test_portable_git_preserves_porcelain_status_columns(self):
        (self.project / 'README.md').write_text('Edited source\n')
        with patch('relay.workspace._USE_DIR_FD', False):
            output = self.relay.ws.git(self.project, 'status', '--porcelain=v1', '-z', raw=True)
            self.assertTrue(output.startswith(' M README.md\0'), repr(output))
            self.assertEqual(self.ide.changes('demo'), [{'path': 'README.md', 'status': 'M', 'staged': False}])

    def test_portable_redaction_honors_reserved_runtime_exclusions(self):
        runtime = self.project / '.venv/cache'
        runtime.mkdir(parents=True)
        (runtime / 'large.bin').write_bytes(b'x' * 1024)
        with patch('relay.workspace._USE_DIR_FD', False), patch.object(self.relay.ws, 'MAX_FILE_BYTES', 512):
            self.relay.ws.redact_files('demo', excluded_names={'.venv'})
            with self.assertRaisesRegex(ValueError, 'size limit'):
                self.relay.ws.redact_files('demo')

    def test_portable_dashboard_reads_files_and_fails_closed_for_editor_writes(self):
        with patch('relay.workspace._USE_DIR_FD', False):
            state = self.ide.state('alice', 'demo')
            self.assertFalse(state['editor_writable'])
            self.assertIn('README.md', {item['path'] for item in state['files']})
            self.assertTrue(all(item['readonly'] for item in state['files']))
            self.assertTrue(self.ide.read_file('alice', 'demo', 'README.md')['readonly'])
            with self.assertRaisesRegex(ValueError, 'POSIX'):
                self.ide.acquire('alice', 'demo', 'portable-browser-012345', 'session')
            with self.assertRaisesRegex(ValueError, 'POSIX'):
                self.ide.save('alice', 'demo', 'portable-browser-012345', 'session', 'new.py', None, 'blocked')
            self.assertFalse((self.project / 'new.py').exists())
            self.assertEqual(self.ide.git('alice', 'demo', '', '', ['status'])['exit_code'], 0)

    def test_ide_initialization_keeps_portable_server_status_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            server = make_server(directory, 0)
            try:
                self.assertIsInstance(server.ide, ProjectIDE)
                self.assertIsNone(server.ide.console)
                metadata = json.loads((Path(directory) / 'server.status.json').read_text())
                self.assertEqual(metadata['url'], server.public_url)
                self.assertEqual(json.loads((Path(directory) / 'server.lock').read_text()), metadata)
            finally:
                server.server_close()


if __name__ == '__main__':
    unittest.main()
