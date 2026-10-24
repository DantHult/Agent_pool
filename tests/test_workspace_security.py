"""Filesystem-boundary regressions using real symlinks, fd races, and Git commits."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from relay.workspace import IMPLEMENTATION, Workspace

class WorkspaceSecurityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.workspace = Workspace(Path(self.tmp.name) / 'workspaces')
        self.project = self.workspace.path('demo')
        self.outside = Path(self.tmp.name) / 'outside'; self.outside.mkdir()
        self.external = self.outside / 'private.txt'; self.external.write_text('outside-private-value')

    def test_static_symlinks_are_never_read_or_checkpointed(self):
        (self.project / 'leak.txt').symlink_to(self.external)
        (self.project / 'external').symlink_to(self.outside, target_is_directory=True)
        view = self.workspace.inspect('demo')
        self.assertNotIn('leak.txt', view['files'])
        self.assertNotIn('outside-private-value', str(view))
        with self.assertRaises(ValueError): self.workspace.checkpoint('demo', 'Refuse unsafe tree')
        self.assertEqual(self.external.read_text(), 'outside-private-value')

    def test_leaf_swap_after_stat_does_not_follow_symlink(self):
        target = self.project / 'generated.py'; target.write_text('safe source')
        original_open = os.open
        swapped = False
        def racing_open(name, flags, *args, **kwargs):
            nonlocal swapped
            if name == 'generated.py' and kwargs.get('dir_fd') is not None and not swapped:
                swapped = True; target.unlink(); target.symlink_to(self.external)
            return original_open(name, flags, *args, **kwargs)
        with patch('relay.workspace.os.open', side_effect=racing_open):
            view = self.workspace.inspect('demo')
        self.assertTrue(swapped)
        self.assertNotIn('generated.py', view['files'])
        self.assertNotIn('outside-private-value', str(view))

    def test_directory_swap_after_stat_cannot_redirect_traversal(self):
        nested = self.project / 'nested'; nested.mkdir(); (nested / 'safe.py').write_text('safe')
        original_open = os.open
        swapped = False
        def racing_open(name, flags, *args, **kwargs):
            nonlocal swapped
            if name == 'nested' and kwargs.get('dir_fd') is not None and not swapped:
                swapped = True; nested.rename(self.project / 'retained'); nested.symlink_to(self.outside, target_is_directory=True)
            return original_open(name, flags, *args, **kwargs)
        with patch('relay.workspace.os.open', side_effect=racing_open):
            with self.assertRaises(OSError): self.workspace.inspect('demo')
        self.assertTrue(swapped)
        self.assertEqual(self.external.read_text(), 'outside-private-value')

    def test_redaction_race_cannot_write_through_symlink(self):
        target = self.project / 'generated.txt'; target.write_text('organization-secret')
        original_open = os.open
        swapped = False
        def racing_open(name, flags, *args, **kwargs):
            nonlocal swapped
            if name == 'generated.txt' and kwargs.get('dir_fd') is not None and not swapped:
                swapped = True; target.unlink(); target.symlink_to(self.external)
            return original_open(name, flags, *args, **kwargs)
        with patch.dict(os.environ, {'RELAY_ORG_OPENAI_API_KEY': 'organization-secret'}), patch('relay.workspace.os.open', side_effect=racing_open):
            with self.assertRaises(OSError): self.workspace.redact_files('demo')
        self.assertEqual(self.external.read_text(), 'outside-private-value')

    def test_hard_links_and_git_metadata_links_fail_closed(self):
        os.link(self.external, self.project / 'hardlink.txt')
        self.assertNotIn('hardlink.txt', self.workspace.inspect('demo')['files'])
        with self.assertRaises(ValueError): self.workspace.redact_files('demo')
        (self.project / 'hardlink.txt').unlink()
        config = self.project / '.git/config'; config.unlink(); config.symlink_to(self.external)
        with self.assertRaises(OSError): self.workspace.inspect('demo')
        self.assertEqual(self.external.read_text(), 'outside-private-value')

    def test_secrets_are_removed_before_git_and_from_display(self):
        secrets = {'RELAY_ORG_OPENAI_API_KEY': 'organization-secret-value', 'RELAY_CODEX_PROXY_TOKEN': 'proxy-capability-value'}
        (self.project / 'result.txt').write_text('organization-secret-value proxy-capability-value')
        with patch.dict(os.environ, secrets):
            self.assertNotIn('organization-secret-value', str(self.workspace.inspect('demo')))
            self.workspace.checkpoint('demo', 'API checkpoint')
            committed = self.workspace.git(self.project, 'show', 'HEAD:result.txt')
        self.assertEqual(committed, '[REDACTED] [REDACTED]')
        self.assertEqual((self.project / 'result.txt').read_text(), '[REDACTED] [REDACTED]')

    def test_secret_filenames_and_large_files_are_not_committed(self):
        target = self.project / 'organization-secret-value.txt'; target.write_text('safe content')
        previous = self.workspace.git(self.project, 'rev-parse', 'HEAD')
        with patch.dict(os.environ, {'RELAY_ORG_OPENAI_API_KEY': 'organization-secret-value'}):
            with self.assertRaisesRegex(ValueError, 'filename'): self.workspace.checkpoint('demo', 'No secret filename')
        self.assertEqual(self.workspace.git(self.project, 'rev-parse', 'HEAD'), previous)
        target.unlink()
        with (self.project / 'large.bin').open('wb') as file: file.truncate(5_000_001)
        with self.assertRaisesRegex(ValueError, 'size limits'): self.workspace.checkpoint('demo', 'No oversized file')
        self.assertEqual(self.workspace.git(self.project, 'rev-parse', 'HEAD'), previous)

    def test_handoff_and_full_history_are_redacted_and_history_stays_out_of_git(self):
        events = [{'sequence':i, 'event_type':'response', 'worker_id':'codexA', 'content':f'Event {i} organization-secret-value'} for i in range(1, 121)]
        with patch.dict(os.environ, {'RELAY_ORG_OPENAI_API_KEY':'organization-secret-value'}):
            self.workspace.write_handoff('demo', {'objective':'organization-secret-value', 'history_directory':'.relay/history'})
            self.workspace.write_history('demo', events)
            self.workspace.checkpoint('demo', 'Metadata checkpoint')
        history = self.project / '.relay/history'
        index = json.loads((history / 'index.json').read_text())
        self.assertEqual(index['event_count'], 120)
        self.assertEqual(len(index['recent_events']), 100)
        self.assertEqual(index['recent_events'][0]['sequence'], 21)
        self.assertEqual(json.loads((history / '000000000001.json').read_text())['content'], 'Event 1 [REDACTED]')
        self.assertEqual((history / '000000000001.json').stat().st_mode & 0o222, 0)
        tracked = self.workspace.git(self.project, 'ls-tree', '-r', '--name-only', 'HEAD')
        self.assertIn('.relay/HANDOFF.md', tracked)
        self.assertNotIn('.relay/history', tracked)
        reopened = Workspace(self.workspace.root)
        self.assertIn('[REDACTED]', reopened.inspect('demo')['files']['.relay/HANDOFF.md'])
        self.assertTrue((history / '000000000001.json').exists())

    def test_metadata_directory_symlink_cannot_write_outside(self):
        (self.project / '.relay').symlink_to(self.outside, target_is_directory=True)
        with self.assertRaises(OSError): self.workspace.write_handoff('demo', {'objective':'test'})
        with self.assertRaises(OSError): self.workspace.write_history('demo', [])
        self.assertEqual(list(self.outside.iterdir()), [self.external])

    def test_legacy_host_test_helper_rejects_untrusted_source(self):
        (self.project / 'validator.py').write_text('raise RuntimeError("must not run")')
        with self.assertRaises(ValueError): self.workspace.test('demo')
        self.workspace.implement('demo')
        self.assertEqual((self.project / 'validator.py').read_text(), IMPLEMENTATION)
        result, output = self.workspace.test('demo')
        self.assertEqual(result, 0); self.assertIn('Ran 5 tests', output)

if __name__ == '__main__': unittest.main()
