"""The server must retain its filesystem lock through writer shutdown."""
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from relay.fs_lock import try_exclusive_lock
from relay.server import make_server


class ShutdownLockTests(unittest.TestCase):
    def test_directory_lock_is_held_until_execution_shutdown_finishes(self):
        with tempfile.TemporaryDirectory() as directory:
            server = make_server(directory, 0)
            entered_close = threading.Event()
            finish_close = threading.Event()
            close_errors = []
            original_close = server.relay.close

            def delayed_writer_shutdown():
                entered_close.set()
                if not finish_close.wait(3):
                    raise RuntimeError('Test did not allow writer shutdown to finish')
                original_close()

            def close_server():
                try:
                    server.server_close()
                except Exception as exc:
                    close_errors.append(exc)

            with patch.object(server.relay, 'close', side_effect=delayed_writer_shutdown):
                thread = threading.Thread(target=close_server)
                thread.start()
                try:
                    self.assertTrue(entered_close.wait(2))
                    # The listening socket may already be closed, but another
                    # process must not recover/reassign this data directory yet.
                    with (Path(directory) / 'server.lock').open('a+') as contender:
                        with self.assertRaises(BlockingIOError):
                            try_exclusive_lock(contender)
                finally:
                    finish_close.set()
                    thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(close_errors, [])
            with (Path(directory) / 'server.lock').open('a+') as contender:
                try_exclusive_lock(contender)


if __name__ == '__main__':
    unittest.main()
