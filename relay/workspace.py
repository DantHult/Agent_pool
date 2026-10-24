"""Only fixed, trusted demo files and commands are executed."""
import os
import subprocess
import sys
import json
import stat
import uuid
from contextlib import contextmanager
from pathlib import Path
from .security import clean, scrub

IMPLEMENTATION = '''def password_strength(value):
    """Return weak, medium or strong using four explicit checks."""
    score = sum((len(value) >= 8, any(c.isupper() for c in value),
                 any(c.isdigit() for c in value), any(not c.isalnum() for c in value)))
    return 'strong' if score == 4 else 'medium' if score >= 2 else 'weak'
'''
TESTS = '''import unittest
from validator import password_strength
class ValidatorTests(unittest.TestCase):
    def test_weak(self): self.assertEqual(password_strength('abc'), 'weak')
    def test_medium(self): self.assertEqual(password_strength('abcdefgh'), 'weak')
    def test_two_checks(self): self.assertEqual(password_strength('Abcdefgh'), 'medium')
    def test_strong(self): self.assertEqual(password_strength('Secure123!'), 'strong')
    def test_empty(self): self.assertEqual(password_strength(''), 'weak')
'''

class Workspace:
    MAX_FILE_BYTES = 5_000_000
    MAX_CHECKPOINT_BYTES = 20_000_000
    MAX_CHECKPOINT_FILES = 5000
    DISPLAY_BYTES = 200_000

    def __init__(self, root):
        original = Path(root)
        if original.is_symlink():
            raise ValueError('Unsafe workspace root')
        original.mkdir(parents=True, exist_ok=True)
        # Resolve trusted setup aliases (such as macOS /var -> /private/var) once.
        # All subsequent traversal is anchored at opened, no-follow directory fds.
        self.root = original.resolve(strict=True)

    @contextmanager
    def _root_directory(self):
        fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for component in self.root.parts[1:]:
                child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd); fd = child
            yield fd
        finally:
            os.close(fd)

    @contextmanager
    def _project_directory(self, project):
        if not isinstance(project, str) or not project.isalnum():
            raise ValueError('Invalid project ID')
        with self._root_directory() as root:
            fd = os.open(project, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root)
            try: yield fd
            finally: os.close(fd)

    @contextmanager
    def _directory(self, root, components, create=False):
        fd = os.dup(root)
        try:
            for component in components:
                if not component or component in ('.', '..') or '/' in component:
                    raise ValueError('Invalid workspace component')
                if create:
                    try: os.mkdir(component, mode=0o755, dir_fd=fd)
                    except FileExistsError: pass
                child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd); fd = child
            yield fd
        finally:
            os.close(fd)

    @staticmethod
    def _file_stat(fd):
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError('Unsafe nonregular or hard-linked workspace file')
        return info

    @contextmanager
    def _file(self, directory, name, writable=False):
        fd = os.open(name, (os.O_RDWR if writable else os.O_RDONLY) | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        try:
            self._file_stat(fd)
            yield fd
        finally:
            os.close(fd)

    def _read(self, fd, limit):
        if self._file_stat(fd).st_size > limit:
            raise ValueError('Workspace file exceeds size limit')
        os.lseek(fd, 0, os.SEEK_SET)
        chunks = []; remaining = limit + 1
        while remaining:
            chunk = os.read(fd, min(65536, remaining))
            if not chunk: break
            chunks.append(chunk); remaining -= len(chunk)
        data = b''.join(chunks)
        if len(data) > limit:
            raise ValueError('Workspace file grew beyond size limit')
        return data

    @staticmethod
    def _write_all(fd, data):
        view = memoryview(data)
        while view:
            size = os.write(fd, view)
            view = view[size:]

    def _atomic_write(self, directory, name, data, mode=0o644):
        temporary = '.relay-write-' + uuid.uuid4().hex
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
        try:
            self._write_all(fd, data)
            os.fchmod(fd, mode)
            os.fsync(fd)
            os.close(fd); fd = None
            os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
        finally:
            if fd is not None: os.close(fd)
            try: os.unlink(temporary, dir_fd=directory)
            except FileNotFoundError: pass

    def _walk(self, directory, prefix=(), display=False, strict=False):
        for name in sorted(os.listdir(directory)):
            relative = prefix + (name,)
            if relative == ('.git',) or relative[:2] == ('.relay', 'history'):
                continue
            if display and name in ('__pycache__', '.venv', 'node_modules'):
                continue
            info = os.stat(name, dir_fd=directory, follow_symlinks=False)
            if stat.S_ISLNK(info.st_mode):
                if strict: raise ValueError('Symlink in generated workspace; review before checkpoint')
                continue
            if stat.S_ISDIR(info.st_mode):
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
                try: yield from self._walk(child, relative, display, strict)
                finally: os.close(child)
            elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                yield directory, name, '/'.join(relative)
            elif strict:
                raise ValueError('Unsafe file type or hard link in generated workspace')

    def path(self, project):
        if not isinstance(project, str) or not project.isalnum(): raise ValueError('Invalid project ID')
        created = False
        with self._root_directory() as root:
            try:
                os.mkdir(project, dir_fd=root); created = True
            except FileExistsError: pass
            fd = os.open(project, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root)
            os.close(fd)
        p = self.root / project
        if created:
            self.git(p, 'init', '-q')
            with self._project_directory(project) as fd:
                self._atomic_write(fd, 'README.md', b'# Relay sample\nA safe, predefined Python demo worktree.\n')
                self._atomic_write(fd, '.gitignore', b'__pycache__/\n.relay/history/\n')
            self.checkpoint(project, 'Initialize demo')
        return p

    def _validate_git(self, directory, initializing=False):
        try:
            gitfd = os.open('.git', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
        except FileNotFoundError:
            if initializing: return
            raise ValueError('Missing managed Git metadata')
        try:
            with self._file(gitfd, 'config') as fd:
                config = self._read(fd, 100_000).decode()
                # A coding container cannot change .git. Reject unsafe imported metadata too.
                lowered = config.lower()
                if any(token in lowered for token in ('[include', '[filter ', '[extensions]', 'fsmonitor', 'worktree')):
                    raise ValueError('Unsafe managed Git configuration')
        finally:
            os.close(gitfd)

    def git(self, p, *args, input=None):
        project = Path(p).name
        if Path(p).parent != self.root: raise ValueError('Git path is outside managed workspaces')
        with self._project_directory(project) as directory:
            self._validate_git(directory, initializing=bool(args and args[0] == 'init'))
            # A tiny isolated launcher fchdirs to the inherited fd before exec.
            # macOS /dev/fd cannot be used as a directory cwd; no preexec_fn is
            # needed, so this remains safe in the threaded HTTP server.
            launcher = [sys.executable, '-I', '-c',
                "import os,sys; os.fchdir(int(sys.argv[1])); os.execvpe('git',['git',*sys.argv[2:]],os.environ)", str(directory)]
            result = subprocess.run([*launcher, '-c', 'core.hooksPath=/dev/null', '-c', 'core.fsmonitor=false',
                '-c', 'core.attributesFile=/dev/null', '-c', 'core.excludesFile=/dev/null', '-c', 'user.name=Relay', '-c', 'user.email=relay@localhost', *args],
                pass_fds=(directory,),
                env={'PATH':os.environ.get('PATH','/usr/bin:/bin'), 'GIT_CONFIG_NOSYSTEM':'1',
                     'GIT_CONFIG_GLOBAL':'/dev/null', 'HOME':str(self.root)},
                capture_output=True, text=True, timeout=10, check=True, input=input)
            return result.stdout.strip()

    def checkpoint(self, project, message):
        p = self.path(project)
        # This is intentionally repeated at the boundary: callers cannot bypass the
        # size/secret checks by invoking checkpoint directly.
        self.redact_files(project)
        # Enumerate eligible paths rather than passing an excluded ignored path
        # directly (older Git versions report that as an ignored-path error).
        paths = self.git(p, 'ls-files', '--cached', '--others', '--exclude-standard', '-z').split('\0')
        paths = [name for name in paths if name and name != '.relay/history' and not name.startswith('.relay/history/')]
        self.git(p, '--literal-pathspecs', 'add', '-A', '--pathspec-from-file=-', '--pathspec-file-nul',
                 input=''.join(name + '\0' for name in paths))
        self.git(p, 'commit', '--allow-empty', '-qm', clean(message))
        return self.git(p, 'rev-parse', 'HEAD')

    def implement(self, project):
        self.path(project)
        with self._project_directory(project) as directory:
            self._atomic_write(directory, 'validator.py', IMPLEMENTATION.encode())

    def test(self, project):
        p = self.path(project)
        with self._project_directory(project) as directory:
            with self._file(directory, 'validator.py') as fd:
                if self._read(fd, self.DISPLAY_BYTES) != IMPLEMENTATION.encode():
                    raise ValueError('Host tests require the trusted demo fixture')
            self._atomic_write(directory, 'test_validator.py', TESTS.encode())
        result = subprocess.run([sys.executable,'-I','-c',"import sys,unittest; sys.path.insert(0,'.'); r=unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.discover('.',pattern='test_validator.py')); sys.exit(not r.wasSuccessful())"],
            cwd=p, capture_output=True, text=True, timeout=10, env={'PATH':'/usr/bin:/bin'})
        return result.returncode, clean((result.stdout + result.stderr)[-12000:])

    def inspect(self, project):
        p = self.path(project); files = {}
        with self._project_directory(project) as directory:
            for parent, name, relative in self._walk(directory, display=True):
                if clean(relative) != relative: continue
                try:
                    with self._file(parent, name) as fd:
                        files[relative] = clean(self._read(fd, self.DISPLAY_BYTES).decode())
                except (UnicodeDecodeError, ValueError, OSError):
                    continue
                if len(files) >= 100: break
        count = self.git(p, 'rev-list', '--count', 'HEAD')
        diff = self.git(p, 'diff', '--no-ext-diff', '--no-textconv', 'HEAD~1', 'HEAD') if count != '1' else ''
        return {'files':files, 'diff':clean(diff[:100000]), 'checkpoint':self.git(p,'rev-parse','HEAD'),
                'uncommitted_changes':clean(self.status(project))}

    def status(self, project):
        return clean(self.git(self.path(project), 'status', '--short', '--untracked-files=normal'))

    def write_handoff(self, project, data):
        self.path(project)
        content = '# Relay working context\n\nThis curated summary is lossy. Retrieve referenced events for the original history.\n\n```json\n' + json.dumps(scrub(data), indent=2) + '\n```\n'
        with self._project_directory(project) as root:
            with self._directory(root, ('.relay',), create=True) as metadata:
                self._atomic_write(metadata, 'HANDOFF.md', content.encode())

    def write_history(self, project, events):
        """Expose redacted full events on demand, outside Git and model prompts.

        Original events remain append-only in SQLite. Each numbered file is a
        workspace copy; the small index points to the latest 100 and backend pages.
        """
        self.path(project)
        headers = []
        with self._project_directory(project) as root:
            with self._directory(root, ('.relay', 'history'), create=True) as history:
                for event in events:
                    sequence = event.get('sequence')
                    if not isinstance(sequence, int) or sequence < 1:
                        raise ValueError('Invalid historical event sequence')
                    filename = f'{sequence:012d}.json'
                    body = json.dumps(scrub(event), ensure_ascii=False, indent=2).encode()
                    if len(body) > self.MAX_FILE_BYTES:
                        raise ValueError('Historical event exceeds per-file artifact limit; retrieve from the backend')
                    self._atomic_write(history, filename, body, mode=0o444)
                    headers.append({'sequence':sequence, 'event_type':clean(event.get('event_type',''))[:60],
                                    'worker_id':clean(event.get('worker_id') or '')[:100], 'file':filename})
                index = {'description':'Read referenced numbered JSON files on demand. These files are not the active context window.',
                         'event_count':len(events), 'recent_events':headers[-100:],
                         'first_sequence':headers[0]['sequence'] if headers else None,
                         'last_sequence':headers[-1]['sequence'] if headers else None}
                self._atomic_write(history, 'index.json', json.dumps(index, indent=2).encode(), mode=0o444)

    def redact_files(self, project):
        """Sanitize stopped-worker files using no-follow fds before Git sees them."""
        self.path(project)
        secrets = [os.environ[name].encode() for name in ('RELAY_ORG_OPENAI_API_KEY','RELAY_CODEX_PROXY_TOKEN','OPENAI_API_KEY','CODEX_API_KEY') if os.environ.get(name)]
        total = 0; count = 0
        with self._project_directory(project) as directory:
            for parent, name, relative in self._walk(directory, strict=True):
                if clean(relative) != relative:
                    raise ValueError('Secret-containing workspace filename; rename it before checkpointing')
                with self._file(parent, name, writable=True) as fd:
                    info = self._file_stat(fd); total += info.st_size; count += 1
                    if info.st_size > self.MAX_FILE_BYTES or total > self.MAX_CHECKPOINT_BYTES or count > self.MAX_CHECKPOINT_FILES:
                        raise ValueError('Workspace exceeds checkpoint size limits; review generated files before retry')
                    original = self._read(fd, self.MAX_FILE_BYTES)
                    content = original
                    for secret in secrets:
                        content = content.replace(secret, b'[REDACTED]')
                    if content != original:
                        os.lseek(fd, 0, os.SEEK_SET); self._write_all(fd, content)
                        os.ftruncate(fd, len(content)); os.fsync(fd)
