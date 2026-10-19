"""PTY owner process for :mod:`tmux_agent_tower.terminal`.

This module is started only by an explicit TerminalRegistry.create() call.
Its command line accepts a private directory and opaque IDs, never a program
name or shell command. The account shell is read from the local passwd entry.
"""

from __future__ import annotations

import base64
import codecs
import ctypes
import errno
import fcntl
import json
import os
from pathlib import Path
import pty
import selectors
import signal
import socket
import stat
import struct
import subprocess
import sys
import termios
import time
from typing import Optional
import uuid
import zlib


if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tmux_agent_tower.terminal.manager import (  # noqa: E402
    MAX_FRAME,
    MAX_REQUEST_FRAME,
    MAX_UNIX_SOCKET_PATH_BYTES,
    PROTOCOL,
    SESSION_ID_RE,
    SESSION_META,
    SOCKET_NAME,
    TerminalError,
    _atomic_metadata,
    _private_stat,
    _proc_start,
    _read_metadata,
    _validate_shell,
)

try:
    import pyte
except ImportError as exc:
    pyte = None
    _PYTE_IMPORT_STATE = "dependency_missing" if exc.name == "pyte" else "import_failed"
except Exception:
    pyte = None
    _PYTE_IMPORT_STATE = "import_failed"
else:
    _PYTE_IMPORT_STATE = "ready"


if pyte is not None:
    class _JttScreen(pyte.Screen):
        """Screen adapter for xterm's private SGR extension marker.

        pyte's parser dispatches CSI ``? ... m`` with ``private=True`` but its
        Screen handler only accepts numeric attributes. Private SGR has no
        portable rendition semantics, so ignore that extension instead of
        taking the otherwise healthy terminal offline.
        """

        def select_graphic_rendition(self, *attrs: int, private: bool = False) -> None:
            if not private:
                super().select_graphic_rendition(*attrs)
else:
    _JttScreen = None


MAX_OUTPUT_EVENTS = 128
MAX_OUTPUT_BYTES = 1024 * 1024
READ_CHUNK = 8192
POLL_BYTES = 48 * 1024
MAX_SCREEN_B64 = 768 * 1024
MAX_CLIENTS = 8
LEASE_SECONDS = 8.0
DEFAULT_COLUMNS = 80
DEFAULT_LINES = 24
MAX_COLUMNS = 300
MAX_LINES = 120


def _peer_uid(conn: socket.socket) -> int:
    if not hasattr(socket, "SO_PEERCRED"):
        raise TerminalError("peer credentials unavailable")
    _pid, uid, _gid = struct.unpack(
        "3i", conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
    )
    if uid != os.getuid():
        raise TerminalError("peer uid mismatch")
    return uid


def _process_start_or_fail(pid: int) -> str:
    value = _proc_start(pid)
    if not value:
        raise TerminalError("cannot identify process start")
    return value


def _session_processes(session_id: int) -> dict[int, str]:
    """Return live ``/proc`` members of one PTY-owned process session.

    A shell can place a background job in another process group without
    leaving its session. Signalling only the shell's process group therefore
    leaks that job on explicit terminal stop. The PTY child is started as a
    fresh session leader, so its session ID is the scope boundary here.
    """

    members = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            fields = (entry / "stat").read_text(encoding="ascii").rsplit(")", 1)[1].split()
            if fields[0] in ("Z", "X") or int(fields[3]) != session_id:
                continue
            members[int(entry.name)] = fields[19]
        except (OSError, IndexError, ValueError):
            # Processes can exit while /proc is enumerated. The next scan
            # observes the stable set; malformed/unreadable entries are not
            # signalled.
            continue
    return members


def _process_start_in_session(pid: int, session_id: int) -> Optional[str]:
    try:
        fields = Path("/proc/{}/stat".format(pid)).read_text(
            encoding="ascii"
        ).rsplit(")", 1)[1].split()
        if fields[0] in ("Z", "X") or int(fields[3]) != session_id:
            return None
        return fields[19]
    except (OSError, IndexError, ValueError):
        return None


def _signal_session_processes(session_id: int, signum: int,
                              already_signalled: Optional[set[tuple[int, str]]] = None
                              ) -> set[tuple[int, str]]:
    """Signal only still-matching process identities in the owned session."""

    sent = already_signalled if already_signalled is not None else set()
    for pid, start in _session_processes(session_id).items():
        identity = (pid, start)
        if identity in sent:
            continue
        # Re-read immediately before signalling so an exited PID reused by an
        # unrelated process is not acted upon.
        pidfd_open = getattr(os, "pidfd_open", None)
        pidfd_send_signal = getattr(signal, "pidfd_send_signal", None)
        pidfd = None
        if pidfd_open is not None and pidfd_send_signal is not None:
            try:
                pidfd = pidfd_open(pid, 0)
            except ProcessLookupError:
                continue
            except OSError as exc:
                # Python may expose the API while the running kernel or a
                # seccomp profile denies the syscall. Use the documented
                # start-time/session recheck fallback for those cases.
                if exc.errno not in (errno.ENOSYS, errno.EINVAL, errno.EPERM):
                    raise
            else:
                try:
                    if _process_start_in_session(pid, session_id) != start:
                        continue
                    try:
                        pidfd_send_signal(pidfd, signum)
                    except ProcessLookupError:
                        continue
                    except OSError as exc:
                        if exc.errno not in (errno.ENOSYS, errno.EINVAL, errno.EPERM):
                            raise
                        # Some kernels expose pidfd_open but not pidfd_send_signal.
                        # Close this descriptor and use the checked kill(2) path.
                    else:
                        sent.add(identity)
                        continue
                finally:
                    os.close(pidfd)

        # Older Linux/Python combinations without pidfd support still recheck
        # both session membership and process start time immediately before
        # kill(2); pidfd is preferred because it closes the PID-reuse race.
        if _process_start_in_session(pid, session_id) != start:
            continue
        try:
            os.kill(pid, signum)
        except ProcessLookupError:
            continue
        sent.add(identity)
    return sent


def _safe_child_setup(slave_fd: int) -> None:
    """Run between fork and exec, establishing the PTY controlling TTY."""

    os.umask(0o077)
    os.setsid()
    fcntl.ioctl(slave_fd, termios.TIOCSCTTY, 0)
    # If this owner process crashes, ask the shell leader to terminate.
    try:
        if ctypes.CDLL(None).prctl(1, signal.SIGTERM) != 0:
            os._exit(125)
    except Exception:
        os._exit(125)


def _working_directory(current_home: str, passwd_home: str) -> str:
    """Use a valid user-owned HOME, otherwise the already-validated passwd home."""

    if current_home and os.path.isabs(current_home):
        try:
            candidate = Path(current_home).resolve(strict=True)
            info = candidate.stat()
            if stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid():
                return str(candidate)
        except OSError:
            pass
    return passwd_home


def _cell_json(cell):
    return [
        cell.data,
        cell.fg,
        cell.bg,
        int(bool(cell.bold)),
        int(bool(cell.italics)),
        int(bool(cell.underscore)),
        int(bool(cell.reverse)),
        int(bool(cell.blink)),
        int(bool(cell.strikethrough)),
    ]


class PtyOwner:
    def __init__(self, session_dir: Path, session_id: str, generation: str):
        self.startup_stage = "session_validation"
        _private_stat(session_dir, directory=True)
        if not SESSION_ID_RE.fullmatch(session_id):
            raise TerminalError("invalid session id")
        self.session_dir = session_dir
        self.session_id = session_id
        self.generation = generation
        self.metadata_path = session_dir / SESSION_META
        self.metadata = _read_metadata(self.metadata_path)
        if (self.metadata.get("session_id") != session_id
                or self.metadata.get("generation") != generation
                or self.metadata.get("protocol") != PROTOCOL
                or self.metadata.get("state") != "starting"):
            raise TerminalError("session launch identity mismatch")
        shell_path, account_home = _validate_shell()
        if self.metadata.get("shell_path") != shell_path:
            raise TerminalError("account shell changed during terminal startup")
        self.account_home = account_home

        self.columns = DEFAULT_COLUMNS
        self.lines = DEFAULT_LINES
        self.sequence = 0
        self.output = []
        self.output_size = 0
        self.clients = {}
        self.lease_owner = None
        self.lease_deadline = 0.0
        self.stopping = False
        self.child = None
        self.master = None
        self.listener = None
        self.socket_owned = False
        self.selector = selectors.DefaultSelector()
        self.screen = None
        self.stream = None
        self.screen_error = False
        self.renderer_state = _PYTE_IMPORT_STATE
        self.snapshot_synced = False
        self.decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self.supervisor_start = _process_start_or_fail(os.getpid())
        self.shell_start = ""
        self.shell_path = shell_path
        self.name = str(self.metadata.get("name", "Terminal"))
        self.created_at = int(time.time())
        self._store_state("starting", startup_stage="renderer_init")
        self._open_vt()
        try:
            self.startup_stage = "pty_shell_start"
            self._store_state("starting", startup_stage=self.startup_stage)
            self._start_shell()
            self.startup_stage = "socket_start"
            self._store_state("starting", startup_stage=self.startup_stage)
            self._start_socket()
            self.startup_stage = "ready"
            self._store_state("running")
        except Exception:
            self._shutdown()
            raise

    @property
    def renderer_available(self) -> bool:
        return (self.screen is not None and not self.screen_error
                and self.renderer_state not in {
                    "dependency_missing", "import_failed", "initialization_failed", "parse_failed",
                })

    def _open_vt(self) -> None:
        if pyte is None:
            self.renderer_state = _PYTE_IMPORT_STATE
            raise TerminalError("renderer_" + self.renderer_state)
        try:
            self.screen = _JttScreen(self.columns, self.lines)
            self.stream = pyte.Stream(self.screen)
            self.screen.write_process_input = self._terminal_reply
            self.stream.feed("\x1b[0m")
            if len(self.screen.display) != self.lines:
                raise ValueError("VT screen shape mismatch")
            self.renderer_state = "ready"
        except Exception as exc:
            self.screen = None
            self.stream = None
            self.screen_error = True
            self.renderer_state = "initialization_failed"
            raise TerminalError("renderer_initialization_failed") from exc

    def _start_shell(self) -> None:
        try:
            self.master, slave = pty.openpty()
            fcntl.ioctl(slave, termios.TIOCSWINSZ,
                        struct.pack("HHHH", self.lines, self.columns, 0, 0))
            env = dict(os.environ)
            # Do not let JTT nest into an outer tmux merely because Tower itself
            # happened to be started from a tmux client.
            env.pop("TMUX", None)
            env.pop("TMUX_PANE", None)
            env.pop("JTT_SESSION_ID", None)
            working_directory = _working_directory(env.get("HOME", ""), self.account_home)
            env["HOME"] = working_directory
            # The current curses renderer targets the xterm baseline. Do not
            # inherit a TERM value that advertises unsupported color modes.
            env["TERM"] = "xterm"
            shell_argv0 = "-" + Path(self.shell_path).name

            def child_setup():
                _safe_child_setup(slave)

            self.child = subprocess.Popen(
                [shell_argv0],
                executable=self.shell_path,
                stdin=slave,
                stdout=slave,
                stderr=slave,
                cwd=working_directory,
                env=env,
                shell=False,
                close_fds=True,
                preexec_fn=child_setup,
            )
            os.close(slave)
            os.set_blocking(self.master, False)
            self.shell_start = _process_start_or_fail(self.child.pid)
            self.selector.register(self.master, selectors.EVENT_READ, "pty")
        except Exception:
            try:
                os.close(slave)
            except (OSError, UnboundLocalError):
                pass
            if self.child is not None:
                self._terminate_child()
            if self.master is not None:
                os.close(self.master)
                self.master = None
            raise

    def _start_socket(self) -> None:
        path = self.session_dir / SOCKET_NAME
        if os.path.lexists(path):
            raise TerminalError("session socket already exists")
        if len(os.fsencode(path)) > MAX_UNIX_SOCKET_PATH_BYTES:
            raise TerminalError("socket path is too long")
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(str(path))
        self.socket_owned = True
        os.chmod(str(path), 0o600)
        self.listener.listen(MAX_CLIENTS)
        self.listener.setblocking(False)
        self.selector.register(self.listener, selectors.EVENT_READ, "listener")

    def _store_state(self, state: str, **extra) -> None:
        self.metadata.update({
            "protocol": PROTOCOL,
            "session_id": self.session_id,
            "generation": self.generation,
            "supervisor_pid": os.getpid(),
            "supervisor_start": self.supervisor_start,
            "shell_pid": self.child.pid if self.child is not None else 0,
            "shell_start": self.shell_start,
            "shell_path": self.shell_path,
            "name": self.name,
            "state": state,
            "columns": self.columns,
            "lines": self.lines,
            "renderer_available": self.renderer_available,
            "renderer_state": self.renderer_state,
            "created_at": self.created_at,
        })
        self.metadata.update(extra)
        _atomic_metadata(self.metadata_path, self.metadata)

    def _accept(self) -> None:
        conn, _address = self.listener.accept()
        conn.settimeout(1.0)
        try:
            _peer_uid(conn)
        except Exception:
            conn.close()
            return
        if len(self.clients) >= MAX_CLIENTS:
            self._respond_and_close(conn, {"ok": False, "error": "client_limit"})
            return
        self.clients[conn] = {"buffer": bytearray(), "client_id": None, "uid": os.getuid()}
        self.selector.register(conn, selectors.EVENT_READ, "client")

    def _respond_and_close(self, conn, response) -> None:
        try:
            self._send_response(conn, response)
        except OSError:
            pass
        self._drop_client(conn)

    def _drop_client(self, conn) -> None:
        if conn in self.clients:
            client_id = self.clients[conn].get("client_id")
            if client_id and self.lease_owner == client_id:
                self._release_lease(client_id)
            self.clients.pop(conn, None)
        try:
            self.selector.unregister(conn)
        except Exception:
            pass
        try:
            conn.close()
        except OSError:
            pass

    def _send_response(self, conn, response) -> None:
        data = json.dumps(response, separators=(",", ":"), ensure_ascii=True).encode("utf-8") + b"\n"
        if len(data) > MAX_FRAME:
            raise OSError("response too large")
        conn.sendall(data)

    def _client_ready(self, conn) -> None:
        try:
            chunk = conn.recv(65536)
        except OSError:
            self._drop_client(conn)
            return
        if not chunk:
            self._drop_client(conn)
            return
        state = self.clients[conn]
        state["buffer"].extend(chunk)
        if len(state["buffer"]) > MAX_REQUEST_FRAME:
            self._drop_client(conn)
            return
        newline = state["buffer"].find(b"\n")
        if newline < 0:
            return
        frame = bytes(state["buffer"][:newline])
        del state["buffer"][:newline + 1]
        try:
            request = json.loads(frame.decode("utf-8"))
            response, stop_after = self._dispatch(state, request)
            self._send_response(conn, response)
            if stop_after:
                self.stopping = True
        except (ValueError, TypeError, KeyError, TerminalError) as exc:
            try:
                self._send_response(conn, {"ok": False, "error": _safe_error(exc)})
            except OSError:
                self._drop_client(conn)
        except OSError:
            self._drop_client(conn)

    def _dispatch(self, client, request):
        if not isinstance(request, dict):
            raise TerminalError("invalid_request")
        op = request.get("op")
        client_id = request.get("client_id")
        if not isinstance(client_id, str) or len(client_id) != 32:
            raise TerminalError("invalid_client")
        if client["client_id"] is None:
            if op != "hello":
                raise TerminalError("hello_required")
            if (request.get("session_id") != self.session_id
                    or request.get("generation") != self.generation
                    or request.get("supervisor_pid") != os.getpid()
                    or request.get("supervisor_start") != self.supervisor_start):
                raise TerminalError("stale_identity")
            client["client_id"] = client_id
            return self._base(ok=True, state=self._state()), False
        if client["client_id"] != client_id:
            raise TerminalError("client_mismatch")
        if op == "poll":
            return self._poll(request), False
        if op == "acquire":
            return self._acquire(client_id, request), False
        if op == "release":
            self._release_lease(client_id)
            return self._base(ok=True, state=self._state(), lease=False), False
        if op == "input":
            return self._input(client_id, request), False
        if op == "resize":
            return self._resize(client_id, request), False
        if op == "stop":
            if self.child is not None:
                self._store_state("stopping", termination="requested")
                self._terminate_child()
            return self._base(ok=True, state=self._state()), True
        raise TerminalError("unsupported_operation")

    def _base(self, **extra):
        response = {
            "protocol": PROTOCOL,
            "session_id": self.session_id,
            "generation": self.generation,
            "state": self._state(),
            "lease": self.lease_owner is not None,
            "columns": self.columns,
            "lines": self.lines,
            "renderer_available": self.renderer_available,
            "renderer_state": self.renderer_state,
            "synced": self.renderer_available and self._state() == "running",
            "sequence": self.sequence,
        }
        response.update(extra)
        return response

    def _state(self) -> str:
        if self.child is None:
            return "exited"
        return "running" if self.child.poll() is None else "exited"

    def _screen_payload(self):
        if not self.renderer_available:
            return None
        try:
            self.renderer_state = "ready"
            cells = []
            for y in range(self.lines):
                row = self.screen.buffer[y]
                cells.append([_cell_json(row[x]) for x in range(self.columns)])
            raw = json.dumps({
                "rows": cells,
                "cursor_x": int(self.screen.cursor.x),
                "cursor_y": int(self.screen.cursor.y),
                "cursor_visible": not bool(self.screen.cursor.hidden),
            }, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
            packed = base64.b64encode(zlib.compress(raw, level=1)).decode("ascii")
            if len(packed) > MAX_SCREEN_B64:
                # A large but valid screen is a temporary transport limitation,
                # not a VT parser failure. Keep the renderer recoverable and
                # fail closed for input until a complete snapshot fits again.
                self.renderer_state = "snapshot_too_large"
                return None
            return packed
        except Exception:
            self.renderer_state = "snapshot_unavailable"
            return None

    def _poll(self, request):
        try:
            after = int(request.get("after", 0))
        except (TypeError, ValueError):
            raise TerminalError("invalid_sequence")
        if after < 0:
            raise TerminalError("invalid_sequence")
        oldest = self.output[0][0] if self.output else self.sequence + 1
        gap = after < oldest - 1
        data = bytearray()
        new_sequence = after
        more = False
        for sequence, chunk in self.output:
            if sequence <= after:
                continue
            if data and len(data) + len(chunk) > POLL_BYTES:
                more = True
                break
            data.extend(chunk)
            new_sequence = sequence
        if not more and new_sequence < self.sequence:
            more = True
        payload = self._screen_payload()
        synced = self.renderer_available and payload is not None and not self.screen_error
        if payload is None:
            gap = True
        self.snapshot_synced = synced and not gap

        client_id = request.get("client_id")
        if self.lease_owner == client_id:
            now = time.monotonic()
            if self.snapshot_synced and now < self.lease_deadline:
                # The active UI's regular poll is its lease heartbeat. This
                # keeps a visible input mode usable during ordinary idle time.
                self.lease_deadline = now + LEASE_SECONDS
            else:
                self._release_lease(client_id)
        response = self._base(
            ok=True,
            lease=self.lease_owner == request.get("client_id"),
            gap=gap,
            more=more,
            data=base64.b64encode(bytes(data)).decode("ascii"),
            sequence=new_sequence,
            snapshot_sequence=self.sequence,
            screen_z=payload,
            synced=synced,
        )
        return response

    def _acquire(self, client_id, request):
        try:
            after = int(request.get("after", -1))
        except (TypeError, ValueError):
            raise TerminalError("invalid_sequence")
        if self._state() != "running":
            raise TerminalError("session_exited")
        if not self.renderer_available or self.screen_error or not self.snapshot_synced:
            raise TerminalError("renderer_unavailable")
        if after < 0 or after > self.sequence:
            raise TerminalError("stale_identity")
        if self.lease_owner not in (None, client_id) and time.monotonic() < self.lease_deadline:
            return self._base(ok=True, lease=False, error="input_lease_busy")
        self.lease_owner = client_id
        self.lease_deadline = time.monotonic() + LEASE_SECONDS
        return self._base(ok=True, lease=True)

    def _release_lease(self, client_id):
        if self.lease_owner == client_id:
            self.lease_owner = None
            self.lease_deadline = 0.0

    def _input(self, client_id, request):
        if self._state() != "running":
            self._release_lease(client_id)
            raise TerminalError("session_exited")
        if not self.renderer_available or self.screen_error or not self.snapshot_synced:
            self._release_lease(client_id)
            raise TerminalError("renderer_unavailable")
        if (self.lease_owner != client_id or time.monotonic() >= self.lease_deadline):
            self._release_lease(client_id)
            raise TerminalError("input_lease_required")
        after = int(request.get("after", -1))
        if after < 0 or after > self.sequence:
            self._release_lease(client_id)
            raise TerminalError("stale_identity")
        encoded = request.get("data")
        if not isinstance(encoded, str) or len(encoded) > 12000:
            raise TerminalError("invalid_input")
        try:
            data = base64.b64decode(encoded, validate=True)
        except ValueError as exc:
            raise TerminalError("invalid_input") from exc
        if not data or len(data) > 8192:
            raise TerminalError("invalid_input")
        written = os.write(self.master, data)
        self.lease_deadline = time.monotonic() + LEASE_SECONDS
        return self._base(ok=True, lease=True, written=written)

    def _resize(self, client_id, request):
        if self._state() != "running":
            self._release_lease(client_id)
            raise TerminalError("session_exited")
        if not self.renderer_available or self.screen_error or not self.snapshot_synced:
            raise TerminalError("renderer_unavailable")
        if self.lease_owner != client_id or time.monotonic() >= self.lease_deadline:
            self._release_lease(client_id)
            raise TerminalError("input_lease_required")
        columns = request.get("columns")
        lines = request.get("lines")
        if (isinstance(columns, bool) or isinstance(lines, bool)
                or not isinstance(columns, int) or not isinstance(lines, int)
                or not 20 <= columns <= MAX_COLUMNS or not 5 <= lines <= MAX_LINES):
            raise TerminalError("invalid_terminal_size")
        after = int(request.get("after", -1))
        if after < 0 or after > self.sequence:
            raise TerminalError("stale_identity")
        fcntl.ioctl(self.master, termios.TIOCSWINSZ,
                    struct.pack("HHHH", lines, columns, 0, 0))
        self.columns, self.lines = columns, lines
        try:
            self.screen.resize(lines=lines, columns=columns)
        except Exception:
            self.screen_error = True
            self.renderer_state = "initialization_failed"
            raise TerminalError("renderer_resize_failed")
        try:
            os.killpg(self.child.pid, signal.SIGWINCH)
        except ProcessLookupError:
            self._release_lease(client_id)
            raise TerminalError("session_exited")
        self.lease_deadline = time.monotonic() + LEASE_SECONDS
        self._store_state("running")
        return self._base(ok=True, lease=True)

    def _terminal_reply(self, value):
        if self.master is None or self._state() != "running":
            return
        if isinstance(value, str):
            data = value.encode("utf-8", "replace")
        elif isinstance(value, bytes):
            data = value
        else:
            return
        if not data or len(data) > 1024:
            return
        try:
            os.write(self.master, data)
        except OSError:
            self.screen_error = True

    def _record_output(self, data: bytes) -> None:
        self.sequence += 1
        self.output.append((self.sequence, data))
        self.output_size += len(data)
        while self.output and (len(self.output) > MAX_OUTPUT_EVENTS
                               or self.output_size > MAX_OUTPUT_BYTES):
            _seq, old = self.output.pop(0)
            self.output_size -= len(old)
        if self.stream is not None and not self.screen_error:
            try:
                decoded = self.decoder.decode(data)
                if decoded:
                    self.stream.feed(decoded)
            except Exception:
                self.screen_error = True
                self.renderer_state = "parse_failed"
                self.lease_owner = None
                self._store_state(self._state(), renderer_error="vt_parse_failed")

    def _pty_ready(self):
        try:
            data = os.read(self.master, READ_CHUNK)
        except OSError as exc:
            if exc.errno in (errno.EIO, errno.EBADF):
                data = b""
            else:
                raise
        if not data:
            try:
                self.selector.unregister(self.master)
            except Exception:
                pass
            return
        self._record_output(data)

    def _check_child(self):
        if self.child is None:
            return
        code = self.child.poll()
        if code is None or self.metadata.get("state") == "exited":
            return
        self.lease_owner = None
        self.lease_deadline = 0.0
        if code < 0:
            self._store_state("exited", exit_code=None, exit_signal=-code)
        else:
            self._store_state("exited", exit_code=code, exit_signal=None)

    def run(self) -> None:
        try:
            while not self.stopping:
                self._check_child()
                if self.lease_owner is not None and time.monotonic() >= self.lease_deadline:
                    self._release_lease(self.lease_owner)
                for key, _mask in self.selector.select(timeout=0.1):
                    if key.data == "listener":
                        self._accept()
                    elif key.data == "pty":
                        self._pty_ready()
                    elif key.data == "client":
                        self._client_ready(key.fileobj)
        finally:
            self._shutdown()

    def _terminate_child(self):
        child = self.child
        if child is None:
            return
        pid = child.pid
        expected_start = self.shell_start
        current_start = _proc_start(pid)
        if expected_start and current_start not in (None, expected_start):
            raise TerminalError("shell identity changed during stop")
        if not expected_start:
            # Startup can fail after Popen but before /proc identity capture.
            # At that point the fresh shell has received no terminal input or
            # commands, and Popen still owns its unreaped child PID.
            child.terminate()
            try:
                child.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=2.0)
            self._check_child()
            return

        # Every PTY shell starts a fresh session (setsid in _safe_child_setup).
        # Terminate its live members by verified PID/start-time within that
        # session, not just its process group: shells can put jobs in their
        # own process groups while keeping those jobs in the same session.
        term_sent = set()
        try:
            _signal_session_processes(pid, signal.SIGTERM, term_sent)
            term_deadline = time.monotonic() + 2.0
            while time.monotonic() < term_deadline:
                child.poll()  # Reap the shell if it has exited.
                if not _session_processes(pid):
                    break
                _signal_session_processes(pid, signal.SIGTERM, term_sent)
                time.sleep(0.05)

            # Escalate only against processes still observed in this PTY's
            # session. This also catches stubborn children in separate groups.
            kill_deadline = time.monotonic() + 2.0
            while time.monotonic() < kill_deadline:
                remaining = _session_processes(pid)
                if not remaining:
                    break
                _signal_session_processes(pid, signal.SIGKILL)
                child.poll()
                time.sleep(0.02)
        except OSError as exc:
            raise TerminalError("cannot inspect PTY process session") from exc

        try:
            child.wait(timeout=2.0)
        except subprocess.TimeoutExpired as exc:
            raise TerminalError("PTY shell did not exit after session termination") from exc
        self._check_child()

    def _shutdown(self):
        self.lease_owner = None
        self.lease_deadline = 0.0
        if self.child is not None:
            self._terminate_child()
        for conn in tuple(self.clients):
            self._drop_client(conn)
        if self.listener is not None:
            try:
                self.selector.unregister(self.listener)
            except Exception:
                pass
            self.listener.close()
        if self.master is not None:
            try:
                self.selector.unregister(self.master)
            except Exception:
                pass
            os.close(self.master)
            self.master = None
        self.selector.close()
        socket_path = self.session_dir / SOCKET_NAME
        try:
            if self.socket_owned and stat.S_ISSOCK(socket_path.lstat().st_mode):
                socket_path.unlink()
        except FileNotFoundError:
            pass
        if self.metadata.get("state") != "exited":
            self._store_state("stopped")
        elif self.metadata.get("state") == "exited":
            self._store_state("exited", exit_code=self.metadata.get("exit_code"),
                              exit_signal=self.metadata.get("exit_signal"))


def _safe_error(exc: Exception) -> str:
    value = str(exc)
    allowed = {
        "invalid_request", "invalid_client", "hello_required", "stale_identity",
        "client_mismatch", "session_exited", "renderer_unavailable",
        "input_lease_required", "stream_gap", "invalid_sequence", "invalid_input",
        "invalid_terminal_size", "renderer_resize_failed", "unsupported_operation",
    }
    return value if value in allowed else "request_rejected"


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    os.umask(0o077)
    if len(args) != 3:
        return 2
    session_dir = Path(args[0])
    session_id, generation = args[1], args[2]
    owner = None
    stage = "owner_initialization"
    try:
        owner = PtyOwner(session_dir, session_id, generation)
        stage = "event_loop"
        owner.run()
        return 0
    except Exception as exc:
        if owner is not None:
            owner._shutdown()
            stage = owner.startup_stage
        # A deliberately small, opt-in diagnostic: static stage and exception
        # type only. Never persist PTY bytes, environment values, paths from an
        # exception, or shell output. This lets fixtures distinguish startup
        # failures without turning stderr into an output log.
        if os.environ.get("JTT_TERMINAL_FIXTURE_DIAGNOSTICS") == "1":
            try:
                metadata_path = session_dir / SESSION_META
                metadata = _read_metadata(metadata_path)
                if (metadata.get("session_id") == session_id
                        and metadata.get("generation") == generation):
                    prior_stage = metadata.get("startup_stage")
                    if isinstance(prior_stage, str) and prior_stage.isidentifier():
                        stage = prior_stage
                    metadata["startup_failure"] = {
                        "stage": stage,
                        "exception": type(exc).__name__,
                    }
                    _atomic_metadata(metadata_path, metadata)
            except Exception:
                pass
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
