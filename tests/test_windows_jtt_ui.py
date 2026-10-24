"""Textual JTT UI tests; native ConPTY coverage runs on Windows only."""

from __future__ import annotations

import asyncio
import importlib
import io
import sys
import time
from types import SimpleNamespace

import pytest
from textual.screen import ModalScreen
from textual.widgets import Button, Static

import ju0_terminal_tower  # noqa: F401 - activates the guarded product namespace
from ju0_terminal_tower.state.jtt_layouts import JTTLayoutStore
from ju0_terminal_tower.state.jtt_workspaces import JTTWorkspaceStore
from ju0_terminal_tower.terminal.manager import (
    TerminalCell,
    TerminalIdentity,
    TerminalObservation,
    TerminalScreen,
)
from ju0_terminal_tower.ui.windows_jtt import WindowsJTTApp, _same_identity
from ju0_terminal_tower.ui.windows_mini_live import identity_key as _mini_identity_key


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


async def _wait_for_ui_state(pilot, predicate, reason: str, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            pytest.fail(f"timed out waiting for UI state: {reason}")
        await pilot.pause(min(0.05, remaining))


class FakeClient:
    def __init__(self, identity):
        self.identity = identity
        self.lease = False
        self.closed = False
        self.sent: list[bytes] = []
        self.resizes: list[tuple[int, int]] = []
        self.acquire_calls = 0
        self.observation_state = "running"
        self.screen_text = "SESSION_" + self.identity.session_id[-4:]

    def observe(self):
        return TerminalObservation(
            session_id=self.identity.session_id, generation=self.identity.generation,
            shell_pid=self.identity.shell_pid, sequence=len(self.sent),
            state=self.observation_state,
            synced=True, columns=100, lines=30,
            screen=_screen(self.screen_text),
            renderer_state="ready",
        )

    def acquire(self):
        self.acquire_calls += 1
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
        self.list_calls = 0

    def list_sessions(self):
        self.list_calls += 1
        return tuple(self.sessions)

    def connect(self, session_id, *, timeout=3.0):
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
            deadline = asyncio.get_running_loop().time() + 2.0
            while (app._busy or app.selected_id != first.session_id
                   or app.client is None):
                if asyncio.get_running_loop().time() >= deadline:
                    pytest.fail("initial terminal selection did not become ready")
                await asyncio.sleep(0.01)
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


def test_exited_live_observation_clears_right_and_mini_card_screen():
    async def scenario():
        identity = _identity(1)
        registry = FakeRegistry([identity])
        app = WindowsJTTApp(registry)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.15)
            client = app.client
            assert client is not None
            key = _mini_identity_key(identity)
            stale_screen = _screen("STALE_CARD")
            cached = (key, 77, time.monotonic(), stale_screen)
            app._right_observation = cached
            app._mini_cache[identity.session_id] = cached
            app._card_signature = None
            await app._render_cards()
            card = app.query_one("#terminal-" + identity.session_id, Button)
            assert "STALE_CARD" in card.label.plain

            await app.enter_input_mode()
            assert client.lease
            sent_before = len(client.sent)
            resizes_before = len(client.resizes)
            client.observation_state = "exited"
            await app._refresh_live()

            assert not app.input_mode
            assert not client.lease
            assert app._right_observation is None
            assert identity.session_id not in app._mini_cache
            card = app.query_one("#terminal-" + identity.session_id, Button)
            assert "STALE_CARD" not in card.label.plain
            assert len(client.sent) == sent_before
            assert len(client.resizes) == resizes_before

    asyncio.run(scenario())


def test_mini_live_skips_detached_cards_after_window_creation(tmp_path):
    async def scenario():
        identity = _identity(1)
        registry = FakeRegistry([identity])
        workspace_store = JTTWorkspaceStore(tmp_path / "jtt")
        workspace_store.set_terminal_ids(
            workspace_store.DEFAULT_ID, [identity.session_id]
        )
        app = WindowsJTTApp(
            registry, workspace_store=workspace_store,
            layout_store=JTTLayoutStore(workspace_store.root),
        )
        # Drive refreshes explicitly so the test can place the missing-widget
        # boundary deterministically instead of depending on interval timing.
        app.set_interval = lambda *_args, **_kwargs: SimpleNamespace(stop=lambda: None)
        async with app.run_test(size=(160, 44)) as pilot:
            await pilot.pause(0.05)
            await pilot.click("#new-window")
            await pilot.press("enter")
            await pilot.pause(0.05)
            assert app.selected_window_id != "window-default"
            cards = app.query_one("#terminal-cards")
            await cards.remove()

            sampler = app._mini_sampler
            assert sampler is not None
            sampler.take = lambda: None
            sampler.submit = lambda *_args, **_kwargs: pytest.fail(
                "Mini LIVE must not submit observations without its card view"
            )
            await app._refresh_mini_live()

    asyncio.run(scenario())


def test_unmount_stops_refresh_timers_and_closes_mini_sampler():
    class TimerProbe:
        def __init__(self, timer):
            self.timer = timer
            self.stopped = False

        def stop(self):
            self.stopped = True
            self.timer.stop()

    async def scenario():
        app = WindowsJTTApp(FakeRegistry([_identity(1), _identity(2)]), auto_select=False)
        timers = []
        set_interval = app.set_interval
        submit_calls = []

        def track_interval(*args, **kwargs):
            timer = set_interval(*args, **kwargs)
            probe = TimerProbe(timer)
            timers.append(probe)
            return probe

        app.set_interval = track_interval
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.05)
            sampler = app._mini_sampler
            assert sampler is not None
            sampler.submit = lambda *_args, **_kwargs: submit_calls.append("submit") or True
            await app._refresh_mini_live()
            assert submit_calls == ["submit"]
        assert len(timers) == 3
        assert all(timer.stopped for timer in timers), (
            f"closing={app._mini_live_stopping}, stopped={[timer.stopped for timer in timers]}, "
            f"remaining={len(app._refresh_timers)}"
        )
        assert app._mini_sampler is None
        assert sampler._stopping
        assert not sampler._thread.is_alive()
        await app._refresh_mini_live()
        assert submit_calls == ["submit"]

    asyncio.run(scenario())


def test_mini_live_render_aborts_when_cards_detach_during_unmount():
    async def scenario():
        identity = _identity(1)
        app = WindowsJTTApp(FakeRegistry([identity]))
        app.set_interval = lambda *_args, **_kwargs: SimpleNamespace(
            stop=lambda: None
        )
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.05)
            cards = app.query_one("#terminal-cards")
            sampler = app._mini_sampler
            assert sampler is not None
            sample = {
                "session_id": identity.session_id,
                "identity": _mini_identity_key(identity),
                "sequence": 1,
                "screen": _screen("FRESH_MINI_SAMPLE"),
                "synced": True,
            }
            sampler.take = lambda: ((app._mini_scope(), [sample]))
            submits = []
            sampler.submit = lambda *_args, **_kwargs: submits.append(True) or True

            removing = asyncio.Event()
            resume_render = asyncio.Event()
            remove_children = cards.remove_children

            async def pause_after_detaching_children():
                await remove_children()
                removing.set()
                await resume_render.wait()

            cards.remove_children = pause_after_detaching_children
            app._card_signature = None
            refresh = asyncio.create_task(app._refresh_mini_live())
            await asyncio.wait_for(removing.wait(), timeout=1.0)

            # Simulate the shutdown latch and detach the view while its
            # renderer is suspended between clearing and remounting widgets.
            app._stop_refresh_timers()
            await cards.remove()
            resume_render.set()
            await asyncio.wait_for(refresh, timeout=1.0)
            assert submits == []

    asyncio.run(scenario())


def test_logical_window_switch_filters_cards_and_releases_old_lease(tmp_path):
    async def scenario():
        first, second = _identity(1), _identity(2)
        registry = FakeRegistry([first, second])
        workspace_store = JTTWorkspaceStore(tmp_path / "jtt")
        workspace_id = workspace_store.DEFAULT_ID
        workspace_store.set_terminal_ids(
            workspace_id, [first.session_id, second.session_id]
        )
        layout_store = JTTLayoutStore(workspace_store.root)
        window = layout_store.create_window(workspace_id, "folder-default", "Build")
        layout_store.move_terminal(
            workspace_id, second.session_id, window["id"],
            [first.session_id, second.session_id],
        )
        app = WindowsJTTApp(
            registry, workspace_store=workspace_store, layout_store=layout_store,
        )
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.15)
            assert app.selected_window_id == "window-default"
            assert set(app.sessions) == {first.session_id}
            assert app.selected_id == first.session_id
            await app.enter_input_mode()
            old_client = app.client
            assert old_client is not None and old_client.lease

            await pilot.click("#window-" + window["id"])
            await pilot.pause(0.1)

            assert old_client.closed and not old_client.lease
            assert app.selected_window_id == window["id"]
            assert set(app.sessions) == {second.session_id}
            assert app.selected_id == second.session_id
            assert app.client is not None and app.client.identity.session_id == second.session_id
            assert app.client.lease is False
            assert registry.create_count == 0

    asyncio.run(scenario())


def test_keyboard_window_switch_keeps_left_focus_and_does_not_acquire_new_lease(tmp_path):
    async def scenario():
        first, second = _identity(1), _identity(2)
        registry = FakeRegistry([first, second])
        workspace_store = JTTWorkspaceStore(tmp_path / "jtt")
        workspace_id = workspace_store.DEFAULT_ID
        workspace_store.set_terminal_ids(
            workspace_id, [first.session_id, second.session_id]
        )
        layout_store = JTTLayoutStore(workspace_store.root)
        next_window = layout_store.create_window(workspace_id, "folder-default", "Review")
        layout_store.move_terminal(
            workspace_id, second.session_id, next_window["id"],
            [first.session_id, second.session_id],
        )
        app = WindowsJTTApp(
            registry, workspace_store=workspace_store, layout_store=layout_store,
        )
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.15)
            await app.enter_input_mode()
            old_client = app.client
            assert old_client is not None and old_client.lease

            await pilot.press("ctrl+g", "w", "down")
            await pilot.pause(0.1)

            assert app.selected_window_id == next_window["id"]
            assert app._panel_index == 0
            assert getattr(app.focused, "id", None) == "window-" + next_window["id"]
            assert app.selected_id == second.session_id
            assert app.client is not None and not app.client.lease
            assert old_client.closed and not old_client.lease
            assert registry.create_count == 0

    asyncio.run(scenario())


def test_mini_live_observes_each_card_without_lease_input_or_resize(tmp_path):
    async def scenario():
        first, second = _identity(1), _identity(2)
        registry = FakeRegistry([first, second])
        workspace_store = JTTWorkspaceStore(tmp_path / "jtt")
        workspace_store.set_terminal_ids(
            workspace_store.DEFAULT_ID, [first.session_id, second.session_id]
        )
        layout_store = JTTLayoutStore(workspace_store.root)
        app = WindowsJTTApp(
            registry, workspace_store=workspace_store, layout_store=layout_store,
        )
        async with app.run_test(size=(160, 44)) as pilot:
            visible_targets = app._visible_mini_targets()
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline and second.session_id not in app._mini_cache:
                await pilot.pause(0.1)
            entry = app._mini_cache.get(second.session_id)
            assert entry is not None, (
                "non-selected terminal did not receive a Mini LIVE sample; "
                f"visible={[item.session_id for item in visible_targets]}, "
                f"clients={[item.identity.session_id for item in registry.clients]}, "
                f"sampler_busy={app._mini_sampler.busy if app._mini_sampler else None}, "
                f"mini_last={app._mini_last_submit:.3f}, now={time.monotonic():.3f}, "
                f"busy={app._busy}, "
                f"thread_alive={app._mini_sampler._thread.is_alive()}, "
                f"stopping={app._mini_sampler._stopping}, "
                f"requests={app._mini_sampler._requests.qsize()}, "
                f"results={app._mini_sampler._results.qsize()}, "
                f"window_sessions={list(app.sessions)}"
            )
            assert entry[0] == _mini_identity_key(second)
            await pilot.pause(0.05)
            assert "SESSION_0002" in app.query_one(
                "#terminal-" + second.session_id, Button
            ).label.plain
            observers = [client for client in registry.clients
                         if client.identity.session_id == second.session_id]
            assert observers
            assert all(not client.lease and client.acquire_calls == 0 for client in observers)
            assert all(not client.sent and not client.resizes for client in observers)
            assert registry.create_count == 0

            changed = TerminalIdentity(**{**second.__dict__, "generation": "f" * 32})
            registry.sessions[1] = changed
            await app._refresh_sessions()
            assert second.session_id not in app._mini_cache
            assert "SESSION_0002" not in app.query_one(
                "#terminal-" + second.session_id, Button
            ).label.plain

    asyncio.run(scenario())


def test_concurrent_session_and_mini_refreshes_serialize_card_mounts(tmp_path):
    async def scenario():
        first, second = _identity(1), _identity(2)
        registry = FakeRegistry([first, second])
        workspace_store = JTTWorkspaceStore(tmp_path / "jtt")
        workspace_store.set_terminal_ids(
            workspace_store.DEFAULT_ID, [first.session_id, second.session_id]
        )
        app = WindowsJTTApp(
            registry, workspace_store=workspace_store,
            layout_store=JTTLayoutStore(workspace_store.root),
        )
        async with app.run_test(size=(120, 40)) as pilot:
            container = app.query_one("#terminal-cards")
            original_remove = container.remove_children
            first_remove_entered = asyncio.Event()
            release_first_remove = asyncio.Event()
            active_removals = 0
            maximum_active_removals = 0

            async def gated_remove_children(*args, **kwargs):
                nonlocal active_removals, maximum_active_removals
                active_removals += 1
                maximum_active_removals = max(
                    maximum_active_removals, active_removals,
                )
                first_remove_entered.set()
                try:
                    await release_first_remove.wait()
                    return await original_remove(*args, **kwargs)
                finally:
                    active_removals -= 1

            container.remove_children = gated_remove_children
            app._card_signature = None
            first_render = asyncio.create_task(app._render_cards())
            await first_remove_entered.wait()
            app.sessions.pop(second.session_id)
            second_render = asyncio.create_task(app._render_cards())
            await asyncio.sleep(0)  # yield one event-loop turn, without timing assumptions
            assert maximum_active_removals == 1
            release_first_remove.set()
            await asyncio.gather(first_render, second_render)
            buttons = app.query(".terminal-card")
            assert {button.id for button in buttons} == {
                "terminal-" + first.session_id,
            }
            assert all(button.is_attached for button in buttons)
            assert maximum_active_removals == 1
            await pilot.pause(0)

    asyncio.run(scenario())


def test_windows_ui_projects_legacy_sessions_without_startup_migration(tmp_path):
    async def scenario():
        identity = _identity(1)
        registry = FakeRegistry([identity])
        workspace_store = JTTWorkspaceStore(tmp_path / "jtt")
        layout_store = JTTLayoutStore(workspace_store.root)
        app = WindowsJTTApp(
            registry, workspace_store=workspace_store, layout_store=layout_store,
        )
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause(0.15)
            assert app.selected_id == identity.session_id
            assert set(app.sessions) == {identity.session_id}
            assert registry.create_count == 0
            assert not workspace_store.path.exists()
            assert not layout_store.path.exists()

    asyncio.run(scenario())


@pytest.mark.parametrize("size", [(80, 24), (160, 44)])
def test_ui_logical_window_creation_is_blank_and_does_not_start_a_terminal(tmp_path, size):
    async def scenario():
        existing = _identity(1)
        registry = FakeRegistry([existing])
        workspace_store = JTTWorkspaceStore(tmp_path / "jtt")
        layout_store = JTTLayoutStore(workspace_store.root)
        saved = workspace_store.create_workspace("Saved workspace")
        workspace_store.set_terminal_ids(saved["id"], ["saved-session"])
        saved_window = layout_store.create_window(
            saved["id"], "folder-default", "Saved Window",
        )
        layout_store.move_terminal(
            saved["id"], "saved-session", saved_window["id"], ["saved-session"],
        )
        saved_layout_before = layout_store.layout(saved["id"], ["saved-session"])
        app = WindowsJTTApp(
            registry, workspace_store=workspace_store, layout_store=layout_store,
        )
        async with app.run_test(size=size) as pilot:
            await pilot.pause(0.15)
            await app.enter_input_mode()
            prior_client = app.client
            assert prior_client is not None and prior_client.lease
            await pilot.click("#new-window")
            assert not app.input_mode and not prior_client.lease
            assert app.screen.query_one("#window-name").value == "Window 2"
            await pilot.press("enter")
            await pilot.pause(0.15)

            assert prior_client.closed
            assert registry.create_count == 0
            assert registry.sessions == [existing]
            assert len(app.windows) == 2
            created = next(item for item in app.windows
                           if item["id"] == app.selected_window_id)
            assert created["name"] == "Window 2"
            assert created["terminal_ids"] == []
            assert app.sessions == {}
            assert app.selected_id is None
            assert app.client is None
            persisted = layout_store.layout(workspace_store.DEFAULT_ID, [existing.session_id])
            assert any(item["id"] == created["id"] and not item["terminal_ids"]
                       for item in persisted["windows"])
            assert layout_store.layout(saved["id"], ["saved-session"]) == saved_layout_before

    asyncio.run(scenario())


@pytest.mark.parametrize("size", [(80, 24), (160, 44)])
def test_saved_workspace_open_reconnects_exact_live_reference_without_lease_or_write(
    tmp_path, size,
):
    async def scenario():
        current = _identity(1)
        saved_identity = _identity(2)
        registry = FakeRegistry([current, saved_identity])
        workspace_store = JTTWorkspaceStore(tmp_path / "jtt")
        layout_store = JTTLayoutStore(workspace_store.root)
        workspace_store.set_terminal_ids(workspace_store.DEFAULT_ID, [current.session_id])
        saved = workspace_store.create_workspace("Review session")
        workspace_store.set_terminal_ids(saved["id"], [saved_identity.session_id])
        saved_window = layout_store.create_window(
            saved["id"], "folder-default", "Review",
        )
        layout_store.move_terminal(
            saved["id"], saved_identity.session_id, saved_window["id"],
            [saved_identity.session_id],
        )
        workspace_bytes = workspace_store.path.read_bytes()
        layout_bytes = layout_store.path.read_bytes()
        app = WindowsJTTApp(
            registry, workspace_store=workspace_store, layout_store=layout_store,
        )
        async with app.run_test(size=size) as pilot:
            await _wait_for_ui_state(
                pilot, lambda: app.selected_id == current.session_id,
                "initial running workspace selection",
            )
            assert app.selected_id == current.session_id
            await app.enter_input_mode()
            previous_client = app.client
            assert previous_client is not None and previous_client.lease

            await pilot.click("#workspace-picker")
            assert isinstance(app.screen, ModalScreen)
            assert not previous_client.lease
            assert app.screen.query_one("#saved-workspace-preview", Static)
            await pilot.click("#saved-workspace-choice-1")
            await pilot.click("#open-saved-workspace")
            await _wait_for_ui_state(
                pilot,
                lambda: (app.workspace_id == saved["id"]
                         and app.selected_id == saved_identity.session_id
                         and app.client is not None and not app.client.lease
                         and app._right_observation is not None),
                "saved workspace read-only reconnect",
            )

            assert app.workspace_id == saved["id"]
            assert app.workspace_name == saved["name"]
            assert app.selected_window_id == saved_window["id"]
            assert app.selected_id == saved_identity.session_id
            assert app.selected_identity == saved_identity
            assert app.client is not None
            assert app.client.identity == saved_identity
            assert not app.input_mode and not app.client.lease
            assert app.client.acquire_calls == 0
            assert previous_client.closed and not previous_client.lease
            assert registry.create_count == 0
            assert workspace_store.path.read_bytes() == workspace_bytes
            assert layout_store.path.read_bytes() == layout_bytes

    asyncio.run(scenario())


def test_saved_workspace_missing_and_exited_refs_clear_old_live_without_fake_restore(tmp_path):
    async def scenario():
        current = _identity(1)
        exited = _identity(2, state="exited")
        registry = FakeRegistry([current, exited])
        workspace_store = JTTWorkspaceStore(tmp_path / "jtt")
        layout_store = JTTLayoutStore(workspace_store.root)
        workspace_store.set_terminal_ids(workspace_store.DEFAULT_ID, [current.session_id])
        saved = workspace_store.create_workspace("Old task")
        workspace_store.set_terminal_ids(saved["id"], [exited.session_id, "missing-session"])
        workspace_bytes = workspace_store.path.read_bytes()
        layout_bytes = layout_store.path.read_bytes() if layout_store.path.exists() else None
        app = WindowsJTTApp(
            registry, workspace_store=workspace_store, layout_store=layout_store,
        )
        async with app.run_test(size=(120, 40)) as pilot:
            await _wait_for_ui_state(
                pilot, lambda: app._right_observation is not None,
                "initial RIGHT observation before stale workspace switch",
            )
            assert app._right_observation is not None
            assert "SESSION_0001" in str(app.query_one("#live", Static).render())
            await pilot.click("#workspace-picker")
            await pilot.click("#saved-workspace-choice-1")
            await pilot.click("#open-saved-workspace")
            await _wait_for_ui_state(
                pilot,
                lambda: (app.workspace_id == saved["id"]
                         and app._missing_terminal_refs == {"missing-session"}
                         and app.sessions == {exited.session_id: exited}),
                "stale and exited saved references projected without restore",
            )

            assert app.workspace_id == saved["id"]
            assert app.selected_id is None
            assert app.client is None
            assert app._right_observation is None
            assert current.session_id not in app._mini_cache
            assert app.sessions == {exited.session_id: exited}
            assert app._missing_terminal_refs == {"missing-session"}
            live_text = str(app.query_one("#live", Static).render())
            assert "SESSION_0001" not in live_text
            card_text = str(app.query_one("#terminal-" + exited.session_id, Button).label)
            assert "셸 종료" in card_text
            assert registry.create_count == 0
            assert workspace_store.path.read_bytes() == workspace_bytes
            if layout_bytes is None:
                assert not layout_store.path.exists()
            else:
                assert layout_store.path.read_bytes() == layout_bytes

    asyncio.run(scenario())


def test_saved_workspace_selection_rejects_changed_snapshot(tmp_path):
    async def scenario():
        identity = _identity(1)
        registry = FakeRegistry([identity])
        workspace_store = JTTWorkspaceStore(tmp_path / "jtt")
        layout_store = JTTLayoutStore(workspace_store.root)
        saved = workspace_store.create_workspace("Snapshot target")
        app = WindowsJTTApp(
            registry, workspace_store=workspace_store, layout_store=layout_store,
        )
        async with app.run_test(size=(120, 40)) as pilot:
            await _wait_for_ui_state(
                pilot, lambda: app.selected_id == identity.session_id,
                "initial running workspace selection before snapshot race",
            )
            await pilot.click("#workspace-picker")
            workspace_store.set_terminal_ids(saved["id"], [identity.session_id])
            await pilot.click("#saved-workspace-choice-1")
            await pilot.click("#open-saved-workspace")
            await _wait_for_ui_state(
                pilot, lambda: "changed" in app._notice,
                "changed saved workspace snapshot rejection",
            )

            assert app.workspace_id == workspace_store.DEFAULT_ID
            assert app.selected_id == identity.session_id
            assert registry.create_count == 0
            assert "changed" in app._notice

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
            calls_before_modal_tick = registry.list_calls
            # Keep the modal open across the one-second session refresh interval.
            # Polling must safely defer rather than query the hidden base screen.
            await pilot.pause(1.2)
            assert registry.list_calls == calls_before_modal_tick
            assert app.screen.query_one("#cancel-stop", Button).has_focus
            await app._refresh_live()
            await pilot.click("#cancel-stop")
            assert not isinstance(app.screen, ModalScreen)
            # The interval remains scheduled while the modal is open. Once
            # the modal closes, a later tick must refresh both the cards and
            # the selected LIVE view without an explicit manual refresh.
            await pilot.pause(1.1)
            assert registry.list_calls > calls_before_modal_tick
            assert str(app.query_one("#terminal-summary", Static).render()) == "터미널 1개"
            assert "SESSION_0001" in str(app.query_one("#live", Static).render())
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
                await pilot.pause(1.2)
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
