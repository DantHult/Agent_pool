"""Relay HTTP boundary: invite-only behind HTTPS; explicit loopback demo mode."""
import argparse
import ipaddress
import json
import os
import re
import socket
import threading
import time
from http.cookies import SimpleCookie, CookieError
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from .store import Relay
from .auth import LocalDemoIdentityProvider
from .accounts import InviteIdentityProvider,LoginRequired,csrf_token,digest
from .config import load_env_file
from .fs_lock import current_uid, process_uid, try_exclusive_lock
from .security import reject_credentials,clean

STATIC=Path(__file__).resolve().parent.parent/'static'

def public_origin(value,allow_http=False):
    url=urlparse(value)
    if not url.hostname or url.username or url.password or url.path not in ('','/') or url.query or url.fragment:raise ValueError('RELAY_PUBLIC_URL must be one origin without a path or credentials')
    if url.scheme!='https' and not (allow_http and url.scheme=='http' and url.hostname in ('localhost','127.0.0.1')):raise ValueError('HTTPS is required; HTTP is allowed only for explicit loopback testing')
    return value.rstrip('/')

class Handler(BaseHTTPRequestHandler):
    server_version='Relay'
    sys_version=''
    def setup(self):
        super().setup();self.connection.settimeout(15)
    def log_message(self,fmt,*args):pass  # No URL, prompt, cookie or password logs.
    def reply(self,status,data,mime='application/json',cookie=None,extra=None):
        body=json.dumps(data).encode() if mime=='application/json' else data
        self.send_response(status);self.send_header('Content-Type',mime);self.send_header('Content-Length',str(len(body)))
        self.send_header('Cache-Control','no-store');self.send_header('X-Content-Type-Options','nosniff');self.send_header('Referrer-Policy','no-referrer')
        # Monaco lays out editor lines using dynamic inline styles. Scripts and
        # editor workers remain same-origin; eval, CDNs and frames stay blocked.
        self.send_header('Content-Security-Policy',"default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self'; worker-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'; object-src 'none'")
        if self.server.secure:self.send_header('Strict-Transport-Security','max-age=31536000')
        if cookie:self.send_header('Set-Cookie',cookie)
        for name,value in (extra or {}).items():self.send_header(name,value)
        self.end_headers();self.wfile.write(body)
    def token(self):
        cookie=SimpleCookie()
        try:cookie.load(self.headers.get('Cookie',''))
        except CookieError:raise LoginRequired('Sign in to Relay') from None
        token=cookie.get(self.server.cookie_name)
        return token.value if token else ''
    def identity(self):
        token=self.token()
        if not token:raise LoginRequired('Sign in to Relay') if self.server.auth_mode!='demo' else PermissionError('Sign in to the local demo')
        return self.server.identity_provider.resolve(token)
    def cookie(self,token):
        ttl=self.server.identity_provider.ttl if self.server.auth_mode!='demo' else 28800
        return f'{self.server.cookie_name}={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age={ttl if token else 0}'+('; Secure' if self.server.secure else '')
    def guard(self,mutation=False):
        host=self.headers.get('Host','');origin=self.headers.get('Origin')
        if self.server.auth_mode=='local-owner':
            if not ipaddress.ip_address(self.client_address[0]).is_loopback or any(name.lower()=='forwarded' or name.lower().startswith('x-forwarded-') for name in self.headers):raise PermissionError('Local-owner mode rejects remote or forwarded requests')
        if self.server.auth_mode=='demo':
            hosts=(f'127.0.0.1:{self.server.server_port}',f'localhost:{self.server.server_port}')
            origins=tuple('http://'+value for value in hosts)
        else:
            hosts=(urlparse(self.server.public_url).netloc,);origins=(self.server.public_url,)
            if self.server.secure and self.headers.get('X-Forwarded-Proto')!='https':raise PermissionError('Use the configured HTTPS endpoint')
        if host not in hosts:raise PermissionError('Invalid host')
        if (origin and origin not in origins) or (mutation and self.server.auth_mode!='demo' and not origin):raise PermissionError('A same-origin request is required')
        if self.headers.get('Sec-Fetch-Site')=='cross-site':raise PermissionError('Cross-site request blocked')
    def csrf(self):
        if self.server.auth_mode=='demo':return
        import hmac
        if not hmac.compare_digest(self.headers.get('X-Relay-CSRF',''),csrf_token(self.token())):raise PermissionError('Session security check failed; reload Relay and try again')
    def client_key(self):
        client=self.client_address[0]
        # Only the loopback HTTPS proxy is trusted; multi-proxy chains are not.
        if self.server.secure and client in ('127.0.0.1','::1'):
            forwarded=self.headers.get('X-Forwarded-For','')
            try:client=str(ipaddress.ip_address(forwarded))
            except ValueError:pass
        return client
    def failure(self,exc):
        if isinstance(exc,(BrokenPipeError,ConnectionResetError,ConnectionAbortedError,socket.timeout)):return
        status=getattr(exc,'status',403 if isinstance(exc,PermissionError) else 400 if isinstance(exc,(ValueError,KeyError,TypeError)) else 500)
        message=clean(exc) if isinstance(exc,(ValueError,PermissionError)) else 'Invalid request fields' if status==400 else 'Operation failed; inspect task state before retrying'
        try:self.reply(status,{'error':message,'code':getattr(exc,'code','request_failed')},extra={'Retry-After':'600'} if status==429 else None)
        except (BrokenPipeError,ConnectionResetError,ConnectionAbortedError,socket.timeout):return
    def stream(self,user,conversation,after):
        relay=self.server.relay
        with relay.lock:relay.events(user,conversation,after,1)
        if self.headers.get('Upgrade'):raise ValueError('WebSocket transport is not enabled; use authenticated event-stream or polling')
        if not self.server.stream_slots.acquire(blocking=False):
            self.reply(429,{'error':'Too many live streams; polling remains available'});return
        try:
            self.send_response(200);self.send_header('Content-Type','text/event-stream');self.send_header('Cache-Control','no-store');self.send_header('X-Accel-Buffering','no');self.send_header('X-Content-Type-Options','nosniff');self.send_header('Connection','close');self.end_headers()
            self.wfile.write(b'retry: 2000\n\n');self.wfile.flush();deadline=time.monotonic()+25
            while time.monotonic()<deadline and not self.server.closing:
                with relay.lock:
                    # Expiration, logout, account disable and project revocation
                    # take effect during an already-open subscription.
                    current=self.identity()
                    events=relay.events(current,conversation,after,100)['events']
                if events:
                    for event in events:
                        after=event['sequence'];self.wfile.write(('id: '+str(after)+'\ndata: '+json.dumps(event)+'\n\n').encode())
                else:self.wfile.write(b': keepalive\n\n')
                self.wfile.flush();time.sleep(.5)
        except (PermissionError,ValueError,OSError):pass
        finally:self.server.stream_slots.release();self.close_connection=True
    def do_GET(self):
        try:
            self.guard();url=urlparse(self.path);path=url.path;q=parse_qs(url.query);relay=self.server.relay
            if path=='/healthz':return self.reply(200,{'status':'ok'})
            if path=='/api/auth/config':return self.reply(200,{'mode':self.server.auth_mode,'secure':self.server.secure,'registration':'offline-owner-only' if self.server.auth_mode=='local-owner' else 'invite-only' if self.server.auth_mode=='invite' else 'demo'})
            if not path.startswith('/api/'):
                names={'/':'index.html','/app.js':'app.js','/style.css':'style.css','/ide.js':'ide.js','/activity.js':'activity.js'}
                if path.startswith('/vendor/'):
                    asset=(STATIC/path.lstrip('/')).resolve()
                    if not asset.is_relative_to((STATIC/'vendor').resolve()) or not asset.is_file():return self.reply(404,{'error':'Not found'})
                    mime={'.js':'text/javascript; charset=utf-8','.css':'text/css; charset=utf-8','.ttf':'font/ttf','.json':'application/json; charset=utf-8'}.get(asset.suffix,'text/plain; charset=utf-8')
                    return self.reply(200,asset.read_bytes(),mime)
                if path not in names:return self.reply(404,{'error':'Not found'})
                name=names[path];mime={'html':'text/html; charset=utf-8','js':'text/javascript; charset=utf-8','css':'text/css; charset=utf-8'}[name.split('.')[-1]]
                return self.reply(200,(STATIC/name).read_bytes(),mime)
            user=self.identity()
            if path=='/api/auth/session':
                with relay.lock:profile=relay.user(user)
                return self.reply(200,{'user':profile,'csrf_token':csrf_token(self.token()) if self.server.auth_mode!='demo' else None})
            if re.fullmatch(r'/api/conversations/[^/]+/stream',path):return self.stream(user,path.split('/')[3],int(q.get('after',[self.headers.get('Last-Event-ID','0')])[0]))
            if self.headers.get('Upgrade'):raise ValueError('WebSocket transport is not enabled; use event-stream or polling')
            with relay.lock:
                if path=='/api/overview':result=relay.overview(user)
                elif path=='/api/ide/state':result=self.server.ide.state(user,q.get('project_id',[''])[0],q.get('client_id',[''])[0],digest(self.token()))
                elif path=='/api/ide/file':result=self.server.ide.read_file(user,q.get('project_id',[''])[0],q.get('path',[''])[0],q.get('create',['0'])[0]=='1')
                elif path=='/api/ide/entry':
                    try:result=self.server.ide.files.inspect(user,q.get('project_id',[''])[0],q.get('path',[''])[0])
                    except OSError as exc:raise ValueError('Cannot inspect project entry: '+(exc.strerror or 'filesystem error')) from None
                elif path=='/api/tasks':result=relay.management.tasks(user,q.get('project_id',[''])[0],q.get('worker_id',[None])[0])
                elif path=='/api/ide/review':result=relay.management.review(user,q.get('project_id',[''])[0],q.get('task_id',[None])[0],q.get('before',[None])[0],q.get('after',[None])[0])
                elif path=='/api/ide/conversations':result=self.server.ide.conversations(user,q.get('project_id',[''])[0])
                elif path=='/api/ide/console':
                    if not self.server.ide.console:raise PermissionError('Console execution requires the pinned local owner')
                    result=self.server.ide.console.poll(user,q.get('session_id',[''])[0],digest(self.token()))
                elif path=='/api/native/profiles':result=relay.native_profiles(user,q.get('project_id',[None])[0])
                elif path=='/api/workers':result=relay.workers(user,q.get('project_id',[None])[0],q.get('mine',['0'])[0]=='1')
                elif re.fullmatch(r'/api/projects/[^/]+/(diff|download)',path):
                    project=path.split('/')[3];relay.access(user,project);detail=relay.detail(user,project)
                    if path.endswith('/diff'):result={'diff':detail['workspace'].get('task_diff',detail['workspace'].get('diff',''))}
                    else:
                        name=q.get('file',[''])[0];files=detail['workspace']['files']
                        if name not in files:raise ValueError('File is not available for download')
                        filename=re.sub(r'[^A-Za-z0-9_.-]','_',Path(name).name)
                        return self.reply(200,files[name].encode(),'text/plain; charset=utf-8',extra={'Content-Disposition':'attachment; filename="'+filename+'"'})
                elif re.fullmatch(r'/api/projects/[^/]+',path):result=relay.detail(user,path.split('/')[-1],q.get('conversation_id',[None])[0])
                elif re.fullmatch(r'/api/tasks/[^/]+/result',path):result=relay.execution_result(user,path.split('/')[3])
                elif re.fullmatch(r'/api/conversations/[^/]+/events',path):result=relay.events(user,path.split('/')[3],int(q.get('after',['0'])[0]),int(q.get('limit',['50'])[0]))
                elif re.fullmatch(r'/api/artifacts/[^/]+',path):result=relay.artifact(user,path.split('/')[-1])
                elif path=='/api/organization/members':
                    if not relay.is_admin(user):raise PermissionError('Administrator required')
                    result={'members':relay.rows('SELECT u.id,u.name,a.email,m.role FROM users u JOIN accounts a ON a.user_id=u.id JOIN organization_memberships m ON m.user_id=u.id WHERE m.organization_id=?',('orgdemo',))}
                else:return self.reply(404,{'error':'Not found'})
            self.reply(200,result)
        except Exception as exc:self.failure(exc)
    def do_POST(self):
        try:
            self.guard(mutation=True)
            if not self.headers.get('Content-Type','').startswith('application/json') or self.headers.get('Transfer-Encoding'):raise ValueError('JSON with a content length is required')
            path=urlparse(self.path).path
            length=int(self.headers.get('Content-Length','0'))
            maximum=1_200_000 if path in ('/api/ide/save','/api/ide/draft') else 16000
            if not 1<=length<=maximum:raise ValueError('Invalid request size')
            data=json.loads(self.rfile.read(length))
            if not path.startswith('/api/') or not isinstance(data,dict):raise ValueError('JSON object and API route required')
            method=path.removeprefix('/api/');provider=self.server.identity_provider;relay=self.server.relay
            if self.server.auth_mode=='local-owner' and (method=='auth/register' or method.startswith('invitations') or method=='membership'):raise PermissionError('Local-owner mode cannot register or delegate to other users')
            if self.server.auth_mode!='demo' and method in ('login','auth/register'):
                fields={'email','password'} if method=='login' else {'email','password','name','invitation_token'}
                if set(data)-fields:raise ValueError('Unexpected Relay account fields; provider credentials are never accepted')
                if method=='auth/register':
                    return self.reply(201,provider.accept(data,client=self.client_key()))
                token=provider.login(data.get('email'),data.get('password'),self.client_key(),self.token())
                return self.reply(200,{'ok':True,'csrf_token':csrf_token(token)},cookie=self.cookie(token))
            reject_credentials(data)
            if method=='login' and self.server.auth_mode=='demo':
                token=provider.login(data['user_id']);return self.reply(200,{'ok':True},cookie=self.cookie(token))
            user=self.identity();self.csrf()
            if method in ('logout','auth/logout-all'):
                if self.server.ide.console:self.server.ide.console.close_auth(digest(self.token()))
                if method=='auth/logout-all' and self.server.auth_mode!='demo':provider.revoke_sessions(user)
                else:provider.logout(self.token())
                return self.reply(200,{'ok':True},cookie=self.cookie(''))
            if method.startswith('ide/console/'):
                console=self.server.ide.console
                if not console:raise PermissionError('Console execution requires the pinned local owner')
                operation=method.removeprefix('ide/console/');binding=digest(self.token())
                if operation=='open':result=console.open(user,data,binding)
                elif operation=='ensure':result=console.ensure(user,data,binding)
                elif operation=='run':result=console.run(user,data,binding)
                elif operation=='input':result=console.write(user,data,binding)
                elif operation=='cancel':result=console.cancel(user,data,binding)
                elif operation=='apply':result=console.apply(user,data,binding)
                elif operation=='close':result=console.close_session(user,data['session_id'],binding)
                else:raise ValueError('Unknown console action')
                return self.reply(200,result)
            if method.startswith('ide/'):
                ide=self.server.ide;project=data['project_id'];binding=digest(self.token());client=data.get('client_id','')
                with relay.lock:
                    if method=='ide/lock':result=ide.acquire(user,project,client,binding,data.get('recover') is True)
                    elif method=='ide/prepare':result=ide.prepare(user,project,client,binding)
                    elif method=='ide/finish':result=ide.finish(user,project,client,binding,data.get('conversation_id'))
                    elif method=='ide/save':result=ide.save(user,project,client,binding,data['path'],data.get('version'),data['content'],data.get('conversation_id'))
                    elif method=='ide/draft':result=ide.draft(user,project,client,binding,data['path'],data.get('version'),data['content'])
                    elif method=='ide/discard':result=ide.discard(user,project,client,binding,data['path'])
                    elif method=='ide/release':result=ide.release(user,project,client,binding,data.get('checkpoint') is True,data.get('conversation_id'))
                    elif method=='ide/git':result=ide.git(user,project,client,binding,data['arguments'],data.get('conversation_id'))
                    elif method=='ide/conversations':result=ide.create_conversation(user,project,data['title'])
                    elif method=='ide/conversations/update':result=ide.update_conversation(user,project,data['conversation_id'],data)
                    elif method=='ide/files':
                        try:result=ide.files.mutate(user,project,client,binding,data)
                        except OSError as exc:raise ValueError('Explorer operation failed: '+(exc.strerror or 'filesystem error')+'. Refresh and check that the parent folder exists and is writable.') from None
                    else:raise ValueError('Unknown editor action')
                return self.reply(200,result)
            if self.server.auth_mode!='demo':
                if method=='invitations':
                    result=provider.invite(user,data);result['invitation_url']=self.server.public_url+'/#invite='+result.pop('invitation_token')
                    return self.reply(201,result)
                if method=='invitations/revoke':return self.reply(200,provider.revoke_invite(user,data['id']))
                if method=='invitations/accept':return self.reply(200,provider.accept(data,user,self.client_key()))
                if method=='auth/revoke-sessions':return self.reply(200,provider.revoke_sessions(user,data.get('user_id')))
            if method=='native/profiles/enroll':
                with relay.lock:result=relay.enroll_native_profile(user,data.get('profile'),data['project_id'])
                return self.reply(200,result)
            result=relay.mutate(user,method,data);self.reply(200,result)
        except Exception as exc:self.failure(exc)

class RelayServer(ThreadingHTTPServer):
    daemon_threads=True
    allow_reuse_address=True
    def __init__(self,*args,**kwargs):
        self.closing=False;self.request_slots=threading.BoundedSemaphore(64);self.stream_slots=threading.BoundedSemaphore(16)
        super().__init__(*args,**kwargs)
    def process_request(self,request,client_address):
        if not self.request_slots.acquire(blocking=False):
            try:request.sendall(b'HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\nConnection: close\r\n\r\n')
            finally:self.shutdown_request(request)
            return
        try:super().process_request(request,client_address)
        except Exception:self.request_slots.release();raise
    def process_request_thread(self,*args):
        try:super().process_request_thread(*args)
        finally:self.request_slots.release()
    def server_close(self):
        self.closing=True;super().server_close()
        if getattr(self,'ide',None) and self.ide.console:self.ide.console.close()
        if hasattr(self,'relay'):self.relay.close()
        from .native import close_native_sessions
        close_native_sessions()
        if hasattr(self,'workspace_lock'):self.workspace_lock.close()

class ServerAlreadyRunning(RuntimeError):
    def __init__(self,root,metadata):
        url=metadata.get('url');address=f'Open {url}.' if url else 'Check the running Relay terminal for its URL (default: http://127.0.0.1:8000).'
        super().__init__(f'Relay is already running for {Path(root).resolve()}. {address}\nTo restart, stop the existing server with Ctrl+C in its terminal, then run this command again. For a separate demo, use both --data <new-directory> and --port <unused-port>. Do not delete server.lock while a server is running.')

def _read_server_status(root):
    """Read live-server metadata without touching the exclusive lock file.

    Windows msvcrt locks are mandatory: other handles cannot read server.lock while
    it is held, so URL/pid live in server.status.json (also written on Unix).
    """
    for name in ('server.status.json','server.lock'):
        path=Path(root)/name
        try:
            metadata=json.loads(path.read_text(encoding='utf-8'))
            if isinstance(metadata,dict):return metadata
        except (ValueError,OSError):pass
    return {}

def _write_server_status(root,metadata):
    path=Path(root)/'server.status.json'
    path.write_text(json.dumps(metadata),encoding='utf-8')

def make_server(root,port=8000,auth_mode=None,origin=None,allow_http=False):
    # Legacy programmatic construction remains loopback demo for existing callers.
    # The deployable CLI explicitly selects invite authentication by default.
    mode=auth_mode or os.environ.get('RELAY_AUTH_MODE','demo')
    if mode not in ('demo','invite','local-owner'):raise ValueError('Unknown authentication mode; use invite, local-owner or explicit local demo')
    Path(root).mkdir(parents=True,exist_ok=True);lock=open(Path(root)/'server.lock','a+');server=None
    try:
        try:try_exclusive_lock(lock)
        except BlockingIOError:
            raise ServerAlreadyRunning(root,_read_server_status(root)) from None
        if mode!='demo' and current_uid()==0:raise ValueError('Run the shared Relay server as a non-root OS user')
        configured=public_origin(origin or os.environ.get('RELAY_PUBLIC_URL',''),allow_http) if mode=='invite' else None
        if mode=='local-owner':
            selected=origin or os.environ.get('RELAY_PUBLIC_URL') or f'http://127.0.0.1:{port}'
            configured=public_origin(selected,True)
            parsed=urlparse(configured)
            if parsed.scheme!='http' or parsed.hostname not in ('localhost','127.0.0.1'):raise ValueError('Local-owner mode requires a direct HTTP loopback origin; shared/public/proxied deployment is forbidden')
            if (parsed.port if parsed.port is not None else 80)!=port:raise ValueError('Local-owner origin port must match the loopback listener')
        server=RelayServer(('127.0.0.1',port),Handler);server.workspace_lock=lock
        server.auth_mode=mode;server.secure=bool(configured and configured.startswith('https://'))
        if configured and urlparse(configured).port==0:configured=configured[:-1]+str(server.server_port)
        server.public_url=configured or f'http://127.0.0.1:{server.server_port}'
        server.cookie_name='relay_local_owner_session' if mode=='local-owner' else '__Host-relay_session' if server.secure else 'relay_session'
        server.relay=Relay(root,demo=mode=='demo',native_enabled=False)
        owner_id=None
        if mode=='local-owner':
            bindings=server.relay.rows('SELECT * FROM local_owner_binding WHERE singleton=1')
            if not bindings or bindings[0]['os_uid']!=process_uid():raise ValueError('No matching local-owner pin. Bootstrap a dedicated directory with scripts/manage_accounts.py bootstrap --local-owner')
            owner_id=bindings[0]['user_id']
            accounts=server.relay.rows('SELECT user_id FROM accounts WHERE disabled=0')
            if len(accounts)!=1 or accounts[0]['user_id']!=owner_id:raise ValueError('Local-owner mode requires its one pinned account; use a dedicated data directory')
            server.relay.local_owner_id=owner_id;server.relay.native_enabled=True
        ttl=int(os.environ.get('RELAY_SESSION_TTL_SECONDS','28800'));idle=int(os.environ.get('RELAY_SESSION_IDLE_SECONDS','1800'))
        if not 600<=ttl<=86400 or not 60<=idle<=ttl:raise ValueError('Session TTL must be 600–86400 seconds and idle limit 60 seconds up to TTL')
        server.identity_provider=InviteIdentityProvider(server.relay,ttl,idle,owner_id=owner_id) if mode!='demo' else LocalDemoIdentityProvider(server.relay.user)
        if mode=='demo':server.sessions=server.identity_provider.sessions
        from .ide import ProjectIDE
        server.ide=ProjectIDE(server.relay,mode=='local-owner')
        if mode=='local-owner':
            from .console import CommandConsole
            server.ide.console=CommandConsole(server)
        metadata={'pid':os.getpid(),'url':server.public_url}
        # Keep legacy lock-file JSON for older readers; status JSON is the Windows-safe source.
        lock.seek(0);lock.truncate();json.dump(metadata,lock);lock.flush()
        _write_server_status(root,metadata)
        return server
    except Exception:
        if server is not None:server.server_close()
        lock.close();raise

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--backend',choices=('native','docker-api'));parser.add_argument('--auth',choices=('invite','demo','local-owner'));parser.add_argument('--port',type=int,default=8000);parser.add_argument('--data',default=None);parser.add_argument('--env-file',type=Path);parser.add_argument('--public-url');parser.add_argument('--allow-http-local',action='store_true');args=parser.parse_args()
    try:
        if args.env_file:load_env_file(args.env_file)
        # Windows local demos default to seed identities; shared invite servers stay Unix/HTTPS.
        default_mode='demo' if os.name=='nt' else 'invite'
        mode=args.auth or os.environ.get('RELAY_AUTH_MODE',default_mode)
        if mode=='local-owner' and args.backend is None:args.backend='native'
        if args.backend=='native' and mode!='local-owner':raise ValueError('Native dashboard execution requires --auth local-owner; demo/shared identities cannot authorize it')
        if mode=='local-owner' and args.backend!='native':raise ValueError('Local-owner mode supports the native backend; Docker/API execution uses its authorized shared backend')
        if args.backend=='native':os.environ['RELAY_ENABLE_NATIVE']='1'
        server=make_server(args.data or os.environ.get('RELAY_DATA_DIR','data'),args.port,mode,args.public_url,args.allow_http_local)
    except (ValueError,OSError,ServerAlreadyRunning) as exc:parser.exit(1,clean(exc)+'\n')
    print(f'Relay: {server.public_url} · {mode} authentication · '+('NATIVE LOCAL: authenticated pinned owner only' if mode=='local-owner' else 'shared live execution uses organization API workers'),flush=True)
    try:server.serve_forever()
    except KeyboardInterrupt:pass
    finally:server.server_close()
if __name__=='__main__':main()
