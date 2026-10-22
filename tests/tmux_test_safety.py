"""Fail-closed tmux harness for tests.

Every server command is pinned to a unique socket below a private test-only
directory. The PATH shim also pins product-code subprocesses which invoke the
``tmux`` executable without an explicit socket argument.
"""

from __future__ import annotations

import os
import shutil
import socket as socket_module
import stat
import subprocess
import tempfile
import time
from pathlib import Path


class TmuxSafetyError(RuntimeError):
    """A test tried to control a tmux server outside its private sandbox."""


_MARKER = ".jtt-isolated-tmux-test"
_MARKER_CONTENT = b"JTT isolated tmux test root v1\n"
_DESTRUCTIVE = {"kill-server", "kill-session", "kill-window", "kill-pane", "kill-client"}


def _is_beneath(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return path != root
    except ValueError:
        return False


def _validate_root(root: Path) -> None:
    root = Path(root)
    if not root.is_absolute() or root.parent.resolve() != Path("/tmp").resolve():
        raise TmuxSafetyError("refused tmux: test root is not a direct child of /tmp")
    if not root.name.startswith("jtt-tmux-test-") or root.resolve() != root:
        raise TmuxSafetyError("refused tmux: test root identity is invalid")
    try:
        root_stat = root.lstat()
        marker_stat = (root / _MARKER).lstat()
    except OSError as exc:
        raise TmuxSafetyError("refused tmux: test root marker is unavailable") from exc
    if not stat.S_ISDIR(root_stat.st_mode) or root_stat.st_uid != os.getuid():
        raise TmuxSafetyError("refused tmux: test root is not owned by this user")
    if stat.S_IMODE(root_stat.st_mode) & 0o077:
        raise TmuxSafetyError("refused tmux: test root permissions are not private")
    if (not stat.S_ISREG(marker_stat.st_mode) or marker_stat.st_uid != os.getuid()
            or stat.S_IMODE(marker_stat.st_mode) & 0o077
            or (root / _MARKER).read_bytes() != _MARKER_CONTENT):
        raise TmuxSafetyError("refused tmux: test root marker is invalid")


def _validate_socket_path(path: Path, root: Path, *, must_exist: bool) -> Path:
    root = root.resolve()
    lexical = Path(os.path.abspath(str(path)))
    resolved = lexical.resolve(strict=False)
    if not _is_beneath(resolved, root):
        raise TmuxSafetyError("refused tmux: socket path is outside the test root")
    if lexical != resolved:
        raise TmuxSafetyError("refused tmux: socket path resolves through a symlink")
    parent = lexical.parent
    while parent != root:
        try:
            item = parent.lstat()
        except FileNotFoundError:
            if must_exist:
                raise TmuxSafetyError("refused tmux: socket directory is missing")
            parent = parent.parent
            continue
        if (not stat.S_ISDIR(item.st_mode) or item.st_uid != os.getuid()
                or stat.S_IMODE(item.st_mode) & 0o077):
            raise TmuxSafetyError("refused tmux: socket directory is not private and owned")
        parent = parent.parent
    try:
        item = lexical.lstat()
    except FileNotFoundError:
        if must_exist:
            raise TmuxSafetyError("refused tmux: expected test socket is missing")
        return lexical
    if (not stat.S_ISSOCK(item.st_mode) or item.st_uid != os.getuid()
            or stat.S_IMODE(item.st_mode) & 0o022):
        raise TmuxSafetyError("refused tmux: socket type or owner is invalid")
    return lexical


def _process_identity(pid: int):
    """Return (uid, Linux start ticks), without reading command contents."""
    status = Path("/proc") / str(pid) / "status"
    stat_path = Path("/proc") / str(pid) / "stat"
    try:
        uid_line = next(line for line in status.read_text(encoding="ascii").splitlines()
                        if line.startswith("Uid:"))
        uid = int(uid_line.split()[1])
        raw = stat_path.read_text(encoding="ascii")
        fields = raw[raw.rfind(")") + 2:].split()
        state = fields[0]
        start_ticks = int(fields[19])
    except (OSError, StopIteration, IndexError, ValueError) as exc:
        raise TmuxSafetyError("refused tmux shutdown: server process identity is unavailable") from exc
    return uid, start_ticks, state


def _environment(source=None):
    env = dict(os.environ if source is None else source)
    env.pop("TMUX", None)
    env.pop("TMUX_PANE", None)
    return env


def _server_pid(binary: str, socket_path: Path, root: Path, *, env=None) -> int:
    _validate_root(root)
    _validate_socket_path(socket_path, root, must_exist=True)
    result = subprocess.run(
        [binary, "-S", str(socket_path), "-f", "/dev/null", "display-message", "-p", "#{pid}"],
        capture_output=True, text=True, check=False, timeout=5, env=_environment(env),
    )
    if result.returncode != 0 or not result.stdout.strip().isdecimal():
        raise TmuxSafetyError("refused tmux shutdown: isolated server did not identify itself")
    pid = int(result.stdout.strip())
    uid, _start, _state = _process_identity(pid)
    if uid != os.getuid():
        raise TmuxSafetyError("refused tmux shutdown: server is owned by another user")
    return pid


def _socket_path_is_listening(socket_path: Path) -> bool:
    """Check the exact Unix socket path without contacting another server."""
    try:
        rows = Path("/proc/net/unix").read_text(encoding="ascii").splitlines()[1:]
    except OSError as exc:
        raise TmuxSafetyError("refused tmux cleanup: Unix socket table is unavailable") from exc
    expected = str(socket_path)
    return any(row.split()[-1:] == [expected] for row in rows)


def _safe_stop_socket(binary: str, socket_path: Path, root: Path):
    """Stop only a verified server on a verified socket below the test root."""
    _validate_root(root)
    try:
        _validate_socket_path(socket_path, root, must_exist=True)
    except TmuxSafetyError:
        try:
            socket_path.lstat()
        except FileNotFoundError:
            if not _socket_path_is_listening(socket_path):
                return None
        raise
    try:
        pid = _server_pid(binary, socket_path, root)
    except TmuxSafetyError:
        # A final kill-session can make tmux exit before fixture teardown. Only
        # unlink the resulting stale node when the kernel confirms that this
        # exact private path is no longer bound. If /proc cannot prove that,
        # preserve the sandbox and fail closed.
        if _socket_path_is_listening(socket_path):
            raise TmuxSafetyError("refused tmux cleanup: private socket is still listening")
        _validate_socket_path(socket_path, root, must_exist=True)
        socket_path.unlink()
        return None
    before = _process_identity(pid)
    result = subprocess.run(
        [binary, "-S", str(socket_path), "-f", "/dev/null", "kill-server"],
        capture_output=True, text=True, timeout=10, check=False,
        env={**_environment(), "TMUX_TMPDIR": str(root)},
    )
    if result.returncode != 0:
        raise TmuxSafetyError("isolated tmux server shutdown failed; sandbox retained")
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        try:
            current = _process_identity(pid)
        except TmuxSafetyError:
            current = None
        if current is None or current[2] == "Z" or current != before:
            break
        time.sleep(0.02)
    else:
        raise TmuxSafetyError("isolated tmux server is still alive; sandbox retained")
    try:
        socket_path.lstat()
    except FileNotFoundError:
        pass
    else:
        _validate_socket_path(socket_path, root, must_exist=True)
        socket_path.unlink()
    return result


def _remove_socket_after_verified_server_exit(socket_path: Path, root: Path, pid: int, before):
    """Remove a stale filesystem socket only after its known server is gone."""
    try:
        _validate_socket_path(socket_path, root, must_exist=True)
    except TmuxSafetyError:
        try:
            socket_path.lstat()
        except FileNotFoundError:
            if not _socket_path_is_listening(socket_path):
                return
        raise
    try:
        current = _process_identity(pid)
    except TmuxSafetyError:
        current = None
    if current is not None and current[2] != "Z" and current == before:
        raise TmuxSafetyError("refused socket cleanup: tmux server process is still alive")
    socket_path.unlink()


def run_isolated_tmux(*args, timeout=10, check=False, capture_output=True,
                      text=True, env=None, _root=None, _socket_path=None,
                      _binary=None):
    """Run tmux with a validated absolute ``-S`` path from the active fixture."""
    root_value = str(_root) if _root is not None else os.environ.get("JTT_TEST_TMUX_ROOT", "")
    socket_value = (str(_socket_path) if _socket_path is not None
                    else os.environ.get("JTT_TEST_TMUX_SOCKET", ""))
    binary = _binary or os.environ.get("JTT_TEST_TMUX_BINARY", "")
    if not root_value or not socket_value or not binary:
        raise TmuxSafetyError("refused tmux: no active isolated test fixture")
    root = Path(root_value)
    socket_path = Path(socket_value)
    _validate_root(root)
    if socket_path != root / "server.sock":
        raise TmuxSafetyError("refused tmux: fixture socket identity is invalid")
    if args and (args[0] in ("-S", "-L", "-f") or args[0].startswith(("-S", "-L", "-f"))):
        raise TmuxSafetyError("refused tmux: nested socket selection is forbidden")
    command = next((arg for arg in args if not arg.startswith("-")), "")
    if socket_path.exists():
        _validate_socket_path(socket_path, root, must_exist=True)
    if command == "kill-server":
        if not socket_path.exists():
            raise TmuxSafetyError("refused tmux shutdown: expected test socket is missing")
        return _safe_stop_socket(binary, socket_path, root)
    server_identity = None
    server_pid = None
    if command in _DESTRUCTIVE:
        server_pid = _server_pid(binary, socket_path, root, env=env)
        server_identity = _process_identity(server_pid)
    child_env = _environment(env)
    child_env["TMUX_TMPDIR"] = str(root)
    result = subprocess.run(
        [binary, "-S", str(socket_path), "-f", "/dev/null", *args],
        timeout=timeout, check=check, capture_output=capture_output, text=text,
        env=child_env,
    )
    if socket_path.exists():
        _validate_socket_path(socket_path, root, must_exist=True)
        try:
            after_pid = _server_pid(binary, socket_path, root, env=child_env)
        except TmuxSafetyError:
            if server_pid is None or server_identity is None:
                raise
            _remove_socket_after_verified_server_exit(
                socket_path, root, server_pid, server_identity,
            )
        else:
            if server_pid is not None and after_pid != server_pid:
                raise TmuxSafetyError("refused tmux: isolated socket server identity changed")
    return result


class IsolatedTmuxTestRoot:
    def __init__(self, binary):
        if not binary:
            raise TmuxSafetyError("tmux executable is unavailable")
        self.binary = str(Path(binary).resolve())
        self.root = Path(tempfile.mkdtemp(prefix="jtt-tmux-test-", dir="/tmp"))
        self.root.chmod(0o700)
        self.socket_path = self.root / "server.sock"
        self.marker = self.root / _MARKER
        fd = os.open(str(self.marker), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            os.write(fd, _MARKER_CONTENT)
        finally:
            os.close(fd)
        self.bin_dir = self.root / "bin"
        self.bin_dir.mkdir(mode=0o700)
        self.shim = self.bin_dir / "tmux"
        self.shim.write_text(self._shim_source(), encoding="utf-8")
        self.shim.chmod(0o700)
        _validate_root(self.root)

    def _shim_source(self):
        return "\n".join((
            "#!" + os.sys.executable,
            "import os, stat, subprocess, sys",
            "from pathlib import Path",
            "REAL = " + repr(self.binary),
            "ROOT = Path(" + repr(str(self.root)) + ")",
            "MARKER = " + repr(_MARKER),
            "MARKER_CONTENT = " + repr(_MARKER_CONTENT),
            "UID = os.geteuid()",
            "def fail(message):",
            "    sys.stderr.write(message + '\\n')",
            "    raise SystemExit(125)",
            "def under(path, root):",
            "    try: path.relative_to(root); return path != root",
            "    except ValueError: return False",
            "def validate_root():",
            "    try:",
            "        st = ROOT.lstat(); marker = (ROOT / MARKER).lstat()",
            "        valid = (stat.S_ISDIR(st.st_mode) and st.st_uid == UID and not stat.S_IMODE(st.st_mode) & 0o077 and stat.S_ISREG(marker.st_mode) and marker.st_uid == UID and not stat.S_IMODE(marker.st_mode) & 0o077 and (ROOT / MARKER).read_bytes() == MARKER_CONTENT)",
            "    except OSError: valid = False",
            "    if not valid: fail('refused tmux: private test root validation failed')",
            "def socket_path(raw, kind):",
            "    if kind == 'L':",
            "        if not raw or '/' in raw or raw in ('.', '..'): fail('refused tmux: invalid test socket name')",
            "        directory = ROOT / ('tmux-' + str(UID))",
            "        try: directory.mkdir(mode=0o700, exist_ok=True)",
            "        except OSError: fail('refused tmux: private named-socket directory cannot be created')",
            "        dst = directory.lstat()",
            "        if not stat.S_ISDIR(dst.st_mode) or dst.st_uid != UID or stat.S_IMODE(dst.st_mode) & 0o077: fail('refused tmux: named-socket directory is not private')",
            "        path = directory / raw",
            "    else: path = Path(raw)",
            "    if not path.is_absolute(): fail('refused tmux: socket must be absolute')",
            "    resolved = path.resolve(strict=False)",
            "    if not under(resolved, ROOT) or resolved != path: fail('refused tmux: socket is outside private test root')",
            "    parent = path.parent",
            "    while parent != ROOT:",
            "        if parent.exists():",
            "            try: st = parent.lstat()",
            "            except OSError: fail('refused tmux: socket directory cannot be verified')",
            "            if not stat.S_ISDIR(st.st_mode) or st.st_uid != UID or stat.S_IMODE(st.st_mode) & 0o077: fail('refused tmux: socket directory is not private')",
            "        parent = parent.parent",
            "    return path",
            "def validate_socket(path, required=False):",
            "    if not path.exists():",
            "        if required: fail('refused tmux: test socket is missing')",
            "        return False",
            "    try: st = path.lstat()",
            "    except OSError: fail('refused tmux: socket cannot be verified')",
            "    if not stat.S_ISSOCK(st.st_mode) or st.st_uid != UID or stat.S_IMODE(st.st_mode) & 0o022: fail('refused tmux: socket owner or type is invalid')",
            "    return True",
            "def validate_config(raw):",
            "    if raw == '/dev/null': return",
            "    path = Path(raw); resolved = path.resolve(strict=False)",
            "    if not under(resolved, ROOT) or resolved != path: fail('refused tmux: config is outside private test root')",
            "    try: st = path.lstat(); content = path.read_text(encoding='utf-8')",
            "    except OSError: fail('refused tmux: test config cannot be verified')",
            "    if not stat.S_ISREG(st.st_mode) or st.st_uid != UID or stat.S_IMODE(st.st_mode) & 0o077: fail('refused tmux: test config is not private')",
            "    for line in content.splitlines():",
            "        stripped = line.strip()",
            "        if stripped and not stripped.startswith('#') and stripped.split(None, 1)[0] not in ('bind-key', 'unbind-key'): fail('refused tmux: test config contains a non-binding command')",
            "def process_identity(pid):",
            "    status = Path('/proc') / str(pid) / 'status'; raw = (Path('/proc') / str(pid) / 'stat').read_text(encoding='ascii')",
            "    uid_line = next(line for line in status.read_text(encoding='ascii').splitlines() if line.startswith('Uid:'))",
            "    fields = raw[raw.rfind(')') + 2:].split()",
            "    return int(uid_line.split()[1]), int(fields[19]), fields[0]",
            "def safe_stop(path):",
            "    validate_socket(path, required=True)",
            "    probe = subprocess.run([REAL, '-S', str(path), '-f', '/dev/null', 'display-message', '-p', '#{pid}'], capture_output=True, text=True, timeout=5, env=clear_env(), check=False)",
            "    if probe.returncode or not probe.stdout.strip().isdecimal(): fail('refused tmux shutdown: isolated server identity unavailable')",
            "    pid = int(probe.stdout.strip())",
            "    try: before = process_identity(pid)",
            "    except (OSError, StopIteration, ValueError, IndexError): fail('refused tmux shutdown: server identity is unverifiable')",
            "    if before[0] != UID: fail('refused tmux shutdown: server owner mismatch')",
            "    result = subprocess.run([REAL, '-S', str(path), '-f', '/dev/null', 'kill-server'], capture_output=True, text=False, timeout=10, env=clear_env(), check=False)",
            "    if result.returncode: sys.stderr.buffer.write(result.stderr); raise SystemExit(result.returncode)",
            "    deadline = __import__('time').monotonic() + 3",
            "    while __import__('time').monotonic() < deadline:",
            "        try: current = process_identity(pid)",
            "        except (OSError, StopIteration, ValueError, IndexError): current = None",
            "        if current is None or current[2] == 'Z' or current != before: break",
            "        __import__('time').sleep(0.02)",
            "    else: fail('isolated tmux server is still alive; sandbox retained')",
            "    if path.exists(): validate_socket(path, required=True); path.unlink()",
            "    sys.stdout.buffer.write(result.stdout); sys.stderr.buffer.write(result.stderr); raise SystemExit(0)",
            "def clear_env():",
            "    env = os.environ.copy(); env.pop('TMUX', None); env.pop('TMUX_PANE', None); env['TMUX_TMPDIR'] = str(ROOT); return env",
            "args = sys.argv[1:]",
            "if args == ['-V']:",
            "    os.execve(REAL, [REAL, '-V'], clear_env())",
            "validate_root()",
            "kind = None; raw = None; config = None; prefix = []; index = 0",
            "while index < len(args):",
            "    item = args[index]",
            "    if item in ('-S', '-L', '-f'):",
            "        if index + 1 >= len(args): fail('refused tmux: incomplete global option')",
            "        value = args[index + 1]",
            "        if item == '-f': config = value",
            "        else:",
            "            if kind is not None: fail('refused tmux: multiple socket selectors')",
            "            kind, raw = item[1], value",
            "        index += 2; continue",
            "    if item.startswith('-S') and len(item) > 2:",
            "        if kind is not None: fail('refused tmux: multiple socket selectors')",
            "        kind, raw = 'S', item[2:]; index += 1; continue",
            "    if item.startswith('-L') and len(item) > 2:",
            "        if kind is not None: fail('refused tmux: multiple socket selectors')",
            "        kind, raw = 'L', item[2:]; index += 1; continue",
            "    if item.startswith('-f') and len(item) > 2:",
            "        config = item[2:]; index += 1; continue",
            "    if item.startswith('-'):",
            "        prefix.append(item); index += 1; continue",
            "    break",
            "if config: validate_config(config)",
            "path = socket_path(raw, kind) if kind else ROOT / 'server.sock'",
            "if path == ROOT / 'server.sock' and os.environ.get('JTT_TEST_TMUX_SOCKET') != str(path): fail('refused tmux: fixture socket binding is invalid')",
            "if validate_socket(path): pass",
            "tail = args[index:]",
            "command = tail[0] if tail else ''",
            "if command == 'kill-server': safe_stop(path)",
            "if command in " + repr(sorted(_DESTRUCTIVE - {'kill-server'})) + ":",
            "    if not validate_socket(path, required=True): fail('refused tmux shutdown: no isolated socket')",
            "    probe = subprocess.run([REAL, '-S', str(path), '-f', '/dev/null', 'display-message', '-p', '#{pid}'], capture_output=True, text=True, timeout=5, env=clear_env(), check=False)",
            "    if probe.returncode or not probe.stdout.strip().isdecimal(): fail('refused tmux shutdown: isolated server identity unavailable')",
            "    try:",
            "        pid = int(probe.stdout.strip()); status = Path('/proc') / str(pid) / 'status'",
            "        uid_line = next(line for line in status.read_text(encoding='ascii').splitlines() if line.startswith('Uid:'))",
            "        if int(uid_line.split()[1]) != UID: fail('refused tmux shutdown: server owner mismatch')",
            "    except (OSError, StopIteration, ValueError, IndexError): fail('refused tmux shutdown: server identity is unverifiable')",
            "argv = [REAL, '-S', str(path), '-f', (config or '/dev/null'), *prefix, *tail]",
            "result = subprocess.run(argv, capture_output=True, text=False, env=clear_env())",
            "sys.stdout.buffer.write(result.stdout)",
            "sys.stderr.buffer.write(result.stderr)",
            "if path.exists(): validate_socket(path, required=True)",
            "raise SystemExit(result.returncode)",
            "",
        ))

    def install(self, monkeypatch):
        _validate_root(self.root)
        monkeypatch.delenv("TMUX", raising=False)
        monkeypatch.delenv("TMUX_PANE", raising=False)
        monkeypatch.setenv("TMUX_TMPDIR", str(self.root))
        monkeypatch.setenv("JTT_TEST_TMUX_ROOT", str(self.root))
        monkeypatch.setenv("JTT_TEST_TMUX_SOCKET", str(self.socket_path))
        monkeypatch.setenv("JTT_TEST_TMUX_BINARY", self.binary)
        old_path = os.environ.get("PATH", os.defpath)
        monkeypatch.setenv("PATH", str(self.bin_dir) + os.pathsep + old_path)

    def run(self, *args, **kwargs):
        return run_isolated_tmux(
            *args, _root=self.root, _socket_path=self.socket_path,
            _binary=self.binary, **kwargs,
        )

    def _socket_paths(self):
        paths = []
        try:
            self.socket_path.lstat()
        except FileNotFoundError:
            pass
        else:
            paths.append(self.socket_path)
        socket_dir = self.root / ("tmux-" + str(os.getuid()))
        try:
            item = socket_dir.lstat()
        except FileNotFoundError:
            return paths
        if (not stat.S_ISDIR(item.st_mode) or item.st_uid != os.getuid()
                or stat.S_IMODE(item.st_mode) & 0o077):
            raise TmuxSafetyError("refused tmux cleanup: named-socket directory is not private")
        try:
            entries = tuple(socket_dir.iterdir())
        except FileNotFoundError:
            return paths
        paths.extend(entries)
        return paths

    def close(self):
        _validate_root(self.root)
        for path in self._socket_paths():
            _safe_stop_socket(self.binary, path, self.root)
        leftovers = self._socket_paths()
        if leftovers:
            raise TmuxSafetyError("isolated tmux sockets remain; sandbox retained")
        socket_dir = self.root / ("tmux-" + str(os.getuid()))
        try:
            item = socket_dir.lstat()
        except FileNotFoundError:
            item = None
        if item is not None:
            if (not stat.S_ISDIR(item.st_mode) or item.st_uid != os.getuid()
                    or stat.S_IMODE(item.st_mode) & 0o077):
                raise TmuxSafetyError("refused tmux cleanup: named-socket directory changed")
            try:
                socket_dir.rmdir()
            except FileNotFoundError:
                pass
        shim_stat = self.shim.lstat()
        marker_stat = self.marker.lstat()
        if shim_stat.st_uid != os.getuid() or marker_stat.st_uid != os.getuid():
            raise TmuxSafetyError("test artifacts changed owner; sandbox retained")
        self.shim.unlink()
        self.bin_dir.rmdir()
        self.marker.unlink()
        self.root.rmdir()


def create_isolated_tmux_test_root(monkeypatch):
    binary = shutil.which("tmux")
    if not binary:
        monkeypatch.delenv("TMUX", raising=False)
        monkeypatch.delenv("TMUX_PANE", raising=False)
        return None
    guard = IsolatedTmuxTestRoot(binary)
    guard.install(monkeypatch)
    return guard
