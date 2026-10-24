"""Narrow inference proxy. No general URL forwarding, CONNECT, cookies, or logs.

Only the separate proxy container has the organization API credential. The coding
container can invoke this one inference capability but cannot read the key or
route arbitrary network traffic through it. No host port is published.
"""
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, HTTPRedirectHandler, build_opener

ALLOWED = {('POST', '/v1/responses'), ('GET', '/v1/models')}
MAX_BODY = 2 * 1024 * 1024

class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

urlopen = build_opener(NoRedirect()).open

class Proxy(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    def log_message(self, fmt, *args): pass
    def deny(self, status):
        body = json.dumps({'error': {'message': 'Inference proxy request refused', 'type': 'proxy_refusal'}}).encode()
        self.send_response(status); self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body))); self.end_headers(); self.wfile.write(body)
    def forward(self):
        token = os.environ.get('RELAY_CODEX_PROXY_TOKEN', '')
        key = os.environ.get('RELAY_ORG_OPENAI_API_KEY', '')
        if not token or not key or not hmac.compare_digest(self.headers.get('Authorization', ''), 'Bearer ' + token):
            self.deny(403); return
        parts = urlsplit(self.path)
        if parts.scheme or parts.netloc or parts.query or (self.command, parts.path) not in ALLOWED:
            self.deny(403); return
        if self.headers.get('Transfer-Encoding'):
            self.deny(400); return
        try: length = int(self.headers.get('Content-Length', '0'))
        except ValueError: self.deny(400); return
        if length < 0 or length > MAX_BODY:
            self.deny(413); return
        body = self.rfile.read(length) if length else None
        if self.command == 'POST':
            try:
                payload = json.loads(body or b'{}')
                if not isinstance(payload, dict): raise ValueError()
                # Do not persist provider-side conversations, regardless of generated input.
                payload['store'] = False
                body = json.dumps(payload).encode()
            except ValueError:
                self.deny(400); return
        request = Request('https://api.openai.com' + parts.path, data=body, method=self.command,
                          headers={'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json',
                                   'Accept': self.headers.get('Accept', 'text/event-stream'),
                                   'OpenAI-Beta': self.headers.get('OpenAI-Beta', '')})
        try:
            try: upstream = urlopen(request, timeout=120)
            except HTTPError as exc: upstream = exc
            with upstream:
                self.send_response(upstream.status)
                self.send_header('Content-Type', upstream.headers.get('Content-Type', 'application/json'))
                for header in ('Retry-After','X-Request-Id','X-RateLimit-Reset-Requests','X-RateLimit-Reset-Tokens'):
                    value=upstream.headers.get(header)
                    if value and '\r' not in value and '\n' not in value:
                        self.send_header(header,value[:200])
                self.send_header('Connection', 'close'); self.end_headers()
                carry = b''; secret = key.encode(); keep = max(len(secret) - 1, 1)
                while True:
                    chunk = upstream.read1(16384)
                    if not chunk: break
                    # Keep a suffix so an upstream error split across chunks cannot leak the key.
                    carry = (carry + chunk).replace(secret, b'[REDACTED]')
                    if len(carry) > keep:
                        self.wfile.write(carry[:-keep]); self.wfile.flush()
                        carry = carry[-keep:]
                self.wfile.write(carry.replace(secret, b'[REDACTED]')); self.wfile.flush()
                self.close_connection = True
        except (URLError, OSError, TimeoutError):
            # If streaming has started the connection simply closes; never leak exception details.
            self.close_connection = True
    do_POST = forward
    do_GET = forward
    def do_CONNECT(self): self.deny(403)

if __name__ == '__main__':
    if not os.environ.get('RELAY_ORG_OPENAI_API_KEY') or not os.environ.get('RELAY_CODEX_PROXY_TOKEN'):
        raise SystemExit('Configure organization API key and proxy capability through server-side secrets')
    ThreadingHTTPServer(('0.0.0.0', 8080), Proxy).serve_forever()
