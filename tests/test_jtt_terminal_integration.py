"""JTT UI and two local PTY shells; no tmux, network, or agent CLI."""

from __future__ import annotations

import json
import os
from pathlib import Path
import pty
import pwd
import select
import shutil
import signal
import struct
import tempfile
import termios
import time
import uuid

import pytest

from tmux_agent_tower.state.jtt_workspaces import JTTWorkspaceStore
from tmux_agent_tower.terminal import TerminalRegistry, renderer_available
from tmux_agent_tower.ui import jtt
from tmux_agent_tower.ui import ux_preview_v2 as renderer


pytestmark = [
    pytest.mark.skipif(not renderer_available(), reason="interactive VT renderer unavailable"),
    pytest.mark.skipif(not Path("/bin/bash").exists(), reason="bash fixture requires /bin/bash"),
]


class _Screen:
    def getmaxyx(self):
        return (45, 160)


class _UIProcess:
    def __init__(self, runtime_root: Path, source_root: Path, pyte_root: Path):
        import pyte

        self.pid, self.fd = pty.fork()
        self.raw = bytearray()
        if self.pid == 0:
            env = {
                "PATH": os.defpath,
                "HOME": str(runtime_root / "shell-home"),
                "JTT_HOME": str(runtime_root / "state"),
                "LANG": "C.UTF-8",
                "LC_ALL": "C.UTF-8",
                "TERM": "xterm",
                "PYTHONPATH": os.pathsep.join((str(pyte_root), str(source_root))),
                "ESCDELAY": "25",
            }
            try:
                os.execve(os.sys.executable,
                          [os.sys.executable, "-m", "tmux_agent_tower.jtt"], env)
            except OSError:
                os._exit(127)
        os.set_blocking(self.fd, False)
        import fcntl
        fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", 45, 160, 0, 0))
        self.screen = pyte.Screen(160, 45)
        self.stream = pyte.Stream(self.screen)

    def send(self, data: bytes) -> None:
        os.write(self.fd, data)

    def pump(self, timeout: float = 0.05) -> None:
        ready, _, _ = select.select([self.fd], [], [], timeout)
        if not ready:
            return
        try:
            data = os.read(self.fd, 65536)
        except OSError:
            return
        if data:
            self.raw.extend(data)
            self.stream.feed(data.decode("utf-8", errors="replace"))

    def text(self) -> str:
        return "\n".join(self.screen.display)

    def exit(self, timeout: float = 5.0) -> int:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.pump()
            waited, status = os.waitpid(self.pid, os.WNOHANG)
            if waited:
                self.pid = -1
                return os.waitstatus_to_exitcode(status)
        raise AssertionError("JTT curses process did not exit")

    def terminate(self) -> None:
        if self.pid > 0:
            try:
                os.kill(self.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                os.waitpid(self.pid, 0)
            except ChildProcessError:
                pass
            self.pid = -1
        if self.fd >= 0:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = -1


def _set_private_process_environment(monkeypatch, root: Path, source_root: Path, pyte_root: Path):
    home = root / "shell-home"
    home.mkdir(mode=0o700)
    home.chmod(0o700)
    # The supervisor inherits only a small test environment; no credentials or user config.
    allowed = {"PATH", "HOME", "LANG", "LC_ALL", "TERM", "PYTHONPATH", "ESCDELAY"}
    for key in tuple(os.environ):
        if key not in allowed:
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("PATH", os.defpath)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("LANG", "C.UTF-8")
    monkeypatch.setenv("LC_ALL", "C.UTF-8")
    monkeypatch.setenv("TERM", "xterm")
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join((str(pyte_root), str(source_root))))
    monkeypatch.setenv("JTT_HOME", str(root / "state"))


def _binding_text(binding) -> str:
    screen = binding.screen
    if screen is None:
        return ""
    return "\n".join("".join(cell.text for cell in row) for row in screen.rows)


def _wait_for_marker(binding, marker: str, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        binding.refresh()
        if binding.synced and _binding_text(binding).count(marker) >= 2:
            return
        time.sleep(0.02)
    raise AssertionError("selected PTY did not display both its echo and output marker")


def _wait_for_ui_text(ui: _UIProcess, expected: str, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        ui.pump()
        if expected in ui.text():
            return
    raise AssertionError("JTT curses UI did not render the expected terminal marker")


def _enter_right_input(screen, state, binding) -> None:
    binding.refresh()
    assert binding.synced
    state["active_panel"] = 1
    assert renderer._handle_key(screen, state, "\n") is False  # center -> RIGHT
    assert state["active_panel"] == 2
    assert renderer._handle_key(screen, state, "\n") is False  # RIGHT -> input lease
    assert binding.input_mode


def _type_through_ui(screen, state, text: str) -> None:
    for char in text:
        assert renderer._handle_key(screen, state, char) is False


def test_jtt_terminal_input_switch_disconnect_reconnect_and_stop(monkeypatch):
    if Path(pwd.getpwuid(os.getuid()).pw_shell).resolve() != Path("/bin/bash").resolve():
        pytest.skip("fixture deliberately exercises the account's /bin/bash login shell")

    root = Path(tempfile.mkdtemp(prefix="jtt-it-"))
    root.chmod(0o700)
    source_root = Path(__file__).resolve().parents[1] / "src"
    import pyte
    pyte_root = Path(pyte.__file__).resolve().parent.parent
    _set_private_process_environment(monkeypatch, root, source_root, pyte_root)
    store = JTTWorkspaceStore(root / "state")
    registry = TerminalRegistry(root=root / "state" / "terminals")
    sessions: list[str] = []
    clients = {}
    try:
        # Two shells are created only by this explicit test action.
        first = registry.create("Fixture A")
        sessions.append(first.identity.session_id)
        second = registry.create("Fixture B")
        sessions.append(second.identity.session_id)
        original_pids = {item.session_id: item.shell_pid for item in registry.list_sessions()}
        store.set_terminal_ids(store.DEFAULT_ID, sessions)
        state = jtt.build_state(store, registry)
        clients = state["managed_terminals"]
        assert [row["managed_id"] for row in state["panes"]] == sessions
        assert [row["pane_pid"] for row in state["panes"]] == [
            str(original_pids[sessions[0]]), str(original_pids[sessions[1]])
        ]

        screen = _Screen()
        markers = {}
        for pane_index, session_id in enumerate(sessions):
            state["pane_index"] = pane_index
            row = state["panes"][pane_index]
            state["selected_target_id"] = row["target_id"]
            state["selected_signature"] = renderer._pane_signature(row)
            binding = renderer._managed_binding(state)
            assert binding is not None and binding.session_id == session_id
            _enter_right_input(screen, state, binding)
            marker = f"JTT_CARD_{pane_index}_" + uuid.uuid4().hex[:8]
            markers[session_id] = marker
            _type_through_ui(screen, state, f"printf '%s\\n' {marker}\n")
            _wait_for_marker(binding, marker)
            assert renderer._handle_key(screen, state, "\x1d") is False
            assert not binding.input_mode

        # Each card remains bound to its own output; the other marker is absent.
        for session_id, other_id in ((sessions[0], sessions[1]), (sessions[1], sessions[0])):
            binding = clients[session_id]
            binding.refresh()
            own_text = _binding_text(binding)
            other_text = _binding_text(clients[other_id])
            assert markers[session_id] in own_text
            assert markers[other_id] not in own_text
            assert markers[session_id] not in other_text

        # A UI detach releases its clients but keeps the exact shells alive.
        for binding in clients.values():
            binding.close()
        state["managed_terminals"].clear()
        assert {item.session_id: item.shell_pid for item in registry.list_sessions()} == original_pids
        for pid in original_pids.values():
            os.kill(pid, 0)

        # The actual curses UI enters RIGHT input mode, sends one harmless
        # printf command through the selected card, then detaches without
        # stopping either shell. No tmux or user-agent path is involved.
        ui = _UIProcess(root, source_root, pyte_root)
        try:
            _wait_for_ui_text(ui, "Fixture A")
            ui.send(b"\t\t\n")  # LEFT -> CENTER -> RIGHT -> input mode.
            _wait_for_ui_text(ui, "Ctrl+]")
            right_marker = "JTT_RIGHT_" + uuid.uuid4().hex[:8]
            ui.send(("printf '%s\\n' '" + right_marker + "'\n").encode("ascii"))
            _wait_for_ui_text(ui, right_marker)
            ui.send(b"\x1d")  # Return to navigation; release the input lease.
            _wait_for_ui_text(ui, "관찰 중")
            ui.send(b"q")
            assert ui.exit() == 0
            assert b"\x1b[?1049l" in ui.raw
        finally:
            ui.terminate()
        assert {item.session_id: item.shell_pid for item in registry.list_sessions()} == original_pids

        # A fresh curses process reconnects to the same stored cards and PIDs.
        ui = _UIProcess(root, source_root, pyte_root)
        try:
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                ui.pump()
                if "터미널" in ui.text() and "Fixture A" in ui.text():
                    break
            assert "터미널" in ui.text()
            assert "Fixture A" in ui.text()
            assert "Fixture B" in ui.text()
            ui.send(b"q")
            assert ui.exit() == 0
            assert b"\x1b[?1049l" in ui.raw
        finally:
            ui.terminate()
        assert {item.session_id: item.shell_pid for item in registry.list_sessions()} == original_pids
    finally:
        for binding in clients.values():
            binding.close()
        for session_id in sessions:
            try:
                registry.stop(session_id)
            except (OSError, RuntimeError, ValueError):
                pass
        shutil.rmtree(root, ignore_errors=True)


def test_real_curses_launch_and_quit_does_not_autostart_a_shell(monkeypatch):
    root = Path(tempfile.mkdtemp(prefix="jtt-tty-"))
    root.chmod(0o700)
    source_root = Path(__file__).resolve().parents[1] / "src"
    import pyte
    pyte_root = Path(pyte.__file__).resolve().parent.parent
    _set_private_process_environment(monkeypatch, root, source_root, pyte_root)
    ui = None
    try:
        ui = _UIProcess(root, source_root, pyte_root)
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            ui.pump()
            if "작업공간" in ui.text() and "터미널 배치" in ui.text():
                break
        assert "작업공간" in ui.text()
        assert "터미널 배치" in ui.text()
        assert not (root / "state").exists()
        ui.send(b"q")
        assert ui.exit() == 0
        assert b"\x1b[?1049l" in ui.raw
        assert not (root / "state").exists()
        ui.terminate()
        ui = None
    finally:
        if ui is not None:
            ui.terminate()
        shutil.rmtree(root, ignore_errors=True)
