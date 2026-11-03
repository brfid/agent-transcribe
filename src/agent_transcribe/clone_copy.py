"""Independent conversion copies: opportunistic Darwin CoW, normal copy2 fallback.

No receipt/settings change and no reduced storage reservation. Existing archive
objects are never relinked or rewritten. A clone is a separate mutable inode.
"""
import ctypes
import errno
import os
from pathlib import Path
import shutil
import stat
import sys
import tempfile

_UNSUPPORTED = {errno.EXDEV, errno.ENOSYS, errno.ENOTSUP, errno.EOPNOTSUPP}
# Darwin SDK sys/stat.h: document-ID tracking and Finder visibility hints.
# Only these two metadata bits are proven harmless here. All other/unknown
# flags retain the earlier ordinary-copy path, including immutable/append,
# entitlement-protected, nounlink, firmlink and data-less system flags.
_DARWIN_CLONE_METADATA_FLAGS = 0x00000040 | 0x00008000  # UF_TRACKED | UF_HIDDEN


def _blocking_source_flags(source):
    flags = getattr(os.stat(source), 'st_flags', 0)
    return flags & ~_DARWIN_CLONE_METADATA_FLAGS if sys.platform == 'darwin' else flags


def _clonefile(source, destination):
    if sys.platform != 'darwin':
        raise OSError(errno.ENOSYS, 'clonefile is unavailable')
    library = ctypes.CDLL(None, use_errno=True)
    try:
        clone = library.clonefile
    except AttributeError:
        raise OSError(errno.ENOSYS, 'clonefile is unavailable') from None
    clone.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int]
    clone.restype = ctypes.c_int
    if clone(os.fsencode(source), os.fsencode(destination), 0):
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), os.fspath(destination))


def clone_copy2(source, destination, *, follow_symlinks=True):
    """copy2 contract; fresh regular files may use native CoW cloning.

    Existing targets, symlinks and nonregular sources use copy2 directly. Only
    unsupported/cross-volume clone errors fall back; permission/I/O/space errors
    remain fatal. A conflicting path created during a failed clone is preserved.
    """
    target = os.fspath(destination)
    if os.path.isdir(target):
        target = os.path.join(target, os.path.basename(source))
    if (not follow_symlinks or os.path.islink(source) or os.path.lexists(target)
            or _blocking_source_flags(source)
            or not stat.S_ISREG(os.stat(source).st_mode)):
        return shutil.copy2(source, target, follow_symlinks=follow_symlinks)
    try:
        _clonefile(source, target)
    except OSError as exc:
        if exc.errno not in _UNSUPPORTED or os.path.lexists(target):
            raise
        return shutil.copy2(source, target, follow_symlinks=follow_symlinks)
    shutil.copystat(source, target, follow_symlinks=follow_symlinks)
    return target


def atomic_clone_copy(path, source):
    """Exclusive publication of an independent clone/copy, never shared inode.

    A private sibling directory provides an absent clone target. The final link
    is only the existing exclusive-publish primitive; removing the private name
    leaves one independent published inode, never an alias of the source.
    """
    # clonefile/copy2 can inherit blocking flags; historical atomic_copy
    # could read such a source and publish an ordinary private new file.
    # Keep that contract without weakening permission-error propagation.
    if _blocking_source_flags(source):
        from .common import atomic_copy
        return atomic_copy(path, source)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.pending-copy-', dir=path.parent) as tmp:
        temporary = Path(tmp) / 'payload'
        clone_copy2(source, temporary)
        # Preserve atomic_copy's private publication mode; source mtimes remain.
        os.chmod(temporary, 0o600)
        with temporary.open('rb') as stream:
            os.fsync(stream.fileno())
        os.link(temporary, path)
