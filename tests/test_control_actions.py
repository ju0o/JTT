"""Pane control stays inside Tower. Focus is G, and both UIs share actions."""

import inspect

from tmux_agent_tower.control import actions
from tmux_agent_tower.server import httpapi
from tmux_agent_tower.ui import control_view, tower as tower_module


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
    assert captured == [("%44", actions.MAX_SCREEN_LINES)]
    assert len([call for call in calls if call[0] == "list-panes"]) == 3
    assert all(call[0] in {"list-panes"} for call in calls)


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
