"""Owner-initiated local ChatGPT Codex sessions, with isolated profiles/worktrees.

There is no account rotation, quota workaround, copied auth state, remote token
storage or cross-user subscription delegation in this backend.
"""
import atexit
import hashlib
import fcntl
import json
import os
from pathlib import Path
import platform
import shutil
import shlex
import subprocess
import sys
import tempfile
import threading
import time
import uuid

from .app_server import AppServerClient,AppServerError,PERMISSION_NAME,server_command,server_environment
from .codex_events import progress_from_message
from .security import clean,scrub
from .workspace import Workspace

PROFILE_IDS=('a','b','c')

def native_root():return Path(os.environ.get('RELAY_NATIVE_ROOT','.relay-native')).expanduser().resolve()
def native_owner():return os.environ.get('RELAY_NATIVE_OWNER','alice')
def profile_paths(profile):
    if profile not in PROFILE_IDS:raise ValueError('Unknown native profile; choose a, b or c')
    root=native_root()
    return root/'profiles'/profile,root/'workspaces'/profile

def initialize_profiles():
    for profile in PROFILE_IDS:
        for path in profile_paths(profile):
            if path.is_symlink():raise ValueError('Native directories must not be symlinks')
            path.mkdir(parents=True,exist_ok=True);path.chmod(0o700)

def thread_options(working):
    params={'cwd':str(working),'runtimeWorkspaceRoots':[str(working)],'permissions':PERMISSION_NAME,'approvalPolicy':'never','ephemeral':True,
            'config':{'shell_environment_policy.set':{'PATH':os.environ.get('PATH','/usr/bin:/bin'),'HOME':str(working),'TMPDIR':str(working/'.tmp')}}}
    if os.environ.get('RELAY_NATIVE_MODEL'):params['model']=os.environ['RELAY_NATIVE_MODEL']
    return params

class NativeSession:
    def __init__(self,profile):
        self.profile=profile;self.lock=threading.Lock();self.client=None;self.account_hash=None;self.profile_lock=None;self.probed=False
    def connect(self):
        home,workspaces=profile_paths(self.profile)
        if not home.is_dir() or home.is_symlink() or not workspaces.is_dir() or workspaces.is_symlink():
            raise AppServerError('Native profile directories are missing; run scripts/native_profiles.py init')
        for path in (home,workspaces):
            if (path.stat().st_mode&0o077) or path.stat().st_uid!=os.getuid():raise AppServerError('Native profile and workspace directories must be owned by the current OS user with mode 0700')
        codex=shutil.which(os.environ.get('RELAY_CODEX_BIN','codex'))
        if not codex:raise AppServerError('Codex CLI is not installed or not on PATH')
        if self.client and not self.client.closed and self.client.process.poll() is None:return self.client
        if self.client:self.client.close();self.client=None;self.probed=False;self.account_hash=None
        if self.profile_lock is None:
            self.profile_lock=open(home/'relay.lock','a+')
            try:fcntl.flock(self.profile_lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:
                self.profile_lock.close();self.profile_lock=None
                raise AppServerError('Native profile is owned by another Relay process') from None
        try:
            self.client=AppServerClient(server_command(codex),server_environment(home),home)
            self.client.initialize()
        except Exception:
            self.close();raise
        return self.client
    def auth(self):
        client=self.connect()
        result=client.request('account/read',{'refreshToken':False})
        account=result.get('account') or {}
        if account.get('type')!='chatgpt':
            raise AppServerError('Profile '+self.profile+' needs its own interactive Codex ChatGPT login; API-key authentication is not accepted by native mode')
        # The protocol provides email, not tokens. Keep only an ephemeral digest
        # for duplicate-account checks; never print/persist identity or response.
        identity=account.get('email')
        self.account_hash=hashlib.sha256(identity.strip().lower().encode()).hexdigest() if isinstance(identity,str) and identity else None
        return {'authenticated':True,'auth_mode':'chatgpt'}
    def permissions(self,cwd):
        result=self.connect().request('permissionProfile/list',{'cwd':str(cwd)})
        if not any(p.get('id')==PERMISSION_NAME and p.get('allowed') for p in result.get('data',[])):
            raise AppServerError('Installed Codex or managed policy does not allow the required native permission profile')
    def probe(self):
        if self.probed:return
        home,cwd=profile_paths(self.profile);canary=home/('isolation-'+uuid.uuid4().hex)
        peer=PROFILE_IDS[(PROFILE_IDS.index(self.profile)+1)%len(PROFILE_IDS)]
        peer_canary=profile_paths(peer)[1]/('isolation-'+uuid.uuid4().hex)
        canary.write_text('nonsecret-isolation-probe')
        try:
            peer_canary.write_text('nonsecret-peer-isolation-probe')
            paths=' '.join(shlex.quote(str(path)) for path in (canary,peer_canary))
            command='for path in '+paths+'; do if /bin/cat "$path" >/dev/null 2>&1; then exit 91; fi; done; printf probe > .native-probe; /bin/rm .native-probe; printf native-isolation-ok'
            result=self.connect().request('command/exec',{'command':['/bin/sh','-c',command],'cwd':str(cwd),'permissionProfile':PERMISSION_NAME,'timeoutMs':10000})
            if result.get('exitCode')!=0 or result.get('stdout')!='native-isolation-ok':raise AppServerError('Native OS sandbox isolation probe failed; no model execution is allowed')
            result=self.connect().request('command/exec',{'command':[sys.executable,'-I','-c','import unittest; print("native-python-ok")'],'cwd':str(cwd),'permissionProfile':PERMISSION_NAME,'timeoutMs':10000})
            if result.get('exitCode')!=0 or result.get('stdout','').strip()!='native-python-ok':raise AppServerError('Native Python test runtime probe failed')
            # Exercise the same thread parameters and sandboxed instruction
            # loader used by real tasks, without a turn or model request.
            with tempfile.TemporaryDirectory(prefix='thread-probe-',dir=cwd) as directory:
                working=Path(directory);(working/'.tmp').mkdir()
                if not self.connect().request('thread/start',thread_options(working)).get('thread',{}).get('id'):
                    raise AppServerError('App Server did not create a native probe thread')
            self.probed=True
        finally:canary.unlink(missing_ok=True);peer_canary.unlink(missing_ok=True)
    def close(self):
        if self.client:self.client.close();self.client=None
        if self.profile_lock:self.profile_lock.close();self.profile_lock=None
        self.probed=False;self.account_hash=None

_sessions:dict[tuple[str,str],NativeSession]={}
_sessions_lock=threading.Lock()
def session_for(profile):
    with _sessions_lock:
        key=(str(native_root()),profile)
        if key not in _sessions:_sessions[key]=NativeSession(profile)
        return _sessions[key]
def close_native_sessions():
    with _sessions_lock:
        sessions=list(_sessions.values());_sessions.clear()
    for session in sessions:session.close()
atexit.register(close_native_sessions)

def native_readiness():
    checks=[('Native macOS execution host',platform.system()=='Darwin'),('Codex CLI installed',bool(shutil.which(os.environ.get('RELAY_CODEX_BIN','codex'))))]
    details=[];identities=[]
    if not all(ok for _,ok in checks):return checks,details
    for profile in PROFILE_IDS:
        try:
            session=session_for(profile)
            with session.lock:
                session.connect();checks.append(('Profile '+profile+' App Server initialized',True))
                session.permissions(profile_paths(profile)[1]);session.probe()
                checks.append(('Profile '+profile+' workspace, OS sandbox, Python runtime and thread creation',True))
                session.auth()
            checks.append(('Profile '+profile+' authenticated with ChatGPT',True))
            identities.append(session.account_hash)
        except (AppServerError,OSError) as exc:
            checks.append(('Profile '+profile+' ready',False));details.append(clean(str(exc)))
    if len(identities)==3:
        distinct=all(identities) and len(set(identities))==3
        checks.append(('Three distinct ChatGPT accounts (identity values never displayed)',bool(distinct)))
    return checks,details

class NativeCodexAdapter:
    capabilities={'coding':True,'simulation':False,'sandbox':'native_codex_permissions','personal_subscription':True,'arbitrary_objectives':True,'owner_initiated_only':True}
    def __init__(self,worker):
        self.worker=worker;self.profile=worker.get('native_profile');self._active={}
    def health(self):
        base={'available':False,'connected':False,'mode':'native','capabilities':self.capabilities}
        if os.environ.get('RELAY_ENABLE_NATIVE')!='1':return {**base,'reason':'Native execution disabled; use --backend native or enable RELAY_ENABLE_NATIVE'}
        if platform.system()!='Darwin':return {**base,'reason':'Native execution requires macOS sandbox support'}
        if self.worker.get('owner_type')!='user' or self.worker.get('owner_id')!=native_owner():return {**base,'reason':'Native subscription is restricted to its configured local owner'}
        try:
            session=session_for(self.profile)
            if not session.lock.acquire(blocking=False):return {**base,'reason':'Native profile is executing another task'}
            try:session.connect();session.permissions(profile_paths(self.profile)[1]);session.probe();session.auth()
            finally:session.lock.release()
            return {**base,'available':True,'reason':'Local ChatGPT profile authenticated; execution must be initiated by its owner. No API key or Docker required.'}
        except (ValueError,AppServerError,OSError) as exc:return {**base,'reason':clean(str(exc))}
    def cancel(self,task_id):
        active=self._active.get(task_id)
        if active:active['cancel'].set()
    def checkpoint(self,workspace):return {'managed_by':'relay_scheduler','isolated_native_worktree':True}
    def recover(self,task_id):
        # Native commands can write only their private clone. The canonical repo
        # is written solely by this adapter after it receives turn completion.
        return task_id not in self._active
    def _prepare(self,canonical):
        source=canonical.resolve();_,root=profile_paths(self.profile);identity=uuid.uuid4().hex;target=root/identity
        expected=Workspace(source.parent).git(source,'rev-parse','HEAD')
        env={'PATH':os.environ.get('PATH','/usr/bin:/bin'),'GIT_CONFIG_GLOBAL':'/dev/null','GIT_CONFIG_NOSYSTEM':'1'}
        subprocess.run(['git','-c','core.hooksPath=/dev/null','clone','--quiet','--no-hardlinks','--no-local',str(source),str(target)],env=env,check=True,capture_output=True,timeout=15)
        metadata=source/'.relay'
        if metadata.is_dir() and not metadata.is_symlink():shutil.copytree(metadata,target/'.relay',symlinks=False,dirs_exist_ok=True)
        (target/'.tmp').mkdir();return target,expected
    def _publish(self,canonical,working,expected):
        source_ws=Workspace(working.parent);canonical_ws=Workspace(canonical.parent)
        source_ws.redact_files(working.name)
        if canonical_ws.git(canonical,'rev-parse','HEAD')!=expected:raise ValueError('Conflicting canonical checkpoint; native worktree retained for review')
        source_ws.git(working,'add','-A','--','.',':(exclude).relay',':(exclude).tmp')
        patch=source_ws.git(working,'diff','--cached','--binary','--no-ext-diff',expected,'--','.',':(exclude).relay',':(exclude).tmp')
        if patch:
            if len(patch)>20_000_000:raise ValueError('Native patch exceeds size limit')
            canonical_ws.git(canonical,'apply','--check','--binary','-',input=patch+'\n')
            canonical_ws.git(canonical,'apply','--binary','-',input=patch+'\n')
        return len(patch.encode())
    def start(self,task,workspace,context,emit,cancel):
        from .workers import ExecutionResult
        started=time.monotonic();session=session_for(self.profile);native=None;summary='Native Codex did not complete';progress={};working=None
        if not self.health()['available']:return ExecutionResult('failed',self.health()['reason'],error_category='native_preflight')
        if not session.lock.acquire(blocking=False):return ExecutionResult('failed','Native profile is busy',error_category='worker_busy')
        try:
            session.auth();client=session.connect();client.drain()
            working,expected=self._prepare(workspace)
            session.permissions(working)
            native=client.request('thread/start',thread_options(working))['thread']['id']
            emit('session_started','Started a fresh native Codex thread',{'native_thread_id':native,'native_profile':self.profile})
            phases=int(task.get('native_phases',2));index=int(task.get('execution_phase',0))
            phase=f'Complete useful phase {index+1} of {phases}; leave meaningful pending work for the next owner-initiated handoff.' if task.get('pause_after_turn') and index<phases-1 else 'Complete the remaining objective, inspect previous changes and run tests.'
            text=('Work only in the current private workspace. Read .relay/HANDOFF.md and relevant .relay/history. Never read or request authentication credentials. '
                  'Git checkpoints are managed by Relay; do not modify .git or .relay. For Python tests use '+shlex.quote(sys.executable)+'. '
                  'Preserve existing work and report actual completed work, pending steps, tests and errors using the result schema. '+phase+'\n\nOriginal request:\n'+task['objective']+'\n\nWorking context:\n'+json.dumps(context)[:30000])
            schema=json.loads((Path(__file__).resolve().parent.parent/'deploy/result.schema.json').read_text())
            turn=client.request('turn/start',{'threadId':native,'input':[{'type':'text','text':text}],'cwd':str(working),'runtimeWorkspaceRoots':[str(working)],'permissions':PERMISSION_NAME,'approvalPolicy':'never','outputSchema':schema})['turn']['id']
            self._active[task['id']]={'cancel':cancel,'client':client,'thread':native,'turn':turn}
            timeout=max(5,min(int(os.environ.get('RELAY_NATIVE_TIMEOUT_SECONDS','180')),900));deadline=time.monotonic()+timeout;interrupt_at=None;state='failed';category=None
            while True:
                if (cancel.is_set() or time.monotonic()>deadline) and interrupt_at is None:
                    category=None if cancel.is_set() else 'timeout'
                    client.request('turn/interrupt',{'threadId':native,'turnId':turn},timeout=5);interrupt_at=time.monotonic()
                    emit('stop','Native turn interruption requested; waiting for completion before publishing.',{})
                if interrupt_at and time.monotonic()-interrupt_at>10:
                    session.close();return ExecutionResult('failed','Native interruption was not confirmed; private workspace preserved without publishing.',native,error_category='timeout')
                event=client.next_event()
                if event is None:continue
                method=event.get('method','');params=event.get('params') or {}
                if params.get('threadId') not in (None,native):continue
                item=params.get('item') or {}
                metadata={'provider_item_id':item.get('id') or params.get('itemId'),'native_profile':self.profile}
                if method=='item/started' and item.get('type')=='commandExecution':emit('tool_invocation',str(item.get('command','')),metadata)
                elif method=='item/completed':
                    if item.get('type')=='agentMessage':
                        raw=str(item.get('text',''));parsed=progress_from_message(raw);progress=scrub(parsed) if parsed else progress
                        summary=clean(parsed.get('summary',raw));emit('response',raw,metadata)
                    elif item.get('type')=='commandExecution':emit('tool_output',str(item.get('aggregatedOutput','')),{**metadata,'exit_code':item.get('exitCode'),'duration_ms':item.get('durationMs')})
                    elif item.get('type')=='fileChange':emit('tool_output',json.dumps(item.get('changes',[])),metadata)
                elif method=='item/commandExecution/outputDelta':emit('runtime_output',str(params.get('delta','')),{**metadata,'channel':'command'})
                elif method=='item/agentMessage/delta':emit('agent_delta',str(params.get('delta','')),metadata)
                elif method=='error':
                    error=params.get('error') or {};info=error.get('codexErrorInfo')
                    error_category='usage_limit' if 'usagelimitexceeded' in str(info).lower().replace('_','') or '429' in str(info) else 'native_provider_error'
                    if not params.get('willRetry'):category=error_category
                    emit('provider_error',str(error.get('message','Native Codex failed')),{'category':error_category,'will_retry':bool(params.get('willRetry'))})
                elif method=='turn/completed':
                    completed=params.get('turn') or {};status=completed.get('status')
                    if completed.get('id')!=turn:continue
                    error=completed.get('error') or {}
                    if error:
                        info=str(error.get('codexErrorInfo','')).lower().replace('_','')
                        category='usage_limit' if 'usagelimitexceeded' in info or '429' in info else 'native_provider_error'
                        summary=clean(error.get('message','Native turn failed'));emit('provider_error',summary,{'category':category})
                    state={'completed':'completed','interrupted':'cancelled','failed':'failed'}.get(status,'failed')
                    if category=='usage_limit':state='quota_blocked'
                    break
            patch_bytes=self._publish(workspace,working,expected)
            emit('native_patch','Published validated native workspace changes to the Relay project',{'native_profile':self.profile,'workspace_id':working.name,'patch_bytes':patch_bytes,'checkpoint_parent':expected})
            return ExecutionResult(state,summary,native,exit_code=0 if state=='completed' else None,duration_ms=int((time.monotonic()-started)*1000),error_category=category,progress=progress)
        except (AppServerError,OSError,ValueError,subprocess.SubprocessError) as exc:
            detail=clean(str(exc))[:1000] if isinstance(exc,(AppServerError,ValueError)) else type(exc).__name__
            emit('error','Native runtime stopped: '+detail,{})
            session.close()
            return ExecutionResult('failed','Native execution failed; inspect persisted events. Private work was retained without an unverified publish.',native,error_category='native_runtime_error',duration_ms=int((time.monotonic()-started)*1000))
        finally:
            self._active.pop(task['id'],None);session.lock.release()
    resume=start
