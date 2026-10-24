"""Safe local configuration and actionable live readiness without provider calls."""
import contextlib
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from relay.config import load_env_file
from scripts.live_smoke import main,preflight,readiness

class LocalConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'.env'
    def test_loads_supported_values_without_shell_expansion(self):
        self.path.write_text('RELAY_ENABLE_ORG_API=1\nexport RELAY_CODEX_MODEL="approved-model" # comment\nRELAY_CODEX_PROXY_TOKEN="$(touch never-execute)"\n')
        with patch.dict(os.environ,{},clear=True):
            load_env_file(self.path)
            self.assertEqual(os.environ['RELAY_ENABLE_ORG_API'],'1')
            self.assertEqual(os.environ['RELAY_CODEX_MODEL'],'approved-model')
            self.assertEqual(os.environ['RELAY_CODEX_PROXY_TOKEN'],'$(touch never-execute)')
    def test_shell_exports_take_precedence(self):
        self.path.write_text('RELAY_CODEX_MODEL=file-value\n')
        with patch.dict(os.environ,{'RELAY_CODEX_MODEL':'shell-value'},clear=True):
            load_env_file(self.path);self.assertEqual(os.environ['RELAY_CODEX_MODEL'],'shell-value')
    def test_rejected_input_is_atomic_and_does_not_echo_values(self):
        self.path.write_text('RELAY_ENABLE_ORG_API=1\nACCESS_TOKEN=private-value\n')
        with patch.dict(os.environ,{},clear=True):
            with self.assertRaises(ValueError) as raised:load_env_file(self.path)
            self.assertNotIn('private-value',str(raised.exception))
            self.assertNotIn('RELAY_ENABLE_ORG_API',os.environ)
    def test_bad_quotes_do_not_echo_secret(self):
        self.path.write_text('RELAY_ORG_OPENAI_API_KEY="private-value\n')
        with self.assertRaises(ValueError) as raised:load_env_file(self.path)
        self.assertNotIn('private-value',str(raised.exception))
    def test_missing_file_is_actionable(self):
        with self.assertRaisesRegex(ValueError,'copy .env.example'):load_env_file(self.path)
    def test_preflight_reports_all_blockers_and_never_starts_docker(self):
        with patch.dict(os.environ,{},clear=True),patch('scripts.live_smoke.shutil.which',return_value=None),patch('scripts.live_smoke.subprocess.run') as run:
            with self.assertRaises(RuntimeError) as raised:preflight()
            message=str(raised.exception)
            for required in ('RELAY_ENABLE_ORG_API','RELAY_ORG_OPENAI_API_KEY','RELAY_CODEX_MODEL','Docker CLI installed'):
                self.assertIn(required,message)
            run.assert_not_called()
    def test_check_loads_env_file_without_executing_coding_or_printing_secrets(self):
        self.path.write_text('RELAY_ENABLE_ORG_API=1\nRELAY_ORG_OPENAI_API_KEY=private-key\nRELAY_CODEX_PROXY_TOKEN=private-proxy\n')
        output=io.StringIO()
        with patch.dict(os.environ,{},clear=True),patch('sys.argv',['live_smoke.py','--env-file',str(self.path),'--check']),patch('scripts.live_smoke.shutil.which',return_value=None),patch('scripts.live_smoke.run') as run,contextlib.redirect_stdout(output),contextlib.redirect_stderr(output):
            with self.assertRaises(SystemExit) as raised:main()
            self.assertEqual(raised.exception.code,2);run.assert_not_called()
        self.assertIn('PASS: Organization API enabled',output.getvalue())
        self.assertNotIn('private-key',output.getvalue());self.assertNotIn('private-proxy',output.getvalue())
    def test_readiness_reports_stopped_daemon_safely(self):
        import subprocess
        with patch.dict(os.environ,{},clear=True),patch('scripts.live_smoke.shutil.which',return_value='/bin/docker'),patch('scripts.live_smoke.subprocess.run',return_value=subprocess.CompletedProcess([],1,b'',b'private-environment')):
            self.assertIn(('Docker daemon running',False),readiness())

if __name__=='__main__':unittest.main()
