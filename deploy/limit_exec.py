"""Independent in-container deadline, even if the host loses its Docker connection."""
import os
import signal
import subprocess
import sys

seconds = max(5, min(int(os.environ.get('RELAY_TASK_TIMEOUT', '180')), 900))
process = subprocess.Popen(sys.argv[1:], start_new_session=True)
try:
    raise SystemExit(process.wait(timeout=seconds))
except subprocess.TimeoutExpired:
    os.killpg(process.pid, signal.SIGKILL)
    process.wait()
    print('Relay container deadline reached; process group stopped.', file=sys.stderr)
    raise SystemExit(124)
