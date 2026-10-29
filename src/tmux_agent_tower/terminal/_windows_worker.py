"""Isolated native-Windows ConPTY supervisor entry point.

The shell is started in a bootstrap wait loop. It cannot accept user commands
until the worker has assigned its retained process handle to the owned Job
Object. This closes the pywinpty spawn-to-job window for the supported
PowerShell launch path; it is not an OS sandbox against the same user account.
"""
from __future__ import annotations

import base64
from collections import deque
import ctypes
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import uuid
import weakref
import zlib


MAX_MESSAGE = 896 * 1024
MAX_REQUEST = 96 * 1024
MAX_POLL_BYTES = 128 * 1024
OUTPUT_BUFFER_BYTES = 256 * 1024
LEASE_SECONDS = 15.0
MAX_PIPE_CLIENTS = 8
MAX_PIPE_INSTANCES = MAX_PIPE_CLIENTS + 1  # connected clients plus one listener
MAX_COLUMNS = 300
MAX_LINES = 120
BOOTSTRAP_WAIT_MS = 30_000
JOB_PROCESS_EXIT_TIMEOUT = 2.0
JOB_PROCESS_EXIT_POLL_INTERVAL = 0.025
_STAGE = "worker_import"


def _set_stage(value):
    global _STAGE
    _STAGE = value


def _load_runtime():
    package_root = Path(__file__).resolve().parents[1]
    source_root = package_root.parent
    sys.path.insert(0, str(source_root))
    if package_root.name == "ju0_terminal_tower":
        import ju0_terminal_tower  # noqa: F401
    from tmux_agent_tower.terminal import windows_manager
    return windows_manager


def _current_process_start(win32api, win32process):
    return str(win32process.GetProcessTimes(win32api.GetCurrentProcess())["CreationTime"])


def _cancel_synchronous_io(thread):
    """Cancel a blocking synchronous pipe operation owned by our worker thread."""
    if not thread.is_alive():
        return False
    native_id = getattr(thread, "native_id", None)
    if not isinstance(native_id, int) or native_id <= 0:
        return False
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    open_thread = kernel32.OpenThread
    open_thread.argtypes = (ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32)
    open_thread.restype = ctypes.c_void_p
    cancel_io = kernel32.CancelSynchronousIo
    cancel_io.argtypes = (ctypes.c_void_p,)
    cancel_io.restype = ctypes.c_int
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (ctypes.c_void_p,)
    close_handle.restype = ctypes.c_int
    handle = open_thread(0x0001, False, native_id)  # THREAD_TERMINATE
    if not handle:
        return False
    try:
        # ERROR_NOT_FOUND simply means this thread no longer has synchronous
        # I/O pending; shutdown still joins it using the common deadline.
        return bool(cancel_io(handle))
    finally:
        close_handle(handle)


class _ClientThreadRegistry:
    """Track only active pipe workers and serialize admission with shutdown."""

    def __init__(self):
        self._lock = threading.RLock()
        # Weak references cannot retain completed Thread objects if a caller
        # outlives their worker. The accept loop separately enforces the live
        # named-pipe client limit using client_handles under state_lock.
        self._threads = {}
        self._closing = False

    def _active_locked(self):
        active = []
        for key, reference in list(self._threads.items()):
            thread = reference()
            if thread is None or not thread.is_alive():
                if self._threads.get(key) is reference:
                    self._threads.pop(key, None)
            else:
                active.append(thread)
        return tuple(active)

    def start(self, target, *, args=(), name="jtt-named-pipe-client"):
        """Start one worker, unless shutdown has closed admission."""
        with self._lock:
            if self._closing:
                return None, "closing"
            self._active_locked()
            thread = threading.Thread(
                target=target, args=args, name=name, daemon=True
            )
            key = id(thread)
            reference = weakref.ref(thread)
            self._threads[key] = reference
            try:
                thread.start()
            except BaseException:
                if self._threads.get(key) is reference:
                    self._threads.pop(key, None)
                raise
            return thread, None

    def begin_shutdown(self, stop_event):
        """Atomically stop admission, signal shutdown, and snapshot active workers."""
        with self._lock:
            self._closing = True
            stop_event.set()
            return self._active_locked()

    def snapshot(self):
        with self._lock:
            return self._active_locked()


def _screen_payload(pyte, screen):
    cells = []
    for y in range(screen.lines):
        row = screen.buffer[y]
        cells.append([[
            cell.data, cell.fg, cell.bg, int(bool(cell.bold)),
            int(bool(cell.italics)), int(bool(cell.underscore)),
            int(bool(cell.reverse)), int(bool(cell.blink)),
            int(bool(cell.strikethrough)),
        ] for cell in (row[x] for x in range(screen.columns))])
    raw = json.dumps({
        "rows": cells,
        "cursor_x": int(screen.cursor.x),
        "cursor_y": int(screen.cursor.y),
        "cursor_visible": not bool(screen.cursor.hidden),
    }, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    encoded = base64.b64encode(zlib.compress(raw, 1)).decode("ascii")
    if len(encoded) > MAX_MESSAGE - 4096:
        raise ValueError("terminal snapshot exceeds protocol limit")
    return encoded


def _identity_data(session_id, generation, supervisor_pid, supervisor_start,
                   shell_pid, shell_start):
    return {
        "session_id": session_id,
        "generation": generation,
        "supervisor_pid": int(supervisor_pid),
        "supervisor_start": str(supervisor_start),
        "shell_pid": int(shell_pid),
        "shell_start": str(shell_start),
    }


def _wait_for_job_empty(query_active_processes, *, timeout,
                        poll_interval=JOB_PROCESS_EXIT_POLL_INTERVAL,
                        clock=time.monotonic, sleep=time.sleep):
    """Return only after the owned Job reports zero active processes.

    Query errors and malformed counts are not interpreted as process exit.
    The caller keeps the session record and reports Stop failure on either.
    """
    deadline = clock() + timeout
    while True:
        try:
            active = query_active_processes()
        except Exception as exc:
            raise RuntimeError("owned Job process count is unavailable") from exc
        if isinstance(active, bool) or not isinstance(active, int) or active < 0:
            raise RuntimeError("owned Job process count is invalid")
        if active == 0:
            return True
        remaining = deadline - clock()
        if remaining <= 0:
            return False
        sleep(min(poll_interval, remaining))


def _owned_stop_state_is_retryable(state: str) -> bool:
    """Allow only an identity-bound retry after an explicit Stop failure."""
    return state in ("running", "exited", "stopping")


def assign_owned_job(job, process_handle, win32job, win32api, win32event,
                     win32con):
    """Assign only the retained child handle; fail closed on any error."""
    try:
        win32job.AssignProcessToJobObject(job, process_handle)
        if not win32job.IsProcessInJob(process_handle, job):
            raise RuntimeError("PowerShell Job membership was not confirmed")
    except Exception:
        win32api.TerminateProcess(process_handle, 1)
        if win32event.WaitForSingleObject(process_handle, 5000) != win32con.WAIT_OBJECT_0:
            raise RuntimeError("owned PowerShell cleanup after job assignment failure timed out")
        raise


def _bootstrap_script(release_name, abort_name):
    """Keep PowerShell non-interactive unless the worker opens the release gate."""
    return (
        "$ErrorActionPreference='Stop'; "
        "try { "
        "$r=[System.Threading.EventWaitHandle]::OpenExisting('" + release_name + "'); "
        "$a=[System.Threading.EventWaitHandle]::OpenExisting('" + abort_name + "'); "
        "$w=[System.Threading.WaitHandle]::WaitAny("
        "[System.Threading.WaitHandle[]]@($r,$a)," + str(BOOTSTRAP_WAIT_MS) + "); "
        "if ($w -ne 0) { [System.Environment]::Exit(70) }; "
        "$r.Dispose(); $a.Dispose(); "
        "} catch { [System.Environment]::Exit(71) }; "
        "[Console]::InputEncoding=[System.Text.UTF8Encoding]::new($false); "
        "[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new($false); "
        "$OutputEncoding=[Console]::OutputEncoding; "
        "Remove-Module PSReadLine -ErrorAction SilentlyContinue; "
        "function global:prompt { "
        "$path=$ExecutionContext.SessionState.Path.CurrentLocation.Path; "
        "$width=120; try { $width=[int]$Host.UI.RawUI.WindowSize.Width } catch {}; "
        # Leave about 30 columns for command entry when the path is abbreviated.
        "$limit=[Math]::Max(12,$width-36); "
        "if ($path.Length -gt $limit) { "
        "$root=[System.IO.Path]::GetPathRoot($path); "
        "if ($null -eq $root) { $root='' }; "
        "if ($root.Length -gt ($limit-5)) { $root=$root.Substring(0,$limit-5) }; "
        "$tailCount=[Math]::Max(1,$limit-$root.Length-4); "
        "$tailStart=[Math]::Max($root.Length,$path.Length-$tailCount); "
        "$path=$root+'...\\'+$path.Substring($tailStart) "
        "}; 'PS '+$path+'> ' }"
    )


def _run(session_dir_text, session_id, generation, name, runtime=None):
    _set_stage("native_imports")
    if sys.platform != "win32":
        raise RuntimeError("native Windows is required; no WSL fallback")
    import pyte
    import pywintypes
    import win32api
    import win32con
    import win32event
    import win32file
    import win32job
    import win32pipe
    import win32process
    import win32security
    from winpty import Backend, PTY

    _set_stage("runtime_import")
    runtime = runtime or _load_runtime()
    _set_stage("session_validation")
    session_dir = Path(session_dir_text).resolve(strict=True)
    runtime._ensure_private_directory(session_dir.parent)
    runtime._private_acl(session_dir, directory=True)
    metadata_path = session_dir / runtime.SESSION_META
    metadata = runtime._read_metadata(metadata_path)
    if (metadata.get("session_id") != session_id
            or metadata.get("generation") != generation
            or metadata.get("state") != "starting"):
        raise RuntimeError("terminal startup identity mismatch")

    _set_stage("pipe_acl")
    owner_token = win32security.OpenProcessToken(
        win32api.GetCurrentProcess(), win32con.TOKEN_QUERY
    )
    try:
        owner_sid = win32security.GetTokenInformation(
            owner_token, win32security.TokenUser
        )[0]
    finally:
        owner_token.Close()
    acl = win32security.ACL()
    acl.AddAccessAllowedAce(win32security.ACL_REVISION, win32con.GENERIC_ALL, owner_sid)
    descriptor = win32security.SECURITY_DESCRIPTOR()
    descriptor.SetSecurityDescriptorDacl(True, acl, False)
    attributes = pywintypes.SECURITY_ATTRIBUTES()
    attributes.SECURITY_DESCRIPTOR = descriptor
    pipe_name = runtime._planned_socket_path(session_dir, session_id)

    def create_server_pipe(first_instance=False):
        access = win32pipe.PIPE_ACCESS_DUPLEX
        if first_instance:
            access |= 0x00080000  # FILE_FLAG_FIRST_PIPE_INSTANCE
        server_pipe = win32pipe.CreateNamedPipe(
            pipe_name, access,
            win32pipe.PIPE_TYPE_MESSAGE | win32pipe.PIPE_READMODE_MESSAGE
            | win32pipe.PIPE_WAIT,
            MAX_PIPE_INSTANCES, MAX_MESSAGE, MAX_REQUEST, 3000, attributes,
        )
        try:
            actual = win32security.GetSecurityInfo(
                server_pipe, win32security.SE_KERNEL_OBJECT,
                win32security.DACL_SECURITY_INFORMATION,
            ).GetSecurityDescriptorDacl()
            if (actual is None or actual.GetAceCount() != 1
                    or actual.GetAce(0)[0][0] != win32security.ACCESS_ALLOWED_ACE_TYPE
                    or actual.GetAce(0)[2] != owner_sid):
                raise RuntimeError("owner-only named pipe ACL validation failed")
            return server_pipe
        except Exception:
            server_pipe.Close()
            raise

    pipe = create_server_pipe(first_instance=True)
    try:
        _set_stage("renderer_init")
        pyte_screen = pyte.Screen(100, 30)
        stream = pyte.Stream(pyte_screen)
        stream.feed("\x1b[0m")
        screen_lock = threading.RLock()
        state_lock = threading.RLock()
        stop_reader = threading.Event()
        stop_worker = threading.Event()
        output = deque()
        output_bytes = 0
        sequence = 0
        screen_error = False
        state = "starting"
        exit_code = None
        lease_owner = None
        lease_deadline = 0.0
        # A bounded set of connected observers. Each client must complete its
        # own hello, while the lease below remains exclusive to one connection.
        clients = {}
        client_handles = {}
        client_threads = _ClientThreadRegistry()
        client_changed = threading.Event()
        connection_counter = 0

        _set_stage("shell_resolution")
        shell_path = Path(os.environ["SystemRoot"]) / (
            r"System32\WindowsPowerShell\v1.0\powershell.exe"
        )
        shell_path = shell_path.resolve(strict=True)
        working_directory = Path(str(metadata.get("working_directory") or Path.cwd()))
        if not working_directory.is_dir():
            raise RuntimeError("terminal working directory is unavailable")
        env_keys = (
            "SystemRoot", "WINDIR", "PATH", "LOCALAPPDATA", "APPDATA",
            "USERPROFILE", "HOMEDRIVE", "HOMEPATH",
        )
        shell_env = {key: os.environ[key] for key in env_keys if key in os.environ}
        shell_env.setdefault("SystemRoot", r"C:\Windows")
        shell_env.setdefault("WINDIR", shell_env["SystemRoot"])
        shell_env.setdefault("PATH", str(Path(shell_env["SystemRoot"]) / "System32"))
        for key in ("TEMP", "TMP"):
            if key in os.environ:
                shell_env[key] = os.environ[key]
        shell_env["JTT_HOME"] = str(session_dir.parent.parent)
        shell_env["TERM"] = "xterm"

        # PowerShell executes only this controlled wait loop until the process
        # has joined the Job. Both unique events are local to this session.
        _set_stage("gate_create")
        gate_name = "Local\\JTT-" + uuid.uuid4().hex
        abort_name = "Local\\JTT-" + uuid.uuid4().hex
        gate = win32event.CreateEvent(attributes, True, False, gate_name)
        abort_gate = win32event.CreateEvent(attributes, True, False, abort_name)
        _set_stage("conpty_create")
        terminal = PTY(100, 30, backend=Backend.ConPTY)
        _set_stage("job_create")
        job = win32job.CreateJobObject(None, "")
        _set_stage("job_query")
        job_info = win32job.QueryInformationJobObject(
            job, win32job.JobObjectExtendedLimitInformation
        )
        job_info["BasicLimitInformation"]["LimitFlags"] = (
            win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        )
        _set_stage("job_configure")
        win32job.SetInformationJobObject(
            job, win32job.JobObjectExtendedLimitInformation, job_info
        )
        shell_handle = None
        shell_in_job = False
        try:
            _set_stage("shell_spawn")
            gate_script = _bootstrap_script(gate_name, abort_name)
            argv = ["-NoLogo", "-NoProfile", "-NoExit", "-Command", gate_script]
            # Record that spawn is about to be attempted. If its result becomes
            # ambiguous, the manager must preserve this registry entry.
            metadata["spawn_attempted"] = True
            runtime._atomic_metadata(metadata_path, metadata)
            spawned = terminal.spawn(
                str(shell_path), cmdline=subprocess.list2cmdline(argv),
                cwd=str(working_directory),
                env="\0".join(f"{key}={value}" for key, value in shell_env.items()) + "\0",
            )
            if not spawned:
                raise RuntimeError("ConPTY PowerShell creation failed")
            shell_pid = int(terminal.pid)
            # Publish this immediately. Until creation time is available it is
            # diagnostic only and must never authorize a PID-based signal.
            metadata.update({"shell_pid": shell_pid, "shell_path": str(shell_path)})
            runtime._atomic_metadata(metadata_path, metadata)
            _set_stage("shell_handle_open")
            shell_handle = win32api.OpenProcess(
                win32con.PROCESS_QUERY_INFORMATION | win32con.PROCESS_SET_QUOTA
                | win32con.PROCESS_TERMINATE | win32con.SYNCHRONIZE,
                False, shell_pid,
            )
            # The controlled PS bootstrap cannot run user input before the
            # retained process handle and its creation timestamp are captured.
            _set_stage("process_times")
            shell_start = str(win32process.GetProcessTimes(shell_handle)["CreationTime"])
            supervisor_start = _current_process_start(win32api, win32process)
            identity = _identity_data(
                session_id, generation, os.getpid(), supervisor_start,
                shell_pid, shell_start,
            )
            metadata.update(identity)
            metadata.update({
                "shell_pid": shell_pid, "shell_start": shell_start,
                "supervisor_pid": os.getpid(), "supervisor_start": supervisor_start,
                "shell_path": str(shell_path), "name": name, "state": "starting",
                "renderer_state": "ready", "working_directory": str(working_directory),
            })
            _set_stage("metadata_publish")
            runtime._atomic_metadata(metadata_path, metadata)
            # The command loop is still blocked on the private gate here.
            # Job membership is established before the first interactive prompt.
            _set_stage("job_assignment")
            assign_owned_job(job, shell_handle, win32job, win32api,
                             win32event, win32con)
            # The retained shell handle is verified as a Job member before
            # PowerShell's bootstrap gate is released. Descendants inherit
            # membership because neither breakaway flag is enabled.
            shell_in_job = True
            _set_stage("publish_running_gate")
            win32event.SetEvent(gate)
            with state_lock:
                state = "running"
                metadata["state"] = state
                _set_stage("publish_running_metadata")
                runtime._atomic_metadata(metadata_path, metadata)

            def read_output():
                nonlocal sequence, output_bytes, screen_error, state, exit_code
                try:
                    while not stop_reader.is_set():
                        if win32event.WaitForSingleObject(shell_handle, 0) == win32con.WAIT_OBJECT_0:
                            with state_lock:
                                if state == "running":
                                    state = "exited"
                                    exit_code = int(win32process.GetExitCodeProcess(shell_handle))
                                    metadata.update({"state": state, "exit_code": exit_code,
                                                     "termination": "shell_exit"})
                                    runtime._atomic_metadata(metadata_path, metadata)
                        try:
                            data = terminal.read(blocking=False)
                        except Exception:
                            if win32event.WaitForSingleObject(shell_handle, 0) == win32con.WAIT_OBJECT_0:
                                data = ""
                            else:
                                raise
                        if data:
                            encoded = data.encode("utf-8", errors="strict")
                            with screen_lock:
                                stream.feed(data)
                            with state_lock:
                                for offset in range(0, len(encoded), 64 * 1024):
                                    sequence += 1
                                    chunk = encoded[offset:offset + 64 * 1024]
                                    output.append((sequence, chunk))
                                    output_bytes += len(chunk)
                                    while output and output_bytes > OUTPUT_BUFFER_BYTES:
                                        _seq, old = output.popleft()
                                        output_bytes -= len(old)
                        else:
                            stop_reader.wait(0.01)
                except Exception:
                    with state_lock:
                        screen_error = True

            _set_stage("serve_pipe")
            reader = threading.Thread(target=read_output, name="jtt-conpty-reader", daemon=True)
            reader.start()
            pipe_error = []

            def serve_client(client_pipe, connection_id):
                nonlocal lease_owner, lease_deadline
                try:
                    while not stop_worker.is_set():
                        try:
                            _status, request_bytes = win32file.ReadFile(
                                client_pipe, MAX_REQUEST
                            )
                            if len(request_bytes) > MAX_REQUEST:
                                raise ValueError("request too large")
                        except pywintypes.error as exc:
                            if (exc.winerror in (109, 232, 233)
                                    or (stop_worker.is_set() and exc.winerror == 995)):
                                break
                            raise
                        win32security.ImpersonateNamedPipeClient(client_pipe)
                        try:
                            token = win32security.OpenThreadToken(
                                win32api.GetCurrentThread(), win32con.TOKEN_QUERY, True
                            )
                            try:
                                caller_sid = win32security.GetTokenInformation(
                                    token, win32security.TokenUser
                                )[0]
                            finally:
                                token.Close()
                        finally:
                            win32security.RevertToSelf()
                        if caller_sid != owner_sid:
                            raise PermissionError("named pipe caller SID rejected")
                        try:
                            request = json.loads(request_bytes.decode("utf-8"))
                            if not isinstance(request, dict):
                                raise ValueError("object request required")
                        except (ValueError, UnicodeError):
                            response = {"ok": False, "error": "invalid_request"}
                            request = None
                        else:
                            response = dispatch(request, connection_id)
                        with state_lock:
                            response["lease"] = (
                                isinstance(request, dict)
                                and lease_owner == (connection_id, request.get("client_id"))
                            )
                        payload = json.dumps(response, separators=(",", ":"),
                                             ensure_ascii=True).encode("utf-8")
                        if len(payload) > MAX_MESSAGE:
                            payload = b'{"ok":false,"error":"response_too_large"}'
                        win32file.WriteFile(client_pipe, payload)
                        if response.get("stopping"):
                            client_threads.begin_shutdown(stop_worker)
                            break
                except Exception as exc:
                    pipe_error.append(type(exc).__name__)
                finally:
                    with state_lock:
                        clients.pop(connection_id, None)
                        client_handles.pop(connection_id, None)
                        if lease_owner is not None and lease_owner[0] == connection_id:
                            lease_owner = None
                            lease_deadline = 0.0
                    if not stop_worker.is_set():
                        try:
                            win32pipe.DisconnectNamedPipe(client_pipe)
                        except Exception:
                            pass
                    try:
                        client_pipe.Close()
                    except Exception:
                        pass
                    client_changed.set()

            def dispatch(request, connection_id):
                with state_lock:
                    return dispatch_locked(request, connection_id)

            def dispatch_locked(request, connection_id):
                nonlocal lease_owner, lease_deadline, state
                client_id = request.get("client_id")
                op = request.get("op")
                if not isinstance(client_id, str) or len(client_id) != 32:
                    return base(error="invalid_client")
                if op == "hello":
                    if connection_id not in clients:
                        return base(error="client_busy")
                    if request.get("identity") != identity:
                        return base(error="stale_identity")
                    if clients[connection_id] not in (None, client_id):
                        return base(error="stale_identity")
                    clients[connection_id] = client_id
                    return base(ok=True, lease=False)
                if clients.get(connection_id) != client_id:
                    return base(error="hello_required")
                if request.get("identity") != identity:
                    return base(error="stale_identity")
                now = time.monotonic()
                if lease_owner is not None and now >= lease_deadline:
                    lease_owner = None
                    lease_deadline = 0.0
                if op == "observe":
                    with screen_lock, state_lock:
                        return observation()
                if op == "poll":
                    try:
                        after = int(request.get("after", -1))
                    except (TypeError, ValueError):
                        return base(error="invalid_sequence")
                    with state_lock:
                        if after < 0 or after > sequence:
                            return base(error="stale_identity")
                        oldest = output[0][0] if output else sequence + 1
                        gap = after < oldest - 1
                        chunks = []
                        total = 0
                        new_sequence = after
                        more = False
                        for item_sequence, chunk in output:
                            if item_sequence <= after:
                                continue
                            if total and total + len(chunk) > MAX_POLL_BYTES:
                                more = True
                                break
                            chunks.append(chunk)
                            total += len(chunk)
                            new_sequence = item_sequence
                        if not more and new_sequence < sequence:
                            more = True
                        synced = state == "running" and not screen_error and not gap
                        if lease_owner is not None and lease_owner[0] == connection_id:
                            if synced and client_id == lease_owner[1]:
                                lease_deadline = now + LEASE_SECONDS
                            else:
                                lease_owner = None
                                lease_deadline = 0.0
                    with screen_lock:
                        payload = _screen_payload(pyte, pyte_screen)
                    response = base(ok=True, lease=bool(lease_owner and lease_owner[1] == client_id))
                    response.update({
                        "gap": gap, "more": more, "data": base64.b64encode(b"".join(chunks)).decode("ascii"),
                        "sequence": new_sequence, "snapshot_sequence": sequence,
                        "screen_z": payload, "synced": synced,
                    })
                    return response
                if op == "acquire":
                    if state != "running":
                        return base(error="session_exited")
                    with screen_lock:
                        payload = _screen_payload(pyte, pyte_screen)
                    if screen_error or not payload:
                        return base(error="renderer_unavailable")
                    if lease_owner is not None and lease_owner != (connection_id, client_id):
                        return base(ok=True, lease=False)
                    lease_owner = (connection_id, client_id)
                    lease_deadline = now + LEASE_SECONDS
                    return base(ok=True, lease=True)
                if op == "release":
                    if lease_owner == (connection_id, client_id):
                        lease_owner = None
                        lease_deadline = 0.0
                    return base(ok=True, lease=False)
                if op in ("input", "resize"):
                    if state != "running":
                        return base(error="session_exited")
                    if lease_owner != (connection_id, client_id) or now >= lease_deadline:
                        lease_owner = None
                        lease_deadline = 0.0
                        return base(error="lease_expired")
                    try:
                        after = int(request.get("after", -1))
                    except (TypeError, ValueError):
                        return base(error="invalid_sequence")
                    if after < 0 or after > sequence:
                        return base(error="stale_identity")
                    if op == "input":
                        oldest = output[0][0] if output else sequence + 1
                        if after < oldest - 1:
                            # Input must never continue from a client whose
                            # output cursor fell behind the bounded stream.
                            lease_owner = None
                            lease_deadline = 0.0
                            return base(error="stream_gap")
                        if screen_error:
                            lease_owner = None
                            lease_deadline = 0.0
                            return base(error="renderer_unavailable")
                        try:
                            data = base64.b64decode(request.get("data", ""), validate=True)
                            text = data.decode("utf-8", errors="strict")
                        except (ValueError, UnicodeError, TypeError):
                            return base(error="invalid_input")
                        if not data or len(data) > 8192:
                            return base(error="invalid_input")
                        terminal.write(text)
                        lease_deadline = now + LEASE_SECONDS
                        return base(ok=True, written=len(data), lease=True)
                    columns, lines = request.get("columns"), request.get("lines")
                    if (isinstance(columns, bool) or isinstance(lines, bool)
                            or not isinstance(columns, int) or not isinstance(lines, int)
                            or not 20 <= columns <= MAX_COLUMNS or not 5 <= lines <= MAX_LINES):
                        return base(error="invalid_terminal_size")
                    terminal.set_size(columns, lines)
                    with screen_lock:
                        pyte_screen.resize(lines=lines, columns=columns)
                    with state_lock:
                        metadata["columns"], metadata["lines"] = columns, lines
                        runtime._atomic_metadata(metadata_path, metadata)
                    return base(ok=True, lease=True, columns=columns, lines=lines)
                if op == "stop":
                    if not _owned_stop_state_is_retryable(state):
                        return base(error="session_unavailable")
                    with state_lock:
                        state = "stopping"
                        metadata.update({"state": state, "termination": "requested"})
                        runtime._atomic_metadata(metadata_path, metadata)
                    try:
                        win32job.TerminateJobObject(job, 0)
                    except Exception:
                        return base(error="owned_job_termination_failed")
                    if win32event.WaitForSingleObject(shell_handle, 5000) != win32con.WAIT_OBJECT_0:
                        return base(error="owned_process_exit_timeout")
                    try:
                        job_empty = _wait_for_job_empty(
                            lambda: win32job.QueryInformationJobObject(
                                job, win32job.JobObjectBasicAccountingInformation
                            )["ActiveProcesses"],
                            timeout=JOB_PROCESS_EXIT_TIMEOUT,
                        )
                    except Exception:
                        return base(error="owned_job_exit_unverified")
                    if not job_empty:
                        return base(error="owned_job_processes_remain")
                    with state_lock:
                        exit_code = int(win32process.GetExitCodeProcess(shell_handle))
                        state = "exited"
                        metadata.update({"state": state, "exit_code": exit_code,
                                         "termination": "requested",
                                         "job_processes_empty": True})
                        runtime._atomic_metadata(metadata_path, metadata)
                    response = base(ok=True, state="exited", stopping=True)
                    return response
                return base(error="unsupported_operation")

            def base(**extra):
                with state_lock:
                    current_state, current_sequence = state, sequence
                    current_lease = bool(lease_owner)
                response = {
                    "ok": False, "protocol": 1, "session_id": session_id,
                    "generation": generation, "state": current_state,
                    "lease": current_lease, "columns": pyte_screen.columns,
                    "lines": pyte_screen.lines, "renderer_available": not screen_error,
                    "renderer_state": "error" if screen_error else "ready",
                    "synced": current_state == "running" and not screen_error,
                    "sequence": current_sequence,
                }
                response.update(extra)
                return response

            def observation():
                payload = _screen_payload(pyte, pyte_screen) if not screen_error else None
                with state_lock:
                    current_state, current_sequence = state, sequence
                return {
                    "ok": True, "protocol": 1, "session_id": session_id,
                    "generation": generation, "shell_pid": shell_pid,
                    "terminal_state": current_state,
                    "renderer_available": not screen_error,
                    "renderer_state": "ready" if not screen_error else "parse_failed",
                    "synced": bool(payload) and current_state == "running",
                    "columns": pyte_screen.columns, "lines": pyte_screen.lines,
                    "output_sequence": current_sequence,
                    "snapshot_revision": current_sequence, "screen_z": payload,
                }

            accept_done = threading.Event()

            def accept_clients():
                nonlocal pipe, connection_counter, state
                try:
                    while not stop_worker.is_set():
                        if pipe is None:
                            try:
                                pipe = create_server_pipe()
                            except pywintypes.error as exc:
                                if exc.winerror not in (231, 232):
                                    raise
                                client_changed.wait(0.05)
                                client_changed.clear()
                                continue
                        listener = pipe
                        pipe = None
                        try:
                            win32pipe.ConnectNamedPipe(listener, None)
                        except pywintypes.error as exc:
                            if exc.winerror != 535:  # ERROR_PIPE_CONNECTED
                                if stop_worker.is_set():
                                    try:
                                        listener.Close()
                                    except Exception:
                                        pass
                                    break
                                raise
                        if stop_worker.is_set():
                            try:
                                win32pipe.DisconnectNamedPipe(listener)
                            except Exception:
                                pass
                            listener.Close()
                            break
                        with state_lock:
                            if len(client_handles) >= MAX_PIPE_CLIENTS:
                                connection_id = None
                            else:
                                connection_counter += 1
                                connection_id = connection_counter
                                clients[connection_id] = None
                                client_handles[connection_id] = listener
                        if connection_id is None:
                            try:
                                win32pipe.DisconnectNamedPipe(listener)
                            except Exception:
                                pass
                            listener.Close()
                            client_changed.set()
                            continue
                        try:
                            thread, rejection = client_threads.start(
                                serve_client,
                                args=(listener, connection_id),
                            )
                        except Exception:
                            with state_lock:
                                clients.pop(connection_id, None)
                                client_handles.pop(connection_id, None)
                            try:
                                win32pipe.DisconnectNamedPipe(listener)
                            except Exception:
                                pass
                            try:
                                listener.Close()
                            except Exception:
                                pass
                            raise
                        if rejection is not None:
                            with state_lock:
                                clients.pop(connection_id, None)
                                client_handles.pop(connection_id, None)
                            try:
                                win32pipe.DisconnectNamedPipe(listener)
                            except Exception:
                                pass
                            try:
                                listener.Close()
                            except Exception:
                                pass
                            client_changed.set()
                            if rejection == "closing":
                                break
                except Exception as exc:
                    pipe_error.append(type(exc).__name__)
                    failure = {
                        "stage": "pipe_accept",
                        "exception": type(exc).__name__,
                    }
                    winerror = getattr(exc, "winerror", None)
                    if isinstance(winerror, int):
                        failure["cause_stage"] = "pipe_accept"
                        failure["cause"] = "winerror_{}".format(winerror)
                    try:
                        with state_lock:
                            metadata["worker_failure"] = failure
                            if state == "running":
                                state = "unavailable"
                                metadata["state"] = state
                                metadata["renderer_state"] = "error"
                            runtime._atomic_metadata(metadata_path, metadata)
                    except Exception:
                        pass
                    client_threads.begin_shutdown(stop_worker)
                finally:
                    if pipe is not None:
                        try:
                            pipe.Close()
                        except Exception:
                            pass
                        pipe = None
                    accept_done.set()

            accept_thread = threading.Thread(
                target=accept_clients, name="jtt-named-pipe-accept", daemon=True
            )
            accept_thread.start()
            while not stop_worker.wait(0.05):
                pass
            # Admission and shutdown share the registry lock: after this call,
            # accept can no longer publish a client worker outside this set.
            client_threads.begin_shutdown(stop_worker)
            shutdown_deadline = time.monotonic() + 3.0
            _cancel_synchronous_io(accept_thread)
            active_client_threads = client_threads.snapshot()
            for thread in active_client_threads:
                _cancel_synchronous_io(thread)
            accept_thread.join(timeout=max(0.0, shutdown_deadline - time.monotonic()))
            for thread in client_threads.snapshot():
                thread.join(timeout=max(0.0, shutdown_deadline - time.monotonic()))
            stop_reader.set()
            reader.join(timeout=1)
        finally:
            try:
                if not shell_in_job and abort_gate is not None:
                    # Wake a child blocked before ownership was established.
                    # Its script exits instead of falling through to a prompt.
                    try:
                        win32event.SetEvent(abort_gate)
                    except Exception:
                        pass
                if shell_handle is not None:
                    if win32event.WaitForSingleObject(shell_handle, 0) == win32con.WAIT_TIMEOUT:
                        if shell_in_job:
                            win32job.TerminateJobObject(job, 1)
                        else:
                            # Before Job assignment the gated PowerShell has no
                            # user input or child commands; terminate only its
                            # retained process handle instead of signaling a PID.
                            win32api.TerminateProcess(shell_handle, 1)
                        result = win32event.WaitForSingleObject(shell_handle, 5000)
                        if result != win32con.WAIT_OBJECT_0:
                            raise RuntimeError("owned PowerShell cleanup timed out")
                        if shell_in_job and not _wait_for_job_empty(
                                lambda: win32job.QueryInformationJobObject(
                                    job, win32job.JobObjectBasicAccountingInformation
                                )["ActiveProcesses"],
                                timeout=JOB_PROCESS_EXIT_TIMEOUT):
                            raise RuntimeError("owned Job children remain after cleanup")
            finally:
                if shell_handle is not None:
                    shell_handle.Close()
                gate.Close()
                abort_gate.Close()
                job.Close()
    finally:
        if pipe is not None:
            pipe.Close()


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 5 or argv[0] != "--worker":
        return 2
    runtime = None
    session_dir = Path(argv[1])
    _set_stage("runtime_import")
    try:
        runtime = _load_runtime()
        _run(*argv[1:], runtime=runtime)
    except Exception as exc:
        # Worker stderr is redirected to DEVNULL. Do not persist terminal
        # output, environment values, paths, or exception messages.
        if runtime is not None:
            try:
                metadata_path = session_dir / runtime.SESSION_META
                metadata = runtime._read_metadata(metadata_path)
                if metadata.get("state") == "starting":
                    failure = {
                        "stage": _STAGE, "exception": type(exc).__name__,
                    }
                    diagnostic = getattr(exc, "diagnostic", None)
                    if (isinstance(diagnostic, dict)
                            and isinstance(diagnostic.get("stage"), str)
                            and diagnostic["stage"].isidentifier()
                            and isinstance(diagnostic.get("exception"), str)
                            and diagnostic["exception"].isidentifier()):
                        failure["cause_stage"] = diagnostic["stage"]
                        failure["cause"] = diagnostic["exception"]
                    metadata["startup_failure"] = failure
                    runtime._atomic_metadata(metadata_path, metadata)
            except Exception:
                pass
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
