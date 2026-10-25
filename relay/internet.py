"""Per-project internet access for agents, only through Relay.

Agents' shells stay offline. By default a project's agents may reach any public
host; its administrator can instead list the only domains they may reach, or
turn internet access off. Relay's own proxy (for apps under review) and
fetch_url (for the agent) enforce that choice, refuse anything that resolves to
this machine or a private network, and, for listed domains only, add API keys
named by the project from Relay's own environment (RELAY_SECRET_<NAME>) without
showing their values to agents.
"""
import collections
import http.client
import ipaddress
import os
import re
import select
import socket
import socketserver
import ssl
import threading
import time
from urllib.parse import urlsplit

from .security import SECRET_PREFIX

DOMAIN = re.compile(r'(\*\.)?(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}')
KEY_NAME = re.compile(r'[A-Z][A-Z0-9_]{0,63}')
PORTS = (80, 443)
PLACEHOLDER = re.compile(r'\$\{([A-Z][A-Z0-9_]{0,63})\}')
MODES = ('public', 'listed', 'off')
# Relay's own marker for "any public host"; normalize_domains never accepts it.
ANY_PUBLIC = '*'
# python.org builds ship no CA bundle; macOS provides one here.
SYSTEM_CERTIFICATES = '/private/etc/ssl/cert.pem'


def tls_context():
    if not ssl.get_default_verify_paths().cafile and os.path.isfile(SYSTEM_CERTIFICATES):
        return ssl.create_default_context(cafile=SYSTEM_CERTIFICATES)
    return ssl.create_default_context()


def normalize_domains(values):
    if not isinstance(values, list) or len(values) > 50:
        raise ValueError('List at most 50 domains')
    domains = []
    for value in values:
        domain = value.strip().lower().rstrip('.') if isinstance(value, str) else ''
        if not domain:
            continue
        if not DOMAIN.fullmatch(domain) or domain in ('localhost',) or domain.endswith(('.local', '.localhost', '.internal')):
            raise ValueError(domain + ' is not a public domain name; use a host like api.example.com or *.example.com, without scheme, port or path')
        domains.append(domain)
    return list(dict.fromkeys(domains))


def normalize_key_names(values):
    if not isinstance(values, list) or len(values) > 20:
        raise ValueError('List at most 20 API key names')
    names = []
    for value in values:
        name = value.strip() if isinstance(value, str) else ''
        if name.upper().startswith(SECRET_PREFIX):
            name = name[len(SECRET_PREFIX):]
        if not name:
            continue
        if not KEY_NAME.fullmatch(name):
            raise ValueError(name + ' is not a key name; use capital letters, digits and underscores, like WEATHER_API_KEY')
        names.append(name)
    return list(dict.fromkeys(names))


def configured(name):
    return bool(os.environ.get(SECRET_PREFIX + name))


def allowed(host, domains):
    host = (host or '').lower().rstrip('.')
    return any(domain == ANY_PUBLIC or host == domain or (domain.startswith('*.') and host.endswith(domain[1:])) for domain in domains)


def describe(domains):
    return 'any public host' if ANY_PUBLIC in domains else ', '.join(domains)


def resolve_public(host, port):
    """One public address for host; refuse if any address is private or local.

    Checking every resolved address (and then connecting to the checked one)
    stops an allowed name from being pointed at this machine or the LAN.
    """
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror:
        raise ValueError(host + ' could not be resolved') from None
    addresses = list(dict.fromkeys(info[4][0] for info in infos))
    if not addresses or any(not ipaddress.ip_address(address.split('%')[0]).is_global for address in addresses):
        raise ValueError(host + ' resolves to a private or local address, which Relay never allows')
    return addresses[0]


def check(host, port, domains, resolve):
    if port not in PORTS:
        raise ValueError('only ports 80 and 443 are allowed, not ' + str(port))
    if not domains:
        raise ValueError('this project has internet access turned off; a project administrator can turn it on in Relay')
    if not allowed(host, domains):
        raise ValueError(host + ' is not in this project\'s allowed internet domains (' + ', '.join(domains) + ')')
    return resolve(host, port)


class EgressProxy:
    """HTTP(S) forward proxy for one review session, bound to 127.0.0.1.

    The session's Seatbelt profile lets project code connect only to this
    port, so every request it makes to the Internet passes this allowlist.
    """
    def __init__(self, domains, resolve=None):
        self.domains = list(domains)
        self.resolve = resolve or (lambda host, port: resolve_public(host, port))
        self.recent = collections.deque(maxlen=200)
        proxy = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                proxy._handle(self.request)

        class Server(socketserver.ThreadingTCPServer):
            daemon_threads = True
            allow_reuse_address = True

        self.server = Server(('127.0.0.1', 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def _note(self, text):
        self.recent.append(time.strftime('%H:%M:%S') + ' ' + text)

    def _handle(self, client):
        client.settimeout(30)
        head = b''
        while b'\r\n\r\n' not in head:
            chunk = client.recv(65536)
            if not chunk or len(head) > 65536:
                return
            head += chunk
        head, rest = head.split(b'\r\n\r\n', 1)
        lines = head.decode('latin-1').split('\r\n')
        try:
            method, target, _ = lines[0].split(' ', 2)
            if method == 'CONNECT':
                host, _, port = target.rpartition(':')
                host, port = host.strip('[]'), int(port)
            else:
                parts = urlsplit(target)
                if parts.scheme != 'http' or not parts.hostname:
                    raise ValueError('send HTTPS through CONNECT and HTTP as an absolute URL')
                host, port = parts.hostname, parts.port or 80
            address = check(host, port, self.domains, self.resolve)
        except ValueError as exc:
            self._note('blocked ' + lines[0].split(' ')[1][:200] + ': ' + str(exc))
            body = ('Relay blocked this request: ' + str(exc) + '\n').encode()
            client.sendall(b'HTTP/1.1 403 Forbidden\r\nContent-Type: text/plain\r\nConnection: close\r\nContent-Length: ' + str(len(body)).encode() + b'\r\n\r\n' + body)
            return
        try:
            upstream = socket.create_connection((address, port), timeout=10)
        except OSError as exc:
            self._note('failed ' + host + ':' + str(port) + ': ' + str(exc))
            client.sendall(b'HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\nContent-Length: 0\r\n\r\n')
            return
        self._note('allowed ' + method + ' ' + host + ':' + str(port))
        with upstream:
            if method == 'CONNECT':
                client.sendall(b'HTTP/1.1 200 Connection Established\r\n\r\n')
            else:
                # Origin-form request on a fresh connection that closes after
                # one response, so a reused socket cannot reach another host.
                path = (urlsplit(target).path or '/') + ('?' + urlsplit(target).query if urlsplit(target).query else '')
                headers = [line for line in lines[1:] if not line.lower().startswith(('proxy-', 'connection:', 'keep-alive:'))]
                upstream.sendall((' '.join((method, path, 'HTTP/1.1')) + '\r\n' + '\r\n'.join(headers + ['Connection: close']) + '\r\n\r\n').encode('latin-1') + rest)
                rest = b''
            if rest:
                upstream.sendall(rest)
            self._pipe(client, upstream)

    @staticmethod
    def _pipe(client, upstream, idle=60, limit=100 * 1024 * 1024):
        moved, sockets = 0, [client, upstream]
        while moved < limit:
            ready, _, _ = select.select(sockets, [], [], idle)
            if not ready:
                return
            for source in ready:
                data = source.recv(65536)
                if not data:
                    return
                (upstream if source is client else client).sendall(data)
                moved += len(data)

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class _Pinned:
    """Connect to the address already checked, keeping the real hostname for TLS."""
    def connect(self):
        self.sock = socket.create_connection((self.address, self.port), self.timeout)
        if isinstance(self, http.client.HTTPSConnection):
            self.sock = self._context.wrap_socket(self.sock, server_hostname=self.host)


class PinnedHTTP(_Pinned, http.client.HTTPConnection):
    pass


class PinnedHTTPS(_Pinned, http.client.HTTPSConnection):
    pass


def substitute(text, keys):
    """Replace ${NAME} with that enabled key's value, on Relay's side only."""
    def value(match):
        if match.group(1) not in keys:
            raise ValueError('${' + match.group(1) + '} is not an API key this project enables' + (' (available: ' + ', '.join(sorted(keys)) + ')' if keys else ''))
        return keys[match.group(1)]
    return PLACEHOLDER.sub(value, text)


def fetch(url, method, headers, body, domains, keys, resolve=None, limit=200000):
    parts = urlsplit(url)
    if parts.scheme not in ('http', 'https') or not parts.hostname:
        raise ValueError('url must start with http:// or https://')
    port = parts.port or (443 if parts.scheme == 'https' else 80)
    address = check(parts.hostname, port, domains, resolve or resolve_public)
    target = substitute((parts.path or '/') + ('?' + parts.query if parts.query else ''), keys)
    sent = {name: substitute(value, keys) for name, value in headers.items()}
    sent.setdefault('User-Agent', 'Relay-agent/1.0')
    connection = PinnedHTTPS(parts.hostname, port, timeout=20, context=tls_context()) if parts.scheme == 'https' else PinnedHTTP(parts.hostname, port, timeout=20)
    connection.address = address
    try:
        started = time.monotonic()
        connection.request(method, target, body=substitute(body, keys).encode() if body is not None else None, headers=sent)
        response = connection.getresponse()
        data = response.read(limit + 1)
        return response, data[:limit], len(data) > limit, int((time.monotonic() - started) * 1000)
    except ssl.SSLError as exc:
        raise ValueError('TLS failed for ' + parts.hostname + ': ' + str(exc)) from None
    finally:
        connection.close()
