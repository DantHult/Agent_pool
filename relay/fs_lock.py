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
    """Locate git on PATH or common Windows install directories."""
    found = shutil.which('git')
    if found:
        return found
    if os.name != 'nt':
        return None
    candidates = [
        Path(r'D:\Git') / 'cmd' / 'git.exe',
        Path(r'D:\Git') / 'bin' / 'git.exe',
        Path(os.environ.get('ProgramFiles', r'C:\Program Files')) / 'Git' / 'cmd' / 'git.exe',
        Path(os.environ.get('ProgramFiles', r'C:\Program Files')) / 'Git' / 'bin' / 'git.exe',
        Path(os.environ.get('ProgramFiles(x86)', r'C:\Program Files (x86)')) / 'Git' / 'cmd' / 'git.exe',
        Path(os.environ.get('LocalAppData', '')) / 'Programs' / 'Git' / 'cmd' / 'git.exe',
    ]
    for candidate in candidates:
        if candidate.is_file():
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


def current_gid():
    getter = getattr(os, 'getgid', None)
    return getter() if getter else None
