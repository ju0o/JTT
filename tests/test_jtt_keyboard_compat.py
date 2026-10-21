"""Strict JTT key configuration and byte-routing safety tests."""

from __future__ import annotations

import curses
import json
import os
import pty
import select
import signal
import struct
import termios
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from tmux_agent_tower.state.jtt_keys import (
    DEFAULT_JTT_KEYBINDINGS,
    JTTKeybindings,
    JTTKeybindingsError,
    load_jtt_keybindings,
)
from tmux_agent_tower.terminal import TerminalIdentity
from tmux_agent_tower.ui import jtt
from tmux_agent_tower.ui import ux_preview_v2 as renderer


def _private_home(path: Path) -> Path:
    path.mkdir(mode=0o700, parents=True)
    path.chmod(0o700)
    return path


def _write_config(root: Path, raw: str, mode: int = 0o600) -> Path:
    _private_home(root)
    path = root / "keybindings.json"
    path.write_text(raw, encoding="utf-8")
    path.chmod(mode)
    return path


def test_missing_key_config_uses_default_keys_and_four_second_timeout(tmp_path):
    assert load_jtt_keybindings(tmp_path / "not-created") == DEFAULT_JTT_KEYBINDINGS
    home = _private_home(tmp_path / "jtt")
    keys = load_jtt_keybindings(home)
    assert (keys.prefix, keys.prefix_byte) == ("Ctrl+G", 7)
    assert (keys.input_escape, keys.input_escape_byte) == ("Ctrl+]", 29)
    assert keys.timeout_seconds == 4.0
    assert not (home / "keybindings.json").exists()


def test_strict_private_config_independently_changes_prefix_and_escape(tmp_path):
    home = tmp_path / "jtt"
    _write_config(home, '{"version":1,"prefix":"Ctrl+P","input_escape":"Ctrl+K"}')
    keys = load_jtt_keybindings(home)
    assert keys.prefix == "Ctrl+P" and keys.prefix_byte == 16
    assert keys.input_escape == "Ctrl+K" and keys.input_escape_byte == 11
    assert keys.timeout_seconds == 4.0
    assert keys.matches_prefix("\x10") and keys.matches_prefix(16)
    assert keys.matches_input_escape("\x0b") and keys.matches_input_escape(11)


def test_owner_read_only_key_config_is_accepted(tmp_path):
    home = tmp_path / "jtt"
    _write_config(home, '{"version":1,"prefix":"Ctrl+P","input_escape":"Ctrl+]"}', 0o400)
    keys = load_jtt_keybindings(home)
    assert keys.prefix == "Ctrl+P" and keys.input_escape == "Ctrl+]"


@pytest.mark.parametrize("raw", [
    "{",
    '{"version":1,"prefix":"Ctrl+G","input_escape":"Ctrl+]","extra":0}',
    '{"version":1,"prefix":"Ctrl+G"}',
    '{"version":1.0,"prefix":"Ctrl+G","input_escape":"Ctrl+]"}',
    '{"version":true,"prefix":"Ctrl+G","input_escape":"Ctrl+]"}',
    '{"version":1,"prefix":"Ctrl+G","prefix":"Ctrl+P","input_escape":"Ctrl+]"}',
    '{"version":1,"prefix":"n","input_escape":"Ctrl+]"}',
    '{"version":1,"prefix":"Ctrl+H","input_escape":"Ctrl+]"}',
    '{"version":1,"prefix":"Ctrl+S","input_escape":"Ctrl+]"}',
    '{"version":1,"prefix":"Ctrl+C","input_escape":"Ctrl+]"}',
    '{"version":1,"prefix":"Ctrl+G","input_escape":"Ctrl+G"}',
])
def test_invalid_or_ambiguous_key_config_fails_closed(tmp_path, raw):
    home = tmp_path / "jtt"
    _write_config(home, raw)
    with pytest.raises(JTTKeybindingsError):
        load_jtt_keybindings(home)


@pytest.mark.parametrize("mode", [0o644, 0o640, 0o700, 0o200])
def test_key_config_requires_private_readable_non_executable_file(tmp_path, mode):
    home = tmp_path / "jtt"
    _write_config(home, '{"version":1,"prefix":"Ctrl+G","input_escape":"Ctrl+]"}', mode)
    with pytest.raises(JTTKeybindingsError):
        load_jtt_keybindings(home)


def test_key_config_rejects_symlink_hardlink_and_nonprivate_home(tmp_path):
    home = _private_home(tmp_path / "jtt")
    target = tmp_path / "config-target.json"
    target.write_text('{"version":1,"prefix":"Ctrl+G","input_escape":"Ctrl+]"}')
    target.chmod(0o600)
    (home / "keybindings.json").symlink_to(target)
    with pytest.raises(JTTKeybindingsError):
        load_jtt_keybindings(home)
    (home / "keybindings.json").unlink()

    path = home / "keybindings.json"
    path.write_text('{"version":1,"prefix":"Ctrl+G","input_escape":"Ctrl+]"}')
    path.chmod(0o600)
    os.link(path, home / "linked.json")
    with pytest.raises(JTTKeybindingsError):
        load_jtt_keybindings(home)
    (home / "linked.json").unlink()

    home.chmod(0o755)
    with pytest.raises(JTTKeybindingsError):
        load_jtt_keybindings(home)


def test_unsafe_key_config_stops_before_curses_or_terminal_creation(tmp_path, monkeypatch, capsys):
    home = tmp_path / "jtt"
    _write_config(home, '{"version":1,"prefix":"n","input_escape":"Ctrl+]"}')
    store = jtt.JTTWorkspaceStore(home)
    monkeypatch.setattr(jtt.curses, "wrapper", lambda *_args, **_kwargs: pytest.fail(
        "unsafe config must stop before curses starts"
    ))

    assert jtt.run_jtt_ui(store=store, registry=object()) == 2
    assert "keybindings.json" in capsys.readouterr().err
    assert not (home / "workspaces.json").exists()
    assert not (home / "terminals").exists()


class _FakeClient:
    def __init__(self, identity):
        self.identity = identity
        self.lease = True
        self.sent: list[bytes] = []
        self.releases = 0
        self.closed = False

    def send(self, data):
        self.sent.append(bytes(data))
        return len(data)

    def acquire(self):
        self.lease = True
        return True

    def release(self):
        self.lease = False
        self.releases += 1

    def close(self):
        self.closed = True


class _Screen:
    def getmaxyx(self):
        return (24, 80)


def _identity(index: int) -> TerminalIdentity:
    session_id = f"session-{index}"
    pid = 8000 + index
    return TerminalIdentity(session_id, f"generation-{index}", 7000 + index,
                            f"supervisor-{index}", pid, f"shell-{index}",
                            "/bin/bash", session_id, "running", renderer_state="ready")


def _state(count: int = 1, keybindings=DEFAULT_JTT_KEYBINDINGS):
    rows = []
    bindings = {}
    for index in range(count):
        identity = _identity(index)
        session_id = identity.session_id
        client = _FakeClient(identity)
        binding = jtt.ManagedTerminalBinding(client, identity, keybindings=keybindings)
        binding.input_mode = True
        binding.synced = True
        binding.screen = object()
        binding.renderer_state = "ready"
        binding.terminal_view_state = "running_ready"
        bindings[session_id] = binding
        rows.append({
            "kind": "pane", "pane_id": session_id, "target_id": session_id,
            "managed_id": session_id, "terminal_kind": "tower_managed",
            "managed_identity": {"session_id": session_id,
                                 "generation": identity.generation,
                                 "shell_pid": identity.shell_pid},
        })
    state = {
        "runtime_mode": "jtt", "workspace_id": "workspace-default",
        "panes": rows, "pane_index": 0, "selected_target_id": rows[0]["target_id"],
        "selected_signature": renderer._pane_signature(rows[0]), "active_panel": 2,
        "managed_terminals": bindings, "jtt_keybindings": keybindings,
        "jtt_prefix_pending": False, "jtt_prefix_deadline": 0.0,
        "jtt_prefix_target": None, "jtt_notice": "", "jtt_notice_until": 0.0,
        "commands": [],
    }
    state["jtt_prefix_handler"] = lambda _screen, current, command: (
        current["commands"].append(command) or False
    )
    return state


def _key(screen, state, value):
    return renderer._handle_key(screen, state, value)


def test_input_priority_bypasses_and_configured_escape_are_separate():
    keys = JTTKeybindings("Ctrl+P", "Ctrl+K", 16, 11)
    state = _state(keybindings=keys)
    screen = _Screen()
    client = state["managed_terminals"]["session-0"].client

    assert _key(screen, state, "n") is False
    assert client.sent == [b"n"]
    assert _key(screen, state, "\x10") is False  # prefix is consumed, not sent
    assert client.sent == [b"n"] and state["jtt_prefix_pending"]
    assert _key(screen, state, "n") is False
    assert state["commands"] == ["n"] and client.sent == [b"n"]

    assert _key(screen, state, 16) is False
    assert _key(screen, state, 16) is False  # doubled configured prefix => one literal byte
    assert _key(screen, state, "\x10") is False
    assert _key(screen, state, 11) is False  # prefix + input escape => one literal byte
    assert client.sent == [b"n", b"\x10", b"\x0b"]

    assert _key(screen, state, "\x0b") is False  # standalone input escape returns to navigation
    assert not state["managed_terminals"]["session-0"].input_mode
    assert client.sent == [b"n", b"\x10", b"\x0b"]


def test_ordinary_terminal_controls_and_utf8_flow_through_in_input_mode():
    state = _state()
    binding = state["managed_terminals"]["session-0"]
    for key in ("q", "Q", "\x1b", "\t", curses.KEY_UP, curses.KEY_LEFT, "한", "é"):
        assert _key(_Screen(), state, key) is False
    assert binding.client.sent == [
        b"q", b"Q", b"\x1b", b"\t", b"\x1b[A", b"\x1b[D", "한".encode(), "é".encode(),
    ]


def test_shift_tab_and_enter_use_terminal_protocol_bytes():
    assert renderer._key_bytes(curses.KEY_BTAB) == b"\x1b[Z"
    assert renderer._key_bytes("\n") == b"\n"  # legacy preview fixture contract
    assert renderer._jtt_key_bytes("\n") == b"\n"
    assert renderer._jtt_key_bytes("\r") == b"\r"
    assert renderer._jtt_key_bytes(curses.KEY_ENTER) == b"\r"


def test_pending_prefix_consumes_unknown_and_escape_cancel_never_leaks():
    state = _state()
    client = state["managed_terminals"]["session-0"].client
    assert _key(_Screen(), state, "\x07") is False
    assert _key(_Screen(), state, "\x1b") is False
    assert not state["jtt_prefix_pending"]
    assert _key(_Screen(), state, "\x07") is False
    assert _key(_Screen(), state, "z") is False
    assert not state["jtt_prefix_pending"]
    assert client.sent == []


@pytest.mark.parametrize("mutation", [
    "selection", "generation", "client_identity", "workspace", "lease", "sync",
])
def test_literal_bypass_fails_closed_when_captured_target_changes(mutation):
    state = _state(count=2)
    old_binding = state["managed_terminals"]["session-0"]
    assert _key(_Screen(), state, "\x07") is False
    if mutation == "selection":
        state["pane_index"] = 1
        state["selected_target_id"] = "session-1"
        state["selected_signature"] = renderer._pane_signature(state["panes"][1])
    elif mutation == "generation":
        state["panes"][0]["managed_identity"]["generation"] = "replacement-generation"
    elif mutation == "client_identity":
        old_binding.client.identity = _identity(99)
    elif mutation == "workspace":
        state["workspace_id"] = "workspace-replacement"
    elif mutation == "lease":
        old_binding.client.lease = False
    else:
        old_binding.synced = False

    assert _key(_Screen(), state, "\x07") is False
    assert old_binding.client.sent == []
    assert state["jtt_prefix_pending"] is False


def test_prefix_timeout_is_four_seconds_and_clears_without_leaking(monkeypatch):
    clock = [20.0]
    monkeypatch.setattr(renderer.time, "monotonic", lambda: clock[0])
    state = _state()
    client = state["managed_terminals"]["session-0"].client
    assert _key(_Screen(), state, "\x07") is False
    assert state["jtt_prefix_deadline"] == 24.0
    clock[0] = 23.999
    assert _key(_Screen(), state, "n") is False
    assert state["commands"] == ["n"] and client.sent == []

    assert _key(_Screen(), state, "\x07") is False
    clock[0] = 28.0
    assert _key(_Screen(), state, "n") is False
    assert state["jtt_prefix_pending"] is False
    assert client.sent == [b"n"]


def test_prefix_literal_requires_live_exact_input_lease():
    state = _state()
    binding = state["managed_terminals"]["session-0"]
    assert _key(_Screen(), state, "\x07") is False
    binding.client.lease = False
    assert _key(_Screen(), state, "\x07") is False
    assert binding.client.sent == []


def _real_curses_pty_audit() -> dict:
    """Run the production key router under a real curses PTY with a fake client."""

    audit_read, audit_write = os.pipe()
    pid, master = pty.fork()
    if pid == 0:
        os.close(audit_read)
        try:
            os.environ.clear()
            os.environ.update({"TERM": "xterm", "LANG": "C.UTF-8",
                               "LC_ALL": "C.UTF-8", "ESCDELAY": "25"})
            import locale
            locale.setlocale(locale.LC_ALL, "")
            state = _state()
            binding = state["managed_terminals"]["session-0"]
            state["commands"] = []

            def app(stdscr):
                stdscr.keypad(True)
                stdscr.timeout(1500)
                state["stdscr"] = stdscr
                events = []
                signals = []
                signal.signal(signal.SIGINT, lambda *_args: signals.append("INT"))
                signal.signal(signal.SIGTSTP, lambda *_args: signals.append("TSTP"))
                initial_attrs = termios.tcgetattr(0)
                # Model terminals whose cbreak setup leaves CR translation on.
                # JTT must remove it during the lease so CR and Ctrl+J stay
                # distinguishable regardless of the incoming PTY defaults.
                initial_attrs[0] |= termios.ICRNL
                termios.tcsetattr(0, termios.TCSANOW, initial_attrs)
                initial_attrs = termios.tcgetattr(0)
                original_monotonic = time.monotonic
                test_clock = [10.0]
                renderer.time.monotonic = lambda: test_clock[0]
                original_handle = renderer._handle_key

                def handle_and_audit(current_screen, current_state, key):
                    names = {
                        curses.KEY_UP: "UP", curses.KEY_DOWN: "DOWN",
                        curses.KEY_LEFT: "LEFT", curses.KEY_RIGHT: "RIGHT",
                        curses.KEY_HOME: "HOME", curses.KEY_END: "END",
                        curses.KEY_BACKSPACE: "BACKSPACE",
                        curses.KEY_DC: "DELETE", curses.KEY_BTAB: "SHIFT_TAB",
                        curses.KEY_ENTER: "ENTER_KEY",
                    }
                    if key in names:
                        event = names[key]
                    elif isinstance(key, str) and len(key) == 1:
                        codepoint = ord(key)
                        event = (f"BYTE_{codepoint:02x}" if codepoint < 0x100
                                 else f"UNICODE_{codepoint:04x}")
                    else:
                        event = str(key)
                    events.append(event)
                    if key == "z" and current_state.get("jtt_prefix_pending"):
                        # Exercise expiry through real curses input without a
                        # four-second wall-clock delay in the test suite.
                        test_clock[0] += DEFAULT_JTT_KEYBINDINGS.timeout_seconds
                    return original_handle(current_screen, current_state, key)

                # Selective raw terminal control preserves curses cbreak/keypad
                # operation but disables ISIG and IXON/IXOFF while leased.
                entered = binding.enter(stdscr, state)
                leased_attrs = termios.tcgetattr(0)
                if not entered:
                    raise AssertionError("fake leased PTY could not enter terminal input mode")
                # Signal readiness only after curses and the JTT lease have
                # both configured the actual PTY; otherwise Ctrl+C can race
                # the initial cbreak setup and become a process signal.
                os.write(audit_write, b"R")
                while binding.input_mode:
                    key = stdscr.get_wch()
                    handle_and_audit(stdscr, state, key)
                released_attrs = termios.tcgetattr(0)
                renderer.time.monotonic = original_monotonic
                return {
                    "commands": state["commands"],
                    "sent_hex": [item.hex() for item in binding.client.sent],
                    "input_mode": binding.input_mode,
                    "events": events,
                    "signals": signals,
                    "initial_isig": bool(initial_attrs[3] & termios.ISIG),
                    "leased_isig": bool(leased_attrs[3] & termios.ISIG),
                    "restored_isig": bool(released_attrs[3] & termios.ISIG),
                    "initial_ixon": bool(initial_attrs[0] & termios.IXON),
                    "leased_ixon": bool(leased_attrs[0] & termios.IXON),
                    "restored_ixon": bool(released_attrs[0] & termios.IXON),
                    "initial_icrnl": bool(initial_attrs[0] & termios.ICRNL),
                    "leased_icrnl": bool(leased_attrs[0] & termios.ICRNL),
                    "restored_icrnl": bool(released_attrs[0] & termios.ICRNL),
                    "released_attrs_exact": released_attrs == initial_attrs,
                }

            before_wrapper_attrs = termios.tcgetattr(0)
            before_wrapper_attrs[0] |= termios.ICRNL
            termios.tcsetattr(0, termios.TCSANOW, before_wrapper_attrs)
            before_wrapper_attrs = termios.tcgetattr(0)
            audit = curses.wrapper(app)
            after_wrapper_attrs = termios.tcgetattr(0)
            audit["wrapper_restored_exact"] = after_wrapper_attrs == before_wrapper_attrs
            audit["wrapper_restored_isig"] = bool(after_wrapper_attrs[3] & termios.ISIG)
            audit["wrapper_restored_ixon"] = bool(after_wrapper_attrs[0] & termios.IXON)
            os.write(audit_write, json.dumps(audit, ensure_ascii=False).encode("utf-8"))
            os._exit(0)
        except BaseException as exc:
            try:
                os.write(audit_write, json.dumps({"error": type(exc).__name__,
                                                   "detail": str(exc)}).encode("utf-8"))
            except OSError:
                pass
            os._exit(1)

    os.close(audit_write)
    os.set_blocking(master, False)
    os.set_blocking(audit_read, False)
    fcntl = __import__("fcntl")
    fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
    transcript = bytearray()
    terminal_output = bytearray()
    deadline = time.monotonic() + 8.0
    try:
        # Wait only for the child test harness readiness byte, not a product timing assumption.
        while (b"R" not in transcript and b'"error"' not in transcript
               and time.monotonic() < deadline):
            readable, _, _ = select.select([audit_read], [], [], 0.1)
            if readable:
                transcript.extend(os.read(audit_read, 65536))
        if b'"error"' in transcript:
            pytest.fail(bytes(transcript).decode("utf-8", errors="replace"))
        assert transcript.startswith(b"R"), "PTY audit child did not become ready"
        # curses enters the terminal's application-keypad mode. xterm's
        # application cursor-up sequence is therefore ESC O A, not CSI.
        # The first Ctrl+G+n is a JTT command; doubled prefix and prefix+escape
        # are literal bypasses. Prefix+Esc cancels, and prefix+z expires at the
        # fake monotonic boundary before z is routed to the child.
        os.write(master, b"\x01\x02\x03\x04\x07n\x07\x07\x07\x1d\x07\x1b\x07z")
        os.write(master, b"\x18\x1a\x13\x11")
        time.sleep(0.08)
        os.write(master, b"\x1b")
        time.sleep(0.08)  # Let curses resolve standalone ESC after keypad parsing.
        os.write(master, b"\r\n\t\x1b[Z\x1bOA\x1bOB\x1bOD\x1bOC\x1bOH\x1bOF\x1b[3~")
        os.write(master, b"\x7f" + "한".encode("utf-8") + b"\x1d")

        child_done = False
        while time.monotonic() < deadline:
            readable, _, _ = select.select([audit_read, master], [], [], 0.05)
            for fd in readable:
                try:
                    data = os.read(fd, 65536)
                except OSError:
                    data = b""
                if fd == audit_read:
                    if data:
                        transcript.extend(data)
                elif data:
                    terminal_output.extend(data)
            waited, _status = os.waitpid(pid, os.WNOHANG)
            if waited:
                child_done = True
                pid = -1
                break
        assert child_done, "PTY audit child did not finish after configured input escape"
        payload = bytes(transcript[1:]).strip()
        assert payload, "PTY audit child returned no byte audit"
        result = json.loads(payload.decode("utf-8"))
        assert "error" not in result, result
        return result
    finally:
        if pid > 0:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                os.waitpid(pid, 0)
            except ChildProcessError:
                pass
        for fd in (master, audit_read):
            try:
                os.close(fd)
            except OSError:
                pass


def _real_curses_signal_audit(signum: int, home: Path) -> dict:
    """Signal the real JTT wrapper while its PTY input lease is active."""

    audit_read, audit_write = os.pipe()
    pid, master = pty.fork()
    if pid == 0:
        os.close(audit_read)
        try:
            os.environ.clear()
            os.environ.update({"TERM": "xterm", "LANG": "C.UTF-8",
                               "LC_ALL": "C.UTF-8", "ESCDELAY": "25"})
            import locale
            locale.setlocale(locale.LC_ALL, "")
            home.chmod(0o700)
            state = _state()
            binding = state["managed_terminals"]["session-0"]
            before_attrs = termios.tcgetattr(0)
            previous_handler = signal.getsignal(signum)
            lease_attrs = None
            entered = False

            def fake_build_state(_store, _registry, clients, **_kwargs):
                clients.update(state["managed_terminals"])
                return state

            def begin_lease(current_state, stdscr):
                nonlocal lease_attrs, entered
                if entered:
                    return
                entered = binding.enter(stdscr, current_state)
                if not entered:
                    raise AssertionError("JTT did not acquire the fixture input lease")
                lease_attrs = termios.tcgetattr(0)
                # run_jtt_ui has installed its catchable signal handlers before
                # _run reaches this first refresh callback.
                os.write(audit_write, b"R")

            jtt.build_state = fake_build_state
            jtt._refresh_and_resize = begin_lease
            renderer.draw_preview_v2 = lambda *_args, **_kwargs: None
            status = jtt.run_jtt_ui(store=SimpleNamespace(root=home), registry=object())
            after_attrs = termios.tcgetattr(0)
            audit = {
                "status": status,
                "signal": signum,
                "lease_entered": entered,
                "lease_isig": bool(lease_attrs[3] & termios.ISIG),
                "lease_ixon": bool(lease_attrs[0] & termios.IXON),
                "lease_icrnl": bool(lease_attrs[0] & termios.ICRNL),
                "restored_exact": after_attrs == before_attrs,
                "prior_handler_restored": signal.getsignal(signum) == previous_handler,
                "client_released": not binding.client.lease,
                "client_closed": binding.client.closed,
            }
            os.write(audit_write, json.dumps(audit).encode("ascii"))
            os._exit(0)
        except BaseException as exc:
            try:
                os.write(audit_write, json.dumps({"error": type(exc).__name__,
                                                   "detail": str(exc)}).encode("utf-8"))
            except OSError:
                pass
            os._exit(1)

    os.close(audit_write)
    os.set_blocking(master, False)
    os.set_blocking(audit_read, False)
    fcntl = __import__("fcntl")
    fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
    transcript = bytearray()
    deadline = time.monotonic() + 5.0
    child_done = False
    try:
        while b"R" not in transcript and b'"error"' not in transcript:
            if time.monotonic() >= deadline:
                pytest.fail("JTT did not reach the leased input state")
            readable, _, _ = select.select([audit_read], [], [], 0.05)
            if readable:
                transcript.extend(os.read(audit_read, 65536))
        if b'"error"' in transcript:
            pytest.fail(bytes(transcript).decode("utf-8", errors="replace"))
        os.kill(pid, signum)
        while time.monotonic() < deadline:
            readable, _, _ = select.select([audit_read], [], [], 0.05)
            if readable:
                transcript.extend(os.read(audit_read, 65536))
            waited, _status = os.waitpid(pid, os.WNOHANG)
            if waited:
                child_done = True
                pid = -1
                break
        assert child_done, "JTT did not unwind after catchable termination signal"
        payload = bytes(transcript[1:]).strip()
        assert payload, "signal audit returned no result"
        result = json.loads(payload.decode("ascii"))
        assert "error" not in result, result
        return result
    finally:
        if pid > 0:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                os.waitpid(pid, 0)
            except ChildProcessError:
                pass
        for fd in (master, audit_read):
            try:
                os.close(fd)
            except OSError:
                pass


def test_real_curses_pty_routes_control_escape_utf8_and_special_keys_as_expected():
    result = _real_curses_pty_audit()
    assert result["commands"] == ["n"]
    assert result["input_mode"] is False
    assert result["initial_isig"] is True and result["leased_isig"] is False
    assert result["restored_isig"] is True
    assert result["initial_ixon"] is True and result["leased_ixon"] is False
    assert result["restored_ixon"] is True
    assert result["initial_icrnl"] is True and result["leased_icrnl"] is False
    assert result["restored_icrnl"] is True
    assert result["released_attrs_exact"] is True
    assert result["wrapper_restored_exact"] is True
    assert result["wrapper_restored_isig"] is True
    assert result["wrapper_restored_ixon"] is True
    assert result["signals"] == []
    assert result["sent_hex"] == [
        "01", "02", "03", "04", "07", "1d", "7a",
        "18", "1a", "13", "11", "1b", "0d", "0a", "09", "1b5b5a",
        "1b5b41", "1b5b42", "1b5b44", "1b5b43", "1b5b48", "1b5b46",
        "1b5b337e", "7f", "ed959c",
    ]
    assert {"UP", "DOWN", "LEFT", "RIGHT", "HOME", "END", "DELETE", "SHIFT_TAB"} <= set(result["events"])
    assert {f"BYTE_{value:02x}" for value in
            (1, 2, 3, 4, 7, 24, 26, 19, 17, 27, 29, 9)} <= set(result["events"])
    assert {"BACKSPACE", "DELETE"} <= set(result["events"])
    assert "BYTE_0d" in result["events"] or "BYTE_0a" in result["events"]
    assert "UNICODE_d55c" in result["events"]  # UTF-8 is decoded as one code point.


@pytest.mark.parametrize("signum", [signal.SIGHUP, signal.SIGTERM, signal.SIGQUIT])
def test_catchable_termination_during_input_lease_restores_jtt_tty(tmp_path, signum):
    home = tmp_path / "private-jtt"
    home.mkdir(mode=0o700)
    home.chmod(0o700)
    result = _real_curses_signal_audit(signum, home)
    assert result["status"] == 128 + signum
    assert result["lease_entered"] is True
    assert result["lease_isig"] is False
    assert result["lease_ixon"] is False
    assert result["lease_icrnl"] is False
    assert result["restored_exact"] is True
    assert result["prior_handler_restored"] is True
    assert result["client_released"] is True
    assert result["client_closed"] is True
