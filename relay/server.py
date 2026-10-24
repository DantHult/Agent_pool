"""Loopback-only demo server. Replace demo login before remote deployment."""
import argparse
import fcntl
import json
import os
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from .store import Relay
from .auth import LocalDemoIdentityProvider
from .config import load_env_file
from .security import reject_credentials, clean

STATIC=Path(__file__).resolve().parent.parent/'static'

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args): pass  # No prompt, cookie, or credential logging.
    def reply(self,status,data,mime='application/json',cookie=None):
        body=json.dumps(data).encode() if mime=='application/json' else data
        self.send_response(status); self.send_header('Content-Type',mime); self.send_header('Content-Length',str(len(body)))
        self.send_header('Cache-Control','no-store'); self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('Content-Security-Policy',"default-src 'self'; style-src 'self'; script-src 'self'; frame-ancestors 'none'")
        if cookie: self.send_header('Set-Cookie',cookie)
        self.end_headers(); self.wfile.write(body)
    def identity(self):
        cookie=SimpleCookie(); cookie.load(self.headers.get('Cookie',''))
        token=cookie.get('relay_session')
        if not token: raise PermissionError('Sign in to the local demo')
        return self.server.identity_provider.resolve(token.value)
    def guard(self):
        # Block DNS rebinding and cross-origin mutations; no CORS access is granted.
        host=self.headers.get('Host','')
        if host not in (f'127.0.0.1:{self.server.server_port}',f'localhost:{self.server.server_port}'): raise PermissionError('Invalid host')
        origin=self.headers.get('Origin')
        if origin and origin not in (f'http://127.0.0.1:{self.server.server_port}',f'http://localhost:{self.server.server_port}'): raise PermissionError('Invalid origin')
        if self.headers.get('Sec-Fetch-Site')=='cross-site': raise PermissionError('Cross-site request blocked')
    def do_GET(self):
        try:
            self.guard(); url=urlparse(self.path); path=url.path
            if not path.startswith('/api/'):
                names={'/':'index.html','/app.js':'app.js','/style.css':'style.css'}
                if path not in names: return self.reply(404,{'error':'Not found'})
                name=names[path]; mime={'html':'text/html; charset=utf-8','js':'text/javascript; charset=utf-8','css':'text/css; charset=utf-8'}[name.split('.')[-1]]
                return self.reply(200,(STATIC/name).read_bytes(),mime)
            user=self.identity(); relay=self.server.relay
            with relay.lock:
                if path=='/api/overview': result=relay.overview(user)
                elif path=='/api/workers':
                    q=parse_qs(url.query)
                    result=relay.workers(user,q.get('project_id',[None])[0],q.get('mine',['0'])[0]=='1')
                elif path.startswith('/api/projects/'): result=relay.detail(user,path.split('/')[-1])
                elif path.startswith('/api/tasks/') and path.endswith('/result'):
                    result=relay.execution_result(user,path.split('/')[3])
                elif path.startswith('/api/conversations/') and path.endswith('/events'):
                    q=parse_qs(url.query); result=relay.events(user,path.split('/')[3],int(q.get('after',['0'])[0]),int(q.get('limit',['50'])[0]))
                elif path.startswith('/api/artifacts/'):
                    result=relay.artifact(user,path.split('/')[-1])
                else: return self.reply(404,{'error':'Not found'})
            self.reply(200,result)
        except PermissionError as e: self.reply(403,{'error':clean(e)})
        except (ValueError,KeyError) as e: self.reply(400,{'error':clean(e)})
        except Exception: self.reply(500,{'error':'Server error; request did not report success'})
    def do_POST(self):
        try:
            self.guard()
            if not self.headers.get('Content-Type','').startswith('application/json'): raise ValueError('JSON required')
            length=int(self.headers.get('Content-Length','0'))
            if length<1 or length>16000: raise ValueError('Invalid request size')
            data=json.loads(self.rfile.read(length)); method=self.path.removeprefix('/api/')
            if not isinstance(data,dict): raise ValueError('JSON object required')
            reject_credentials(data)
            if method=='login':
                token=self.server.identity_provider.login(data['user_id'])
                return self.reply(200,{'ok':True},cookie=f'relay_session={token}; HttpOnly; SameSite=Strict; Path=/')
            user=self.identity(); result=self.server.relay.mutate(user,method,data); self.reply(200,result)
        except PermissionError as e: self.reply(403,{'error':clean(e)})
        except (ValueError,KeyError) as e: self.reply(400,{'error':clean(e),'code':getattr(e,'code','invalid_request')})
        except Exception: self.reply(500,{'error':'Operation failed; inspect task state before retrying'})

class RelayServer(ThreadingHTTPServer):
    def server_close(self):
        super().server_close()
        # Keep the directory lock until every task writer has stopped.
        # Releasing it first lets a second server recover the same worktree.
        if hasattr(self,'relay'): self.relay.close()
        if hasattr(self, 'workspace_lock'): self.workspace_lock.close()

class ServerAlreadyRunning(RuntimeError):
    """Another server owns the data directory; leave its lock and data intact."""

    def __init__(self, root, metadata):
        url = metadata.get('url')
        address = f'Open {url}.' if url else 'Check the running Relay terminal for its URL (default: http://127.0.0.1:8000).'
        super().__init__(
            f'Relay is already running for {Path(root).resolve()}. {address}\n'
            'To restart, stop the existing server with Ctrl+C in its terminal, then run this command again. '
            'For a separate demo, use both --data <new-directory> and --port <unused-port>. '
            'Do not delete server.lock while a server is running.'
        )

def make_server(root,port=8000):
    if os.environ.get('RELAY_AUTH_MODE','demo')!='demo':
        raise ValueError('Only explicitly labeled local demo authentication is implemented. Install a verified IdentityProvider before using another mode.')
    Path(root).mkdir(parents=True,exist_ok=True)
    lock=open(Path(root)/'server.lock','a+')
    server=None
    try:
        try:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            lock.seek(0)
            try:
                metadata=json.load(lock)
                if not isinstance(metadata,dict): metadata={}
            except (ValueError,OSError):
                metadata={}
            raise ServerAlreadyRunning(root,metadata) from None
        server=RelayServer(('127.0.0.1',port),Handler)
        server.workspace_lock=lock; server.relay=Relay(root)
        server.identity_provider=LocalDemoIdentityProvider(server.relay.user)
        server.sessions=server.identity_provider.sessions
        # Only the lock owner may replace metadata. Never unlink the lock file:
        # a new inode could allow two writers to own the same workspace.
        lock.seek(0); lock.truncate()
        json.dump({'pid':os.getpid(),'url':f'http://127.0.0.1:{server.server_port}'},lock)
        lock.flush()
        return server
    except Exception:
        if server is not None:
            server.server_close()
            if hasattr(server,'relay'): server.relay.db.close()
        lock.close(); raise

def main():
    parser=argparse.ArgumentParser(); parser.add_argument('--port',type=int,default=8000); parser.add_argument('--data',default=None); parser.add_argument('--env-file',type=Path,help='Explicit Relay environment file'); args=parser.parse_args()
    try:
        if args.env_file: load_env_file(args.env_file)
    except (ValueError,OSError) as exc: parser.exit(2,clean(str(exc))+'\n')
    args.data=args.data or os.environ.get('RELAY_DATA_DIR','data')
    try:
        server=make_server(args.data,args.port)
    except ServerAlreadyRunning as exc:
        parser.exit(1,f'{exc}\n')
    print(f'Relay local demo: http://127.0.0.1:{server.server_port} — simulated Codex execution',flush=True)
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close()
if __name__=='__main__': main()
