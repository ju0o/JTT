"""JTT UI, isolated PTY shells, and optional private-socket tmux; no agents."""

from __future__ import annotations

import json
import os
from pathlib import Path
import pty
import pwd
import select
import shlex
import shutil
import signal
import stat
import struct
import subprocess
import tempfile
import termios
import time
import uuid

import pytest

from tmux_agent_tower.state.jtt_workspaces import JTTWorkspaceStore
from tmux_agent_tower.state.jtt_work_kits import JTTWorkKitStore
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


def _wait_for_created_terminal(ui: _UIProcess, registry: TerminalRegistry,
                               timeout: float = 8.0):
    """Wait for both the exact-one registry row and successful RIGHT input lease.

    The generic prefix footer also mentions Ctrl+] while merely awaiting a
    prefix command, so that key label is not a creation/lease completion oracle.
    """

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        ui.pump()
        sessions = registry.list_sessions()
        if len(sessions) > 1:
            raise AssertionError("prefix created more than one terminal")
        if len(sessions) == 1 and "터미널 준비 완료" in ui.text():
            return sessions[0]
    raise AssertionError("prefix did not create one ready terminal and acquire RIGHT")


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
        # printf command through the selected card, then explicitly detaches without
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
            # Bare navigation q is inert; follow it with a prefix help command
            # so the real curses screen proves the UI stayed alive.
            ui.send(b"q\x07?")
            _wait_for_ui_text(ui, "JTT 키 안내")
            ui.send(b"\x1b")  # Close the help dialog, not the JTT UI.
            ui.send(b"\x07d")  # Only the explicit prefix command detaches.
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
                if all(marker in ui.text() for marker in ("터미널", "Fixture A", "Fixture B")):
                    break
            assert "터미널" in ui.text()
            assert "Fixture A" in ui.text()
            assert "Fixture B" in ui.text()
            ui.send(b"\x07d")
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


def test_read_only_observe_keeps_two_pty_screens_distinct_and_does_not_take_lease(monkeypatch):
    if Path(pwd.getpwuid(os.getuid()).pw_shell).resolve() != Path("/bin/bash").resolve():
        pytest.skip("fixture deliberately exercises the account's /bin/bash login shell")

    root = Path(tempfile.mkdtemp(prefix="jtt-observe-it-"))
    root.chmod(0o700)
    source_root = Path(__file__).resolve().parents[1] / "src"
    import pyte
    pyte_root = Path(pyte.__file__).resolve().parent.parent
    _set_private_process_environment(monkeypatch, root, source_root, pyte_root)
    registry = TerminalRegistry(root=root / "state" / "terminals")
    created = []
    observers = []
    active = []
    try:
        for name, marker in (("Mini A", "JTT_MINI_A"), ("Mini B", "JTT_MINI_B")):
            client = registry.create(name)
            created.append(client.identity.session_id)
            assert client.acquire()
            client.send(f"printf '%s\\n' {marker}\n".encode("ascii"))
            _wait_for_marker(jtt.ManagedTerminalBinding(client, client.identity), marker)
            active.append(client)

        first_observer = registry.connect(created[0])
        second_observer = registry.connect(created[1])
        observers.extend((first_observer, second_observer))
        first_sequence = active[0]._sequence
        second_sequence = active[1]._sequence
        first_screen = first_observer.observe()
        second_screen = second_observer.observe()
        first_text = "\n".join("".join(cell.text for cell in row) for row in first_screen.screen.rows)
        second_text = "\n".join("".join(cell.text for cell in row) for row in second_screen.screen.rows)

        assert first_screen.synced and second_screen.synced
        assert "JTT_MINI_A" in first_text and "JTT_MINI_B" not in first_text
        assert "JTT_MINI_B" in second_text and "JTT_MINI_A" not in second_text
        assert first_observer._sequence == 0 and second_observer._sequence == 0
        assert not first_observer.lease and not second_observer.lease
        assert active[0]._sequence == first_sequence and active[1]._sequence == second_sequence
        assert active[0].lease and active[1].lease
        assert (first_screen.columns, first_screen.lines) == (80, 24)
        assert (second_screen.columns, second_screen.lines) == (80, 24)

        # The original input owners remain able to write after observation.
        active[0].send(b"printf 'JTT_AFTER_OBSERVE\\n'\n")
        assert "JTT_AFTER_OBSERVE" in _screen_text(_wait_for_text(active[0], "JTT_AFTER_OBSERVE"))
    finally:
        for observer in observers:
            observer.close()
        for client in active:
            client.close()
        for session_id in created:
            registry.stop(session_id)
        shutil.rmtree(root, ignore_errors=True)


def test_real_curses_launch_and_prefix_detach_does_not_autostart_a_shell(monkeypatch):
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
        ui.send(b"\x07d")
        assert ui.exit() == 0
        assert b"\x1b[?1049l" in ui.raw
        assert not (root / "state").exists()
        ui.terminate()
        ui = None
    finally:
        if ui is not None:
            ui.terminate()
        shutil.rmtree(root, ignore_errors=True)


def test_real_curses_work_kit_creation_selects_template_without_starting_shells(monkeypatch):
    """Create and select a Work Kit through the actual curses PTY, not a mock UI."""
    root = Path(tempfile.mkdtemp(prefix="jtt-work-kit-pty-"))
    root.chmod(0o700)
    source_root = Path(__file__).resolve().parents[1] / "src"
    import pyte
    pyte_root = Path(pyte.__file__).resolve().parent.parent
    _set_private_process_environment(monkeypatch, root, source_root, pyte_root)
    registry = TerminalRegistry(root=root / "state" / "terminals")
    ui = None
    try:
        assert registry.list_sessions() == ()
        ui = _UIProcess(root, source_root, pyte_root)
        _wait_for_ui_text(ui, "터미널 배치")
        assert registry.list_sessions() == ()

        # A fresh store exposes the default workspace, its default folder and
        # Window before the Work Kit create action in LEFT. Walk that real
        # navigation order, then enter the create form.
        ui.send(b"\x1bOB\x1bOB\x1bOB\n")  # Three application-mode downs, then Enter.
        _wait_for_ui_text(ui, "Work Kit 이름:")
        ui.send("Integration Kit\nTemplate preview only\n1\nShell\nLocal command line\n\n".encode("utf-8"))

        _wait_for_ui_text(
            ui, "Integration Kit Work Kit 저장 완료 · 터미널 1개 구성 · 아직 실행되지 않음",
        )
        # The newly created Kit is the selected LEFT entry and its saved
        # template is visible as the selected CENTER card, not as a live PTY.
        _wait_for_ui_text(ui, "▶ Integration Kit")
        _wait_for_ui_text(ui, "▶ Shell")
        _wait_for_ui_text(ui, "Local command line")
        _wait_for_ui_text(ui, "터미널 · Shell")
        _wait_for_ui_text(ui, "템플릿 · 셸/명령 미실행")
        assert "실행 중인 터미널이 아닙니다." in ui.text()
        assert "관찰 중" not in ui.text()
        assert registry.list_sessions() == ()
        kits = JTTWorkKitStore(root / "state").kits()
        assert len(kits) == 1
        assert kits[0]["name"] == "Integration Kit"
        assert kits[0]["layout"] == "single"
        assert not (root / "state" / "workspaces.json").exists()

        ui.send(b"\x07d")
        assert ui.exit() == 0
        assert b"\x1b[?1049l" in ui.raw
        assert registry.list_sessions() == ()
    finally:
        if ui is not None:
            ui.terminate()
        shutil.rmtree(root, ignore_errors=True)


def test_prefix_creates_one_shell_autofocuses_and_reconnects_without_recreation(monkeypatch):
    if Path(pwd.getpwuid(os.getuid()).pw_shell).resolve() != Path("/bin/bash").resolve():
        pytest.skip("fixture deliberately exercises the account's /bin/bash login shell")

    root = Path(tempfile.mkdtemp(prefix="jtt-prefix-it-"))
    root.chmod(0o700)
    source_root = Path(__file__).resolve().parents[1] / "src"
    import pyte
    pyte_root = Path(pyte.__file__).resolve().parent.parent
    _set_private_process_environment(monkeypatch, root, source_root, pyte_root)
    registry = TerminalRegistry(root=root / "state" / "terminals")
    ui = None
    session_id = None
    shell_pid = None
    try:
        assert registry.list_sessions() == ()
        ui = _UIProcess(root, source_root, pyte_root)
        _wait_for_ui_text(ui, "터미널 배치")
        assert registry.list_sessions() == ()  # UI startup is still read-only.

        ui.send(b"\x07n")
        created = _wait_for_created_terminal(ui, registry)
        sessions = registry.list_sessions()
        assert len(sessions) == 1 and sessions[0] == created
        session_id = created.session_id
        shell_pid = created.shell_pid
        marker = "JTT_PREFIX_N_" + uuid.uuid4().hex[:8]
        ui.send(("printf '%s\\n' '" + marker + "'\n").encode("ascii"))
        _wait_for_ui_text(ui, marker)

        # Prove Ctrl+G never leaks, and the second Ctrl+G arrives as exactly
        # one byte at the selected PTY while it is waiting for one raw byte.
        ctrl_probe = "read -r -n 1 -s key; printf 'JTT_CTRL_G_ORD=%d\\n' \"'$key\""
        ui.send((ctrl_probe + "\n").encode("ascii"))
        ui.send(b"\x07\x07")
        _wait_for_ui_text(ui, "JTT_CTRL_G_ORD=7")

        ui.send(b"\x1d")
        _wait_for_ui_text(ui, "관찰 중")
        ui.send(b"\x07d")
        assert ui.exit() == 0
        ui = None
        assert [(item.session_id, item.shell_pid) for item in registry.list_sessions()] == [
            (session_id, shell_pid)
        ]

        ui = _UIProcess(root, source_root, pyte_root)
        _wait_for_ui_text(ui, "JTT_PREFIX_N_")
        assert [(item.session_id, item.shell_pid) for item in registry.list_sessions()] == [
            (session_id, shell_pid)
        ]  # Reconnect never creates a replacement PTY.
        # Prefix state starts clear after reconnect: bare n is not a command.
        ui.send(b"n")
        time.sleep(0.15)
        assert [(item.session_id, item.shell_pid) for item in registry.list_sessions()] == [
            (session_id, shell_pid)
        ]
        # Prefix detach releases only the UI client and leaves the shell alive.
        ui.send(b"\x07d")
        assert ui.exit() == 0
        ui = None
        assert [(item.session_id, item.shell_pid) for item in registry.list_sessions()] == [
            (session_id, shell_pid)
        ]

        # A further UI connection remains attached to the same PTY. Explicit
        # prefix detach still does not create or terminate a shell.
        ui = _UIProcess(root, source_root, pyte_root)
        _wait_for_ui_text(ui, "JTT_PREFIX_N_")
        assert [(item.session_id, item.shell_pid) for item in registry.list_sessions()] == [
            (session_id, shell_pid)
        ]
        ui.send(b"\x07d")
        assert ui.exit() == 0
        ui = None
        assert [(item.session_id, item.shell_pid) for item in registry.list_sessions()] == [
            (session_id, shell_pid)
        ]
    finally:
        if ui is not None:
            ui.terminate()
        if session_id is not None:
            try:
                registry.stop(session_id)
            except (OSError, RuntimeError, ValueError):
                pass
        shutil.rmtree(root, ignore_errors=True)


@pytest.mark.skipif(shutil.which("vim") is None, reason="interactive Vim TUI is unavailable")
def test_vim_alternate_screen_unicode_keys_resize_and_shell_return(monkeypatch):
    """Exercise an installed, no-config TUI only inside a fresh JTT PTY."""

    if Path(pwd.getpwuid(os.getuid()).pw_shell).resolve() != Path("/bin/bash").resolve():
        pytest.skip("fixture deliberately exercises the account's /bin/bash login shell")

    root = Path(tempfile.mkdtemp(prefix="jtt-vim-it-"))
    root.chmod(0o700)
    source_root = Path(__file__).resolve().parents[1] / "src"
    import pyte
    pyte_root = Path(pyte.__file__).resolve().parent.parent
    _set_private_process_environment(monkeypatch, root, source_root, pyte_root)
    registry = TerminalRegistry(root=root / "state" / "terminals")
    client = None
    session_id = None
    try:
        client = registry.create("Isolated Vim fixture")
        session_id = client.identity.session_id
        initial = client.poll()
        assert initial.synced and initial.screen is not None
        assert client.acquire()

        # No user file is opened: Vim uses a new unnamed buffer, ignores user
        # vimrc/plugins/history, and is told not to create a swap file.
        command = b"vim -Nu NONE -n -i NONE\n"
        assert client.send(command) == len(command)
        deadline = time.monotonic() + 6.0
        tui_output = bytearray()
        update = initial
        while time.monotonic() < deadline:
            update = client.poll()
            tui_output.extend(update.output)
            if b"\x1b[?1049h" in tui_output and update.synced and not update.more:
                break
            time.sleep(0.02)
        assert update.synced and not update.more and update.screen is not None
        assert b"\x1b[?1049h" in tui_output  # Vim entered its alternate screen.

        marker = "JTT_VIM_한글"
        insertion = ("i" + marker + "\r\nNEXT").encode("utf-8")
        assert client.send(insertion) == len(insertion)

        deadline = time.monotonic() + 6.0
        while time.monotonic() < deadline:
            update = client.poll()
            tui_output.extend(update.output)
            if update.synced and not update.more and marker in _binding_text(update):
                break
            time.sleep(0.02)
        assert update.synced and not update.more and update.screen is not None
        assert marker in _binding_text(update)
        assert 0 <= update.screen.cursor_x < update.columns
        assert 0 <= update.screen.cursor_y < update.lines
        wide_positions = []
        for row in update.screen.rows:
            wide_positions.extend(index for index, cell in enumerate(row) if cell.text == "한")
        assert wide_positions
        assert any(row[x + 2].text == "글" for row in update.screen.rows
                   for x, cell in enumerate(row[:-2]) if cell.text == "한")

        # Escape leaves insert mode; arrow and Enter are delivered to Vim.
        movement = b"\x1b\x1b[A\r"
        assert client.send(movement) == len(movement)
        client.resize(100, 30)
        resize_deadline = time.monotonic() + 4.0
        while time.monotonic() < resize_deadline:
            update = client.poll()
            tui_output.extend(update.output)
            if update.synced and not update.more and update.columns == 100 and update.lines == 30:
                break
            time.sleep(0.02)
        assert update.synced and update.columns == 100 and update.lines == 30
        assert update.screen is not None and len(update.screen.rows) == 30

        # Ask the running TUI itself to report the dimensions it observed.
        assert client.send(b":echo 'JTT_VIM_SIZE=' . &lines . 'x' . &columns\r") > 0
        deadline = time.monotonic() + 4.0
        while time.monotonic() < deadline:
            update = client.poll()
            tui_output.extend(update.output)
            if (update.synced and not update.more
                    and "JTT_VIM_SIZE=30x100" in _binding_text(update)):
                break
            time.sleep(0.02)
        assert "JTT_VIM_SIZE=30x100" in _binding_text(update)

        assert client.send(b":q!\r") == 4
        deadline = time.monotonic() + 4.0
        while time.monotonic() < deadline:
            update = client.poll()
            tui_output.extend(update.output)
            if b"\x1b[?1049l" in tui_output and update.synced and not update.more:
                break
            time.sleep(0.02)
        assert b"\x1b[?1049l" in tui_output  # Vim restored the shell screen.
        after = "JTT_AFTER_VIM_" + uuid.uuid4().hex[:8]
        shell_command = f"printf '%s\\n' '{after}'\n".encode("ascii")
        assert client.send(shell_command) == len(shell_command)
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            update = client.poll()
            if update.synced and after in _binding_text(update):
                break
            time.sleep(0.02)
        assert update.synced and after in _binding_text(update)
        assert client.identity.session_id == session_id
    finally:
        if client is not None:
            client.close()
        if session_id is not None:
            registry.stop(session_id)
        shutil.rmtree(root, ignore_errors=True)


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux fixture is unavailable")
def test_tmux_default_prefix_and_detach_stay_on_private_socket_in_jtt_right(monkeypatch):
    """Exercise a real nested tmux TUI only in a fresh JTT-owned PTY."""

    if Path(pwd.getpwuid(os.getuid()).pw_shell).resolve() != Path("/bin/bash").resolve():
        pytest.skip("fixture deliberately exercises the account's /bin/bash login shell")

    tmux = shutil.which("tmux")
    assert tmux is not None
    root = Path(tempfile.mkdtemp(prefix="jtt-private-tmux-it-"))
    root.chmod(0o700)
    source_root = Path(__file__).resolve().parents[1] / "src"
    import pyte
    pyte_root = Path(pyte.__file__).resolve().parent.parent
    _set_private_process_environment(monkeypatch, root, source_root, pyte_root)
    socket_path = root / "tmux-test.sock"
    assert not socket_path.exists()
    registry = TerminalRegistry(root=root / "state" / "terminals")
    client = None
    session_id = None
    shell_pid = None

    def private_tmux(*args: str) -> subprocess.CompletedProcess[str]:
        # -S always names the unique fixture socket; -f bypasses user tmux config.
        return subprocess.run(
            [tmux, "-S", str(socket_path), "-f", "/dev/null", *args],
            env={
                "PATH": os.defpath,
                "HOME": str(root / "shell-home"),
                "LANG": "C.UTF-8",
                "LC_ALL": "C.UTF-8",
                "TERM": "xterm",
            },
            capture_output=True,
            text=True,
            timeout=5.0,
            check=False,
        )

    try:
        client = registry.create("Private tmux fixture")
        session_id = client.identity.session_id
        shell_pid = client.identity.shell_pid
        update = client.poll()
        assert update.synced and update.screen is not None
        assert client.acquire()

        # The shell starts a new tmux server on a unique socket, then resumes
        # and prints a marker after the tmux client detaches.
        launch = (
            f"tmux -S {shlex.quote(str(socket_path))} -f /dev/null "
            "new-session -s jtt_fixture; printf '\\nJTT_TMUX_DETACHED_OK\\n'\n"
        )
        assert client.send(launch.encode("utf-8")) == len(launch.encode("utf-8"))
        deadline = time.monotonic() + 5.0
        listed = private_tmux("list-sessions")
        while listed.returncode != 0 and time.monotonic() < deadline:
            update = client.poll()
            listed = private_tmux("list-sessions")
            time.sleep(0.02)
        assert listed.returncode == 0 and "jtt_fixture" in listed.stdout
        assert socket_path.exists()
        deadline = time.monotonic() + 5.0
        attached = private_tmux("list-clients", "-F", "#{client_pid}")
        while not attached.stdout.strip() and time.monotonic() < deadline:
            update = client.poll()
            attached = private_tmux("list-clients", "-F", "#{client_pid}")
            time.sleep(0.02)
        assert attached.returncode == 0 and attached.stdout.strip()
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            update = client.poll()
            if update.synced and not update.more:
                break
            time.sleep(0.02)
        assert update.synced and not update.more, (
            f"tmux initial redraw did not resynchronize: state={update.renderer_state}; "
            f"output_tail={update.output[-800:]!r}"
        )

        inner_marker = "JTT_TMUX_INNER_OK"
        command = f"printf '{inner_marker}\\n'\n".encode("ascii")
        assert client.send(command) == len(command)
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            update = client.poll()
            if update.synced and inner_marker in _binding_text(update):
                break
            time.sleep(0.02)
        assert inner_marker in _binding_text(update)

        # tmux's default Ctrl+B prefix remains available to the nested program.
        assert client.send(b"\x02d") == 2
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            update = client.poll()
            if update.synced and "JTT_TMUX_DETACHED_OK" in _binding_text(update):
                break
            time.sleep(0.02)
        assert "JTT_TMUX_DETACHED_OK" in _binding_text(update)
        listed = private_tmux("list-sessions")
        assert listed.returncode == 0 and "jtt_fixture" in listed.stdout

        client.release()
        assert [(row.session_id, row.shell_pid) for row in registry.list_sessions()] == [
            (session_id, shell_pid)
        ]

        stopped = private_tmux("kill-server")
        assert stopped.returncode == 0
        deadline = time.monotonic() + 3.0
        probe = private_tmux("list-sessions")
        while probe.returncode == 0 and time.monotonic() < deadline:
            time.sleep(0.02)
            probe = private_tmux("list-sessions")
        assert probe.returncode != 0 and "no server running" in probe.stderr.lower()
        # tmux may leave the unlinked server's filesystem socket node behind;
        # TemporaryDirectory cleanup below removes this private fixture node.
        if socket_path.exists():
            assert stat.S_ISSOCK(socket_path.lstat().st_mode)
    finally:
        if client is not None:
            client.close()
        if socket_path.exists():
            try:
                private_tmux("kill-server")
            except (OSError, subprocess.SubprocessError):
                pass
        if session_id is not None:
            try:
                registry.stop(session_id)
            except (OSError, RuntimeError, ValueError):
                pass
        shutil.rmtree(root, ignore_errors=True)
