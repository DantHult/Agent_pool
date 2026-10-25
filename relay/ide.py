"""Project-scoped editor and constrained Git operations for the existing Relay.

Human reservations are durable and managed automatically by normal IDE actions.
Browsers renew a lease while they hold one. An expired lease never deletes the
reservation, its drafts or Git state: it only lets the same account move the
reservation to a new browser tab. Other users and agents remain blocked.
All entry points run under Relay.lock, alongside the existing agent scheduler.
"""
import hashlib
import os
import platform
import re
import shutil
import subprocess
import time

from .security import clean
from .store import now, uid
from . import workspace as workspace_api


class WorkspaceConflict(ValueError):
    status = 409
    code = 'workspace_conflict'


PROTECTED = {'.git', '.codex', '.aws', '.ssh', '.venv', '.native', '.tmp', '.dependencies',
             'node_modules', '__pycache__', 'auth.json', 'credentials', 'id_rsa', 'id_ed25519',
             '.npmrc', '.pypirc', '.netrc'}
MAX_TEXT = 512_000
# Browsers renew every 15 s; hidden tabs may slow timers to once a minute.
LEASE_SECONDS = 120


def file_path(value, write=False):
    if not isinstance(value, str) or not value or len(value) > 400 or '\\' in value or any(ord(char)<32 or ord(char)==127 for char in value):
        raise ValueError('Invalid relative project path')
    parts = value.split('/')
    if any(not part or part in ('.', '..') or ord(part[0]) < 32 for part in parts):
        raise ValueError('Invalid relative project path')
    if any(part.casefold() in PROTECTED or part.casefold() == '.env' or part.casefold().endswith(('.env', '.key', '.pem')) or
           (part.casefold().startswith('.env.') and part.casefold() != '.env.example') for part in parts):
        raise PermissionError('Protected credentials or runtime path')
    if any(part.casefold()=='.relay' for part in parts) and (write or value != '.relay/HANDOFF.md'):
        raise PermissionError('Relay metadata is read-only')
    return parts


def version(data):
    return hashlib.sha256(data).hexdigest()


def reject_secret_content(data):
    for name in ('RELAY_ORG_OPENAI_API_KEY', 'RELAY_CODEX_PROXY_TOKEN', 'OPENAI_API_KEY', 'CODEX_API_KEY'):
        value = os.environ.get(name)
        if value and value.encode() in data:
            raise PermissionError('Configured credentials cannot be exposed or saved through the editor')


class ProjectIDE:
    def __init__(self, relay, local_owner=False):
        self.relay = relay
        self.local_owner = local_owner
        self.console = None
        from .file_operations import FileOperations
        self.files = FileOperations(self)

    def conversation(self, user, project, conversation=None):
        self.relay.access(user, project)
        if conversation:
            return self.relay.one('SELECT * FROM conversations WHERE project_id=? AND id=?', (project, conversation))
        return self.relay.one('SELECT * FROM conversations WHERE project_id=? ORDER BY rowid LIMIT 1', (project,))

    def conversations(self, user, project):
        self.relay.access(user, project)
        rows = self.relay.rows('SELECT * FROM conversations WHERE project_id=? ORDER BY updated_at DESC,rowid DESC', (project,))
        for row in rows:
            tasks = self.relay.rows('SELECT id,state,worker_id,requested_worker_id,objective FROM tasks WHERE conversation_id=? ORDER BY rowid DESC LIMIT 1', (row['id'],))
            row['latest_task'] = tasks[0] if tasks else None
            row['event_count'] = self.relay.db.execute('SELECT COUNT(*) FROM events WHERE conversation_id=?', (row['id'],)).fetchone()[0]
            row['participants'] = self.relay.rows('SELECT DISTINCT w.id,w.name,w.native_profile FROM assignments a JOIN tasks t ON t.id=a.task_id JOIN workers w ON w.id=a.worker_id WHERE t.conversation_id=?', (row['id'],))
        return {'conversations': rows}

    def all_conversations(self, user):
        """Every project the user can open, each with its conversations."""
        return {'projects': [{'id': p['id'], 'name': p['name'], 'can_write': p['can_write'], 'conversations': self.conversations(user, p['id'])['conversations']}
                             for p in self.relay.visible_projects(user)]}

    def create_conversation(self, user, project, title):
        self.relay.write_access(user, project)
        if not isinstance(title, str) or not title.strip() or len(title) > 100:
            raise ValueError('Conversation title must contain 1–100 characters')
        identity = uid()
        self.relay.db.execute('INSERT INTO conversations(id,project_id,title,created_at,updated_at) VALUES(?,?,?,?,?)', (identity, project, clean(title.strip()), now(), now()))
        self.relay.audit(user, 'conversation_created', project=project, metadata={'conversation_id': identity})
        return self.conversation(user, project, identity)

    def update_conversation(self, user, project, identity, data):
        self.relay.write_access(user, project)
        self.conversation(user, project, identity)
        if 'title' in data:
            title = data['title']
            if not isinstance(title, str) or not title.strip() or len(title)>100:raise ValueError('Conversation title must contain 1–100 characters')
            self.relay.db.execute('UPDATE conversations SET title=?,updated_at=? WHERE id=?', (clean(title.strip()), now(), identity))
        elif isinstance(data.get('archived'), bool):
            if data['archived']:
                if data.get('confirmed') is not True:raise ValueError('Archiving requires confirmation')
                if self.relay.rows("SELECT 1 FROM tasks WHERE conversation_id=? AND state NOT IN ('completed','failed','cancelled')", (identity,)):
                    raise WorkspaceConflict('Finish or cancel this conversation task before archiving')
            self.relay.db.execute('UPDATE conversations SET archived_at=?,updated_at=? WHERE id=?', (now() if data['archived'] else None, now(), identity))
        else:raise ValueError('Choose a title or archive/restore action')
        self.relay.audit(user, 'conversation_updated', project=project, metadata={'conversation_id': identity})
        return self.conversation(user, project, identity)

    def _agent_idle(self, project):
        if self.relay.rows('SELECT 1 FROM workspace_leases WHERE project_id=?', (project,)):
            raise WorkspaceConflict('The agent owns this project. Pause and wait for its checkpoint before editing or changing Git.')

    @staticmethod
    def _holds(row, user, client, session_hash):
        return (row['user_id'], row['client_id'], row['session_hash']) == (user, client, session_hash)

    @staticmethod
    def _label(value):
        if not isinstance(value, str):return None
        return clean(''.join(char for char in value if char.isprintable()).strip())[:80] or None

    def _commands_active(self, project):
        return bool(self.console and self.console.active(project))

    def _session_open(self, session_hash):
        return bool(self.relay.rows('SELECT 1 FROM sessions WHERE digest=? AND expires_at>?', (session_hash, time.time())))

    def _lease(self, row):
        """Liveness of one reservation. A command session always counts as live.

        heartbeat_at 0 records that the browser reported closing. NULL (an
        upgraded row) and older heartbeats are measured from process start, so
        a backend restart gives open tabs one lease period to reconnect.
        """
        commands = self._commands_active(row['project_id'])
        closed = row['heartbeat_at'] == 0
        seen = None if closed else max(row['heartbeat_at'] or 0, self.relay.started)
        idle = None if closed else max(0, time.time() - seen)
        # Only local-owner sessions are durable rows; demo sessions are in memory.
        ended = self.local_owner and not self._session_open(row['session_hash'])
        stale = not commands and (closed or ended or idle > LEASE_SECONDS)
        return {'stale': stale, 'commands_active': commands, 'closed': closed, 'session_ended': bool(ended),
                'idle_seconds': None if idle is None else int(idle), 'expires_in': None if idle is None else max(0, int(LEASE_SECONDS - idle))}

    def _auto_recoverable(self, row, user):
        return bool(self.local_owner and user == self.relay.local_owner_id and row['user_id'] == user and self._lease(row)['stale'])

    def _reservation(self, user, project, client, session_hash):
        self.relay.write_access(user, project)
        self._require_editor_support()
        self._agent_idle(project)
        rows = self.relay.rows('SELECT * FROM human_workspace_locks WHERE project_id=?', (project,))
        if not rows or not self._holds(rows[0], user, client, session_hash):
            raise WorkspaceConflict('Acquire the project edit lock in this browser before writing files or changing Git.')
        # Every authorized write by the holder also renews its lease.
        self.relay.db.execute('UPDATE human_workspace_locks SET heartbeat_at=? WHERE project_id=?', (time.time(), project))
        return rows[0]

    def acquire(self, user, project, client, session_hash, recover=False, label=None, automatic=False):
        """Reserve the project, or explicitly move this account's reservation here.

        recover is a user action (or an automatic recovery of a stale lease by
        prepare). It is refused for other users and while a command runs.
        """
        self.relay.write_access(user, project)
        self._require_editor_support()
        self._agent_idle(project)
        if not isinstance(client, str) or not re.fullmatch('[A-Za-z0-9-]{16,80}', client):
            raise ValueError('Invalid editor client identifier')
        rows = self.relay.rows('SELECT * FROM human_workspace_locks WHERE project_id=?', (project,))
        moved = bool(rows) and not self._holds(rows[0], user, client, session_hash)
        if moved:
            if not recover or rows[0]['user_id'] != user:
                raise WorkspaceConflict('This project is reserved by another editor. Its owner must explicitly release or recover the lock.')
            if self._commands_active(project):
                raise WorkspaceConflict('A command session is still open for this project in the editor that holds it. Stop or close it there before moving editing here.')
        stamp = time.time()
        if not rows:
            self.relay.db.execute('INSERT INTO human_workspace_locks(project_id,user_id,client_id,session_hash,created_at,heartbeat_at,label) VALUES(?,?,?,?,?,?,?)',
                                  (project, user, client, session_hash, now(), stamp, self._label(label)))
        else:
            self.relay.db.execute('UPDATE human_workspace_locks SET client_id=?,session_hash=?,heartbeat_at=?,label=COALESCE(?,label) WHERE project_id=?',
                                  (client, session_hash, stamp, self._label(label), project))
        if moved:
            self.relay.audit(user, 'editor_lock_recovered', project=project, metadata={'automatic': automatic, 'stale': self._lease(rows[0])['stale']})
        else:
            self.relay.audit(user, 'editor_lock_acquired', project=project)
        return self.state(user, project, client, session_hash)

    def prepare(self, user, project, client, session_hash, label=None):
        """Reserve an editable workspace without an edit-mode activation step.

        The local owner's own stale reservation moves here without a click. A
        live tab of the same account, an open command or another user never does.
        """
        self.relay.write_access(user, project)
        self._require_editor_support()
        self._agent_idle(project)
        rows = self.relay.rows('SELECT * FROM human_workspace_locks WHERE project_id=?', (project,))
        if rows:
            prior = rows[0]
            if not self._holds(prior, user, client, session_hash) and prior['user_id'] == user:
                reauthenticated = prior['client_id'] == client and not self._session_open(prior['session_hash']) and not self._commands_active(project)
                if reauthenticated or self._auto_recoverable(prior, user):
                    return self.acquire(user, project, client, session_hash, recover=True, label=label, automatic=True)
            self._reservation(user, project, client, session_hash)
            return self.state(user, project, client, session_hash)
        return self.acquire(user, project, client, session_hash, label=label)

    def heartbeat(self, user, project, client, session_hash, label=None):
        """Renew this browser's lease. Reports, and never takes, another holder."""
        self.relay.write_access(user, project)
        rows = self.relay.rows('SELECT * FROM human_workspace_locks WHERE project_id=?', (project,))
        held = bool(rows) and self._holds(rows[0], user, client, session_hash)
        if held:
            self.relay.db.execute('UPDATE human_workspace_locks SET heartbeat_at=?,label=COALESCE(?,label) WHERE project_id=?', (time.time(), self._label(label), project))
        return {'held': held, 'lock': self._lock_view(user, project, client, session_hash)}

    def _give_up(self, user, row, reason):
        """Release a clean reservation, or mark it closed and keep protecting it.

        Saved drafts, uncommitted source or staged Git changes keep the row so
        no agent can start over them; nothing is checkpointed or discarded.
        """
        project = row['project_id']
        if not self.relay.rows('SELECT 1 FROM editor_drafts WHERE project_id=?', (project,)) and not self.relay.ws.status(project):
            self.relay.db.execute('DELETE FROM human_workspace_locks WHERE project_id=?', (project,))
            self.relay.audit(user, 'editor_lock_released', project=project, metadata={'automatic': True, 'reason': reason})
            return {'released': True}
        self.relay.db.execute('UPDATE human_workspace_locks SET heartbeat_at=0 WHERE project_id=?', (project,))
        self.relay.audit(user, 'editor_lock_detached', project=project, metadata={'reason': reason})
        return {'released': False, 'detached': True}

    def detach(self, user, project, client, session_hash):
        """A closing or handing-over browser tab gives up its reservation."""
        self.relay.write_access(user, project)
        rows = self.relay.rows('SELECT * FROM human_workspace_locks WHERE project_id=?', (project,))
        if not rows or not self._holds(rows[0], user, client, session_hash):
            return {'released': False, 'detached': False}
        if self._commands_active(project):
            raise WorkspaceConflict('Stop or close the open command session before handing over this project')
        return self._give_up(user, rows[0], 'browser closed or handed over')

    def detach_session(self, session_hash):
        """Logout: give up every reservation bound to that login session."""
        for row in self.relay.rows('SELECT * FROM human_workspace_locks WHERE session_hash=?', (session_hash,)):
            if not self._commands_active(row['project_id']):
                self._give_up(row['user_id'], row, 'signed out')

    def finish(self, user, project, client, session_hash, conversation=None):
        """Release clean human work before navigation or an explicit AI action.

        Drafts, foreign reservations, running commands and unpublished console
        edits fail closed. Publication remains an explicit reviewed operation.
        """
        self.relay.write_access(user, project)
        conversation = self.conversation(user, project, conversation)['id']
        rows = self.relay.rows('SELECT * FROM human_workspace_locks WHERE project_id=?', (project,))
        if not rows:return {'released': False}
        self._reservation(user, project, client, session_hash)
        if self.relay.rows('SELECT 1 FROM editor_drafts WHERE project_id=?', (project,)):
            raise WorkspaceConflict('Save or discard retained drafts before continuing with the agent')
        if self.console:self.console.finish_clean(user, project, client, session_hash)
        if self.relay.ws.status(project):
            return self.release(user, project, client, session_hash, True, conversation)
        checkpoint = self.relay.ws.git(self.relay.ws.path(project), 'rev-parse', 'HEAD')
        self.relay.db.execute('DELETE FROM human_workspace_locks WHERE project_id=?', (project,))
        self.relay.audit(user, 'editor_lock_released', project=project, metadata={'checkpoint': checkpoint, 'automatic': True})
        return {'released': True, 'checkpoint': checkpoint}

    def _read(self, project, name, missing=False, limit=MAX_TEXT):
        parts = file_path(name)
        if not workspace_api._USE_DIR_FD:
            files = self.relay.ws.inspect(project)['files']
            if name not in files:
                if missing:return None
                raise ValueError('File is not available in the read-only workspace view')
            return files[name].encode('utf-8')
        with self.relay.ws._project_directory(project) as root:
            try:
                with self.relay.ws._directory(root, parts[:-1]) as parent:
                    with self.relay.ws._file(parent, parts[-1]) as descriptor:
                        data = self.relay.ws._read(descriptor, limit)
            except FileNotFoundError:
                if missing:
                    return None
                raise ValueError('Project file does not exist') from None
        reject_secret_content(data)
        return data

    @staticmethod
    def _require_editor_support():
        if not workspace_api._USE_DIR_FD:
            raise ValueError('Secure editor writes require POSIX directory-descriptor protection. This platform supports the read-only workspace, the demo, and native Codex execution.')

    def read_file(self, user, project, name, create=False):
        self.relay.access(user, project)
        data = self._read(project, name, create)
        try:
            content = data.decode('utf-8') if data is not None else ''
        except UnicodeDecodeError:
            raise ValueError('This editor supports UTF-8 text files only') from None
        if '\x00' in content:
            raise ValueError('Binary files are not editable')
        return {'path': name, 'content': content, 'version': version(data) if data is not None else None,
                'readonly': not workspace_api._USE_DIR_FD or name.startswith('.relay/'), 'exists': data is not None}

    def draft(self, user, project, client, session_hash, name, base_version, content):
        self._reservation(user, project, client, session_hash)
        file_path(name, write=True)
        if not isinstance(content, str) or len(content.encode()) > MAX_TEXT:
            raise ValueError('Editor content exceeds the 512 KB text limit')
        reject_secret_content(content.encode())
        existing = self.relay.rows('SELECT path FROM editor_drafts WHERE project_id=? AND user_id=?', (project, user))
        if len(existing) >= 30 and name not in {row['path'] for row in existing}:
            raise ValueError('Save or discard an existing draft before editing more files')
        self.relay.db.execute('INSERT INTO editor_drafts VALUES(?,?,?,?,?,?) ON CONFLICT(project_id,user_id,path) DO UPDATE SET base_version=excluded.base_version,content=excluded.content,updated_at=excluded.updated_at',
                              (project, user, name, base_version, content, now()))
        return {'saved_draft': True, 'path': name}

    def save(self, user, project, client, session_hash, name, expected, content, conversation=None):
        self._reservation(user, project, client, session_hash)
        conversation = self.conversation(user, project, conversation)['id']
        parts = file_path(name, write=True)
        if not isinstance(content, str) or len(content.encode()) > MAX_TEXT:
            raise ValueError('Editor content exceeds the 512 KB text limit')
        encoded = content.encode()
        reject_secret_content(encoded)
        with self.relay.ws._project_directory(project) as root:
            with self.relay.ws._directory(root, parts[:-1], create=True) as parent:
                try:
                    with self.relay.ws._file(parent, parts[-1]) as descriptor:
                        prior = self.relay.ws._read(descriptor, MAX_TEXT)
                        mode = os.fstat(descriptor).st_mode & 0o777
                    current = version(prior)
                except FileNotFoundError:
                    current = None
                    mode = 0o644
                if current != expected:
                    raise WorkspaceConflict('This file changed since you opened it. Your draft is retained; reload or compare before saving.')
                self.relay.ws._atomic_write(parent, parts[-1], encoded, mode=mode)
        self.relay.db.execute('DELETE FROM editor_drafts WHERE project_id=? AND user_id=? AND path=?', (project, user, name))
        self._event(user, project, conversation, 'human_edit', 'Saved ' + name, {'path': name, 'version': version(encoded)})
        return self.read_file(user, project, name)

    def discard(self, user, project, client, session_hash, name):
        self._reservation(user, project, client, session_hash)
        self.relay.db.execute('DELETE FROM editor_drafts WHERE project_id=? AND user_id=? AND path=?', (project, user, name))
        return {'discarded': True}

    def _event(self, user, project, conversation, kind, text, metadata=None):
        c = self.conversation(user, project, conversation)
        self.relay.event({'id': None, 'conversation_id': c['id'], 'execution_mode': 'native' if self.local_owner else 'simulation'},
                         kind, text, metadata={**(metadata or {}), 'origin': 'human', 'user_id': user})

    def _record_commit(self, user, project, conversation, commit, reason):
        c = self.conversation(user, project, conversation)
        pending = self.relay.rows("SELECT t.* FROM tasks t JOIN conversations c ON c.id=t.conversation_id WHERE c.project_id=? AND t.state NOT IN ('completed','failed','cancelled','queued') ORDER BY t.rowid DESC LIMIT 1", (project,))
        task = pending[0] if pending else None
        self.relay.db.execute('INSERT INTO workspace_checkpoints VALUES(?,?,?,?,?,?,?,?,?)',
                              (uid(), project, task['conversation_id'] if task else c['id'], task['id'] if task else None, None, commit, now(), reason, ''))
        self._event(user, project, task['conversation_id'] if task else c['id'], 'checkpoint', commit, {'reason': reason, 'authorized_by': user})
        if task:
            self.relay.context(task, commit)
        self.relay.audit(user, 'human_git_checkpoint', project=project, metadata={'checkpoint': commit, 'reason': reason})

    def release(self, user, project, client, session_hash, checkpoint=False, conversation=None):
        self._reservation(user, project, client, session_hash)
        conversation = self.conversation(user, project, conversation)['id']
        if self.console and self.console.active(project):
            raise WorkspaceConflict('Close the command console before releasing the project lock')
        if self.relay.rows('SELECT 1 FROM editor_drafts WHERE project_id=? AND user_id=?', (project, user)):
            raise WorkspaceConflict('Save or discard every draft before releasing the project')
        if not checkpoint:
            raise ValueError('Release requires an explicit project checkpoint')
        commit = self.relay.ws.checkpoint(project, 'Human editor checkpoint')
        self._record_commit(user, project, conversation, commit, 'Human editor checkpoint')
        self.relay.db.execute('DELETE FROM human_workspace_locks WHERE project_id=?', (project,))
        self.relay.audit(user, 'editor_lock_released', project=project, metadata={'checkpoint': commit})
        return {'released': True, 'checkpoint': commit}

    def changes(self, project):
        workspace = self.relay.ws.path(project)
        raw = self.relay.ws.git(workspace, 'status', '--porcelain=v1', '-z', '--untracked-files=all', raw=True)
        result = []
        records = iter(raw.split('\0'))
        for record in records:
            if not record:
                continue
            code, name = record[:2], record[3:]
            if 'R' in code or 'C' in code:
                next(records, None)
            try:
                file_path(name, write=True)
            except (ValueError, PermissionError):
                continue
            result.append({'path': name, 'status': code.strip(), 'staged': code[0] not in (' ', '?')})
        return result

    def state(self, user, project, client='', session_hash=''):
        self.relay.access(user, project)
        files = []
        if not workspace_api._USE_DIR_FD:
            for relative, content in self.relay.ws.inspect(project)['files'].items():
                try:file_path(relative)
                except (ValueError, PermissionError):continue
                files.append({'path': relative, 'size': len(content.encode('utf-8')), 'readonly': True})
        else:
            files = self._descriptor_files(project)
        return self._state_metadata(user, project, client, session_hash, files)

    def _descriptor_files(self, project):
        files = []
        with self.relay.ws._project_directory(project) as directory:
            for parent, name, relative in self.relay.ws._walk(directory, display=True, excluded_names=PROTECTED):
                try:
                    file_path(relative)
                except (ValueError, PermissionError):
                    continue
                info = os.stat(name, dir_fd=parent, follow_symlinks=False)
                files.append({'path': relative, 'size': info.st_size, 'readonly': relative.startswith('.relay/')})
                if len(files) >= 3000:
                    break
        return files

    def _lock_view(self, user, project, client, session_hash):
        agent = self.relay.rows('SELECT task_id,worker_id FROM workspace_leases WHERE project_id=?', (project,))
        human = self.relay.rows('SELECT * FROM human_workspace_locks WHERE project_id=?', (project,))
        if human:
            row = human[0]
            own = row['user_id'] == user
            mine = self._holds(row, user, client, session_hash)
            lease = self._lease(row)
            lock = {'kind': 'human', 'user_id': row['user_id'], 'name': self.relay.user(row['user_id'])['name'], 'mine': mine,
                    'recoverable': own and not mine and not lease['commands_active'],
                    'auto_recoverable': not mine and self._auto_recoverable(row, user),
                    'stale': lease['stale'], 'commands_active': lease['commands_active'], 'created_at': row['created_at']}
            if own:
                # Session details are shown only to the account that holds them.
                lock.update({'label': row['label'], 'same_browser': row['session_hash'] == session_hash,
                             **{name: lease[name] for name in ('closed', 'session_ended', 'idle_seconds', 'expires_in')}})
            return lock
        if agent:
            return {'kind': 'agent', **agent[0]}
        return None

    def _state_metadata(self, user, project, client, session_hash, files):
        workspace = self.relay.ws.path(project)
        lock = self._lock_view(user, project, client, session_hash)
        drafts = self.relay.rows('SELECT path,base_version,content,updated_at FROM editor_drafts WHERE project_id=? AND user_id=?', (project, user))
        tasks = self.relay.rows("SELECT t.id,t.conversation_id,t.worker_id,t.state,t.generation FROM tasks t JOIN conversations c ON c.id=t.conversation_id WHERE c.project_id=? AND t.state NOT IN ('completed','failed','cancelled','queued') LIMIT 1", (project,))
        console_owner = self.local_owner and user == self.relay.local_owner_id
        console_available = bool(console_owner and platform.system()=='Darwin' and shutil.which(os.environ.get('RELAY_CODEX_BIN','codex')))
        return {'files': files, 'folders': self.files.folders(project) if workspace_api._USE_DIR_FD else [], 'editor_writable': workspace_api._USE_DIR_FD, 'changes': self.changes(project), 'branch': self.relay.ws.git(workspace, 'rev-parse', '--abbrev-ref', 'HEAD'),
                'checkpoint': self.relay.ws.git(workspace, 'rev-parse', 'HEAD'), 'lock': lock, 'drafts': drafts, 'active_project_task': tasks[0] if tasks else None,
                'console_available': console_available,
                'terminal_available': bool(console_owner and workspace_api._USE_DIR_FD),
                'console_unavailable_reason': None if console_available else 'The native command sandbox requires macOS and the Codex CLI on the server PATH' if console_owner else 'Command execution requires the authenticated local owner',
                'console_limitation': 'Restricted approved-command console in a private clone; no general shell or PTY. Git writes use the constrained project Git API.'}

    def git(self, user, project, client, session_hash, arguments, conversation=None):
        self.relay.access(user, project)
        conversation = self.conversation(user, project, conversation)['id']
        if not isinstance(arguments, list) or not arguments or any(not isinstance(value, str) or len(value) > 1000 or '\x00' in value for value in arguments):
            raise ValueError('Invalid Git argument list')
        command, *values = arguments
        workspace = self.relay.ws.path(project)
        if command == 'status' and not values:
            args = ['status', '--short', '--branch']
        elif command == 'diff' and values in ([], ['--staged']):
            names = self.relay.ws.git(workspace, 'ls-files', '-z', raw=True).split('\0')
            allowed = []
            for name in filter(None, names):
                try:
                    file_path(name, write=True)
                    allowed.append(name)
                except (ValueError, PermissionError):
                    pass
            args = ['--literal-pathspecs', 'diff', '--no-ext-diff', '--no-textconv', *values, '--', *allowed] if allowed else ['diff', '--no-ext-diff', '--no-textconv', '--', '.', ':(exclude)**']
        elif command == 'log' and not values:
            args = ['log', '-20', '--oneline', '--decorate', '--no-show-signature']
        elif command == 'branch' and not values:
            args = ['branch', '--no-color']
        else:
            self._reservation(user, project, client, session_hash)
            if self.relay.rows('SELECT 1 FROM editor_drafts WHERE project_id=?', (project,)):
                raise WorkspaceConflict('Save or discard drafts before changing Git')
            if command == 'add' and values:
                changes=self.changes(project)
                names=[]
                for requested in values:
                    if requested!='.':file_path(requested,write=True)
                    names.extend(entry['path'] for entry in changes if requested=='.' or entry['path']==requested or entry['path'].startswith(requested+'/'))
                names=list(dict.fromkeys(names))
                if not names:
                    return {'output': 'No source changes to stage.', 'exit_code': 0}
                for name in names:
                    file_path(name, write=True)
                    # Expand directories into eligible paths so protected
                    # descendants cannot enter the index through git add DIR.
                    self._read(project,name,missing=True,limit=self.relay.ws.MAX_FILE_BYTES)
                args = ['--literal-pathspecs', 'add', '-A', '--', *names]
            elif command == 'unstage' and values:
                names = [entry['path'] for entry in self.changes(project) if entry['staged']] if values == ['.'] else values
                if not names:return {'output': 'No staged source changes.', 'exit_code': 0}
                for name in names:file_path(name, write=True)
                args = ['--literal-pathspecs', 'restore', '--staged', '--', *names]
            elif command == 'commit' and len(values) == 2 and values[0] == '-m' and values[1].strip():
                staged = self.relay.ws.git(workspace, 'diff', '--cached', '--name-only', '-z', raw=True).split('\0')
                for name in filter(None, staged):
                    file_path(name, write=True)
                self.relay.ws.redact_files(project)
                args = ['-c', 'commit.gpgsign=false', 'commit', '--no-gpg-sign', '--no-verify', '-m', clean(values[1])]
            elif command in ('branch', 'switch') and len(values) in (1, 2):
                if len(values) == 2 and (command != 'switch' or values[0] != '-c'):
                    raise ValueError('Supported branch commands: branch NAME, switch NAME, switch -c NAME')
                name = values[-1]
                if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9/_-]{0,79}', name) or '..' in name or name.endswith('.lock') or '//' in name:
                    raise ValueError('Invalid branch name')
                if command == 'switch' and self.relay.rows("SELECT 1 FROM tasks t JOIN conversations c ON c.id=t.conversation_id WHERE c.project_id=? AND t.state NOT IN ('completed','failed','cancelled')", (project,)):
                    raise WorkspaceConflict('Finish or cancel the project task before switching Git branches')
                args = [command, *values]
            else:
                raise ValueError('Supported Git commands: status, diff [--staged], log, branch [NAME], add PATH..., commit -m MESSAGE, switch [-c] NAME')
        operation = uid()
        started = time.monotonic()
        try:
            self.relay.write_access(user, project)
            log_visible = True
        except PermissionError:
            log_visible = False
        if log_visible:self._event(user, project, conversation, 'tool_invocation', 'git ' + ' '.join(arguments), {'provider_item_id': operation, 'channel': 'human_git'})
        try:
            output = self.relay.ws.git(workspace, *args)
            code = 0
        except subprocess.CalledProcessError as exc:
            output = clean((exc.stdout or '') + (exc.stderr or ''))[-16000:]
            code = exc.returncode
        if code == 0 and command == 'commit':
            self._record_commit(user, project, conversation, self.relay.ws.git(workspace, 'rev-parse', 'HEAD'), 'Human Git commit')
        if log_visible:self._event(user, project, conversation, 'tool_output', clean(output)[:100000] or '(no output)', {'provider_item_id': operation, 'channel': 'human_git', 'exit_code': code, 'duration_ms': int((time.monotonic()-started)*1000)})
        self.relay.audit(user, 'project_git_command', project=project, metadata={'command': command, 'exit_code': code})
        return {'output': clean(output)[:100000], 'exit_code': code}
