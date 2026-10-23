"""Native Windows session registry for the JTT ``TerminalRegistry`` contract.

The POSIX implementation remains in :mod:`manager`. This backend owns only
sessions it created: each session has a private registry record, one dedicated
worker, a ConPTY PowerShell process, and an owner-SID named pipe.
"""
from __future__ import annotations

import base64
import ctypes
from dataclasses import replace
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import time
import uuid
import zlib
from typing import Any, Optional

from . import manager as _shared
from .manager import (
    MAX_FRAME,
    MAX_REQUEST_FRAME,
    PROTOCOL,
    SESSION_ID_RE,
    SESSION_META,
    TerminalCell,
    TerminalError,
    TerminalIdentity,
    TerminalObservation,
    TerminalScreen,
    TerminalUpdate,
    _decode_screen,
    _identity,
    _terminal_identity_key,
    _validate_name,
)


PIPE_PREFIX = r"\\.\pipe\jtt-"
MAX_PIPE_NAME_CHARS = 240
LEASE_SECONDS = 15.0
OUTPUT_BUFFER_BYTES = 256 * 1024
METADATA_REPLACE_TIMEOUT = 1.0
STARTUP_TIMEOUT = 12.0
STOP_TIMEOUT = 8.0
WORKER = Path(__file__).with_name("_windows_worker.py")
_WINDOWS = sys.platform == "win32"


def _require_windows() -> None:
    if not _WINDOWS:
        raise TerminalError("the native Windows terminal backend requires Windows")


def renderer_available() -> bool:
    return _shared.renderer_available()


def _default_root() -> Path:
    explicit = os.environ.get("JTT_HOME")
    if explicit:
        return Path(explicit).expanduser() / "terminals"
    local = os.environ.get("LOCALAPPDATA")
    if not local:
        raise TerminalError("LOCALAPPDATA is unavailable; set JTT_HOME explicitly")
    return Path(local) / "JTT" / "terminals"


def _worker_executable() -> Path:
    """Resolve the real interpreter, bypassing Windows venv redirector PIDs."""
    executable = getattr(sys, "_base_executable", None) or sys.executable
    try:
        path = Path(executable).resolve(strict=True)
    except (OSError, TypeError, ValueError) as exc:
        raise TerminalError("cannot resolve the Windows terminal worker interpreter") from exc
    if not path.is_file():
        raise TerminalError("Windows terminal worker interpreter is unavailable")
    return path


def _worker_import_paths() -> tuple[Path, ...]:
    """Return the product source and active environment package directories."""
    package_root = Path(__file__).resolve().parents[2]
    try:
        prefix = Path(sys.prefix).resolve()
    except (OSError, RuntimeError):
        prefix = Path(sys.prefix)
    candidates = [package_root]
    for entry in sys.path:
        if not entry:
            continue
        try:
            path = Path(entry).resolve()
        except (OSError, RuntimeError):
            continue
        if not any(part.casefold() in ("site-packages", "dist-packages")
                   for part in path.parts):
            continue
        try:
            path.relative_to(prefix)
        except ValueError:
            continue
        if path.is_dir() and path not in candidates:
            candidates.append(path)
    return tuple(candidates)


def _planned_socket_path(session_dir: Path, session_id: str) -> str:
    # Compatibility name for the diagnostics/UI boundary; this is a pipe, not
    # a filesystem socket, and the session UUID makes its name unguessable.
    return PIPE_PREFIX + session_id


def _current_sid():
    _require_windows()
    import win32api
    import win32con
    import win32security

    token = win32security.OpenProcessToken(
        win32api.GetCurrentProcess(), win32con.TOKEN_QUERY
    )
    try:
        return win32security.GetTokenInformation(token, win32security.TokenUser)[0]
    finally:
        token.Close()


def _security_modules():
    _require_windows()
    try:
        import pywintypes
        import win32con
        import win32security
    except ImportError as exc:
        raise TerminalError(
            "Windows runtime dependency pywin32 is missing; reinstall JTT"
        ) from exc
    return pywintypes, win32con, win32security


def _is_reparse(path: Path) -> bool:
    _require_windows()
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_attributes = kernel32.GetFileAttributesW
    get_attributes.argtypes = [ctypes.c_wchar_p]
    get_attributes.restype = ctypes.c_uint32
    attributes = get_attributes(str(path))
    if attributes == 0xFFFFFFFF:
        error = ctypes.get_last_error()
        if error == 2:
            return False
        raise TerminalError("cannot inspect Windows terminal path")
    return bool(attributes & 0x400)  # FILE_ATTRIBUTE_REPARSE_POINT


def _private_acl(path: Path, *, directory: bool, apply: bool = False) -> None:
    """Validate or install a protected, current-user-only Windows DACL."""
    _require_windows()
    _pywintypes, _win32con, win32security = _security_modules()
    sid = _current_sid()
    if _is_reparse(path) or path.is_symlink():
        raise TerminalError("terminal registry path must not contain reparse points")
    if apply:
        acl = win32security.ACL()
        flags = 0
        if directory:
            flags = (win32security.OBJECT_INHERIT_ACE
                     | win32security.CONTAINER_INHERIT_ACE)
        acl.AddAccessAllowedAceEx(
            win32security.ACL_REVISION, flags, _win32con.GENERIC_ALL, sid
        )
        descriptor = win32security.SECURITY_DESCRIPTOR()
        # Elevated Windows runners can otherwise assign the Administrators
        # group as owner even when the DACL is user-only. Pin the owner to the
        # same user SID as the sole allowed ACE before validating the result.
        descriptor.SetSecurityDescriptorOwner(sid, False)
        descriptor.SetSecurityDescriptorDacl(True, acl, False)
        protected = getattr(win32security, "PROTECTED_DACL_SECURITY_INFORMATION", 0x80000000)
        win32security.SetFileSecurity(
            str(path),
            win32security.OWNER_SECURITY_INFORMATION
            | win32security.DACL_SECURITY_INFORMATION | protected,
            descriptor,
        )
    try:
        descriptor = win32security.GetFileSecurity(
            str(path), win32security.OWNER_SECURITY_INFORMATION
            | win32security.DACL_SECURITY_INFORMATION
        )
        owner = descriptor.GetSecurityDescriptorOwner()
        acl = descriptor.GetSecurityDescriptorDacl()
    except Exception as exc:
        raise TerminalError(
            "terminal path security could not be inspected",
            diagnostic={"stage": "private_acl", "exception": "inspection_failed"},
        ) from exc
    if owner != sid:
        raise TerminalError(
            "terminal path owner is not the current Windows user",
            diagnostic={"stage": "private_acl", "exception": "owner_mismatch"},
        )
    if acl is None:
        raise TerminalError(
            "terminal path has no Windows access-control list",
            diagnostic={"stage": "private_acl", "exception": "dacl_missing"},
        )
    if acl.GetAceCount() != 1:
        raise TerminalError(
            "terminal path is not private to the current Windows user",
            diagnostic={"stage": "private_acl", "exception": "dacl_entry_count"},
        )
    ace = acl.GetAce(0)
    if ace[0][0] != win32security.ACCESS_ALLOWED_ACE_TYPE or ace[2] != sid:
        raise TerminalError(
            "terminal path has an unsafe Windows DACL",
            diagnostic={"stage": "private_acl", "exception": "dacl_identity_or_type"},
        )


def _check_no_reparse_components(path: Path) -> None:
    absolute = Path(os.path.abspath(os.fspath(path)))
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current = current / part
        if current.exists() and _is_reparse(current):
            raise TerminalError("terminal registry path must not contain reparse points")


def _ensure_private_directory(path: Path) -> None:
    _require_windows()
    path = Path(os.path.abspath(os.fspath(path)))
    _check_no_reparse_components(path)
    missing = []
    current = path
    while not current.exists():
        missing.append(current)
        parent = current.parent
        if parent == current:
            raise TerminalError("cannot create private terminal registry")
        current = parent
    for directory in reversed(missing):
        try:
            directory.mkdir()
        except FileExistsError:
            pass
        except OSError as exc:
            raise TerminalError("cannot create private terminal registry") from exc
        _private_acl(directory, directory=True, apply=True)
    if not path.is_dir():
        raise TerminalError("terminal registry path is not a directory")
    _private_acl(path, directory=True)


def is_private_directory(path: Path) -> bool:
    """Read-only privacy probe used by ``jtt doctor`` on Windows."""
    try:
        _private_acl(Path(path), directory=True)
        return True
    except (TerminalError, OSError):
        return False


def _read_metadata(path: Path) -> dict[str, Any]:
    _private_acl(path.parent, directory=True)
    if _is_reparse(path) or path.is_symlink():
        raise TerminalError("terminal metadata must not be a reparse point")
    try:
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > 16 * 1024:
            raise TerminalError("terminal metadata is invalid")
        _private_acl(path, directory=False)
        if _WINDOWS:
            value = json.loads(_read_windows_metadata_text(path))
        else:
            value = json.loads(path.read_text(encoding="utf-8"))
    except TerminalError:
        raise
    except (OSError, ValueError, UnicodeError) as exc:
        raise TerminalError("terminal metadata is unavailable or invalid") from exc
    if not isinstance(value, dict):
        raise TerminalError("terminal metadata is invalid")
    return value


def _read_windows_metadata_text(path: Path) -> str:
    """Read one metadata generation with delete sharing for atomic replacement.

    The registry writer replaces metadata with ``os.replace``. A default
    Python text handle on Windows can deny delete sharing, making the writer
    fail with ERROR_ACCESS_DENIED while another JTT process validates a
    session. Keep the read bound and allow the old generation to remain
    readable through the retained file handle.
    """
    import win32con
    import win32file

    share = (win32con.FILE_SHARE_READ | win32con.FILE_SHARE_WRITE
             | win32con.FILE_SHARE_DELETE)
    try:
        handle = win32file.CreateFile(
            str(path), win32con.GENERIC_READ, share, None,
            win32con.OPEN_EXISTING, win32con.FILE_ATTRIBUTE_NORMAL, None,
        )
    except Exception as exc:
        raise TerminalError("terminal metadata is unavailable or invalid") from exc
    try:
        data = bytearray()
        while len(data) <= 16 * 1024:
            _status, chunk = win32file.ReadFile(
                handle, min(4096, 16 * 1024 + 1 - len(data))
            )
            if not chunk:
                break
            data.extend(chunk)
        if len(data) > 16 * 1024:
            raise TerminalError("terminal metadata is invalid")
        return bytes(data).decode("utf-8")
    except TerminalError:
        raise
    except Exception as exc:
        raise TerminalError("terminal metadata is unavailable or invalid") from exc
    finally:
        handle.Close()


def _replace_metadata(source: Path, destination: Path) -> None:
    """Atomically replace metadata after brief Windows reader contention.

    Windows can report ERROR_ACCESS_DENIED when another process has the old
    file open, even when that reader allowed delete sharing. Retry only the
    two sharing/contention errors and only for a bounded interval; permanent
    ACL failures still fail closed.
    """
    deadline = time.monotonic() + METADATA_REPLACE_TIMEOUT
    delay = 0.005
    while True:
        try:
            os.replace(source, destination)
            return
        except OSError as exc:
            code = getattr(exc, "winerror", None)
            if code is None and getattr(exc, "args", ()):
                try:
                    code = int(exc.args[0])
                except (TypeError, ValueError):
                    code = None
            if code not in (5, 32) or time.monotonic() >= deadline:
                raise
            remaining = deadline - time.monotonic()
            time.sleep(min(delay, max(0.0, remaining)))
            delay = min(delay * 2, 0.05)


def _atomic_metadata(path: Path, value: dict[str, Any]) -> None:
    temp = path.with_name(".meta-" + uuid.uuid4().hex)
    try:
        with temp.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, separators=(",", ":"), ensure_ascii=True)
            stream.flush()
            os.fsync(stream.fileno())
        _private_acl(temp, directory=False, apply=True)
        _replace_metadata(temp, path)
        _private_acl(path, directory=False)
    except Exception as exc:
        try:
            temp.unlink()
        except OSError:
            pass
        if isinstance(exc, TerminalError):
            if exc.diagnostic is not None:
                raise
            raise TerminalError(
                "terminal metadata could not be saved",
                diagnostic={"stage": "metadata_write", "exception": "terminal_error"},
            ) from exc
        code = getattr(exc, "winerror", None)
        if code is None and getattr(exc, "args", ()):
            try:
                code = int(exc.args[0])
            except (TypeError, ValueError):
                code = None
        category = {5: "access_denied", 32: "sharing_violation",
                    80: "already_exists", 183: "already_exists"}.get(
                        code, "metadata_io_error"
                    )
        raise TerminalError(
            "terminal metadata could not be saved",
            diagnostic={"stage": "metadata_write", "exception": category},
        ) from exc


def _process_facts(pid: int, *, expected_start: Optional[str] = None,
                   verify_image: bool = True) -> tuple[str, str, bool]:
    """Return (creation time, image, alive) using a retained query handle.

    A process that cannot be inspected raises instead of being reported as
    dead, so cleanup cannot mistake access denial for an exited process.
    """
    _require_windows()
    import win32api
    import win32con
    import win32event
    import win32process

    try:
        handle = win32api.OpenProcess(
            win32con.PROCESS_QUERY_LIMITED_INFORMATION | win32con.SYNCHRONIZE,
            False, int(pid),
        )
    except Exception as exc:
        code = getattr(exc, "winerror", None)
        if code is None and getattr(exc, "args", ()):
            try:
                code = int(exc.args[0])
            except (TypeError, ValueError):
                code = None
        if code == 87:  # ERROR_INVALID_PARAMETER: the PID no longer exists.
            return "", "", False
        raise TerminalError("terminal process identity could not be verified") from exc
    try:
        wait_result = win32event.WaitForSingleObject(handle, 0)
        if wait_result == win32con.WAIT_OBJECT_0:
            return "", "", False
        if wait_result != win32con.WAIT_TIMEOUT:
            raise TerminalError("terminal process identity could not be verified")
        times = win32process.GetProcessTimes(handle)
        created = str(times["CreationTime"])
        if expected_start is not None and created != str(expected_start):
            # This is a different process after PID reuse. Its image is not
            # relevant to the expected identity and must not be treated as it.
            return created, "", True
        if not verify_image:
            # Used only after the session identity and pipe were validated
            # before sending its stop request. Creation time on this retained
            # query handle is enough to observe that exact process exiting.
            return created, "", True
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        query_image = kernel32.QueryFullProcessImageNameW
        query_image.argtypes = [ctypes.c_void_p, ctypes.c_uint32,
                                ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_uint32)]
        query_image.restype = ctypes.c_int
        image_buffer = ctypes.create_unicode_buffer(32768)
        image_length = ctypes.c_uint32(len(image_buffer))
        if not query_image(ctypes.c_void_p(int(handle)), 0, image_buffer,
                           ctypes.byref(image_length)):
            # The retained handle still names the same process. If it became
            # signaled between the initial wait and this query, report it as
            # exited; otherwise a query failure remains an unknown identity.
            wait_result = win32event.WaitForSingleObject(handle, 0)
            if wait_result == win32con.WAIT_OBJECT_0:
                return "", "", False
            if wait_result != win32con.WAIT_TIMEOUT:
                raise TerminalError(
                    "terminal process identity could not be verified",
                    diagnostic={"stage": "process_exit_confirmation",
                                "exception": "wait_result_invalid"},
                )
            error_code = ctypes.get_last_error()
            category = ("image_query_winerror_{}".format(error_code)
                        if error_code else "image_query_failed")
            raise TerminalError(
                "terminal process image could not be verified",
                diagnostic={"stage": "process_image_query", "exception": category},
            )
        image = image_buffer.value
        return created, os.path.normcase(os.path.abspath(image)), True
    except TerminalError:
        raise
    except Exception as exc:
        raise TerminalError("terminal process identity could not be verified") from exc
    finally:
        handle.Close()


def _process_image(handle) -> str:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    query_image = kernel32.QueryFullProcessImageNameW
    query_image.argtypes = [ctypes.c_void_p, ctypes.c_uint32,
                            ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_uint32)]
    query_image.restype = ctypes.c_int
    image_buffer = ctypes.create_unicode_buffer(32768)
    image_length = ctypes.c_uint32(len(image_buffer))
    if not query_image(ctypes.c_void_p(int(handle)), 0, image_buffer,
                       ctypes.byref(image_length)):
        raise TerminalError("terminal process image could not be verified")
    return os.path.normcase(os.path.abspath(image_buffer.value))


def _terminate_owned_powershell(pid: int, expected_start: str,
                                expected_path: str) -> bool:
    """Terminate only the exact PowerShell child recorded by failed startup."""
    _require_windows()
    import win32api
    import win32con
    import win32event
    import win32process

    if pid <= 0 or not expected_start:
        raise TerminalError("owned PowerShell identity is incomplete; registry retained")
    if os.path.normcase(os.path.abspath(expected_path)) != os.path.normcase(
            os.path.abspath(_powershell_executable())):
        raise TerminalError("owned PowerShell executable identity changed; registry retained")
    try:
        handle = win32api.OpenProcess(
            win32con.PROCESS_QUERY_LIMITED_INFORMATION
            | win32con.PROCESS_TERMINATE | win32con.SYNCHRONIZE,
            False, int(pid),
        )
    except Exception:
        created, _image, alive = _process_facts(pid)
        if not alive or created != expected_start:
            return False
        raise TerminalError("owned PowerShell handle is unavailable; registry retained")
    try:
        wait_result = win32event.WaitForSingleObject(handle, 0)
        if wait_result == win32con.WAIT_OBJECT_0:
            return False
        if wait_result != win32con.WAIT_TIMEOUT:
            raise TerminalError("owned PowerShell identity could not be verified; registry retained")
        created = str(win32process.GetProcessTimes(handle)["CreationTime"])
        if created != expected_start:
            return False
        image = _process_image(handle)
        if image != os.path.normcase(os.path.abspath(expected_path)):
            raise TerminalError("owned PowerShell image identity changed; registry retained")
        win32api.TerminateProcess(handle, 1)
        if win32event.WaitForSingleObject(handle, 5000) != win32con.WAIT_OBJECT_0:
            raise TerminalError("owned PowerShell did not exit; registry retained")
        return True
    finally:
        handle.Close()


def _powershell_executable() -> Path:
    shell = Path(os.environ.get("SystemRoot", r"C:\Windows")) / (
        r"System32\WindowsPowerShell\v1.0\powershell.exe"
    )
    try:
        return shell.resolve(strict=True)
    except OSError as exc:
        raise TerminalError("Windows PowerShell is unavailable") from exc


def _identity_matches_process(identity: TerminalIdentity) -> tuple[bool, bool]:
    """Return (worker_matches, shell_matches) without signaling any process."""
    expected_worker = os.path.normcase(os.path.abspath(_worker_executable()))
    worker_reason = _process_identity_mismatch(
        identity.supervisor_pid, identity.supervisor_start, expected_worker
    )
    shell_reason = _process_identity_mismatch(
        identity.shell_pid, identity.shell_start
    )
    return worker_reason is None, shell_reason is None


def _process_identity_mismatch(
    pid: int, expected_start: str, expected_image: Optional[str] = None
) -> Optional[str]:
    created, image, alive = _process_facts(pid, expected_start=expected_start)
    if not alive:
        return "process_unavailable"
    if created != expected_start:
        return "creation_time_mismatch"
    if expected_image is not None and image != expected_image:
        return "image_path_mismatch"
    return None


def _startup_error_category(message: str) -> str:
    """Map startup validation errors to a small, path-free diagnostic enum."""
    categories = (
        ("terminal supervisor identity is stale", "supervisor_identity_stale"),
        ("terminal shell identity is stale", "shell_identity_stale"),
        ("terminal shell identity is inconsistent", "shell_identity_inconsistent"),
        ("terminal named pipe server identity is stale", "pipe_server_identity_stale"),
        ("terminal named pipe identity is invalid", "pipe_identity_invalid"),
        ("terminal supervisor connection failed", "pipe_connection_failed"),
        ("terminal session is not ready", "session_not_ready"),
    )
    for fragment, category in categories:
        if fragment in message:
            return category
    return "startup_validation_failed"


def _validate_identity(root: Path, session_id: str) -> tuple[Path, dict[str, Any]]:
    _ensure_private_directory(root)
    if not SESSION_ID_RE.fullmatch(str(session_id)):
        raise TerminalError("invalid terminal session id")
    session_dir = root / session_id
    _private_acl(session_dir, directory=True)
    metadata = _read_metadata(session_dir / SESSION_META)
    if (metadata.get("protocol") != PROTOCOL or metadata.get("session_id") != session_id
            or not isinstance(metadata.get("generation"), str)
            or not SESSION_ID_RE.fullmatch(metadata["generation"])):
        raise TerminalError("terminal identity does not match the requested session")
    identity = _identity(metadata)
    expected_worker = os.path.normcase(os.path.abspath(_worker_executable()))
    worker_reason = _process_identity_mismatch(
        identity.supervisor_pid, identity.supervisor_start, expected_worker
    )
    if worker_reason is not None:
        raise TerminalError(
            "terminal supervisor identity is stale",
            diagnostic={"stage": "identity_validation",
                        "exception": "worker_" + worker_reason},
        )
    shell_reason = _process_identity_mismatch(
        identity.shell_pid, identity.shell_start
    )
    if identity.state == "running" and shell_reason is not None:
        raise TerminalError(
            "terminal shell identity is stale",
            diagnostic={"stage": "identity_validation",
                        "exception": "shell_" + shell_reason},
        )
    if identity.state == "exited" and shell_reason is None:
        raise TerminalError(
            "terminal shell identity is inconsistent",
            diagnostic={"stage": "identity_validation",
                        "exception": "shell_still_alive"},
        )
    return session_dir, metadata


class TerminalRegistry:
    """Windows implementation of the UI-facing terminal registry contract."""

    def __init__(self, root: Optional[os.PathLike] = None):
        self.root = Path(os.path.abspath(os.fspath(root if root is not None else _default_root())))

    def list_sessions(self) -> tuple[TerminalIdentity, ...]:
        _require_windows()
        if not self.root.exists():
            return ()
        _ensure_private_directory(self.root)
        result = []
        for entry in self.root.iterdir():
            if not SESSION_ID_RE.fullmatch(entry.name):
                continue
            try:
                session_dir, metadata = _validate_identity(self.root, entry.name)
                identity = _identity(metadata)
                worker_ok, shell_ok = _identity_matches_process(identity)
                if worker_ok:
                    state = "running" if shell_ok else (
                        "exited" if identity.state == "exited" else "identity_mismatch"
                    )
                else:
                    state = "orphaned"
                result.append(replace(identity, state=state))
            except TerminalError as exc:
                # Keep a safely parseable card visible as a conflict; never turn
                # invalid identity into permission to connect or stop.
                try:
                    metadata = _read_metadata(entry / SESSION_META)
                    identity = _identity(metadata)
                    state = "identity_mismatch" if "identity" in str(exc) else "orphaned"
                    result.append(replace(identity, state=state))
                except TerminalError:
                    continue
        return tuple(result)

    def create(self, name: Optional[str] = None) -> "TerminalClient":
        _require_windows()
        if not renderer_available():
            _shared._require_renderer()
        try:
            import win32api  # noqa: F401
            import win32pipe  # noqa: F401
            from winpty import Backend, PTY  # noqa: F401
        except ImportError as exc:
            raise TerminalError(
                "Windows terminal dependencies are missing; reinstall JTT"
            ) from exc
        display_name = _validate_name(name)
        _ensure_private_directory(self.root)
        session_id, generation = uuid.uuid4().hex, uuid.uuid4().hex
        session_dir = self.root / session_id
        try:
            session_dir.mkdir()
            _private_acl(session_dir, directory=True, apply=True)
        except Exception as exc:
            raise TerminalError("cannot create a private Windows terminal session") from exc
        try:
            shell = _powershell_executable()
        except TerminalError:
            _remove_session_files(session_dir)
            raise
        pipe_name = _planned_socket_path(session_dir, session_id)
        if len(pipe_name) > MAX_PIPE_NAME_CHARS:
            _remove_session_files(session_dir)
            raise TerminalError("Windows named pipe path is too long")
        initial = {
            "protocol": PROTOCOL, "session_id": session_id, "generation": generation,
            "supervisor_pid": 0, "supervisor_start": "", "shell_pid": 0,
            "shell_start": "", "shell_path": str(shell), "name": display_name,
            "state": "starting", "renderer_state": "starting", "pipe_name": pipe_name,
            "working_directory": os.getcwd(),
        }
        _atomic_metadata(session_dir / SESSION_META, initial)
        args = [str(_worker_executable()), "-S", str(WORKER), "--worker",
                str(session_dir), session_id, generation, display_name]
        env = _worker_environment(session_dir)
        try:
            process = subprocess.Popen(
                args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, close_fds=True, shell=False, env=env,
                creationflags=(getattr(subprocess, "CREATE_NO_WINDOW", 0)
                               | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)),
            )
        except OSError as exc:
            _remove_session_files(session_dir)
            raise TerminalError("cannot start the Windows terminal supervisor") from exc
        deadline = time.monotonic() + STARTUP_TIMEOUT
        last_startup_reason = "starting"
        while time.monotonic() < deadline:
            if process.poll() is not None:
                break
            try:
                _, metadata = _validate_identity(self.root, session_id)
                if metadata.get("state") == "running":
                    return self.connect(session_id, timeout=1.0)
            except TerminalError as exc:
                safe_diagnostic = getattr(exc, "diagnostic", None)
                if (isinstance(safe_diagnostic, dict)
                        and isinstance(safe_diagnostic.get("exception"), str)
                        and safe_diagnostic["exception"].isidentifier()):
                    last_startup_reason = safe_diagnostic["exception"]
                else:
                    last_startup_reason = _startup_error_category(str(exc))
            time.sleep(0.02)
        # Popen retains the process handle created for this exact worker. Never
        # terminate a PID discovered from the registry during failed startup.
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired as exc:
                raise TerminalError(
                    "Windows supervisor startup timed out; registry retained for safety"
                ) from exc
        try:
            metadata = _read_metadata(session_dir / SESSION_META)
            if metadata.get("session_id") != session_id or metadata.get("generation") != generation:
                raise TerminalError("terminal identity changed during startup cleanup")
            startup_failure = metadata.get("startup_failure")
            diagnostic = None
            if isinstance(startup_failure, dict):
                stage = startup_failure.get("stage")
                exception = startup_failure.get("exception")
                if (isinstance(stage, str) and stage.isidentifier()
                        and isinstance(exception, str) and exception.isidentifier()):
                    cause_stage = startup_failure.get("cause_stage")
                    cause = startup_failure.get("cause")
                    if (isinstance(cause_stage, str) and cause_stage.isidentifier()
                            and isinstance(cause, str) and cause.isidentifier()):
                        exception = cause_stage + "_" + cause
                    diagnostic = {"stage": stage, "exception": exception}
            if diagnostic is None and metadata.get("state") == "running":
                diagnostic = {
                    "stage": "startup_validation",
                    "exception": last_startup_reason,
                }
            shell_pid = int(metadata.get("shell_pid") or 0)
            shell_start = str(metadata.get("shell_start") or "")
            if shell_pid:
                shell_path = metadata.get("shell_path")
                if not isinstance(shell_path, str):
                    raise TerminalError("owned PowerShell path is unavailable; registry retained")
                _terminate_owned_powershell(shell_pid, shell_start, shell_path)
            _remove_session_files(session_dir)
            if diagnostic is not None:
                raise TerminalError(
                    "Windows terminal startup failed "
                    "(stage={}, exception={})".format(
                        diagnostic["stage"], diagnostic["exception"]
                    ), diagnostic=diagnostic,
                )
        except TerminalError:
            raise
        raise TerminalError("Windows terminal supervisor failed during startup")

    def connect(self, session_id: str, *, timeout: float = 3.0) -> "TerminalClient":
        _require_windows()
        session_dir, metadata = _validate_identity(self.root, session_id)
        identity = _identity(metadata)
        if identity.state not in ("running", "exited"):
            raise TerminalError("terminal session is not ready")
        pipe_name = metadata.get("pipe_name")
        if not isinstance(pipe_name, str) or pipe_name != _planned_socket_path(session_dir, session_id):
            raise TerminalError("terminal named pipe identity is invalid")
        return TerminalClient(self.root, session_dir, identity, pipe_name, timeout=timeout)

    def stop(self, session_id: str, *, expected_generation: Optional[str] = None,
             expected_shell_pid: Optional[int] = None) -> None:
        _require_windows()
        session_dir, metadata = _validate_identity(self.root, session_id)
        identity = _identity(metadata)
        if expected_generation is not None and identity.generation != str(expected_generation):
            raise TerminalError("terminal identity changed before stop")
        if expected_shell_pid is not None and identity.shell_pid != int(expected_shell_pid):
            raise TerminalError("terminal identity changed before stop")
        client = self.connect(session_id)
        try:
            if _terminal_identity_key(client.identity) != _terminal_identity_key(identity):
                raise TerminalError("terminal identity changed before stop")
            client._request("stop")
        finally:
            client.close()
        deadline = time.monotonic() + STOP_TIMEOUT
        while time.monotonic() < deadline:
            worker_created, _worker_image, worker_alive = _process_facts(
                identity.supervisor_pid, expected_start=identity.supervisor_start,
                verify_image=False,
            )
            shell_created, _shell_image, shell_alive = _process_facts(
                identity.shell_pid, expected_start=identity.shell_start,
                verify_image=False,
            )
            if (not worker_alive or worker_created != identity.supervisor_start):
                if shell_alive and shell_created == identity.shell_start:
                    raise TerminalError("owned PowerShell did not exit; registry retained")
                _remove_session_files(session_dir)
                return
            time.sleep(0.025)
        raise TerminalError("Windows supervisor did not stop cleanly; registry retained")


def _worker_environment(session_dir: Path) -> dict[str, str]:
    keys = ("SystemRoot", "WINDIR", "PATH", "TEMP", "TMP", "LOCALAPPDATA",
            "APPDATA", "USERPROFILE", "HOMEDRIVE", "HOMEPATH")
    env = {key: os.environ[key] for key in keys if key in os.environ}
    env.setdefault("SystemRoot", r"C:\Windows")
    env.setdefault("WINDIR", env["SystemRoot"])
    env.setdefault("PATH", str(Path(env["SystemRoot"]) / "System32"))
    import_roots = _worker_import_paths()
    env["PYTHONPATH"] = os.pathsep.join(str(path) for path in import_roots)
    env["PYTHONNOUSERSITE"] = "1"
    dll_roots = [str(path / "pywin32_system32") for path in import_roots
                 if (path / "pywin32_system32").is_dir()]
    if dll_roots:
        env["PATH"] = os.pathsep.join([*dll_roots, env["PATH"]])
    env["JTT_TERMINAL_SESSION_DIR"] = str(session_dir)
    return env


def _remove_session_files(path: Path) -> None:
    _private_acl(path, directory=True)
    for entry in tuple(path.iterdir()):
        if _is_reparse(entry) or entry.is_dir():
            raise TerminalError("refusing to remove an unexpected Windows session artifact")
        _private_acl(entry, directory=False)
        entry.unlink()
    path.rmdir()


def _identity_payload(identity: TerminalIdentity) -> dict[str, Any]:
    return {
        "session_id": identity.session_id, "generation": identity.generation,
        "supervisor_pid": identity.supervisor_pid,
        "supervisor_start": identity.supervisor_start,
        "shell_pid": identity.shell_pid, "shell_start": identity.shell_start,
    }


class TerminalClient:
    """Synchronous, bounded client for one owner-SID named-pipe connection."""

    def __init__(self, root: Path, session_dir: Path, identity: TerminalIdentity,
                 pipe_name: str, *, timeout: float = 3.0):
        _require_windows()
        import win32con
        import win32file
        import win32pipe

        self._root, self._session_dir = root, session_dir
        self.identity, self._pipe_name = identity, pipe_name
        self.state = identity.state
        self.synced = False
        self.lease = False
        self._sequence = 0
        self._client_id = uuid.uuid4().hex
        self._closed = False
        try:
            timeout_ms = max(1, min(30000, int(float(timeout) * 1000)))
        except (TypeError, ValueError) as exc:
            raise TerminalError("terminal connection timeout is invalid") from exc
        try:
            win32pipe.WaitNamedPipe(pipe_name, timeout_ms)
            self._pipe = win32file.CreateFile(
                pipe_name, win32con.GENERIC_READ | win32con.GENERIC_WRITE, 0,
                None, win32con.OPEN_EXISTING, 0, None,
            )
            win32pipe.SetNamedPipeHandleState(
                self._pipe, win32pipe.PIPE_READMODE_MESSAGE, None, None
            )
            if _named_pipe_server_pid(self._pipe) != identity.supervisor_pid:
                raise TerminalError("terminal named pipe server identity is stale")
            self._request("hello")
        except Exception as exc:
            if hasattr(self, "_pipe"):
                self._pipe.Close()
            self._closed = True
            if isinstance(exc, TerminalError):
                raise
            raise TerminalError("terminal supervisor connection failed") from exc

    def _request(self, operation: str, **fields: Any) -> dict[str, Any]:
        if self._closed:
            raise TerminalError("terminal client is closed")
        if operation in ("acquire", "input", "resize", "stop"):
            try:
                _session_dir, metadata = _validate_identity(self._root, self.identity.session_id)
                current = _identity(metadata)
                if _terminal_identity_key(current) != _terminal_identity_key(self.identity):
                    self.lease = False
                    raise TerminalError("stale_identity")
            except TerminalError:
                self.lease = False
                raise
        request = {
            "op": operation, "client_id": self._client_id,
            "identity": _identity_payload(self.identity), **fields,
        }
        frame = json.dumps(request, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
        if len(frame) > MAX_REQUEST_FRAME:
            raise TerminalError("terminal request is too large")
        import win32file
        try:
            win32file.WriteFile(self._pipe, frame)
            _status, payload = win32file.ReadFile(self._pipe, MAX_FRAME)
            response = json.loads(payload.decode("utf-8"))
        except Exception as exc:
            self.state, self.synced, self.lease = "unavailable", False, False
            raise TerminalError("terminal supervisor connection failed") from exc
        if not isinstance(response, dict):
            raise TerminalError("terminal response is invalid")
        if not response.get("ok"):
            reason = str(response.get("error") or "terminal request rejected")
            if reason in ("stale_identity", "session_exited", "input_lease_required",
                          "stream_gap", "lease_expired"):
                self.lease = False
                if reason in ("stale_identity", "stream_gap"):
                    self.synced = False
            raise TerminalError(reason)
        if (response.get("session_id") != self.identity.session_id
                or response.get("generation") != self.identity.generation):
            self.synced = self.lease = False
            raise TerminalError("stale_identity")
        self.state = str(response.get("state", self.state))
        if "lease" in response:
            self.lease = bool(response["lease"])
        return response

    def poll(self) -> TerminalUpdate:
        response = self._request("poll", after=self._sequence)
        try:
            output = base64.b64decode(response.get("data", ""), validate=True)
            sequence = int(response.get("sequence", self._sequence))
            if sequence < self._sequence:
                raise ValueError("sequence regressed")
        except (ValueError, TypeError) as exc:
            self.synced = False
            raise TerminalError("terminal output frame is invalid") from exc
        self._sequence = sequence
        self.synced = bool(response.get("synced"))
        screen = _decode_screen(response)
        available = bool(response.get("renderer_available"))
        if available and screen is None:
            self.synced = False
        renderer_state = str(response.get("renderer_state") or "unknown")
        return TerminalUpdate(
            output=output, sequence=sequence, state=self.state,
            synced=self.synced, gap=bool(response.get("gap")),
            more=bool(response.get("more")), columns=int(response.get("columns", 80)),
            lines=int(response.get("lines", 24)), screen=screen,
            renderer_available=available, snapshot_sequence=int(response.get("snapshot_sequence", 0)),
            renderer_state=renderer_state,
        )

    def observe(self) -> TerminalObservation:
        response = self._request("observe")
        if (response.get("session_id") != self.identity.session_id
                or response.get("generation") != self.identity.generation
                or int(response.get("shell_pid") or 0) != self.identity.shell_pid):
            self.synced = self.lease = False
            raise TerminalError("stale_identity")
        screen = _decode_screen(response) if response.get("synced") else None
        synced = bool(response.get("synced")) and screen is not None
        return TerminalObservation(
            session_id=self.identity.session_id, generation=self.identity.generation,
            shell_pid=self.identity.shell_pid,
            sequence=int(response.get("output_sequence") or 0),
            state=str(response.get("terminal_state") or "unknown"), synced=synced,
            columns=int(response.get("columns") or 0), lines=int(response.get("lines") or 0),
            screen=screen, renderer_state=str(response.get("renderer_state") or "unknown"),
        )

    def acquire(self) -> bool:
        update = self.poll()
        if not update.synced or update.gap:
            self.lease = False
            raise TerminalError("stream_gap")
        response = self._request("acquire", after=self._sequence)
        self.lease = bool(response.get("lease"))
        return self.lease

    def release(self) -> None:
        if self._closed:
            self.lease = False
            return
        try:
            self._request("release")
        finally:
            self.lease = False

    def send(self, data: bytes) -> int:
        if not isinstance(data, bytes) or not data or len(data) > 8192:
            raise TerminalError("terminal input must be 1-8192 bytes")
        update = self.poll()
        if not update.synced or update.gap:
            self.release()
            raise TerminalError("stream_gap")
        if not self.lease:
            raise TerminalError("input_lease_required")
        response = self._request(
            "input", data=base64.b64encode(data).decode("ascii"), after=self._sequence
        )
        return int(response.get("written", 0))

    def resize(self, columns: int, lines: int) -> None:
        if (isinstance(columns, bool) or isinstance(lines, bool)
                or not isinstance(columns, int) or not isinstance(lines, int)
                or not 20 <= columns <= 300 or not 5 <= lines <= 120):
            raise TerminalError("terminal size is outside the supported range")
        if not self.lease:
            raise TerminalError("input_lease_required")
        update = self.poll()
        if not update.synced or update.gap:
            self.release()
            raise TerminalError("stream_gap")
        self._request("resize", columns=columns, lines=lines, after=self._sequence)

    def close(self) -> None:
        if self._closed:
            return
        if self.lease:
            try:
                self._request("release")
            except TerminalError:
                pass
        self._closed, self.lease, self.synced = True, False, False
        self._pipe.Close()

    def __enter__(self) -> "TerminalClient":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def _named_pipe_server_pid(handle) -> int:
    _require_windows()
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_server_pid = kernel32.GetNamedPipeServerProcessId
    get_server_pid.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
    get_server_pid.restype = ctypes.c_int
    pid = ctypes.c_ulong()
    if not get_server_pid(ctypes.c_void_p(int(handle)), ctypes.byref(pid)):
        raise TerminalError("named pipe server process could not be verified")
    return int(pid.value)
