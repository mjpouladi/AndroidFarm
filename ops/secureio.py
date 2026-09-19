"""Small fail-closed helpers for root-owned JSON state and secrets."""
import json
import os
from pathlib import Path
import tempfile


def require_private_file(path, description='file'):
    path = Path(path)
    try:
        stat = path.lstat()
    except FileNotFoundError as exc:
        raise RuntimeError(f'{description} does not exist: {path}') from exc
    if path.is_symlink() or not path.is_file():
        raise RuntimeError(f'{description} must be a regular file, not a symlink')
    if os.name == 'posix':
        owner = 0 if os.geteuid() == 0 else os.geteuid()
        parent = path.parent.lstat()
        if path.parent.is_symlink() or not path.parent.is_dir() or parent.st_uid != owner or parent.st_mode & 0o077:
            expected = 'root-owned' if owner == 0 else 'owned by the current user'
            raise RuntimeError(f'{description} parent must be {expected} and chmod 700')
        if stat.st_uid != owner or stat.st_mode & 0o077:
            expected = 'root-owned' if owner == 0 else 'owned by the current user'
            raise RuntimeError(f'{description} must be {expected} and chmod 600')
    return path


def require_private_directory(path, description='directory', create=False):
    """Return a root/current-user-only directory, refusing symlink traversal."""
    path = Path(path)
    if create and not path.exists():
        path.mkdir(parents=True, mode=0o700)
    try:
        stat = path.lstat()
    except FileNotFoundError as exc:
        raise RuntimeError(f'{description} does not exist: {path}') from exc
    if path.is_symlink() or not path.is_dir():
        raise RuntimeError(f'{description} must be a directory, not a symlink')
    if os.name == 'posix':
        owner = 0 if os.geteuid() == 0 else os.geteuid()
        if stat.st_uid != owner or stat.st_mode & 0o077:
            expected = 'root-owned' if owner == 0 else 'owned by the current user'
            raise RuntimeError(f'{description} must be {expected} and chmod 700')
    return path


def require_trusted_release_file(path, description='release file'):
    """Require a root-owned, non-symlink release path for root Docker use.

    A Compose file is executable infrastructure when Docker is invoked as
    root. This check prevents a group-writable Coolify checkout or symlink
    from becoming an unreviewed root-controlled build/mount specification.
    """
    path = Path(path)
    if not path.is_absolute():
        raise RuntimeError(f'{description} must be an absolute path')
    parts = path.parts
    current = Path(parts[0])
    for part in parts[1:]:
        current /= part
        try:
            stat = current.lstat()
        except FileNotFoundError as exc:
            raise RuntimeError(f'{description} does not exist: {path}') from exc
        if current.is_symlink():
            raise RuntimeError(f'{description} may not traverse symlinks: {current}')
        if os.name == 'posix' and (stat.st_uid != 0 or stat.st_mode & 0o022):
            raise RuntimeError(f'{description} path must be root-owned and not writable by group/others: {current}')
    if not path.is_file():
        raise RuntimeError(f'{description} must be a regular file')
    return path


def require_trusted_release_tree(path, description='release tree', max_entries=10000):
    """Validate a small, immutable build subtree without following links."""
    path = Path(path)
    # The file helper checks every ancestor too; use a harmless sentinel only
    # after ensuring the root directory itself is trustworthy.
    if not path.is_absolute() or not path.exists() or path.is_symlink() or not path.is_dir():
        raise RuntimeError(f'{description} must be an existing non-symlink directory')
    current = Path(path.parts[0])
    for part in path.parts[1:]:
        current /= part
        stat = current.lstat()
        if current.is_symlink() or (os.name == 'posix' and (stat.st_uid != 0 or stat.st_mode & 0o022)):
            raise RuntimeError(f'{description} path must be root-owned, non-symlink and not group/world writable')
    count = 0
    for parent, directories, files in os.walk(path, followlinks=False):
        for name in [*directories, *files]:
            count += 1
            if count > max_entries:
                raise RuntimeError(f'{description} exceeds {max_entries} entries; use a minimal release directory')
            candidate = Path(parent) / name
            stat = candidate.lstat()
            if candidate.is_symlink() or (os.name == 'posix' and (stat.st_uid != 0 or stat.st_mode & 0o022)):
                raise RuntimeError(f'{description} contains an untrusted path: {candidate}')
    return path


def read_private_json(path, description='JSON file'):
    path = require_private_file(path, description)
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f'{description} is not valid UTF-8 JSON') from exc


def atomic_json(path, value):
    """Atomically replace JSON using a private unpredictable file in the same directory."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary_name = tempfile.mkstemp(prefix=f'.{path.name}.', suffix='.tmp', dir=path.parent)
    temporary = Path(temporary_name)
    try:
        if hasattr(os, 'fchmod'):
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(value, stream, indent=2, sort_keys=True)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        if os.name == 'posix':
            directory = os.open(path.parent, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        if temporary.exists():
            temporary.unlink()
