"""Owner-initiated local ChatGPT Codex sessions, with isolated profiles/worktrees.

There is no account rotation, quota workaround, copied auth state, remote token
storage or cross-user subscription delegation in this backend.
"""
import atexit
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid

from .app_server import AppServerClient,AppServerError,PERMISSION_NAME,sandbox_python,server_command,server_environment
from .codex_events import progress_from_message
from .fs_lock import current_uid, host_path, null_device, try_exclusive_lock, which_git
from .security import clean,scrub
from .workspace import Workspace, remove_tree
from .native_environment import RUNTIME_NAMES,create_shell_profile,hydrate,toolchain,toolchain_signature,workspace_environment

PROFILE_IDS=('a','b','c')
# A readiness check holds the profile session briefly; callers wait for it
# instead of mistaking it for a running task.
PROBE_WAIT_SECONDS=20
USAGE_CACHE_SECONDS=60

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

def remove_private_workspace(profile, identity):
    """Delete one task's private clone of a project, if it still exists."""
    if profile not in PROFILE_IDS or not isinstance(identity, str) or not re.fullmatch('[0-9a-f]{32}', identity):return False
    target=profile_paths(profile)[1]/identity
    if target.is_symlink() or not target.is_dir():return False
    remove_tree(target);return True

# Offered with Relay's app review tools; the shell itself still has no network.
REVIEW_INSTRUCTIONS=('To review an app you build, call run_app to start it (your shell cannot open ports or reach the internet; servers must listen on 127.0.0.1 using $HOST and $PORT), then use http_request and view_page '
                     '(visible text, console messages, JavaScript errors and a screenshot) and app_logs; call stop_app when done. '
                     'Do not report blocked only because your shell cannot start a server. If the app cannot run even with these tools, list that under limitations. ')

def thread_options(working,tools=False):
    params={'cwd':str(working),'runtimeWorkspaceRoots':[str(working)],'permissions':PERMISSION_NAME,'approvalPolicy':'never','ephemeral':True,
            'config':{'shell_environment_policy.set':workspace_environment(working)}}
    if tools:
        from .app_review import TOOLS
        params['dynamicTools']=TOOLS
    if os.environ.get('RELAY_NATIVE_MODEL'):params['model']=os.environ['RELAY_NATIVE_MODEL']
    return params

class NativeSession:
    def __init__(self,profile):
        self.profile=profile;self.lock=threading.Lock();self.client=None;self.account_hash=None;self.profile_lock=None;self.probed=False;self.tools_signature=None
        # True from the moment a task claims this profile until it releases it.
        self.executing=False
        self.usage=None
    def connect(self):
        home,workspaces=profile_paths(self.profile)
        if not home.is_dir() or home.is_symlink() or not workspaces.is_dir() or workspaces.is_symlink():
            raise AppServerError('Native profile directories are missing; run scripts/native_profiles.py init')
        for path in (home,workspaces):
            uid=current_uid()
            if uid is not None and ((path.stat().st_mode&0o077) or path.stat().st_uid!=uid):raise AppServerError('Native profile and workspace directories must be owned by the current OS user with mode 0700')
        codex=shutil.which(os.environ.get('RELAY_CODEX_BIN','codex'))
        if not codex:raise AppServerError('Codex CLI is not installed or not on PATH')
        signature=toolchain_signature()
        if self.client and not self.client.closed and self.client.process.poll() is None and signature==self.tools_signature:return self.client
        if self.client:self.client.close();self.client=None;self.probed=False;self.account_hash=None
        if self.profile_lock is None:
            self.profile_lock=open(home/'relay.lock','a+')
            try:try_exclusive_lock(self.profile_lock)
            except BlockingIOError:
                self.profile_lock.close();self.profile_lock=None
                raise AppServerError('Native profile is owned by another Relay process') from None
        try:
            self.client=AppServerClient(server_command(codex),server_environment(home),home)
            self.client.initialize()
            self.tools_signature=signature
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
    def limits(self,fresh=False):
        """This account's 5-hour and weekly usage windows, or None if not reported.

        Each read is a provider request, so a reading that allows work is
        reused for a minute. Blocked or recovering accounts are always re-read.
        """
        if not fresh and self.usage and not self.usage[1]['blocked'] and time.monotonic()-self.usage[0]<USAGE_CACHE_SECONDS:return self.usage[1]
        try:result=self.connect().request('account/rateLimits/read',{'excludeResetCreditDetails':True})
        except AppServerError:return None
        snapshot=usage_snapshot(result);self.usage=(time.monotonic(),snapshot)
        return snapshot
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
            result=self.connect().request('command/exec',{'command':_isolation_command(canary,peer_canary),'cwd':str(cwd),'permissionProfile':PERMISSION_NAME,'timeoutMs':10000})
            if result.get('exitCode')!=0 or result.get('stdout','').strip()!='native-isolation-ok':raise AppServerError('Native OS sandbox isolation probe failed; no model execution is allowed')
            result=self.connect().request('command/exec',{'command':[str(sandbox_python()),'-I','-c','import unittest; print("native-python-ok")'],'cwd':str(cwd),'permissionProfile':PERMISSION_NAME,'timeoutMs':10000})
            if result.get('exitCode')!=0 or result.get('stdout','').strip()!='native-python-ok':raise AppServerError('Native Python test runtime probe failed')
            # Exercise the same thread parameters and sandboxed instruction
            # loader used by real tasks, without a turn or model request.
            with tempfile.TemporaryDirectory(prefix='thread-probe-',dir=cwd) as directory:
                working=Path(directory);(working/'.tmp').mkdir()
                if toolchain():
                    env=workspace_environment(working);create_shell_profile(working)
                    for args in (['node','--version'],['npm','--version']):
                        result=self.connect().request('command/exec',{'command':args,
                            'cwd':str(working),'env':env,'permissionProfile':PERMISSION_NAME,'timeoutMs':10000})
                        if result.get('exitCode')!=0:raise AppServerError('Approved native Node/npm runtime probe failed; inspect toolchain preparation')
                if not self.connect().request('thread/start',thread_options(working)).get('thread',{}).get('id'):
                    raise AppServerError('App Server did not create a native probe thread')
            self.probed=True
        finally:canary.unlink(missing_ok=True);peer_canary.unlink(missing_ok=True)
    def close(self):
        try:
            if self.client:self.client.close()
        finally:
            self.client=None
            if self.profile_lock:
                self.profile_lock.close();self.profile_lock=None
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

def native_host():
    # Codex supplies the OS sandbox. The isolation probe must pass before any model turn.
    return platform.system() in ('Darwin', 'Windows')

def _isolation_command(canary, peer):
    script = (
        'import pathlib,sys\n'
        'for raw in sys.argv[1:]:\n'
        '    try: pathlib.Path(raw).read_bytes()\n'
        '    except OSError: continue\n'
        '    raise SystemExit(91)\n'
        'probe=pathlib.Path(".native-probe"); probe.write_text("probe"); probe.unlink()\n'
        'print("native-isolation-ok")\n'
    )
    return [str(sandbox_python()), '-I', '-c', script, str(canary), str(peer)]

def usage_snapshot(result):
    """Reduce account/rateLimits/read to the 5-hour and weekly windows.

    ordinaryUsageAllowed is the provider's verdict on whether this account may
    work. When it is null the windows are informative: an exhausted window
    still blocks a start, but recovery is never inferred from them.
    """
    limits=result.get('rateLimits') or {}
    windows={}
    for slot in ('primary','secondary'):
        window=limits.get(slot)
        if not isinstance(window,dict) or not isinstance(window.get('usedPercent'),(int,float)):continue
        minutes=window.get('windowDurationMins')
        name='weekly' if (minutes or 0)>=1440 or (minutes is None and slot=='secondary') else 'five_hour'
        windows[name]={'used_percent':window['usedPercent'],'resets_at':window.get('resetsAt'),'window_minutes':minutes}
    allowed=result.get('ordinaryUsageAllowed')
    allowed=allowed if isinstance(allowed,bool) else None
    exhausted=[name for name,window in windows.items() if window['used_percent']>=100]
    return {'allowed':allowed,'blocked':allowed is False or (allowed is None and bool(exhausted)),'windows':windows,'exhausted':exhausted}

def usage_reason(usage):
    labels={'five_hour':'5-hour','weekly':'weekly'}
    names=usage['exhausted'] or list(usage['windows'])
    resets=[usage['windows'][name]['resets_at'] for name in names if usage['windows'][name].get('resets_at')]
    when=' Resets '+time.strftime('%a %H:%M',time.localtime(max(resets)))+'.' if resets else ''
    limit=' and '.join(labels[name] for name in usage['exhausted']) or 'usage'
    return 'This ChatGPT account reached its '+limit+' limit.'+when+' No other account is used to bypass it.'

def native_readiness(profiles=PROFILE_IDS):
    checks=[('Native macOS or Windows execution host',native_host()),('Codex CLI installed',bool(shutil.which(os.environ.get('RELAY_CODEX_BIN','codex'))))]
    details=[];identities=[]
    if not all(ok for _,ok in checks):return checks,details
    for profile in profiles:
        try:
            session=session_for(profile)
            with session.lock:
                session.connect();checks.append(('Profile '+profile+' App Server initialized',True))
                session.permissions(profile_paths(profile)[1]);session.probe()
                checks.append(('Profile '+profile+' workspace, OS sandbox, Python runtime and thread creation',True))
                session.auth()
            checks.append(('Profile '+profile+' authenticated with ChatGPT',True))
            identities.append(session.account_hash)
        except (AppServerError,OSError,ValueError) as exc:
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
        base={'available':False,'connected':False,'mode':'native','status':'unavailable','authentication':'unknown','capabilities':self.capabilities}
        if not self.worker.get('_native_enabled',os.environ.get('RELAY_ENABLE_NATIVE')=='1'):return {**base,'reason':'Native execution disabled; use --auth local-owner --backend native'}
        if not native_host():return {**base,'reason':'Native execution requires the Codex CLI sandbox on macOS or Windows'}
        expected_owner=self.worker.get('_native_owner_id') or native_owner()
        if self.worker.get('owner_type')!='user' or self.worker.get('owner_id')!=expected_owner:return {**base,'reason':'Native subscription is restricted to its configured local owner'}
        try:
            session=session_for(self.profile)
            busy={**base,'status':'busy','reason':'Native profile is executing another task'}
            if session.executing or not session.lock.acquire(timeout=PROBE_WAIT_SECONDS):return busy
            try:session.connect();session.permissions(profile_paths(self.profile)[1]);session.probe();session.auth();usage=session.limits(fresh=self.worker.get('state')=='quota_blocked')
            finally:session.lock.release()
            # A usage-limit failure blocks the profile until the provider itself
            # reports ordinary usage allowed again; nothing else lifts it.
            recovered=bool(usage and usage['allowed'] is True)
            if self.worker.get('state')=='quota_blocked' and not recovered:
                reason=usage_reason(usage) if usage and usage['windows'] else 'Provider reported this profile exhausted; wait for capacity. No automatic account rotation.'
                return {**base,'status':'quota_blocked','authentication':'chatgpt','reason':reason,'usage':usage}
            if usage and usage['blocked']:
                return {**base,'status':'quota_blocked','authentication':'chatgpt','reason':usage_reason(usage),'usage':usage}
            return {**base,'available':True,'status':'available','authentication':'chatgpt','usage':usage,'quota_recovered':self.worker.get('state')=='quota_blocked',
                    'reason':'Local ChatGPT profile authenticated; execution must be initiated by its owner. No API key or Docker required.'}
        except (ValueError,AppServerError,OSError) as exc:
            message=clean(str(exc))
            return {**base,'status':'unauthenticated' if 'ChatGPT login' in message else 'unavailable','reason':message}
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
        null=null_device()
        env={'PATH':host_path(),'GIT_CONFIG_GLOBAL':null,'GIT_CONFIG_NOSYSTEM':'1','GIT_TERMINAL_PROMPT':'0'}
        subprocess.run([which_git() or 'git','-c','safe.directory=*','-c',f'core.hooksPath={null}','clone','--quiet','--no-hardlinks','--no-local',str(source),str(target)],env=env,check=True,capture_output=True,timeout=60)
        metadata=source/'.relay'
        if metadata.is_dir() and not metadata.is_symlink():shutil.copytree(metadata,target/'.relay',symlinks=False,dirs_exist_ok=True)
        environment=hydrate(target)
        # Local exclusions cannot be edited by model commands (.git is read-only).
        # Runtime assets must never reach redaction/checkpoint file limits or Git.
        with (target/'.git/info/exclude').open('a') as output:
            output.write('\n'+''.join(name+'/\n' for name in sorted(RUNTIME_NAMES)))
        (target/'.native/environment.json').write_text(json.dumps(environment,indent=2))
        return target,expected
    def _publish(self,canonical,working,expected):
        source_ws=Workspace(working.parent);canonical_ws=Workspace(canonical.parent)
        source_ws.redact_files(working.name,excluded_names=RUNTIME_NAMES)
        if canonical_ws.git(canonical,'rev-parse','HEAD')!=expected:raise ValueError('Conflicting canonical checkpoint; native worktree retained for review')
        exclusions=[':(exclude).relay',*(':(exclude,glob)**/'+name+'/**' for name in sorted(RUNTIME_NAMES))]
        source_ws.git(working,'add','-A','--','.',*exclusions)
        patch=source_ws.git(working,'diff','--cached','--binary','--no-ext-diff',expected,'--','.',*exclusions)
        if patch:
            if len(patch)>20_000_000:raise ValueError('Native patch exceeds size limit')
            canonical_ws.git(canonical,'apply','--check','--binary','-',input=patch+'\n')
            canonical_ws.git(canonical,'apply','--binary','-',input=patch+'\n')
        return len(patch.encode())
    def _align(self,canonical,working):
        """Advance the trusted clone base after Relay commits a turn checkpoint."""
        ws=Workspace(working.parent)
        ws.git(working,'fetch','--quiet',str(canonical),'HEAD')
        expected=ws.git(working,'rev-parse','FETCH_HEAD')
        ws.git(working,'reset','--mixed',expected)
        def replace_copy(source,target):
            # History files intentionally have mode 0444. Replace through the
            # trusted parent rather than opening the immutable file for write.
            if Path(source).is_symlink():raise ValueError('Symlink in Relay metadata')
            fd,temporary=tempfile.mkstemp(prefix='.context-',dir=Path(target).parent)
            os.close(fd)
            try:
                shutil.copy2(source,temporary);os.replace(temporary,target)
            finally:Path(temporary).unlink(missing_ok=True)
            return target
        shutil.copytree(canonical/'.relay',working/'.relay',dirs_exist_ok=True,copy_function=replace_copy)
        return expected

    def _turn(self,client,session,native,turn,emit,cancel):
        from .workers import ExecutionResult
        summary='Native turn ended without a structured final result';progress={};category=None;interrupt_at=None
        # This is an inactivity watchdog, not a wall-clock budget for useful work.
        idle=max(30,int(os.environ.get('RELAY_NATIVE_IDLE_SECONDS','900')))
        explicit=int(os.environ.get('RELAY_NATIVE_TIMEOUT_SECONDS','0'))
        began=last_activity=time.monotonic()
        while True:
            expired=(explicit>0 and time.monotonic()-began>explicit) or time.monotonic()-last_activity>idle
            if (cancel.is_set() or expired) and interrupt_at is None:
                category=None if cancel.is_set() else 'timeout'
                interrupt_at=time.monotonic()
                emit('stop','Native turn interruption requested; waiting for process cleanup.',{})
                try:client.request('turn/interrupt',{'threadId':native,'turnId':turn},timeout=5)
                except AppServerError:
                    session.close()
                    return ExecutionResult('cancelled' if cancel.is_set() else 'failed','Native execution terminated after interruption transport failure',native,error_category=category)
            if interrupt_at and time.monotonic()-interrupt_at>10:
                session.close()
                return ExecutionResult('cancelled' if cancel.is_set() else 'failed','Native process terminated after slow interruption; partial files retained',native,error_category=category)
            event=client.next_event()
            if event is None:continue
            method=event.get('method','');params=event.get('params') or {}
            if params.get('threadId') not in (None,native):continue
            if params.get('turnId') not in (None,turn):continue
            last_activity=time.monotonic()
            item=params.get('item') or {}
            metadata={'provider_item_id':item.get('id') or params.get('itemId'),'native_profile':self.profile,'native_turn_id':turn}
            if method=='item/started' and item.get('type')=='commandExecution':emit('tool_invocation',str(item.get('command','')),metadata)
            elif method=='item/started' and item.get('type')=='fileChange':
                emit('file_change','Applying reported file changes',{**metadata,'changes':item.get('changes',[])})
            elif method=='item/completed':
                if item.get('type')=='agentMessage':
                    raw=str(item.get('text',''));parsed=progress_from_message(raw);progress=scrub(parsed) if parsed else progress
                    summary=clean(parsed.get('summary',raw));emit('response',raw,metadata)
                elif item.get('type')=='commandExecution':emit('tool_output',str(item.get('aggregatedOutput','')),{**metadata,'exit_code':item.get('exitCode'),'duration_ms':item.get('durationMs')})
                elif item.get('type')=='fileChange':emit('file_change','Reported file changes',{**metadata,'changes':item.get('changes',[])})
            elif method=='item/commandExecution/outputDelta':emit('runtime_output',str(params.get('delta','')),{**metadata,'channel':'command'})
            elif method=='item/agentMessage/delta':emit('agent_delta',str(params.get('delta','')),metadata)
            elif method=='error':
                error=params.get('error') or {};info=error.get('codexErrorInfo')
                error_category='usage_limit' if 'usagelimitexceeded' in str(info).lower().replace('_','') or '429' in str(info) else 'native_provider_error'
                if not params.get('willRetry'):category=error_category
                emit('provider_error',str(error.get('message','Native Codex failed')),{'category':error_category,'will_retry':bool(params.get('willRetry'))})
            elif method=='turn/completed':
                completed=params.get('turn') or {}
                if completed.get('id')!=turn:continue
                error=completed.get('error') or {}
                if error:
                    info=str(error.get('codexErrorInfo','')).lower().replace('_','')
                    category='usage_limit' if 'usagelimitexceeded' in info or '429' in info else 'context_window' if 'contextwindowexceeded' in info else 'native_provider_error'
                    summary=clean(error.get('message','Native turn failed'));emit('provider_error',summary,{'category':category})
                state={'completed':'completed','interrupted':'cancelled','failed':'failed'}.get(completed.get('status'),'failed')
                if category=='usage_limit':state='quota_blocked'
                elif category:state='failed'
                if cancel.is_set():
                    # turn/interrupt is the supported child-tool cancellation.
                    # Close only this profile's dedicated process group as well.
                    session.close();state='cancelled'
                return ExecutionResult(state,summary,native,error_category=category,progress=progress)

    def start(self,task,workspace,context,emit,cancel):
        from .workers import ExecutionResult
        from .supervision import decision,evidence_signature
        started=time.monotonic();session=session_for(self.profile);native=None;working=None
        health=self.health()
        if not health['available']:return ExecutionResult('failed',health['reason'],error_category='native_preflight')
        # Claim the profile before waiting, so readiness checks report it busy
        # rather than queueing behind this task.
        session.executing=True
        if not session.lock.acquire(timeout=PROBE_WAIT_SECONDS):
            session.executing=False
            return ExecutionResult('failed','Native profile is busy',error_category='worker_busy')
        review=None
        try:
            session.auth();client=session.connect();client.drain()
            working,expected=self._prepare(workspace)
            from . import app_review
            loader=self.worker.get('_internet');project=Path(workspace).name
            internet=(lambda:loader(project)) if loader else None
            if app_review.available():
                review=app_review.AppReview(working,emit,internet);client.tool_handler=review.handle
            access=app_review.internet_note(internet()) if review and internet else ''
            environment=json.loads((working/'.native/environment.json').read_text())
            emit('native_environment',environment['message'],environment)
            session.permissions(working)
            native=client.request('thread/start',thread_options(working,bool(review)))['thread']['id']
            emit('session_started','Started a fresh native Codex thread',{'native_thread_id':native,'native_profile':self.profile,'workspace_id':working.name})
            phases=int(task.get('native_phases',2));index=int(task.get('execution_phase',0))
            boundary=bool(task.get('pause_after_turn')) and index<phases-1
            phase=f'Complete useful phase {index+1} of {phases}; leave meaningful pending work for the explicitly requested handoff.' if boundary else 'Complete the entire assigned objective and verify its acceptance criteria. Checkpoints and model turn endings do not end the task.'
            text=('Work only in the current private workspace. Read .relay/HANDOFF.md and relevant .relay/history. Never read or request authentication credentials. '
                  'Git checkpoints are managed by Relay; do not modify .git or .relay. Use python/python3 from PATH and the approved node/npm on PATH. '
                  'Read .native/environment.json for dependency readiness. Package downloads are disabled; do not retry network installation. '
                  +(REVIEW_INSTRUCTIONS+access if review else '')+
                  'Stay within the original authorization. Report completion_state=continue with concrete pending_steps only when useful authorized work remains. '
                  'Report blocked for missing dependencies or genuine obstacles, needs_authorization for out-of-scope permissions, uncertain for ambiguous completion. '
                  'Report completed only when all acceptance criteria are met and include concrete acceptance_evidence, completed_work and actual test_results. '+phase+'\n\nOriginal request:\n'+task['objective']+'\n\nWorking context:\n'+json.dumps(context)[:30000])
            schema=json.loads((Path(__file__).resolve().parent.parent/'deploy/result.schema.json').read_text())
            schema['properties'].update({'completion_state':{'type':'string','enum':['completed','continue','blocked','needs_authorization','uncertain']},'acceptance_evidence':{'type':'array','items':{'type':'string'}}})
            schema['required']+=['completion_state','acceptance_evidence']
            signatures=set();stalled=0;turn_index=0;context_failures=0;saved_context=context
            while True:
                if cancel.is_set():
                    session.close()
                    result=ExecutionResult('cancelled','Stopped before starting another native turn',native)
                    break
                turn=client.request('turn/start',{'threadId':native,'input':[{'type':'text','text':text}],'cwd':str(working),'runtimeWorkspaceRoots':[str(working)],'permissions':PERMISSION_NAME,'approvalPolicy':'never','outputSchema':schema})['turn']['id']
                self._active[task['id']]={'cancel':cancel,'client':client,'thread':native,'turn':turn}
                emit('turn_started','Native turn '+str(turn_index+1),{'native_thread_id':native,'native_turn_id':turn,'turn_index':turn_index})
                result=self._turn(client,session,native,turn,emit,cancel)
                patch_bytes=self._publish(workspace,working,expected)
                emit('native_patch','Published validated native workspace changes to the Relay project',{'native_profile':self.profile,'workspace_id':working.name,'patch_bytes':patch_bytes,'checkpoint_parent':expected})
                context_restart=result.error_category=='context_window'
                if (result.state!='completed' and not context_restart) or boundary:break
                if context_restart:
                    context_failures+=1
                    result.progress=saved_context.get('worker_progress',{})
                    if context_failures>1:
                        result.state='blocked';result.error_category='context_recovery_failed'
                        result.summary='Context continuation failed twice; saved work requires review.'
                        break
                    outcome='continue'
                else:
                    context_failures=0
                    outcome=decision(result.progress)
                if outcome=='completed':break
                if outcome=='continue':
                    ws=Workspace(working.parent)
                    source=ws.git(working,'diff','--cached','--binary',expected,'--','.',':(exclude).relay')
                    # Compare the full source tree, not incremental patch sizes.
                    tree=ws.git(working,'write-tree')
                    signature=evidence_signature(result.progress,tree)
                    stalled=stalled+1 if signature in signatures or (not source and turn_index>0) else 0
                    signatures.add(signature)
                    if stalled>=3:outcome='uncertain';result.error_category='no_progress'
                if outcome!='continue':
                    result.state='blocked';result.error_category=result.error_category or ('authorization_required' if outcome=='needs_authorization' else 'completion_uncertain' if outcome=='uncertain' else 'task_blocked')
                    result.progress['completion_state']=outcome
                    result.summary+=' · '+('No meaningful progress after repeated turns; inspect before retry.' if result.error_category=='no_progress' else 'Task stopped: '+outcome)
                    emit('task_blocked',result.summary,{'category':result.error_category})
                    break
                # The scheduler persists context/checkpoint while retaining the
                # exact assignment and writer lease. No new account or task.
                saved=emit('continuation_checkpoint',result.summary,{'progress':result.progress,'native_turn_id':turn})
                if not isinstance(saved,dict) or not saved.get('checkpoint'):
                    raise ValueError('Continuation checkpoint was not acknowledged; no turn replayed')
                expected=self._align(workspace,working);saved_context=saved
                if context_restart:
                    native=client.request('thread/start',thread_options(working,bool(review)))['thread']['id']
                    emit('session_started','Context boundary: new thread on the same native profile, with saved checkpoint and instructions',{'native_thread_id':native,'native_profile':self.profile,'workspace_id':working.name})
                turn_index+=1
                text=('Continue the same original authorized objective below. Inspect the saved context and files; preserve completed work. '
                      'The previous turn ended at an intermediate checkpoint, not task completion. Do remaining steps and actual tests. '
                      'Stop with blocked or needs_authorization when appropriate. Do not request routine permission already granted. '+(REVIEW_INSTRUCTIONS+access if review else '')+'\nOriginal request:\n'+task['objective']+'\nSaved progress:\n'+json.dumps(saved)[:30000])
                emit('continuation','Continuing the unfinished objective on the same worker and native thread',{'native_thread_id':native,'turn_index':turn_index,'checkpoint':expected})
            result.duration_ms=int((time.monotonic()-started)*1000)
            return result
        except (AppServerError,OSError,ValueError,subprocess.SubprocessError) as exc:
            detail=clean(str(exc))[:1000] if isinstance(exc,(AppServerError,ValueError)) else type(exc).__name__
            emit('error','Native runtime stopped: '+detail,{})
            safe=True
            try:session.close()
            except (OSError,subprocess.SubprocessError):safe=False
            return ExecutionResult('failed','Native execution failed: '+detail+'. Private work retained for review.',native,safe_to_release=safe,error_category='native_runtime_error',duration_ms=int((time.monotonic()-started)*1000))
        finally:
            if review:
                # The app never outlives the task that started it.
                client.tool_handler=None
                try:review.close()
                except (OSError,subprocess.SubprocessError):pass
            self._active.pop(task['id'],None);session.executing=False;session.lock.release()
    resume=start
