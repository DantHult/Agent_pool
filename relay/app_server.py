"""Official Codex App Server JSON-RPC transport over local stdio.

Authentication stays inside the child's CODEX_HOME. Raw auth messages, account
objects, stderr and reasoning notifications are never logged by this transport.
"""
import json
import os
from pathlib import Path
import queue
import signal
import subprocess
import sys
import threading
from .security import clean

class AppServerError(RuntimeError):
    pass

class AppServerClient:
    def __init__(self,command,env,cwd):
        self.process=subprocess.Popen(command,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                                      cwd=cwd,env=env,start_new_session=True,bufsize=0)
        self.responses={};self.notifications=queue.Queue(maxsize=4096)
        self.guard=threading.Lock();self.write_guard=threading.Lock();self.serial=0;self.closed=False
        self.reader=threading.Thread(target=self._read,daemon=True);self.reader.start()
        self.stderr_reader=threading.Thread(target=self._discard_stderr,daemon=True);self.stderr_reader.start()
    def _discard_stderr(self):
        try:
            while self.process.stderr.read(4096):pass
        except (OSError,ValueError):pass
    def _read(self):
        try:
            while True:
                line=self.process.stdout.readline(2*1024*1024+1)
                if not line:break
                if len(line)>2*1024*1024:raise AppServerError('App Server message exceeds size limit')
                try: message=json.loads(line)
                except (ValueError,TypeError):continue
                if not isinstance(message,dict):continue
                if 'id' in message and 'method' not in message:
                    with self.guard:pending=self.responses.get(message['id'])
                    if pending:pending.put_nowait(message)
                elif 'id' in message:
                    # No caller can grant escalation or request externally managed tokens.
                    method=message.get('method','')
                    if method in ('item/commandExecution/requestApproval','item/fileChange/requestApproval'):
                        self.send({'id':message['id'],'result':{'decision':'decline'}})
                    elif method=='item/tool/requestUserInput':
                        self.send({'id':message['id'],'result':{'answers':{}}})
                    else:self.send({'id':message['id'],'error':{'code':-32601,'message':'Relay does not grant this capability'}})
                elif 'method' in message:
                    method=message.get('method','')
                    if 'reasoning' in method.lower() or method.startswith('account/login') or method.startswith('account/updated'):continue
                    self.notifications.put_nowait(message)
        except (OSError,ValueError,queue.Full,AppServerError):pass
        finally:
            self.closed=True
            with self.guard:
                for pending in self.responses.values():
                    try:pending.put_nowait({'error':{'message':'Local App Server disconnected'}})
                    except queue.Full:pass
    def send(self,message):
        data=(json.dumps(message,separators=(',',':'))+'\n').encode()
        with self.write_guard:
            if self.process.poll() is not None:raise AppServerError('Local App Server is not running')
            try:self.process.stdin.write(data);self.process.stdin.flush()
            except (OSError,ValueError):raise AppServerError('Unable to write to local App Server') from None
    def request(self,method,params=None,timeout=15,sent=None):
        with self.guard:
            self.serial+=1;number=self.serial;pending=queue.Queue(maxsize=1);self.responses[number]=pending
        try:
            self.send({'id':number,'method':method,'params':params or {}})
            if sent is not None:sent.set()
            try:message=pending.get(timeout=timeout)
            except queue.Empty:raise AppServerError('Local App Server request timed out: '+method) from None
            if message.get('error'):
                error=message['error'];detail=clean(str(error.get('message','')))[:500] if method in ('initialize','thread/start','turn/start','permissionProfile/list') else ''
                raise AppServerError('Local App Server rejected '+method+(': '+detail if detail else ''))
            result=message.get('result',{})
            if not isinstance(result,dict):raise AppServerError('Invalid App Server response')
            return result
        finally:
            with self.guard:self.responses.pop(number,None)
    def initialize(self):
        info=self.request('initialize',{'clientInfo':{'name':'relay_native','title':'Relay local workspace','version':'0.1.0'},'capabilities':{'experimentalApi':True}})
        self.send({'method':'initialized','params':{}})
        return info
    def next_event(self,timeout=.1):
        try:return self.notifications.get(timeout=timeout)
        except queue.Empty:
            if self.closed:raise AppServerError('Local App Server disconnected') from None
            return None
    def drain(self):
        while True:
            try:self.notifications.get_nowait()
            except queue.Empty:return
    def _stop_process(self,kill):
        # Process groups are a POSIX facility. Windows terminates the child directly.
        # signal.SIGKILL does not exist on Windows, so it must not be evaluated there.
        if os.name=='nt':
            try:
                if kill:self.process.kill()
                else:self.process.terminate()
            except OSError:pass
            return
        try:os.killpg(self.process.pid,signal.SIGKILL if kill else signal.SIGTERM)
        except ProcessLookupError:pass
    def close(self):
        if self.process.poll() is None:
            self._stop_process(False)
            try:self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._stop_process(True)
                self.process.wait(timeout=5)
        for stream in (self.process.stdin,self.process.stdout,self.process.stderr):
            if stream and not stream.closed:stream.close()
        self.closed=True

PERMISSION_NAME='relay-native'

def server_command(codex):
    # Native tool subprocesses get minimal runtime reads, this turn's private
    # workspace, and no other profiles/canonical project/auth paths or network.
    codex=str(Path(codex).resolve())
    executable=json.dumps(codex)
    runtime=Path(sys.base_prefix).resolve()
    if runtime in (Path('/'),Path.home().resolve()):raise AppServerError('Python runtime prefix is too broad for native isolation')
    # macOS framework/Homebrew Python installs live outside :minimal. Grant
    # this runtime's installation, never the user's home or credential roots.
    runtime_rule=json.dumps(str(runtime))+'="read",' if runtime!=Path(codex) else ''
    from .native_environment import toolchain
    tools=toolchain()
    tools_rule=json.dumps(str(tools))+'="read",' if tools else ''
    filesystem='{":root"="deny",":minimal"="read",'+executable+'="read",'+runtime_rule+tools_rule+'":tmpdir"="deny",":slash_tmp"="deny",":workspace_roots"={"."="write",".git"="read",".relay"="read","**/*.env"="deny"}}'
    return [codex,'-c','cli_auth_credentials_store="file"','-c','approval_policy="never"','-c','default_permissions="relay-native"',
            '-c','mcp_servers={}','-c','shell_environment_policy.inherit="none"',
            '-c','permissions.relay-native.extends=":workspace"',
            '-c','permissions.relay-native.filesystem='+filesystem,
            '-c','permissions.relay-native.network.enabled=false',
            'app-server','--stdio']

def server_environment(home):
    # No organization API credential, cookie, access token, AWS credential or
    # inherited CODEX_HOME can affect this locally authenticated profile.
    home=Path(home).resolve()
    inherited=('PATH','SystemRoot','WINDIR','PATHEXT','COMSPEC') if os.name=='nt' else ('PATH','HOME','LANG','LC_CTYPE')
    env={name:os.environ[name] for name in inherited if name in os.environ}
    from .fs_lock import host_path
    env['CODEX_HOME']=str(home);env.setdefault('PATH',host_path())
    if os.name=='nt':
        # Keep Codex's files inside this profile. Do not inherit the operator's
        # USERPROFILE or APPDATA, which can point at another Codex login.
        env['HOME']=str(home);env['USERPROFILE']=str(home)
        env['APPDATA']=str(home/'AppData'/'Roaming');env['LOCALAPPDATA']=str(home/'AppData'/'Local')
        env['TEMP']=str(home/'Temp');env['TMP']=env['TEMP']
        for path in (env['APPDATA'],env['LOCALAPPDATA'],env['TEMP']):Path(path).mkdir(parents=True,exist_ok=True)
    return env
