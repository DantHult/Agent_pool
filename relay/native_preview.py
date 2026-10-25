"""Operator-only Django/Vite previews with a separate, port-scoped OS sandbox.

This never changes the native App Server permission profile. The operator picks
two loopback ports; project code cannot connect to Relay, peer previews, or the
Internet. Child processes inherit the same filesystem and network restrictions.
"""
import json
import errno
import os
from pathlib import Path
import platform
import re
import signal
import socket
import subprocess
import sys

from .native_environment import root, toolchain, workspace_environment


def private_workspace(workspace):
    original = Path(workspace).absolute()
    for component in (original, *original.parents):
        if component.is_symlink():
            raise ValueError('Preview workspace must not contain symlinks')
    workspace = original.resolve(strict=True)
    base = root() / 'workspaces'
    if not workspace.is_relative_to(base):
        raise ValueError('Preview requires a private native worker workspace')
    parts = workspace.relative_to(base).parts
    if len(parts) != 2 or parts[0] not in ('a', 'b', 'c') or not re.fullmatch('[0-9a-f]{32}', parts[1]):
        raise ValueError('Preview requires one isolated native worktree')
    if not workspace.is_dir():
        raise ValueError('Preview workspace must be owned by this OS user')
    uid = getattr(os, 'getuid', lambda: None)()
    profile = workspace.parent
    # Windows has no uid and its permission bits do not encode mode 0700.
    if uid is not None and (workspace.stat().st_uid != uid or profile.stat().st_mode & 0o077 or profile.stat().st_uid != uid):
        raise ValueError('Preview workspace must be owned by this OS user and its parent must have mode 0700')
    return workspace


def ports(backend, frontend):
    if any(type(port) is not int or not 1024 <= port <= 65535 for port in (backend, frontend)) or backend == frontend:
        raise ValueError('Select two distinct unprivileged loopback ports')
    return backend, frontend


def ensure_port_available(port):
    """Refuse to grant a preexisting service access through a preview's ports.

    Check both loopback families because Seatbelt's localhost matcher covers
    both. This is an availability probe, not a reservation against races by
    other trusted applications running under the operator's OS account.
    """
    probes = []
    try:
        for family, address in ((socket.AF_INET, '127.0.0.1'), (socket.AF_INET6, '::1')):
            try:
                probe = socket.socket(family, socket.SOCK_STREAM)
                probes.append(probe)
                if family == socket.AF_INET6:
                    probe.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
                try:
                    probe.bind((address, port))
                except OSError as exc:
                    if exc.errno != errno.EADDRINUSE:
                        raise
                    # Completed HTTP connections can leave TIME_WAIT sockets
                    # after our backend exits. SO_REUSEADDR alone is unsafe on
                    # macOS: it can also bind a more specific address beneath
                    # an active wildcard listener. First require an explicit
                    # refusal, then verify the reusable bind/listen succeeds.
                    with socket.socket(family, socket.SOCK_STREAM) as connection:
                        connection.settimeout(.25)
                        if connection.connect_ex((address, port)) != errno.ECONNREFUSED:
                            raise exc
                    probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    probe.bind((address, port))
                    probe.listen(1)
            except OSError as exc:
                if family == socket.AF_INET6 and exc.errno in (errno.EAFNOSUPPORT, errno.EPROTONOSUPPORT, errno.EADDRNOTAVAIL):
                    continue
                raise ValueError('Preview loopback port ' + str(port) + ' is already occupied or unavailable') from None
    finally:
        for probe in probes:
            probe.close()


def preview_policy(workspace, backend_port=8000, frontend_port=5173, runtime=None):
    return sandbox_policy(workspace, ports(backend_port, frontend_port), runtime)


def sandbox_policy(workspace, selected, runtime=None):
    """Seatbelt profile for project code that may use only the given loopback ports."""
    workspace = private_workspace(workspace)
    approved = toolchain()
    if runtime is not None and Path(runtime).resolve() != approved:
        raise ValueError('Preview runtime must be the approved toolchain')
    runtime = approved
    if not 1 <= len(selected) <= 2 or len(set(selected)) != len(selected) or any(type(port) is not int or not 1024 <= port <= 65535 for port in selected):
        raise ValueError('Select one or two distinct unprivileged loopback ports')
    python = Path(sys.base_prefix).resolve()
    if python in (Path('/'), Path.home().resolve()) or python.is_relative_to(root()):
        raise ValueError('Unsafe Python runtime scope')
    quoted = lambda value: json.dumps(str(value))
    lines = ['(version 1)', '(deny default)', '(allow process-exec)', '(allow process-fork)',
             '(allow signal (target same-sandbox))', '(allow process-info* (target same-sandbox))',
             '(allow sysctl-read (sysctl-name-prefix "hw.") '
             '(sysctl-name "machdep.cpu.brand_string") (sysctl-name "kern.argmax") '
             '(sysctl-name "kern.hostname") (sysctl-name "kern.osproductversion") '
             '(sysctl-name "kern.osrelease") (sysctl-name "kern.ostype") '
             '(sysctl-name "kern.osversion") (sysctl-name "kern.version") '
             '(sysctl-name "kern.maxfilesperproc") (sysctl-name "kern.maxproc") '
             '(sysctl-name "vm.loadavg") (sysctl-name "sysctl.proc_cputype"))',
             '(allow mach-lookup (global-name "com.apple.system.opendirectoryd.libinfo"))',
             '(allow file-read* (literal "/") (subpath "/System") (subpath "/usr/lib") '
             '(subpath "/usr/share") (subpath "/usr/bin") (subpath "/bin") '
             '(literal "/private/etc/localtime") (literal "/private/etc/hosts") '
             '(literal "/dev/null") (literal "/dev/random") (literal "/dev/urandom"))',
             '(allow file-write-data (literal "/dev/null"))',
             '(allow file-read* (subpath ' + quoted(python) + '))',
             '(allow file-read* file-write* (subpath ' + quoted(workspace) + '))',
             '(deny file-read* file-write* (subpath ' + quoted(workspace / '.git') + ') '
             '(subpath ' + quoted(workspace / '.relay') + ') '
             '(regex #"^/.*[.]env$"))']
    if runtime:
        lines.append('(allow file-read* (subpath ' + quoted(runtime) + '))')
    # realpath()/framework launchers inspect ancestor metadata. This grants no
    # file contents or directory listings outside the permitted runtime/clone.
    ancestors = {parent for scope in (workspace, python, runtime) if scope for parent in Path(scope).parents}
    lines.append('(allow file-read-metadata ' + ' '.join('(literal ' + quoted(parent) + ')' for parent in sorted(ancestors)) + ')')
    for port in selected:
        # Seatbelt accepts only '*' or 'localhost' here. 'localhost' is its
        # loopback matcher (not a DNS lookup); actual servers bind IPv4 127.0.0.1.
        address = '"localhost:' + str(port) + '"'
        lines.extend(('(allow network-bind (local ip ' + address + '))',
                      '(allow network-inbound (local ip ' + address + '))',
                      '(allow network-outbound (remote ip ' + address + '))'))
    return '\n'.join(lines) + '\n'


def stop_group(child):
    """Stop a sandboxed process and every descendant in its process group."""
    if child is None:
        return
    # Always signal the whole process group, even if its direct child exited.
    if hasattr(os, 'killpg'):
        try:
            os.killpg(child.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        finally:
            # A parent may exit while a descendant ignores SIGTERM. Reaping the
            # direct process alone must not leave a project server running.
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        child.wait(timeout=5)
        return
    try:
        if child.poll() is None:
            child.terminate()
        child.wait(timeout=5)
    except subprocess.TimeoutExpired:
        child.kill()
        child.wait(timeout=5)


class Preview:
    """No HTTP endpoint exposes this runner. Invoke explicitly as the OS owner."""
    def __init__(self, workspace, backend_port=8000, frontend_port=5173):
        if platform.system() != 'Darwin':
            raise ValueError('Native previews require macOS Seatbelt')
        self.workspace = private_workspace(workspace)
        self.backend_port, self.frontend_port = ports(backend_port, frontend_port)
        # No project code runs if either selected port belongs to Relay or any
        # other existing service. Never silently pick a broader/fallback port.
        ensure_port_available(self.backend_port)
        ensure_port_available(self.frontend_port)
        self.policy = preview_policy(self.workspace, backend_port, frontend_port)
        self.environment = workspace_environment(self.workspace)
        self.processes = {}
        self.logs = {}

    def _start(self, name, command, cwd):
        if name in self.processes and self.processes[name].poll() is None:
            raise ValueError('Preview process is already running')
        self.stop(name)
        if name in ('backend', 'frontend'):
            # On backend recovery the frontend legitimately remains running;
            # recheck only the port of the process being started.
            ensure_port_available(self.backend_port if name == 'backend' else self.frontend_port)
        log_path = self.workspace / '.native' / ('preview-' + name + '.log')
        if log_path.is_symlink():
            raise ValueError('Preview log must not be a symlink')
        if hasattr(os, 'O_DIRECTORY') and hasattr(os, 'O_NOFOLLOW'):
            directory = os.open(self.workspace / '.native', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                fd = os.open(log_path.name, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=directory)
            finally:
                os.close(directory)
            output = os.fdopen(fd, 'ab')
        else:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            output = open(log_path, 'ab')
        try:
            child = subprocess.Popen(['/usr/bin/sandbox-exec', '-p', self.policy, *map(str, command)],
                                     cwd=cwd, env=self.environment, stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
                                     start_new_session=True)
        finally:
            output.close()
        self.processes[name] = child
        self.logs[name] = log_path
        return child

    def start_backend(self):
        python = self.workspace / '.venv/bin/python'
        if not python.exists() or not (self.workspace / 'backend/manage.py').is_file():
            raise ValueError('Prepare the project Python environment before previewing')
        return self._start('backend', [python, 'manage.py', 'runserver',
                                      '127.0.0.1:' + str(self.backend_port), '--noreload'], self.workspace / 'backend')

    def start_frontend(self):
        runtime = toolchain()
        vite = self.workspace / 'frontend/node_modules/vite/bin/vite.js'
        if not runtime or not vite.is_file():
            raise ValueError('Prepare the approved Node/Vite environment before previewing')
        # The project's Vite config controls the proxy target. Defaults match the
        # generated sample (8000/5173); callers choosing other ports must review
        # and change only their private clone's config themselves.
        return self._start('frontend', [runtime / 'bin/node', vite, '--host', '127.0.0.1',
                                       '--port', str(self.frontend_port), '--strictPort'], self.workspace / 'frontend')

    def stop(self, name):
        stop_group(self.processes.pop(name, None))

    def stop_backend(self):
        self.stop('backend')

    def close(self):
        for name in tuple(self.processes):
            self.stop(name)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
