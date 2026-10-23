"""Isolated native-Windows ConPTY supervisor entry point.

The shell is started in a bootstrap wait loop. It cannot accept user commands
until the worker has assigned its retained process handle to the owned Job
Object. This closes the pywinpty spawn-to-job window for the supported
PowerShell launch path; it is not an OS sandbox against the same user account.
"""
from __future__ import annotations

import base64
from collections import deque
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import uuid
import zlib


MAX_MESSAGE = 896 * 1024
MAX_REQUEST = 96 * 1024
MAX_POLL_BYTES = 128 * 1024
OUTPUT_BUFFER_BYTES = 256 * 1024
LEASE_SECONDS = 15.0
MAX_COLUMNS = 300
MAX_LINES = 120
BOOTSTRAP_WAIT_MS = 30_000
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


def assign_owned_job(job, process_handle, win32job, win32api, win32event,
                     win32con):
    """Assign only the retained child handle; fail closed on any error."""
    try:
        win32job.AssignProcessToJobObject(job, process_handle)
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
        "function prompt { 'JTT> ' }"
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
    pipe = win32pipe.CreateNamedPipe(
        pipe_name,
        win32pipe.PIPE_ACCESS_DUPLEX | 0x00080000,  # FIRST_PIPE_INSTANCE
        win32pipe.PIPE_TYPE_MESSAGE | win32pipe.PIPE_READMODE_MESSAGE | win32pipe.PIPE_WAIT,
        1, MAX_MESSAGE, MAX_REQUEST, 3000, attributes,
    )
    try:
        actual = win32security.GetSecurityInfo(
            pipe, win32security.SE_KERNEL_OBJECT,
            win32security.DACL_SECURITY_INFORMATION,
        ).GetSecurityDescriptorDacl()
        if (actual is None or actual.GetAceCount() != 1
                or actual.GetAce(0)[2] != owner_sid):
            raise RuntimeError("owner-only named pipe ACL validation failed")

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
        active_client = None
        active_client_id = None
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
                    screen_error = True

            _set_stage("serve_pipe")
            reader = threading.Thread(target=read_output, name="jtt-conpty-reader", daemon=True)
            reader.start()
            ready_event = threading.Event()
            pipe_error = []

            def serve_client():
                nonlocal lease_owner, lease_deadline, active_client, active_client_id
                nonlocal connection_counter
                try:
                    try:
                        win32pipe.ConnectNamedPipe(pipe, None)
                    except pywintypes.error as exc:
                        if exc.winerror != 535:
                            raise
                except Exception as exc:
                    pipe_error.append(type(exc).__name__)
                    ready_event.set()
                    return
                with state_lock:
                    connection_counter += 1
                    connection_id = connection_counter
                try:
                    while not stop_worker.is_set():
                        try:
                            _status, request_bytes = win32file.ReadFile(pipe, MAX_REQUEST)
                            if len(request_bytes) > MAX_REQUEST:
                                raise ValueError("request too large")
                        except pywintypes.error as exc:
                            if exc.winerror in (109, 232, 233):
                                break
                            raise
                        win32security.ImpersonateNamedPipeClient(pipe)
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
                        else:
                            response = dispatch(request, connection_id)
                        payload = json.dumps(response, separators=(",", ":"),
                                             ensure_ascii=True).encode("utf-8")
                        if len(payload) > MAX_MESSAGE:
                            payload = b'{"ok":false,"error":"response_too_large"}'
                        win32file.WriteFile(pipe, payload)
                        if response.get("stopping"):
                            stop_worker.set()
                            break
                except Exception as exc:
                    pipe_error.append(type(exc).__name__)
                finally:
                    with state_lock:
                        if active_client == connection_id:
                            active_client = None
                            active_client_id = None
                        if lease_owner is not None and lease_owner[0] == connection_id:
                            lease_owner = None
                            lease_deadline = 0.0
                    try:
                        win32pipe.DisconnectNamedPipe(pipe)
                    except Exception:
                        pass
                    ready_event.set()

            def dispatch(request, connection_id):
                nonlocal active_client, active_client_id, lease_owner, lease_deadline, state
                client_id = request.get("client_id")
                op = request.get("op")
                if not isinstance(client_id, str) or len(client_id) != 32:
                    return base(error="invalid_client")
                if op == "hello":
                    if active_client not in (None, connection_id):
                        return base(error="client_busy")
                    if request.get("identity") != identity:
                        return base(error="stale_identity")
                    active_client = connection_id
                    active_client_id = client_id
                    return base(ok=True, lease=False)
                if active_client != connection_id:
                    return base(error="hello_required")
                if active_client_id != client_id or request.get("identity") != identity:
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
                        try:
                            data = base64.b64decode(request.get("data", ""), validate=True)
                            text = data.decode("utf-8", errors="strict")
                        except (ValueError, UnicodeError, TypeError):
                            return base(error="invalid_input")
                        if not data or len(data) > 8192:
                            return base(error="invalid_input")
                        terminal.write(text)
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
                    if state not in ("running", "exited"):
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
                    with state_lock:
                        exit_code = int(win32process.GetExitCodeProcess(shell_handle))
                        state = "exited"
                        metadata.update({"state": state, "exit_code": exit_code,
                                         "termination": "requested"})
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

            # Queue one client at a time. Disconnect releases its lease before
            # the next client can attach; simultaneous pipe clients fail closed.
            while not stop_worker.is_set():
                ready_event.clear()
                server_thread = threading.Thread(target=serve_client, daemon=True)
                server_thread.start()
                ready_event.wait()
                server_thread.join(timeout=1)
                if stop_worker.is_set():
                    break
            stop_reader.set()
            reader.join(timeout=2)
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
                        win32event.WaitForSingleObject(shell_handle, 5000)
            finally:
                if shell_handle is not None:
                    shell_handle.Close()
                gate.Close()
                abort_gate.Close()
                job.Close()
    finally:
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
