"""Isolated Windows feasibility probe; NOT the JTT production backend.

Only creates a new no-profile PowerShell in a caller-owned temporary directory.
No existing terminal, registry, credentials, or user configuration is opened.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import uuid

MAX_MESSAGE = 65536


def assign_owned_job(job, handle):
    """Fail closed using only the retained handle of the child we just created."""
    import win32api
    import win32con
    import win32event
    import win32job
    try:
        win32job.AssignProcessToJobObject(job, handle)
    except Exception:
        win32api.TerminateProcess(handle, 1)
        if win32event.WaitForSingleObject(handle, 5000) != win32con.WAIT_OBJECT_0:
            raise RuntimeError("owned child cleanup did not complete")
        raise


def own_sid():
    import win32api
    import win32con
    import win32security
    token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
    try:
        return win32security.GetTokenInformation(token, win32security.TokenUser)[0]
    finally:
        token.Close()


def worker(address, root, session, generation):
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
    from winpty import PTY, Backend

    sid = own_sid()
    acl = win32security.ACL()
    acl.AddAccessAllowedAce(win32security.ACL_REVISION, win32con.GENERIC_ALL, sid)
    descriptor = win32security.SECURITY_DESCRIPTOR()
    descriptor.SetSecurityDescriptorDacl(True, acl, False)
    attributes = pywintypes.SECURITY_ATTRIBUTES()
    attributes.SECURITY_DESCRIPTOR = descriptor
    # Local only, a single server instance, explicit owner SID DACL.
    pipe = win32pipe.CreateNamedPipe(
        address, win32pipe.PIPE_ACCESS_DUPLEX | 0x00080000,
        win32pipe.PIPE_TYPE_MESSAGE | win32pipe.PIPE_READMODE_MESSAGE | 0x8,
        1, MAX_MESSAGE, MAX_MESSAGE, 3000, attributes,
    )
    actual_acl = win32security.GetSecurityInfo(
        pipe, win32security.SE_KERNEL_OBJECT, win32security.DACL_SECURITY_INFORMATION
    ).GetSecurityDescriptorDacl()
    if actual_acl.GetAceCount() != 1 or actual_acl.GetAce(0)[2] != sid:
        pipe.Close()
        raise RuntimeError("owner-only pipe ACL validation failed")

    root = Path(root).resolve()
    for name in ("home", "config", "cache", "tmp", "work"):
        (root / name).mkdir(exist_ok=True)
    system = os.environ["SystemRoot"]
    shell = str(Path(system) / "System32/WindowsPowerShell/v1.0/powershell.exe")
    env = {
        "SystemRoot": system, "WINDIR": system,
        "PATH": str(Path(system) / "System32"),
        "HOME": str(root / "home"), "USERPROFILE": str(root / "home"),
        "APPDATA": str(root / "config"), "LOCALAPPDATA": str(root / "cache"),
        "TEMP": str(root / "tmp"), "TMP": str(root / "tmp"),
        "JTT_HOME": str(root / "config"), "TERM": "xterm",
    }
    terminal = PTY(100, 30, backend=Backend.ConPTY)
    argv = ["-NoLogo", "-NoProfile", "-NoExit", "-Command",
            "Remove-Module PSReadLine -ErrorAction SilentlyContinue; function prompt { 'JTT> ' }"]
    job = win32job.CreateJobObject(attributes, "jtt-poc-job-" + uuid.uuid4().hex)
    if win32api.GetLastError() == 183:
        job.Close()
        raise RuntimeError("unexpected existing job")
    info = win32job.QueryInformationJobObject(job, win32job.JobObjectExtendedLimitInformation)
    info["BasicLimitInformation"]["LimitFlags"] = win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    win32job.SetInformationJobObject(job, win32job.JobObjectExtendedLimitInformation, info)
    handle = None
    stop_reader = threading.Event()
    try:
        spawned = terminal.spawn(shell, cmdline=subprocess.list2cmdline(argv),
                                 cwd=str(root / "work"),
                                 env="\0".join(f"{k}={v}" for k, v in env.items()) + "\0")
        if not spawned:
            raise RuntimeError("ConPTY child creation failed")
        handle = win32api.OpenProcess(
            win32con.PROCESS_QUERY_INFORMATION | win32con.PROCESS_SET_QUOTA
            | win32con.PROCESS_TERMINATE | win32con.SYNCHRONIZE, False, terminal.pid)
        assign_owned_job(job, handle)
        identity = dict(session=session, generation=generation, pid=terminal.pid,
                        created=str(win32process.GetProcessTimes(handle)["CreationTime"]))
        screen = pyte.Screen(100, 30)
        stream = pyte.Stream(screen)
        lock = threading.Lock()
        sequence = 0
        renderer_failed = False

        def read_output():
            nonlocal sequence, renderer_failed
            try:
                while not stop_reader.is_set():
                    data = terminal.read(blocking=False)
                    if data:
                        with lock:
                            stream.feed(data)
                            sequence += 1
                    else:
                        stop_reader.wait(0.01)
            except Exception:
                renderer_failed = True

        threading.Thread(target=read_output, daemon=True).start()
        print("READY", flush=True)
        running = True
        while running:
            try:
                win32pipe.ConnectNamedPipe(pipe, None)
            except pywintypes.error as exc:
                if exc.winerror != 535:  # client connected before accept
                    raise
            leased = False
            try:
                while True:
                    _, data = win32file.ReadFile(pipe, MAX_MESSAGE)
                    win32security.ImpersonateNamedPipeClient(pipe)
                    try:
                        token = win32security.OpenThreadToken(
                            win32api.GetCurrentThread(), win32con.TOKEN_QUERY, True)
                        try:
                            if win32security.GetTokenInformation(token, win32security.TokenUser)[0] != sid:
                                raise RuntimeError("client SID rejected")
                        finally:
                            token.Close()
                    finally:
                        win32security.RevertToSelf()
                    try:
                        request = json.loads(data.decode("utf-8"))
                        if not isinstance(request, dict):
                            raise ValueError("object required")
                    except (ValueError, UnicodeError):
                        leased = False
                        win32file.WriteFile(pipe, b'{"error":"invalid_request"}')
                        continue
                    op = request.get("op")
                    if op != "snapshot" and request.get("identity") != identity:
                        response = {"error": "identity_mismatch"}
                    elif op not in ("snapshot", "stop") and win32event.WaitForSingleObject(handle, 0) != win32con.WAIT_TIMEOUT:
                        response = {"error": "process_exited"}
                    elif op == "acquire":
                        leased = not renderer_failed
                        response = {"ok": leased}
                    elif op == "input":
                        text = request.get("text", "")
                        if not leased or renderer_failed or not isinstance(text, str) or len(text) > 4096:
                            response = {"error": "input_denied"}
                        else:
                            terminal.write(text)
                            response = {"ok": True}
                    elif op == "resize":
                        cols, rows = request.get("columns"), request.get("lines")
                        if not leased or (cols, rows) not in ((100, 30), (80, 24), (160, 44)):
                            response = {"error": "resize_denied"}
                        else:
                            with lock:
                                terminal.set_size(cols, rows)
                                screen.resize(lines=rows, columns=cols)
                            response = {"ok": True}
                    elif op == "stop":
                        win32job.TerminateJobObject(job, 0)
                        if win32event.WaitForSingleObject(handle, 5000) != win32con.WAIT_OBJECT_0:
                            raise RuntimeError("owned job cleanup did not complete")
                        response = {"ok": True, "stopped": True}
                        running = False
                    elif op == "snapshot":
                        with lock:
                            response = dict(identity=identity, sequence=sequence,
                                            text="\n".join(screen.display),
                                            cursor=[screen.cursor.x, screen.cursor.y],
                                            columns=screen.columns, lines=screen.lines,
                                            renderer_failed=renderer_failed, owner_only_acl=True)
                    else:
                        response = {"error": "unknown_operation"}
                    win32file.WriteFile(pipe, json.dumps(response, ensure_ascii=False).encode("utf-8"))
                    if not running:
                        break
            except pywintypes.error as exc:
                print(f"pipe_error:{exc.winerror}:{exc.funcname}", file=sys.stderr, flush=True)
                if exc.winerror not in (109, 232, 233):
                    raise
            finally:
                leased = False
                win32pipe.DisconnectNamedPipe(pipe)
    finally:
        stop_reader.set()
        job.Close()  # only this probe's retained job, never a PID lookup kill
        if handle is not None:
            handle.Close()
        pipe.Close()


class Client:
    def __init__(self, address):
        import win32con
        import win32file
        import win32pipe
        win32pipe.WaitNamedPipe(address, 5000)
        self.pipe = win32file.CreateFile(address, win32con.GENERIC_READ | win32con.GENERIC_WRITE,
                                        0, None, win32con.OPEN_EXISTING, 0, None)
        win32pipe.SetNamedPipeHandleState(self.pipe, win32pipe.PIPE_READMODE_MESSAGE, None, None)

    def call(self, op, **fields):
        import win32file
        win32file.WriteFile(self.pipe, json.dumps(dict(op=op, **fields)).encode("utf-8"))
        return json.loads(win32file.ReadFile(self.pipe, MAX_MESSAGE)[1])

    def close(self):
        self.pipe.Close()


def start(root):
    """Explicit creation only; callers own and must finally clean this process."""
    if sys.platform != "win32":
        raise RuntimeError("native Windows required; no WSL fallback")
    session, generation = uuid.uuid4().hex, uuid.uuid4().hex
    address = rf"\\.\pipe\jtt-poc-{session}"
    process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--worker",
                                address, str(root), session, generation],
                               stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True)
    return process, address


if __name__ == "__main__":
    if sys.platform != "win32" or len(sys.argv) != 6 or sys.argv[1] != "--worker":
        raise SystemExit("test-owned Windows worker only")
    worker(*sys.argv[2:])
