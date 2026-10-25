"""Agents run and inspect their apps through Relay tools, in a port-scoped sandbox."""
import json
import os
from pathlib import Path
import re
import shutil
import socket
import tempfile
import threading
import time
import unittest
import uuid
from unittest.mock import patch

from relay import app_review
from relay.app_review import AppReview, TOOLS
import test_local_native_dashboard as native_fixture

SERVER = '''import http.server, os, socket
def attempt(action):
    try:
        action(); return 'ALLOWED'
    except OSError as exc:
        return 'denied'
class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args): pass
    def do_GET(self):
        probes = {'other_local': attempt(lambda: socket.create_connection(('127.0.0.1', int(os.environ['OTHER'])), 2)),
                  'internet': attempt(lambda: socket.create_connection(('1.1.1.1', 443), 3)),
                  'outside_file': attempt(lambda: open(os.environ['SECRET']).read()),
                  'other_port': attempt(lambda: socket.socket().bind(('127.0.0.1', 0)))}
        body = ('<!doctype html><title>Probe app</title><h1 id=h>static</h1><script>document.getElementById("h").textContent="Rendered by JS";'
                'console.warn("probe warning");setTimeout(()=>{throw new Error("probe failure")},10)</script>' + repr(probes)).encode()
        self.send_response(200); self.send_header('Content-Type', 'text/html'); self.end_headers(); self.wfile.write(body)
print('probe app ready', flush=True)
http.server.ThreadingHTTPServer(('127.0.0.1', int(os.environ['PORT'])), Handler).serve_forever()
'''


@unittest.skipUnless(app_review.available(), 'Requires macOS Seatbelt (sandbox-exec)')
class AppReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory();self.root = Path(self.temp.name).resolve()
        self.env = patch.dict(os.environ, {'RELAY_NATIVE_ROOT': str(self.root / 'native')});self.env.start()
        from relay.native import initialize_profiles, profile_paths
        initialize_profiles()
        self.work = profile_paths('a')[1] / uuid.uuid4().hex;(self.work / '.native').mkdir(parents=True)
        (self.work / 'app.py').write_text(SERVER)
        self.secret = self.root / 'secret.txt';self.secret.write_text('outside the workspace')
        self.other = socket.socket();self.other.bind(('127.0.0.1', 0));self.other.listen()  # stands in for Relay
        self.events = []
        self.review = AppReview(self.work, lambda kind, content, metadata=None: self.events.append((kind, metadata)))

    def tearDown(self):
        self.review.close();self.other.close();self.env.stop();self.temp.cleanup()

    def call(self, tool, **arguments):
        result = self.review.handle({'tool': tool, 'arguments': arguments, 'callId': 'call-' + tool, 'turnId': 'turn'})
        return result['success'], result['contentItems'][0]['text'], [item for item in result['contentItems'] if item['type'] == 'inputImage']

    def start(self, **extra):
        with patch.dict(os.environ, {}):
            original = app_review.workspace_environment
            with patch.object(app_review, 'workspace_environment', lambda work: {**original(work), 'OTHER': str(self.other.getsockname()[1]), 'SECRET': str(self.secret)}):
                return self.call('run_app', command='python3 app.py', **extra)

    def test_app_runs_on_its_port_and_can_reach_nothing_else(self):
        ok, text, _ = self.start()
        self.assertTrue(ok, text);self.assertIn('listening at http://127.0.0.1:', text);self.assertIn('probe app ready', text)
        ok, text, _ = self.call('http_request', path='/')
        self.assertTrue(ok, text);self.assertIn('-> 200 OK', text)
        for probe in ('other_local', 'internet', 'outside_file', 'other_port'):
            self.assertIn("'" + probe + "': 'denied'", text)
        self.assertEqual([kind for kind, _ in self.events][:2], ['tool_invocation', 'tool_output'])
        self.assertEqual(self.events[0][1]['channel'], 'app_review')
        port = self.review.ports[0]
        self.call('stop_app')
        with self.assertRaises(OSError):socket.create_connection(('127.0.0.1', port), 1)

    def test_mistakes_get_clear_answers(self):
        self.assertIn('start it with run_app first', self.call('http_request', path='/')[1])
        self.assertIn('shell operators', self.call('run_app', command='python3 app.py && rm -rf /')[1])
        self.assertIn('cwd must be', self.call('run_app', command='python3 app.py', cwd='missing')[1])
        with self.assertRaises(ValueError):app_review._app_path('no-slash')
        self.assertIn('Unknown Relay tool', self.call('delete_everything')[1])
        busy = socket.socket();busy.bind(('127.0.0.1', 0));busy.listen()
        try:
            ok, text, _ = self.call('run_app', command='python3 app.py', port=busy.getsockname()[1])
            self.assertFalse(ok);self.assertIn('already occupied', text)
        finally:busy.close()
        ok, text, _ = self.call('run_app', command='python3 -c "raise SystemExit(3)"')
        self.assertIn('exited with code 3', text)
        (self.work / 'fixed.py').write_text("import http.server\nhttp.server.ThreadingHTTPServer(('127.0.0.1', 1234), http.server.SimpleHTTPRequestHandler).serve_forever()\n")
        ok, text, _ = self.call('run_app', command='python3 fixed.py', wait_seconds=2)
        self.assertIn('pass that port', text);self.assertNotIn('\x1b[', text)

    def test_app_is_never_reachable_from_the_network(self):
        ok, text, _ = self.call('run_app', command='python3 -m http.server 8123', port=8123) if self._free(8123) else (None, '', [])
        if ok is not None:
            self.assertIn('must listen on 127.0.0.1', text);self.assertIn('--bind 127.0.0.1', text)
        self.assertTrue(self.start()[0])
        port = self.review.ports[0]
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
                udp.connect(('192.0.2.1', 9));lan = udp.getsockname()[0]  # no packet is sent
        except OSError:
            self.skipTest('This machine has no network address')
        with self.assertRaises(OSError):socket.create_connection((lan, port), 1)
        self.assertIn('-> 200 OK', self.call('http_request', path='/')[1])
        self.call('stop_app')
        for family, address in ((socket.AF_INET, '0.0.0.0'), (socket.AF_INET6, '::')):
            with socket.socket(family) as released:
                # A held guard refuses even a reusing bind; TIME_WAIT does not.
                released.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                if family == socket.AF_INET6:released.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
                released.bind((address, port))

    @staticmethod
    def _free(port):
        try:
            with socket.socket() as probe:probe.bind(('127.0.0.1', port));return True
        except OSError:
            return False

    @unittest.skipUnless(app_review._browser(), 'Requires Google Chrome for headless review')
    def test_view_page_renders_scripts_and_reports_console_and_errors(self):
        self.assertTrue(self.start()[0])
        ok, text, images = self.call('view_page', path='/')
        self.assertTrue(ok, text)
        self.assertIn('Title: Probe app', text);self.assertIn('Rendered by JS', text)
        self.assertIn('warning: probe warning', text);self.assertIn('Error: probe failure', text)
        self.assertEqual(len(images), 1);self.assertTrue(images[0]['imageUrl'].startswith('data:image/png;base64,iVBOR'))
        self.assertFalse(any('relay-review-' in name for name in os.listdir(tempfile.gettempdir()) if (Path(tempfile.gettempdir()) / name).is_dir() and time.time() - (Path(tempfile.gettempdir()) / name).stat().st_mtime < 5))

    def test_tool_specs_are_valid_for_codex(self):
        self.assertEqual([tool['name'] for tool in TOOLS], ['run_app', 'http_request', 'view_page', 'app_logs', 'stop_app', 'fetch_url', 'run_command'])
        for tool in TOOLS:
            self.assertEqual(tool['inputSchema']['type'], 'object');self.assertFalse(tool['inputSchema']['additionalProperties'])


@unittest.skipUnless(app_review.available(), 'Requires macOS Seatbelt (sandbox-exec)')
class AppReviewNativeTaskTests(unittest.TestCase):
    def setUp(self):self.fixture = native_fixture.LocalNativeDashboardTests();self.fixture.setUp()
    def tearDown(self):self.fixture.tearDown()

    def test_agent_tool_calls_run_through_relay_during_a_native_task(self):
        from relay.native import close_native_sessions, profile_paths
        close_native_sessions();home, _ = profile_paths('b')
        (home / 'scenario.json').write_text(json.dumps({'complete': True, 'review': True}))
        worker = self.fixture.enroll('b')
        task = self.fixture.client.post('tasks/assign', {'project_id': self.fixture.project, 'conversation_id': self.fixture.conversation, 'worker_id': worker, 'prompt': native_fixture.OBJECTIVE})
        self.fixture.wait(task, 'completed')
        self.assertEqual(json.loads((home / 'dynamic-tools.json').read_text()), [tool['name'] for tool in TOOLS])
        started, fetched = json.loads((home / 'tool-results.json').read_text())
        self.assertTrue(started['result']['success'], started);self.assertIn('listening at', started['result']['contentItems'][0]['text'])
        self.assertTrue(fetched['result']['success'], fetched);self.assertIn('-> 200 OK', fetched['result']['contentItems'][0]['text'])
        port = int(re.search(r'127\.0\.0\.1:(\d+)', started['result']['contentItems'][0]['text'])[1])
        with self.assertRaises(OSError):socket.create_connection(('127.0.0.1', port), 1)  # stopped with the task
        prompt = json.loads((home / 'received.json').read_text())['text']
        self.assertIn('call run_app', prompt)
        events = self.fixture.client.get('conversations/' + self.fixture.conversation + '/events?after=0&limit=200')['events']
        self.assertTrue(any(e['event_type'] == 'tool_invocation' and e['content'].startswith('run_app') for e in events))
