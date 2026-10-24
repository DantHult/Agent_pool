"""Codex execution adapters. Personal subscription delegation is deliberately disabled.

Live coding runs only in a restricted Docker container; no host Codex/auth fallback.
The scheduler owns persistence, writer leases, checkpoints, and handoff authorization.
"""
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re
import selectors
import shutil
import subprocess
import sys
import threading
import time
import uuid
from typing import Callable, Protocol

from .workspace import IMPLEMENTATION, TESTS
from .codex_events import provider_error, progress_from_message

Emit = Callable[[str, str, dict], None]
PERSONAL_UNSUPPORTED_REASON = (
    'Employee-owned ChatGPT/Codex subscription execution is unsupported in Relay. '
    'The official documentation reviewed does not explicitly authorize this remotely hosted '
    'cross-user delegated execution use case. A Relay login or owner opt-in is insufficient. '
    'Use simulation or an explicitly authorized organization API worker; never upload personal '
    'ChatGPT tokens, cookies, or CLI auth files.'
)


class UnsupportedExecutionMode(ValueError):
    code = 'unsupported_execution_mode'


@dataclass
class ExecutionResult:
    state: str
    summary: str
    native_thread_id: str | None = None
    safe_to_release: bool = True
    exit_code: int | None = None
    duration_ms: int = 0
    error_category: str | None = None
    retry_after_seconds: float | None = None
    progress: dict = field(default_factory=dict)


class WorkerAdapter(Protocol):
    capabilities: dict
    def start(self, task: dict, workspace: Path, context: dict, emit: Emit,
              cancel: threading.Event) -> ExecutionResult: ...
    def resume(self, task: dict, workspace: Path, context: dict, emit: Emit,
               cancel: threading.Event) -> ExecutionResult: ...
    def cancel(self, task_id: str) -> None: ...
    def checkpoint(self, workspace: Path) -> dict: ...
    def health(self) -> dict: ...
    def recover(self, task_id: str) -> bool: ...


def _safe_file(workspace: Path, filename: str) -> Path:
    base = workspace.resolve(strict=True)
    target = workspace / filename
    if target.is_symlink() or target.resolve().parent != base:
        raise ValueError('Unsafe workspace file; no tool was executed')
    return target


def _redact(value: str, secrets: tuple = ()) -> str:
    for secret in secrets:
        if secret:
            value = value.replace(secret, '[REDACTED]')
    value = re.sub(r'sk-[A-Za-z0-9_-]{8,}', '[REDACTED]', value)
    return re.sub(r'(?i)(bearer\s+|(?:api[_-]?key|token|secret)\s*[:=]\s*)\S+', r'\1[REDACTED]', value)


class SimulationWorkerAdapter:
    capabilities = {'coding': True, 'simulation': True, 'sandbox': 'fixed_demo_commands',
                    'personal_subscription': False, 'arbitrary_objectives': False}

    def __init__(self, worker: dict):
        self.worker_id = worker['id']
        self._cancellations: dict[str, threading.Event] = {}

    def health(self) -> dict:
        return {'available': True, 'connected': False, 'mode': 'simulation',
                'reason': 'Deterministic simulated Codex; no provider request',
                'capabilities': self.capabilities}

    def recover(self, task_id: str) -> bool:
        return True

    def checkpoint(self, workspace: Path) -> dict:
        return {'managed_by': 'relay_scheduler', 'workspace_present': workspace.is_dir()}

    def cancel(self, task_id: str) -> None:
        event = self._cancellations.get(task_id)
        if event:
            event.set()

    def start(self, task, workspace, context, emit, cancel):
        self._cancellations[task['id']] = cancel
        try:
            if cancel.is_set():
                return ExecutionResult('cancelled', 'Cancelled before demo tool execution')
            emit('tool_invocation', 'Write predefined validator.py (simulated Codex)', {})
            _safe_file(workspace, 'validator.py').write_text(IMPLEMENTATION)
            emit('tool_output', 'validator.py written; tests pending', {'simulated_worker': True})
            summary = (f'{self.worker_id} · SIMULATED: Implemented validator.py in the shared Git worktree. '
                       'Tests remain. Simulate Usage Limit to demonstrate a handoff, or Continue to finish.')
            emit('response', summary, {})
            return ExecutionResult('running', summary)
        finally:
            self._cancellations.pop(task['id'], None)

    def resume(self, task, workspace, context, emit, cancel):
        self._cancellations[task['id']] = cancel
        process = None
        try:
            if cancel.is_set():
                return ExecutionResult('cancelled', 'Cancelled before continuation')
            validator = _safe_file(workspace, 'validator.py')
            # A real worker may have changed this file. Never execute arbitrary generated code on host.
            if not validator.exists() or validator.read_text() != IMPLEMENTATION:
                return ExecutionResult('failed', 'Simulation can only test the trusted validator fixture. Current source was preserved; use an API sandbox to test other code.')
            summary = (f"{self.worker_id} · SIMULATED: Loaded context v{context.get('version', 0)} "
                       f"from {task['conversation_id']}, checkpoint {context.get('checkpoint', '')[:8]}. "
                       f"Remaining objective: {context.get('todos', [])}. Previous test status: "
                       f"{context.get('tests', 'Tests not run yet')}. Adding and running validator tests.")
            emit('response', summary, {'context_received': context})
            _safe_file(workspace, 'test_validator.py').write_text(TESTS)
            emit('tool_invocation', 'Run fixed isolated Python unittest suite', {})
            command = [sys.executable, '-I', '-c',
                       "import sys,unittest; sys.path.insert(0,'.'); r=unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.discover('.',pattern='test_validator.py')); sys.exit(not r.wasSuccessful())"]
            process = subprocess.Popen(command, cwd=workspace, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                       text=True, env={'PATH': '/usr/bin:/bin'}, start_new_session=True)
            deadline = time.monotonic() + 10
            while True:
                if cancel.is_set() or time.monotonic() > deadline:
                    process.kill(); output, _ = process.communicate(timeout=2)
                    emit('tool_output', output[-12000:], {'interrupted': True})
                    return ExecutionResult('cancelled' if cancel.is_set() else 'failed',
                                           'Demo test cancelled' if cancel.is_set() else 'Demo test timed out after 10 seconds')
                try:
                    output, _ = process.communicate(timeout=.1)
                    break
                except subprocess.TimeoutExpired:
                    pass
            emit('tool_output', output[-12000:], {'exit_code': process.returncode})
            return ExecutionResult('completed' if process.returncode == 0 else 'failed',
                                   'All 5 validator tests passed. Objective complete.' if process.returncode == 0 else 'Tests failed; inspect the tool output.')
        finally:
            if process is not None and process.poll() is None:
                process.kill(); process.wait(timeout=2)
            self._cancellations.pop(task['id'], None)


class PersonalSubscriptionCodexAdapter:
    capabilities = {'coding': False, 'simulation': False, 'personal_subscription': True}
    def __init__(self, worker: dict): self.worker = worker
    def health(self):
        return {'available': False, 'connected': False, 'mode': 'personal_supported',
                'reason': PERSONAL_UNSUPPORTED_REASON, 'code': 'unsupported_execution_mode',
                'capabilities': self.capabilities}
    def checkpoint(self, workspace): return {'managed_by': 'relay_scheduler'}
    def recover(self, task_id): return True
    def cancel(self, task_id): return None
    def start(self, task, workspace, context, emit, cancel):
        raise UnsupportedExecutionMode(PERSONAL_UNSUPPORTED_REASON)
    resume = start


class OrganizationApiCodexAdapter:
    """Official `codex exec --json` behind a secret-isolating inference proxy.

    Only a generated project directory is mounted. CODEX_HOME is new tmpfs state,
    the organization key remains in the separate proxy, and coding has no Internet
    route because Docker's internal network is mandatory. No host auth is read.
    """
    capabilities = {'coding': True, 'simulation': False, 'sandbox': 'docker',
                    'personal_subscription': False, 'arbitrary_objectives': True,
                    'quota_scope': 'shared_organization_api'}
    _health_cache: dict[tuple, tuple[float, dict]] = {}

    def __init__(self, worker: dict):
        self.worker = worker
        self.worker_id = worker['id']
        self._containers: dict[str, str] = {}
        self._cancellations: dict[str, threading.Event] = {}
        self._results: dict[str, ExecutionResult] = {}

    def _configuration(self):
        return {
            'enabled': os.environ.get('RELAY_ENABLE_ORG_API') == '1',
            'image': os.environ.get('RELAY_CODEX_IMAGE', 'relay-codex:local'),
            'network': os.environ.get('RELAY_CODEX_NETWORK', ''),
            'proxy_url': os.environ.get('RELAY_CODEX_PROXY_URL', 'http://relay-codex-proxy:8080/v1'),
            'model': os.environ.get('RELAY_CODEX_MODEL', ''),
            'timeout': max(5, min(int(os.environ.get('RELAY_CODEX_TIMEOUT_SECONDS', '180')), 900)),
            'request_retries': max(0,min(int(os.environ.get('RELAY_CODEX_REQUEST_RETRIES','2')),4)),
            'stream_retries': max(0,min(int(os.environ.get('RELAY_CODEX_STREAM_RETRIES','1')),2)),
        }

    def health(self):
        try:
            config = self._configuration()
        except (TypeError, ValueError):
            return self._unavailable('Invalid server-side Codex configuration')
        if self.worker.get('owner_type') != 'org':
            return self._unavailable('Organization API workers must be organization-owned')
        if self.worker.get('credential_ref') != 'env:RELAY_ORG_OPENAI_API_KEY':
            return self._unavailable('Organization credential reference is not configured')
        if not config['enabled']:
            return self._unavailable('Organization API execution is disabled; set RELAY_ENABLE_ORG_API=1 on the server')
        if not os.environ.get('RELAY_ORG_OPENAI_API_KEY'):
            return self._unavailable('Authorized organization API credential is not configured on the server')
        if not os.environ.get('RELAY_CODEX_PROXY_TOKEN'):
            return self._unavailable('Inference proxy authorization is not configured')
        if not config['network']:
            return self._unavailable('A restricted internal Docker network is required')
        if not config['model']:
            return self._unavailable('Set RELAY_CODEX_MODEL to a Codex model available to the organization')
        if os.getuid()==0:
            return self._unavailable('Run Relay as a non-root OS user so coding containers cannot run as root')
        if shutil.which('docker') is None:
            return self._unavailable('Docker is unavailable; there is no host execution fallback')
        cache_key = (config['image'], config['network'])
        cached = self._health_cache.get(cache_key)
        if cached and cached[0] > time.monotonic():
            return dict(cached[1])
        try:
            network = subprocess.run(['docker', 'network', 'inspect', config['network']],
                                     capture_output=True, text=True, timeout=3, check=True)
            networks = json.loads(network.stdout)
            if not networks or networks[0].get('Internal') is not True:
                result = self._unavailable('Docker coding network must be internal; unrestricted egress is refused')
            else:
                subprocess.run(['docker', 'image', 'inspect', config['image']],
                               capture_output=True, timeout=3, check=True)
                result = {'available': True, 'connected': False, 'mode': 'organization_api',
                          'reason': 'Docker and organization credentials configured; provider execution is verified per task, not by configuration',
                          'capabilities': self.capabilities}
        except (OSError, ValueError, subprocess.SubprocessError):
            result = self._unavailable('Docker image/network is unavailable; follow deploy/README.md')
        self._health_cache[cache_key] = (time.monotonic() + 10, result)
        return dict(result)

    def _unavailable(self, reason):
        return {'available': False, 'connected': False, 'mode': 'organization_api',
                'reason': reason, 'capabilities': self.capabilities}

    def checkpoint(self, workspace): return {'managed_by': 'relay_scheduler', 'workspace_present': workspace.is_dir()}

    def cancel(self, task_id):
        event = self._cancellations.get(task_id)
        if event: event.set()
        container = self._containers.get(task_id)
        if container: self._remove_container(container)

    @staticmethod
    def _remove_container(name):
        try:
            result = subprocess.run(['docker', 'rm', '-f', name], capture_output=True, timeout=5)
            return result.returncode == 0 or b'No such container' in result.stderr
        except (OSError, subprocess.SubprocessError):
            return False

    def recover(self, task_id):
        """Verify/stop only Relay-managed containers correlated to this persisted task."""
        if not re.fullmatch(r'[A-Za-z0-9_-]+', task_id): return False
        try:
            result = subprocess.run(['docker', 'ps', '-aq', '--filter', 'label=relay.managed=true',
                                     '--filter', 'label=relay.task_id=' + task_id],
                                    capture_output=True, text=True, timeout=5, check=True)
            containers = result.stdout.split()
            if len(containers) > 128 or any(not re.fullmatch(r'[a-f0-9]{12,64}', c) for c in containers):
                return False
            return all(self._remove_container(container) for container in containers)
        except (OSError, subprocess.SubprocessError):
            return False

    def _command(self, workspace, config, name, task_id=None):
        path = workspace.resolve(strict=True)
        if not (path / '.git').is_dir() or (path / '.git').is_symlink():
            raise ValueError('A Relay-managed Git worktree is required')
        command = ['docker', 'run', '--rm', '--pull=never', '-i', '--name', name,
                   '--read-only', '--cap-drop=ALL', '--security-opt=no-new-privileges',
                   '--pids-limit=128', '--memory=768m', '--cpus=1',
                   '--network', config['network'], '--dns', '127.0.0.1', '--user', f'{os.getuid()}:{os.getgid()}',
                   '--tmpfs', '/tmp:rw,nosuid,nodev,size=128m,mode=1777',
                   '--tmpfs', '/home/relay:rw,nosuid,nodev,size=64m,mode=1777',
                   '--mount', f'type=bind,src={path},dst=/workspace',
                   '--mount', f'type=bind,src={path / ".git"},dst=/workspace/.git,readonly',
                   '--workdir', '/workspace', '--env', 'RELAY_PROXY_TOKEN',
                   '--env', 'CODEX_HOME=/home/relay/.codex', '--env', 'HOME=/home/relay',
                   '--env', f'RELAY_TASK_TIMEOUT={config.get("timeout", 180)}',
                   '--label', 'relay.managed=true']
        if task_id:
            command += ['--label', 'relay.task_id=' + task_id]
        if (path / '.relay').exists():
            command += ['--mount', f'type=bind,src={path / ".relay"},dst=/workspace/.relay,readonly']
        command += [config['image'], 'codex', 'exec', '--json', '--ephemeral',
                    '--ignore-user-config', '--ignore-rules', '--color', 'never',
                    '--sandbox', 'danger-full-access',  # The outer Docker sandbox is the security boundary.
                    '-c', 'approval_policy="never"', '-c', 'model_provider="relay"',
                    '-c', 'model_providers.relay.name="Relay organization API"',
                    '-c', 'model_providers.relay.wire_api="responses"',
                    '-c', 'model_providers.relay.env_key="RELAY_PROXY_TOKEN"',
                    '-c', 'model_providers.relay.requires_openai_auth=false',
                    '-c', 'model_providers.relay.supports_websockets=false',
                    '-c', f'model_providers.relay.base_url={json.dumps(config["proxy_url"])}',
                    '-c', f'model_providers.relay.request_max_retries={config.get("request_retries",2)}',
                    '-c', f'model_providers.relay.stream_max_retries={config.get("stream_retries",1)}',
                    '--output-schema', '/opt/relay/result.schema.json',
                    '--model', config['model'], '-']
        return command

    def _translate(self, event: dict, emit: Emit, secrets: tuple):
        """Persist visible agent/tool items only; drop reasoning items entirely."""
        kind = event.get('type')
        item = event.get('item') or {}
        if not isinstance(item,dict): return
        if item.get('type') == 'reasoning':
            return
        metadata = {'provider_event_type': kind, 'provider_item_id': item.get('id')}
        if kind=='thread.started':
            emit('session_started','Codex initialized a distinct native thread',{'native_thread_id':_redact(str(event.get('thread_id','')),secrets)})
        elif item.get('type') == 'agent_message' and kind == 'item.completed':
            emit('response', _redact(str(item.get('text', ''))[:12000], secrets), metadata)
        elif item.get('type') == 'command_execution':
            if kind == 'item.started':
                emit('tool_invocation', _redact(str(item.get('command', ''))[:12000], secrets), metadata)
            elif kind == 'item.completed':
                metadata['exit_code'] = item.get('exit_code')
                emit('tool_output', _redact(str(item.get('aggregated_output', '')), secrets), metadata)
        elif item.get('type') == 'file_change' and kind == 'item.completed':
            emit('tool_output', _redact(json.dumps(item.get('changes', []))[:12000], secrets), metadata)
        elif item.get('type') in ('mcp_tool_call','web_search','plan') and kind in ('item.started','item.completed'):
            emit('tool_invocation' if kind=='item.started' else 'tool_output',_redact(json.dumps(item),secrets),metadata)
        elif kind in ('error', 'turn.failed'):
            error = event.get('error') or event.get('message') or 'Provider execution failed'
            emit('error', _redact(str(error)[:12000], secrets), metadata)
        elif kind == 'turn.completed':
            emit('usage', 'Provider-reported usage for this completed turn; organization API quota is shared.',
                 {'source': 'provider_reported', 'usage': event.get('usage', {}), 'quota_scope': 'shared_organization_api'})

    def get_result(self, task_id: str) -> ExecutionResult | None:
        return self._results.get(task_id)

    def resume_or_start_from_handoff(self, task, workspace, context, emit, cancel):
        return self.start(task, workspace, context, emit, cancel)

    def stream_events(self, task, workspace, context, cancel):
        """Optional iterator API; normal Relay execution uses the durable emit callback."""
        import queue
        channel = queue.Queue()
        def run():
            try: self.start(task, workspace, context, lambda k,c,m:channel.put((k,c,m)), cancel)
            finally: channel.put(None)
        thread=threading.Thread(target=run,daemon=True); thread.start()
        try:
            while True:
                event=channel.get()
                if event is None: break
                yield event
        finally:
            cancel.set(); self.cancel(task['id']); thread.join(timeout=15)

    def start(self, task, workspace, context, emit, cancel):
        started=time.monotonic()
        health=self.health()
        if not health['available']:
            result=ExecutionResult('failed',health['reason'],error_category='configuration_error')
            self._results[task['id']]=result
            return result
        if cancel.is_set(): return ExecutionResult('cancelled','Cancelled before container startup')
        config=self._configuration()
        name='relay-task-'+uuid.uuid4().hex
        self._containers[task['id']]=name; self._cancellations[task['id']]=cancel
        process=None; selector=selectors.DefaultSelector()
        secrets=(os.environ.get('RELAY_ORG_OPENAI_API_KEY',''),os.environ.get('RELAY_CODEX_PROXY_TOKEN',''))
        native=None; completed=False; failed=False; failure={}; progress={}
        summary='Codex did not report a completed turn'; result=None
        buffers={'stdout':b'','stderr':b''}; total=0
        def line_received(line,channel):
            nonlocal native,completed,failed,failure,progress,summary
            text=line.decode(errors='replace')
            if not text: return
            if channel=='stderr':
                emit('runtime_output',_redact(text,secrets),{'channel':'stderr','source':'codex_runtime'})
                return
            try: event=json.loads(text)
            except (ValueError,TypeError):
                emit('runtime_output',_redact(text,secrets),{'channel':'stdout','source':'codex_runtime','structured':False})
                return
            if not isinstance(event,dict): return
            if event.get('type')=='thread.started': native=event.get('thread_id')
            if event.get('type')=='turn.completed': completed=True
            if event.get('type') in ('error','turn.failed'):
                observed=provider_error(event)
                # Reconnecting diagnostics may precede a successful turn. Quota errors
                # are terminal for this scope; ordinary diagnostics do not fabricate success.
                if event.get('type')=='turn.failed' or observed['category'] in ('rate_limit_exceeded','insufficient_quota'):
                    failed=True
                failure=observed
                emit('provider_error',_redact(observed['message'],secrets),{k:v for k,v in observed.items() if k!='message'})
            self._translate(event,emit,secrets)
            item=event.get('item') or {}
            if isinstance(item,dict) and item.get('type')=='agent_message' and event.get('type')=='item.completed':
                raw=str(item.get('text',''))
                summary=_redact(raw,secrets)
                # Parse structure first: regex filtering a bearer value in raw
                # JSON can otherwise consume its closing quote and lose TODOs.
                parsed=progress_from_message(raw)
                if parsed:
                    progress={key:[_redact(value,secrets) for value in values] if isinstance(values,list) else _redact(values,secrets) for key,values in parsed.items()}
                    summary=progress.get('summary',summary)
        try:
            command=self._command(workspace,config,name,task['id'])
            env={k:os.environ[k] for k in ('PATH','DOCKER_HOST','DOCKER_CONTEXT','DOCKER_CONFIG') if k in os.environ}
            env['RELAY_PROXY_TOKEN']=os.environ['RELAY_CODEX_PROXY_TOKEN']
            emit('tool_invocation','Start isolated organization API Codex container',
                 {'execution_mode':'organization_api','quota_scope':'shared_organization_api','container':name})
            process=subprocess.Popen(command,stdin=subprocess.PIPE,stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE,env=env,start_new_session=True)
            phase=('PHASE 1 / HANDOFF BOUNDARY: Complete a useful first part only. Leave substantive implementation or tests unfinished for the next worker. Return the actual completed_work, pending_steps and test_results; do not claim the full objective is complete. '
                   if task.get('pause_after_turn') and not task.get('execution_phase') else
                   'Complete the remaining objective. Inspect the preserved code and handoff before editing. ')
            prompt=('Work only inside /workspace. Preserve existing work. Do not modify .git or .relay. '
                    'Read /workspace/.relay/HANDOFF.md and referenced .relay/history files when relevant. '
                    'Run appropriate tests inside this container. Never request or print credentials. '
                    'The final response must follow the supplied JSON schema and describe actual results, errors and remaining work. '
                    +phase+'\n\nOriginal user request (verbatim except secret redaction):\n'+str(task['objective'])+
                    '\n\nCurated Relay context:\n'+json.dumps(context)[:30000])
            # Docker startup must not block on a full stdin pipe outside the deadline.
            input_bytes=memoryview(prompt.encode())
            os.set_blocking(process.stdin.fileno(),False)
            selector.register(process.stdin,selectors.EVENT_WRITE,'stdin')
            selector.register(process.stdout,selectors.EVENT_READ,'stdout')
            selector.register(process.stderr,selectors.EVENT_READ,'stderr')
            deadline=time.monotonic()+config['timeout']
            while selector.get_map():
                if cancel.is_set() or time.monotonic()>deadline:
                    self._remove_container(name)
                    if process.poll() is None: process.kill()
                    category=None if cancel.is_set() else 'timeout'
                    summary='Codex execution interrupted; preserved workspace will be checkpointed.' if cancel.is_set() else 'Codex execution timed out; inspect preserved files before an explicit retry.'
                    emit('stop' if cancel.is_set() else 'error',summary,{'error_category':category})
                    quota=failure.get('category') in ('rate_limit_exceeded','insufficient_quota')
                    result=ExecutionResult('quota_blocked' if quota else ('cancelled' if cancel.is_set() else 'failed'),summary,native,
                                           error_category=failure.get('category') if quota else category,
                                           retry_after_seconds=failure.get('retry_after_seconds'))
                    break
                for key,_ in selector.select(.1):
                    channel=key.data
                    if channel=='stdin':
                        try:
                            written=os.write(key.fileobj.fileno(),input_bytes[:16384]); input_bytes=input_bytes[written:]
                        except BlockingIOError: continue
                        except BrokenPipeError: input_bytes=memoryview(b'')
                        if not input_bytes: selector.unregister(key.fileobj); key.fileobj.close()
                        continue
                    chunk=os.read(key.fileobj.fileno(),65536)
                    if not chunk:
                        if buffers[channel]: line_received(buffers[channel],channel); buffers[channel]=b''
                        selector.unregister(key.fileobj); continue
                    total+=len(chunk)
                    if total>2*1024*1024: raise ValueError('Codex stream exceeded its output limit')
                    buffers[channel]+=chunk
                    while b'\n' in buffers[channel]:
                        line,buffers[channel]=buffers[channel].split(b'\n',1); line_received(line,channel)
            returncode=process.wait(timeout=5)
            if result is None:
                quota=failure.get('category') in ('rate_limit_exceeded','insufficient_quota')
                state='quota_blocked' if quota else ('completed' if returncode==0 and completed and not failed else 'failed')
                category=(failure.get('category') or ('timeout' if returncode==124 else 'runtime_exit' if returncode else 'missing_completion')) if state!='completed' else None
                result=ExecutionResult(state,summary,native,error_category=category,
                                       retry_after_seconds=failure.get('retry_after_seconds'),progress=progress)
            result.exit_code=returncode
        except (OSError,ValueError,TypeError,subprocess.SubprocessError) as exc:
            emit('error','Organization API execution failed; workspace preserved for review.',{'error_type':type(exc).__name__})
            result=ExecutionResult('failed','Organization API execution failed; review the persisted errors before retry.',native,error_category='transport_error')
        finally:
            selector.close()
            if result is None: result=ExecutionResult('failed','Codex execution did not return a result',native,error_category='runtime_error')
            if process is not None:
                if process.poll() is None: process.kill()
                try: process.wait(timeout=5)
                except subprocess.TimeoutExpired: result.safe_to_release=False
                for pipe in (process.stdout,process.stderr,process.stdin):
                    if pipe is not None and not pipe.closed: pipe.close()
            # Stop the Docker client before the final removal check so it cannot
            # submit a late container start after cleanup was considered complete.
            removed=self._remove_container(name)
            if not removed or not result.safe_to_release:
                result.safe_to_release=False; result.state='failed'
                result.summary='Container cleanup could not be confirmed. Writer lease must remain held.'
            result.duration_ms=int((time.monotonic()-started)*1000)
            self._results[task['id']]=result
            self._containers.pop(task['id'],None); self._cancellations.pop(task['id'],None)
        return result

    resume=start


def get_adapter(worker: dict) -> WorkerAdapter:
    mode = worker.get('execution_mode', 'simulation')
    if mode == 'simulation': return SimulationWorkerAdapter(worker)
    if mode == 'organization_api': return OrganizationApiCodexAdapter(worker)
    if mode == 'personal_supported': return PersonalSubscriptionCodexAdapter(worker)
    raise UnsupportedExecutionMode('Unknown execution mode; assignment refused')


# Compatibility shim for old callers; execution now goes through WorkerAdapter.
class SimulatedCodexWorker:
    def __init__(self, worker_id): self.worker_id, self.events = worker_id, []
    def start(self, context):
        message = f'{self.worker_id} · SIMULATED: Implemented validator.py; tests remain.'
        self.events.append(message); return message
    def continue_task(self, context):
        message = f'{self.worker_id} · SIMULATED: Loaded context v{context["version"]} from {context["conversation_id"]}.'
        self.events.append(message); return message
    def pause(self): return 'paused'
    def resume_or_recreate(self, context): return self.continue_task(context)
    def status(self): return 'available'
    def cancel(self): return 'cancelled'
    def get_events(self): return list(self.events)
