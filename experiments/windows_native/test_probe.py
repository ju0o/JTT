"""Native Windows only. Never imports the Linux JTT runtime or existing state."""
import asyncio
import json
import queue
import subprocess
import sys
import threading
import time
from importlib.metadata import version

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="native Windows required")


def wait_text(client, expected):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        snapshot = client.call("snapshot")
        assert not snapshot["renderer_failed"]
        if expected in snapshot["text"]:
            return snapshot
        time.sleep(0.02)
    raise AssertionError("synthetic terminal marker not observed")


@pytest.mark.parametrize("shell_exits_first", [False, True])
def test_native_conpty_reconnect_identity_and_textual(tmp_path, shell_exits_first):
    from probe import Client, start
    from textual.app import App, ComposeResult
    from textual.containers import Horizontal
    from textual.widgets import Static
    import win32api
    import win32con
    import win32event
    import win32file

    process, address = start(tmp_path)
    client = None
    shell_handle = None
    try:
        ready = queue.Queue()
        threading.Thread(target=lambda: ready.put(process.stdout.readline()), daemon=True).start()
        assert ready.get(timeout=15).strip() == "READY", "worker initialization failed"
        # Bounded connection readiness, not a sleep hiding startup failures.
        deadline = time.monotonic() + 15
        while True:
            try:
                client = Client(address)
                break
            except Exception:
                assert process.poll() is None, "probe worker exited during startup"
                if time.monotonic() >= deadline:
                    raise
        first = wait_text(client, "JTT>")
        identity = first["identity"]
        shell_handle = win32api.OpenProcess(win32con.SYNCHRONIZE, False, identity["pid"])
        assert first["owner_only_acl"]
        assert client.call("input", identity=identity, text="forbidden") == {"error": "input_denied"}
        assert client.call("acquire", identity=identity)["ok"]
        for malformed in (b"not-json", b"[]"):
            win32file.WriteFile(client.pipe, malformed)
            assert json.loads(win32file.ReadFile(client.pipe, 65536)[1]) == {"error": "invalid_request"}
            assert client.call("input", identity=identity, text="forbidden") == {"error": "input_denied"}
            assert client.call("acquire", identity=identity)["ok"]
        wrong = dict(identity, generation="stale")
        assert client.call("stop", identity=wrong) == {"error": "identity_mismatch"}
        assert client.call("input", identity=wrong, text="forbidden") == {"error": "identity_mismatch"}
        command = "[Console]::WriteLine(('JTT_'+'NATIVE_OK')); [Console]::WriteLine([char]0xD55C+[string][char]0xAE00)\r"
        assert client.call("input", identity=identity, text=command)["ok"]
        shown = wait_text(client, "JTT_NATIVE_OK")
        shown = wait_text(client, "한글")
        client.close()
        client = None
        assert process.poll() is None
        assert win32event.WaitForSingleObject(shell_handle, 0) == win32con.WAIT_TIMEOUT
        # A separate UI-client process reconnects, then exits without the shell.
        result = subprocess.run(
            [sys.executable, "-c",
             "from probe import Client; import json,sys; c=Client(sys.argv[1]); "
             "print(json.dumps(c.call('snapshot')['identity'])); c.close()", address],
            cwd=str(__import__("pathlib").Path(__file__).resolve().parent),
            capture_output=True, text=True, timeout=10, check=True)
        assert json.loads(result.stdout) == identity
        assert process.poll() is None
        client = Client(address)
        again = client.call("snapshot")
        assert again["identity"] == identity
        assert again["sequence"] >= shown["sequence"]
        assert client.call("input", identity=identity, text="forbidden") == {"error": "input_denied"}
        assert client.call("acquire", identity=identity)["ok"]
        assert client.call("resize", identity=identity, columns=80, lines=24)["ok"]
        assert client.call("snapshot")["columns"] == 80
        assert client.call("input", identity=identity,
                           text="[Console]::WriteLine(('SIZE='+[Console]::WindowWidth)); Write-Output ('입'+'력')\r")["ok"]
        wait_text(client, "SIZE=80")
        wait_text(client, "입력")

        # UI-framework feasibility only, not the approved JTT UI or native WT visual QA.
        class ProbeUI(App):
            CSS = "#left {width: 20%;} #cards {width: 30%;} #right {width: 50%;}"

            def compose(self) -> ComposeResult:
                with Horizontal():
                    yield Static("Windows\nWindow 1", id="left")
                    yield Static("Terminal 1", id="cards")
                    yield Static(again["text"], id="right", markup=False)

        async def render():
            for size in ((160, 44), (80, 24)):
                app = ProbeUI()
                async with app.run_test(size=size) as pilot:
                    await pilot.pause()
                    assert app.query_one("#right").size.width == size[0] // 2

        asyncio.run(render())
        if shell_exits_first:
            assert client.call("input", identity=identity, text="exit\r")["ok"]
            assert win32event.WaitForSingleObject(shell_handle, 5000) == win32con.WAIT_OBJECT_0
            assert client.call("stop", identity=wrong) == {"error": "identity_mismatch"}
            assert client.call("input", identity=identity, text="forbidden") == {"error": "process_exited"}
        assert client.call("stop", identity=identity)["stopped"]
        client.close()
        client = None
        assert process.wait(timeout=10) == 0
        assert win32event.WaitForSingleObject(shell_handle, 0) == win32con.WAIT_OBJECT_0
        print(json.dumps({"platform": sys.platform, "supervisor_pid": process.pid,
                          "shell_pid": identity["pid"], "same_identity_reconnect": True,
                          "conpty": True, "owner_only_pipe_dacl": True,
                          "wrong_generation_rejected": True, "lease_revoked": True,
                          "korean_output": True, "cleanup": True,
                          "shell_exits_first": shell_exits_first,
                          "textual_headless_sizes": ["160x44", "80x24"],
                          "native_windows_terminal_visual": "NOT_TESTED",
                          "versions": {p: version(p) for p in ("pywinpty", "pywin32", "textual", "pyte")}}))
    finally:
        if client is not None:
            client.close()
        if process.poll() is None:
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.terminate()  # only our retained worker handle; job kills its shell
                process.wait(timeout=10)
        if process.returncode:
            error = process.stderr.read().splitlines()
            print("worker_error:", "\n".join(error[-4:]) if error else "no diagnostic")
        if shell_handle is not None:
            assert win32event.WaitForSingleObject(shell_handle, 5000) == win32con.WAIT_OBJECT_0
            shell_handle.Close()


def test_job_assignment_failure_cleans_only_owned_child(tmp_path, monkeypatch):
    import os
    from pathlib import Path
    import win32api
    import win32con
    import win32event
    import win32job
    from probe import assign_owned_job

    shell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    process = subprocess.Popen([str(shell), "-NoProfile", "-NonInteractive", "-Command", "Start-Sleep 60"],
                               cwd=tmp_path, stdin=subprocess.DEVNULL,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    handle = win32api.OpenProcess(win32con.PROCESS_TERMINATE | win32con.SYNCHRONIZE, False, process.pid)
    try:
        def fail_assignment(job, child):
            raise RuntimeError("injected assignment failure")

        monkeypatch.setattr(win32job, "AssignProcessToJobObject", fail_assignment)
        with pytest.raises(RuntimeError, match="injected assignment failure"):
            assign_owned_job(None, handle)
        assert win32event.WaitForSingleObject(handle, 0) == win32con.WAIT_OBJECT_0
        process.wait(timeout=5)
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=5)
        handle.Close()
