"""Owner-prepared tools and offline dependencies for isolated native worktrees.

Only the operator CLI downloads packages. Model tools retain the existing
network-denied permission profile and cannot modify this store. Auth homes are
never inputs to provisioning or dependency transfer.
"""
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import ssl
import subprocess
import sys
import tarfile
import tempfile
import urllib.request

NPM_VERSION = '12.2.1'
NPM_INTEGRITY = 'bKCmFRVT/BB+YPEYbZwQ1XUI3N5t4xKSpU1MxOjo7z3iWzKBJ0ZXRC1xFdEN+TC7N5TdywaBzf2dMXGJq/e2PQ=='
RUNTIME_NAMES = frozenset(('.venv', 'node_modules', '.tmp', '.native'))


def root():
    return Path(os.environ.get('RELAY_NATIVE_ROOT', '.relay-native')).expanduser().resolve()


def private_directory(path):
    path = Path(path)
    # Reject symlink components, including links into credential directories.
    for parent in (path, *path.parents):
        if parent.is_symlink():
            raise ValueError('Native environment directories must not contain symlinks')
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Windows permission bits do not record a Unix owner or mode 0700.
    if os.name != 'nt' and (path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o077):
        raise ValueError('Native environment directories must be owned by this user with mode 0700')
    return path


def digest(path):
    value = hashlib.sha256()
    with Path(path).open('rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            value.update(chunk)
    return value.hexdigest()


def tree_digest(path):
    path = Path(path)
    if path.is_symlink() or not path.is_dir():
        raise ValueError('Approved artifact tree must be a regular directory')
    value = hashlib.sha256()
    for entry in sorted(Path(path).rglob('*')):
        name = str(entry.relative_to(path))
        if entry.is_symlink():
            if Path(os.readlink(entry)).is_absolute() or not entry.resolve().is_relative_to(path.resolve()):
                raise ValueError('Dependency link escapes its approved bundle')
            content = 'link:' + os.readlink(entry)
        elif entry.is_file():
            content = digest(entry) + ':' + str(entry.stat().st_mode & 0o777)
        elif entry.is_dir():
            continue
        else:
            raise ValueError('Unsafe dependency file type')
        value.update((name + '\0' + content + '\0').encode())
    return value.hexdigest()


def write_json(path, value):
    temporary = path.with_suffix('.new')
    with temporary.open('x') as output:
        os.chmod(temporary, 0o600)
        json.dump(value, output, indent=2)
    temporary.replace(path)


def toolchain():
    path = root() / 'tools'
    if not path.exists():
        return None
    private_directory(path)
    if not (path / 'approved.json').exists():
        # Interrupted provisioning must not break unrelated Python-only tasks.
        return None
    if (path / 'approved.json').is_symlink():
        raise ValueError('Approved toolchain metadata must not be a symlink')
    info = json.loads((path / 'approved.json').read_text())
    if (os.name != 'nt' and info.get('uid') != os.getuid()) or info.get('digest') != tree_digest(path / 'runtime'):
        raise ValueError('Approved native toolchain changed; provision it again')
    return path / 'runtime'


def toolchain_signature():
    runtime = toolchain()
    return digest(runtime.parent / 'approved.json') if runtime else ''


def _venv_directory(root):
    return Path(root) / '.venv' / ('Scripts' if os.name == 'nt' else 'bin')


def clean_environment(home, runtime=None):
    from .fs_lock import null_device
    home = Path(home)
    null = null_device()
    bins = [_venv_directory(home.parent)]
    if runtime:
        bins.append(Path(runtime) / ('Scripts' if os.name == 'nt' else 'bin'))
    bins.append(Path(sys.executable).resolve().parent)
    if os.name != 'nt':
        bins.extend((Path('/usr/bin'), Path('/bin'), Path('/usr/sbin'), Path('/sbin')))
    env = {'PATH': os.pathsep.join(map(str, bins)), 'HOME': str(home), 'ZDOTDIR': str(home),
            'TMPDIR': str(home.parent.parent / '.tmp'), 'TMP': str(home.parent.parent / '.tmp'),
            'TEMP': str(home.parent.parent / '.tmp'), 'TMPPREFIX': str(home.parent.parent / '.tmp/zsh'),
            'XDG_CONFIG_HOME': str(home / 'config'), 'XDG_CACHE_HOME': str(home / 'cache'),
            'PYTHONNOUSERSITE': '1', 'PIP_CONFIG_FILE': null, 'PIP_NO_INDEX': '1',
            'PIP_DISABLE_PIP_VERSION_CHECK': '1', 'NPM_CONFIG_USERCONFIG': null,
            'NPM_CONFIG_GLOBALCONFIG': str(home / 'empty-global-npmrc'), 'NPM_CONFIG_CACHE': str(home / 'npm-cache'),
            'NPM_CONFIG_IGNORE_SCRIPTS': 'true', 'NPM_CONFIG_OFFLINE': 'true',
            # Node's optional host OpenSSL config is outside the approved reads.
            # Use library defaults; HTTPS certificate verification stays enabled.
            'OPENSSL_CONF': null,
            'GIT_CONFIG_GLOBAL': null, 'GIT_CONFIG_NOSYSTEM': '1', 'LANG': 'en_US.UTF-8'}
    if os.name == 'nt':
        env['USERPROFILE'] = str(home)
        for name in ('SystemRoot', 'WINDIR', 'PATHEXT', 'COMSPEC'):
            if os.environ.get(name):
                env[name] = os.environ[name]
    return env


def workspace_environment(working):
    working = Path(working)
    # A retained clone is model-controlled. Never follow its directory links
    # while the trusted owner creates caches or starts a preview.
    if os.name != 'nt' and hasattr(os, 'O_DIRECTORY') and hasattr(os, 'O_NOFOLLOW'):
        descriptor = os.open(working, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for components in (('.tmp',), ('.native', 'home')):
                current = os.dup(descriptor)
                try:
                    for name in components:
                        try:
                            os.mkdir(name, mode=0o700, dir_fd=current)
                        except FileExistsError:
                            pass
                        child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current)
                        os.close(current)
                        current = child
                finally:
                    os.close(current)
        finally:
            os.close(descriptor)
    else:
        (working / '.tmp').mkdir(exist_ok=True)
        (working / '.native/home').mkdir(parents=True, exist_ok=True)
    home = working / '.native/home'
    env = clean_environment(home, toolchain())
    env['PATH'] = str(_venv_directory(working)) + os.pathsep + os.pathsep.join(env['PATH'].split(os.pathsep)[1:])
    env['RELAY_NATIVE_PATH'] = env['PATH']
    if (working / '.venv/pyvenv.cfg').is_file():
        env['VIRTUAL_ENV'] = str(working / '.venv')
    return env


def create_shell_profile(workspace):
    """Write only into a fresh clone; reject links and existing startup files."""
    if os.name != 'nt' and hasattr(os, 'O_DIRECTORY') and hasattr(os, 'O_NOFOLLOW'):
        current = os.open(workspace, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for name in ('.native', 'home'):
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current)
                os.close(current)
                current = child
            file = os.open('.zprofile', os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=current)
            try:
                os.write(file, b'export PATH="$RELAY_NATIVE_PATH"\n')
            finally:
                os.close(file)
        finally:
            os.close(current)
        return
    home = Path(workspace) / '.native' / 'home'
    if home.is_symlink() or any(parent.is_symlink() for parent in home.parents):
        raise ValueError('Native shell profile directories must not be symlinks')
    home.mkdir(parents=True, exist_ok=True)
    profile = home / '.zprofile'
    if profile.is_symlink():
        raise ValueError('Native shell profile must not be a symlink')
    file = os.open(profile, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(file, b'export PATH="$RELAY_NATIVE_PATH"\n')
    finally:
        os.close(file)


def run(command, cwd, env, timeout=180):
    # Do not forward arbitrary package-manager output, which may contain URLs or
    # local machine details. Callers report a fixed operation on failure.
    result = subprocess.run(list(map(str, command)), cwd=cwd, env=env, stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout)
    if result.returncode:
        raise ValueError('Native dependency operation failed: ' + Path(str(command[0])).name + ' (exit ' + str(result.returncode) + ')')
    return result


def provision_node(source):
    source = Path(source).expanduser().resolve(strict=True)
    if not source.is_file() or not os.access(source, os.X_OK):
        raise ValueError('Select an executable Node binary')
    target = private_directory(root() / 'tools')
    if (target / 'runtime').exists():
        raise ValueError('Toolchain already provisioned; retain it or explicitly move tools aside while Relay is stopped')
    with tempfile.TemporaryDirectory(prefix='provision-', dir=target) as temporary:
        stage = Path(temporary)
        runtime = stage / 'runtime'
        (runtime / 'bin').mkdir(parents=True)
        shutil.copyfile(source, runtime / 'bin/node')
        (runtime / 'bin/node').chmod(0o755)
        home = stage / 'home'
        home.mkdir()
        env = clean_environment(home)
        version = run([runtime / 'bin/node', '--version'], stage, env).stdout.decode().strip()
        if not supported_node(version):
            raise ValueError('Pinned npm requires a supported Node 22.22.2+, 24.15+, or 26+ runtime')
        archive = stage / 'npm.tgz'
        # Framework Python may ship without its optional CA bundle. Use the
        # macOS system CA file in that case; never turn certificate checks off.
        ca_file = '/etc/ssl/cert.pem' if sys.platform == 'darwin' and not ssl.get_default_verify_paths().cafile else None
        context = ssl.create_default_context(cafile=ca_file)
        with urllib.request.urlopen('https://registry.npmjs.org/npm/-/npm-' + NPM_VERSION + '.tgz', timeout=60, context=context) as response:
            data = response.read(20_000_001)
        if len(data) > 20_000_000 or base64.b64encode(hashlib.sha512(data).digest()).decode() != NPM_INTEGRITY:
            raise ValueError('Pinned npm archive integrity check failed')
        archive.write_bytes(data)
        with tarfile.open(archive) as package:
            package.extractall(runtime, filter='data')
        for name, script in (('npm', 'npm-cli.js'), ('npx', 'npx-cli.js')):
            # No shell expansion or inherited runtime lookup.
            (runtime / 'bin' / name).symlink_to('../package/bin/' + script)
        run([runtime / 'bin/node', runtime / 'package/bin/npm-cli.js', '--version'], stage, env)
        (runtime).rename(target / 'runtime')
        write_json(target / 'approved.json', {'uid': os.getuid(), 'node_version': version, 'npm_version': NPM_VERSION,
                                            'digest': tree_digest(target / 'runtime')})
    return {'node': version, 'npm': NPM_VERSION}


def supported_node(version):
    match = re.fullmatch(r'v(\d+)\.(\d+)\.(\d+)', version)
    if not match:
        return False
    major, minor, patch = map(int, match.groups())
    return major >= 26 or (major == 24 and (minor, patch) >= (15, 0)) or (major == 22 and (minor, patch) >= (22, 2))


def _manifest_git():
    if os.name != 'nt':
        return '/usr/bin/git', '/dev/null', {'PATH': '/usr/bin:/bin', 'GIT_CONFIG_GLOBAL': '/dev/null', 'GIT_CONFIG_NOSYSTEM': '1'}
    from .fs_lock import host_path, null_device, which_git
    null = null_device()
    return which_git() or 'git', null, {
        'PATH': host_path(), 'GIT_CONFIG_GLOBAL': null, 'GIT_CONFIG_NOSYSTEM': '1', 'GIT_TERMINAL_PROMPT': '0',
        'SystemRoot': os.environ.get('SystemRoot', ''), 'WINDIR': os.environ.get('WINDIR', ''),
    }


def manifests(workspace):
    workspace = Path(workspace).resolve()
    git, null, env = _manifest_git()
    names = run([git, '-c', 'safe.directory=*', '-c', f'core.hooksPath={null}', 'ls-tree', '-r', '--name-only', '-z', 'HEAD'], workspace, env).stdout.decode().split('\0')
    selected = {}
    for name in names:
        path = Path(name)
        if any(part in RUNTIME_NAMES for part in path.parts):
            raise ValueError('Tracked files collide with managed native environment directories')
        if path.name not in ('requirements.txt', 'package.json', 'package-lock.json'):
            continue
        if path.is_absolute() or '..' in path.parts or len(selected) >= 30:
            raise ValueError('Unsafe or excessive dependency manifests')
        data = run([git, '-c', 'safe.directory=*', '-c', f'core.hooksPath={null}', 'show', 'HEAD:' + name], workspace, env).stdout
        if len(data) > 2_000_000:
            raise ValueError('Dependency manifest too large')
        selected[name] = data.decode()
    return selected


def bundle_key(files):
    data = {'files': files, 'python': sys.version, 'tools': toolchain_signature()}
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def validate_requirements(text):
    # Deliberately small reviewed input language: package names/extras and version
    # constraints only. No pip options, URLs, includes, editables, or local code.
    lines = [line.strip() for line in text.splitlines() if line.strip() and not line.lstrip().startswith('#')]
    if any(not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*(?:\[[A-Za-z0-9_,.-]+\])?(?:\s*(?:==|~=|>=|<=|!=|>|<)\s*[A-Za-z0-9.*+!-]+(?:\s*,\s*(?:==|~=|>=|<=|!=|>|<)\s*[A-Za-z0-9.*+!-]+)*)?', line) for line in lines):
        raise ValueError('Requirements must contain registry package/version constraints only')
    return lines


def validate_package(text):
    package = json.loads(text)
    if any(package.get(name) for name in ('workspaces', 'overrides', 'bundledDependencies', 'bundleDependencies')):
        raise ValueError('Workspace, override and bundled dependency installers require separate review')
    for group in ('dependencies', 'devDependencies', 'optionalDependencies'):
        for name, version in package.get(group, {}).items():
            if not re.fullmatch(r'(?:@[a-z0-9._-]+/)?[a-z0-9._-]+', name) or not isinstance(version, str) or not re.fullmatch(r'[0-9xX*.^~<>=| -]+', version):
                raise ValueError('Only registry dependencies with version ranges are supported')
    return package


def validate_lock(text):
    lock = json.loads(text)
    if lock.get('lockfileVersion') != 3:
        raise ValueError('Expected npm lockfile version 3')
    for name, package in lock.get('packages', {}).items():
        if Path(name).is_absolute() or '..' in Path(name).parts or package.get('link'):
            raise ValueError('Unsafe npm lockfile path')
        if name and (not package.get('resolved', '').startswith('https://registry.npmjs.org/') or not package.get('integrity', '').startswith('sha512-')):
            raise ValueError('Only integrity-pinned official npm registry artifacts are allowed')


def prepare_dependencies(workspace):
    files = manifests(workspace)
    cache = private_directory(root() / 'environments')
    key = bundle_key(files)
    target = cache / key
    if target.exists():
        verify_bundle(target)
        return key
    runtime = toolchain()
    with tempfile.TemporaryDirectory(prefix='prepare-', dir=cache) as temporary:
        stage = Path(temporary)
        payload = stage / 'payload'
        payload.mkdir()
        home = stage / '.native/home'
        home.mkdir(parents=True)
        (stage / '.tmp').mkdir()
        env = clean_environment(home, runtime)
        env.pop('PIP_NO_INDEX')
        env.pop('NPM_CONFIG_OFFLINE')
        requirements = []
        for name, text in files.items():
            if Path(name).name == 'requirements.txt':
                requirements.extend(validate_requirements(text))
            elif Path(name).name == 'package.json':
                validate_package(text)
                if not runtime:
                    raise ValueError('Provision an approved Node/npm toolchain first')
                directory = payload / Path(name).parent
                directory.mkdir(parents=True, exist_ok=True)
                (directory / 'package.json').write_text(text)
                lock_name = str(Path(name).with_name('package-lock.json'))
                if lock_name in files:
                    validate_lock(files[lock_name])
                    (directory / 'package-lock.json').write_text(files[lock_name])
                command = [runtime / 'bin/node', runtime / 'package/bin/npm-cli.js']
                options = ['--ignore-scripts', '--no-audit', '--no-fund', '--registry=https://registry.npmjs.org']
                run([*command, 'install', '--package-lock-only', *options], directory, env)
                validate_lock((directory / 'package-lock.json').read_text())
                run([*command, 'ci', *options], directory, env)
        if requirements:
            wheels = payload / 'wheels'
            wheels.mkdir()
            run([sys.executable, '-I', '-m', 'pip', '--isolated', 'download', '--only-binary=:all:',
                 '--index-url', 'https://pypi.org/simple', '--dest', wheels, *requirements], stage, env)
        write_json(stage / 'bundle.json', {'files': files, 'key': key, 'requirements': bool(requirements), 'digest': tree_digest(payload)})
        shutil.rmtree(home.parent)
        stage.rename(target)
    return key


def verify_bundle(path):
    private_directory(path)
    if (path / 'bundle.json').is_symlink():
        raise ValueError('Dependency metadata must not be a symlink')
    info = json.loads((path / 'bundle.json').read_text())
    if info['digest'] != tree_digest(path / 'payload'):
        raise ValueError('Prepared dependency artifacts changed; prepare a new bundle')
    return info


def hydrate(workspace):
    """Fresh, per-turn installs. No site packages, live venvs or auth are shared."""
    workspace = Path(workspace)
    files = manifests(workspace)
    env = workspace_environment(workspace)
    # macOS /etc/zprofile runs path_helper and prepends system Python paths.
    # Restore the explicit tool order after that login-shell startup step.
    # hydrate only receives a fresh clone with reserved paths rejected above.
    create_shell_profile(workspace)
    key = bundle_key(files)
    bundle = root() / 'environments' / key
    # A lock generated by the previous turn may have become a checkpoint. Match
    # it only to the exact lock and input manifests approved in an earlier bundle.
    if not bundle.exists() and (root() / 'environments').exists():
        for candidate in sorted((root() / 'environments').iterdir()):
            if not candidate.name.isalnum() or len(candidate.name) != 64:
                continue
            info = verify_bundle(candidate)
            expected = dict(info['files'])
            for name in files:
                lock = candidate / 'payload' / name
                if Path(name).name == 'package-lock.json' and lock.is_file():
                    expected[name] = lock.read_text()
            if files == expected and info['key'] == bundle_key(info['files']):
                bundle = candidate
                break
    if not bundle.exists():
        return {'ready': not bool(files), 'bundle': None, 'message': 'No approved dependency bundle. Ask the local owner to run scripts/native_environment.py prepare; network installs in workers are disabled.'}
    info = verify_bundle(bundle)
    if info['key'] != bundle_key(info['files']):
        raise ValueError('Dependency bundle targets a different toolchain or Python version')
    payload = bundle / 'payload'
    if info['requirements']:
        run([sys.executable, '-I', '-m', 'venv', workspace / '.venv'], workspace, env)
        wheels = sorted((payload / 'wheels').glob('*.whl'))
        run([_venv_directory(workspace) / ('python.exe' if os.name == 'nt' else 'python'), '-I', '-m', 'pip', '--isolated', 'install', '--no-index', '--no-deps', *wheels], workspace, env)
    for name in info['files']:
        if Path(name).name == 'package.json':
            relative = Path(name).parent
            shutil.copytree(payload / relative / 'node_modules', workspace / relative / 'node_modules', symlinks=True)
            shutil.copyfile(payload / relative / 'package-lock.json', workspace / relative / 'package-lock.json')
    return {'ready': True, 'bundle': bundle.name, 'message': 'Approved offline dependencies installed in this private workspace.'}
