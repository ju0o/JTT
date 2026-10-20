"""Private local PTY registry and synchronous client API.

The worker process owns the PTY and account login shell. UI clients may detach
without stopping that shell, then reconnect using the opaque session id. No
terminal output or environment variables are written to disk.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
import json
import os
from pathlib import Path
import pwd
import re
import signal
import socket
import stat
import struct
import subprocess
import sys
import time
from typing import Any, Dict, Optional, Tuple
import uuid
import zlib


PROTOCOL = 1
# A bounded VT snapshot can be substantially larger than terminal text because
# cells carry style metadata. This limit accommodates the capped snapshot plus
# one bounded output batch while keeping each protocol frame finite.
MAX_FRAME = 896 * 1024
MAX_REQUEST_FRAME = 96 * 1024
SESSION_ID_RE = re.compile(r"^[0-9a-f]{32}$")
SESSION_META = "session.json"
SOCKET_NAME = "s"
MAX_UNIX_SOCKET_PATH_BYTES = 107  # Linux sockaddr_un.sun_path, excluding NUL.
SUPERVISOR_STARTUP_TIMEOUT = 5.0
WORKER = Path(__file__).with_name("_worker.py")


class TerminalError(RuntimeError):
    """A terminal session could not be safely created, reached, or used."""

    def __init__(self, message: str, *, diagnostic: Optional[Dict[str, str]] = None):
        super().__init__(message)
        # Sanitized fixture-only startup context (stage + exception class).
        self.diagnostic = diagnostic


@dataclass(frozen=True)
class TerminalIdentity:
    session_id: str
    generation: str
    supervisor_pid: int
    supervisor_start: str
    shell_pid: int
    shell_start: str
    shell_path: str
    name: str
    state: str
    exit_code: Optional[int] = None
    exit_signal: Optional[int] = None
    termination: Optional[str] = None
    renderer_state: str = "unknown"

    @property
    def pid(self) -> int:
        """Compatibility alias for the managed shell PID shown on a card."""

        return self.shell_pid


@dataclass(frozen=True)
class TerminalScreen:
    """Immutable visible VT cells and cursor state, when pyte is available."""

    rows: Tuple[Tuple["TerminalCell", ...], ...]
    cursor_x: int
    cursor_y: int
    cursor_visible: bool


@dataclass(frozen=True)
class TerminalCell:
    text: str
    foreground: str
    background: str
    bold: bool
    italic: bool
    underline: bool
    reverse: bool
    blink: bool
    strikethrough: bool


@dataclass(frozen=True)
class TerminalUpdate:
    output: bytes
    sequence: int
    state: str
    synced: bool
    gap: bool
    more: bool
    columns: int
    lines: int
    screen: Optional[TerminalScreen] = None
    renderer_available: bool = False
    snapshot_sequence: int = 0
    renderer_state: str = "unknown"


@dataclass(frozen=True)
class TerminalObservation:
    """Read-only current screen sample; it never advances a client poll cursor."""

    session_id: str
    generation: str
    shell_pid: int
    sequence: int
    state: str
    synced: bool
    columns: int
    lines: int
    screen: Optional[TerminalScreen]
    renderer_state: str


def _probe_renderer() -> str:
    """Import and initialize the supported VT renderer without side effects."""

    try:
        import pyte
    except ImportError as exc:
        return "dependency_missing" if exc.name == "pyte" else "import_failed"
    except Exception:
        return "import_failed"
    try:
        screen = pyte.Screen(4, 2)
        stream = pyte.Stream(screen)
        stream.feed("\x1b[0m")
        if len(screen.display) != 2:
            return "initialization_failed"
    except Exception:
        return "initialization_failed"
    return "ready"


def renderer_available() -> bool:
    """Return whether pyte imports and initializes the supported VT path."""

    return _probe_renderer() == "ready"


def _require_renderer() -> None:
    state = _probe_renderer()
    messages = {
        "dependency_missing": "VT 렌더러 pyte가 없습니다. scripts/jtt-setup-dev를 실행하세요.",
        "import_failed": "VT 렌더러 pyte를 불러오지 못했습니다. .venv-jtt 환경을 복구하세요.",
        "initialization_failed": "VT 렌더러 초기화가 실패했습니다. .venv-jtt 설치를 확인하세요.",
    }
    if state != "ready":
        raise TerminalError(messages.get(state, "VT 렌더러를 사용할 수 없어 셸을 만들지 않았습니다."))


def _proc_start(pid: int) -> Optional[str]:
    try:
        raw = Path("/proc/{}/stat".format(pid)).read_text(encoding="ascii")
        fields = raw.rsplit(")", 1)[1].split()
        # A terminated child can remain as a zombie until its original
        # Popen owner reaps it. It is no longer a live supervisor/session and
        # must not hold explicit cleanup open indefinitely.
        if fields[0] in ("Z", "X"):
            return None
        return fields[19]
    except (OSError, IndexError, ValueError):
        return None


def _worker_identity_status(pid: int, start: str, session_dir: Path,
                            session_id: str, generation: str) -> str:
    """Classify a supervisor process check without exposing argv contents."""

    try:
        raw_stat = Path("/proc/{}/stat".format(pid)).read_text(encoding="ascii")
        fields = raw_stat.rsplit(")", 1)[1].split()
        if fields[0] in ("Z", "X"):
            return "not_running"
        if fields[19] != start:
            return "start_mismatch"
        raw_args = Path("/proc/{}/cmdline".format(pid)).read_bytes()
        argv = [os.fsdecode(item) for item in raw_args.split(b"\0") if item]
    except (OSError, IndexError, ValueError):
        return "unavailable"
    expected = [str(WORKER), str(session_dir), session_id, generation]
    return "verified" if len(argv) == 5 and argv[1:] == expected else "argv_mismatch"


def _worker_is_same_process(pid: int, start: str, session_dir: Path,
                            session_id: str, generation: str) -> bool:
    """Match a live supervisor by start time and its fixed argv identity."""

    return _worker_identity_status(
        pid, start, session_dir, session_id, generation
    ) == "verified"


def _private_stat(path: Path, *, directory: bool) -> os.stat_result:
    try:
        info = path.lstat()
    except OSError as exc:
        raise TerminalError("private terminal path is unavailable") from exc
    wanted = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if (not wanted or info.st_uid != os.getuid() or info.st_mode & 0o077
            or (not directory and info.st_nlink != 1)):
        raise TerminalError("terminal path is not a private user-owned object")
    return info


def _ensure_private_root(root: Path) -> Path:
    """Create a private JTT home/root chain without changing existing parents.

    ``root.parent`` is the JTT-owned home and must already be private if it
    exists. Missing components below the nearest existing ancestor are made
    one at a time with owner-only permissions. Existing ancestors above that
    boundary (for example ``/tmp`` or ``~/.local/state``) are never chmod'ed.
    """

    path = Path(os.path.abspath(os.fspath(root)))
    _check_root_components(path, require_leaf=False)
    private_home = path.parent

    # The chosen JTT home and final registry root are private boundaries. Do
    # not silently fix an existing user's directory: reject it unchanged.
    if os.path.lexists(private_home):
        _require_private_directory(private_home)
    if os.path.lexists(path):
        _require_private_directory(path)

    if not os.path.lexists(path):
        missing = []
        current = path
        while not os.path.lexists(current):
            missing.append(current)
            current = current.parent
        _check_existing_directory(current)
        for component in reversed(missing):
            created = False
            try:
                component.mkdir(mode=0o700)
                created = True
            except FileExistsError:
                # A concurrent creator is acceptable only if its directory is
                # already private; never chmod an object we did not create.
                pass
            except OSError as exc:
                raise TerminalError("cannot prepare the private terminal registry") from exc
            if created:
                try:
                    os.chmod(component, 0o700)
                except OSError as exc:
                    raise TerminalError("cannot protect a new terminal directory") from exc
            _require_private_directory(component)

    _require_private_directory(private_home)
    _require_private_directory(path)
    return path


def _short_socket_root() -> Path:
    """Return a short, per-user runtime directory for long JTT_HOME paths."""

    return Path("/tmp") / "jtt-{}".format(os.getuid()) / "sockets"


def _planned_socket_path(session_dir: Path, session_id: str) -> Path:
    """Keep the historical in-session socket unless AF_UNIX path limits require
    a short private runtime location.
    """

    local_path = session_dir / SOCKET_NAME
    if len(os.fsencode(local_path)) <= MAX_UNIX_SOCKET_PATH_BYTES:
        return local_path
    return _short_socket_root() / session_id


def _session_socket_path(session_dir: Path, metadata: Dict[str, Any]) -> Path:
    """Resolve a socket path while accepting legacy in-session metadata."""

    stored = metadata.get("socket_path")
    if stored is None:
        path = session_dir / SOCKET_NAME
    else:
        expected = _short_socket_root() / session_dir.name
        if not isinstance(stored, str) or stored != str(expected):
            raise TerminalError("terminal socket path does not match the session")
        path = expected
    if len(os.fsencode(path)) > MAX_UNIX_SOCKET_PATH_BYTES:
        raise TerminalError("terminal socket path is too long")
    return path


def _check_existing_directory(path: Path) -> os.stat_result:
    try:
        info = path.lstat()
    except OSError as exc:
        raise TerminalError("cannot inspect terminal registry parent") from exc
    if not stat.S_ISDIR(info.st_mode) or info.st_uid not in (0, os.getuid()):
        raise TerminalError("terminal registry parent is not a trusted directory")
    return info


def _require_private_directory(path: Path) -> os.stat_result:
    info = _check_existing_directory(path)
    if (info.st_uid != os.getuid() or info.st_mode & 0o077
            or info.st_mode & 0o700 != 0o700):
        raise TerminalError("terminal home and registry directories must be private and user-owned")
    return info


def _check_root_components(path: Path, *, require_leaf: bool) -> None:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current = current / part
        try:
            component = current.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise TerminalError("cannot inspect terminal registry path") from exc
        if stat.S_ISLNK(component.st_mode):
            raise TerminalError("terminal registry path must not contain symlinks")
        if not stat.S_ISDIR(component.st_mode):
            raise TerminalError("terminal registry path components must be directories")
    if require_leaf:
        info = _private_stat(path, directory=True)
        if info.st_mode & 0o077:
            raise TerminalError("terminal registry must be private")


def _default_root() -> Path:
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime:
        base = Path(runtime)
        try:
            info = base.lstat()
        except OSError as exc:
            raise TerminalError("XDG_RUNTIME_DIR is unavailable") from exc
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077):
            raise TerminalError("XDG_RUNTIME_DIR is not private")
        return base / "jtt" / "terminals"
    account = pwd.getpwuid(os.getuid())
    home = Path(account.pw_dir)
    info = home.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise TerminalError("account home is not owned by the current user")
    return home / ".local" / "state" / "jtt" / "terminals"


def _validate_shell() -> Tuple[str, str]:
    """Resolve only the current account's configured login shell."""

    account = pwd.getpwuid(os.getuid())
    configured = account.pw_shell
    if not configured or not os.path.isabs(configured):
        raise TerminalError("account login shell must be an absolute path")
    shell = Path(configured)
    try:
        resolved = shell.resolve(strict=True)
        info = resolved.stat()
    except OSError as exc:
        raise TerminalError("account login shell is unavailable") from exc
    if (not stat.S_ISREG(info.st_mode) or info.st_mode & 0o022
            or info.st_uid not in (0, os.getuid()) or not os.access(resolved, os.X_OK)):
        raise TerminalError("account login shell is not a trusted executable")
    home = Path(account.pw_dir)
    try:
        home_info = home.stat()
    except OSError as exc:
        raise TerminalError("account home directory is unavailable") from exc
    if not stat.S_ISDIR(home_info.st_mode) or home_info.st_uid != os.getuid():
        raise TerminalError("account home directory is not owned by this user")
    return str(resolved), str(home)


def _read_metadata(path: Path) -> Dict[str, Any]:
    _private_stat(path, directory=False)
    if path.stat().st_size > 16 * 1024:
        raise TerminalError("terminal metadata exceeds the allowed size")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(str(path), flags)
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or info.st_nlink != 1
                or info.st_size > 16 * 1024):
            os.close(fd)
            raise TerminalError("terminal metadata is not private")
        with os.fdopen(fd, "r", encoding="utf-8") as stream:
            value = json.load(stream)
    except (OSError, ValueError) as exc:
        raise TerminalError("terminal metadata is invalid") from exc
    if not isinstance(value, dict):
        raise TerminalError("terminal metadata is invalid")
    return value


def _atomic_metadata(path: Path, value: Dict[str, Any]) -> None:
    temp = path.with_name(".meta-" + uuid.uuid4().hex)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(str(temp), flags, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, separators=(",", ":"), ensure_ascii=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(str(temp), str(path))
        os.chmod(str(path), 0o600)
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass


def _identity(value: Dict[str, Any]) -> TerminalIdentity:
    try:
        return TerminalIdentity(
            session_id=str(value["session_id"]),
            generation=str(value["generation"]),
            supervisor_pid=int(value["supervisor_pid"]),
            supervisor_start=str(value["supervisor_start"]),
            shell_pid=int(value["shell_pid"]),
            shell_start=str(value["shell_start"]),
            shell_path=str(value["shell_path"]),
            name=str(value.get("name", "Terminal")),
            state=str(value.get("state", "starting")),
            exit_code=(int(value["exit_code"]) if value.get("exit_code") is not None else None),
            exit_signal=(int(value["exit_signal"]) if value.get("exit_signal") is not None else None),
            termination=(str(value["termination"]) if value.get("termination") else None),
            renderer_state=str(value.get("renderer_state") or (
                "ready" if value.get("renderer_available") else "dependency_missing"
            )),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise TerminalError("terminal identity is incomplete") from exc


def _validate_identity(root: Path, session_id: str) -> Tuple[Path, Dict[str, Any]]:
    _check_root_components(root, require_leaf=True)
    if not SESSION_ID_RE.fullmatch(session_id):
        raise TerminalError("invalid terminal session id")
    session_dir = root / session_id
    _private_stat(session_dir, directory=True)
    metadata = _read_metadata(session_dir / SESSION_META)
    if (metadata.get("protocol") != PROTOCOL
            or metadata.get("session_id") != session_id
            or not isinstance(metadata.get("generation"), str)
            or not metadata.get("generation")):
        raise TerminalError("terminal identity does not match the requested session")
    identity = _identity(metadata)
    if (_proc_start(identity.supervisor_pid) != identity.supervisor_start
            or not _worker_is_same_process(
                identity.supervisor_pid, identity.supervisor_start,
                session_dir, session_id, identity.generation)):
        raise TerminalError("terminal supervisor identity is stale")
    if identity.state == "running" and _proc_start(identity.shell_pid) != identity.shell_start:
        raise TerminalError("terminal shell identity is stale")
    return session_dir, metadata


class TerminalRegistry:
    """Create and reconnect to private local shell sessions.

    ``create`` is the only method that starts a process. The class does not
    auto-create shells while listing or connecting.
    """

    def __init__(self, root: Optional[os.PathLike] = None):
        self.root = Path(os.path.abspath(os.fspath(root if root is not None else _default_root())))

    def list_sessions(self) -> Tuple[TerminalIdentity, ...]:
        sessions = []
        if not os.path.lexists(self.root):
            return ()
        _check_root_components(self.root, require_leaf=True)
        try:
            entries = tuple(self.root.iterdir())
        except OSError as exc:
            raise TerminalError("cannot list terminal sessions") from exc
        for entry in entries:
            if not SESSION_ID_RE.fullmatch(entry.name):
                continue
            try:
                _private_stat(entry, directory=True)
                metadata = _read_metadata(entry / SESSION_META)
                if metadata.get("session_id") != entry.name or metadata.get("protocol") != PROTOCOL:
                    continue
                ident = _identity(metadata)
                supervisor_status = _worker_identity_status(
                    ident.supervisor_pid, ident.supervisor_start, entry,
                    ident.session_id, ident.generation,
                )
                shell_ok = _proc_start(ident.shell_pid) == ident.shell_start
                normal_exit_record = (
                    ident.state == "exited"
                    and (ident.exit_code is not None or ident.exit_signal is not None)
                )
                if supervisor_status in {"start_mismatch", "argv_mismatch"}:
                    # A reused PID or changed argv is an identity conflict, not
                    # evidence that the old shell exited. Never make it
                    # eligible for a guessed stop/reconnect.
                    state = "identity_mismatch"
                elif supervisor_status != "verified":
                    if shell_ok:
                        state = "orphaned"
                    elif normal_exit_record:
                        state = "exited"
                    else:
                        # WSL shutdown/restart can remove both processes before
                        # the supervisor writes a normal-exit record.
                        state = "orphaned"
                elif ident.state == "exited" and shell_ok:
                    state = "identity_mismatch"
                elif not shell_ok and ident.state != "exited":
                    state = "identity_mismatch"
                elif ident.state == "exited":
                    state = "exited" if normal_exit_record else "identity_mismatch"
                elif ident.state == "stopping":
                    state = "stopping"
                else:
                    state = "running" if shell_ok else "identity_mismatch"
                sessions.append(TerminalIdentity(
                    ident.session_id, ident.generation, ident.supervisor_pid,
                    ident.supervisor_start, ident.shell_pid, ident.shell_start,
                    ident.shell_path, ident.name, state, ident.exit_code,
                    ident.exit_signal, ident.termination, ident.renderer_state,
                ))
            except (TerminalError, OSError):
                # A malformed or foreign entry must never be treated as a session.
                continue
        return tuple(sorted(sessions, key=lambda item: item.session_id))

    def inspect_reference(self, session_id: str) -> str:
        """Classify a saved reference without starting, connecting, or deleting anything.

        The result contains only a stable state label; malformed metadata and
        process arguments are never surfaced to the UI or logs.
        """
        if not SESSION_ID_RE.fullmatch(str(session_id)):
            return "identity_mismatch"
        if not os.path.lexists(self.root):
            return "not_found"
        try:
            _check_root_components(self.root, require_leaf=True)
        except (OSError, TerminalError):
            return "registry_unavailable"
        session_dir = self.root / session_id
        if not os.path.lexists(session_dir):
            return "not_found"
        try:
            _private_stat(session_dir, directory=True)
            metadata = _read_metadata(session_dir / SESSION_META)
            if (metadata.get("protocol") != PROTOCOL
                    or metadata.get("session_id") != session_id):
                return "identity_mismatch"
            identity = _identity(metadata)
        except (OSError, TerminalError, TypeError, ValueError):
            return "identity_mismatch"

        worker_status = _worker_identity_status(
            identity.supervisor_pid, identity.supervisor_start, session_dir,
            identity.session_id, identity.generation,
        )
        shell_ok = _proc_start(identity.shell_pid) == identity.shell_start
        if worker_status in {"start_mismatch", "argv_mismatch"}:
            return "identity_mismatch"
        normal_exit_record = (
            identity.state == "exited"
            and (identity.exit_code is not None or identity.exit_signal is not None)
        )
        if worker_status == "verified":
            if identity.state == "exited":
                return "exited" if normal_exit_record and not shell_ok else "identity_mismatch"
            if not shell_ok:
                return "identity_mismatch"
            return identity.state if identity.state in {"running", "stopping"} else "orphaned"
        if shell_ok:
            return "orphaned"
        if normal_exit_record:
            return "exited"
        return "orphaned"

    def create(self, name: Optional[str] = None) -> "TerminalClient":
        # Fail before creating the registry/session directory or spawning a
        # supervisor. Existing sessions remain listable/connectable if the
        # renderer is later removed from the development environment.
        _require_renderer()
        _ensure_private_root(self.root)
        shell_path, _home = _validate_shell()
        display_name = _validate_name(name)
        session_id = uuid.uuid4().hex
        generation = uuid.uuid4().hex
        session_dir = self.root / session_id
        socket_path = _planned_socket_path(session_dir, session_id)
        if socket_path.parent != session_dir:
            _ensure_private_root(socket_path.parent)
        try:
            session_dir.mkdir(mode=0o700)
            os.chmod(str(session_dir), 0o700)
        except OSError as exc:
            raise TerminalError("cannot create a private terminal session") from exc
        initial = {
            "protocol": PROTOCOL,
            "session_id": session_id,
            "generation": generation,
            "supervisor_pid": 0,
            "supervisor_start": "",
            "shell_pid": 0,
            "shell_start": "",
            "shell_path": shell_path,
            "name": display_name,
            "state": "starting",
        }
        if socket_path.parent != session_dir:
            initial["socket_path"] = str(socket_path)
        try:
            _atomic_metadata(session_dir / SESSION_META, initial)
        except Exception:
            _remove_private_session(session_dir)
            raise
        args = [sys.executable, str(WORKER), str(session_dir), session_id, generation]
        try:
            process = subprocess.Popen(
                args,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                start_new_session=True,
                shell=False,
            )
        except OSError as exc:
            _remove_private_session(session_dir)
            raise TerminalError("cannot start local terminal supervisor") from exc
        deadline = time.monotonic() + SUPERVISOR_STARTUP_TIMEOUT
        while time.monotonic() < deadline:
            try:
                _, current = _validate_identity(self.root, session_id)
                if (current.get("generation") == generation
                        and current.get("state") == "running"
                        and socket_path.exists()):
                    return self.connect(session_id)
            except TerminalError:
                # Startup begins with a deliberately incomplete identity. Keep
                # waiting, but do not later report that transient stale check as
                # the actual worker startup failure.
                pass
            if process.poll() is not None:
                break
            time.sleep(0.02)
        timed_out = process.poll() is None
        exit_code = process.poll()
        startup_stage = None
        identity_status = None
        try:
            startup_metadata = _read_metadata(session_dir / SESSION_META)
            candidate_stage = startup_metadata.get("startup_stage")
            if isinstance(candidate_stage, str) and candidate_stage.isidentifier():
                startup_stage = candidate_stage
            if timed_out and startup_metadata.get("supervisor_pid") == process.pid:
                candidate_start = startup_metadata.get("supervisor_start")
                if isinstance(candidate_start, str) and candidate_start:
                    identity_status = _worker_identity_status(
                        process.pid, candidate_start, session_dir, session_id, generation
                    )
                else:
                    identity_status = "start_missing"
            elif timed_out:
                identity_status = "identity_not_published"
        except TerminalError:
            if timed_out:
                identity_status = "metadata_unavailable"
        if timed_out:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
        diagnostic = None
        if os.environ.get("JTT_TERMINAL_FIXTURE_DIAGNOSTICS") == "1":
            try:
                failure = _read_metadata(session_dir / SESSION_META).get("startup_failure")
                if isinstance(failure, dict):
                    stage = failure.get("stage")
                    exception = failure.get("exception")
                    if (isinstance(stage, str) and stage.isidentifier()
                            and isinstance(exception, str) and exception.isidentifier()):
                        diagnostic = {"stage": stage, "exception": exception}
            except TerminalError:
                pass
        _remove_private_session(session_dir)
        if diagnostic is not None:
            detail = "terminal supervisor startup failed (stage={}, exception={})".format(
                diagnostic["stage"], diagnostic["exception"]
            )
            raise TerminalError(
                detail,
                diagnostic=diagnostic,
            )
        if not timed_out:
            detail = "terminal supervisor exited during startup (exit_code={})".format(
                exit_code
            )
            if startup_stage is not None:
                detail += " (stage={})".format(startup_stage)
        elif identity_status == "argv_mismatch":
            detail = "terminal supervisor argv identity could not be verified"
        elif identity_status == "start_mismatch":
            detail = "terminal supervisor process start identity changed"
        elif identity_status == "unavailable":
            detail = "terminal supervisor process identity is unavailable"
        elif identity_status == "identity_not_published":
            detail = "terminal supervisor timed out before publishing its identity"
        elif identity_status == "metadata_unavailable":
            detail = "terminal supervisor timed out with unreadable identity metadata"
        elif startup_stage is not None:
            detail = "terminal supervisor startup timed out (stage={})".format(startup_stage)
        else:
            detail = "terminal supervisor startup timed out"
        raise TerminalError(detail)

    def connect(self, session_id: str, *, timeout: float = 3.0) -> "TerminalClient":
        session_dir, metadata = _validate_identity(self.root, session_id)
        identity = _identity(metadata)
        if identity.state not in ("running", "exited"):
            raise TerminalError("terminal session is not ready")
        sock_path = _session_socket_path(session_dir, metadata)
        if sock_path.parent != session_dir:
            _require_private_directory(sock_path.parent)
        try:
            info = sock_path.lstat()
        except OSError as exc:
            raise TerminalError("terminal control socket is unavailable") from exc
        if (not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or info.st_nlink != 1):
            raise TerminalError("terminal control socket is not private")
        if len(os.fsencode(sock_path)) > MAX_UNIX_SOCKET_PATH_BYTES:
            raise TerminalError("terminal socket path is too long for this platform")
        return TerminalClient(session_dir, identity, socket_path=sock_path, timeout=timeout)

    def stop(
        self,
        session_id: str,
        *,
        expected_generation: Optional[str] = None,
        expected_shell_pid: Optional[int] = None,
    ) -> None:
        """Stop one session, optionally requiring the UI-captured identity.

        The second validation is deliberately performed on the connected
        client's identity as well as on the registry snapshot. This prevents a
        stale card from stopping a replacement session that reused its opaque
        session id while a confirmation dialog was open.
        """

        session_dir, metadata = _validate_identity(self.root, session_id)
        ident = _identity(metadata)
        if (expected_generation is not None
                and ident.generation != str(expected_generation)):
            raise TerminalError("terminal identity changed before stop")
        if (expected_shell_pid is not None
                and ident.shell_pid != int(expected_shell_pid)):
            raise TerminalError("terminal identity changed before stop")
        client = self.connect(session_id)
        try:
            if (expected_generation is not None
                    and client.identity.generation != str(expected_generation)):
                raise TerminalError("terminal identity changed before stop")
            if (expected_shell_pid is not None
                    and client.identity.shell_pid != int(expected_shell_pid)):
                raise TerminalError("terminal identity changed before stop")
            client._request("stop")
        finally:
            client.close()
        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline:
            if not _worker_is_same_process(
                    ident.supervisor_pid, ident.supervisor_start,
                    session_dir, ident.session_id, ident.generation):
                break
            time.sleep(0.02)
        else:
            raise TerminalError("terminal supervisor did not stop cleanly")
        _remove_private_session(session_dir)


def _validate_name(name: Optional[str]) -> str:
    if name is None:
        return "Terminal"
    if not isinstance(name, str):
        raise TerminalError("terminal name must be text")
    clean = name.strip()
    if (not clean or len(clean) > 64
            or any(ord(char) < 32 or ord(char) == 127 for char in clean)):
        raise TerminalError("terminal name must be 1-64 printable characters")
    return clean


def _remove_private_session(path: Path) -> None:
    _private_stat(path, directory=True)
    try:
        for entry in tuple(path.iterdir()):
            info = entry.lstat()
            if info.st_uid != os.getuid() or info.st_nlink != 1 or stat.S_ISDIR(info.st_mode):
                raise TerminalError("refusing to remove an unexpected session artifact")
            entry.unlink()
        path.rmdir()
    except OSError as exc:
        raise TerminalError("cannot clean terminal session files") from exc


class TerminalClient:
    """A reconnectable client for one supervisor-owned PTY."""

    def __init__(self, session_dir: Path, identity: TerminalIdentity, *,
                 socket_path: Optional[Path] = None, timeout: float = 3.0):
        self._session_dir = session_dir
        self._socket_path = socket_path or (session_dir / SOCKET_NAME)
        self.identity = identity
        self.state = identity.state
        self.synced = False
        self.lease = False
        self._sequence = 0
        self._client_id = uuid.uuid4().hex
        try:
            timeout = float(timeout)
        except (TypeError, ValueError) as exc:
            raise TerminalError("terminal connection timeout is invalid") from exc
        if not 0 < timeout <= 30:
            raise TerminalError("terminal connection timeout is invalid")
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.settimeout(timeout)
        self._closed = False
        try:
            self._sock.connect(str(self._socket_path))
            _check_peer_uid(self._sock, expected_pid=identity.supervisor_pid)
            self._request("hello", client_id=self._client_id,
                          session_id=identity.session_id, generation=identity.generation,
                          supervisor_pid=identity.supervisor_pid,
                          supervisor_start=identity.supervisor_start)
        except Exception:
            self._sock.close()
            self._closed = True
            raise

    def _request(self, operation: str, **fields: Any) -> Dict[str, Any]:
        if self._closed:
            raise TerminalError("terminal client is closed")
        if operation in ("acquire", "input", "resize"):
            try:
                _session_dir, metadata = _validate_identity(
                    self._session_dir.parent, self.identity.session_id
                )
                current = _identity(metadata)
                if (current.generation != self.identity.generation
                        or current.supervisor_pid != self.identity.supervisor_pid
                        or current.supervisor_start != self.identity.supervisor_start
                        or current.shell_pid != self.identity.shell_pid
                        or current.shell_start != self.identity.shell_start):
                    self.lease = False
                    raise TerminalError("stale_identity")
            except TerminalError:
                self.lease = False
                raise
        request = {"op": operation, "client_id": self._client_id, **fields}
        frame = json.dumps(request, separators=(",", ":")).encode("utf-8") + b"\n"
        if len(frame) > MAX_REQUEST_FRAME:
            raise TerminalError("terminal request is too large")
        try:
            self._sock.sendall(frame)
            response = _read_frame(self._sock)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            self.state = "unavailable"
            self.synced = False
            self.lease = False
            raise TerminalError("terminal supervisor connection failed") from exc
        if not isinstance(response, dict) or not response.get("ok"):
            reason = response.get("error", "terminal request rejected") if isinstance(response, dict) else "invalid response"
            if reason in ("stale_identity", "session_exited", "input_lease_required", "stream_gap"):
                self.synced = False if reason in ("stale_identity", "stream_gap") else self.synced
                self.lease = False if reason in ("stale_identity", "session_exited") else self.lease
            raise TerminalError(str(reason))
        if (response.get("session_id") != self.identity.session_id
                or response.get("generation") != self.identity.generation):
            self.synced = False
            self.lease = False
            raise TerminalError("stale_identity")
        if "state" in response:
            self.state = str(response["state"])
        if "lease" in response:
            self.lease = bool(response["lease"])
        return response

    def poll(self) -> TerminalUpdate:
        response = self._request("poll", after=self._sequence)
        gap = bool(response.get("gap"))
        try:
            output = base64.b64decode(response.get("data", ""), validate=True)
        except (ValueError, TypeError) as exc:
            self.synced = False
            raise TerminalError("terminal output frame is invalid") from exc
        self._sequence = int(response.get("sequence", self._sequence))
        self.synced = bool(response.get("synced"))
        screen = _decode_screen(response)
        if screen is None and response.get("renderer_available"):
            self.synced = False
        return TerminalUpdate(
            output=output,
            sequence=self._sequence,
            state=self.state,
            synced=self.synced,
            gap=gap,
            more=bool(response.get("more")),
            columns=int(response.get("columns", 80)),
            lines=int(response.get("lines", 24)),
            snapshot_sequence=int(response.get("snapshot_sequence", 0)),
            screen=screen,
            renderer_available=bool(response.get("renderer_available")),
            renderer_state=str(response.get("renderer_state") or (
                "ready" if response.get("renderer_available") and self.synced
                else "snapshot_unavailable" if response.get("renderer_available")
                else "dependency_missing"
            )),
        )

    def observe(self) -> TerminalObservation:
        """Read an atomic bounded screen snapshot without polling output or leases."""
        _session_dir, metadata = _validate_identity(
            self._session_dir.parent, self.identity.session_id
        )
        current = _identity(metadata)
        if (current.session_id != self.identity.session_id
                or current.generation != self.identity.generation
                or current.supervisor_pid != self.identity.supervisor_pid
                or current.supervisor_start != self.identity.supervisor_start
                or current.shell_pid != self.identity.shell_pid
                or current.shell_start != self.identity.shell_start):
            raise TerminalError("stale_identity")
        response = self._request("observe")
        if (response.get("session_id") != self.identity.session_id
                or response.get("generation") != self.identity.generation
                or int(response.get("shell_pid") or 0) != self.identity.shell_pid):
            self.synced = False
            raise TerminalError("stale_identity")
        screen = _decode_screen(response) if response.get("synced") else None
        synced = bool(response.get("synced")) and screen is not None
        return TerminalObservation(
            session_id=str(response.get("session_id") or ""),
            generation=str(response.get("generation") or ""),
            shell_pid=int(response.get("shell_pid") or 0),
            sequence=int(response.get("output_sequence") or 0),
            state=str(response.get("terminal_state") or "unknown"),
            synced=synced,
            columns=int(response.get("columns") or 0),
            lines=int(response.get("lines") or 0),
            screen=screen,
            renderer_state=str(response.get("renderer_state") or "unknown"),
        )

    def acquire(self) -> bool:
        update = self.poll()
        if not update.synced:
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
        if not update.synced:
            self.release()
            raise TerminalError("stream_gap")
        if not self.lease:
            raise TerminalError("input_lease_required")
        response = self._request("input", data=base64.b64encode(data).decode("ascii"),
                                 after=self._sequence)
        return int(response.get("written", 0))

    def resize(self, columns: int, lines: int) -> None:
        if (isinstance(columns, bool) or isinstance(lines, bool)
                or not isinstance(columns, int) or not isinstance(lines, int)
                or not 20 <= columns <= 300 or not 5 <= lines <= 120):
            raise TerminalError("terminal size is outside the supported range")
        if not self.lease:
            raise TerminalError("input_lease_required")
        update = self.poll()
        if not update.synced:
            self.release()
            raise TerminalError("stream_gap")
        self._request("resize", columns=columns, lines=lines,
                      after=self._sequence)

    def close(self) -> None:
        if self._closed:
            return
        if self.lease:
            try:
                self._request("release")
            except TerminalError:
                pass
        self._closed = True
        self.lease = False
        self.synced = False
        try:
            self._sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self._sock.close()

    def __enter__(self) -> "TerminalClient":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def _check_peer_uid(sock: socket.socket, expected_pid: Optional[int] = None) -> None:
    if not hasattr(socket, "SO_PEERCRED"):
        raise TerminalError("peer credential validation is unavailable")
    try:
        pid, uid, _gid = struct.unpack(
            "3i", sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
        )
    except OSError as exc:
        raise TerminalError("cannot validate terminal supervisor peer") from exc
    if uid != os.getuid():
        raise TerminalError("terminal supervisor belongs to another user")
    if expected_pid is not None and pid != expected_pid:
        raise TerminalError("terminal supervisor process identity does not match")


def _read_frame(sock: socket.socket) -> Dict[str, Any]:
    data = bytearray()
    while len(data) <= MAX_FRAME:
        chunk = sock.recv(min(4096, MAX_FRAME + 1 - len(data)))
        if not chunk:
            raise OSError("terminal peer closed the connection")
        newline = chunk.find(b"\n")
        if newline >= 0:
            data.extend(chunk[:newline])
            return json.loads(data.decode("utf-8"))
        data.extend(chunk)
    raise ValueError("terminal frame exceeds the allowed size")


def _decode_screen(response: dict) -> Optional[TerminalScreen]:
    encoded = response.get("screen_z")
    if not isinstance(encoded, str):
        return None
    try:
        packed = base64.b64decode(encoded, validate=True)
        decompressor = zlib.decompressobj()
        raw = decompressor.decompress(packed, 4 * 1024 * 1024 + 1)
        if len(raw) > 4 * 1024 * 1024 or not decompressor.eof:
            return None
        value = json.loads(raw.decode("utf-8"))
        if not isinstance(value, dict) or not isinstance(value.get("rows"), list):
            return None
        rows = tuple(
            tuple(TerminalCell(
                text=str(cell[0]), foreground=str(cell[1]), background=str(cell[2]),
                bold=bool(cell[3]), italic=bool(cell[4]), underline=bool(cell[5]),
                reverse=bool(cell[6]), blink=bool(cell[7]), strikethrough=bool(cell[8]),
            ) for cell in row)
            for row in value["rows"]
        )
        return TerminalScreen(
            rows=rows, cursor_x=int(value["cursor_x"]), cursor_y=int(value["cursor_y"]),
            cursor_visible=bool(value["cursor_visible"]),
        )
    except (ValueError, TypeError, KeyError, IndexError, zlib.error,
            UnicodeDecodeError, json.JSONDecodeError):
        return None
