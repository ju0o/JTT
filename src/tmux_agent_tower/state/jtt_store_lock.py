"""Cross-store serialization for mutations under one private JTT state root."""

from __future__ import annotations

from contextlib import contextmanager
import fcntl
import os
import stat
from pathlib import Path
from typing import Callable, Iterator, Type


class JTTStateLockError(RuntimeError):
    """The private JTT state lock cannot be safely acquired."""


@contextmanager
def state_write_lock(root: Path, ensure_private_root: Callable[[], None],
                     error_type: Type[RuntimeError] = JTTStateLockError) -> Iterator[None]:
    """Serialize read/modify/atomic-replace transactions across JTT stores."""
    ensure_private_root()
    path = root / ".jtt-state.lock"
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(path, flags, 0o600)
    except OSError as exc:
        raise error_type("Cannot open the private JTT state lock") from exc
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) & 0o077 or info.st_nlink != 1):
            raise error_type("The JTT state lock is not private")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
        except OSError as exc:
            raise error_type("Cannot acquire the private JTT state lock") from exc
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
