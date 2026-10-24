"""Real isolated runtime verification on disposable copies; no model requests."""
import argparse
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from relay.app_server import AppServerClient, PERMISSION_NAME, server_command, server_environment  # noqa: E402
from relay.native import NativeCodexAdapter, profile_paths  # noqa: E402
from relay.native_environment import private_directory, workspace_environment  # noqa: E402
from relay.workspace import Workspace  # noqa: E402


def execute(client, working, command, report, name):
    result = client.request('command/exec', {'command': command, 'cwd': str(working),
                            'env': workspace_environment(working), 'permissionProfile': PERMISSION_NAME,
                            'timeoutMs': 90000}, timeout=100)
    report['checks'][name] = result
    print(name + ': ' + str(result['exitCode']), flush=True)
    if result['exitCode'] != 0:
        raise RuntimeError(name + ' failed; inspect the local report')


def browser_check(working, report):
    from relay.native_preview import Preview
    from playwright.sync_api import sync_playwright
    # Allocate disposable preview ports; the Relay dashboard or an operator's
    # existing dev server may already own the sample's default 8000/5173.
    reservations = [socket.socket(), socket.socket()]
    try:
        for listener in reservations:
            listener.bind(('127.0.0.1', 0))
        backend, frontend = [listener.getsockname()[1] for listener in reservations]
    finally:
        for listener in reservations:
            listener.close()
    backend_url = 'http://127.0.0.1:' + str(backend)
    frontend_url = 'http://127.0.0.1:' + str(frontend)
    config = working / 'frontend/vite.config.js'
    if config.is_symlink() or not config.resolve().is_relative_to(working):
        raise ValueError('Unsafe preview configuration path')
    text = config.read_text()
    if 'http://127.0.0.1:8000' not in text:
        raise ValueError('Review the project proxy target before previewing')
    config.write_text(text.replace('http://127.0.0.1:8000', backend_url))
    report['preview'] = {'backend': backend_url, 'frontend': frontend_url, 'proxy_override_in_disposable_clone_only': True}
    with Preview(working, backend, frontend) as preview:
        preview.start_backend()
        preview.start_frontend()
        wait_http(backend_url + '/api/ping/')
        wait_http(frontend_url + '/')
        report['django_ping'] = json.loads(urllib.request.urlopen(backend_url + '/api/ping/', timeout=5).read())
        report['vite_proxy_ping'] = json.loads(urllib.request.urlopen(frontend_url + '/api/ping/', timeout=5).read())
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome', headless=True)
            try:
                page = browser.new_page()
                logs, errors = [], []
                page.on('console', lambda message: logs.append({'type': message.type, 'text': message.text}))
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.goto(frontend_url + '/')
                page.get_by_text('Django says: pong', exact=False).wait_for(timeout=15000)
                if not any('[ping] Django response:' in log['text'] and 'pong' in log['text'] for log in logs):
                    raise RuntimeError('Missing actual browser ping console output')
                preview.stop_backend()
                page.get_by_role('button', name='Send ping').click()
                page.get_by_text('Could not reach Django.', exact=False).wait_for(timeout=15000)
                report['backend_unavailable_visible'] = True
                preview.start_backend()
                wait_http(backend_url + '/api/ping/')
                page.get_by_role('button', name='Send ping').click()
                page.get_by_text('Django says: pong', exact=False).wait_for(timeout=15000)
                report.update({'browser_console': logs, 'browser_page_errors': errors, 'browser_recovery': not bool(errors)})
                if errors:
                    raise RuntimeError('Unexpected browser JavaScript errors')
            finally:
                browser.close()


def wait_http(url):
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1) as response:
                if response.status == 200:
                    return
        except OSError:
            pass
        time.sleep(.15)
    raise RuntimeError('Preview did not become ready: ' + url)


def verify(source, destination, browser=False):
    source = source.resolve()
    private_directory(destination)
    output = private_directory(destination / uuid.uuid4().hex)
    managed = Workspace(output / 'source')
    canonical = managed.root / 'project'
    git_env = {'PATH': '/usr/bin:/bin', 'GIT_CONFIG_GLOBAL': '/dev/null', 'GIT_CONFIG_NOSYSTEM': '1'}
    original = subprocess.check_output(['/usr/bin/git', 'rev-parse', 'HEAD'], cwd=source, env=git_env).decode().strip()
    subprocess.run(['/usr/bin/git', '-c', 'core.hooksPath=/dev/null', 'clone', '--quiet', '--no-local', '--no-hardlinks', source, canonical], env=git_env, check=True, capture_output=True)
    if (source / '.relay').is_dir():
        shutil.copytree(source / '.relay', canonical / '.relay', dirs_exist_ok=True)
    report = {'source_checkpoint': original, 'runtime_verified': False, 'model_execution': False,
              'saved_conversation_resumed': False, 'checks': {}, 'workspaces': []}
    path = output / 'report.json'
    home = private_directory(output / 'unauthenticated-app-server')
    codex = shutil.which(os.environ.get('RELAY_CODEX_BIN', 'codex'))
    if not codex:
        raise RuntimeError('Codex CLI is not installed or not on PATH')
    client = AppServerClient(server_command(codex), server_environment(home), home)
    try:
        client.initialize()
        previous = None
        for profile in ('b', 'c'):
            _, profile_root = profile_paths(profile)
            private_directory(profile_root)
            adapter = NativeCodexAdapter({'native_profile': profile})
            working, parent = adapter._prepare(canonical)
            report['workspaces'].append(str(working))
            environment = json.loads((working / '.native/environment.json').read_text())
            if not environment['ready']:
                raise RuntimeError(environment['message'])
            report['checks'][profile + '_dependencies'] = environment
            execute(client, working, ['/bin/zsh', '-lc', 'command -v node && node --version && command -v npm && npm --version && command -v python3 && python3 -c "import django; print(django.get_version())"'], report, profile + '_tools')
            execute(client, working, ['/bin/zsh', '-lc', 'python backend/manage.py check && python backend/manage.py test ping --verbosity 2'], report, profile + '_django')
            execute(client, working, ['/bin/zsh', '-lc', 'cd frontend && npm run build'], report, profile + '_vite_build')
            canary = home / 'nonsecret-canary'
            canary.write_text('nonsecret-isolation-probe')
            blocked = [str(canary), str(canonical / 'README.md')]
            if previous:
                blocked.append(str(previous / 'frontend/package.json'))
            code = ('import pathlib,socket,sys\n'
                    'for name in sys.argv[1:]:\n'
                    ' try: pathlib.Path(name).read_bytes()\n'
                    ' except PermissionError: pass\n'
                    ' else: raise RuntimeError("outside workspace readable")\n'
                    'try: socket.create_connection(("127.0.0.1",9000),timeout=1)\n'
                    'except PermissionError: pass\n'
                    'else: raise RuntimeError("worker network policy not enforced")\n'
                    'print("isolation and network denials verified")')
            execute(client, working, [str(working / '.venv/bin/python'), '-I', '-c', code, *blocked], report, profile + '_isolation')
            if profile == 'b':
                adapter._publish(canonical, working, parent)
                report['disposable_checkpoint'] = managed.checkpoint('project', 'Verify prepared native dependencies and lockfile')
                previous = working
            elif browser:
                browser_check(working, report)
        if subprocess.check_output(['/usr/bin/git', 'rev-parse', 'HEAD'], cwd=source, env=git_env).decode().strip() != original:
            raise RuntimeError('Source project changed during verification')
        report.update({'runtime_verified': True, 'dependency_continuation_verified': True, 'source_checkpoint_unchanged': True})
    finally:
        client.close()
        path.write_text(json.dumps(report, indent=2))
        print('Report: ' + str(path), flush=True)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace', type=Path, required=True)
    parser.add_argument('--output', type=Path, default=Path('native-environment-proof'))
    parser.add_argument('--browser', action='store_true')
    args = parser.parse_args()
    try:
        report = verify(args.workspace, args.output.resolve(), args.browser)
        print('PASS: actual sandboxed runtime/dependency continuation' + (' and browser recovery' if report.get('browser_recovery') else '') + '; no model turn requested.')
    except (ValueError, RuntimeError, OSError) as exc:
        print('BLOCKED/FAILED: ' + str(exc), file=sys.stderr)
        raise SystemExit(2) from None


if __name__ == '__main__':
    main()
