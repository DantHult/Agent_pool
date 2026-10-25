"""Per-project internet access: any public host or an allowlist, private-address refusal, keys and the sandbox path."""
import http.client
import http.server
import json
import os
from pathlib import Path
import socket
import tempfile
import threading
import unittest
import uuid
from unittest.mock import patch

from relay import app_review, internet
from relay.config import load_env_file
from relay.security import clean
from relay.store import Relay
import test_local_native_dashboard as native_fixture


class Upstream:
    """A local stand-in for an internet API that records what it receives."""
    def __init__(self):
        self.seen = []
        upstream = self
        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_GET(self):
                upstream.seen.append((self.path, dict(self.headers)))
                body = json.dumps({'path': self.path, 'key': self.headers.get('X-Api-Key')}).encode()
                self.send_response(200); self.send_header('Content-Type', 'application/json'); self.end_headers(); self.wfile.write(body)
        self.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.port = self.server.server_port
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
    def close(self):self.server.shutdown();self.server.server_close()


class RulesTests(unittest.TestCase):
    def test_domains_must_be_public_hostnames(self):
        self.assertEqual(internet.normalize_domains([' API.Example.com. ', '*.example.org', '', 'api.example.com']), ['api.example.com', '*.example.org'])
        for bad in ('localhost', '1.2.3.4', 'http://x.com', 'x.com:443', 'x.com/path', '*', '*.com', 'intranet', 'printer.local', 'db.internal'):
            with self.subTest(bad=bad), self.assertRaises(ValueError):internet.normalize_domains([bad])
        with self.assertRaises(ValueError):internet.normalize_domains(['a%d.example.com' % i for i in range(51)])

    def test_key_names_and_matching(self):
        self.assertEqual(internet.normalize_key_names(['RELAY_SECRET_WEATHER_KEY', 'OPENAI_KEY', 'OPENAI_KEY']), ['WEATHER_KEY', 'OPENAI_KEY'])
        for bad in ('weather', 'A-B', '1KEY'):
            with self.assertRaises(ValueError):internet.normalize_key_names([bad])
        self.assertTrue(internet.allowed('api.example.com', ['api.example.com']))
        self.assertTrue(internet.allowed('a.b.example.org', ['*.example.org']))
        self.assertFalse(internet.allowed('example.org', ['*.example.org']))  # a wildcard means subdomains only
        self.assertFalse(internet.allowed('evil-example.com', ['example.com']))
        self.assertTrue(internet.allowed('api.anything.io', [internet.ANY_PUBLIC]))  # never from normalize_domains, which refuses '*'
        self.assertEqual(internet.describe([internet.ANY_PUBLIC]), 'any public host')

    def test_names_resolving_to_this_machine_or_the_lan_are_refused(self):
        with self.assertRaises(ValueError):internet.resolve_public('localhost', 443)
        mixed = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.215.14', 443)), (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('192.168.1.5', 443))]
        with patch('socket.getaddrinfo', return_value=mixed), self.assertRaises(ValueError):internet.resolve_public('rebind.example.com', 443)
        with self.assertRaises(ValueError):internet.check('api.example.com', 8443, ['api.example.com'], lambda h, p: '93.184.215.14')
        with self.assertRaises(ValueError):internet.check('api.example.com', 443, [], lambda h, p: '93.184.215.14')  # turned off
        # Any public host still never means this machine or the LAN. (Simulated
        # answers: on macOS a real lookup after urllib's system proxy check
        # makes later subprocesses in the same process crash.)
        with patch('socket.getaddrinfo', return_value=mixed), self.assertRaises(ValueError):internet.check('rebind.example.com', 443, [internet.ANY_PUBLIC], internet.resolve_public)
        literal = lambda host, port, **kwargs: [(socket.AF_INET6 if ':' in host else socket.AF_INET, socket.SOCK_STREAM, 6, '', (host, port))]
        with patch('socket.getaddrinfo', literal):
            for private in ('127.0.0.1', '10.0.0.8', '100.64.0.1', '169.254.169.254', '::1', 'fd00::1'):
                with self.subTest(private=private), self.assertRaises(ValueError):internet.check(private, 80, [internet.ANY_PUBLIC], internet.resolve_public)
            self.assertEqual(internet.check('93.184.215.14', 443, [internet.ANY_PUBLIC], internet.resolve_public), '93.184.215.14')

    def test_secret_values_are_redacted_and_env_file_accepts_only_secret_names(self):
        with patch.dict(os.environ, {'RELAY_SECRET_DEMO_KEY': 'value-to-hide-123'}):
            self.assertEqual(clean('key is value-to-hide-123!'), 'key is [REDACTED]!')
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {}, clear=False):
            good = Path(temp) / 'good.env';good.write_text('RELAY_SECRET_WEATHER_KEY=abc123xyz\n')
            os.environ.pop('RELAY_SECRET_WEATHER_KEY', None);load_env_file(good)
            self.assertEqual(os.environ.pop('RELAY_SECRET_WEATHER_KEY'), 'abc123xyz')
            bad = Path(temp) / 'bad.env';bad.write_text('WEATHER_KEY=abc\n')
            with self.assertRaises(ValueError):load_env_file(bad)


class ProxyTests(unittest.TestCase):
    def setUp(self):
        self.upstream = Upstream()
        self.ports = patch.object(internet, 'PORTS', (80, 443, self.upstream.port));self.ports.start()
        self.proxy = internet.EgressProxy(['api.allowed.test'], resolve=lambda host, port: '127.0.0.1')

    def tearDown(self):self.proxy.close();self.upstream.close();self.ports.stop()

    def get(self, url):
        # http.client, not urllib: urllib's macOS system proxy lookup makes
        # later subprocesses in this process crash after a real DNS lookup.
        connection = http.client.HTTPConnection('127.0.0.1', self.proxy.port, timeout=5)
        try:
            connection.request('GET', url)
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()

    def test_proxy_forwards_allowed_hosts_and_refuses_others(self):
        status, body = self.get('http://api.allowed.test:%d/v1/ping?x=1' % self.upstream.port)
        self.assertEqual(status, 200);self.assertEqual(json.loads(body)['path'], '/v1/ping?x=1')
        for url in ('http://other.test:%d/' % self.upstream.port, 'http://api.allowed.test:8081/'):
            with self.subTest(url=url):self.assertEqual(self.get(url)[0], 403)
        with socket.create_connection(('127.0.0.1', self.proxy.port), 5) as tunnel:
            tunnel.sendall(b'CONNECT api.allowed.test:%d HTTP/1.1\r\n\r\n' % self.upstream.port)
            self.assertIn(b'200 Connection Established', tunnel.recv(200))
            tunnel.sendall(b'GET /tunnel HTTP/1.0\r\nHost: api.allowed.test\r\n\r\n')
            self.assertIn(b'/tunnel', tunnel.recv(4000) + tunnel.recv(4000))
        self.assertTrue(any(line.endswith('blocked other.test:%d/: other.test is not in this project\'s allowed internet domains (api.allowed.test)' % self.upstream.port) or 'blocked' in line for line in self.proxy.recent))

    def test_fetch_inserts_keys_on_relays_side_only(self):
        keys = {'DEMO_KEY': 'value-to-hide-123'}
        response, data, truncated, _ = internet.fetch('http://api.allowed.test:%d/q?k=${DEMO_KEY}' % self.upstream.port, 'GET', {'X-Api-Key': '${DEMO_KEY}'}, None,
                                                       ['api.allowed.test'], keys, resolve=lambda host, port: '127.0.0.1')
        self.assertEqual(response.status, 200)
        self.assertEqual(self.upstream.seen[-1][0], '/q?k=value-to-hide-123');self.assertEqual(self.upstream.seen[-1][1]['X-Api-Key'], 'value-to-hide-123')
        with self.assertRaises(ValueError):internet.fetch('http://api.allowed.test:%d/' % self.upstream.port, 'GET', {'X': '${OTHER}'}, None, ['api.allowed.test'], keys, resolve=lambda h, p: '127.0.0.1')


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory();self.relay = Relay(Path(self.temp.name) / 'data')

    def tearDown(self):self.relay.close();self.temp.cleanup()

    def test_only_project_administrators_change_access_and_values_stay_hidden(self):
        with self.assertRaises(PermissionError):self.relay.mutate('alice', 'projects/internet', {'project_id': 'demo', 'domains': ['api.example.com']})
        with self.assertRaises(ValueError):self.relay.mutate('admin', 'projects/internet', {'project_id': 'demo', 'domains': ['localhost']})
        with patch.dict(os.environ, {'RELAY_SECRET_WEATHER_KEY': 'value-to-hide-123'}):
            saved = self.relay.mutate('admin', 'projects/internet', {'project_id': 'demo', 'mode': 'listed', 'domains': ['api.example.com'], 'key_names': ['WEATHER_KEY', 'MISSING_KEY']})
            self.assertEqual(saved['mode'], 'listed');self.assertEqual(saved['keys_configured'], {'WEATHER_KEY': True, 'MISSING_KEY': False})
            self.assertNotIn('value-to-hide-123', json.dumps(saved))
            self.assertEqual(self.relay.internet_for('demo'), {'mode': 'listed', 'domains': ['api.example.com'], 'keys': {'WEATHER_KEY': 'value-to-hide-123'}, 'missing': ['MISSING_KEY']})
        self.assertFalse(self.relay.internet_settings('alice', 'demo')['editable'])
        self.assertEqual([(p['internet_mode'], p['internet_domains']) for p in self.relay.visible_projects('alice')], [('listed', 1)])
        self.assertTrue(self.relay.rows("SELECT 1 FROM audit_events WHERE action='project_internet_changed'"))
        self.relay.mutate('admin', 'projects/delete', {'project_id': 'demo', 'confirm_name': 'Password toolkit'})
        self.assertEqual(self.relay.internet_for('demo'), {'mode': 'off', 'domains': [], 'keys': {}, 'missing': []})

    def test_projects_reach_any_public_host_by_default_without_keys(self):
        self.assertEqual(self.relay.internet_settings('alice', 'demo')['mode'], 'public')
        self.assertEqual(self.relay.internet_for('demo'), {'mode': 'public', 'domains': [internet.ANY_PUBLIC], 'keys': {}, 'missing': []})
        self.assertEqual(self.relay.visible_projects('alice')[0]['internet_mode'], 'public')
        with patch.dict(os.environ, {'RELAY_SECRET_WEATHER_KEY': 'value-to-hide-123'}):
            listed = {'project_id': 'demo', 'domains': ['api.example.com'], 'key_names': ['WEATHER_KEY']}
            saved = self.relay.mutate('admin', 'projects/internet', {**listed, 'mode': 'public'})
            self.assertEqual(saved['domains'], ['api.example.com'])  # kept for switching back
            self.assertEqual(self.relay.internet_for('demo')['keys'], {})  # any host could receive a key
            self.relay.mutate('admin', 'projects/internet', {**listed, 'mode': 'off'})
            self.assertEqual(self.relay.internet_for('demo')['domains'], [])
            self.relay.mutate('admin', 'projects/internet', {**listed, 'mode': 'listed'})
            self.assertEqual(self.relay.internet_for('demo')['keys'], {'WEATHER_KEY': 'value-to-hide-123'})
        with self.assertRaises(ValueError):self.relay.mutate('admin', 'projects/internet', {'project_id': 'demo', 'mode': 'listed', 'domains': []})
        with self.assertRaises(ValueError):self.relay.mutate('admin', 'projects/internet', {'project_id': 'demo', 'mode': 'everything'})
        self.relay.mutate('admin', 'projects/internet', {'project_id': 'demo', 'domains': []})  # older clients: an empty list turns it off
        self.assertEqual(self.relay.internet_for('demo')['mode'], 'off')


@unittest.skipUnless(app_review.available(), 'Requires macOS Seatbelt (sandbox-exec)')
class SandboxPathTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory();root = Path(self.temp.name).resolve()
        self.env = patch.dict(os.environ, {'RELAY_NATIVE_ROOT': str(root / 'native')});self.env.start()
        from relay.native import initialize_profiles, profile_paths
        initialize_profiles()
        self.work = profile_paths('a')[1] / uuid.uuid4().hex;(self.work / '.native').mkdir(parents=True)
        self.upstream = Upstream()
        self.patches = [patch.object(internet, 'PORTS', (80, 443, self.upstream.port)), patch.object(internet, 'resolve_public', lambda host, port: '127.0.0.1')]
        for item in self.patches:item.start()
        self.settings = {'domains': [], 'keys': {}}
        self.review = AppReview = app_review.AppReview(self.work, None, lambda: self.settings)

    def tearDown(self):
        self.review.close();self.upstream.close()
        for item in self.patches:item.stop()
        self.env.stop();self.temp.cleanup()

    def run_command(self, script):
        (self.work / 'check.py').write_text(script)
        result = self.review.handle({'tool': 'run_command', 'arguments': {'command': 'python3 check.py'}, 'callId': 'c', 'turnId': 't'})
        return result['contentItems'][0]['text']

    def test_run_command_reaches_only_allowed_domains_through_relay(self):
        script = ('import os, socket, urllib.request\n'
                  'def get(url):\n    try: return str(urllib.request.urlopen(url, timeout=5).status)\n    except Exception as e: return "failed " + str(e)[:60]\n'
                  'print("allowed", get("http://api.allowed.test:%d/"))\nprint("other", get("http://other.test:%d/"))\n'
                  'try:\n    socket.create_connection(("127.0.0.1", %d), 2); print("direct ALLOWED")\nexcept OSError: print("direct denied")\n'
                  'print("key", os.environ.get("DEMO_KEY"))\n') % ((self.upstream.port,) * 3)
        offline = self.run_command(script)
        self.assertIn('without internet access', offline);self.assertIn('allowed failed', offline)
        self.settings = {'domains': ['api.allowed.test'], 'keys': {'DEMO_KEY': 'value-to-hide-123'}}
        with patch.dict(os.environ, {'RELAY_SECRET_DEMO_KEY': 'value-to-hide-123'}):online = self.run_command(script)
        self.assertIn('allowed 200', online);self.assertIn('other failed', online);self.assertIn('direct denied', online)
        self.assertIn('key [REDACTED]', online);self.assertNotIn('value-to-hide-123', online)
        self.assertIn('Internet requests (through Relay)', online)
        self.settings = {'mode': 'public', 'domains': [internet.ANY_PUBLIC], 'keys': {}}
        anywhere = self.run_command(script)
        self.assertIn('with internet access to any public host', anywhere)
        self.assertIn('allowed 200', anywhere);self.assertIn('other 200', anywhere);self.assertIn('direct denied', anywhere);self.assertIn('key None', anywhere)

    def test_agent_instructions_describe_the_projects_access(self):
        self.assertIn('internet access turned off', app_review.internet_note({'domains': [], 'keys': {}}))
        public = app_review.internet_note({'domains': [internet.ANY_PUBLIC], 'keys': {}})
        self.assertIn('any public website or API', public);self.assertIn('no API keys are available', public)
        note = app_review.internet_note({'domains': ['api.example.com'], 'keys': {'WEATHER_KEY': 'x'}})
        self.assertIn('api.example.com', note);self.assertIn('WEATHER_KEY', note);self.assertNotIn("'x'", note)


@unittest.skipUnless(app_review.available() and os.environ.get('RELAY_RUN_INTERNET_TESTS') == '1', 'Opt-in real internet test')
class RealInternetTests(unittest.TestCase):
    def test_real_allowlisted_https_through_the_sandbox(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {'RELAY_NATIVE_ROOT': str(Path(temp).resolve() / 'native')}):
            from relay.native import initialize_profiles, profile_paths
            initialize_profiles();work = profile_paths('a')[1] / uuid.uuid4().hex;(work / '.native').mkdir(parents=True)
            # An HTTP error status still means the site was reached (Wikipedia
            # answers Python's default User-Agent with 403).
            (work / 'check.py').write_text('import urllib.error, urllib.request\nfor url in ("https://example.com", "https://www.wikipedia.org", "https://localtest.me"):\n'
                                           '    try: print(url, "reached", urllib.request.urlopen(url, timeout=15).status)\n'
                                           '    except urllib.error.HTTPError as e: print(url, "reached", e.code)\n    except Exception as e: print(url, "blocked")\n')
            settings = {'domains': ['example.com', 'localtest.me'], 'keys': {}}
            review = app_review.AppReview(work, None, lambda: settings)
            run = lambda: review.handle({'tool': 'run_command', 'arguments': {'command': 'python3 check.py'}, 'callId': 'c', 'turnId': 't'})['contentItems'][0]['text']
            try:
                text = run()
                settings = {'domains': [internet.ANY_PUBLIC], 'keys': {}};anywhere = run()
            finally:review.close()
            self.assertIn('https://example.com reached 200', text);self.assertIn('https://www.wikipedia.org blocked', text);self.assertIn('https://localtest.me blocked', text)
            # localtest.me resolves to 127.0.0.1, so even "any public host" refuses it.
            self.assertIn('https://www.wikipedia.org reached', anywhere);self.assertIn('https://localtest.me blocked', anywhere)


class InternetHTTPTests(unittest.TestCase):
    def setUp(self):self.fixture = native_fixture.LocalNativeDashboardTests();self.fixture.setUp()
    def tearDown(self):self.fixture.tearDown()

    def test_settings_routes_and_agent_instructions(self):
        client, project = self.fixture.client, self.fixture.project
        self.assertEqual(client.get('projects/' + project + '/internet')['mode'], 'public')
        saved = client.post('projects/internet', {'project_id': project, 'mode': 'listed', 'domains': ['api.example.com'], 'key_names': ['WEATHER_KEY']})
        self.assertEqual(saved['domains'], ['api.example.com']);self.assertTrue(client.get('overview?light=1')['features']['internet_access'])
        self.assertEqual(client.request('/api/projects/internet', {'project_id': project, 'domains': [], 'api_key': 'x'})[0], 400)  # values never travel over HTTP
        if app_review.available():
            from relay.native import close_native_sessions, profile_paths
            close_native_sessions();home, _ = profile_paths('b');(home / 'scenario.json').write_text('{"complete":true}')
            worker = self.fixture.enroll('b')
            task = client.post('tasks/assign', {'project_id': project, 'conversation_id': self.fixture.conversation, 'worker_id': worker, 'prompt': native_fixture.OBJECTIVE})
            self.fixture.wait(task, 'completed')
            prompt = json.loads((home / 'received.json').read_text())['text']
            self.assertIn('can reach these allowed domains: api.example.com', prompt)
            self.assertIn('not yet configured by the administrator, so unavailable: WEATHER_KEY', prompt)


if __name__ == '__main__':
    unittest.main()
