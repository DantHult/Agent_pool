"""Let a native agent run and look at the app it is building.

An agent's shell has no network, so it cannot start a server and open it.
These Relay tools start the app from the agent's private worktree in a
separate Seatbelt sandbox that may use only its assigned 127.0.0.1 port(s),
then reach it over HTTP or in a headless Chrome locked to those ports. Neither
the app nor the browser can reach other local services (such as Relay) or the
Internet. Tools are offered to Codex as dynamic tools; Relay answers each call.
"""
import http.client
import json
import os
import platform
import re
import select
import shlex
import shutil
import socket
import subprocess
import tempfile
import threading
import time

from . import internet
from .ide import file_path
from .native_environment import workspace_environment
from .native_preview import ensure_port_available, private_workspace, sandbox_policy, stop_group
from .security import clean

SANDBOX = '/usr/bin/sandbox-exec'
CHROME = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'
TEXT_LIMIT = 20000
METHODS = ('GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'HEAD', 'OPTIONS')


def _schema(properties, required=()):
    return {'type': 'object', 'properties': properties, 'required': list(required), 'additionalProperties': False}


TOOLS = [
    {'type': 'function', 'name': 'run_app',
     'description': 'Start the app you are building so you can review it. Your shell cannot open network ports; this runs the command from your workspace in a sandbox that may listen only on its assigned port(s), with no Internet access. The app must listen on 127.0.0.1 (HOST), not on all interfaces. PORT and HOST are set in its environment. If the app hard-codes a port, pass that port. One app runs at a time; calling again restarts it.',
     'inputSchema': _schema({'command': {'type': 'string', 'description': 'Command to run, for example "python3 server.py" or "npm run dev". No shell syntax.'},
                             'cwd': {'type': 'string', 'description': 'Directory relative to the workspace root; defaults to the root.'},
                             'port': {'type': 'integer', 'description': 'Port the app listens on (1024-65535). Omit to receive a free port in $PORT.'},
                             'second_port': {'type': 'integer', 'description': 'Optional second port, for example a separate API server.'},
                             'wait_seconds': {'type': 'integer', 'description': 'How long to wait for the port to accept connections (default 20, max 60).'}}, ('command',))},
    {'type': 'function', 'name': 'http_request',
     'description': 'Send an HTTP request to the running app and return the status, headers and body.',
     'inputSchema': _schema({'path': {'type': 'string', 'description': 'Path starting with /, for example /api/ping.'},
                             'method': {'type': 'string', 'enum': list(METHODS)},
                             'body': {'type': 'string'}, 'headers': {'type': 'object', 'additionalProperties': {'type': 'string'}},
                             'port': {'type': 'integer', 'description': 'Which of the app\'s ports; defaults to the first.'}}, ('path',))},
    {'type': 'function', 'name': 'view_page',
     'description': 'Open a page of the running app in headless Chrome and return its visible text, console messages, uncaught JavaScript errors, failed requests and a screenshot. Other local services are unreachable; external resources load only where the project\'s internet access allows.',
     'inputSchema': _schema({'path': {'type': 'string', 'description': 'Path starting with /; defaults to /.'},
                             'port': {'type': 'integer'}, 'wait_ms': {'type': 'integer', 'description': 'Extra time after load for scripts to run (default 1500, max 10000).'},
                             'width': {'type': 'integer'}, 'height': {'type': 'integer'}, 'screenshot': {'type': 'boolean', 'description': 'Include a screenshot (default true).'}})},
    {'type': 'function', 'name': 'app_logs',
     'description': 'Show the running app\'s recent output and whether it is still running.',
     'inputSchema': _schema({'lines': {'type': 'integer', 'description': 'Number of lines (default 80, max 400).'}})},
    {'type': 'function', 'name': 'stop_app', 'description': 'Stop the running app.', 'inputSchema': _schema({})},
    {'type': 'function', 'name': 'fetch_url',
     'description': 'Call an internet API yourself. Any public host is reachable unless the project limits access to listed domains or turns it off. Write ${NAME} in the url, headers or body to use an API key the project enables (only with listed domains); Relay inserts the value, which you never see. Redirects are returned, not followed.',
     'inputSchema': _schema({'url': {'type': 'string', 'description': 'http:// or https:// URL the project\'s internet access allows.'},
                             'method': {'type': 'string', 'enum': list(METHODS)}, 'body': {'type': 'string'},
                             'headers': {'type': 'object', 'additionalProperties': {'type': 'string'}}}, ('url',))},
    {'type': 'function', 'name': 'run_command',
     'description': 'Run a short command from your workspace (a script or tests) with this project\'s internet access and API keys (as environment variables), and return its exit code and output. Your shell has no internet; use this for code that calls an external API. No ports can be opened.',
     'inputSchema': _schema({'command': {'type': 'string', 'description': 'Command to run, for example "python3 -m unittest" or "node check_api.js". No shell syntax.'},
                             'cwd': {'type': 'string', 'description': 'Directory relative to the workspace root.'},
                             'timeout_seconds': {'type': 'integer', 'description': 'Default 60, max 300.'}}, ('command',))},
]


def internet_note(settings):
    """One paragraph for the agent's instructions about this project's internet access."""
    if not settings or not settings['domains']:
        return 'Internet: this project has internet access turned off, so external APIs cannot be reached; a project administrator can turn it on in Relay. '
    if internet.ANY_PUBLIC in settings['domains']:
        return ('Internet: your shell has no internet, but run_app apps, view_page, run_command and fetch_url can reach any public website or API on ports 80 and 443 '
                '(addresses on this machine or a local network are refused). Prefer APIs that need no key: no API keys are available while the project allows any host, '
                'so an API that requires a key needs a project administrator to limit access to listed domains and enable that key. ')
    keys = sorted(settings['keys'])
    return ('Internet: your shell has no internet, but run_app apps, view_page, run_command and fetch_url can reach these allowed domains: ' + ', '.join(settings['domains']) + '. '
            + ('API keys available as environment variables in run_app and run_command, and as ${NAME} in fetch_url: ' + ', '.join(keys) + '. Never print, log or commit key values. ' if keys else 'No API keys are enabled. ')
            + ('Listed but not yet configured by the administrator, so unavailable: ' + ', '.join(settings['missing']) + '. ' if settings.get('missing') else ''))


def available():
    return platform.system() == 'Darwin' and os.path.exists(SANDBOX)


def _browser():
    path = os.environ.get('RELAY_REVIEW_BROWSER', CHROME)
    return path if os.path.isfile(path) and os.access(path, os.X_OK) else None


def _free_port():
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        return probe.getsockname()[1]


def _reserve_wildcards(port):
    """Hold the all-interfaces addresses on an app port for as long as it runs.

    Seatbelt's port rule cannot tell loopback from LAN peers, so an app bound
    to 0.0.0.0 or :: would be reachable from the network. With these bound
    (never listening), such a bind fails; a 127.0.0.1 bind with address reuse,
    as HTTP servers use, still works beneath them.
    """
    guards = []
    for family, address in ((socket.AF_INET, '0.0.0.0'), (socket.AF_INET6, '::')):
        try:
            guard = socket.socket(family, socket.SOCK_STREAM)
        except OSError:
            continue  # no IPv6 on this host
        try:
            if family == socket.AF_INET6:
                guard.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
            guard.bind((address, port))
        except OSError:
            guard.close()
            for held in guards:
                held.close()
            raise ValueError('port ' + str(port) + ' is not available on all interfaces; choose another port') from None
        guards.append(guard)
    return guards


def _bounded(value, default, low, high, name):
    if value is None:
        return default
    if type(value) is not int or not low <= value <= high:
        raise ValueError(name + ' must be a whole number from ' + str(low) + ' to ' + str(high))
    return value


def _app_path(path):
    path = '/' if path is None else path
    if not isinstance(path, str) or not path.startswith('/') or len(path) > 2000 or any(char.isspace() or ord(char) < 32 for char in path):
        raise ValueError('path must start with / and contain no spaces or control characters')
    return path


class AppReview:
    def __init__(self, workspace, emit=None, internet=None):
        self.workspace = private_workspace(workspace)
        self.emit = emit or (lambda *args, **kwargs: None)
        # Re-read on every call, so an administrator's change applies at once.
        self.internet = internet or (lambda: {'domains': [], 'keys': {}})
        self.proxy = None
        self.child = None
        self.ports = ()
        self.guards = []
        self.log = self.workspace / '.native' / 'app-review.log'
        self.lock = threading.Lock()

    def handle(self, params):
        """Answer one item/tool/call request from the App Server."""
        tool, arguments = params.get('tool'), params.get('arguments') or {}
        metadata = {'provider_item_id': params.get('callId'), 'native_turn_id': params.get('turnId'), 'channel': 'app_review'}
        self.emit('tool_invocation', tool + ' ' + json.dumps(arguments)[:500], metadata)
        image = None
        try:
            if tool not in {spec['name'] for spec in TOOLS} or not isinstance(arguments, dict):
                raise ValueError('Unknown Relay tool ' + str(tool))
            with self.lock:
                text = getattr(self, '_' + tool)(arguments)
            if isinstance(text, tuple):
                text, image = text
            success = True
        except (ValueError, OSError, TimeoutError, http.client.HTTPException) as exc:
            text, success = 'Relay could not ' + str(tool).replace('_', ' ') + ': ' + str(exc), False
        text = clean(text)[:TEXT_LIMIT]
        self.emit('tool_output', text, {**metadata, 'exit_code': 0 if success else 1, 'screenshot': bool(image)})
        items = [{'type': 'inputText', 'text': text}]
        if image:
            items.append({'type': 'inputImage', 'imageUrl': 'data:image/png;base64,' + image})
        return {'contentItems': items, 'success': success}

    def close(self):
        with self.lock:
            self._stop_app({})
            if self.proxy:
                self.proxy.close()
                self.proxy = None

    def _egress(self):
        """This project's settings, with the session proxy matching its domains."""
        settings = self.internet()
        if settings['domains']:
            if not self.proxy:
                self.proxy = internet.EgressProxy(settings['domains'])
            self.proxy.domains = list(settings['domains'])
        elif self.proxy:
            self.proxy.domains = []  # an open tunnel finishes; nothing new is allowed
        return settings

    def _sandbox(self, selected, settings):
        """Seatbelt policy and environment for project code in this session."""
        online = bool(settings['domains'])
        policy = sandbox_policy(self.workspace, selected, outbound=(self.proxy.port,) if online else (), certificates=online)
        environment = {**workspace_environment(self.workspace), 'HOST': '127.0.0.1', 'PYTHONUNBUFFERED': '1', 'BROWSER': 'none', 'NO_COLOR': '1'}
        if online:
            proxy = 'http://127.0.0.1:' + str(self.proxy.port)
            environment.update({name: proxy for name in ('HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy')})
            environment.update({'NO_PROXY': '127.0.0.1,localhost', 'no_proxy': '127.0.0.1,localhost', 'NODE_USE_ENV_PROXY': '1',
                                'SSL_CERT_FILE': internet.SYSTEM_CERTIFICATES, 'NODE_EXTRA_CA_CERTS': internet.SYSTEM_CERTIFICATES})
            environment.update(settings['keys'])
        return policy, environment

    def _internet_log(self):
        if not self.proxy or not self.proxy.recent:
            return ''
        return '\n\nInternet requests (through Relay):\n' + '\n'.join(list(self.proxy.recent)[-20:])

    def _running(self):
        return self.child is not None and self.child.poll() is None

    def _port(self, arguments):
        if not self.ports:
            raise ValueError('No app is running; start it with run_app first')
        port = arguments.get('port', self.ports[0])
        if port not in self.ports:
            raise ValueError('The app was given port(s) ' + ', '.join(map(str, self.ports)) + ' only')
        return port

    def _tail(self, lines=40):
        try:
            data = self.log.read_bytes()[-200000:].decode('utf-8', 'replace')
        except FileNotFoundError:
            return ''
        # Show text only, without terminal colour or cursor sequences.
        data = re.sub(r'\x1b(?:\][^\x07]*(?:\x07|\x1b\\)|\[[0-?]*[ -/]*[@-~])', '', data)
        return '\n'.join(data.splitlines()[-lines:])

    def _command(self, arguments):
        command = arguments.get('command')
        if not isinstance(command, str) or not command.strip() or len(command) > 1000:
            raise ValueError('command must be 1-1000 characters')
        try:
            argv = shlex.split(command)
        except ValueError:
            raise ValueError('command has an unclosed quote') from None
        if any(token in ('|', '||', '&&', ';', '>', '>>', '<', '&') for token in argv):
            raise ValueError('run one program; shell operators are not available')
        cwd = self.workspace
        if arguments.get('cwd') not in (None, '', '.'):
            cwd = self.workspace.joinpath(*file_path(arguments['cwd'], write=True))
            if cwd.is_symlink() or not cwd.resolve().is_relative_to(self.workspace) or not cwd.is_dir():
                raise ValueError('cwd must be an existing folder inside your workspace')
        return argv, cwd

    def _run_app(self, arguments):
        argv, cwd = self._command(arguments)
        selected = [_bounded(arguments.get('port'), None, 1024, 65535, 'port') or _free_port()]
        if arguments.get('second_port') is not None:
            selected.append(_bounded(arguments['second_port'], None, 1024, 65535, 'second_port'))
        wait = _bounded(arguments.get('wait_seconds'), 20, 1, 60, 'wait_seconds')
        self._stop_app({})
        for port in selected:
            ensure_port_available(port)  # never hand an existing service to project code
        for port in selected:
            self.guards += _reserve_wildcards(port)
        policy, environment = self._sandbox(selected, self._egress())
        environment['PORT'] = str(selected[0])
        if len(selected) > 1:
            environment['SECOND_PORT'] = str(selected[1])
        directory = os.open(self.workspace / '.native', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            fd = os.open(self.log.name, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600, dir_fd=directory)
        finally:
            os.close(directory)
        with os.fdopen(fd, 'ab') as output:
            self.child = subprocess.Popen([SANDBOX, '-p', policy, *argv], cwd=cwd, env=environment, stdin=subprocess.DEVNULL,
                                          stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
        self.ports = tuple(selected)
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline and self._running():
            try:
                with socket.create_connection(('127.0.0.1', selected[0]), timeout=.5):
                    return ('App is running and listening at http://127.0.0.1:' + str(selected[0]) + '/ (process ' + str(self.child.pid) + ').'
                            + (' Second port: ' + str(selected[1]) + '.' if len(selected) > 1 else '')
                            + ' Use http_request or view_page to review it.\n\nRecent output:\n' + (self._tail(20) or '(none yet)'))
            except OSError:
                time.sleep(.2)
        output = self._tail(40) or '(no output)'
        hint = ' If the app listens on a different port, pass that port; the sandbox refuses every other port.' if 'not permitted' in output.lower() else ''
        if 'address already in use' in output.lower() or 'eaddrinuse' in output.lower():
            hint += (' The app must listen on 127.0.0.1 (the HOST variable), not on all interfaces, so it stays reachable only from this machine;'
                     ' for example "python3 -m http.server $PORT --bind 127.0.0.1" or listen(PORT, "127.0.0.1"). Servers that do not enable address reuse (SO_REUSEADDR) must also enable it.')
        if not self._running():
            code = self.child.returncode
            self._stop_app({})
            return 'The app exited with code ' + str(code) + ' before listening on port ' + str(selected[0]) + '.' + hint + '\n\nOutput:\n' + output
        return 'The app is running but port ' + str(selected[0]) + ' did not accept connections within ' + str(wait) + ' s.' + hint + '\n\nOutput:\n' + output

    def _http_request(self, arguments):
        port, path = self._port(arguments), _app_path(arguments.get('path'))
        method = arguments.get('method', 'GET')
        if method not in METHODS:
            raise ValueError('method must be one of ' + ', '.join(METHODS))
        headers = arguments.get('headers') or {}
        if not isinstance(headers, dict) or len(headers) > 20 or any(not isinstance(k, str) or not isinstance(v, str) or any(c in k + v for c in '\r\n') for k, v in headers.items()):
            raise ValueError('headers must be at most 20 single-line text values')
        body = arguments.get('body')
        if body is not None and (not isinstance(body, str) or len(body) > 100000):
            raise ValueError('body must be text of at most 100,000 characters')
        connection = http.client.HTTPConnection('127.0.0.1', port, timeout=15)
        try:
            started = time.monotonic()
            connection.request(method, path, body=body.encode() if body is not None else None, headers=headers)
            response = connection.getresponse()
            data = response.read(TEXT_LIMIT + 1)
            elapsed = int((time.monotonic() - started) * 1000)
        except ConnectionRefusedError:
            raise ValueError('nothing is listening on port ' + str(port) + ('; the app has exited' if not self._running() else '')) from None
        finally:
            connection.close()
        shown = '\n'.join(name + ': ' + value for name, value in response.getheaders() if name.lower() in ('content-type', 'content-length', 'location', 'set-cookie', 'cache-control'))
        text = data[:TEXT_LIMIT].decode('utf-8', 'replace')
        return (method + ' ' + path + ' -> ' + str(response.status) + ' ' + response.reason + ' in ' + str(elapsed) + ' ms\n' + shown + '\n\n'
                + (text if text else '(empty body)') + ('\n\n[body truncated]' if len(data) > TEXT_LIMIT else ''))

    def _view_page(self, arguments):
        browser = _browser()
        if not browser:
            raise ValueError('headless Chrome was not found (set RELAY_REVIEW_BROWSER to its executable); use http_request instead')
        port, path = self._port(arguments), _app_path(arguments.get('path'))
        wait = _bounded(arguments.get('wait_ms'), 1500, 0, 10000, 'wait_ms')
        width = _bounded(arguments.get('width'), 1280, 320, 2560, 'width')
        height = _bounded(arguments.get('height'), 800, 240, 2000, 'height')
        screenshot = arguments.get('screenshot', True) is not False
        self._egress()
        page = PageView(browser, self.ports, self.proxy.port if self.proxy and self.proxy.domains else None)
        try:
            result = page.open('http://127.0.0.1:' + str(port) + path, wait, width, height, screenshot)
        finally:
            page.close()
        lines = ['Viewed ' + path + ' (' + str(width) + 'x' + str(height) + '). Title: ' + (result['title'] or '(none)')]
        for heading, items in (('Uncaught errors', result['errors']), ('Console', result['console']), ('Failed requests', result['failed'])):
            if items:
                lines += ['', heading + ':'] + ['- ' + item for item in items[:40]]
        if result['failed']:
            lines.append('(Requests to other local services, and to hosts the project\'s internet access does not allow, are blocked in review.)')
        lines += ['', 'Visible text:', result['text'][:8000] or '(no visible text)']
        return '\n'.join(lines), result['screenshot']

    def _app_logs(self, arguments):
        lines = _bounded(arguments.get('lines'), 80, 1, 400, 'lines')
        if self.child is None:
            return 'No app has been started in this task.'
        state = 'running' if self._running() else 'exited with code ' + str(self.child.returncode)
        return 'App is ' + state + '.\n\n' + (self._tail(lines) or '(no output)') + self._internet_log()

    def _fetch_url(self, arguments):
        settings = self._egress()
        url, method = arguments.get('url'), arguments.get('method', 'GET')
        if not isinstance(url, str) or len(url) > 4000 or any(ord(char) < 33 for char in url):
            raise ValueError('url must be a single http:// or https:// address')
        if method not in METHODS:
            raise ValueError('method must be one of ' + ', '.join(METHODS))
        headers = arguments.get('headers') or {}
        if not isinstance(headers, dict) or len(headers) > 20 or any(not isinstance(k, str) or not isinstance(v, str) or any(c in k + v for c in '\r\n') for k, v in headers.items()):
            raise ValueError('headers must be at most 20 single-line text values')
        body = arguments.get('body')
        if body is not None and (not isinstance(body, str) or len(body) > 100000):
            raise ValueError('body must be text of at most 100,000 characters')
        response, data, truncated, elapsed = internet.fetch(url, method, headers, body, settings['domains'], settings['keys'])
        shown = '\n'.join(name + ': ' + value for name, value in response.getheaders() if name.lower() in ('content-type', 'content-length', 'location', 'retry-after', 'x-ratelimit-remaining'))
        text = data[:TEXT_LIMIT].decode('utf-8', 'replace')
        return (method + ' ' + url + ' -> ' + str(response.status) + ' ' + response.reason + ' in ' + str(elapsed) + ' ms\n' + shown + '\n\n'
                + (text or '(empty body)') + ('\n\n[body truncated]' if truncated or len(data) > TEXT_LIMIT else ''))

    def _run_command(self, arguments):
        argv, cwd = self._command(arguments)
        timeout = _bounded(arguments.get('timeout_seconds'), 60, 1, 300, 'timeout_seconds')
        settings = self._egress()
        policy, environment = self._sandbox([], settings)
        started = time.monotonic()
        child = subprocess.Popen([SANDBOX, '-p', policy, *argv], cwd=cwd, env=environment, stdin=subprocess.DEVNULL,
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            output, _ = child.communicate(timeout=timeout)
            status = 'exited with code ' + str(child.returncode)
        except subprocess.TimeoutExpired:
            stop_group(child)
            output, status = child.stdout.read() if child.stdout else b'', 'was stopped after ' + str(timeout) + ' s'
        text = output[-TEXT_LIMIT:].decode('utf-8', 'replace')
        text = re.sub(r'\x1b(?:\][^\x07]*(?:\x07|\x1b\\)|\[[0-?]*[ -/]*[@-~])', '', text)
        reach = ('with internet access to ' + internet.describe(settings['domains'])) if settings['domains'] else 'without internet access (this project has it turned off)'
        return ('Command ' + status + ' in ' + str(int((time.monotonic() - started) * 1000)) + ' ms, run ' + reach + '.\n\n' + (text or '(no output)') + self._internet_log())

    def _stop_app(self, arguments):
        for guard in self.guards:
            guard.close()
        self.guards = []
        if self.child is None:
            self.ports = ()
            return 'No app is running.'
        stop_group(self.child)
        self.child, self.ports = None, ()
        return 'App stopped.'


class PageView:
    """One headless Chrome page driven over a private DevTools pipe.

    Only the app's ports bypass the proxy, which is the session's egress proxy
    or, without internet access, a closed port; so page scripts cannot reach
    other local services, and reach the Internet only as the project allows.
    A fresh temporary profile holds no cookies or credentials.
    """
    def __init__(self, browser, allowed_ports, proxy_port=None):
        self.profile = tempfile.mkdtemp(prefix='relay-review-')
        # Without internet access the proxy is a closed port, so every request fails.
        closed = proxy_port or _free_port()
        bypass = ';'.join(['<-loopback>'] + ['127.0.0.1:' + str(port) for port in allowed_ports])
        flags = ['--headless=new', '--use-mock-keychain', '--password-store=basic', '--no-first-run', '--no-default-browser-check',
                 '--disable-extensions', '--disable-background-networking', '--disable-component-update', '--disable-sync',
                 '--disable-default-apps', '--user-data-dir=' + self.profile, '--remote-debugging-pipe',
                 '--proxy-server=http://127.0.0.1:' + str(closed), '--proxy-bypass-list=' + bypass,
                 '--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1', '--force-webrtc-ip-handling-policy=disable_non_proxied_udp', 'about:blank']
        to_browser, self.commands = os.pipe()
        self.replies, from_browser = os.pipe()
        # Chrome reads commands on fd 3 and writes replies on fd 4; the shell
        # maps them without an unsafe preexec hook in this threaded server.
        try:
            self.process = subprocess.Popen(['/bin/sh', '-c', 'exec "$0" "$@" 3<&' + str(to_browser) + ' 4>&' + str(from_browser), browser, *flags],
                                            pass_fds=(to_browser, from_browser), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                            stderr=subprocess.DEVNULL, start_new_session=True)
        finally:
            os.close(to_browser)
            os.close(from_browser)
        self.buffer, self.serial, self.events = b'', 0, []

    def _read(self, deadline):
        while b'\0' not in self.buffer:
            ready, _, _ = select.select([self.replies], [], [], max(0, deadline - time.monotonic()))
            if not ready:
                return None
            chunk = os.read(self.replies, 1 << 20)
            if not chunk:
                raise OSError('headless Chrome closed unexpectedly')
            self.buffer += chunk
        raw, self.buffer = self.buffer.split(b'\0', 1)
        return json.loads(raw)

    def _call(self, method, params=None, session=None, timeout=15):
        self.serial += 1
        message = {'id': self.serial, 'method': method, 'params': params or {}}
        if session:
            message['sessionId'] = session
        os.write(self.commands, json.dumps(message).encode() + b'\0')
        deadline = time.monotonic() + timeout
        while True:
            reply = self._read(deadline)
            if reply is None:
                raise TimeoutError('headless Chrome did not answer ' + method)
            if reply.get('id') == self.serial:
                if reply.get('error'):
                    raise OSError('headless Chrome refused ' + method + ': ' + str(reply['error'].get('message')))
                return reply.get('result', {})
            self.events.append(reply)

    def _settle(self, seconds):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            reply = self._read(deadline)
            if reply:
                self.events.append(reply)

    def open(self, url, wait_ms, width, height, screenshot):
        target = self._call('Target.createTarget', {'url': 'about:blank'})['targetId']
        session = self._call('Target.attachToTarget', {'targetId': target, 'flatten': True})['sessionId']
        for method in ('Page.enable', 'Runtime.enable', 'Log.enable'):
            self._call(method, session=session)
        self._call('Emulation.setDeviceMetricsOverride', {'width': width, 'height': height, 'deviceScaleFactor': 1, 'mobile': False}, session=session)
        navigation = self._call('Page.navigate', {'url': url}, session=session)
        if navigation.get('errorText'):
            raise ValueError('the page could not be loaded: ' + navigation['errorText'])
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and not any(event.get('method') == 'Page.loadEventFired' for event in self.events):
            reply = self._read(deadline)
            if reply:
                self.events.append(reply)
        self._settle(wait_ms / 1000)
        evaluate = lambda expression: self._call('Runtime.evaluate', {'expression': expression, 'returnByValue': True}, session=session).get('result', {}).get('value') or ''
        result = {'title': evaluate('document.title'), 'text': evaluate('document.body ? document.body.innerText : ""'), 'screenshot': None}
        if screenshot:
            result['screenshot'] = self._call('Page.captureScreenshot', {'format': 'png'}, session=session)['data']
        console, errors, failed = [], [], []
        for event in self.events:
            params = event.get('params', {})
            if event.get('method') == 'Runtime.consoleAPICalled':
                console.append(params.get('type', 'log') + ': ' + ' '.join(str(arg.get('value', arg.get('description', ''))) for arg in params.get('args', []))[:500])
            elif event.get('method') == 'Runtime.exceptionThrown':
                details = params.get('exceptionDetails', {})
                errors.append((details.get('exception', {}).get('description') or details.get('text', 'Uncaught error')).splitlines()[0][:500])
            elif event.get('method') == 'Log.entryAdded' and params.get('entry', {}).get('source') == 'network':
                entry = params['entry']
                failed.append(entry.get('text', '')[:200] + ' ' + entry.get('url', '')[:200])
        result.update({'console': console, 'errors': errors, 'failed': failed})
        return result

    def close(self):
        try:
            self._call('Browser.close', timeout=5)
        except (OSError, TimeoutError, ValueError):
            pass
        stop_group(self.process)
        for fd in (self.commands, self.replies):
            try:
                os.close(fd)
            except OSError:
                pass
        shutil.rmtree(self.profile, ignore_errors=True)
