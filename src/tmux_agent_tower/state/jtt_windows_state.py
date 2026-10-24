"""Windows ACL helpers for the existing JTT workspace and layout stores.

The JSON schemas remain shared with POSIX. Windows validates the same files
with the current-user-only ACL used by the ConPTY registry rather than
interpreting POSIX owner and mode bits that Windows does not provide.
"""

from __future__ import annotations

import os
from pathlib import Path
import stat
from typing import Type


def ensure_private_directory(path: Path, error_type: Type[RuntimeError]) -> None:
    try:
        from ..terminal.windows_manager import _ensure_private_directory

        _ensure_private_directory(Path(path))
    except Exception as exc:
        raise error_type("JTT state directory is not private") from exc


def validate_private_directory(path: Path, error_type: Type[RuntimeError]) -> None:
    try:
        from ..terminal.windows_manager import (
            _check_no_reparse_components,
            _private_acl,
        )

        absolute = Path(os.path.abspath(os.fspath(path)))
        _check_no_reparse_components(absolute)
        _private_acl(absolute, directory=True)
    except Exception as exc:
        raise error_type("JTT state directory is not private") from exc


def validate_private_file(path: Path, error_type: Type[RuntimeError], *,
                          allow_missing: bool = False) -> None:
    path = Path(path)
    if not os.path.lexists(path):
        if allow_missing:
            return
        raise error_type("JTT state file is missing")
    try:
        from ..terminal.windows_manager import _private_acl

        _private_acl(path, directory=False)
        info = os.lstat(path)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise error_type("JTT state file is invalid")
    except error_type:
        raise
    except Exception as exc:
        raise error_type("JTT state file is not private") from exc


def validate_new_private_file(path: Path, error_type: Type[RuntimeError]) -> None:
    """Pin and verify the current-user ACL on a file created exclusively by JTT."""
    try:
        from ..terminal.windows_manager import _private_acl

        _private_acl(Path(path), directory=False, apply=True)
    except Exception as exc:
        raise error_type("JTT state file cannot be secured") from exc
    validate_private_file(path, error_type)


def read_private_file(path: Path, limit: int, error_type: Type[RuntimeError]) -> bytes:
    """Read a bounded private file while allowing atomic replacement readers."""
    validate_private_file(path, error_type)
    try:
        import win32con
        import win32file

        share = (win32con.FILE_SHARE_READ | win32con.FILE_SHARE_WRITE
                 | win32con.FILE_SHARE_DELETE)
        flags = (win32con.FILE_ATTRIBUTE_NORMAL
                 | getattr(win32con, "FILE_FLAG_OPEN_REPARSE_POINT", 0x00200000))
        handle = win32file.CreateFile(
            str(path), win32con.GENERIC_READ, share, None,
            win32con.OPEN_EXISTING, flags, None,
        )
    except Exception as exc:
        raise error_type("JTT state file cannot be opened safely") from exc
    try:
        info = win32file.GetFileInformationByHandle(handle)
        attributes = int(info[0])
        if (attributes & getattr(win32con, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
                or attributes & win32con.FILE_ATTRIBUTE_DIRECTORY):
            raise error_type("JTT state file is not a regular file")
        data = bytearray()
        while len(data) <= limit:
            _status, chunk = win32file.ReadFile(handle, min(64 * 1024, limit + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
        if len(data) > limit:
            raise error_type("JTT state file exceeds the allowed size")
        return bytes(data)
    except error_type:
        raise
    except Exception as exc:
        raise error_type("JTT state file cannot be read safely") from exc
    finally:
        handle.Close()
