"""Descriptor-scoped explorer mutations under the existing human reservation."""
import ctypes
import errno
import hashlib
import os
import stat
import sys

from .ide import MAX_TEXT, WorkspaceConflict, file_path, reject_secret_content, version


def rename_exclusive(source_fd, source, target_fd, target):
    """Atomic rename with no destination replacement, including empty folders."""
    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform == 'darwin':
        fn = libc.renameatx_np
        flags = 4  # RENAME_EXCL
    elif sys.platform.startswith('linux') and hasattr(libc, 'renameat2'):
        fn = libc.renameat2
        flags = 1  # RENAME_NOREPLACE
    else:
        raise ValueError('Atomic no-overwrite rename is unsupported on this platform')
    fn.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    fn.restype = ctypes.c_int
    if fn(source_fd, os.fsencode(source), target_fd, os.fsencode(target), flags):
        error = ctypes.get_errno()
        if error in (errno.EEXIST, errno.ENOTEMPTY):
            raise WorkspaceConflict('The destination already exists; choose a different name')
        raise OSError(error, os.strerror(error))


class FileOperations:
    def __init__(self, ide):
        self.ide, self.relay, self.ws = ide, ide.relay, ide.relay.ws

    def folders(self, project):
        result = []
        with self.ws._project_directory(project) as root:
            def walk(fd, prefix=()):
                for name in sorted(os.listdir(fd)):
                    path = '/'.join((*prefix, name))
                    try:file_path(path, write=True)
                    except (ValueError, PermissionError):continue
                    info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                    if stat.S_ISDIR(info.st_mode):
                        result.append(path)
                        if len(result) > 3000:raise ValueError('Explorer directory limit exceeded')
                        with self.ws._directory(fd, [name]) as child:walk(child, (*prefix, name))
            walk(root)
        return result

    def _snapshot(self, root, parts):
        """Hash all affected entries, rejecting protected files and unsafe links."""
        digest = hashlib.sha256()
        count, total = 0, 0
        def visit(parent, name, path):
            nonlocal count, total
            file_path(path, write=True)
            count += 1
            if count > 3000:raise ValueError('Explorer operation exceeds 3,000 entries')
            info = os.stat(name, dir_fd=parent, follow_symlinks=False)
            digest.update((path + '\0' + str(info.st_mode) + '\0').encode())
            if stat.S_ISDIR(info.st_mode):
                with self.ws._directory(parent, [name]) as child:
                    for entry in sorted(os.listdir(child)):visit(child, entry, path + '/' + entry)
                return 'folder', None
            with self.ws._file(parent, name) as fd:
                data = self.ws._read(fd, MAX_TEXT)
                reject_secret_content(data)
                total += len(data)
                if total > 20_000_000:raise ValueError('Explorer operation exceeds 20 MB')
                digest.update(data)
            return 'file', version(data)
        with self.ws._directory(root, parts[:-1]) as parent:
            kind, content_version = visit(parent, parts[-1], '/'.join(parts))
        return {'kind': kind, 'version': content_version if kind == 'file' else digest.hexdigest(), 'entries': count}

    def inspect(self, user, project, name):
        self.relay.access(user, project)
        self.ide._require_editor_support()
        parts = file_path(name, write=True)
        with self.ws._project_directory(project) as root:
            return {'path': name, **self._snapshot(root, parts)}

    def mutate(self, user, project, client, binding, data):
        self.ide._reservation(user, project, client, binding)
        conversation = self.ide.conversation(user, project, data.get('conversation_id'))['id']
        action, name = data.get('action'), data.get('path')
        parts = file_path(name, write=True)
        if action not in ('mkdir', 'rename', 'delete'):raise ValueError('Unknown explorer operation')
        affected = [name]
        target = data.get('target')
        target_parts = file_path(target, write=True) if action == 'rename' else None
        if target_parts:
            affected.append(target)
            if target == name or target.startswith(name + '/'):
                raise ValueError('Cannot move a folder into itself')
        for row in self.relay.rows('SELECT path FROM editor_drafts WHERE project_id=?', (project,)):
            # macOS commonly resolves case aliases to the same inode. Treat
            # them conservatively on all platforms when protecting drafts.
            if any(row['path'].casefold() == path.casefold() or row['path'].casefold().startswith(path.casefold() + '/') for path in affected):
                raise WorkspaceConflict('Save or discard affected editor drafts before renaming or deleting')
        with self.ws._project_directory(project) as root:
            if action == 'mkdir':
                # Parent folders must exist; each explicit folder creation is exclusive.
                with self.ws._directory(root, parts[:-1]) as parent:
                    try:os.mkdir(parts[-1], 0o755, dir_fd=parent)
                    except FileExistsError:raise WorkspaceConflict('A file or folder already exists at this path') from None
            else:
                snapshot = self._snapshot(root, parts)
                if snapshot['version'] != data.get('version'):
                    raise WorkspaceConflict('The file or folder changed. Refresh and review before retrying')
                if action == 'rename':
                    with self.ws._directory(root, parts[:-1]) as source:
                        with self.ws._directory(root, target_parts[:-1]) as destination:
                            rename_exclusive(source, parts[-1], destination, target_parts[-1])
                else:
                    if data.get('confirmed') is not True:raise ValueError('Deletion requires explicit confirmation')
                    def remove(parent, leaf, path):
                        file_path(path, write=True)
                        info = os.stat(leaf, dir_fd=parent, follow_symlinks=False)
                        if stat.S_ISDIR(info.st_mode):
                            with self.ws._directory(parent, [leaf]) as child:
                                for entry in os.listdir(child):remove(child, entry, path + '/' + entry)
                            os.rmdir(leaf, dir_fd=parent)
                        else:
                            with self.ws._file(parent, leaf):pass
                            os.unlink(leaf, dir_fd=parent)
                    with self.ws._directory(root, parts[:-1]) as parent:remove(parent, parts[-1], name)
        self.ide._event(user, project, conversation, 'human_file_operation', action + ' ' + name,
                        {'action': action, 'path': name, 'target': target})
        self.relay.audit(user, 'explorer_' + action, project=project, metadata={'path': name, 'target': target})
        return {'action': action, 'path': name, 'target': target}
