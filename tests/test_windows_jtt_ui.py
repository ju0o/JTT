"""Textual JTT UI tests; native ConPTY coverage runs on Windows only."""

from __future__ import annotations

import asyncio
import importlib
import io
import sys
import time
from types import SimpleNamespace

import pytest
from textual.widgets import Button, Static

import ju0_terminal_tower  # noqa: F401 - activates the guarded product namespace
from ju0_terminal_tower.terminal.manager import (
    TerminalCell,
    TerminalIdentity,
    TerminalObservation,
    TerminalScreen,
)
from ju0_terminal_tower.ui.windows_jtt import WindowsJTTApp, _same_identity


def _screen(text: str = "JTT_TEST_READY") -> TerminalScreen:
    cells = tuple(TerminalCell(char, "default", "default", False, False, False,
                               False, False, False) for char in text)
    return TerminalScreen((cells,), min(len(cells), 3), 0, True)


def _identity(index: int, *, state: str = "running") -> TerminalIdentity:
    return TerminalIdentity(
        session_id=f"{index:032x}", generation=f"{index + 100:032x}",
        supervisor_pid=2000 + index, supervisor_start=f"supervisor-{index}",
        shell_pid=3000 + index, shell_start=f"shell-{index}",
        shell_path="powershell.exe", name=f"PowerShell {index}", state=state,
    )


class FakeClient:
    def __init__(self, identity):
        self.identity = identity
        self.lease = False
        self.closed = False
        self.sent: list[bytes] = []
        self.resizes: list[tuple[int, int]] = []

    def observe(self):
        return TerminalObservation(
            session_id=self.identity.session_id, generation=self.identity.generation,
            shell_pid=self.identity.shell_pid, sequence=len(self.sent), state="running",
            synced=True, columns=100, lines=30, screen=_screen(), renderer_state="ready",
        )

    def acquire(self):
        self.lease = True
        return True

    def release(self):
        self.lease = False

    def send(self, data):
        assert self.lease
        self.sent.append(data)
        return len(data)

    def resize(self, columns, lines):
        assert self.lease
        self.resizes.append((columns, lines))

    def close(self):
        self.closed = True
        self.lease = False


class FakeRegistry:
    def __init__(self, sessions=()):
        self.sessions = list(sessions)
        self.clients: list[FakeClient] = []
        self.create_count = 0
        self.stop_calls = []

    def list_sessions(self):
        return tuple(self.sessions)

    def connect(self, session_id):
        identity = next(item for item in self.sessions if item.session_id == session_id)
        client = FakeClient(identity)
        self.clients.append(client)
        return client

    def create(self, name=None):
        self.create_count += 1
        identity = _identity(self.create_count)
        identity = TerminalIdentity(**{**identity.__dict__, "name": name or identity.name})
        self.sessions.append(identity)
        client = FakeClient(identity)
        self.clients.append(client)
        return client

    def stop(self, session_id, *, expected_generation=None, expected_shell_pid=None):
        identity = next(item for item in self.sessions if item.session_id == session_id)
        if (identity.generation != expected_generation or identity.shell_pid != expected_shell_pid):
            raise RuntimeError("identity mismatch")
        self.stop_calls.append(session_id)
        self.sessions.remove(identity)


def test_cli_dispatches_native_windows_ui_before_curses(monkeypatch):
    jtt_module = importlib.import_module("tmux_agent_tower.jtt")
    windows_ui = importlib.import_module("tmux_agent_tower.ui.windows_jtt")
    called = []

    class TTY:
        @staticmethod
        def isatty():
            return True

    fake_sys = SimpleNamespace(
        platform="win32", stdin=TTY(), stdout=TTY(), stderr=io.StringIO()
    )
    monkeypatch.setattr(jtt_module, "sys", fake_sys)
    monkeypatch.setattr(windows_ui, "run_jtt_windows_ui", lambda: called.append(True) or 17)
    assert jtt_module.cli([]) == 17
    assert called == [True]


@pytest.mark.parametrize("size", [(160, 44), (80, 24)])
def test_windows_ui_has_three_panels_and_does_not_autocreate(size):
    async def scenario():
        registry = FakeRegistry()
        app = WindowsJTTApp(registry)
        async with app.run_test(size=size) as pilot:
            await pilot.pause(0.1)
            assert app.query_one("#left")
            assert app.query_one("#center")
            assert app.query_one("#right")
            assert registry.create_count == 0
            assert "터미널 0개" in str(app.query_one("#terminal-summary", Static).render())
            regions = [app.query_one("#left").region,
                       app.query_one("#center").region,
                       app.query_one("#right").region]
            assert all(region.width > 0 and region.height > 0 for region in regions)
            assert regions[0].x < regions[1].x < regions[2].x

    asyncio.run(scenario())


def test_create_prefix_input_switch_and_detach_keep_sessions():
    async def scenario():
        registry = FakeRegistry()
        app = WindowsJTTApp(registry)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.05)
            await pilot.press("ctrl+g", "n")
            await pilot.pause(0.1)
            assert registry.create_count == 1
            assert app.input_mode is True
            first = app.client
            assert first is not None and first.lease
            assert app.query_one("#live").has_focus
            await pilot.press("ctrl+g", "ctrl+g")
            await pilot.press("q", "space", "enter", "backspace", "tab", "shift+tab")
            assert first.sent[:7] == [
                b"\x07", b"q", b" ", b"\r", b"\x7f", b"\t", b"\x1b[Z",
            ]

            await pilot.press("ctrl+g", "n")
            await pilot.pause(0.1)
            assert registry.create_count == 2
            second = app.client
            assert second is not None and second is not first and second.lease
            assert first.closed is True
            await pilot.press("x")
            assert second.sent[-1] == b"x"
            await pilot.press("ctrl+]")
            assert app.input_mode is False
            await pilot.press("ctrl+g", "d")
            await pilot.pause(0.1)
            assert app.is_running is False
            assert len(registry.sessions) == 2
            assert registry.stop_calls == []

    asyncio.run(scenario())


def test_card_switch_releases_previous_lease_and_stale_identity_hides_screen():
    async def scenario():
        first, second = _identity(1), _identity(2)
        registry = FakeRegistry([first, second])
        app = WindowsJTTApp(registry)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.15)
            assert app.selected_id == first.session_id
            assert app.client is not None
            await app.enter_input_mode()
            old_client = app.client
            await pilot.click("#terminal-" + second.session_id)
            assert old_client is not None and old_client.closed
            assert app.selected_id == second.session_id
            assert app.input_mode is False
            await pilot.click("#live")
            assert app.input_mode is True
            await app.leave_input_mode()

            changed = TerminalIdentity(**{**second.__dict__, "generation": "f" * 32})
            registry.sessions[1] = changed
            await app._refresh_sessions()
            assert app.selected_id is None
            assert app.client is None
            assert "신원" in str(app.query_one("#live", Static).render())

    asyncio.run(scenario())


def test_stop_confirmation_defaults_to_cancel_and_revalidates_identity():
    async def scenario():
        identity = _identity(1)
        registry = FakeRegistry([identity])
        app = WindowsJTTApp(registry)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.1)
            await app._stop_selected()
            await pilot.pause(0.05)
            assert app.screen.query_one("#cancel-stop", Button).has_focus
            await pilot.click("#cancel-stop")
            await pilot.pause(0.05)
            assert registry.stop_calls == []
            assert registry.sessions == [identity]

    asyncio.run(scenario())


def test_vt_screen_text_keeps_wide_glyph_cells_adjacent():
    import pyte

    from ju0_terminal_tower.ui.windows_jtt import _screen_text

    screen = pyte.Screen(12, 2)
    pyte.Stream(screen).feed("JTT 한글 OK")
    cells = tuple(TerminalCell(
        screen.buffer[0][x].data, screen.buffer[0][x].fg, screen.buffer[0][x].bg,
        bool(screen.buffer[0][x].bold), bool(screen.buffer[0][x].italics),
        bool(screen.buffer[0][x].underscore), bool(screen.buffer[0][x].reverse),
        bool(screen.buffer[0][x].blink), bool(screen.buffer[0][x].strikethrough),
    ) for x in range(screen.columns))
    rendered = _screen_text(TerminalScreen((cells,), 0, 0, True), 12, 1).plain
    assert rendered.startswith("JTT 한글 OK")


@pytest.mark.skipif(sys.platform != "win32", reason="native ConPTY required")
def test_native_windows_ui_create_input_detach_reconnect_and_safe_stop(tmp_path):
    from ju0_terminal_tower.terminal.windows_manager import TerminalRegistry, _process_facts

    root = tmp_path / "isolated-jtt-home"
    registry = TerminalRegistry(root=root / "terminals")
    app = WindowsJTTApp(registry)
    created = {}

    async def first_ui():
        async with app.run_test(size=(160, 44)) as pilot:
            await pilot.pause(0.1)
            assert registry.list_sessions() == ()
            await pilot.click("#new-terminal")
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline and not app.input_mode:
                await pilot.pause(0.1)
            assert app.input_mode, str(app.query_one("#statusline", Static).render())
            identity = app.selected_identity
            assert identity is not None
            created.update(identity=identity)
            # PowerShell builds the nonce-bearing output from separate source
            # fragments, so the completed result cannot be mistaken for echo.
            nonce = identity.session_id[:10]
            expected_result = f"JTT_UI_RESULT_{nonce}_END"
            command = (
                f"[Console]::WriteLine('JTT_UI_' + 'RESULT_{nonce}' + '_END')"
            )
            assert expected_result not in command
            assert (app.input_mode and app.client is not None and app.client.lease), (
                "input lease was lost before submitting the command; "
                f"status={app.query_one('#statusline', Static).render()}"
            )
            # Submit the line and CR as one lease-checked UI write. The fake UI
            # tests separately cover key-to-byte mapping for Enter; this native
            # check proves PowerShell executed the command, not merely echoed it.
            await app._send_bytes((command + "\r").encode("utf-8"))
            deadline = time.monotonic() + 20
            rendered = ""
            while time.monotonic() < deadline:
                rendered = str(app.query_one("#live", Static).render())
                if expected_result in rendered:
                    break
                await pilot.pause(0.1)
            rendered = str(app.query_one("#live", Static).render())
            assert expected_result in rendered
            await pilot.press("ctrl+g", "d")
            await pilot.pause(0.1)
            assert app.is_running is False

    try:
        asyncio.run(first_ui())
        identity = created["identity"]
        remaining = registry.list_sessions()
        assert len(remaining) == 1
        assert _same_identity(identity, remaining[0])
        assert remaining[0].state == "running"

        reconnect = WindowsJTTApp(registry)

        async def second_ui():
            async with reconnect.run_test(size=(80, 24)) as pilot:
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline and reconnect.client is None:
                    await pilot.pause(0.1)
                assert reconnect.selected_id == identity.session_id
                assert reconnect.client is not None
                assert _same_identity(identity, reconnect.client.identity)
                await pilot.press("ctrl+g", "d")
                await pilot.pause(0.1)

        asyncio.run(second_ui())

        stop_app = WindowsJTTApp(registry)

        async def stop_ui():
            async with stop_app.run_test(size=(160, 44)) as pilot:
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline and stop_app.client is None:
                    await pilot.pause(0.1)
                assert stop_app.selected_identity is not None
                assert _same_identity(identity, stop_app.selected_identity)

                async def assert_still_running(expected):
                    sessions = registry.list_sessions()
                    assert len(sessions) == 1
                    assert _same_identity(expected, sessions[0])
                    probe = registry.connect(expected.session_id)
                    try:
                        observation = probe.observe()
                        assert _same_identity(expected, probe.identity)
                        assert observation.state == "running"
                        assert observation.session_id == expected.session_id
                        assert observation.generation == expected.generation
                        assert observation.shell_pid == expected.shell_pid
                    finally:
                        probe.close()

                # The default action is cancel. Confirm the dialog opened from
                # the real UI command and that cancellation leaves the PTY alive.
                await pilot.press("ctrl+g", "x")
                assert stop_app.screen.query_one("#cancel-stop", Button).has_focus
                await pilot.click("#cancel-stop")
                await assert_still_running(identity)

                # Simulate a stale card identity without changing persisted
                # registry metadata or the owned PowerShell process.
                stale_identity = TerminalIdentity(**{
                    **identity.__dict__, "generation": "f" * 32,
                })
                stop_app.selected_identity = stale_identity
                mismatch_finished = asyncio.Event()
                finish_stop = stop_app._finish_stop

                async def record_mismatch_completion(expected, _finish=finish_stop):
                    await _finish(expected)
                    mismatch_finished.set()

                stop_app._finish_stop = record_mismatch_completion
                await pilot.press("ctrl+g", "x")
                await pilot.click("#confirm-stop")
                await asyncio.wait_for(mismatch_finished.wait(), timeout=10)
                remaining = registry.list_sessions()
                assert len(remaining) == 1 and _same_identity(identity, remaining[0])
                await assert_still_running(identity)

                # Restore the verified identity, open the same confirmation,
                # and approve. The registry performs its generation/PID checks
                # before stopping this isolated ConPTY session.
                await stop_app._select_session(identity, focus=False)
                approved_finished = asyncio.Event()
                finish_stop = stop_app._finish_stop

                async def record_approved_completion(expected, _finish=finish_stop):
                    await _finish(expected)
                    approved_finished.set()

                stop_app._finish_stop = record_approved_completion
                await pilot.press("ctrl+g", "x")
                assert stop_app.screen.query_one("#cancel-stop", Button).has_focus
                await pilot.click("#confirm-stop")
                await asyncio.wait_for(approved_finished.wait(), timeout=15)
                assert registry.list_sessions() == ()

        asyncio.run(stop_ui())
    finally:
        for identity in registry.list_sessions():
            registry.stop(identity.session_id, expected_generation=identity.generation,
                          expected_shell_pid=identity.shell_pid)
    assert registry.list_sessions() == ()
    shell_created, _shell_image, shell_alive = _process_facts(
        created["identity"].shell_pid,
        expected_start=created["identity"].shell_start,
        verify_image=False,
    )
    assert not (shell_alive and shell_created == created["identity"].shell_start)
