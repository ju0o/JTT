"""Pane control stays inside Tower. Focus is G, and both UIs share actions."""

import inspect

import pytest

from tmux_agent_tower.control import actions
from tmux_agent_tower.server import httpapi
from tmux_agent_tower.ui import control_view, tower as tower_module


class _RecordingScreen:
    def __init__(self, height, width):
        self.height, self.width = height, width
        self.drawn = {}

    def erase(self):
        self.drawn.clear()

    def getmaxyx(self):
        return self.height, self.width

    def addnstr(self, y, x, text, _count, _attr=0):
        self.drawn[y] = (x, text)

    def noutrefresh(self):
        pass


def test_enter_opens_control_and_does_not_focus(tmp_path, monkeypatch):
    monkeypatch.setattr(tower_module, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(tower_module, "CONFIG_DIR", tmp_path / "config")
    monkeypatch.setattr(tower_module, "HOST_FILE", tmp_path / "config" / "host")
    monkeypatch.setattr(tower_module, "REMOTE_HOSTS_FILE", tmp_path / "config" / "remote-hosts.txt")
    moved = []
    monkeypatch.setattr(
        "tmux_agent_tower.control.actions.focus_local_pane",
        lambda *args, **kwargs: moved.append(args) or True,
    )

    tower = tower_module.Tower("0", own_pane_id="%6")
    tower.visible_rows = [
        {"kind": "pane", "key": "%44", "pane_id": "%44", "remote": False, "session": "0", "offline": False}
    ]
    tower.selected = 0
    assert actions.enter_intent(tower.visible_rows[0]) == "control"
    assert tower.control_key() == "%44"
    assert moved == []

    # V mode: Enter opens Window Control and does not move the client.
    tower.visible_rows = [
        {
            "kind": "window",
            "key": "@2",
            "window_id": "@2",
            "session": "0",
            "window_index": "1",
            "remote": False,
        }
    ]
    tower.selected = 0
    assert actions.enter_intent(tower.visible_rows[0]) == "window"
    assert tower.control_key() == "@2"
    assert moved == []


def _run_tower_with_one_key(monkeypatch, tower, key):
    from tmux_agent_tower.server import service

    keys = iter((key,))

    class Screen:
        def timeout(self, _milliseconds):
            pass

    def read(_screen):
        try:
            return next(keys)
        except StopIteration:
            raise KeyboardInterrupt

    monkeypatch.setattr(tower_module, "Tower", lambda *_a, **_k: tower)
    monkeypatch.setattr(tower_module, "draw", lambda *_a, **_k: None)
    monkeypatch.setattr(tower_module, "_remote_state", lambda: None)
    monkeypatch.setattr(tower_module, "_apply_background_refresh", lambda *_a: False)
    monkeypatch.setattr(service, "maybe_autostart", lambda **_k: None)
    monkeypatch.setattr(tower_module, "read_key", read)

    tower_module._run_loop(Screen(), "isolated-test", "")


def test_structure_y_explains_non_pane_without_calling_result_copy(monkeypatch):
    from types import SimpleNamespace
    from tmux_agent_tower.ui import widgets

    tower = SimpleNamespace(
        session="isolated-test", own_pane_id="", visible_rows=[{"kind": "window", "key": "@7"}],
        rows=[], selected=0, load=lambda: None,
    )
    shown = []
    monkeypatch.setattr(control_view, "_copy_result", lambda *_a, **_k: pytest.fail("non-pane Y must not copy"))
    monkeypatch.setattr(widgets, "show_message_screen", lambda _s, title, lines: shown.append((title, lines)))

    _run_tower_with_one_key(monkeypatch, tower, "y")

    assert shown == [(tower_module.t("nav.copy_result"), [tower_module.t("control.copy_requires_pane")])]


def test_structure_y_shows_copy_diagnostic_and_leaves_it_in_menu_state(monkeypatch):
    from types import SimpleNamespace
    from tmux_agent_tower.ui import widgets

    row = {
        "kind": "pane", "key": "%44", "pane_id": "%44", "pane_pid": "4400",
        "session": "isolated-test", "result_provider_type": "local_tmux", "result_state": "ready",
    }
    tower = SimpleNamespace(
        session="isolated-test", own_pane_id="", visible_rows=[row], rows=[row], selected=0,
        load=lambda: None,
    )
    copied, shown = [], []

    def copy_result(selected_tower, pane_key, _screen):
        copied.append(pane_key)
        selected_tower.last_result_copy_diagnostic = {
            "pane_key": "%44", "pane_id": "%44", "pane_pid": "4400", "session": "isolated-test",
            "stage": "not_entered", "outcome": "blocked", "reason_code": "SOURCE_PARTIAL",
            "turn_complete": True, "body_complete": False, "result_state": "ready",
        }
        return "결과 복사 거부 [SOURCE_PARTIAL]"

    monkeypatch.setattr(control_view, "_copy_result", copy_result)
    monkeypatch.setattr(widgets, "show_message_screen", lambda _s, title, lines: shown.append((title, lines)))

    _run_tower_with_one_key(monkeypatch, tower, "y")

    assert copied == ["%44"]
    assert shown == [(
        tower_module.t("detail.result"),
        [
            "결과 복사 거부 [SOURCE_PARTIAL]",
            tower_module.t("diagnostics.clipboard").format(result="not_entered / blocked"),
            tower_module.t("diagnostics.reason").format(code="SOURCE_PARTIAL"),
        ],
    )]
    diagnostic_lines = control_view.pane_diagnostic_lines(tower, row)
    assert "not_entered / blocked" in "\n".join(diagnostic_lines)
    assert "SOURCE_PARTIAL" in "\n".join(diagnostic_lines)


def test_selected_pane_diagnostics_show_metadata_without_result_text(monkeypatch):
    from types import SimpleNamespace

    row = {
        "key": "%44", "pane_id": "%44", "pane_pid": "4400", "session": "isolated",
        "tmux_host": "test-host", "execution_host": "test-host", "transport": "local",
        "auto_detected_agent": "Codex", "result_adapter": "Codex",
        "result_provider_type": "local_tmux", "result_state": "none",
    }
    diagnostic = {
        "pane_key": "%44", "pane_id": "%44", "pane_pid": "4400", "session": "isolated",
        "stage": "not_entered", "outcome": "blocked", "provider_type": "local_tmux",
        "result_state": "none", "turn_complete": True, "body_complete": False,
        "reason_code": "SOURCE_PARTIAL",
    }
    tower = SimpleNamespace(
        rows=[row], last_result_copy_diagnostic=diagnostic,
        results=SimpleNamespace(observe=lambda *_a, **_k: pytest.fail("diagnostics mutated ResultTracker")),
        load=lambda: pytest.fail("diagnostics must not refresh or mutate Tower state"),
    )
    row_before = dict(row)
    diagnostic_before = dict(diagnostic)
    monkeypatch.setattr(
        control_view, "get_result",
        lambda *_args, **_kwargs: pytest.fail("diagnostics must not extract a Result"),
    )

    lines = control_view.pane_diagnostic_lines(tower, row)

    rendered = "\n".join(lines)
    assert "%44" in rendered and "4400" in rendered
    assert "Codex" in rendered and "local_tmux" in rendered
    assert "SOURCE_PARTIAL" in rendered
    assert "PRIVATE ANSWER BODY" not in rendered
    assert "not_entered / blocked" in rendered
    assert row == row_before
    assert tower.last_result_copy_diagnostic == diagnostic_before


def test_selected_pane_diagnostics_refuse_reused_pane_identity(monkeypatch):
    from types import SimpleNamespace

    selected = {"key": "%44", "pane_id": "%44", "pane_pid": "old-pid", "session": "isolated"}
    tower = SimpleNamespace(
        rows=[{**selected, "pane_pid": "new-pid"}],
        load=lambda: None,
    )
    monkeypatch.setattr(control_view, "get_result", lambda *_a, **_k: pytest.fail("stale pane read"))

    assert control_view.pane_diagnostic_lines(tower, selected) == [control_view.t("diagnostics.stale")]

def test_g_focuses_the_exact_pane_id(monkeypatch):
    from types import SimpleNamespace

    calls = []

    def fake(args, capture=True, timeout=3.0):
        calls.append(list(args))
        if args[:2] == ["list-panes", "-s"]:
            return "%44"
        return ""

    monkeypatch.setattr(actions.tmux_capture, "run_tmux", fake)
    monkeypatch.setattr(actions.tmux_capture, "pane_exists", lambda pane_id: pane_id == "%44")
    monkeypatch.setattr(
        "tmux_agent_tower.tmux.navigation.subprocess.run",
        lambda args, **_kwargs: calls.append(args[1:]) or SimpleNamespace(returncode=0),
    )
    ok, reason = actions.focus_pane("0", "%44", own_pane_id="%6")
    assert ok is True and reason == ""
    assert ["select-window", "-t", "%44"] in calls
    assert ["select-pane", "-t", "%44"] in calls
    assert not any(call and call[0] == "send-keys" for call in calls)


def test_g_reports_tmux_selection_failure(monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr(actions.tmux_capture, "run_tmux", lambda args, **_kwargs: "%44")
    monkeypatch.setattr(actions.tmux_capture, "pane_exists", lambda _pane_id: True)
    monkeypatch.setattr(
        "tmux_agent_tower.tmux.navigation.subprocess.run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=1),
    )
    assert actions.focus_pane("0", "%44")[0] is False


def test_live_capture_checks_expected_identity_before_and_after_read(monkeypatch):
    calls = []

    def fake_tmux(args, capture=True, timeout=3.0):
        calls.append(list(args))
        if args[:2] == ["list-panes", "-s"] and args[-1] == "#{pane_id}":
            return "%44"
        if args[:2] == ["list-panes", "-s"]:
            return "%44\t123\t0"
        raise AssertionError(f"unexpected tmux command: {args}")

    captured = []
    monkeypatch.setattr(actions.tmux_capture, "run_tmux", fake_tmux)
    monkeypatch.setattr(actions.tmux_capture, "pane_exists", lambda pane_id: pane_id == "%44")
    monkeypatch.setattr(actions.tmux_capture, "capture_pane", lambda pane_id, lines: captured.append((pane_id, lines)) or ["fixture output"])

    ok, reason, payload = actions.get_pane_screen("isolated", "%44", expected_pane_pid="123")

    assert ok and reason == ""
    assert payload["lines"] == ["fixture output"]
    assert captured == [("%44", actions.MAX_SCREEN_LINES + 1)]
    assert len([call for call in calls if call[0] == "list-panes"]) == 3
    assert all(call[0] in {"list-panes"} for call in calls)


def test_screen_capture_marks_earlier_output_as_omitted(monkeypatch):
    monkeypatch.setattr(
        actions.tmux_capture,
        "run_tmux",
        lambda args, capture=True, timeout=3.0: "%44" if args[-1] == "#{pane_id}" else "%44\t123\t0",
    )
    monkeypatch.setattr(actions.tmux_capture, "pane_exists", lambda _pane_id: True)
    captured = []
    monkeypatch.setattr(
        actions.tmux_capture,
        "capture_pane",
        lambda pane_id, lines: captured.append((pane_id, lines)) or [f"line {i}" for i in range(lines)],
    )

    ok, reason, payload = actions.get_pane_screen("isolated", "%44", expected_pane_pid="123")

    assert ok and reason == ""
    assert captured == [("%44", actions.MAX_SCREEN_LINES + 1)]
    assert len(payload["lines"]) == actions.MAX_SCREEN_LINES
    assert payload["truncated"] is True


def test_live_capture_rejects_a_reused_or_dead_pane_before_capture(monkeypatch):
    monkeypatch.setattr(
        actions.tmux_capture,
        "run_tmux",
        lambda args, capture=True, timeout=3.0: "%44\t999\t0" if args[-1] != "#{pane_id}" else "%44",
    )
    monkeypatch.setattr(actions.tmux_capture, "pane_exists", lambda _pane_id: True)
    monkeypatch.setattr(
        actions.tmux_capture,
        "capture_pane",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("stale output was captured")),
    )

    assert actions.get_pane_screen("isolated", "%44", expected_pane_pid="123") == (False, "stale", {})


def test_live_capture_discards_output_if_identity_changes_during_read(monkeypatch):
    identity_reads = iter(("%44\t123\t0", "%44\t999\t0"))

    def fake_tmux(args, capture=True, timeout=3.0):
        if args[-1] == "#{pane_id}":
            return "%44"
        return next(identity_reads)

    monkeypatch.setattr(actions.tmux_capture, "run_tmux", fake_tmux)
    monkeypatch.setattr(actions.tmux_capture, "pane_exists", lambda _pane_id: True)
    monkeypatch.setattr(actions.tmux_capture, "capture_pane", lambda *_args, **_kwargs: ["possibly stale text"])

    assert actions.get_pane_screen("isolated", "%44", expected_pane_pid="123") == (False, "stale", {})


def test_close_kills_only_that_pane_id(monkeypatch):
    calls = []

    def fake_run(argv, **kwargs):
        assert kwargs.get("shell", False) is False
        calls.append(list(argv))

        class Result:
            returncode = 0

        return Result()

    monkeypatch.setattr(actions.subprocess, "run", fake_run)
    monkeypatch.setattr(actions.tmux_capture, "run_tmux", lambda args, capture=True, timeout=3.0: "%51\n%6")
    monkeypatch.setattr(actions.tmux_capture, "pane_exists", lambda pane_id: True)
    ok, reason = actions.close_pane("0", "%51", own_pane_id="%6")
    assert (ok, reason) == (True, "")
    assert calls == [["tmux", "kill-pane", "-t", "%51"]]


def test_close_protects_tower_and_refuses_stale(monkeypatch):
    calls = []
    monkeypatch.setattr(actions.subprocess, "run", lambda *args, **kwargs: calls.append(args))
    monkeypatch.setattr(actions.tmux_capture, "run_tmux", lambda args, capture=True, timeout=3.0: "")
    monkeypatch.setattr(actions.tmux_capture, "pane_exists", lambda pane_id: False)

    assert actions.close_pane("0", "%6", own_pane_id="%6") == (False, "tower_pane")
    assert actions.close_pane("0", "%999", own_pane_id="%6")[0] is False
    assert calls == []


def test_close_refuses_the_last_pane_in_the_session(monkeypatch):
    calls = []
    monkeypatch.setattr(actions.subprocess, "run", lambda *args, **kwargs: calls.append(args))
    monkeypatch.setattr(actions.tmux_capture, "run_tmux", lambda args, capture=True, timeout=3.0: "%51")
    monkeypatch.setattr(actions.tmux_capture, "pane_exists", lambda pane_id: True)
    assert actions.close_pane("0", "%51", own_pane_id="%6") == (False, "last_pane")
    assert calls == []


def test_close_confirmation_names_working_and_approval():
    lines = actions.close_notice_lines("WORKING", "approval_required")
    assert any("진행" in line or "running" in line for line in lines)
    assert any("승인" in line or "approval" in line for line in lines)
    idle = actions.close_notice_lines("IDLE", "input_required")
    assert any("입력" in line or "input" in line for line in idle)


def test_close_confirmation_defaults_to_cancel_and_warns_when_working():
    assert actions.close_choices()[0][0] == "cancel"
    working = actions.close_warning("WORKING")
    waiting = actions.close_warning("WAITING")
    idle = actions.close_warning("IDLE")
    assert working != idle
    assert waiting != idle
    assert "running" in working or "진행" in working
    assert "waiting" in waiting or "답변" in waiting


def test_live_prompt_and_identity_are_the_shared_actions():
    screen = inspect.getsource(httpapi.TowerRemoteHandler._get_screen)
    prompt = inspect.getsource(httpapi.TowerRemoteHandler.do_POST)
    focus = inspect.getsource(httpapi.TowerRemoteHandler._post_focus)
    assert "get_pane_screen" in screen
    assert "send_prompt" in prompt
    assert "focus_pane" in focus
    assert "get_pane_screen" in inspect.getsource(control_view._live_lines)
    detail = inspect.getsource(control_view.open_control_view)
    assert "_submit_composer" in detail
    assert "send_prompt" in inspect.getsource(control_view._submit_composer)
    assert "Composer" in detail
    assert "focus_pane" in inspect.getsource(control_view._go)
    assert "update_identity" in inspect.getsource(tower_module.Tower.edit_selected)
    assert "close_pane" in inspect.getsource(control_view._close)


def test_control_footer_wraps_korean_shortcuts_on_narrow_terminal(monkeypatch):
    from types import SimpleNamespace
    from tmux_agent_tower.ui.render import display_width

    monkeypatch.setattr(control_view.curses, "doupdate", lambda: None)
    monkeypatch.setattr(control_view, "_live_lines", lambda *_args: [f"synthetic live line {i}" for i in range(20)])
    screen = _RecordingScreen(20, 45)
    row = {
        "key": "asus:%1", "remote": True, "status": "IDLE", "agent": "OpenCode",
        "role": "builder", "execution_host": "asus", "host": "asus",
    }

    control_view._draw(screen, SimpleNamespace(config={}), row, "", False)

    action_rows = [screen.drawn[index][1] for index in (18, 19)]
    assert "Ctrl+Y 결과 복사" in " ".join(action_rows)
    assert "Ctrl+S 결과 보기" in " ".join(action_rows)
    assert "Esc 뒤로" in " ".join(action_rows)
    assert all(display_width(line) <= 42 for line in action_rows)
    compact_rows = control_view._action_hint_lines(row, 22)
    assert "Ctrl+Y 복사" in " ".join(compact_rows)
    assert all(display_width(line) <= 18 for line in compact_rows)
    remote_hint = screen.drawn[17][1]
    assert remote_hint.startswith("다른 컴퓨터의 작업은") and remote_hint.endswith("…")
    assert screen.drawn[16][1] == "synthetic live line 19"


def test_control_notice_truncates_by_display_width(monkeypatch):
    from types import SimpleNamespace
    from tmux_agent_tower.ui.render import display_width

    monkeypatch.setattr(control_view.curses, "doupdate", lambda: None)
    monkeypatch.setattr(control_view, "_live_lines", lambda *_args: ["synthetic live line"])
    screen = _RecordingScreen(20, 45)
    row = {
        "key": "%1", "remote": True, "status": "IDLE", "agent": "OpenCode",
        "role": "builder", "execution_host": "asus", "host": "asus",
    }
    notice = "클립보드 전달 실패를 확인하고 다시 시도하기 전에 상태를 점검해 주세요"

    control_view._draw(screen, SimpleNamespace(config={}), row, notice, False)

    shown = screen.drawn[19][1]
    assert shown.endswith("…")
    assert display_width(shown) <= 42
