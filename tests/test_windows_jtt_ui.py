"""Textual JTT UI tests; native ConPTY coverage runs on Windows only."""

from __future__ import annotations

import asyncio
from contextvars import ContextVar
import hashlib
import importlib
import io
import sys
import threading
import time
from types import SimpleNamespace

import pytest
from textual.css.query import NoMatches
from textual.events import Paste
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Static

import ju0_terminal_tower  # noqa: F401 - activates the guarded product namespace
from ju0_terminal_tower.state.jtt_layouts import JTTLayoutStore
from ju0_terminal_tower.state.jtt_work_kits import JTTWorkKitStore
from ju0_terminal_tower.state.jtt_workspaces import JTTWorkspaceStore
from ju0_terminal_tower.terminal.manager import (
    TerminalCell,
    TerminalIdentity,
    TerminalObservation,
    TerminalScreen,
)
from ju0_terminal_tower.ui.windows_jtt import (
    CreateWindowScreen,
    SavedWorkspaceScreen,
    WindowsJTTApp,
    _same_identity,
)
from ju0_terminal_tower.ui.windows_mini_live import identity_key as _mini_identity_key


def _screen(text: str = "JTT_TEST_READY") -> TerminalScreen:
    cells = tuple(TerminalCell(char, "default", "default", False, False, False,
                               False, False, False) for char in text)
    return TerminalScreen((cells,), min(len(cells), 3), 0, True)


def _screen_lines(*lines: str) -> TerminalScreen:
    rows = tuple(tuple(TerminalCell(
        char, "default", "default", False, False, False, False, False, False
    ) for char in line) for line in lines)
    return TerminalScreen(rows, 0, max(0, len(rows) - 1), True)


def test_pty_input_ack_requires_the_complete_utf8_byte_count_without_retry():
    from tmux_agent_tower.terminal._windows_worker import (
        _InputDeliveryUnconfirmed,
        _write_terminal_input,
    )

    class FakePty:
        def __init__(self, result):
            self.result = result
            self.calls = []

        def write(self, text):
            self.calls.append(text)
            if isinstance(self.result, Exception):
                raise self.result
            return self.result

    text = "Write-Output '한글'"
    payload_bytes = len(text.encode("utf-8"))
    complete = FakePty(payload_bytes)
    assert _write_terminal_input(complete, text, payload_bytes) == payload_bytes
    assert complete.calls == [text]

    for incomplete_result in (0, payload_bytes - 1, None):
        partial = FakePty(incomplete_result)
        with pytest.raises(_InputDeliveryUnconfirmed):
            _write_terminal_input(partial, text, payload_bytes)
        assert partial.calls == [text]

    failed = FakePty(OSError("injected PTY write failure"))
    with pytest.raises(_InputDeliveryUnconfirmed):
        _write_terminal_input(failed, text, payload_bytes)
    assert failed.calls == [text]


def test_native_conpty_run_path_finalization_preserves_diagnostic_and_cleanup_evidence():
    """Exercise the diagnostic result contract without starting processes."""
    import ast
    import importlib.util
    from pathlib import Path

    backend_test_path = Path(__file__).with_name("test_windows_terminal_backend.py")
    spec = importlib.util.spec_from_file_location(
        "jtt_windows_backend_test_helpers", backend_test_path,
    )
    assert spec is not None and spec.loader is not None
    backend_test_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(backend_test_module)
    finalize = backend_test_module._finalize_conpty_path_result
    allows_followup = backend_test_module._conpty_path_allows_followup
    input_chunks = backend_test_module._conpty_input_chunks
    error_categories = backend_test_module._conpty_powershell_error_categories
    prompt_after_marker = backend_test_module._conpty_prompt_returned_after_marker
    parser_evidence = backend_test_module._owned_job_parser_preflight_evidence
    start_process_evidence = backend_test_module._owned_job_start_process_evidence
    worker_skip_status = backend_test_module._conpty_worker_scenario_skip_status

    assert input_chunks("Write-Output 'OK'", "single_write") == [
        "Write-Output 'OK'\r"
    ]
    assert input_chunks(
        "Write-Output 'OK'", "single_write", terminator="\r\n"
    ) == ["Write-Output 'OK'\r\n"]
    assert input_chunks("Write-Output '한글'", "per_key") == [
        *"Write-Output '한글'", "\r"
    ]
    assert input_chunks(
        "Write-Output '한글'", "per_key", terminator="\r\n"
    ) == [*"Write-Output '한글'", "\r\n"]
    assert sum(len(part.encode("utf-8")) for part in input_chunks(
        "Write-Output '한글'", "per_key"
    )) == len("Write-Output '한글'\r".encode("utf-8"))
    with pytest.raises(ValueError):
        input_chunks("Write-Output 'OK'", "unknown")

    echoed_throw = (
        "PS C:\\test> if ($x) { throw 'JTT diagnostic state mismatch' }\r\n"
    )
    assert error_categories(echoed_throw) == []
    assert error_categories("Set-Location : Cannot find path 'x'\r\n") == [
        "Set-Location"
    ]
    assert not prompt_after_marker(
        "JTT_DONE_END\r\nPS C:\\test> Write-Output 'next'\r\n",
        "JTT_DONE_END",
    )
    assert prompt_after_marker(
        "JTT_DONE_END\r\nPS C:\\test> \r\n", "JTT_DONE_END"
    )

    valid_parser = parser_evidence(
        "launcher.ps1|VALID;child.ps1|VALID",
        "JTT_PARSE_OK_END",
        script_count=2,
        success_marker="JTT_PARSE_OK_END",
        write_error_marker="JTT_PARSE_WRITE_ERROR",
    )
    assert valid_parser["status"] == "ALL_SCRIPTS_PARSE"
    invalid_parser = parser_evidence(
        "launcher.ps1|INVALID|1|UnexpectedToken;child.ps1|VALID",
        "JTT_PARSE_OK_END",
        script_count=2,
        success_marker="JTT_PARSE_OK_END",
        write_error_marker="JTT_PARSE_WRITE_ERROR",
    )
    assert invalid_parser["status"] == "PARSE_ERRORS_RECORDED"
    parser_write_failure = parser_evidence(
        "ABSENT",
        "JTT_PARSE_WRITE_ERROR|UnauthorizedAccessException|WriteFailure"
        "\r\nJTT_PARSE_WRITE_ERROR_END",
        script_count=2,
        success_marker="JTT_PARSE_OK_END",
        write_error_marker="JTT_PARSE_WRITE_ERROR",
    )
    assert parser_write_failure["status"] == "STAGE_WRITE_FAILED"
    assert parser_write_failure["stage_write_exception_category"] == (
        "UnauthorizedAccessException"
    )
    not_reached = parser_evidence(
        "ABSENT", b"", script_count=2, success_marker="JTT_PARSE_OK_END",
        write_error_marker="JTT_PARSE_WRITE_ERROR",
    )
    assert not_reached["status"] == "MARKER_NOT_OBSERVED"

    start_process_failure = start_process_evidence(
        "ABSENT",
        "JTT_OWNED_STAGE_WRITE_ERROR|start_process|OSError|StageWriteFailed\r\n"
        "JTT_OWNED_START_PROCESS_ERROR|Win32Exception|StartProcessFailed\r\n",
    )
    assert start_process_failure["call_outcome"] == "EXCEPTION"
    assert start_process_failure["exception_category"] == "Win32Exception"
    assert start_process_failure["stage_file_write_outcome"] == "FAILED"
    assert start_process_failure["stage_write_exception_category"] == "OSError"
    child_created_write_fallback = start_process_evidence(
        "child_created_but_stage_write_error|321|OSError|StageWriteFailed",
        "JTT_OWNED_CHILD_PROCESS_CREATED|321\r\n"
        "JTT_OWNED_STAGE_WRITE_ERROR|child_created|OSError|StageWriteFailed\r\n",
    )
    assert child_created_write_fallback["call_outcome"] == "CHILD_CREATED"
    assert child_created_write_fallback["stage_file_write_outcome"] == (
        "INITIAL_WRITE_FAILED_FALLBACK_RECORDED"
    )
    child_created_write_failure = start_process_evidence(
        "ABSENT",
        "JTT_OWNED_CHILD_PROCESS_CREATED|321\r\n"
        "JTT_OWNED_STAGE_WRITE_ERROR|child_created|OSError|StageWriteFailed\r\n",
    )
    assert child_created_write_failure["call_outcome"] == "CHILD_CREATED"
    assert child_created_write_failure["stage_file_write_outcome"] == "FAILED"
    assert child_created_write_failure["stage_write_error_stage"] == "child_created"
    assert worker_skip_status(
        {"cleanup_confirmed": True}, {"cleanup_confirmed": False}
    ) == "NOT_RUN_PREVIOUS_WORKER_CLEANUP_UNCONFIRMED"
    assert worker_skip_status(
        {"cleanup_confirmed": False}, {"cleanup_confirmed": False}
    ) == "NOT_RUN_DIRECT_CLEANUP_UNCONFIRMED"

    backend_tree = ast.parse(backend_test_path.read_text(encoding="utf-8"))
    comparison_test = next(
        node for node in backend_tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "test_native_conpty_input_execution_path_comparison"
    )
    run_path = next(
        node for node in comparison_test.body
        if isinstance(node, ast.FunctionDef) and node.name == "run_path"
    )
    cleanup_try = next(
        node for node in run_path.body
        if isinstance(node, ast.Try) and node.finalbody
    )
    final_return = run_path.body[-1]
    assert isinstance(final_return, ast.Return)
    assert isinstance(final_return.value, ast.Call)
    assert isinstance(final_return.value.func, ast.Name)
    assert final_return.value.func.id == "_finalize_conpty_path_result"
    assert run_path.body.index(cleanup_try) < run_path.body.index(final_return)
    assert not any(
        isinstance(node, ast.Return)
        and isinstance(node.value, ast.Name)
        and node.value.id == "result"
        for node in ast.walk(cleanup_try)
    ), "run_path must not return before post-finally result finalization"

    normal = finalize({
        "status": "COMPLETE",
        "cleanup": "REGISTRY_STOP_COMPLETED",
        "cleanup_confirmed": True,
        "retained_handle_signaled_after_cleanup": True,
    })
    assert normal["finalization"] == {
        "status": "COMPLETE",
        "diagnostic_error_stage": None,
        "diagnostic_error_category": None,
        "cleanup": "REGISTRY_STOP_COMPLETED",
        "cleanup_confirmed": True,
        "cleanup_error_category": None,
        "cleanup_handle_close_error_category": None,
        "client_close_error_category": None,
        "retained_handle_signaled_after_cleanup": True,
    }
    assert allows_followup(normal)

    diagnostic_error = finalize({
        "status": "DIAGNOSTIC_TOOL_ERROR",
        "diagnostic_error_stage": "direct_pyte_feed",
        "diagnostic_error_category": "TypeError",
        "cleanup": "TERMINATED_OR_EXITED_ON_RETAINED_HANDLE",
        "cleanup_confirmed": True,
        "cleanup_error_category": None,
        "cleanup_handle_close_error_category": None,
        "client_close_error_category": None,
    })
    assert diagnostic_error["finalization"]["status"] == "DIAGNOSTIC_TOOL_ERROR"
    assert diagnostic_error["finalization"]["diagnostic_error_stage"] == "direct_pyte_feed"
    assert diagnostic_error["finalization"]["diagnostic_error_category"] == "TypeError"
    assert diagnostic_error["finalization"]["cleanup_confirmed"] is True
    assert allows_followup(diagnostic_error)

    cleanup_failure = finalize({
        "status": "EXECUTION_UNPROVEN",
        "cleanup": "STOP_FAILED_EVIDENCE_RETAINED",
        "cleanup_confirmed": False,
        "cleanup_error_category": "TerminalError",
        "cleanup_handle_close_error_category": "OSError",
        "client_close_error_category": None,
        "retained_handle_signaled_after_cleanup": False,
    })
    assert cleanup_failure["finalization"]["cleanup"] == "STOP_FAILED_EVIDENCE_RETAINED"
    assert cleanup_failure["finalization"]["cleanup_confirmed"] is False
    assert cleanup_failure["finalization"]["cleanup_error_category"] == "TerminalError"
    assert cleanup_failure["finalization"]["cleanup_handle_close_error_category"] == "OSError"
    assert cleanup_failure["finalization"]["retained_handle_signaled_after_cleanup"] is False
    assert not allows_followup(cleanup_failure)
    close_failure_after_exit = finalize({
        "status": "COMPLETE",
        "cleanup": "TERMINATED_OR_EXITED_ON_RETAINED_HANDLE",
        "cleanup_confirmed": True,
        "cleanup_handle_close_error_category": "OSError",
    })
    assert not allows_followup(close_failure_after_exit)


def _plain_screen_text(screen: TerminalScreen) -> str:
    return "\n".join("".join(cell.text for cell in row) for row in screen.rows)


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


async def _wait_for_focused_widget(pilot, app, selector: str, reason: str):
    def focused():
        try:
            widget = app.query_one(selector)
        except NoMatches:
            return False
        return widget.is_attached and app.focused is widget

    await _wait_for_ui_state(pilot, focused, reason)
    return app.query_one(selector)


def _focus_description(app) -> str:
    focused = app.focused
    return (f"widget={type(focused).__name__} id={getattr(focused, 'id', None)!r} "
            f"screen={type(app.screen).__name__} mounted={app.screen.is_mounted}")


async def _keyboard_focus_target(pilot, app, selector: str, key: str, reason: str):
    """Reach an archive control through one bounded pass of its real focus chain."""
    screen = app.screen
    target = screen.query_one(selector)
    visited = []
    for _ in range(len(screen.focus_chain) + 1):
        focused = app.focused
        visited.append(getattr(focused, "id", None))
        if focused is target and target.is_attached:
            return tuple(visited)
        await pilot.press(key)
        await _wait_for_ui_state(
            pilot,
            lambda: (app.screen is screen and app.focused is not None
                     and app.focused is not focused and app.focused.is_attached
                     and app.focused.screen is screen),
            f"{reason}; previous path={visited!r}; {_focus_description(app)}",
        )
    pytest.fail(f"unreachable keyboard target {selector}: {reason}; "
                f"path={visited!r}; {_focus_description(app)}")


async def _wait_for_modal_input_focus(pilot, app, selector: str, reason: str):
    def focused_and_mounted():
        screen = app.screen
        if type(screen) is not CreateWindowScreen or not screen.is_mounted:
            return False
        try:
            widget = screen.query_one(selector, Input)
        except NoMatches:
            return False
        return widget.is_attached and app.focused is widget

    await _wait_for_ui_state(pilot, focused_and_mounted, reason)
    return app.screen.query_one(selector, Input)


async def _wait_for_widget_in_viewport(
    pilot, app, widget_selector: str, viewport_selector: str, reason: str,
):
    def visible():
        try:
            widget = app.query_one(widget_selector)
            viewport = app.query_one(viewport_selector)
        except NoMatches:
            return False
        region = widget.region.intersection(viewport.content_region)
        return widget.is_attached and region.width > 0 and region.height > 0

    await _wait_for_ui_state(pilot, visible, reason)
    return app.query_one(widget_selector)


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


@pytest.mark.parametrize("size", [(80, 24), (120, 35), (160, 44)])
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


@pytest.mark.parametrize("size", [(80, 24), (120, 35), (160, 44)])
def test_r03_responsive_cards_keep_selection_and_archive_accessible(size):
    """Synthetic R03: layout changes do not create or lease a terminal."""
    async def scenario():
        identities = [_identity(1), _identity(2), _identity(3)]
        registry = FakeRegistry(identities)
        app = WindowsJTTApp(registry)
        async with app.run_test(size=size) as pilot:
            await _wait_for_ui_state(
                pilot, lambda: app.selected_id == identities[0].session_id,
                "initial synthetic terminal selection",
            )
            await pilot.pause(0.05)
            container = app.query_one("#terminal-cards")
            cards_per_row = app._cards_per_row(container.size.width)
            rows = list(container.query(".card-row"))
            assert len(rows) == (len(identities) + cards_per_row - 1) // cards_per_row
            assert len(list(rows[0].query(".terminal-card"))) == cards_per_row
            assert app.query_one("#workspace-picker", Button).label.plain == "보관함"
            assert app.query_one("#window-list").region.height > 0
            assert app.query_one("#live").content_region.width > 0
            await app._move_card(1)
            assert app.selected_identity == identities[1]
            assert app.client.identity.generation == identities[1].generation
            assert not app.client.lease
            assert registry.create_count == 0
            assert all(not client.sent and not client.resizes for client in registry.clients)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "width,scroll_y,expected", [(20, 6, [2]), (40, 6, [3, 4])],
)
def test_r03_mini_live_visible_rows_match_responsive_card_columns(width, scroll_y, expected):
    identities = [_identity(index) for index in range(1, 5)]
    container = SimpleNamespace(size=SimpleNamespace(width=width, height=5), scroll_y=scroll_y)
    synthetic = SimpleNamespace(
        sessions={item.session_id: item for item in identities},
        selected_id=identities[0].session_id,
        size=SimpleNamespace(height=24),
        _mini_cards_container=lambda: container,
        _cards_per_row=WindowsJTTApp._cards_per_row,
    )
    targets = WindowsJTTApp._visible_mini_targets(synthetic)
    assert [item.session_id for item in targets] == [identities[index - 1].session_id
                                                     for index in expected]


@pytest.mark.parametrize("size", [(80, 24), (120, 35), (160, 44)])
def test_r03_visible_click_regions_and_scrolled_card_target_identity(tmp_path, size):
    """Synthetic viewport and target contract; no PTY or native rendering claim."""
    async def scenario():
        identities = [_identity(index) for index in range(1, 13)]
        registry = FakeRegistry(identities)
        workspace = JTTWorkspaceStore(tmp_path / "synthetic-jtt")
        layout = JTTLayoutStore(workspace.root)
        app = WindowsJTTApp(registry, workspace_store=workspace, layout_store=layout)
        app.set_interval = lambda *_args, **_kwargs: SimpleNamespace(stop=lambda: None)
        async with app.run_test(size=size) as pilot:
            await _wait_for_ui_state(pilot, lambda: len(app.sessions) == len(identities),
                                     "synthetic sessions")
            await pilot.pause(0.05)
            for selector in ("#left", "#center", "#right"):
                region = app.query_one(selector).region
                assert region.width > 0 and region.height > 0
                assert region.intersection(app.screen.region) == region
            left = app.query_one("#left")
            for selector in ("#new-window", "#workspace-picker", "#window-window-default"):
                region = app.query_one(selector, Button).region
                assert region.width > 0 and region.height > 0
                assert region.intersection(left.region) == region

            await pilot.click("#workspace-picker")
            assert isinstance(app.screen, ModalScreen)
            await pilot.press("escape")
            await _wait_for_ui_state(pilot, lambda: not isinstance(app.screen, ModalScreen),
                                     "archive cancellation")
            target = list(app.sessions.values())[-1]
            await app._select_session(target)
            card_selector = "#terminal-" + target.session_id
            card = await _wait_for_focused_widget(
                pilot, app, card_selector, "selected card focus established",
            )
            assert isinstance(card, Button)
            assert app.selected_identity is not None
            assert _mini_identity_key(app.selected_identity) == _mini_identity_key(target)
            await _wait_for_widget_in_viewport(
                pilot, app, card_selector, "#terminal-cards",
                "selected card intersects CENTER viewport",
            )
            await pilot.click("#terminal-" + target.session_id)
            await _wait_for_ui_state(pilot, lambda: (app.selected_identity == target
                                     and app.client is not None and not app._busy),
                                     "clicked card selection")
            await app._refresh_live()
            assert app.client.identity.session_id == target.session_id
            assert app.client.identity.generation == target.generation
            assert app._right_observation is not None
            assert app._right_observation[0] == _mini_identity_key(target)
            assert not app.client.lease and not app.input_mode
            assert registry.create_count == 0 and registry.stop_calls == []
            assert all(not client.sent and not client.resizes for client in registry.clients)

    asyncio.run(scenario())


@pytest.mark.parametrize("size", [(80, 24), (120, 35), (160, 44)])
def test_r03_nonselected_stale_mini_screen_expires_without_input(size):
    async def scenario():
        first, second = _identity(1), _identity(2)
        registry = FakeRegistry([first, second])
        app = WindowsJTTApp(registry)
        app.set_interval = lambda *_args, **_kwargs: SimpleNamespace(stop=lambda: None)
        async with app.run_test(size=size) as pilot:
            await _wait_for_ui_state(pilot, lambda: app.selected_id == first.session_id,
                                     "synthetic selected terminal")
            await pilot.pause(0.05)
            app._mini_cache[second.session_id] = (
                _mini_identity_key(second), 1, time.monotonic() - 3.0,
                _screen("EXPIRED_NONSELECTED"),
            )
            await app._render_cards()
            label = app.query_one("#terminal-" + second.session_id, Button).label.plain
            assert "EXPIRED_NONSELECTED" not in label
            assert "미니 화면 대기 중" in label
            assert all(not client.lease and not client.sent and not client.resizes
                       for client in registry.clients)
            assert registry.create_count == 0 and registry.stop_calls == []

    asyncio.run(scenario())


@pytest.mark.parametrize("size", [(80, 24), (120, 35), (160, 44)])
def test_r03_window_updates_keep_focused_button_without_replacement(size):
    async def scenario():
        registry = FakeRegistry()
        app = WindowsJTTApp(registry)
        app.set_interval = lambda *_args, **_kwargs: SimpleNamespace(stop=lambda: None)
        async with app.run_test(size=size) as pilot:
            await _wait_for_ui_state(pilot, lambda: bool(app.query(".window-item")),
                                     "initial Window button")
            app._focus_panel(0)
            focused = await _wait_for_focused_widget(
                pilot, app, "#window-window-default", "selected Window focus established",
            )
            app.windows[0]["name"] = "Renamed Window"
            app.windows[0]["terminal_ids"] = ["synthetic-ref"]
            app.windows.append({"id": "synthetic-extra", "name": "Review",
                                "folder_id": "folder-default", "terminal_ids": []})
            await app._render_windows()
            assert app._panel_index == 0 and app.focused is focused
            assert app.query_one("#window-window-default", Button) is focused
            assert "Renamed Window" in focused.label.plain
            assert "1" in focused.label.plain
            assert app.query_one("#window-synthetic-extra", Button)
            assert registry.create_count == 0 and registry.stop_calls == []

    asyncio.run(scenario())


@pytest.mark.parametrize("size", [(80, 24), (120, 35), (160, 44)])
def test_r03_mini_live_sequence_updates_keep_selected_card_focus(size):
    async def scenario():
        identity = _identity(1)
        registry = FakeRegistry([identity])
        app = WindowsJTTApp(registry)
        app.set_interval = lambda *_args, **_kwargs: SimpleNamespace(stop=lambda: None)
        async with app.run_test(size=size) as pilot:
            await _wait_for_ui_state(pilot, lambda: app.selected_id == identity.session_id,
                                     "synthetic card selection")
            await _wait_for_ui_state(
                pilot, lambda: bool(app.query(".terminal-card")),
                "initial card mounted before Mini LIVE update",
            )
            await app._render_cards()
            app._focus_panel(1)
            focused = await _wait_for_focused_widget(
                pilot, app, "#terminal-" + identity.session_id,
                "selected card focus established before Mini LIVE refresh",
            )
            for sequence, marker in ((1, "OBSERVED_ONE"), (2, "OBSERVED_TWO")):
                app._mini_cache[identity.session_id] = (
                    _mini_identity_key(identity), sequence, time.monotonic(), _screen(marker),
                )
                await app._render_cards()
                assert app._panel_index == 1 and app.focused is focused
                assert app.query_one("#terminal-" + identity.session_id, Button) is focused
                assert marker in focused.label.plain
            assert app.client is not None and not app.client.lease
            assert all(not client.sent and not client.resizes for client in registry.clients)

    asyncio.run(scenario())


def test_r03_async_window_refresh_never_steals_focus_from_right():
    async def scenario():
        app = WindowsJTTApp(FakeRegistry())
        app.set_interval = lambda *_args, **_kwargs: SimpleNamespace(stop=lambda: None)
        async with app.run_test(size=(120, 35)) as pilot:
            await _wait_for_ui_state(pilot, lambda: bool(app.query(".window-item")),
                                     "initial Window button")
            app._focus_panel(0)
            entered, resume = asyncio.Event(), asyncio.Event()
            mount = app._mount_if_active

            async def gated_mount(parent, *children):
                if any(child.id == "window-synthetic-extra" for child in children):
                    entered.set()
                    await resume.wait()
                return await mount(parent, *children)

            app._mount_if_active = gated_mount
            app.windows.append({"id": "synthetic-extra", "name": "Review",
                                "folder_id": "folder-default", "terminal_ids": []})
            refresh = asyncio.create_task(app._render_windows())
            await asyncio.wait_for(entered.wait(), timeout=1.0)
            app._focus_panel(2)
            resume.set()
            await asyncio.wait_for(refresh, timeout=1.0)
            assert app._panel_index == 2 and app.focused.id == "live"

    asyncio.run(scenario())


def test_r03_async_window_refresh_cannot_take_modal_focus():
    async def scenario():
        app = WindowsJTTApp(FakeRegistry())
        app.set_interval = lambda *_args, **_kwargs: SimpleNamespace(stop=lambda: None)
        async with app.run_test(size=(120, 35)) as pilot:
            await _wait_for_ui_state(pilot, lambda: bool(app.query(".window-item")),
                                     "initial Window button")
            entered, resume = asyncio.Event(), asyncio.Event()
            mount = app._mount_if_active

            async def gated_mount(parent, *children):
                if any(child.id == "window-synthetic-extra" for child in children):
                    entered.set()
                    await resume.wait()
                return await mount(parent, *children)

            app._mount_if_active = gated_mount
            app.windows.append({"id": "synthetic-extra", "name": "Review",
                                "folder_id": "folder-default", "terminal_ids": []})
            refresh = asyncio.create_task(app._render_windows())
            await asyncio.wait_for(entered.wait(), timeout=1.0)
            await app._prompt_create_window()
            name_input = await _wait_for_modal_input_focus(
                pilot, app, "#window-name", "Window modal input mounted and focused",
            )
            resume.set()
            await asyncio.wait_for(refresh, timeout=1.0)
            assert isinstance(app.screen, ModalScreen)
            assert app.focused is name_input
            assert app.focused.id == "window-name"

    asyncio.run(scenario())


def test_r03_async_card_rebuild_never_steals_focus_from_right():
    async def scenario():
        identity = _identity(1)
        app = WindowsJTTApp(FakeRegistry([identity]))
        app.set_interval = lambda *_args, **_kwargs: SimpleNamespace(stop=lambda: None)
        async with app.run_test(size=(120, 35)) as pilot:
            await _wait_for_ui_state(pilot, lambda: app.selected_id == identity.session_id,
                                     "initial synthetic card")
            app._focus_panel(1)
            container = app.query_one("#terminal-cards")
            remove = container.remove_children
            entered, resume = asyncio.Event(), asyncio.Event()

            async def gated_remove(*args, **kwargs):
                entered.set()
                await resume.wait()
                return await remove(*args, **kwargs)

            container.remove_children = gated_remove
            app._card_signature = None
            app._card_structure = None
            refresh = asyncio.create_task(app._render_cards())
            await asyncio.wait_for(entered.wait(), timeout=1.0)
            app._focus_panel(2)
            resume.set()
            await asyncio.wait_for(refresh, timeout=1.0)
            assert app._panel_index == 2 and app.focused.id == "live"

    asyncio.run(scenario())


@pytest.mark.parametrize("size", [(80, 24), (120, 35), (160, 44)])
def test_r03_card_rebuild_restores_actual_focus_for_same_session_identity(size):
    async def scenario():
        identity = _identity(1)
        registry = FakeRegistry([identity])
        app = WindowsJTTApp(registry)
        app.set_interval = lambda *_args, **_kwargs: SimpleNamespace(stop=lambda: None)
        async with app.run_test(size=size) as pilot:
            await _wait_for_ui_state(
                pilot, lambda: app.selected_id == identity.session_id,
                "selected synthetic terminal before card rebuild",
            )
            await app._render_cards()
            app._focus_panel(1)
            focused_before = await _wait_for_focused_widget(
                pilot, app, "#terminal-" + identity.session_id,
                "selected card focus established before rebuild",
            )
            assert isinstance(focused_before, Button)
            assert focused_before.id == "terminal-" + identity.session_id
            focused_identity = _mini_identity_key(app.sessions[identity.session_id])

            # Force the structural path; output-only changes are covered by
            # the in-place Mini LIVE refresh test above.
            app._card_structure = None
            app._card_signature = None
            await app._render_cards()

            current_card = await _wait_for_focused_widget(
                pilot, app, "#terminal-" + identity.session_id,
                "same-identity card focus restored after rebuild",
            )
            assert current_card.is_attached
            assert app.focused is current_card
            assert _mini_identity_key(app.sessions[identity.session_id]) == focused_identity
            assert app.selected_identity.generation == identity.generation
            assert app.client is not None and not app.client.lease
            assert registry.create_count == 0 and registry.stop_calls == []

    asyncio.run(scenario())


def test_r03_card_rebuild_restores_nonselected_focused_card_identity():
    async def scenario():
        selected, focused_identity = _identity(1), _identity(2)
        registry = FakeRegistry([selected, focused_identity])
        app = WindowsJTTApp(registry)
        app.set_interval = lambda *_args, **_kwargs: SimpleNamespace(stop=lambda: None)
        async with app.run_test(size=(120, 35)) as pilot:
            await _wait_for_ui_state(
                pilot, lambda: app.selected_id == selected.session_id,
                "first terminal selected before focusing another card",
            )
            await app._render_cards()
            first_card = app.query_one("#terminal-" + selected.session_id, Button)
            focused_card = app.query_one(
                "#terminal-" + focused_identity.session_id, Button,
            )
            focused_card.focus()
            focused_card = await _wait_for_focused_widget(
                pilot, app, "#terminal-" + focused_identity.session_id,
                "nonselected card focus established",
            )
            assert app.selected_id == selected.session_id
            assert app.selected_identity.generation == selected.generation
            preserved_focus_key = _mini_identity_key(
                app.sessions[focused_identity.session_id]
            )

            app._card_structure = None
            app._card_signature = None
            await app._render_cards()

            replacement_focused_card = app.query_one(
                "#terminal-" + focused_identity.session_id, Button,
            )
            assert replacement_focused_card is not focused_card
            assert replacement_focused_card.is_attached
            replacement_focused_card = await _wait_for_focused_widget(
                pilot, app, "#terminal-" + focused_identity.session_id,
                "same-identity card focus restored after rebuild",
            )
            assert app.focused is replacement_focused_card
            assert _mini_identity_key(
                app.sessions[focused_identity.session_id]
            ) == preserved_focus_key
            assert app.selected_id == selected.session_id
            assert app.selected_identity.generation == selected.generation
            replacement_selected_card = app.query_one(
                "#terminal-" + selected.session_id, Button,
            )
            assert replacement_selected_card is not first_card
            assert replacement_selected_card.is_attached
            assert app.client is not None and not app.client.lease
            assert all(not client.lease and not client.sent and not client.resizes
                       for client in registry.clients)
            assert registry.create_count == 0 and registry.stop_calls == []

    asyncio.run(scenario())


def test_r03_one_two_column_reflow_keeps_actual_card_focus():
    async def scenario():
        identities = [_identity(index) for index in range(1, 4)]
        registry = FakeRegistry(identities)
        app = WindowsJTTApp(registry)
        app.set_interval = lambda *_args, **_kwargs: SimpleNamespace(stop=lambda: None)
        async with app.run_test(size=(160, 44)) as pilot:
            await _wait_for_ui_state(
                pilot, lambda: app.selected_id == identities[0].session_id,
                "selected terminal before column reflow",
            )
            container = app.query_one("#terminal-cards")
            app._focus_panel(1)
            focused = await _wait_for_focused_widget(
                pilot, app, "#terminal-" + identities[0].session_id,
                "selected card focus established before reflow",
            )
            focused_id = focused.id
            focused_identity = _mini_identity_key(app.sessions[identities[0].session_id])

            # Exercise the same structural 1 <-> 2 column transitions used by
            # responsive widths while keeping the screen mounted and focus
            # observable throughout each actual card-tree rebuild.
            app._cards_per_row = lambda _width: 1
            app._card_structure = None
            app._card_signature = None
            await app._render_cards()
            rows = list(container.query(".card-row"))
            assert len(rows) == len(identities)
            first_card = app.query_one("#" + focused_id, Button)
            first_card = await _wait_for_focused_widget(
                pilot, app, "#" + focused_id,
                "focused card restored after one-column reflow",
            )
            assert app.focused is first_card and first_card.is_attached
            assert _mini_identity_key(app.sessions[identities[0].session_id]) == focused_identity

            app._cards_per_row = lambda _width: 2
            app._card_structure = None
            app._card_signature = None
            await app._render_cards()
            rows = list(container.query(".card-row"))
            assert len(rows) == 2
            first_card = app.query_one("#" + focused_id, Button)
            first_card = await _wait_for_focused_widget(
                pilot, app, "#" + focused_id,
                "focused card restored after two-column reflow",
            )
            assert app.focused is first_card and first_card.is_attached
            assert _mini_identity_key(app.sessions[identities[0].session_id]) == focused_identity
            assert registry.create_count == 0 and registry.stop_calls == []

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "key,expected_panel,expected_widget",
    [("tab", 2, "live"), ("shift+tab", 0, "window-window-default")],
)
def test_r03_navigation_during_card_rebuild_keeps_requested_panel_focus(
    key, expected_panel, expected_widget,
):
    async def scenario():
        identity = _identity(1)
        app = WindowsJTTApp(FakeRegistry([identity]))
        app.set_interval = lambda *_args, **_kwargs: SimpleNamespace(stop=lambda: None)
        async with app.run_test(size=(120, 35)) as pilot:
            await _wait_for_ui_state(
                pilot, lambda: app.selected_id == identity.session_id,
                "selected card before asynchronous rebuild",
            )
            app._focus_panel(1)
            container = app.query_one("#terminal-cards")
            remove_children = container.remove_children
            detached = asyncio.Event()
            resume = asyncio.Event()

            async def gated_remove_children(*args, **kwargs):
                detached.set()
                await resume.wait()
                return await remove_children(*args, **kwargs)

            container.remove_children = gated_remove_children
            app._card_structure = None
            app._card_signature = None
            refresh = asyncio.create_task(app._render_cards())
            await detached.wait()
            await pilot.press(key)
            requested_focus = app.focused
            assert app._panel_index == expected_panel
            assert requested_focus.id == expected_widget

            resume.set()
            await refresh
            assert app.focused is requested_focus
            assert app.focused.id == expected_widget
            assert app._panel_index == expected_panel

    asyncio.run(scenario())


def test_r03_modal_focus_is_not_stolen_by_pending_card_rebuild():
    async def scenario():
        identity = _identity(1)
        app = WindowsJTTApp(FakeRegistry([identity]))
        app.set_interval = lambda *_args, **_kwargs: SimpleNamespace(stop=lambda: None)
        async with app.run_test(size=(120, 35)) as pilot:
            await _wait_for_ui_state(
                pilot, lambda: app.selected_id == identity.session_id,
                "selected card before modal and rebuild",
            )
            app._focus_panel(1)
            container = app.query_one("#terminal-cards")
            remove_children = container.remove_children
            detached = asyncio.Event()
            resume = asyncio.Event()

            async def gated_remove_children(*args, **kwargs):
                detached.set()
                await resume.wait()
                return await remove_children(*args, **kwargs)

            container.remove_children = gated_remove_children
            app._card_structure = None
            app._card_signature = None
            refresh = asyncio.create_task(app._render_cards())
            await detached.wait()
            await app._prompt_create_window()
            name_input = await _wait_for_modal_input_focus(
                pilot, app, "#window-name",
                "create Window modal input mounted during card rebuild",
            )
            modal_focus = name_input
            assert modal_focus.id == "window-name"

            resume.set()
            await refresh
            assert isinstance(app.screen, ModalScreen)
            assert app.focused is modal_focus
            assert app.focused.id == "window-name"

    asyncio.run(scenario())


@pytest.mark.parametrize("change", ["deleted", "generation"])
def test_r03_card_rebuild_does_not_restore_focus_to_stale_identity(change):
    async def scenario():
        identity = _identity(1)
        app = WindowsJTTApp(FakeRegistry([identity]))
        app.set_interval = lambda *_args, **_kwargs: SimpleNamespace(stop=lambda: None)
        async with app.run_test(size=(120, 35)) as pilot:
            await _wait_for_ui_state(
                pilot, lambda: app.selected_id == identity.session_id,
                "selected terminal before identity change",
            )
            await app._render_cards()
            app._focus_panel(1)
            old_card = await _wait_for_focused_widget(
                pilot, app, "#terminal-" + identity.session_id,
                "old-generation card focus established before identity change",
            )
            assert old_card.id == "terminal-" + identity.session_id
            old_identity = _mini_identity_key(app.sessions[identity.session_id])
            container = app.query_one("#terminal-cards")
            remove_children = container.remove_children
            detached = asyncio.Event()
            resume = asyncio.Event()

            async def gated_remove_children(*args, **kwargs):
                detached.set()
                await resume.wait()
                return await remove_children(*args, **kwargs)

            container.remove_children = gated_remove_children
            app._card_structure = None
            app._card_signature = None
            refresh = asyncio.create_task(app._render_cards())
            await detached.wait()
            if change == "deleted":
                app.sessions.pop(identity.session_id)
            else:
                replacement = TerminalIdentity(**{
                    **identity.__dict__, "generation": "f" * 32,
                })
                app.sessions[identity.session_id] = replacement
            resume.set()
            await refresh

            # Complete any safe follow-up render for the changed card set.
            await app._render_cards()
            fallback = app.query_one("#new-terminal", Button)
            assert old_card is not fallback
            fallback = await _wait_for_focused_widget(
                pilot, app, "#new-terminal",
                "stale card focus moved to neutral CENTER action",
            )
            assert app.focused is fallback
            assert fallback.is_attached
            if change == "generation":
                current = app.sessions[identity.session_id]
                assert _mini_identity_key(current) != old_identity
                replacement_card = app.query_one("#terminal-" + identity.session_id, Button)
                assert app.focused is not replacement_card
            else:
                assert identity.session_id not in app.sessions
            assert app.client is not None and not app.client.lease
            assert all(not client.sent and not client.resizes for client in app.registry.clients)

    asyncio.run(scenario())


def test_r03_adding_and_removing_other_cards_preserves_focused_identity():
    async def scenario():
        first = _identity(1)
        second = _identity(2)
        registry = FakeRegistry([first, second])
        app = WindowsJTTApp(registry)
        app.set_interval = lambda *_args, **_kwargs: SimpleNamespace(stop=lambda: None)
        async with app.run_test(size=(120, 35)) as pilot:
            await _wait_for_ui_state(
                pilot, lambda: app.selected_id == first.session_id,
                "first selected card before add/remove",
            )
            app._focus_panel(1)
            focused = await _wait_for_focused_widget(
                pilot, app, "#terminal-" + first.session_id,
                "first selected card focus established before add/remove",
            )
            focused_id = focused.id
            focused_key = _mini_identity_key(app.sessions[first.session_id])
            app.sessions[second.session_id] = second
            await app._render_cards()
            first_card = await _wait_for_focused_widget(
                pilot, app, "#" + focused_id,
                "focused card retained after adding another card",
            )
            assert app.focused is first_card
            assert _mini_identity_key(app.sessions[first.session_id]) == focused_key

            app.sessions.pop(second.session_id)
            await app._render_cards()
            first_card = await _wait_for_focused_widget(
                pilot, app, "#" + focused_id,
                "focused card retained after removing another card",
            )
            assert app.focused is first_card
            assert first_card.is_attached
            assert _mini_identity_key(app.sessions[first.session_id]) == focused_key
            assert registry.create_count == 0 and registry.stop_calls == []

    asyncio.run(scenario())


def test_r03_mini_live_refresh_does_not_steal_focus_after_navigation():
    async def scenario():
        first, second = _identity(1), _identity(2)
        app = WindowsJTTApp(FakeRegistry([first, second]))
        app.set_interval = lambda *_args, **_kwargs: SimpleNamespace(stop=lambda: None)
        async with app.run_test(size=(120, 35)) as pilot:
            await _wait_for_ui_state(
                pilot, lambda: app.selected_id == first.session_id,
                "selected card before Mini LIVE update",
            )
            app._focus_panel(1)
            lock = app._cards_render_lock
            await lock.acquire()
            app._mini_cache[second.session_id] = (
                _mini_identity_key(second), 1, time.monotonic(), _screen("NEW_LIVE_FRAME"),
            )
            refresh = asyncio.create_task(app._render_cards())
            await pilot.press("tab")
            live = app.query_one("#live")
            assert app.focused is live
            lock.release()
            await refresh
            assert app.focused is live
            assert "NEW_LIVE_FRAME" in app.query_one(
                "#terminal-" + second.session_id, Button
            ).label.plain
            assert app.client is not None and not app.client.lease
            assert all(not client.sent and not client.resizes
                       for client in app.registry.clients)

    asyncio.run(scenario())


@pytest.mark.parametrize("size", [(80, 24), (120, 35), (160, 44)])
def test_r03_long_window_list_keeps_selected_window_in_view(tmp_path, size):
    async def scenario():
        registry = FakeRegistry()
        workspace = JTTWorkspaceStore(tmp_path / "synthetic-jtt")
        layout = JTTLayoutStore(workspace.root)
        windows = [layout.create_window(workspace.DEFAULT_ID, "folder-default", f"Window {n}")
                   for n in range(2, 10)]
        app = WindowsJTTApp(registry, workspace_store=workspace, layout_store=layout)
        async with app.run_test(size=size) as pilot:
            await _wait_for_ui_state(pilot, lambda: len(app.windows) == 9,
                                     "synthetic Window list")
            await app.select_window(windows[-1]["id"])
            selected_selector = "#window-" + windows[-1]["id"]
            selected = await _wait_for_focused_widget(
                pilot, app, selected_selector,
                "selected Window focus established",
            )
            selected = await _wait_for_widget_in_viewport(
                pilot, app, selected_selector, "#window-list",
                "selected Window intersects LEFT viewport",
            )
            window_list = app.query_one("#window-list")
            assert app._panel_index == 0 and app.focused is selected
            assert selected.region.intersection(window_list.content_region).height > 0
            assert app.selected_id is None and app.client is None
            assert registry.create_count == 0 and registry.stop_calls == []

    asyncio.run(scenario())


@pytest.mark.parametrize("size", [(80, 24), (120, 35), (160, 44)])
def test_r03_left_actions_are_keyboard_accessible_without_terminal_input(tmp_path, size):
    async def scenario():
        identity = _identity(1)
        registry = FakeRegistry([identity])
        workspace = JTTWorkspaceStore(tmp_path / "synthetic-jtt")
        layout = JTTLayoutStore(workspace.root)
        app = WindowsJTTApp(registry, workspace_store=workspace, layout_store=layout)
        async with app.run_test(size=size) as pilot:
            await _wait_for_ui_state(pilot, lambda: app.selected_id == identity.session_id,
                                     "initial synthetic selection")
            await pilot.press("ctrl+g", "w", "right")
            assert app._panel_index == 0 and app.focused.id == "new-window"
            await app._refresh_sessions()
            assert app.focused.id == "new-window"
            await pilot.press("enter")
            assert isinstance(app.screen, ModalScreen)
            name_input = app.screen.query_one("#window-name")
            name_input.cursor_position = len(name_input.value)
            await pilot.press("left")
            assert name_input.cursor_position == len(name_input.value) - 1
            assert app.focused is name_input
            await pilot.press("escape")
            await _wait_for_ui_state(pilot, lambda: not isinstance(app.screen, ModalScreen),
                                     "Window dialog cancellation")
            await pilot.press("ctrl+g", "w", "right", "right")
            assert app._panel_index == 0 and app.focused.id == "workspace-picker"
            await app._refresh_sessions()
            assert app.focused.id == "workspace-picker"
            await pilot.press("enter")
            assert isinstance(app.screen, ModalScreen)
            assert app.screen.query_one("#tab-work-kits", Button)
            await pilot.press("escape")
            await _wait_for_ui_state(pilot, lambda: not isinstance(app.screen, ModalScreen),
                                     "archive cancellation")
            await pilot.press("tab")
            assert app._panel_index == 1
            assert app.selected_identity == identity
            assert registry.create_count == 0 and registry.stop_calls == []
            assert all(not client.lease and not client.sent and not client.resizes
                       for client in registry.clients)

    asyncio.run(scenario())


@pytest.mark.parametrize("size", [(80, 24), (120, 35), (160, 44)])
def test_r03_keyboard_window_dialog_cancel_and_confirm_are_distinct(tmp_path, size):
    async def scenario():
        registry = FakeRegistry()
        workspace = JTTWorkspaceStore(tmp_path / "synthetic-jtt")
        layout = JTTLayoutStore(workspace.root)
        app = WindowsJTTApp(registry, workspace_store=workspace, layout_store=layout)
        async with app.run_test(size=size) as pilot:
            await _wait_for_ui_state(pilot, lambda: len(app.windows) == 1,
                                     "initial synthetic Window")
            await pilot.press("ctrl+g", "w", "right", "enter")
            assert isinstance(app.screen, ModalScreen)
            await pilot.press("tab")
            assert app.focused.id == "cancel-window"
            await pilot.press("enter")
            await _wait_for_ui_state(pilot, lambda: not isinstance(app.screen, ModalScreen),
                                     "Window creation cancellation")
            assert len(app.windows) == 1 and registry.create_count == 0

            await pilot.press("ctrl+g", "w", "right", "enter")
            assert isinstance(app.screen, ModalScreen)
            await pilot.press("enter")
            await _wait_for_ui_state(pilot, lambda: len(app.windows) == 2,
                                     "confirmed logical Window creation")
            assert app.selected_id is None and app.client is None
            assert registry.create_count == 0 and registry.stop_calls == []

    asyncio.run(scenario())


@pytest.mark.parametrize("size", [(80, 24), (120, 35), (160, 44)])
def test_r03_archive_tabs_preview_and_open_are_keyboard_accessible(tmp_path, size):
    def record_focus(name, value):
        # Keep the actual synthetic focus visible in the runner log even for
        # passing cases, without depending on external pytest plugins.
        print(f"JTT_ARCHIVE_FOCUS size={size!r} {name}={value}",
              file=sys.__stdout__, flush=True)

    async def scenario():
        identity = _identity(1)
        registry = FakeRegistry([identity])
        workspace = JTTWorkspaceStore(tmp_path / "synthetic-jtt")
        workspace.set_terminal_ids(workspace.DEFAULT_ID, [identity.session_id])
        saved = workspace.create_workspace("Saved review")
        workspace.set_terminal_ids(saved["id"], [identity.session_id])
        saved = next(item for item in workspace.workspaces() if item["id"] == saved["id"])
        layout = JTTLayoutStore(workspace.root)
        kits = JTTWorkKitStore(workspace.root)
        kits.create_kit("Review template", "Preview only", [
            {"name": "Checks", "role": "검증", "working_directory": None},
        ])
        app = WindowsJTTApp(registry, workspace_store=workspace, layout_store=layout,
                            work_kit_store=kits)
        async with app.run_test(size=size) as pilot:
            await _wait_for_ui_state(pilot, lambda: app.selected_id == identity.session_id,
                                     "synthetic terminal selection")
            await pilot.press("ctrl+g", "w", "right", "right", "enter")
            assert isinstance(app.screen, ModalScreen)
            await _wait_for_ui_state(
                pilot,
                lambda: type(app.screen) is SavedWorkspaceScreen
                         and app.screen.is_mounted,
                "Saved Workspace archive screen mounted",
            )
            record_focus("archive_initial_focus", _focus_description(app))
            archive = app.screen
            await _wait_for_ui_state(
                pilot,
                lambda: (app.focused is not None and app.focused.is_attached
                         and app.focused.screen is archive),
                "archive has an attached focus target",
            )
            record_focus("archive_ready_focus", _focus_description(app))
            selected_index = next(index for index, item in enumerate(archive.records)
                                  if item["id"] == app.workspace_id)
            saved_index = next(index for index, item in enumerate(archive.records)
                               if item["id"] == saved["id"])
            path = await _keyboard_focus_target(
                pilot, app, f"#saved-workspace-choice-{selected_index}", "tab",
                "current saved reference is keyboard reachable from actual initial focus",
            )
            record_focus("archive_to_selected_reference", repr(path))
            path = await _keyboard_focus_target(
                pilot, app, "#tab-work-kits", "shift+tab",
                "reverse navigation reaches Work Kit tab",
            )
            assert "saved-workspace-list" in path
            record_focus("archive_to_work_kit_tab", repr(path))
            await pilot.press("enter")
            await _wait_for_ui_state(pilot, lambda: app.screen.active_tab == "work-kit",
                                     "Work Kit tab")
            path = await _keyboard_focus_target(
                pilot, app, "#work-kit-choice-0", "tab", "Work Kit choice keyboard access",
            )
            record_focus("archive_to_work_kit_choice", repr(path))
            await pilot.press("enter")
            preview = app.screen.query_one("#work-kit-preview", Static).render()
            assert "Review template" in getattr(preview, "plain", str(preview))
            assert registry.create_count == 0

            path = await _keyboard_focus_target(
                pilot, app, "#tab-work-kits", "shift+tab",
                "reverse navigation reaches Work Kit tab",
            )
            assert "work-kit-list" in path
            record_focus("archive_from_work_kit_choice", repr(path))
            await _keyboard_focus_target(
                pilot, app, "#tab-saved-workspaces", "shift+tab",
                "reverse navigation reaches Saved Workspace tab",
            )
            await pilot.press("enter")
            await _wait_for_ui_state(pilot, lambda: app.screen.active_tab == "saved",
                                     "saved workspace tab")
            await _keyboard_focus_target(
                pilot, app, f"#saved-workspace-choice-{saved_index}", "tab",
                "saved workspace choice receives keyboard focus",
            )
            await pilot.press("enter")
            await _wait_for_ui_state(pilot, lambda: app.screen.selected_id == saved["id"],
                                     "saved workspace preview selection")
            await _keyboard_focus_target(
                pilot, app, "#open-saved-workspace", "tab",
                "open saved workspace action receives keyboard focus",
            )
            await pilot.press("enter")
            await _wait_for_ui_state(pilot, lambda: (app.workspace_id == saved["id"]
                                      and app.selected_identity == identity
                                      and app.client is not None),
                                     "saved reference opened")
            assert app.selected_identity == identity
            assert app.client is not None and not app.client.lease
            assert registry.create_count == 0 and registry.stop_calls == []
            assert all(not client.sent and not client.resizes for client in registry.clients)

    asyncio.run(scenario())


@pytest.mark.parametrize("size", [(80, 24), (120, 35), (160, 44)])
def test_r03_saved_workspace_name_retains_full_identity_in_context(tmp_path, size):
    async def scenario():
        registry = FakeRegistry()
        workspace = JTTWorkspaceStore(tmp_path / "synthetic-jtt")
        layout = JTTLayoutStore(workspace.root)
        name = "긴 저장 작업공간 Synthetic workspace review 0123456789"
        saved = workspace.create_workspace(name)
        app = WindowsJTTApp(registry, workspace_store=workspace, layout_store=layout)
        async with app.run_test(size=size) as pilot:
            await _wait_for_ui_state(pilot, lambda: bool(app.windows), "Window list")
            await app.select_workspace(saved)
            context = app.query_one("#workspace-context", Static)
            rendered = context.render()
            assert getattr(rendered, "plain", str(rendered)) == name
            assert name in str(context.tooltip)
            assert context.styles.text_overflow == "ellipsis"
            assert app.workspace_id == saved["id"]
            assert registry.create_count == 0 and registry.stop_calls == []

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
            await app._refresh_sessions()
            await app._refresh_live()
            assert app.selected_id is None and app.selected_identity is None
            assert app.client is None and not app.input_mode
            assert app._right_observation is None
            assert second.session_id not in app._mini_cache
            assert "신원" in str(app.query_one("#live", Static).render())
            assert all(not client.lease for client in registry.clients)

            await app._select_session(changed)
            assert app.selected_identity == changed
            assert app.client is not None and not app.client.lease
            assert not app.input_mode

    asyncio.run(scenario())


def test_right_paste_uses_only_current_verified_input_lease():
    async def scenario():
        first, second = _identity(1), _identity(2)
        registry = FakeRegistry([first, second])
        app = WindowsJTTApp(registry)
        async with app.run_test(size=(80, 24)) as pilot:
            await _wait_for_ui_state(
                pilot, lambda: app.selected_id == first.session_id,
                "initial selected terminal",
            )
            await app.enter_input_mode()
            client = app.client
            assert client is not None and client.lease

            text = "Write-Output '붙여넣기 JTT'"
            event = Paste(text)
            await app.on_paste(event)
            assert event._stop_propagation
            assert client.sent == [text.encode("utf-8")]

            # A lost lease, stale identity, or a session outside the selected
            # Window must block delivery without touching another client.
            client.sent.clear()
            client.lease = False
            await app.on_paste(Paste("must-not-send-without-lease"))
            assert client.sent == []

            await app.enter_input_mode()
            client = app.client
            assert client is not None and client.lease
            client.lease = True
            changed = TerminalIdentity(**{**first.__dict__, "generation": "f" * 32})
            app.sessions[first.session_id] = changed
            await app.on_paste(Paste("must-not-send-stale-generation"))
            assert client.sent == []

            app.sessions[first.session_id] = first
            await app.enter_input_mode()
            client = app.client
            assert client is not None and client.lease
            app.sessions.pop(first.session_id)
            await app.on_paste(Paste("must-not-send-outside-window"))
            assert client.sent == []
            assert registry.clients[0].sent == []

            app.sessions[first.session_id] = first
            await app.enter_input_mode()
            client = app.client
            assert client is not None and client.lease
            client.sent.clear()
            await app.on_paste(Paste("x" * 8193))
            assert client.sent == []

    asyncio.run(scenario())


def test_prefix_copy_uses_fresh_selected_right_screen_only_on_request(monkeypatch):
    windows_ui = importlib.import_module("ju0_terminal_tower.ui.windows_jtt")
    copied = []
    monkeypatch.setattr(
        windows_ui, "_write_windows_clipboard", lambda text: copied.append(text),
        raising=False,
    )

    async def scenario():
        identity = _identity(1)
        registry = FakeRegistry([identity])
        app = WindowsJTTApp(registry)
        async with app.run_test(size=(120, 40)) as pilot:
            await _wait_for_ui_state(
                pilot, lambda: app.selected_id == identity.session_id
                and app.client is not None,
                "selected PowerShell observation before explicit copy",
            )
            client = app.client
            assert client is not None and not client.lease
            assert copied == []

            lines = (
                "PowerShell 오류: 경로를 찾을 수 없습니다.",
                "Get-Item: Cannot find path 'JTT_MISSING' because it does not exist.",
            )
            screen = _screen_lines(*lines)
            client.observe = lambda: TerminalObservation(
                session_id=identity.session_id, generation=identity.generation,
                shell_pid=identity.shell_pid, sequence=17, state="running",
                synced=True, columns=100, lines=30, screen=screen,
                renderer_state="ready",
            )

            await pilot.press("ctrl+g", "c")
            await _wait_for_ui_state(
                pilot, lambda: bool(copied), "explicit RIGHT screen copy",
            )
            assert copied == ["\r\n".join(lines)]
            assert client.lease is False
            assert app.input_mode is False
            assert client.sent == []

            # A changed generation invalidates the copy target without touching
            # the clipboard or sending anything to the old session.
            app.sessions[identity.session_id] = TerminalIdentity(**{
                **identity.__dict__, "generation": "f" * 32,
            })
            await pilot.press("ctrl+g", "c")
            await pilot.pause(0.05)
            assert copied == ["\r\n".join(lines)]
            assert client.sent == []

    asyncio.run(scenario())


def test_windows_copy_chord_never_becomes_shell_ctrl_c(monkeypatch):
    windows_ui = importlib.import_module("ju0_terminal_tower.ui.windows_jtt")
    copied = []
    monkeypatch.setattr(windows_ui, "_write_windows_clipboard", copied.append)

    async def scenario():
        identity = _identity(1)
        app = WindowsJTTApp(FakeRegistry([identity]))
        async with app.run_test(size=(120, 40)) as pilot:
            await _wait_for_ui_state(
                pilot, lambda: app.selected_id == identity.session_id
                and app.client is not None,
                "selected terminal before copy chord",
            )
            await app.enter_input_mode()
            client = app.client
            assert client is not None and client.lease
            client.sent.clear()

            await pilot.press("ctrl+shift+c")
            assert client.sent == []
            assert copied == [client.screen_text]
            assert app.input_mode is True
            await pilot.press("ctrl+c")
            assert client.sent == [b"\x03"]

    asyncio.run(scenario())


def test_shift_click_keeps_windows_terminal_native_selection_available():
    async def scenario():
        identity = _identity(1)
        app = WindowsJTTApp(FakeRegistry([identity]))
        async with app.run_test(size=(120, 40)) as pilot:
            await _wait_for_ui_state(
                pilot, lambda: app.selected_id == identity.session_id
                and app.client is not None,
                "selected terminal before native selection click",
            )
            assert app.input_mode is False
            await pilot.click("#live", shift=True)
            assert app.input_mode is False
            assert app.client is not None and not app.client.lease

    asyncio.run(scenario())


def test_windows_clipboard_writer_sets_unicode_without_reading(monkeypatch):
    windows_ui = importlib.import_module("ju0_terminal_tower.ui.windows_jtt")
    calls = []
    clipboard = SimpleNamespace(
        OpenClipboard=lambda: calls.append(("open",)),
        EmptyClipboard=lambda: calls.append(("empty",)),
        SetClipboardData=lambda kind, text: calls.append(("set", kind, text)),
        CloseClipboard=lambda: calls.append(("close",)),
    )
    constants = SimpleNamespace(CF_UNICODETEXT=13)
    monkeypatch.setitem(sys.modules, "win32clipboard", clipboard)
    monkeypatch.setitem(sys.modules, "win32con", constants)

    windows_ui._write_windows_clipboard("오류\r\nPowerShell error")

    assert calls == [
        ("open",), ("empty",), ("set", 13, "오류\r\nPowerShell error"), ("close",),
    ]


@pytest.mark.parametrize("size", [(80, 24), (160, 44)])
def test_terminal_card_identity_text_does_not_wrap(size):
    async def scenario():
        identity = _identity(1)
        identity = TerminalIdentity(**{
            **identity.__dict__, "name": "Long PowerShell Session Name",
        })
        app = WindowsJTTApp(FakeRegistry([identity]))
        async with app.run_test(size=size):
            button = app.query_one("#terminal-" + identity.session_id, Button)
            assert button.styles.text_wrap == "nowrap"
            assert button.styles.text_overflow == "ellipsis"

    asyncio.run(scenario())


def test_windows_client_send_does_not_advance_output_cursor(tmp_path):
    from ju0_terminal_tower.terminal.windows_manager import TerminalClient

    identity = _identity(1)
    client = TerminalClient.__new__(TerminalClient)
    client._closed = False
    client.identity = identity
    client._client_id = "client"
    client._output_cursor = 7
    client.synced = True
    client.lease = True
    client.state = "running"
    client._root = tmp_path
    calls = []

    reported_sequences = iter((9, 12))

    def request(operation, **fields):
        calls.append((operation, fields))
        assert operation == "input"
        assert fields["after"] == 7
        return {
            "ok": True, "session_id": identity.session_id,
            "generation": identity.generation, "sequence": next(reported_sequences),
            "synced": True, "renderer_available": True,
            "written": len(b"x"), "lease": True,
        }

    client._request = request
    assert client.send(b"x") == 1
    assert client.send(b"y") == 1
    assert len(calls) == 2
    assert [call[0] for call in calls] == ["input", "input"]
    assert [call[1]["after"] for call in calls] == [7, 7]
    assert client._output_cursor == 7
    assert client.synced and client.lease


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
            # This test invokes the sampler explicitly below. Keep real timer
            # objects so teardown is checked, but prevent background ticks
            # from consuming the sampler before the assertion.
            timer = set_interval(3600, *args[1:], **kwargs)
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
            # Exercise a structural card remount; live-only changes now update
            # existing buttons and do not call remove_children.
            app._card_structure = None
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


@pytest.mark.parametrize("size", [(80, 24), (120, 35), (120, 40), (160, 44)])
def test_logical_window_switch_filters_cards_and_releases_old_lease(tmp_path, size):
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
        async with app.run_test(size=size) as pilot:
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
            assert app.selected_identity.generation == second.generation
            assert app.client.identity.generation == second.generation
            assert app.client.lease is False
            assert registry.create_count == 0

    asyncio.run(scenario())


@pytest.mark.parametrize("size", [(80, 24), (120, 35), (120, 40), (160, 44)])
def test_keyboard_window_switch_keeps_left_focus_and_does_not_acquire_new_lease(tmp_path, size):
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
        async with app.run_test(size=size) as pilot:
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
            assert app.selected_identity.generation == second.generation
            assert app.client is not None and not app.client.lease
            assert old_client.closed and not old_client.lease
            assert registry.create_count == 0

    asyncio.run(scenario())


@pytest.mark.parametrize("size", [(80, 24), (120, 35), (160, 44)])
def test_mini_live_observes_each_card_without_lease_input_or_resize(tmp_path, size):
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
        async with app.run_test(size=size) as pilot:
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
            app._card_structure = None
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


@pytest.mark.parametrize("size", [(80, 24), (120, 35), (160, 44)])
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


@pytest.mark.parametrize("size", [(80, 24), (120, 35), (160, 44)])
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


@pytest.mark.parametrize("size", [(80, 24), (120, 35), (160, 44)])
def test_work_kit_archive_tab_previews_templates_without_running_or_writing(tmp_path, size):
    async def scenario():
        identity = _identity(1)
        registry = FakeRegistry([identity])
        workspace_store = JTTWorkspaceStore(tmp_path / "jtt")
        layout_store = JTTLayoutStore(workspace_store.root)
        work_kit_store = JTTWorkKitStore(workspace_store.root)
        workspace_store.set_terminal_ids(
            workspace_store.DEFAULT_ID, [identity.session_id],
        )
        kit = work_kit_store.create_kit(
            "API review", "Template only", [
                {"name": "Server", "role": "개발", "working_directory": "C:/work/api"},
                {"name": "Checks", "role": "검증", "working_directory": None},
            ],
        )
        workspace_bytes = workspace_store.path.read_bytes()
        kit_bytes = work_kit_store.path.read_bytes()
        app = WindowsJTTApp(
            registry, workspace_store=workspace_store, layout_store=layout_store,
            work_kit_store=work_kit_store,
        )
        async with app.run_test(size=size) as pilot:
            await _wait_for_ui_state(
                pilot, lambda: app.selected_id == identity.session_id,
                "initial terminal observation before Work Kit preview",
            )
            await pilot.click("#workspace-picker")
            await pilot.click("#tab-work-kits")
            await pilot.click("#work-kit-choice-0")

            screen = app.screen
            preview = screen.query_one("#work-kit-preview", Static).render()
            preview_text = getattr(preview, "plain", str(preview))
            assert "API review" in preview_text
            assert "Template only" in preview_text
            assert "Server" in preview_text and "C:/work/api" in preview_text
            assert "실행되지 않습니다" in preview_text
            assert screen.query_one("#open-saved-workspace", Button).has_class(
                "archive-hidden"
            )
            assert screen.query_one("#work-kit-preview-only", Button).disabled
            assert not screen.query_one("#work-kit-list").has_class("archive-hidden")
            assert screen.query_one("#saved-workspace-list").has_class("archive-hidden")
            assert registry.create_count == 0
            assert all(not client.lease and client.acquire_calls == 0
                       and not client.sent and not client.resizes
                       for client in registry.clients)
            assert workspace_store.path.read_bytes() == workspace_bytes
            assert work_kit_store.path.read_bytes() == kit_bytes

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


def test_vt_screen_text_coalesces_uniform_cell_styles_for_large_screens():
    from ju0_terminal_tower.ui.windows_jtt import _screen_text

    columns, lines = 160, 44
    row = tuple(TerminalCell(" ", "default", "default", False, False, False,
                             False, False, False) for _ in range(columns))
    screen = TerminalScreen(tuple(row for _ in range(lines)), 0, 0, True)

    rendered = _screen_text(screen, columns, lines)

    assert len(rendered.plain) == columns * lines + lines - 1
    # A cell-by-cell renderer produced thousands of Rich spans here. The
    # cursor contributes one distinct style span; the remaining cells share
    # one cached style per row at most.
    assert len(rendered.spans) <= lines + 1


@pytest.mark.skipif(sys.platform != "win32", reason="native ConPTY required")
def test_native_windows_paste_event_enters_selected_powershell(tmp_path):
    from ju0_terminal_tower.terminal.windows_manager import TerminalRegistry

    def screen_text(screen):
        return "\n".join("".join(cell.text for cell in row) for row in screen.rows)

    registry = TerminalRegistry(root=tmp_path / "isolated-jtt" / "terminals")
    app = WindowsJTTApp(registry)
    created = {}

    async def scenario():
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.click("#new-terminal")
            await _wait_for_ui_state(
                pilot, lambda: app.input_mode and app.client is not None,
                "new isolated PowerShell input lease",
            )
            identity = app.selected_identity
            assert identity is not None and app.client is not None and app.client.lease
            created["identity"] = identity
            nonce = identity.session_id[:8]
            expected = f"JTT_{nonce}"
            command = f"[Console]::WriteLine('JTT_' + '{nonce}')"
            assert expected not in command
            client = app.client
            assert client is not None
            paste_bytes = command.encode("utf-8")
            paste_written = threading.Event()
            original_send = client.send

            def record_paste(data):
                written = original_send(data)
                if data == paste_bytes:
                    paste_written.set()
                return written

            client.send = record_paste
            assert app.post_message(Paste(command))
            assert await asyncio.to_thread(paste_written.wait, 10), (
                "Textual Paste event was not sent to the active ConPTY lease"
            )
            await app._send_bytes(b"\r")
            await _wait_for_ui_state(
                pilot,
                lambda: app._right_observation is not None
                and expected in screen_text(app._right_observation[3]),
                "PowerShell executed pasted command",
                timeout=20,
            )

    try:
        asyncio.run(scenario())
    finally:
        identity = created.get("identity")
        if identity is not None:
            registry.stop(
                identity.session_id, expected_generation=identity.generation,
                expected_shell_pid=identity.shell_pid,
            )


@pytest.mark.skipif(sys.platform != "win32", reason="native ConPTY required")
def test_native_windows_ui_explicit_copy_captures_unicode_multiline_and_error(
        tmp_path, monkeypatch, record_property):
    # Patch the module that defines the already-imported app class. Earlier
    # tests may also import the shared namespace under its legacy name, so a
    # package-name import here can target a second module object in a full run.
    windows_ui = importlib.import_module(WindowsJTTApp.__module__)
    from ju0_terminal_tower.terminal.windows_manager import TerminalRegistry

    monkeypatch.chdir(tmp_path)
    registry = TerminalRegistry(root=tmp_path / "isolated-copy-home" / "terminals")
    owner = registry.create("Isolated copy output")
    identity = owner.identity
    owner.close()
    app = WindowsJTTApp(registry)
    copied = []
    monkeypatch.setattr(windows_ui, "_write_windows_clipboard", copied.append)

    async def scenario():
        async with app.run_test(size=(160, 44)) as pilot:
            await _wait_for_ui_state(
                pilot, lambda: app.selected_id == identity.session_id
                and app.client is not None,
                "existing isolated ConPTY copy target", timeout=20,
            )
            assert app.selected_identity is not None
            assert _same_identity(identity, app.selected_identity)

            def initial_prompt_visible():
                observation = app._right_observation
                if observation is None:
                    return False
                return any(
                    line.strip().startswith("PS ")
                    and line.strip().endswith(">")
                    and len(line.strip()) > len("PS >")
                    for line in _plain_screen_text(observation[3]).splitlines()
                )

            await _wait_for_ui_state(
                pilot, initial_prompt_visible,
                "initial PowerShell prompt before copy input", timeout=20,
            )
            await app.enter_input_mode()
            assert app.input_mode and app.client is not None and app.client.lease
            nonce = identity.session_id[:10]
            input_literal = f"한글-{nonce}"
            ascii_marker_file = tmp_path / f"f-{nonce}.txt"
            unicode_side_effect_file = tmp_path / f"u-{nonce}.txt"
            unicode_execution_marker = f"JTT_COPY_UNICODE_EXEC_{nonce}_END"
            unicode_echo_probe = f"JTT_COPY_INPUT_ECHO_{nonce}"
            expected = (
                f"jtt_copy_ascii_{nonce}_end",
                f"JTT_COPY_INPUT_{input_literal}_END",
                f"한글-JTT_COPY_OUTPUT_{nonce}-OK",
                f"JTT_ERROR_{nonce}_END",
                f"JTT_FINAL_{nonce}_END",
            )
            commands = (
                ("[System.IO.File]::WriteAllText('" + ascii_marker_file.name
                 + "','" + nonce + "'); [Console]::WriteLine('jtt_copy_ascii_' + '"
                 + nonce + "' + '_end')", expected[0]),
                ("[System.IO.File]::WriteAllText('" + unicode_side_effect_file.name
                 + "','" + nonce + "'); $global:JTT_COPY_INPUT_ECHO='"
                 + unicode_echo_probe + "'; [Console]::WriteLine('JTT_COPY_UNICODE_EXEC_' + '"
                 + nonce + "' + '_END'); Write-Output ('JTT_COPY_INPUT_' + '"
                 + f"{input_literal}" + "' + '_END')", expected[1]),
                ("Write-Output (([string]([char]0xD55C) + "
                 "[string]([char]0xAE00)) + "
                 f"'-JTT_COPY_OUTPUT_{nonce}-OK')", expected[2]),
                (f"Write-Error ('JTT_ERROR_' + '{nonce}' + '_END')", expected[3]),
                (f"Write-Output ('JTT_FINAL_' + '{nonce}' + '_END')", expected[4]),
            )
            assert expected[0] not in commands[0][0]
            assert expected[1] not in commands[1][0]
            assert unicode_execution_marker not in commands[1][0]
            assert input_literal in commands[1][0]
            client = app.client
            original_send = client.send
            accepted_inputs = []
            accepted_payload = bytearray()

            def record_send(data):
                from ju0_terminal_tower.terminal.manager import TerminalError

                outcome = {
                    "attempted": True,
                    "byte_count": len(data),
                    "returned": False,
                    "accepted_bytes": None,
                    "terminal_error": None,
                }
                try:
                    accepted = original_send(data)
                except TerminalError as exc:
                    category = str(exc)
                    if category not in {
                        "input_lease_required", "stream_gap", "stale_identity",
                        "renderer_unavailable", "terminal input response is invalid",
                        "input_delivery_unconfirmed",
                        "terminal input delivery could not be confirmed",
                    }:
                        category = "terminal_error_other"
                    outcome["terminal_error"] = category
                    accepted_inputs.append(outcome)
                    raise
                except Exception as exc:
                    outcome["terminal_error"] = type(exc).__name__
                    accepted_inputs.append(outcome)
                    raise
                outcome.update({
                    "returned": True,
                    "accepted_bytes": accepted,
                })
                accepted_payload.extend(data)
                accepted_inputs.append(outcome)
                return accepted

            monkeypatch.setattr(client, "send", record_send)
            import json

            record_property(
                "copy_right_focus_before_wait",
                json.dumps({
                    "input_mode": bool(app.input_mode),
                    "right_panel_index": app._panel_index == 2,
                    "focused_widget_id": getattr(app.focused, "id", None),
                    "live_widget_has_focus": app.query_one("#live").has_focus,
                    "lease_owned": bool(client.lease),
                    "selected_identity_matches_client": (
                        app.selected_identity is not None
                        and _same_identity(app.selected_identity, client.identity)
                    ),
                }, sort_keys=True),
            )
            await _wait_for_ui_state(
                pilot,
                lambda: app._panel_index == 2
                and getattr(app.focused, "id", None) == "live",
                "RIGHT terminal focus after input lease acquisition",
            )
            record_property(
                "copy_right_focus_after_wait",
                json.dumps({
                    "right_panel_index": app._panel_index == 2,
                    "focused_widget_id": getattr(app.focused, "id", None),
                    "live_widget_has_focus": app.query_one("#live").has_focus,
                }, sort_keys=True),
            )

            async def collect_output_and_screen(
                marker, after_sequence, *, input_echo_probe=None
            ):
                raw = bytearray()
                last_update = None
                deadline = time.monotonic() + 20
                marker_bytes = marker.encode("utf-8")
                raw_marker_seen = False
                screen_observation = app._right_observation
                screen_marker_seen = False
                screen_identity_matches = False
                input_echo_raw_seen = False
                input_echo_screen_seen = False
                while time.monotonic() < deadline:
                    # Serialize this diagnostic poll with the app's observer;
                    # one bounded window gathers both output and UI evidence.
                    last_update = await app._backend_call(client.poll)
                    raw.extend(last_update.output)
                    compact = bytes(raw).replace(b"\r", b"").replace(b"\n", b"")
                    raw_marker_seen = marker_bytes in compact
                    if input_echo_probe is not None:
                        normalized_raw = b"".join(raw.split())
                        input_echo_raw_seen = input_echo_raw_seen or (
                            input_echo_probe.encode("ascii") in normalized_raw
                        )
                    screen_observation = app._right_observation
                    if (screen_observation is not None
                            and screen_observation[1] > after_sequence):
                        screen_text = "".join(
                            _plain_screen_text(screen_observation[3]).split()
                        )
                        screen_marker_seen = marker in screen_text
                        if input_echo_probe is not None:
                            input_echo_screen_seen = input_echo_screen_seen or (
                                input_echo_probe in screen_text
                            )
                        screen_identity_matches = (
                            screen_observation[0] == _mini_identity_key(identity)
                        )
                    if raw_marker_seen and screen_marker_seen:
                        break
                    await pilot.pause(0.02)
                return (
                    last_update, bytes(raw), raw_marker_seen,
                    screen_observation, screen_marker_seen,
                    screen_identity_matches, input_echo_raw_seen,
                    input_echo_screen_seen,
                )

            for index, (command, marker) in enumerate(commands):
                assert marker not in command
                encoded = (command + "\r").encode("utf-8")
                prior_observation = app._right_observation
                assert prior_observation is not None
                prior_sequence = prior_observation[1]
                prior_count = len(accepted_inputs)
                prior_payload_size = len(accepted_payload)
                selected_before_send = app.selected_identity
                visible_before_send = (
                    app.sessions.get(selected_before_send.session_id)
                    if selected_before_send is not None else None
                )
                send_predicates = {
                    "input_mode": bool(app.input_mode),
                    "right_panel_index": app._panel_index == 2,
                    "right_widget_focused": (
                        getattr(app.focused, "id", None) == "live"
                    ),
                    "client_present": app.client is not None,
                    "client_is_current": app.client is client,
                    "lease_owned": bool(client.lease),
                    "selected_identity_present": selected_before_send is not None,
                    "selection_matches": (
                        selected_before_send is not None
                        and app._selection_matches(selected_before_send)
                    ),
                    "visible_session_present": visible_before_send is not None,
                    "visible_identity_matches": (
                        selected_before_send is not None
                        and visible_before_send is not None
                        and _same_identity(selected_before_send, visible_before_send)
                    ),
                    "client_identity_matches": (
                        selected_before_send is not None
                        and _same_identity(selected_before_send, client.identity)
                    ),
                    "last_output_sequence": prior_sequence,
                }
                record_property(
                    f"copy_input_{index}_pre_send",
                    json.dumps(send_predicates, sort_keys=True),
                )
                input_error_category = None
                delivery_path = "app._send_bytes"
                try:
                    if index == 0:
                        delivery_path = "Textual on_key via Pilot.press"
                        await pilot.press(*(
                            "space" if character == " " else character
                            for character in command
                        ))
                        await pilot.press("enter")
                    else:
                        await app._send_bytes(encoded)
                except Exception as exc:
                    from ju0_terminal_tower.terminal.manager import TerminalError

                    if isinstance(exc, TerminalError):
                        category = str(exc)
                        input_error_category = (
                            category if category in {
                                "input_lease_required", "stream_gap",
                                "stale_identity", "renderer_unavailable",
                                "terminal input response is invalid",
                                "input_delivery_unconfirmed",
                                "terminal input delivery could not be confirmed",
                            } else "terminal_error_other"
                        )
                    else:
                        input_error_category = type(exc).__name__
                    raise
                finally:
                    send_attempts = accepted_inputs[prior_count:]
                    record_property(
                        f"copy_input_{index}_send_result",
                        json.dumps({
                            "client_send_called": bool(send_attempts),
                            "send_outcome": (
                                send_attempts[-1] if send_attempts else None
                            ),
                            "input_error_category": input_error_category,
                            "delivery_path": delivery_path,
                            "last_output_sequence": (
                                app._right_observation[1]
                                if app._right_observation is not None else None
                            ),
                            "output_sequence_advanced": (
                                app._right_observation is not None
                                and app._right_observation[1] > prior_sequence
                            ),
                            "utf8_payload_bytes": len(encoded),
                            "utf8_payload_characters": len(command) + 1,
                            "utf8_payload_contains_non_ascii": any(
                                value > 0x7f for value in encoded
                            ),
                            "payload_sha256": hashlib.sha256(encoded).hexdigest(),
                            "payload_template_sha256": hashlib.sha256(
                                (command.replace(nonce, "<NONCE>") + "\r")
                                .encode("utf-8")
                            ).hexdigest(),
                            "terminator": "CR",
                            "input_delivery_mode": (
                                "single_write" if index == 1
                                else "per_key_then_enter"
                            ),
                            "working_directory_length": len(str(tmp_path)),
                            "ui_viewport_size": [
                                getattr(app.size, "width", None),
                                getattr(app.size, "height", None),
                            ],
                            "terminal_screen_size": [
                                getattr(prior_observation[3], "columns", None),
                                getattr(prior_observation[3], "lines", None),
                            ],
                            "worker_start_argument_profile": [
                                "-NoLogo", "-NoProfile", "-NoExit", "-Command",
                                "<product bootstrap gate script>",
                            ],
                            "worker_argument_profile_source": (
                                "_windows_worker.py:_bootstrap_script argv; "
                                "source-derived, not process-commandline observation"
                            ),
                            "protocol_confirmed_pty_write_bytes": (
                                sum(
                                    int(item["accepted_bytes"] or 0)
                                    for item in send_attempts
                                ) if send_attempts and all(
                                    item["returned"]
                                    and item["accepted_bytes"] is not None
                                    for item in send_attempts
                                ) else None
                            ),
                            "pty_write_return_source": (
                                "worker input ACK; _write_terminal_input returns only "
                                "the exact PTY.write byte count"
                            ),
                        }, sort_keys=True),
                    )
                send_attempts = accepted_inputs[prior_count:]
                sent_payload = bytes(accepted_payload[prior_payload_size:])
                assert sent_payload == encoded, (
                    f"UI input {index} bytes did not match the exact UTF-8 "
                    "command payload"
                )
                record_property(
                    f"copy_input_{index}_accepted_payload_bytes_match_utf8",
                    True,
                )
                if index == 0:
                    assert len(send_attempts) == len(encoded), (
                        "Textual key events did not each receive a terminal "
                        "send acknowledgement"
                    )
                    assert all(
                        outcome["returned"] and outcome["accepted_bytes"] == 1
                        and outcome["byte_count"] == 1
                        for outcome in send_attempts
                    ), "Textual key event input was not fully acknowledged"
                else:
                    assert len(send_attempts) == 1, (
                        "UI input did not receive a terminal send acknowledgement"
                    )
                    assert send_attempts[0] == {
                        "attempted": True,
                        "byte_count": len(encoded),
                        "returned": True,
                        "accepted_bytes": len(encoded),
                        "terminal_error": None,
                    }
                (
                    raw_update, raw_output, raw_marker_seen,
                    screen_observation, screen_marker_seen,
                    screen_identity_matches, input_echo_raw_seen,
                    input_echo_screen_seen,
                ) = await collect_output_and_screen(
                    marker, prior_sequence,
                    input_echo_probe=(unicode_echo_probe if index == 1 else None),
                )
                compact_raw_output = raw_output.replace(b"\r", b"").replace(
                    b"\n", b""
                )
                raw_marker_seen = marker.encode("utf-8") in compact_raw_output
                ascii_marker_value = None
                unicode_side_effect_value = None
                if index == 0:
                    try:
                        ascii_marker_value = ascii_marker_file.read_text(
                            encoding="ascii"
                        ).strip()
                    except (OSError, UnicodeError):
                        ascii_marker_value = None
                    record_property(
                        "copy_ascii_command_side_effect_matches_nonce",
                        ascii_marker_value == nonce,
                    )
                elif index == 1:
                    try:
                        unicode_side_effect_value = unicode_side_effect_file.read_text(
                            encoding="ascii"
                        ).strip()
                    except (OSError, UnicodeError):
                        unicode_side_effect_value = None
                raw_sequence = (
                    getattr(raw_update, "sequence", None)
                    if raw_update is not None else None
                )
                raw_snapshot_sequence = (
                    getattr(raw_update, "snapshot_sequence", None)
                    if raw_update is not None else None
                )
                raw_gap = getattr(raw_update, "gap", None) if raw_update is not None else None
                screen_sequence = (
                    screen_observation[1] if screen_observation is not None else None
                )
                raw_text_for_categories = raw_output.decode(
                    "utf-8", errors="replace"
                )
                powershell_error_categories = [
                    category for category in (
                        "ParserError", "PSSecurityException", "ExecutionPolicy",
                        "UnauthorizedAccessException", "Write-Error",
                        "Cannot find path", "Set-Location :",
                    ) if category in raw_text_for_categories
                ]
                unicode_execution_raw_seen = (
                    unicode_execution_marker.encode("ascii")
                    in compact_raw_output
                ) if index == 1 else None
                unicode_execution_screen_seen = (
                    unicode_execution_marker in "".join(
                        _plain_screen_text(screen_observation[3]).split()
                    ) if screen_observation is not None else False
                ) if index == 1 else None
                record_property(
                    f"copy_input_{index}_observation_summary",
                    json.dumps({
                        "ascii_side_effect_value_matches": (
                            ascii_marker_value == nonce if index == 0 else None
                        ),
                        "raw_output_marker_seen": raw_marker_seen,
                        "raw_sequence": raw_sequence,
                        "raw_snapshot_sequence": raw_snapshot_sequence,
                        "raw_gap": raw_gap,
                        "raw_payload_bytes": len(raw_output),
                        "screen_marker_seen": screen_marker_seen,
                        "screen_sequence": screen_sequence,
                        "screen_observation_identity_matches": screen_identity_matches,
                        "powershell_error_categories": powershell_error_categories,
                        "unicode_execution_side_effect_matches": (
                            unicode_side_effect_value == nonce if index == 1 else None
                        ),
                        "unicode_execution_marker_raw_seen": unicode_execution_raw_seen,
                        "unicode_execution_marker_screen_seen": unicode_execution_screen_seen,
                        "unicode_input_echo_probe_raw_seen": (
                            input_echo_raw_seen if index == 1 else None
                        ),
                        "unicode_input_echo_probe_screen_seen": (
                            input_echo_screen_seen if index == 1 else None
                        ),
                    }, sort_keys=True),
                )
                if index == 0:
                    assert ascii_marker_value == nonce, (
                        "ASCII UI command execution was not proven by its "
                        "independent test-owned file side effect"
                    )
                if index == 1:
                    assert unicode_side_effect_value == nonce, (
                        "Unicode-input command execution was not proven by its "
                        "independent test-owned file side effect"
                    )
                    assert unicode_execution_raw_seen, (
                        "Unicode-input command side effect was present but its "
                        "ASCII execution marker was absent from ConPTY output"
                    )
                    assert unicode_execution_screen_seen, (
                        "Unicode-input command execution marker was not reflected "
                        "in the matching RIGHT screen"
                    )
                assert raw_marker_seen, (
                    "command execution evidence was collected, but the expected "
                    "ConPTY output marker was not observed"
                )
                assert raw_update is not None and raw_sequence > prior_sequence
                assert not raw_gap
                record_property(
                    f"copy_input_{index}_raw_output",
                    json.dumps({
                        "output_marker_observed": True,
                        "sequence_before": prior_sequence,
                        "sequence_after": raw_sequence,
                        "snapshot_sequence": raw_snapshot_sequence,
                        "gap": raw_gap,
                        "raw_byte_count": len(raw_output),
                    }, sort_keys=True),
                )
                assert screen_marker_seen, (
                    "ConPTY output was observed, but the matching RIGHT screen "
                    "snapshot was not observed"
                )
                assert screen_identity_matches, (
                    "RIGHT screen observation did not match the selected session identity"
                )
                observed_sequence = app._right_observation[1]
                record_property(
                    f"copy_input_{index}_output",
                    json.dumps({
                        "sequence_advanced": observed_sequence > prior_sequence,
                        "sequence_before": prior_sequence,
                        "sequence_after": observed_sequence,
                        "expected_output_observed": True,
                    }, sort_keys=True),
                )
            await app.leave_input_mode()
            ui_observation_before_copy = app._right_observation
            assert ui_observation_before_copy is not None
            selected = app.selected_identity
            visible = (
                app.sessions.get(selected.session_id)
                if selected is not None else None
            )
            copy_predicates = {
                "not_busy": not app._busy,
                "identity_present": selected is not None,
                "client_present": app.client is not None,
                "client_is_current": app.client is client,
                "selection_matches": (
                    selected is not None and app._selection_matches(selected)
                ),
                "selected_running": (
                    selected is not None and selected.state == "running"
                ),
                "visible_present": visible is not None,
                "visible_running": (
                    visible is not None and visible.state == "running"
                ),
                "visible_identity_matches": (
                    selected is not None and visible is not None
                    and _same_identity(selected, visible)
                ),
                "client_identity_matches": (
                    selected is not None and _same_identity(selected, client.identity)
                ),
                "lease_owned": bool(client.lease),
            }
            record_property(
                "copy_target_predicates",
                json.dumps(copy_predicates, sort_keys=True),
            )
            copy_time_observations = []
            copy_observation_scope = ContextVar(
                "jtt_explicit_copy_observation", default=None
            )
            original_observe = client.observe

            def capture_copy_observation():
                observation = original_observe()
                active_capture = copy_observation_scope.get()
                if active_capture is not None:
                    active_capture.append(observation)
                return observation

            monkeypatch.setattr(client, "observe", capture_copy_observation)
            original_copy = app._copy_right_screen

            async def capture_explicit_copy_observation():
                token = copy_observation_scope.set(copy_time_observations)
                try:
                    await original_copy()
                finally:
                    copy_observation_scope.reset(token)

            monkeypatch.setattr(
                app, "_copy_right_screen", capture_explicit_copy_observation
            )
            # Windows Terminal may reserve Ctrl+Shift+C for its host copy
            # command; exercise JTT's explicit copy fallback through its prefix.
            await pilot.press("ctrl+g", "c")
            await _wait_for_ui_state(
                pilot, lambda: bool(copied), "explicit copy chord payload",
            )
            assert len(copy_time_observations) == 1, (
                "explicit copy must use one scoped observation"
            )
            copy_observation = copy_time_observations[0]
            payload = copied[-1]
            copy_evidence = {
                "observation_synced": copy_observation.synced,
                "observation_running": copy_observation.state == "running",
                "observation_has_screen": copy_observation.screen is not None,
                "observation_session_matches": (
                    copy_observation.session_id == identity.session_id
                ),
                "observation_generation_matches": (
                    copy_observation.generation == identity.generation
                ),
                "observation_shell_pid_matches": (
                    copy_observation.shell_pid == identity.shell_pid
                ),
                "observation_sequence_current": (
                    copy_observation.sequence >= ui_observation_before_copy[1]
                ),
                "payload_is_exact_observed_screen": (
                    copy_observation.screen is not None
                    and payload == windows_ui._screen_clipboard_text(
                        copy_observation.screen
                    )
                ),
            }
            record_property(
                "copy_observation_predicates",
                json.dumps(copy_evidence, sort_keys=True),
            )
            assert copy_observation.synced
            assert copy_observation.state == "running"
            assert copy_observation.screen is not None
            assert copy_observation.session_id == identity.session_id
            assert copy_observation.generation == identity.generation
            assert copy_observation.shell_pid == identity.shell_pid
            assert copy_observation.sequence >= ui_observation_before_copy[1]
            observed_copy_text = "".join(
                _plain_screen_text(copy_observation.screen).split()
            )
            assert all(marker in observed_copy_text for marker in expected)
            assert payload == windows_ui._screen_clipboard_text(copy_observation.screen)
            compact_payload = "".join(payload.split())
            assert all(marker in compact_payload for marker in expected)
            assert "\r\n" in payload
            await pilot.press("ctrl+g", "d")

    try:
        asyncio.run(scenario())
    finally:
        current = next((item for item in registry.list_sessions()
                        if item.session_id == identity.session_id), None)
        if current is not None and _same_identity(identity, current):
            registry.stop(
                current.session_id, expected_generation=identity.generation,
                expected_shell_pid=identity.shell_pid,
            )


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
                async def wait_for_confirmation_button():
                    def is_mounted():
                        try:
                            stop_app.screen.query_one("#confirm-stop", Button)
                        except NoMatches:
                            return False
                        return True

                    await _wait_for_ui_state(
                        pilot, is_mounted, "stop confirmation screen mount",
                    )

                async def send_stop_prefix_command():
                    # Textual dispatches the prefix binding through its event
                    # loop. Wait for the observable pending state before the
                    # command key so repeated modal cycles cannot race it.
                    await pilot.press("ctrl+g")
                    await _wait_for_ui_state(
                        pilot, lambda: stop_app.prefix_pending,
                        "stop command prefix activation",
                    )
                    await pilot.press("x")
                    await wait_for_confirmation_button()

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
                await send_stop_prefix_command()
                assert stop_app.screen.query_one("#cancel-stop", Button).has_focus
                await pilot.pause(1.2)
                assert stop_app.screen.query_one("#cancel-stop", Button).has_focus
                await pilot.click("#cancel-stop")
                await _wait_for_ui_state(
                    pilot, lambda: not isinstance(stop_app.screen, ModalScreen),
                    "cancelled stop modal dismissal",
                )
                await assert_still_running(identity)

                # Simulate a stale card identity without changing persisted
                # registry metadata or the owned PowerShell process. Pause
                # periodic reconciliation so it does not correctly clear the
                # deliberately stale selection before the stop confirmation
                # can exercise its own final identity check.
                stop_app._stop_refresh_timers()
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
                await send_stop_prefix_command()
                await pilot.click("#confirm-stop")
                await asyncio.wait_for(mismatch_finished.wait(), timeout=10)
                await _wait_for_ui_state(
                    pilot, lambda: not isinstance(stop_app.screen, ModalScreen),
                    "stale stop modal dismissal",
                )
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
                await send_stop_prefix_command()
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


@pytest.mark.parametrize("size", [(80, 24), (160, 44)], ids=("80x24", "160x44"))
@pytest.mark.skipif(sys.platform != "win32", reason="native ConPTY required")
def test_native_windows_interactive_latency_profile(tmp_path, size):
    """Report repeatable per-key IPC and screen refresh timings on real ConPTY."""
    from ju0_terminal_tower.terminal.windows_manager import TerminalRegistry

    root = tmp_path / "isolated-jtt-latency"
    registry = TerminalRegistry(root=root / "terminals")
    identities = []
    workspace_store = JTTWorkspaceStore(root / "workspace")
    layout_store = JTTLayoutStore(workspace_store.root)
    app = WindowsJTTApp(
        registry, workspace_store=workspace_store, layout_store=layout_store,
    )

    def percentile(values, percent):
        ordered = sorted(values)
        return ordered[min(len(ordered) - 1, (len(ordered) * percent + 99) // 100 - 1)]

    def screen_contains(screen, text):
        # A short overall UI can wrap the echoed token across RIGHT rows. Keep
        # the assertion about visible output while ignoring that line boundary.
        return text in "".join(
            cell.text for row in screen.rows for cell in row
        )

    async def scenario():
        async with app.run_test(size=size) as pilot:
            await _wait_for_ui_state(
                pilot,
                lambda: app.selected_identity is not None and app.client is not None,
                "first native latency session",
            )
            for index, identity in enumerate(identities):
                if app.selected_id != identity.session_id:
                    await app._select_session(identity, focus=False)
                await app.enter_input_mode()
                client = app.client
                assert client is not None and client.lease

                requests = []
                original_request = client._request
                original_send = client.send
                original_render = app._render_terminal
                send_durations = []
                render_durations = []

                def measured_request(operation, **fields):
                    started = time.perf_counter()
                    try:
                        return original_request(operation, **fields)
                    finally:
                        requests.append((operation, (time.perf_counter() - started) * 1000))

                def measured_send(data):
                    started = time.perf_counter()
                    try:
                        return original_send(data)
                    finally:
                        send_durations.append((time.perf_counter() - started) * 1000)

                def measured_render(screen):
                    started = time.perf_counter()
                    try:
                        return original_render(screen)
                    finally:
                        render_durations.append((time.perf_counter() - started) * 1000)

                client._request = measured_request
                client.send = measured_send
                app._render_terminal = measured_render
                token = f"JTTLAT{index}PROFILE"
                key_latencies = []
                screen_started = None
                for offset, character in enumerate(token):
                    started = time.perf_counter()
                    if offset == len(token) - 1:
                        screen_started = started
                    await pilot.press(character)
                    key_latencies.append((time.perf_counter() - started) * 1000)

                await _wait_for_ui_state(
                    pilot,
                    lambda: (app._right_observation is not None
                             and screen_contains(app._right_observation[3], token)),
                    f"{size[0]}x{size[1]} token echoed by session {index + 1}",
                    timeout=10,
                )
                screen_latency = (time.perf_counter() - screen_started) * 1000
                counts = {}
                for operation, _duration in requests:
                    counts[operation] = counts.get(operation, 0) + 1
                assert counts.get("poll", 0) == 0
                assert counts.get("input", 0) == len(token)
                print(
                    "JTT_LATENCY_PROFILE "
                    f"size={size[0]}x{size[1]} terminals=2 session={index + 1} "
                    f"keys={len(token)} rpc={counts} "
                    f"request_ms_p50={percentile([item[1] for item in requests], 50):.2f} "
                    f"send_ms_p50={percentile(send_durations, 50):.2f} "
                    f"render_ms_p50={percentile(render_durations, 50):.2f} "
                    f"render_ms_p95={percentile(render_durations, 95):.2f} "
                    f"key_ms_p50={percentile(key_latencies, 50):.2f} "
                    f"key_ms_p95={percentile(key_latencies, 95):.2f} "
                    f"screen_ms={screen_latency:.2f}",
                    flush=True,
                )
                app._render_terminal = original_render
                client._request = original_request
                client.send = original_send
                await app.leave_input_mode()

    try:
        for name in ("Profile A", "Profile B"):
            client = registry.create(name)
            identity = client.identity
            client.close()
            identities.append(identity)
        workspace_store.set_terminal_ids(
            workspace_store.DEFAULT_ID, [item.session_id for item in identities],
        )
        asyncio.run(scenario())
    finally:
        for identity in identities:
            registry.stop(
                identity.session_id, expected_generation=identity.generation,
                expected_shell_pid=identity.shell_pid,
            )
    assert registry.list_sessions() == ()
