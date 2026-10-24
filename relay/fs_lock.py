"""Exclusive non-blocking data-directory locks for Unix and Windows."""
from __future__ import annotations

import os
import shutil
from pathlib import Path


def try_exclusive_lock(fileobj) -> None:
    """Acquire an exclusive non-blocking lock on an open file, or raise BlockingIOError.

    The lock is held until the file object is closed. Callers write metadata into the
    same file after a successful acquire (server.lock / relay.lock).
    """
    if os.name == 'nt':
        import msvcrt

        fd = fileobj.fileno()
        position = fileobj.tell()
        try:
            fileobj.seek(0)
            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise BlockingIOError(exc.errno, exc.strerror) from exc
        finally:
            try:
                fileobj.seek(position)
            except OSError:
                pass
        return
    import fcntl

    fcntl.flock(fileobj.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def which_git() -> str | None:
    """Locate git on PATH or common Windows install directories.

    Prefer ``bin\\git.exe`` over the ``cmd\\git.exe`` wrapper. The wrapper
    spends several extra seconds per invocation, which blew the checkpoint
    deadline when a full suite was already using the disk.
    """
    found = shutil.which('git')
    if os.name != 'nt':
        return found
    program_files = os.environ.get('ProgramFiles', r'C:\Program Files')
    local = os.environ.get('LocalAppData', '')
    candidates = []
    if found:
        candidates.append(Path(found))
    candidates.extend([
        Path(r'D:\Git') / 'bin' / 'git.exe',
        Path(r'D:\Git') / 'cmd' / 'git.exe',
        Path(program_files) / 'Git' / 'bin' / 'git.exe',
        Path(program_files) / 'Git' / 'cmd' / 'git.exe',
        Path(os.environ.get('ProgramFiles(x86)', r'C:\Program Files (x86)')) / 'Git' / 'cmd' / 'git.exe',
        Path(local) / 'Programs' / 'Git' / 'bin' / 'git.exe',
        Path(local) / 'Programs' / 'Git' / 'cmd' / 'git.exe',
    ])
    seen = set()
    for candidate in candidates:
        key = str(candidate).lower()
        if key in seen or not candidate.is_file():
            continue
        seen.add(key)
        if candidate.parent.name.lower() == 'cmd':
            sibling = candidate.parent.parent / 'bin' / 'git.exe'
            if sibling.is_file():
                return str(sibling)
        return str(candidate)
    return None


def host_path() -> str:
    """PATH suitable for hosting trusted subprocesses (git, python, unittest)."""
    path = os.environ.get('PATH') or os.defpath
    git = which_git()
    if git:
        directory = str(Path(git).parent)
        if directory and directory.lower() not in path.lower().split(os.pathsep):
            path = directory + os.pathsep + path
    return path


def null_device() -> str:
    return 'NUL' if os.name == 'nt' else '/dev/null'


def current_uid():
    getter = getattr(os, 'getuid', None)
    return getter() if getter else None


def process_uid() -> int:
    """OS user id, or 0 when the platform has no uid (Windows)."""
    uid = current_uid()
    return 0 if uid is None else uid


def current_gid():
    getter = getattr(os, 'getgid', None)
    return getter() if getter else None
