"""The standalone demo must honor the live server's data-directory lock."""
from contextlib import redirect_stdout
import fcntl
import io
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from scripts.demo import run


class DemoLockTests(unittest.TestCase):
    def test_demo_refuses_live_data_lock_before_opening_database(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'server.lock'
            with path.open('a+') as lock:
                lock.write('{"url":"http://127.0.0.1:8000"}')
                lock.flush()
                original = path.read_text()
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                result = subprocess.run(
                    [sys.executable, 'scripts/demo.py', '--data', directory],
                    capture_output=True, text=True, timeout=10,
                    cwd=Path(__file__).resolve().parents[1],
                )
                output = result.stdout + result.stderr
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('in use', output)
                self.assertNotIn('Traceback', output)
                self.assertEqual(path.read_text(), original)
                self.assertFalse((Path(directory) / 'relay.sqlite').exists())

    def test_demo_reuses_old_lock_file_and_releases_it_after_completion(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'server.lock'
            path.write_text('{"pid":99999999}')
            output = io.StringIO()
            with redirect_stdout(output):
                run(directory)
            self.assertIn('PASS: end-to-end owned-worker handoff demo', output.getvalue())
            self.assertTrue((Path(directory) / 'relay.sqlite').exists())
            with path.open('a+') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)


if __name__ == '__main__':
    unittest.main()
