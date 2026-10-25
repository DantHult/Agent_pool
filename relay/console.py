"""Explicit local-owner command console; actual sandboxed argv, no host shell.

Commands run in a private snapshot using an unauthenticated App Server. Git
writes go through ProjectIDE. Applying generated edits is an explicit,
fingerprint-checked operation under the durable human reservation.
"""
import base64
import codecs
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shlex
import shutil
import threading
import time

from .app_server import AppServerClient, AppServerError, PERMISSION_NAME, server_command, server_environment
from .ide import WorkspaceConflict, file_path, reject_secret_content
from .native import profile_paths
from .native_environment import RUNTIME_NAMES, hydrate, private_directory, root, workspace_environment
from .security import clean
from .store import uid
from .workspace import Workspace

COMMANDS = frozenset(('python', 'python3', 'node', 'npm', 'npx', 'pytest', 'git'))
EXCLUSIONS = [':(exclude).relay', *(':(exclude,glob)**/' + name + '/**' for name in sorted(RUNTIME_NAMES))]


def snapshot(relay, project):
    files = {}
    total = 0
    with relay.ws._project_directory(project) as directory:
        for parent, name, relative in relay.ws._walk(directory, strict=True, excluded_names=RUNTIME_NAMES):
            try:
                file_path(relative, write=True)
            except PermissionError:
                continue
            with relay.ws._file(parent, name) as descriptor:
                data = relay.ws._read(descriptor, relay.ws.MAX_FILE_BYTES)
            reject_secret_content(data)
            total += len(data)
            if total > relay.ws.MAX_CHECKPOINT_BYTES or len(files) >= relay.ws.MAX_CHECKPOINT_FILES:
                raise ValueError('Project exceeds the command snapshot limits')
            files[relative] = (data, os.stat(name, dir_fd=parent, follow_symlinks=False).st_mode & 0o777)
    head = relay.ws.git(relay.ws.path(project), 'rev-parse', 'HEAD')
    fingerprint = hashlib.sha256(json.dumps({'head': head, 'files': {name: (hashlib.sha256(data).hexdigest(), mode) for name, (data, mode) in sorted(files.items())}}, sort_keys=True).encode()).hexdigest()
    return files, head, fingerprint


class CommandConsole:
    def __init__(self, server):
        self.server = server
        self.relay = server.relay
        self.ide = server.ide
        self.sessions = {}
        self.closing = False
        self.reaper = threading.Thread(target=self._reap, daemon=True)
        self.reaper.start()

    def _valid(self, user, project, session_hash):
        if self.server.auth_mode != 'local-owner' or user != self.relay.local_owner_id:
            raise PermissionError('The command console is available only to the pinned local owner, without a proxy')
        rows = self.relay.rows('SELECT s.user_id,s.expires_at,s.last_seen,a.disabled FROM sessions s JOIN accounts a ON a.user_id=s.user_id WHERE s.digest=?', (session_hash,))
        if not rows or rows[0]['user_id'] != user or rows[0]['disabled'] or rows[0]['expires_at'] <= time.time() or rows[0]['last_seen'] + self.server.identity_provider.idle <= time.time():
            raise PermissionError('Console authorization expired; sign in again')
        self.relay.write_access(user, project)

    def active(self, project):
        return any(item['project'] == project and not item['closed'] for item in self.sessions.values())

    def _session(self, user, identity, session_hash):
        item = self.sessions.get(identity)
        if not item or item['user'] != user or item['auth'] != session_hash:
            raise PermissionError('Console session is not authorized for this browser session')
        self._valid(user, item['project'], session_hash)
        item['seen'] = time.monotonic()
        return item

    def open(self, user, data, session_hash):
        with self.relay.lock:
            project, client = data['project_id'], data['client_id']
            self._valid(user, project, session_hash)
            if data.get('authorize') is not True:
                raise PermissionError('Explicit project console authorization is required')
            self.ide._reservation(user, project, client, session_hash)
            if self.relay.rows('SELECT 1 FROM editor_drafts WHERE project_id=?', (project,)):
                raise WorkspaceConflict('Save or discard drafts before opening a command snapshot')
            if self.active(project):
                raise WorkspaceConflict('This project already has an open console; close or reconnect to it first')
            if platform.system() != 'Darwin' or not shutil.which(os.environ.get('RELAY_CODEX_BIN', 'codex')):
                raise ValueError('The native command sandbox requires the installed Codex CLI on macOS')
            if sum(not item['closed'] for item in self.sessions.values()) >= 2:
                raise ValueError('At most two project consoles may be open')
            files, head, fingerprint = snapshot(self.relay, project)
            conversation = self.ide.conversation(user, project, data.get('conversation_id'))['id']
            identity = uid()
            item = {'id': identity, 'project': project, 'client_id': client, 'user': user, 'auth': session_hash,
                    'conversation': conversation, 'head': head, 'fingerprint': fingerprint, 'client': None,
                    'working': None, 'baseline': None, 'state': 'preparing', 'output': '', 'command': '',
                    'exit_code': None, 'duration_ms': None, 'closed': False, 'seen': time.monotonic(),
                    'process_id': None, 'thread': None, 'error': None, 'sequence': 0}
            self.sessions[identity] = item
            worker = threading.Thread(target=self._prepare, args=(item, files), daemon=True)
            item['thread'] = worker
            worker.start()
            self.relay.audit(user, 'console_authorized', project=project, metadata={'console_id': identity, 'scope': 'private snapshot; approved argv only; no network'})
            return self.view(item)

    def ensure(self, user, data, session_hash):
        """Run uses the current saved source; refresh an unchanged stale snapshot."""
        with self.relay.lock:
            project, client = data['project_id'], data['client_id']
            self._valid(user, project, session_hash)
            if data.get('authorize') is not True:raise PermissionError('An explicit command action is required')
            self.ide._reservation(user, project, client, session_hash)
            self.ide.conversation(user, project, data.get('conversation_id'))
            for item in list(self.sessions.values()):
                if item['project']!=project or item['closed']:continue
                self._session(user, item['id'], session_hash)
                if item['client_id']!=client:raise WorkspaceConflict('Another editor owns this command workspace')
                if item['state']=='preparing':return self.view(item)
                if item['state'] not in ('ready','finished'):raise WorkspaceConflict('Stop the command or close its failed session before running another command')
                _, _, fingerprint = snapshot(self.relay, project)
                if fingerprint==item['fingerprint']:return self.view(item)
                self.finish_clean(user, project, client, session_hash)
            return self.open(user, data, session_hash)

    def _prepare(self, item, files):
        client = None
        try:
            _, target_root = profile_paths('a')
            private_directory(target_root)
            working = private_directory(target_root / uid())
            ws = Workspace(working.parent)
            # Build a clean snapshot repository. Never copy source .git object
            # history: even deleted credential files could survive in its blobs.
            # Canonical history remains available through the scoped Git API.
            ws.git(working, 'init', '-q')
            for name, (content, mode) in files.items():
                parts = file_path(name, write=True)
                with ws._project_directory(working.name) as descriptor:
                    with ws._directory(descriptor, parts[:-1], create=True) as parent:
                        ws._atomic_write(parent, parts[-1], content, mode=mode)
            if files:
                ws.git(working, '--literal-pathspecs', 'add', '--force', '--pathspec-from-file=-', '--pathspec-file-nul', input=''.join(name+'\0' for name in files))
            ws.git(working, 'commit', '--allow-empty', '-qm', 'Private human command snapshot')
            baseline = ws.git(working, 'rev-parse', 'HEAD')
            hydrate(working)
            with (working/'.git/info/exclude').open('a') as output:
                output.write('\n'+''.join(name+'/\n' for name in sorted(RUNTIME_NAMES)))
            home = private_directory(root() / 'consoles' / item['id'] / 'home')
            codex = shutil.which(os.environ.get('RELAY_CODEX_BIN', 'codex'))
            client = AppServerClient(server_command(codex), server_environment(home), home)
            client.initialize()
            with self.relay.lock:
                if item['closed']:
                    client.close()
                    return
                item.update({'working': working, 'baseline': baseline, 'client': client, 'state': 'ready', 'thread': None})
        except Exception as exc:
            if client:client.close()
            with self.relay.lock:
                item.update({'state': 'failed', 'error': clean(str(exc))[:500] if isinstance(exc, (ValueError, AppServerError)) else 'Unable to prepare the isolated command environment', 'thread': None})

    def view(self, item):
        return {name: item[name] for name in ('id', 'project', 'state', 'output', 'command', 'exit_code', 'duration_ms', 'closed', 'error', 'sequence')}

    def poll(self, user, identity, session_hash):
        with self.relay.lock:
            return self.view(self._session(user, identity, session_hash))

    def finish_clean(self, user, project, client, session_hash):
        """Close idle unchanged snapshots; never discard unreviewed source work."""
        for item in list(self.sessions.values()):
            if item['project']!=project or item['closed']:continue
            self._session(user, item['id'], session_hash)
            if item['client_id']!=client:raise WorkspaceConflict('A console in another editor still owns this project')
            if item['state'] not in ('ready','finished'):
                raise WorkspaceConflict('Stop the command or wait for it to finish before starting the agent')
            working=item['working'];source=Workspace(working.parent)
            changed=source.git(working, 'diff', '--name-only', item['baseline'], '--', '.', *EXCLUSIONS)
            untracked=source.git(working, 'ls-files', '--others', '--exclude-standard', '--', '.', *EXCLUSIONS)
            if changed or untracked:
                raise WorkspaceConflict('The command workspace has unpublished changes. Apply & checkpoint them, or close that session explicitly before starting the agent.')
            self._close(item)

    def run(self, user, data, session_hash):
        with self.relay.lock:
            item = self._session(user, data['session_id'], session_hash)
            self.ide._reservation(user, item['project'], item['client_id'], session_hash)
            if item['closed'] or item['state'] not in ('ready', 'finished'):
                raise WorkspaceConflict('Wait for the current command to finish or cancel it first')
            text = data.get('command')
            if not isinstance(text, str) or not text.strip() or len(text) > 4000 or '\x00' in text:
                raise ValueError('Enter a command of at most 4000 characters')
            argv = shlex.split(text)
            if not argv or argv[0] not in COMMANDS:
                raise ValueError('Approved commands: python, python3, node, npm, npx, pytest, git. Shells, pipes and host paths are unavailable.')
            if argv[0] == 'git' and argv[1:] not in (['status'], ['status', '--short'], ['diff'], ['diff', '--staged'], ['log'], ['branch']):
                raise ValueError('Console Git is read-only. Use Source Control for add, commit, branch and switch in the authoritative project.')
            if argv[0] == 'git':
                safe = {'status': ['status', '--short', '--branch'], 'diff': ['diff', '--no-ext-diff', '--no-textconv', *argv[2:]], 'log': ['log', '-20', '--oneline', '--no-show-signature'], 'branch': ['branch', '--no-color']}
                argv = ['git', *safe[argv[1]]]
            relative = data.get('cwd', '')
            parts = file_path(relative) if relative else []
            cwd = item['working'].joinpath(*parts)
            if cwd.is_symlink() or not cwd.resolve().is_relative_to(item['working']) or not cwd.is_dir():
                raise PermissionError('Command directory must stay inside this private project snapshot')
            process = uid()
            item.update({'state': 'running', 'output': '', 'command': text, 'exit_code': None, 'duration_ms': None, 'error': None, 'process_id': process, 'request_sent': threading.Event()})
            self.ide._event(user, item['project'], item['conversation'], 'tool_invocation', text, {'provider_item_id': process, 'console_id': item['id'], 'channel': 'human_console'})
            item['thread'] = threading.Thread(target=self._execute, args=(item, argv, cwd), daemon=True)
            item['thread'].start()
            return self.view(item)

    def _execute(self, item, argv, cwd):
        complete = threading.Event()
        result = {}
        started = time.monotonic()
        client = item['client']
        def request():
            try:
                result.update(client.request('command/exec', {'command': argv, 'cwd': str(item['working']),
                    # A cwd inside a subdirectory must not change the permission
                    # root. Start in the snapshot and use a trusted exec launcher.
                    'env': workspace_environment(item['working']), 'permissionProfile': PERMISSION_NAME,
                    'processId': item['process_id'], 'streamStdin': True, 'streamStdoutStderr': True,
                    'timeoutMs': 90000, 'outputBytesCap': 256000}, timeout=100, sent=item['request_sent']))
            except Exception as exc:
                result['error'] = clean(str(exc))[:500] if isinstance(exc, (ValueError, AppServerError)) else 'Command execution stopped'
            finally:
                complete.set()
        # Use the approved private Python to chdir/exec an argv vector, without
        # exposing a shell or allowing cwd to enlarge the sandbox's root.
        launcher = ('import os,sys; os.chdir(sys.argv[1]); os.execvpe(sys.argv[2],sys.argv[2:],os.environ)')
        python = item['working'] / '.venv/bin/python'
        if not python.exists():
            import sys
            python = Path(sys.executable)
        argv = [str(python), '-I', '-c', launcher, str(cwd), *argv]
        waiting = threading.Thread(target=request, daemon=True)
        waiting.start()
        decoders = {name: codecs.getincrementaldecoder('utf-8')('replace') for name in ('stdout', 'stderr')}
        last_auth = 0.0
        try:
            while not complete.is_set() or not client.notifications.empty():
                if time.monotonic() - last_auth > .5:
                    with self.relay.lock:
                        self._valid(item['user'], item['project'], item['auth'])
                    last_auth = time.monotonic()
                event = client.next_event(.1)
                if not event or event.get('method') != 'command/exec/outputDelta':
                    continue
                params = event.get('params', {})
                if params.get('processId') != item['process_id']:
                    continue
                decoded = decoders[params.get('stream', 'stdout')].decode(base64.b64decode(params.get('deltaBase64', ''), validate=True))
                # Render text only, with no active ANSI/OSC control sequences.
                decoded = clean(re.sub(r'\x1b(?:\][^\x07]*(?:\x07|\x1b\\)|\[[0-?]*[ -/]*[@-~])', '', decoded))
                with self.relay.lock:
                    item['output'] = (item['output'] + decoded)[-512000:]
                    item['sequence'] += 1
                    self.ide._event(item['user'], item['project'], item['conversation'], 'runtime_output', decoded,
                                    {'provider_item_id': item['process_id'], 'channel': params.get('stream', 'stdout'), 'console_id': item['id']})
        except Exception:
            client.close()
            result['error'] = 'Console stopped after disconnect, authorization expiry, or sandbox failure'
        waiting.join(timeout=5)
        with self.relay.lock:
            item.update({'state': 'closed' if item['closed'] else 'finished', 'exit_code': result.get('exitCode'),
                         'error': result.get('error'), 'duration_ms': int((time.monotonic() - started) * 1000), 'thread': None})
            try:
                self.ide._event(item['user'], item['project'], item['conversation'], 'tool_output', item['output'] or item['error'] or '(no output)',
                                {'provider_item_id': item['process_id'], 'exit_code': item['exit_code'], 'duration_ms': item['duration_ms'], 'console_id': item['id']})
            except PermissionError:
                pass  # Revocation must stop execution without an unauthorized read.

    def write(self, user, data, session_hash):
        with self.relay.lock:
            item = self._session(user, data['session_id'], session_hash)
            if item['state'] != 'running':
                raise WorkspaceConflict('No running command is waiting for input')
            text = data.get('input', '')
            if not isinstance(text, str) or len(text.encode()) > 4096:
                raise ValueError('Input must be at most 4096 bytes')
            client, process, sent = item['client'], item['process_id'], item['request_sent']
        if not sent.wait(5):
            raise WorkspaceConflict('The command is still starting; wait for its first output before sending input')
        client.request('command/exec/write', {'processId': process, 'deltaBase64': base64.b64encode(text.encode()).decode(), 'closeStdin': data.get('close_stdin') is True})
        return {'sent': True}

    def cancel(self, user, data, session_hash):
        with self.relay.lock:
            item = self._session(user, data['session_id'], session_hash)
            client, process = item['client'], item['process_id']
        if item['state'] == 'running':
            try:
                client.request('command/exec/terminate', {'processId': process}, timeout=5)
            except AppServerError:
                client.close()
        return {'cancellation_requested': True}

    def apply(self, user, data, session_hash):
        with self.relay.lock:
            item = self._session(user, data['session_id'], session_hash)
            self.ide._reservation(user, item['project'], item['client_id'], session_hash)
            if item['closed'] or item['state'] not in ('ready', 'finished'):
                raise WorkspaceConflict('Stop the command before applying its source changes')
            if self.relay.rows('SELECT 1 FROM editor_drafts WHERE project_id=?', (item['project'],)):
                raise WorkspaceConflict('Save or discard editor drafts before applying console changes')
            _, head, current = snapshot(self.relay, item['project'])
            if current != item['fingerprint'] or head != item['head']:
                raise WorkspaceConflict('The project changed since this console snapshot. Its files are retained; no edits were overwritten.')
            working = item['working']
            source = Workspace(working.parent)
            source.redact_files(working.name, excluded_names=RUNTIME_NAMES)
            source.git(working, 'add', '-A', '--', '.', *EXCLUSIONS)
            names = source.git(working, 'diff', '--cached', '--name-only', '-z', item['baseline'], '--', '.', *EXCLUSIONS, raw=True).split('\0')
            for name in filter(None, names):
                file_path(name, write=True)
            patch = source.git(working, 'diff', '--cached', '--binary', '--no-ext-diff', item['baseline'], '--', '.', *EXCLUSIONS)
            if len(patch.encode()) > self.relay.ws.MAX_CHECKPOINT_BYTES:
                raise ValueError('Console patch exceeds managed checkpoint limits')
            canonical = self.relay.ws.path(item['project'])
            if patch:
                self.relay.ws.git(canonical, 'apply', '--check', '--binary', '-', input=patch + '\n')
                self.relay.ws.git(canonical, 'apply', '--binary', '-', input=patch + '\n')
            commit = self.relay.ws.checkpoint(item['project'], 'Apply human command snapshot')
            self.ide._record_commit(user, item['project'], item['conversation'], commit, 'Apply human command snapshot')
        self.close_session(user, item['id'], session_hash)
        return {'applied': True, 'checkpoint': commit, 'changed_files': list(filter(None, names))}

    def close_session(self, user, identity, session_hash):
        with self.relay.lock:
            item = self._session(user, identity, session_hash)
        self._close(item)
        return {'closed': True}

    def _close(self, item):
        item['closed'] = True
        client = item.get('client')
        if client:
            client.close()
        thread = item.get('thread')
        if thread and thread is not threading.current_thread():
            thread.join(timeout=10)
        item['state'] = 'closed'

    def close_auth(self, session_hash):
        for item in list(self.sessions.values()):
            if item['auth'] == session_hash and not item['closed']:
                self._close(item)

    def _reap(self):
        while not self.closing:
            time.sleep(1)
            for item in list(self.sessions.values()):
                if not item['closed'] and time.monotonic() - item['seen'] > 120:
                    self._close(item)

    def close(self):
        self.closing = True
        for item in list(self.sessions.values()):
            self._close(item)
