"""Native Windows only. Never imports the Linux JTT runtime or existing state."""
import asyncio
import json
import queue
import subprocess
import sys
import threading
import time
import uuid
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
    raise AssertionError(
        "synthetic terminal marker not observed; "
        f"non_ascii_codepoints={sorted(set(ord(c) for c in snapshot['text'] if ord(c) > 127))}; "
        f"question_marks={snapshot['text'].count('?')}; sequence={snapshot['sequence']}"
    )


def wait_synthetic_marker(client, expected):
    """Wait for a generated result not present contiguously in echoed input."""
    deadline = time.monotonic() + 15
    snapshot = client.call("snapshot")
    while time.monotonic() < deadline:
        assert not snapshot["renderer_failed"], (
            "PTY output reader or VT renderer failed before the result marker; "
            + json.dumps(snapshot["diagnostics"], ensure_ascii=True)
        )
        if expected in snapshot["text"]:
            events = [
                event for event in snapshot["diagnostics"]["synthetic_marker_events"]
                if event["marker"] == expected
            ]
            assert events, (
                "screen displayed the synthetic result but the pywinpty API trace did not; "
                + json.dumps(snapshot["diagnostics"], ensure_ascii=True)
            )
            return snapshot, events[-1]
        snapshot = client.call("snapshot")
        time.sleep(0.02)
    raise AssertionError(
        "generated Korean result marker not observed; command echo is not accepted as output; "
        + json.dumps({
            "expected_marker": expected,
            "marker_codepoints": [ord(char) for char in expected],
            "sequence": snapshot["sequence"],
            "columns": snapshot["columns"],
            "lines": snapshot["lines"],
            "renderer_failed": snapshot["renderer_failed"],
            "diagnostics": snapshot["diagnostics"],
        }, ensure_ascii=True)
    )


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
        client.call("input", identity=identity,
                    text="[Console]::WriteLine(('CP='+[Console]::OutputEncoding.CodePage))\r")
        encoding_snapshot = wait_text(client, "CP=")
        import re
        deadline = time.monotonic() + 10
        while not re.search(r"CP=\d+", encoding_snapshot["text"]):
            assert time.monotonic() < deadline, "console code page response missing"
            encoding_snapshot = client.call("snapshot")
        code_page = re.search(r"CP=(\d+)", encoding_snapshot["text"]).group(1)
        print("console_output_code_page=" + code_page)
        assert code_page == "65001", "fixture shell must use UTF-8, not locale-dependent OEM encoding"
        for malformed in (b"not-json", b"[]"):
            win32file.WriteFile(client.pipe, malformed)
            assert json.loads(win32file.ReadFile(client.pipe, 65536)[1]) == {"error": "invalid_request"}
            assert client.call("input", identity=identity, text="forbidden") == {"error": "input_denied"}
            assert client.call("acquire", identity=identity)["ok"]
        wrong = dict(identity, generation="stale")
        assert client.call("stop", identity=wrong) == {"error": "identity_mismatch"}
        assert client.call("input", identity=wrong, text="forbidden") == {"error": "identity_mismatch"}
        for stale_field, stale_value in (("pid", identity["pid"] + 1),
                                        ("created", identity["created"] + "-stale")):
            stale_identity = dict(identity, **{stale_field: stale_value})
            assert client.call("stop", identity=stale_identity) == {"error": "identity_mismatch"}
            assert client.call("input", identity=stale_identity, text="forbidden") == {
                "error": "identity_mismatch",
            }
        first_nonce = uuid.uuid4().hex[:8]
        first_marker = f"JTT_DIAG_{first_nonce}_한글_END"
        first_command = (
            f"Write-Output ('JTT_DIAG_{first_nonce}_' + "
            "[char]0xD55C + [char]0xAE00 + '_END')"
        )
        assert first_command.isascii(), "the shell response, not command echo, must supply Korean"
        assert first_marker not in first_command, "the result marker must not occur in command echo"
        assert client.call("observe_marker", identity=identity, marker=first_marker) == {
            "ok": True, "synthetic_marker_only": True,
        }
        # Submit this fixture command with Enter in one PTY write, as the
        # original probe did. The expected Korean marker is absent from this
        # ASCII-only source, so an echoed command cannot satisfy the assertion.
        assert client.call("input", identity=identity, text=first_command + "\r")["ok"]
        shown, first_evidence = wait_synthetic_marker(client, first_marker)
        assert first_evidence["same_string_fed_to_pyte"]
        assert first_evidence["api_marker_codepoints"] == [ord(char) for char in first_marker]
        first_row = next(row for row in first_evidence["screen_rows"] if row["exact_marker_on_display"])
        assert first_row["marker_cells_contiguous"]
        first_korean_cells = [cell for cell in first_row["marker_cell_positions"] if cell["char"] in "한글"]
        assert [cell["width"] for cell in first_korean_cells] == [2, 2]
        print("resize_phase=before " + json.dumps(first_evidence, ensure_ascii=True))
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
        resize_snapshot = client.call("snapshot")
        assert resize_snapshot["columns"] == 80
        resize_event = resize_snapshot["diagnostics"]["resize_events"][-1]
        assert resize_event["requested_columns"] == 80
        assert resize_event["screen_columns_after"] == 80
        assert client.call("input", identity=identity,
                           text="[Console]::WriteLine(('SIZE='+[Console]::WindowWidth))\r")["ok"]
        wait_text(client, "SIZE=80")
        resize_nonce = uuid.uuid4().hex[:8]
        resize_marker = f"JTT_DIAG_{resize_nonce}_입력_END"
        resize_command = (
            f"Write-Output ('JTT_DIAG_{resize_nonce}_' + "
            "[char]0xC785 + [char]0xB825 + '_END')"
        )
        assert resize_command.isascii(), "the shell response, not command echo, must supply Korean"
        assert resize_marker not in resize_command, "the result marker must not occur in command echo"
        assert client.call("observe_marker", identity=identity, marker=resize_marker) == {
            "ok": True, "synthetic_marker_only": True,
        }
        assert client.call("input", identity=identity, text=resize_command + "\r")["ok"]
        resized_snapshot, resize_evidence = wait_synthetic_marker(client, resize_marker)
        assert resize_evidence["same_string_fed_to_pyte"]
        assert resize_evidence["api_marker_codepoints"] == [ord(char) for char in resize_marker]
        resize_row = next(row for row in resize_evidence["screen_rows"] if row["exact_marker_on_display"])
        assert resize_row["marker_cells_contiguous"]
        resize_korean_cells = [cell for cell in resize_row["marker_cell_positions"] if cell["char"] in "입력"]
        assert [cell["width"] for cell in resize_korean_cells] == [2, 2]
        assert resize_evidence["last_read_sequence"] >= resize_event["sequence_after"]
        print("resize_phase=after " + json.dumps({
            "resize": resize_event,
            "result": resize_evidence,
            "observed_korean_reads": resized_snapshot["diagnostics"]["read_chunks_with_diagnostic_characters"],
        }, ensure_ascii=True))

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
