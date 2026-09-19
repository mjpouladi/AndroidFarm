"""Non-secret generation marker for a managed authentication file."""
import stat


def auth_revision(path):
    info = path.lstat()
    if path.is_symlink() or not stat.S_ISREG(info.st_mode):
        raise RuntimeError('managed authentication file is unavailable')
    return ':'.join(str(value) for value in (info.st_dev, info.st_ino, info.st_mtime_ns,
                                           info.st_ctime_ns, info.st_size))
