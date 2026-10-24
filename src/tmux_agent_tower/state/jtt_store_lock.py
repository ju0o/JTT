"""Cross-store serialization for mutations under one private JTT state root."""

from __future__ import annotations

from contextlib import contextmanager
import os
import stat
from pathlib import Path
import time
from typing import Callable, Iterator, Type

if os.name == "nt":
    import msvcrt
else:
    import fcntl


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
        fd = (_open_windows_lock(path, error_type) if os.name == "nt"
              else os.open(path, flags, 0o600))
    except OSError as exc:
        raise error_type("Cannot open the private JTT state lock") from exc
    try:
        info = os.fstat(fd)
        if os.name == "nt":
            from .jtt_windows_state import validate_private_file

            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise error_type("The JTT state lock is not private")
            validate_private_file(path, error_type)
            _windows_lock(fd, error_type)
            try:
                yield
            finally:
                _windows_unlock(fd, error_type)
        else:
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


def _windows_lock(fd: int, error_type: Type[RuntimeError]) -> None:
    # msvcrt.locking locks a byte range, so ensure the shared lock file has
    # that byte before attempting the cross-process lock.
    try:
        if os.fstat(fd).st_size == 0:
            os.lseek(fd, 0, os.SEEK_SET)
            os.write(fd, b"\0")
            os.fsync(fd)
    except OSError as exc:
        raise error_type("Cannot initialize the private JTT state lock") from exc
    deadline = time.monotonic() + 30.0
    while True:
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            break
        except OSError as exc:
            if time.monotonic() >= deadline:
                raise error_type("Timed out acquiring the private JTT state lock") from exc
            time.sleep(0.01)


def _open_windows_lock(path: Path, error_type: Type[RuntimeError]) -> int:
    """Open the lock itself without following a Windows reparse point."""
    handle = None
    try:
        import win32con
        import win32file

        flags = (win32con.FILE_ATTRIBUTE_NORMAL
                 | getattr(win32con, "FILE_FLAG_OPEN_REPARSE_POINT", 0x00200000))
        access = win32con.GENERIC_READ | win32con.GENERIC_WRITE
        share = win32con.FILE_SHARE_READ | win32con.FILE_SHARE_WRITE
        try:
            # CREATE_NEW distinguishes a lock created by this call from an
            # existing lock whose ACL must be validated without repair.
            handle = win32file.CreateFile(
                str(path), access, share, None,
                getattr(win32con, "CREATE_NEW", 1), flags, None,
            )
            created = True
        except Exception as exc:
            code = getattr(exc, "winerror", None)
            if code is None and getattr(exc, "args", ()):
                try:
                    code = int(exc.args[0])
                except (TypeError, ValueError):
                    code = None
            if code not in (80, 183):  # ERROR_FILE_EXISTS / ERROR_ALREADY_EXISTS
                raise
            handle = win32file.CreateFile(
                str(path), access, share, None,
                win32con.OPEN_EXISTING, flags, None,
            )
            created = False
        info = win32file.GetFileInformationByHandle(handle)
        attributes = int(info[0])
        if (attributes & getattr(win32con, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
                or attributes & win32con.FILE_ATTRIBUTE_DIRECTORY):
            raise error_type("The JTT state lock is not a regular file")
        from .jtt_windows_state import validate_new_private_file, validate_private_file
        if created:
            # New files inherit the private directory DACL, but an elevated
            # Windows process may assign a different owner. Pin the new file
            # to the current user's SID before accepting it. Existing files
            # are never repaired here: an unsafe existing lock remains a
            # fail-closed error.
            validate_new_private_file(path, error_type)
        else:
            validate_private_file(path, error_type)
        raw_handle = handle.Detach()
        handle = None
        try:
            return msvcrt.open_osfhandle(raw_handle, os.O_RDWR)
        except Exception:
            import win32api

            win32api.CloseHandle(raw_handle)
            raise
    except error_type:
        raise
    except Exception as exc:
        raise error_type("Cannot open the private JTT state lock") from exc
    finally:
        if handle is not None:
            handle.Close()


def _windows_unlock(fd: int, error_type: Type[RuntimeError]) -> None:
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    except OSError as exc:
        raise error_type("Cannot release the private JTT state lock") from exc
