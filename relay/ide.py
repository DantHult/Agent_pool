"""Project-scoped editor and constrained Git operations for the existing Relay.

Human reservations are durable and explicit. They do not expire silently while
a browser has unsaved work; the owner must save/discard and release the lock.
All entry points run under Relay.lock, alongside the existing agent scheduler.
"""
import hashlib
import os
import re
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


def file_path(value, write=False):
    if not isinstance(value, str) or not value or len(value) > 400 or '\\' in value or any(ord(char)<32 or ord(char)==127 for char in value):
        raise ValueError('Invalid relative project path')
    parts = value.split('/')
    if any(not part or part in ('.', '..') or ord(part[0]) < 32 for part in parts):
        raise ValueError('Invalid relative project path')
    if any(part in PROTECTED or part == '.env' or part.endswith(('.env', '.key', '.pem')) or
           (part.startswith('.env.') and part != '.env.example') for part in parts):
        raise PermissionError('Protected credentials or runtime path')
    if '.relay' in parts and (write or value != '.relay/HANDOFF.md'):
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

    def conversation(self, user, project, conversation=None):
        self.relay.access(user, project)
        if conversation:
            return self.relay.one('SELECT * FROM conversations WHERE project_id=? AND id=?', (project, conversation))
        return self.relay.one('SELECT * FROM conversations WHERE project_id=? ORDER BY rowid LIMIT 1', (project,))

    def conversations(self, user, project):
        self.relay.access(user, project)
        rows = self.relay.rows('SELECT * FROM conversations WHERE project_id=? ORDER BY updated_at DESC,rowid DESC', (project,))
        for row in rows:
            tasks = self.relay.rows('SELECT id,state,worker_id,objective FROM tasks WHERE conversation_id=? ORDER BY rowid DESC LIMIT 1', (row['id'],))
            row['latest_task'] = tasks[0] if tasks else None
            row['event_count'] = self.relay.db.execute('SELECT COUNT(*) FROM events WHERE conversation_id=?', (row['id'],)).fetchone()[0]
        return {'conversations': rows}

    def create_conversation(self, user, project, title):
        self.relay.write_access(user, project)
        if not isinstance(title, str) or not title.strip() or len(title) > 100:
            raise ValueError('Conversation title must contain 1–100 characters')
        identity = uid()
        self.relay.db.execute('INSERT INTO conversations VALUES(?,?,?,?,?)', (identity, project, clean(title.strip()), now(), now()))
        self.relay.audit(user, 'conversation_created', project=project, metadata={'conversation_id': identity})
        return self.conversation(user, project, identity)

    def _agent_idle(self, project):
        if self.relay.rows('SELECT 1 FROM workspace_leases WHERE project_id=?', (project,)):
            raise WorkspaceConflict('The agent owns this project. Pause and wait for its checkpoint before editing or changing Git.')

    def _reservation(self, user, project, client, session_hash):
        self.relay.write_access(user, project)
        self._require_editor_support()
        self._agent_idle(project)
        rows = self.relay.rows('SELECT * FROM human_workspace_locks WHERE project_id=?', (project,))
        if not rows or (rows[0]['user_id'], rows[0]['client_id'], rows[0]['session_hash']) != (user, client, session_hash):
            raise WorkspaceConflict('Acquire the project edit lock in this browser before writing files or changing Git.')
        return rows[0]

    def acquire(self, user, project, client, session_hash, recover=False):
        self.relay.write_access(user, project)
        self._require_editor_support()
        self._agent_idle(project)
        if not isinstance(client, str) or not re.fullmatch('[A-Za-z0-9-]{16,80}', client):
            raise ValueError('Invalid editor client identifier')
        rows = self.relay.rows('SELECT * FROM human_workspace_locks WHERE project_id=?', (project,))
        if rows and (rows[0]['user_id'], rows[0]['client_id'], rows[0]['session_hash']) != (user, client, session_hash):
            if not recover or rows[0]['user_id'] != user or (self.console and self.console.active(project)):
                raise WorkspaceConflict('This project is reserved by another editor. Its owner must explicitly release or recover the lock.')
        if not rows:
            self.relay.db.execute('INSERT INTO human_workspace_locks VALUES(?,?,?,?,?)', (project, user, client, session_hash, now()))
        elif recover:
            self.relay.db.execute('UPDATE human_workspace_locks SET client_id=?,session_hash=? WHERE project_id=?', (client, session_hash, project))
        self.relay.audit(user, 'editor_lock_acquired', project=project)
        return self.state(user, project, client, session_hash)

    def _read(self, project, name, missing=False):
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
                        data = self.relay.ws._read(descriptor, MAX_TEXT)
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
        pending = self.relay.rows("SELECT t.* FROM tasks t JOIN conversations c ON c.id=t.conversation_id WHERE c.project_id=? AND t.state NOT IN ('completed','failed','cancelled') ORDER BY t.rowid DESC LIMIT 1", (project,))
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

    def _state_metadata(self, user, project, client, session_hash, files):
        workspace = self.relay.ws.path(project)
        agent = self.relay.rows('SELECT task_id,worker_id FROM workspace_leases WHERE project_id=?', (project,))
        human = self.relay.rows('SELECT * FROM human_workspace_locks WHERE project_id=?', (project,))
        lock = None
        if human:
            row = human[0]
            lock = {'kind': 'human', 'user_id': row['user_id'], 'name': self.relay.user(row['user_id'])['name'],
                    'mine': (row['user_id'], row['client_id'], row['session_hash']) == (user, client, session_hash),
                    'recoverable': row['user_id'] == user, 'created_at': row['created_at']}
        elif agent:
            lock = {'kind': 'agent', **agent[0]}
        drafts = self.relay.rows('SELECT path,base_version,content,updated_at FROM editor_drafts WHERE project_id=? AND user_id=?', (project, user))
        tasks = self.relay.rows("SELECT t.id,t.conversation_id,t.worker_id,t.state FROM tasks t JOIN conversations c ON c.id=t.conversation_id WHERE c.project_id=? AND t.state NOT IN ('completed','failed','cancelled') LIMIT 1", (project,))
        return {'files': files, 'editor_writable': workspace_api._USE_DIR_FD, 'changes': self.changes(project), 'branch': self.relay.ws.git(workspace, 'rev-parse', '--abbrev-ref', 'HEAD'),
                'checkpoint': self.relay.ws.git(workspace, 'rev-parse', 'HEAD'), 'lock': lock, 'drafts': drafts, 'active_project_task': tasks[0] if tasks else None,
                'console_available': self.local_owner and user == self.relay.local_owner_id,
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
                names = [entry['path'] for entry in self.changes(project)] if values == ['.'] else values
                if not names:
                    return {'output': 'No source changes to stage.', 'exit_code': 0}
                for name in names:
                    file_path(name, write=True)
                args = ['--literal-pathspecs', 'add', '-A', '--', *names]
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
