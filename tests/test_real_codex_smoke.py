"""Opt-in real native or API integration; ordinary CI makes no model calls."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

class RealCodexSmoke(unittest.TestCase):
    @unittest.skipUnless(os.environ.get('RELAY_RUN_NATIVE_SMOKE')=='1','Opt-in genuine native ChatGPT A to B to C; requires three authenticated profiles')
    def test_genuine_native_three_worker_handoff(self):
        with tempfile.TemporaryDirectory(prefix='relay-genuine-native-') as directory:
            subprocess.run([sys.executable,'scripts/live_smoke.py','--backend','native','--run-live','--data',directory],
                           cwd=Path(__file__).resolve().parents[1],check=True,timeout=3000)

    @unittest.skipUnless(os.environ.get('RELAY_RUN_LIVE_SMOKE')=='1','Opt-in real Docker/OpenAI test; no paid calls in ordinary CI')
    def test_genuine_two_worker_handoff(self):
        with tempfile.TemporaryDirectory(prefix='relay-genuine-smoke-') as directory:
            subprocess.run([sys.executable,'scripts/live_smoke.py','--run-live','--data',directory],
                           cwd=Path(__file__).resolve().parents[1],check=True,timeout=2100)

if __name__=='__main__':unittest.main()
