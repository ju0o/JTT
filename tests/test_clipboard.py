"""Result copy uses the extracted body and an honest clipboard result."""

import os
import subprocess
from pathlib import Path

import pytest

from tmux_agent_tower.adapters.base import ResultCandidate
from tmux_agent_tower.clipboard import choose_clipboard_tool, copy_text, resolve_terminal_target


def _local_client():
    from tmux_agent_tower.clipboard_dest import CopyClient

    return CopyClient("/dev/pts/0", 100, False, False), (("/dev/pts/0", 100),)


def _ambiguous_client():
    from tmux_agent_tower.clipboard_dest import CopyClient

    clients = (("/dev/pts/1", 1), ("/dev/pts/2", 2))
    return CopyClient(None, None, False, True), clients
from tmux_agent_tower.detection.result import ResultTracker
from tmux_agent_tower.ui.control_view import _copy_result, _show_result


BODY = "한글 결과\n\n```python\nprint('tower')\n```\n"


class _Tower:
    def __init__(self, tracker, text_state="ready"):
        self.results = tracker
        self.rows = [{
            "key": "%9", "pane_id": "%9", "pane_pid": "900", "session": "isolated",
            "result_state": text_state, "remote": False,
        }]

    def load(self):
        return None


def _ready_tower():
    tracker = ResultTracker()
    tracker.observe("%9", "IDLE", ResultCandidate(BODY, "fp-1"))
    return _Tower(tracker), tracker


def _mock_complete_result(monkeypatch):
    payload = {
        "state": "ready", "text": BODY, "complete": True,
        "turn_complete": True, "body_complete": True, "fingerprint": "fp-1",
    }
    monkeypatch.setattr(
        "tmux_agent_tower.ui.control_view.get_result",
        lambda *_args, **_kwargs: (True, "", payload),
    )


def test_choose_clip_on_wsl_before_wayland():
    which = lambda name: "/usr/bin/" + name if name in ("clip.exe", "wl-copy", "xclip") else None
    tool = choose_clipboard_tool(which, {"WAYLAND_DISPLAY": "wayland-0", "TOWER_UNAME": "microsoft-standard"}, "linux")
    assert tool == "clip.exe"


def test_copy_sends_the_body_on_stdin_and_marks_read_only_on_success(monkeypatch):
    seen = []

    def run(argv, data):
        seen.append((argv, data))
        return 0

    monkeypatch.setattr(
        "tmux_agent_tower.clipboard.choose_clipboard_tool",
        lambda which, environ, system: "clip.exe",
    )
    monkeypatch.setattr("tmux_agent_tower.clipboard.shutil.which", lambda name: "clip.exe")
    outcome = copy_text(BODY, run=run, which=lambda name: "clip.exe")
    assert outcome.clipboard is True
    assert outcome.buffer is False
    argv, data = seen[0]
    assert "shell" not in argv
    assert BODY.encode("utf-16le") == data
    assert BODY.encode("utf-8") not in [part.encode("utf-8") if isinstance(part, str) else part for part in argv]

    tower, tracker = _ready_tower()
    _mock_complete_result(monkeypatch)
    monkeypatch.setattr("tmux_agent_tower.ui.control_view.copy_text", lambda text, **_kwargs: outcome)
    monkeypatch.setattr("tmux_agent_tower.ui.control_view.load_preference", lambda: "auto")
    monkeypatch.setattr("tmux_agent_tower.ui.control_view.observe", lambda: _local_client())
    notice = _copy_result(tower, "%9")
    assert notice == "최신 답변을 Windows 클립보드에 복사했습니다."
    assert tracker.snapshot("%9").state == "read"
    assert tracker.snapshot("%9").text == BODY


def test_copy_diagnostic_failure_blocks_external_clipboard_write(monkeypatch):
    writes = []
    attempts = []
    monkeypatch.setattr("tmux_agent_tower.clipboard.choose_clipboard_tool", lambda *_: "clip.exe")
    monkeypatch.setattr("tmux_agent_tower.clipboard.shutil.which", lambda _name: "clip.exe")

    def run(argv, data):
        writes.append((argv, data))
        return 0

    outcome = copy_text(
        BODY, destination="local_host", allow_buffer_fallback=False,
        run=run, which=lambda _name: "clip.exe",
        diagnostic_callback=lambda *fields: attempts.append(fields) or False,
    )

    assert len(attempts) == 1
    assert attempts[0][0:2] == ("local_host", "clip.exe")
    assert attempts[0][2] == len(BODY.encode("utf-8"))
    assert writes == []
    assert outcome.clipboard is False


def test_failed_clipboard_uses_the_buffer_and_does_not_mark_read(monkeypatch):
    def run(argv, data):
        if argv[0] == "tmux":
            return 0
        return 1

    monkeypatch.setattr(
        "tmux_agent_tower.clipboard.choose_clipboard_tool",
        lambda which, environ, system: "wl-copy",
    )
    outcome = copy_text("line\n둘", run=run, which=lambda name: "/bin/wl-copy")
    assert outcome.clipboard is False
    assert outcome.buffer is True
    assert outcome.tool == "tmux"

    tower, tracker = _ready_tower()
    _mock_complete_result(monkeypatch)
    monkeypatch.setattr("tmux_agent_tower.ui.control_view.copy_text", lambda text, **_kwargs: outcome)
    monkeypatch.setattr("tmux_agent_tower.ui.control_view.load_preference", lambda: "auto")
    monkeypatch.setattr("tmux_agent_tower.ui.control_view.observe", lambda: _local_client())
    notice = _copy_result(tower, "%9")
    assert tracker.snapshot("%9").state == "ready"
    assert "Tower 복사함" in notice
    assert "클립보드" not in notice


def test_neither_clipboard_nor_buffer_is_not_success(monkeypatch):
    monkeypatch.setattr(
        "tmux_agent_tower.clipboard.choose_clipboard_tool",
        lambda which, environ, system: "",
    )
    outcome = copy_text("x", run=lambda argv, data: 1, which=lambda name: None)
    assert outcome.clipboard is False and outcome.buffer is False
    tower, tracker = _ready_tower()
    _mock_complete_result(monkeypatch)
    monkeypatch.setattr("tmux_agent_tower.ui.control_view.copy_text", lambda text, **_kwargs: outcome)
    monkeypatch.setattr("tmux_agent_tower.ui.control_view.load_preference", lambda: "auto")
    monkeypatch.setattr("tmux_agent_tower.ui.control_view.observe", lambda: _local_client())
    notice = _copy_result(tower, "%9")
    assert tracker.snapshot("%9").state == "ready"
    assert "✓" not in notice


def test_showing_the_result_marks_it_read_without_copying(monkeypatch):
    from tmux_agent_tower.ui import control_view

    tower, tracker = _ready_tower()
    monkeypatch.setattr(control_view, "get_result", lambda *_args, **_kwargs: (True, "", {
        "state": "ready", "text": BODY, "complete": True,
        "turn_complete": True, "body_complete": True, "fingerprint": "fp-1",
    }))
    notice, shown = _show_result(tower, "%9")
    assert shown is True
    assert tracker.snapshot("%9").state == "read"
    assert BODY.split("\n", 1)[0] not in notice or True


@pytest.mark.skipif(
    os.environ.get("TOWER_RUN_CLIPBOARD_INTEGRATION") != "1",
    reason="set TOWER_RUN_CLIPBOARD_INTEGRATION=1 to write and read the real Windows clipboard",
)
def test_windows_clipboard_round_trip_keeps_korean_and_a_code_block():
    import hashlib
    import shutil

    from tmux_agent_tower.clipboard import _read_tool_clipboard, normalize_newlines

    if shutil.which("clip.exe") is None:
        pytest.skip("clip.exe is unavailable")
    sample = "한글 결과\n\n```python\nprint('tower')\n```\n둘째 줄"
    outcome = copy_text(sample)
    assert outcome.clipboard is True
    assert outcome.tool == "clip.exe"
    pasted = _read_tool_clipboard("clip.exe", shutil.which)
    assert pasted is not None
    assert "\ufffd" not in pasted
    assert normalize_newlines(pasted) == sample
    assert hashlib.sha256(normalize_newlines(pasted).encode()).hexdigest() == hashlib.sha256(
        sample.encode()
    ).hexdigest()


def test_phone_copy_still_uses_the_browser_clipboard():
    from tmux_agent_tower.server.webui import PAGE_HTML

    source = (Path(__file__).resolve().parents[1] / "src" / "tmux_agent_tower" / "server" / "webui.py").read_text(
        encoding="utf-8"
    )
    assert "navigator.clipboard" in PAGE_HTML
    assert "clip.exe" not in PAGE_HTML
    assert "clipboard_destination" not in source
    assert "copy_text(" not in source


def test_auto_ssh_uses_tower_buffer_and_a_local_client_keeps_clip(monkeypatch):
    from tmux_agent_tower.clipboard import terminal_clipboard_client

    seen = []

    def run(argv, data):
        seen.append((list(argv), data))
        return 0

    body = "한글\nbody!"
    outcome = copy_text(
        "한글\nbody\x1b[31m!\x07",
        run=run,
        client="/dev/pts/11",
        ssh=True,
        which=lambda name: "clip.exe",
        read_buffer=lambda name: body.encode("utf-8"),
    )
    assert outcome.clipboard is False
    assert outcome.buffer is True
    assert outcome.tool == "tmux"
    assert outcome.terminal_requested is False
    assert [call[0][0] for call in seen] == ["tmux"]
    assert seen[0][0][:3] == ["tmux", "load-buffer", "-b"]
    assert seen[0][1] == body.encode("utf-8")
    assert all("-w" not in argv for argv, _data in seen)
    assert not any(argv[0] == "clip.exe" or (argv[0].endswith("clip.exe")) for argv, _data in seen)

    seen.clear()
    monkeypatch.setattr(
        "tmux_agent_tower.clipboard.choose_clipboard_tool",
        lambda which, environ, system: "clip.exe",
    )
    host = copy_text("local body", run=run, which=lambda name: "clip.exe")
    assert host.tool == "clip.exe"
    assert seen[0][0] == ["clip.exe"]
    assert "-w" not in seen[0][0]

    assert terminal_clipboard_client(["/dev/pts/11\t5"], ssh_check=lambda pid: True) == "/dev/pts/11"
    assert terminal_clipboard_client(["/dev/pts/1\t5"], ssh_check=lambda pid: False) == "/dev/pts/1"
    assert terminal_clipboard_client(["/dev/pts/1\t5", "/dev/pts/11\t9"], ssh_check=lambda pid: True) is None
    focused = resolve_terminal_target(
        ["/dev/pts/1\t5\tattached", "/dev/pts/11\t9\tattached,focused"],
        ssh_check=lambda pid: pid == 9,
    )
    assert focused.ambiguous is True and focused.tty is None
    ambiguous = resolve_terminal_target(
        ["/dev/pts/1\t5\tattached,focused", "/dev/pts/11\t9\tattached,focused"],
        ssh_check=lambda pid: True,
    )
    assert ambiguous.ambiguous is True and ambiguous.tty is None
    one_ssh = resolve_terminal_target(
        ["/dev/pts/1\t5\tattached,focused", "/dev/pts/3\t9\tattached,focused"],
        ssh_check=lambda pid: pid == 9,
    )
    assert one_ssh.ambiguous is True and one_ssh.tty is None and one_ssh.ssh is False


def test_auto_ssh_with_bridge_uses_one_terminal_destination(monkeypatch):
    from tmux_agent_tower.clipboard import CopyOutcome

    sent = []
    monkeypatch.setattr(
        "tmux_agent_tower.clipboard._post_clipboard_bridge",
        lambda port, token, data: sent.append((port, token, data)) or True,
    )
    outcome = copy_text(
        "workstation-b clipboard",
        client="/dev/pts/3",
        ssh=True,
        destination="auto",
        bridge_port=43210,
        bridge_token="a-very-private-bridge-token-123456789",
    )
    assert outcome == CopyOutcome(True, False, "terminal", False, False)
    assert sent == [(43210, "a-very-private-bridge-token-123456789", b"workstation-b clipboard")]


def test_bridge_failure_does_not_write_a_second_destination(monkeypatch):
    tmux_calls = []
    monkeypatch.setattr("tmux_agent_tower.clipboard._post_clipboard_bridge", lambda *_args: False)
    outcome = copy_text(
        "workstation-b clipboard",
        client="/dev/pts/3",
        ssh=True,
        destination="current_terminal",
        bridge_port=43210,
        bridge_token="a-very-private-bridge-token-123456789",
        run=lambda argv, data: tmux_calls.append((argv, data)) or 0,
    )
    assert outcome.clipboard is False and outcome.buffer is False
    assert tmux_calls == []


def test_bridge_request_keeps_token_out_of_process_arguments(monkeypatch):
    import hashlib
    import json

    from tmux_agent_tower.clipboard import _post_clipboard_bridge

    token = "a-very-private-bridge-token-123456789"
    payload = "한글 payload".encode("utf-8")
    seen = {}

    def run(argv, **kwargs):
        seen.update(argv=argv, **kwargs)
        return type("Proc", (), {
            "returncode": 0,
            "stdout": json.dumps({
                "success": True,
                "payload_sha256": hashlib.sha256(payload).hexdigest(),
            }).encode("ascii"),
        })()

    monkeypatch.setattr("tmux_agent_tower.clipboard._wsl_fallback", lambda _name: "powershell.exe")
    monkeypatch.setattr("tmux_agent_tower.clipboard.subprocess.run", run)
    assert _post_clipboard_bridge(43210, token, payload) is True
    assert token not in " ".join(seen["argv"])
    assert seen["input"] == token.encode("ascii") + b"\n" + payload
    assert seen["timeout"] == 10


def test_explicit_host_provider_can_fail_without_writing_a_buffer(monkeypatch):
    from tmux_agent_tower.clipboard import copy_text

    seen = []
    monkeypatch.setattr(
        "tmux_agent_tower.clipboard.choose_clipboard_tool", lambda *_args: "wl-copy"
    )
    outcome = copy_text(
        "payload",
        which=lambda name: "/usr/bin/wl-copy" if name == "wl-copy" else None,
        run=lambda argv, _data: seen.append(argv) or 0,
        verify=lambda _tool, _text: False,
        destination="local_host",
        allow_buffer_fallback=False,
    )
    assert outcome.clipboard is False and outcome.buffer is False
    assert seen == [["/usr/bin/wl-copy"]]


def test_host_bridge_without_provider_does_not_write_a_buffer(monkeypatch):
    from tmux_agent_tower.clipboard import copy_text

    calls = []
    outcome = copy_text(
        "payload", which=lambda _name: None,
        run=lambda argv, _data: calls.append(argv) or 0,
        destination="local_host", allow_buffer_fallback=False,
    )
    assert outcome.clipboard is False and outcome.buffer is False
    assert calls == []


def test_host_bridge_without_provider_does_not_write_a_buffer(monkeypatch):
    from tmux_agent_tower.clipboard import copy_text

    calls = []
    outcome = copy_text(
        "payload", which=lambda _name: None,
        run=lambda argv, _data: calls.append(argv) or 0,
        destination="local_host", allow_buffer_fallback=False,
    )
    assert outcome.clipboard is False and outcome.buffer is False
    assert calls == []


def test_background_clipboard_owners_do_not_hold_captured_pipes(monkeypatch):
    from tmux_agent_tower.clipboard import _default_run

    seen = {}

    def run(argv, **kwargs):
        seen.update(argv=argv, **kwargs)
        return type("Proc", (), {"returncode": 0, "stderr": None})()

    monkeypatch.setattr("tmux_agent_tower.clipboard.subprocess.run", run)
    assert _default_run(["/usr/bin/wl-copy"], b"payload") == 0
    assert seen["stdout"] == subprocess.DEVNULL
    assert seen["stderr"] == subprocess.DEVNULL
    assert "capture_output" not in seen


def test_wayland_socket_is_discovered_when_tmux_lacks_display_variables(monkeypatch, tmp_path):
    import socket

    from tmux_agent_tower.clipboard import _environ

    socket_path = tmp_path / "wayland-1"
    server = socket.socket(socket.AF_UNIX)
    try:
        server.bind(str(socket_path))
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
        monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
        monkeypatch.delenv("DISPLAY", raising=False)
        env = _environ()
        assert env["WAYLAND_DISPLAY"] == "wayland-1"
        assert env["XDG_RUNTIME_DIR"] == str(tmp_path)
    finally:
        server.close()


def test_wayland_socket_detection_does_not_guess_between_sessions(monkeypatch, tmp_path):
    import socket

    from tmux_agent_tower.clipboard import _environ

    servers = [socket.socket(socket.AF_UNIX), socket.socket(socket.AF_UNIX)]
    try:
        for server, name in zip(servers, ("wayland-0", "wayland-1")):
            server.bind(str(tmp_path / name))
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
        monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
        monkeypatch.delenv("DISPLAY", raising=False)
        assert "WAYLAND_DISPLAY" not in _environ()
    finally:
        for server in servers:
            server.close()


def test_wayland_socket_is_discovered_when_tmux_lacks_display_variables(monkeypatch, tmp_path):
    import socket

    from tmux_agent_tower.clipboard import _environ

    socket_path = tmp_path / "wayland-1"
    server = socket.socket(socket.AF_UNIX)
    try:
        server.bind(str(socket_path))
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
        monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
        monkeypatch.delenv("DISPLAY", raising=False)
        env = _environ()
        assert env["WAYLAND_DISPLAY"] == "wayland-1"
        assert env["XDG_RUNTIME_DIR"] == str(tmp_path)
    finally:
        server.close()


def test_explicit_host_provider_can_fail_without_writing_a_buffer(monkeypatch):
    from tmux_agent_tower.clipboard import copy_text

    seen = []
    monkeypatch.setattr(
        "tmux_agent_tower.clipboard.choose_clipboard_tool", lambda *_args: "wl-copy"
    )
    outcome = copy_text(
        "payload",
        which=lambda name: "/usr/bin/wl-copy" if name == "wl-copy" else None,
        run=lambda argv, _data: seen.append(argv) or 0,
        verify=lambda _tool, _text: False,
        destination="local_host",
        allow_buffer_fallback=False,
    )
    assert outcome.clipboard is False and outcome.buffer is False
    assert seen == [["/usr/bin/wl-copy"]]


def test_clients_are_scoped_to_tower_session(monkeypatch):
    from tmux_agent_tower.clipboard import _client_lines

    calls = []

    def run_tmux(args, **_kwargs):
        calls.append(args)
        if args[0] == "display-message":
            return "$4"
        return "/dev/pts/7\t77\tattached"

    monkeypatch.setenv("TMUX_PANE", "%12")
    monkeypatch.setattr("tmux_agent_tower.tmux.capture.run_tmux", run_tmux)
    assert _client_lines() == ["/dev/pts/7\t77\tattached"]
    assert calls[0] == ["display-message", "-p", "-t", "%12", "#{session_id}"]
    assert calls[1][:3] == ["list-clients", "-t", "$4"]


def test_oversized_terminal_payload_falls_back_without_claiming_success():
    seen = []

    def run(argv, data):
        seen.append(list(argv))
        return 0 if argv[:2] == ["tmux", "load-buffer"] and "-w" not in argv else 1

    outcome = copy_text(
        "a" * (48 * 1024 + 8),
        run=run,
        client="/dev/pts/11",
        ssh=True,
        which=lambda name: None,
    )
    assert outcome.clipboard is False
    assert outcome.buffer is True
    assert seen[0][:3] == ["tmux", "load-buffer", "-b"]
    assert "-w" not in seen[0]
    assert all("-w" not in argv for argv in seen)


def test_copy_notice_names_the_destination_that_actually_worked(monkeypatch):
    from tmux_agent_tower.clipboard import CopyOutcome

    tower, _tracker = _ready_tower()
    _mock_complete_result(monkeypatch)
    monkeypatch.setattr("tmux_agent_tower.ui.control_view.load_preference", lambda: "auto")
    monkeypatch.setattr("tmux_agent_tower.ui.control_view.observe", lambda: _local_client())
    monkeypatch.setattr(
        "tmux_agent_tower.ui.control_view.copy_text",
        lambda text, **_kwargs: CopyOutcome(True, False, "terminal"),
    )
    assert "현재 접속한 터미널" in _copy_result(tower, "%9")

    tower, _tracker = _ready_tower()
    _mock_complete_result(monkeypatch)
    monkeypatch.setattr("tmux_agent_tower.ui.control_view.load_preference", lambda: "auto")
    monkeypatch.setattr("tmux_agent_tower.ui.control_view.observe", lambda: _local_client())
    monkeypatch.setattr(
        "tmux_agent_tower.ui.control_view.copy_text",
        lambda text, **_kwargs: CopyOutcome(True, False, "clip.exe"),
    )
    assert _copy_result(tower, "%9") == "최신 답변을 Windows 클립보드에 복사했습니다."

    tower, _tracker = _ready_tower()
    _mock_complete_result(monkeypatch)
    monkeypatch.setattr("tmux_agent_tower.ui.control_view.load_preference", lambda: "auto")
    monkeypatch.setattr("tmux_agent_tower.ui.control_view.observe", lambda: _local_client())
    monkeypatch.setattr(
        "tmux_agent_tower.ui.control_view.copy_text",
        lambda text, **_kwargs: CopyOutcome(False, True, "tmux"),
    )
    notice = _copy_result(tower, "%9")
    assert "Tower 복사함" in notice
    assert "클립보드" not in notice


def test_clipboard_write_happens_only_from_the_copy_action():
    root = Path(__file__).resolve().parents[1] / "src"
    hits = sorted(path.name for path in root.rglob("*.py") if "copy_text(" in path.read_text(encoding="utf-8"))
    assert hits == ["clipboard.py", "clipboard_dest.py", "control_view.py"]


def test_windows_sshd_parent_counts_when_linux_has_no_ssh_env(monkeypatch):
    from tmux_agent_tower.clipboard import _process_has_ssh_evidence

    monkeypatch.setattr("tmux_agent_tower.clipboard._read_proc", lambda path: b"")
    monkeypatch.setattr("tmux_agent_tower.clipboard._process_start_unix", lambda pid: 1_700_000_100)
    monkeypatch.setattr(
        "tmux_agent_tower.clipboard._ssh_wsl_start_seconds",
        lambda: frozenset({1_700_000_100}),
    )
    assert _process_has_ssh_evidence(4242) is True

    monkeypatch.setattr(
        "tmux_agent_tower.clipboard._ssh_wsl_start_seconds",
        lambda: frozenset({1_700_000_000}),
    )
    assert _process_has_ssh_evidence(4242) is False


def test_ssh_evidence_is_ancestry_not_a_bare_environment_name(monkeypatch):
    from tmux_agent_tower.clipboard import _pid_came_through_ssh

    def plain(path):
        if path.endswith("/comm"):
            return b"bash\n"
        if path.endswith("/environ"):
            return b"HOME=/home\0"
        if path.endswith("/stat"):
            return b"5 (bash) S 1 1 1 0\n"
        return b""

    monkeypatch.setattr("tmux_agent_tower.clipboard._read_proc", plain)
    assert _pid_came_through_ssh(5) is False

    def ssh(path):
        if path == "/proc/8/environ":
            return b"SSH_CONNECTION=192.0.2.1 1 192.0.2.2 22\0"
        if path.endswith("/stat"):
            return b"8 (bash) S 1 1 1 0\n"
        return b"bash\n"

    monkeypatch.setattr("tmux_agent_tower.clipboard._read_proc", ssh)
    assert _pid_came_through_ssh(8) is True


def test_clipboard_text_that_was_already_there_is_not_an_osc52_success(monkeypatch):
    seen = []

    def run(argv, data):
        seen.append(list(argv))
        return 0

    monkeypatch.setattr("tmux_agent_tower.clipboard._read_tool_clipboard", lambda tool, which: "hello")
    monkeypatch.setattr("tmux_agent_tower.clipboard.choose_clipboard_tool", lambda *args: "clip.exe")
    outcome = copy_text("hello", run=run, client="/dev/pts/1", which=lambda name: "clip.exe")
    assert outcome.tool == "clip.exe"
    assert outcome.clipboard is True
    assert outcome.terminal_requested is False
    assert all(argv[:2] != ["tmux", "load-buffer"] for argv in seen)


def test_wayland_or_host_change_is_not_terminal_success(monkeypatch):
    """A readable host clipboard is not a terminal copy."""

    seen = []

    def run(argv, data):
        seen.append(list(argv))
        return 0

    monkeypatch.setattr("tmux_agent_tower.clipboard.choose_clipboard_tool", lambda *args: "clip.exe")
    monkeypatch.setattr("tmux_agent_tower.clipboard._read_tool_clipboard", lambda tool, which: "hello")
    outcome = copy_text("hello", run=run, client="/dev/pts/1", which=lambda name: "clip.exe")
    assert outcome.tool == "clip.exe"
    assert outcome.clipboard is True
    assert outcome.terminal_requested is False
    assert all("-w" not in argv for argv in seen)


def test_clip_exe_readback_uses_windows_clipboard_only():
    from tmux_agent_tower.clipboard import _read_tool_clipboard

    calls = []

    def fake_capture(argv):
        calls.append(list(argv))
        command = argv[-1]
        if "SessionId" in command:
            return 0, b"1 1\n"
        if "Out.Write" in command:
            return 0, b"hello"
        return 1, b""

    import tmux_agent_tower.clipboard as clipboard

    original = clipboard._capture_clipboard
    clipboard._capture_clipboard = fake_capture
    try:
        text = _read_tool_clipboard(
            "clip.exe",
            lambda name: "powershell.exe" if name == "powershell.exe" else "wl-paste",
        )
    finally:
        clipboard._capture_clipboard = original
    assert text == "hello"
    assert calls[0][0] == "powershell.exe"
    assert "OutputEncoding" in calls[1][-1]
    assert "Out.Write" in calls[1][-1]
    assert all("wl-paste" not in arg for argv in calls for arg in argv)


def test_clip_exit_zero_without_windows_readback_is_not_success(monkeypatch):
    seen = []

    def run(argv, data):
        seen.append(list(argv))
        return 0

    monkeypatch.setattr("tmux_agent_tower.clipboard.choose_clipboard_tool", lambda *args: "clip.exe")
    monkeypatch.setattr("tmux_agent_tower.clipboard._read_tool_clipboard", lambda tool, which: "")
    outcome = copy_text(
        "hello",
        run=run,
        client="/dev/pts/1",
        which=lambda name: "clip.exe",
        verify=lambda tool, text: False,
    )
    assert outcome.clipboard is False
    assert outcome.tool == "tmux"
    assert outcome.terminal_requested is False
    assert any(argv[:3] == ["tmux", "load-buffer", "-b"] for argv in seen)
    assert all("-w" not in argv for argv in seen)


def test_clipboard_read_does_not_inherit_the_tower_tty(monkeypatch):
    import subprocess

    seen = {}

    def fake_run(argv, **kwargs):
        seen.update(kwargs)

        class _Proc:
            returncode = 0
            stdout = b"ok"

        return _Proc()

    monkeypatch.setattr("tmux_agent_tower.clipboard.subprocess.run", fake_run)
    from tmux_agent_tower.clipboard import _read_process

    assert _read_process(["powershell.exe"]) == "ok"
    assert seen["stdin"] is subprocess.DEVNULL
    assert seen["start_new_session"] is True


def test_exit_code_without_round_trip_is_not_a_clipboard_success():
    def run(argv, data):
        return 0

    outcome = copy_text(
        "hello",
        run=run,
        which=lambda name: "clip.exe",
        verify=lambda tool, text: False,
    )
    assert outcome.clipboard is False
    assert outcome.buffer is True
    assert outcome.tool == "tmux"


def test_explicit_terminal_without_readback_is_a_request_not_a_copy():
    seen = []

    def run(argv, data):
        seen.append(list(argv))
        return 0 if argv[0] == "tmux" else 1

    outcome = copy_text(
        "probe",
        run=run,
        client="/dev/pts/11",
        ssh=True,
        destination="current_terminal",
        which=lambda name: None,
        verify=lambda tool, text: False,
    )
    assert outcome.clipboard is False
    assert outcome.terminal_requested is True
    assert seen[0][:3] == ["tmux", "load-buffer", "-b"]
    assert seen[1][:3] == ["tmux", "load-buffer", "-w"]
    assert seen[1][5:7] == ["-t", "/dev/pts/11"]
    assert seen[2][:2] == ["tmux", "delete-buffer"]
    assert not any(argv[:2] == ["tmux", "load-buffer"] and "-b" not in argv for argv in seen)

    tower, tracker = _ready_tower()
    from tmux_agent_tower.clipboard import CopyOutcome

    def fake_copy(text, **_kwargs):
        return CopyOutcome(False, False, "none", True, False)

    import tmux_agent_tower.ui.control_view as view

    original = view.copy_text
    original_observe = view.observe
    original_pref = view.load_preference
    original_result = view.get_result
    view.copy_text = fake_copy
    view.observe = lambda: _local_client()
    view.load_preference = lambda: "auto"
    view.get_result = lambda *_args, **_kwargs: (True, "", {
        "state": "ready", "text": BODY, "complete": True,
        "turn_complete": True, "body_complete": True,
    })
    try:
        notice = _copy_result(tower, "%9")
    finally:
        view.copy_text = original
        view.observe = original_observe
        view.load_preference = original_pref
        view.get_result = original_result
    assert notice == "클립보드로 전송했습니다. 붙여넣기로 확인해 주세요."
    assert tracker.snapshot("%9").state == "ready"


def test_ambiguous_clients_do_not_pick_a_terminal(monkeypatch):
    calls = []

    def fake_copy(text, **kwargs):
        calls.append(kwargs)
        from tmux_agent_tower.clipboard import CopyOutcome

        return CopyOutcome(False, True, "tmux", False, True)

    tower, _tracker = _ready_tower()
    _mock_complete_result(monkeypatch)
    monkeypatch.setattr("tmux_agent_tower.ui.control_view.copy_text", fake_copy)
    monkeypatch.setattr("tmux_agent_tower.ui.control_view.load_preference", lambda: "auto")
    monkeypatch.setattr("tmux_agent_tower.ui.control_view.observe", lambda: _ambiguous_client())
    notice = _copy_result(tower, "%9")
    assert calls == []
    assert notice == "복사할 위치를 선택해주세요"
    assert "Windows" not in notice


def test_copy_result_passes_the_exact_text_and_one_destination(monkeypatch):
    import hashlib

    captured = {}

    def fake_copy(text, **kwargs):
        captured["text"] = text
        captured["kwargs"] = kwargs
        from tmux_agent_tower.clipboard import CopyOutcome

        return CopyOutcome(True, False, "clip.exe")

    tower, _tracker = _ready_tower()
    _mock_complete_result(monkeypatch)
    tower.local_host = "workstation-a"
    monkeypatch.setattr("tmux_agent_tower.ui.control_view.copy_text", fake_copy)
    monkeypatch.setattr("tmux_agent_tower.ui.control_view.load_preference", lambda: "auto")
    monkeypatch.setattr("tmux_agent_tower.ui.control_view.observe", lambda: _local_client())
    notice = _copy_result(tower, "%9")
    assert captured["text"] == BODY
    assert hashlib.sha256(captured["text"].encode()).hexdigest() == hashlib.sha256(BODY.encode()).hexdigest()
    assert captured["kwargs"]["destination"] == "local_host"
    assert captured["kwargs"]["ssh"] is False
    assert notice == "최신 답변을 Windows 클립보드에 복사했습니다."
    assert "Windows" in notice
    assert "터미널" not in notice


def test_host_and_terminal_are_not_written_together(monkeypatch):
    seen = []

    def run(argv, data):
        seen.append(list(argv))
        return 0

    monkeypatch.setattr("tmux_agent_tower.clipboard.choose_clipboard_tool", lambda *args: "clip.exe")
    host = copy_text(
        "한글\n둘째\n\n```\ncode\n```\n",
        run=run,
        client="/dev/pts/1",
        ssh=False,
        which=lambda name: "clip.exe",
        verify=lambda tool, text: tool == "clip.exe",
    )
    assert host.tool == "clip.exe" and host.terminal_requested is False
    assert any(argv[0] == "clip.exe" for argv in seen)
    assert all("-w" not in argv for argv in seen)

    seen.clear()
    body = "한글\n둘째\n\n```\ncode\n```\n"
    terminal = copy_text(
        body,
        run=run,
        client="/dev/pts/4",
        ssh=True,
        destination="current_terminal",
        which=lambda name: "clip.exe",
        verify=lambda tool, text: False,
        read_buffer=lambda name: body.encode("utf-8"),
    )
    assert terminal.terminal_requested is True and terminal.clipboard is False
    assert all(argv[0] == "tmux" for argv in seen)
    assert any(argv[5:7] == ["-t", "/dev/pts/4"] for argv in seen)
    assert all(argv[5:7] != ["-t", "/dev/pts/1"] for argv in seen)


def test_private_buffer_rejects_a_replaced_payload_and_cleans_up():
    seen = []

    def run(argv, data):
        seen.append(list(argv))
        return 0

    outcome = copy_text(
        "payload",
        run=run,
        client="/dev/pts/4",
        ssh=True,
        which=lambda name: None,
        read_buffer=lambda name: b"other-buffer",
    )
    assert outcome.clipboard is False and outcome.buffer is False and outcome.terminal_requested is False
    assert seen[0][:3] == ["tmux", "load-buffer", "-b"]
    assert seen[0][3].startswith("tower-copy-")
    assert seen[1][:3] == ["tmux", "delete-buffer", "-b"]
    assert all("-w" not in argv for argv in seen)


def test_ambiguous_client_uses_only_a_private_buffer():
    seen = []

    def run(argv, data):
        seen.append(list(argv))
        return 0

    outcome = copy_text(
        "payload",
        run=run,
        client="/dev/pts/1",
        ssh=True,
        ambiguous=True,
        which=lambda name: "clip.exe",
    )
    assert outcome.buffer is False and outcome.clipboard is False and outcome.terminal_requested is False
    assert seen == []


def test_phone_copy_does_not_use_the_pc_osc52_path():
    from tmux_agent_tower.server.webui import PAGE_HTML

    assert "load-buffer" not in PAGE_HTML
    assert "osc52" not in PAGE_HTML.lower()


def test_normalize_newlines_keeps_spaces_korean_and_blank_lines():
    import hashlib

    from tmux_agent_tower.clipboard import normalize_newlines

    sample = "TOWER_WINDOWS_CLIP_TEST\n한글 테스트\n\nline-3\n"
    windows = sample.replace("\n", "\r\n")
    assert normalize_newlines(windows) == sample
    assert hashlib.sha256(normalize_newlines(windows).encode()).hexdigest() == hashlib.sha256(
        sample.encode()
    ).hexdigest()
    assert normalize_newlines(" a \r\n한글\r") == " a \n한글\r"
    assert normalize_newlines(" a \r\n한글\r") != "a\n한글\n"


def _long_markdown_clipboard_fixture():
    paragraphs = [
        f"문단 {index:03d}: Tower는 줄바꿈만 정규화하고 한글·English·특수문자 § {index}를 보존합니다.\n"
        f"세부 설명 {index:03d}: 본문, 들여쓰기와 문단 경계를 그대로 확인합니다."
        for index in range(72)
    ]
    return (
        "TOWER_CLIPBOARD_START\n"
        "# 긴 한국어 Markdown Result\n\n"
        + "\n\n".join(paragraphs)
        + "\n\n줄 끝 공백 보존 표식  \n"
        + "\n```python\n"
        + "def verify(text):\n    return text == '한글 / English / §'\n"
        + "```\n\n"
        + "끝 문단: 마지막 개행도 보존합니다.\n"
        + "TOWER_CLIPBOARD_END\n"
    )


@pytest.mark.parametrize("source_eol,clipboard_eol", [("\n", "\r\n"), ("\r\n", "\n")])
def test_long_markdown_readback_allows_only_lf_crlf_normalization(
    monkeypatch, source_eol, clipboard_eol,
):
    import hashlib

    from tmux_agent_tower.clipboard import copy_text, normalize_newlines

    lf_source = _long_markdown_clipboard_fixture()
    source = lf_source if source_eol == "\n" else lf_source.replace("\n", "\r\n")
    readback = lf_source if clipboard_eol == "\n" else lf_source.replace("\n", "\r\n")
    assert len(source.encode("utf-8")) >= 8192
    assert "문단 071" in source
    assert "return text == '한글 / English / §'" in source
    assert "줄 끝 공백 보존 표식  " in source
    assert source.endswith("TOWER_CLIPBOARD_END" + source_eol)

    writes = []
    monkeypatch.setattr(
        "tmux_agent_tower.clipboard.choose_clipboard_tool", lambda *_args: "clip.exe"
    )
    monkeypatch.setattr(
        "tmux_agent_tower.clipboard._default_run",
        lambda argv, data: writes.append((argv, data)) or 0,
    )
    monkeypatch.setattr(
        "tmux_agent_tower.clipboard._read_tool_clipboard",
        lambda _tool, _which: readback,
    )

    exact_byte_match = source.encode("utf-8") == readback.encode("utf-8")
    normalized_text_match = normalize_newlines(source) == normalize_newlines(readback)
    assert exact_byte_match is False
    assert normalized_text_match is True
    assert hashlib.sha256(normalize_newlines(source).encode("utf-8")).hexdigest() == (
        hashlib.sha256(normalize_newlines(readback).encode("utf-8")).hexdigest()
    )
    outcome = copy_text(
        source, destination="local_host", allow_buffer_fallback=False,
        which=lambda name: "powershell.exe" if name == "powershell.exe" else "clip.exe",
        environ={"TOWER_UNAME": "microsoft-standard"}, system="linux",
    )
    assert len(writes) == 1
    assert outcome.clipboard is True
    assert outcome.tool == "clip.exe"


@pytest.mark.parametrize(
    "mutation",
    [
        lambda text: text.replace("문단 035:", "문단 035", 1),
        lambda text: text.replace(
            "문단 035: Tower는 줄바꿈만 정규화하고 한글·English·특수문자 § 35를 보존합니다.\r\n"
            "세부 설명 035: 본문, 들여쓰기와 문단 경계를 그대로 확인합니다.\r\n\r\n",
            "",
            1,
        ),
        lambda text: text.replace("return text == '한글 / English / §'", "return False", 1),
        lambda text: text.replace("줄 끝 공백 보존 표식  ", "줄 끝 공백 보존 표식", 1),
        lambda text: text.removesuffix("\r\n"),
        lambda text: text.replace("\r\n\r\n문단 035:", "\r\n문단 035:", 1),
    ],
    ids=[
        "character", "middle-paragraph", "code-block", "trailing-spaces",
        "final-newline", "paragraph-break",
    ],
)
def test_long_markdown_readback_rejects_non_newline_changes(monkeypatch, mutation):
    from tmux_agent_tower.clipboard import _host_matches

    source = _long_markdown_clipboard_fixture()
    readback = mutation(source.replace("\n", "\r\n"))
    monkeypatch.setattr(
        "tmux_agent_tower.clipboard._read_tool_clipboard",
        lambda _tool, _which: readback,
    )
    monkeypatch.setattr("tmux_agent_tower.clipboard.time.sleep", lambda _seconds: None)

    assert _host_matches("clip.exe", source, lambda _name: "powershell.exe") is False


def test_copy_text_readback_failure_is_not_clipboard_success(monkeypatch):
    from tmux_agent_tower.clipboard import copy_text

    writes = []
    body = _long_markdown_clipboard_fixture()
    monkeypatch.setattr(
        "tmux_agent_tower.clipboard.choose_clipboard_tool", lambda *_args: "clip.exe"
    )
    monkeypatch.setattr(
        "tmux_agent_tower.clipboard._default_run",
        lambda argv, data: writes.append((argv, data)) or 0,
    )
    monkeypatch.setattr(
        "tmux_agent_tower.clipboard._read_tool_clipboard",
        lambda _tool, _which: body.replace("문단 035:", "문단 035", 1),
    )
    monkeypatch.setattr("tmux_agent_tower.clipboard.time.sleep", lambda _seconds: None)

    outcome = copy_text(
        body, destination="local_host", allow_buffer_fallback=False,
        which=lambda name: "powershell.exe" if name == "powershell.exe" else "clip.exe",
        environ={"TOWER_UNAME": "microsoft-standard"}, system="linux",
    )

    assert len(writes) == 1
    assert outcome.clipboard is False
    assert outcome.buffer is False


def test_copy_text_missing_readback_is_not_clipboard_success(monkeypatch):
    from tmux_agent_tower.clipboard import copy_text

    writes = []
    monkeypatch.setattr(
        "tmux_agent_tower.clipboard.choose_clipboard_tool", lambda *_args: "clip.exe"
    )
    monkeypatch.setattr(
        "tmux_agent_tower.clipboard._default_run",
        lambda argv, data: writes.append((argv, data)) or 0,
    )
    monkeypatch.setattr(
        "tmux_agent_tower.clipboard._read_tool_clipboard",
        lambda _tool, _which: None,
    )
    monkeypatch.setattr("tmux_agent_tower.clipboard.time.sleep", lambda _seconds: None)

    outcome = copy_text(
        _long_markdown_clipboard_fixture(), destination="local_host",
        allow_buffer_fallback=False,
        which=lambda name: "powershell.exe" if name == "powershell.exe" else "clip.exe",
        environ={"TOWER_UNAME": "microsoft-standard"}, system="linux",
    )

    assert len(writes) == 1
    assert outcome.clipboard is False
    assert outcome.buffer is False


def test_lone_carriage_return_is_not_an_accepted_newline_conversion(monkeypatch):
    from tmux_agent_tower.clipboard import _host_matches, normalize_newlines

    source = "line one\nline two"
    readback = "line one\rline two"
    monkeypatch.setattr(
        "tmux_agent_tower.clipboard._read_tool_clipboard",
        lambda _tool, _which: readback,
    )
    assert normalize_newlines(readback) != normalize_newlines(source)
    assert _host_matches("clip.exe", source, lambda _name: "powershell.exe") is False


def test_incomplete_result_never_reaches_clipboard_router(monkeypatch):
    tower, tracker = _ready_tower()
    _mock_complete_result(monkeypatch)
    monkeypatch.setattr(
        "tmux_agent_tower.ui.control_view.get_result",
        lambda *_args, **_kwargs: (True, "", {
            "state": "ready", "text": BODY, "complete": False,
            "turn_complete": True, "body_complete": False,
            "fingerprint": "partial-fp",
        }),
    )
    clipboard_calls = []
    monkeypatch.setattr(
        "tmux_agent_tower.ui.control_view.copy_text",
        lambda *_args, **_kwargs: clipboard_calls.append(True),
    )

    notice = _copy_result(tower, "%9")

    assert clipboard_calls == []
    assert tracker.snapshot("%9").state == "ready"
    assert "클립보드" not in notice


def test_crlf_readback_matches_and_a_real_change_does_not(monkeypatch):
    from tmux_agent_tower.clipboard import _host_matches

    sample = "TOWER_WINDOWS_CLIP_TEST\n한글 테스트\n\nline-3\n"
    seen = {"text": sample.replace("\n", "\r\n")}
    monkeypatch.setattr(
        "tmux_agent_tower.clipboard._read_tool_clipboard",
        lambda tool, which: seen["text"],
    )
    assert _host_matches("clip.exe", sample, lambda name: "powershell.exe") is True
    seen["text"] = sample.replace("한", "\ufffd")
    assert _host_matches("clip.exe", sample, lambda name: "powershell.exe") is False
    seen["text"] = "hello \r\n"
    assert _host_matches("clip.exe", "hello\n", lambda name: "powershell.exe") is False


def test_utf8_pipe_failure_reads_the_dotnet_utf8_bytes(monkeypatch):
    import base64

    from tmux_agent_tower.clipboard import _read_tool_clipboard

    sample = "한글 테스트\nline-3\n"
    encoded = base64.b64encode(sample.encode("utf-8"))

    def fake_capture(argv):
        command = argv[-1]
        if "SessionId" in command:
            return 0, b"1 1\n"
        if "Out.Write" in command:
            return 0, b"\xff not utf-8 \xfe"
        if "ToBase64String" in command:
            return 0, encoded
        return 1, b""

    monkeypatch.setattr("tmux_agent_tower.clipboard._capture_clipboard", fake_capture)
    assert _read_tool_clipboard("clip.exe", lambda name: "powershell.exe") == sample


def test_other_windows_session_is_not_compared_as_the_payload(monkeypatch):
    from tmux_agent_tower.clipboard import _read_tool_clipboard

    def fake_capture(argv):
        command = argv[-1]
        if "SessionId" in command:
            return 0, b"0 1\n"
        return 0, "TOWER_WINDOWS_CLIP_TEST\n한글".encode()

    monkeypatch.setattr("tmux_agent_tower.clipboard._capture_clipboard", fake_capture)
    assert _read_tool_clipboard("clip.exe", lambda name: "powershell.exe") is None


def test_clip_exe_is_started_by_tmux_and_the_body_stays_out_of_argv(monkeypatch):
    from tmux_agent_tower.clipboard import _run_windows_clip

    body = "TOWER_WINDOWS_CLIP_TEST\n한글 테스트\n\nline-3\n".encode("utf-16le")
    seen = {}

    def fake_run(argv, **_kwargs):
        seen["argv"] = list(argv)
        parts = argv[-1].split()
        seen["file"] = Path(parts[2]).read_bytes()
        Path(parts[3]).write_text("0", encoding="ascii")

        class _Proc:
            returncode = 0

        return _Proc()

    monkeypatch.setattr("tmux_agent_tower.clipboard.subprocess.run", fake_run)
    assert _run_windows_clip(["/mnt/c/Windows/System32/clip.exe"], body) == 0
    assert seen["argv"][:3] == ["tmux", "run-shell", "-b"]
    assert "한글" not in seen["argv"][-1]
    assert body not in seen["argv"][-1].encode()
    assert seen["file"] == body


def test_true_mismatch_uses_a_private_buffer_and_leaves_the_y_body(monkeypatch):
    seen = []
    body = "TOWER_WINDOWS_CLIP_TEST\n한글 테스트\n\nline-3\n"

    def run(argv, data):
        seen.append((list(argv), data))
        return 0

    monkeypatch.setattr("tmux_agent_tower.clipboard.choose_clipboard_tool", lambda *args: "clip.exe")
    outcome = copy_text(
        body,
        run=run,
        which=lambda name: "clip.exe",
        verify=lambda tool, text: False,
        read_buffer=lambda name: body.encode("utf-8"),
    )
    assert outcome.clipboard is False
    assert outcome.buffer is True
    assert outcome.tool == "tmux"
    clip_calls = [item for item in seen if item[0][0] == "clip.exe"]
    assert clip_calls[0][1] == body.encode("utf-16le")
    buffer_calls = [item for item in seen if item[0][:3] == ["tmux", "load-buffer", "-b"]]
    assert buffer_calls[0][0][3].startswith("tower-copy-")
    assert buffer_calls[0][1] == body.encode("utf-8")
    assert all("-w" not in argv for argv, _data in seen)


def test_manual_local_host_does_not_also_write_the_terminal():
    seen = []

    def run(argv, data):
        seen.append(list(argv))
        return 0

    outcome = copy_text(
        "한글",
        run=run,
        client="/dev/pts/11",
        ssh=True,
        destination="local_host",
        environ={"TOWER_UNAME": "microsoft-standard"},
        system="linux",
        which=lambda name: "clip.exe" if name == "clip.exe" else None,
        verify=lambda tool, text: tool == "clip.exe",
    )
    assert outcome.clipboard is True and outcome.terminal_requested is False and outcome.buffer is False
    assert [argv[0] for argv in seen] == ["clip.exe"]


def test_manual_current_terminal_writes_only_that_client():
    seen = []

    def run(argv, data):
        seen.append(list(argv))
        return 0

    outcome = copy_text(
        "한글",
        run=run,
        client="/dev/pts/11",
        ssh=False,
        destination="current_terminal",
        which=lambda name: "clip.exe",
        verify=lambda tool, text: tool == "terminal",
    )
    assert outcome.clipboard is True and outcome.tool == "terminal"
    assert all(argv[0] != "clip.exe" for argv in seen)
    assert ["tmux", "load-buffer", "-w"] == [argv[:3] for argv in seen if "-w" in argv][0][:3]
    assert seen[-2][5:7] == ["-t", "/dev/pts/11"] or any(
        argv[5:7] == ["-t", "/dev/pts/11"] for argv in seen
    )


def test_manual_current_terminal_refuses_an_unknown_client():
    seen = []

    outcome = copy_text(
        "한글",
        run=lambda argv, data: seen.append(list(argv)) or 0,
        client=None,
        ambiguous=True,
        destination="current_terminal",
        which=lambda name: "clip.exe",
    )
    assert outcome.clipboard is False and outcome.buffer is False and outcome.terminal_requested is False
    assert seen == []


def test_manual_tower_box_is_a_private_buffer_only():
    seen = []

    def run(argv, data):
        seen.append(list(argv))
        return 0

    outcome = copy_text(
        "한글",
        run=run,
        client="/dev/pts/11",
        ssh=True,
        destination="tmux_buffer",
        which=lambda name: "clip.exe",
        read_buffer=lambda name: "한글".encode(),
    )
    assert outcome.buffer is True and outcome.clipboard is False and outcome.terminal_requested is False
    assert seen[0][:3] == ["tmux", "load-buffer", "-b"]
    assert seen[0][3].startswith("tower-copy-")
    assert all(argv[0] != "clip.exe" for argv in seen)
    assert all("-w" not in argv for argv in seen)
