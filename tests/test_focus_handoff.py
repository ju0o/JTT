import curses
from types import SimpleNamespace

import pytest

from tmux_agent_tower.control import actions
from tmux_agent_tower.ui import control_room, live_view


def _row(**updates):
    row = {
        "kind": "pane",
        "key": "%44",
        "pane_id": "%44",
        "pane_pid": "4400",
        "target_id": "target-4400",
        "session": "work",
        "session_id": "$7",
        "window_id": "@13",
        "window_ref": "mainpc/work/@13",
        "window_name": "Coding",
        "tmux_host": "mainpc",
        "execution_host": "mainpc",
        "transport": "local",
        "transport_target": "",
        "remote": False,
        "display_name": "Tower task",
        "agent": "Codex",
        "live_lines": ["safe fixture output"],
    }
    row.update(updates)
    return row


class Screen:
    def __init__(self, height, width, keys=()):
        self.size = (height, width)
        self.keys = iter(keys)
        self.writes = []
        self.timeouts = []

    def getmaxyx(self):
        return self.size

    def erase(self):
        self.writes.clear()

    def addnstr(self, y, x, text, _n, attr=0):
        self.writes.append((y, x, text, attr))

    def timeout(self, value):
        self.timeouts.append(value)

    def keypad(self, _value):
        pass

    def refresh(self):
        pass

    def noutrefresh(self):
        pass

    def get_wch(self):
        return next(self.keys)


def _tower(row=None):
    row = row or _row()
    return SimpleNamespace(
        rows=[row],
        visible_rows=[row],
        selected=0,
        own_pane_id="%1",
        local_host="mainpc",
        work_groups=SimpleNamespace(all=lambda: []),
        start_background_refresh=lambda: False,
    )


def _allowed():
    return actions.FocusTargetValidation(actions.FocusTargetStatus.ALLOWED, "%44", ("full-id",))


def test_handoff_revalidates_before_each_selection_and_never_forwards_keys(monkeypatch):
    row = _row()
    tower = _tower(row)
    checks, steps = [], []
    monkeypatch.setattr(actions, "validate_focus_target", lambda *_args: checks.append("verify") or _allowed())
    monkeypatch.setattr(actions.tmux_navigation, "select_window_for_pane", lambda key: steps.append(("window", key)) or True)
    monkeypatch.setattr(actions.tmux_navigation, "select_pane_for_pane", lambda key: steps.append(("pane", key)) or True)
    monkeypatch.setattr(actions, "focus_local_pane", lambda *_a, **_k: pytest.fail("legacy unchecked focus path used"))
    monkeypatch.setattr(actions, "send_prompt", lambda *_a, **_k: pytest.fail("handoff must not relay keys"))

    result = actions.handoff_focus_target(tower, row)

    assert result.status is actions.FocusHandoffStatus.FOCUSED
    assert checks == ["verify", "verify", "verify"]
    assert steps == [("window", "%44"), ("pane", "%44")]


def test_stale_pre_focus_identity_refuses_without_any_tmux_selection(monkeypatch):
    row = _row()
    selection = []
    validation = actions.FocusTargetValidation(actions.FocusTargetStatus.STALE, "%44")
    monkeypatch.setattr(actions, "validate_focus_target", lambda *_args: validation)
    monkeypatch.setattr(actions.tmux_navigation, "select_window_for_pane", lambda *_a: selection.append("window"))
    monkeypatch.setattr(actions.tmux_navigation, "select_pane_for_pane", lambda *_a: selection.append("pane"))

    result = actions.handoff_focus_target(_tower(row), row)

    assert result.status is actions.FocusHandoffStatus.REFUSED
    assert result.validation.status is actions.FocusTargetStatus.STALE
    assert selection == []


def test_identity_change_after_window_selection_stops_before_pane_selection(monkeypatch):
    row = _row()
    checks = iter((_allowed(), actions.FocusTargetValidation(actions.FocusTargetStatus.STALE, "%44")))
    selection = []
    monkeypatch.setattr(actions, "validate_focus_target", lambda *_args: next(checks))
    monkeypatch.setattr(actions.tmux_navigation, "select_window_for_pane", lambda key: selection.append(("window", key)) or True)
    monkeypatch.setattr(actions.tmux_navigation, "select_pane_for_pane", lambda key: selection.append(("pane", key)) or True)

    result = actions.handoff_focus_target(_tower(row), row)

    assert result.status is actions.FocusHandoffStatus.PARTIAL
    assert result.stage == "after_select_window"
    assert selection == [("window", "%44")]


@pytest.mark.parametrize(
    ("window_result", "pane_result", "expected", "expected_calls"),
    [
        (False, True, actions.FocusHandoffStatus.FAILED, ["window"]),
        (None, True, actions.FocusHandoffStatus.PARTIAL, ["window"]),
        (True, False, actions.FocusHandoffStatus.PARTIAL, ["window", "pane"]),
        (True, None, actions.FocusHandoffStatus.PARTIAL, ["window", "pane"]),
    ],
)
def test_focus_step_failures_stop_without_rollback_or_other_commands(
    monkeypatch, window_result, pane_result, expected, expected_calls,
):
    row = _row()
    checks = []
    calls = []

    def validate(*_args):
        checks.append(True)
        return _allowed()

    monkeypatch.setattr(actions, "validate_focus_target", validate)
    monkeypatch.setattr(
        actions.tmux_navigation,
        "select_window_for_pane",
        lambda _key: calls.append("window") or window_result,
    )
    monkeypatch.setattr(
        actions.tmux_navigation,
        "select_pane_for_pane",
        lambda _key: calls.append("pane") or pane_result,
    )
    monkeypatch.setattr(actions, "focus_local_pane", lambda *_a, **_k: pytest.fail("no rollback focus"))
    monkeypatch.setattr(actions, "send_prompt", lambda *_a, **_k: pytest.fail("no input relay"))

    result = actions.handoff_focus_target(_tower(row), row)

    assert result.status is expected
    assert calls == expected_calls
    assert len(checks) == (1 if window_result is not True else (2 if pane_result is not True else 3))


def test_post_pane_identity_change_reports_partial_without_more_tmux_commands(monkeypatch):
    row = _row()
    checks = iter((_allowed(), _allowed(), actions.FocusTargetValidation(actions.FocusTargetStatus.STALE, "%44")))
    calls = []
    monkeypatch.setattr(actions, "validate_focus_target", lambda *_args: next(checks))
    monkeypatch.setattr(actions.tmux_navigation, "select_window_for_pane", lambda _key: calls.append("window") or True)
    monkeypatch.setattr(actions.tmux_navigation, "select_pane_for_pane", lambda _key: calls.append("pane") or True)

    result = actions.handoff_focus_target(_tower(row), row)

    assert result.status is actions.FocusHandoffStatus.PARTIAL
    assert result.stage == "after_select_pane"
    assert calls == ["window", "pane"]


def test_remote_and_self_rows_never_get_an_actionable_handoff_menu(monkeypatch):
    remote = _row(remote=True, tmux_host="asus", transport="remote_tmux", transport_target="asus")
    tower = _tower(remote)
    items = control_room.action_menu_items(
        remote, direct_control_available=control_room._focus_handoff_menu_available(tower, remote),
    )
    assert "agent_handoff" not in [key for key, _label in items]
    assert "agent_handoff_unavailable" in [key for key, _label in items]

    self_row = _row(key="%1", pane_id="%1")
    tower = _tower(self_row)
    items = control_room.action_menu_items(
        self_row, direct_control_available=control_room._focus_handoff_menu_available(tower, self_row),
    )
    assert "agent_handoff" not in [key for key, _label in items]


def test_explicit_confirmation_cancel_does_not_validate_or_select(monkeypatch):
    row = _row()
    tower = _tower(row)
    picks = iter((
        SimpleNamespace(cancelled=False, selected_key="agent_handoff"),
        SimpleNamespace(cancelled=False, selected_key="cancel"),
    ))
    monkeypatch.setattr(control_room, "run_list_picker", lambda *_a, **_k: next(picks))
    monkeypatch.setattr(
        control_room.control_actions,
        "handoff_focus_target",
        lambda *_a, **_k: pytest.fail("cancel must happen before identity or focus calls"),
    )

    result = control_room._open_action_menu(Screen(24, 80), tower, row, [], None, "pane")

    assert result is None


def test_handoff_confirmation_defaults_to_cancel_on_wide_dialog(monkeypatch):
    row = _row()
    tower = _tower(row)
    screen = Screen(44, 160, ["\n"])
    monkeypatch.setattr(
        control_room.control_actions,
        "handoff_focus_target",
        lambda *_args: pytest.fail("Enter on the default Cancel choice must not hand off"),
    )

    assert control_room._request_agent_handoff(screen, tower, row) is None
    frame = "\n".join(text for _y, _x, text, _attr in screen.writes)
    assert "+" in frame
    assert "취소" in frame
    assert "조작 화면으로 이동" in frame


def test_limited_peer_menu_explains_unavailable_without_confirmation(monkeypatch):
    remote = _row(remote=True, tmux_host="asus", execution_host="asus",
                  transport="remote_tmux", transport_target="asus")
    tower = _tower(remote)
    notices = []
    monkeypatch.setattr(
        control_room, "run_list_picker",
        lambda *_a, _title=None, _items=None, **_kw: SimpleNamespace(
            cancelled=False, selected_key="agent_handoff_unavailable",
        ),
    )
    monkeypatch.setattr(control_room, "show_message_screen", lambda _s, _title, lines: notices.extend(lines))
    monkeypatch.setattr(
        control_room.control_actions,
        "handoff_focus_target",
        lambda *_a: pytest.fail("remote peer must not reach focus validation"),
    )

    control_room._open_action_menu(Screen(24, 80), tower, remote, [], None, "pane")

    assert notices == [control_room.t("control_room.handoff_unavailable")]


def test_confirmed_menu_action_calls_verified_handoff_only_after_confirm(monkeypatch):
    row = _row(transport="ssh", transport_target="asus", execution_host="asus")
    tower = _tower(row)
    picks = iter((
        SimpleNamespace(cancelled=False, selected_key="agent_handoff"),
        SimpleNamespace(cancelled=False, selected_key="confirm"),
    ))
    seen = []
    monkeypatch.setattr(control_room, "run_list_picker", lambda *_a, **_k: next(picks))
    monkeypatch.setattr(
        control_room.control_actions,
        "handoff_focus_target",
        lambda selected_tower, selected_row: seen.append((selected_tower, selected_row))
        or SimpleNamespace(
            status=actions.FocusHandoffStatus.FOCUSED,
            validation=_allowed(), stage="complete",
        ),
    )

    result = control_room._open_action_menu(Screen(24, 80), tower, row, [], None, "pane")

    assert result is actions.FocusHandoffStatus.FOCUSED
    assert seen == [(tower, row)]


@pytest.mark.parametrize("height,width", [(24, 80), (40, 120)])
def test_handoff_confirmation_target_and_return_guidance_fit_fixture_screens(height, width, monkeypatch):
    row = _row(transport="ssh", transport_target="asus", execution_host="asus")
    screen = Screen(height, width, [curses.KEY_DOWN, "\n"])
    tower = _tower(row)

    monkeypatch.setattr(
        control_room.control_actions,
        "handoff_focus_target",
        lambda *_args: SimpleNamespace(
            status=actions.FocusHandoffStatus.FOCUSED,
            validation=_allowed(), stage="complete",
        ),
    )
    assert control_room._request_agent_handoff(screen, tower, row) is actions.FocusHandoffStatus.FOCUSED

    frame = "\n".join(text for _y, _x, text, _attr in screen.writes)
    assert "Tower task" in frame
    assert "Codex" in frame
    assert "asus" in frame
    assert "Coding" in frame
    assert "이동" in frame or "Move" in frame
    assert "tmux" in frame


def test_control_room_remains_running_after_handoff_and_keeps_observation_status(monkeypatch):
    row = _row()
    tower = _tower(row)
    screen = Screen(24, 80, [" ", "q"])
    picks = iter((
        SimpleNamespace(cancelled=False, selected_key="agent_handoff"),
        SimpleNamespace(cancelled=False, selected_key="confirm"),
    ))
    calls = []
    monkeypatch.setattr(live_view.curses, "doupdate", lambda: None)
    monkeypatch.setattr(control_room.live_view, "_start_refresh", lambda _tower: False)
    monkeypatch.setattr(control_room, "run_list_picker", lambda *_a, **_kw: next(picks))
    monkeypatch.setattr(
        control_room.control_actions,
        "handoff_focus_target",
        lambda *_args: calls.append("handoff") or SimpleNamespace(
            status=actions.FocusHandoffStatus.FOCUSED,
            validation=_allowed(), stage="complete",
        ),
    )

    control_room.open_control_room(screen, tower)

    frame = "\n".join(text for _y, _x, text, _attr in screen.writes)
    assert calls == ["handoff"]
    assert "관찰 중" in frame
    assert tower.rows == [row]
    assert screen.timeouts[-1] == 200


def test_tmux_handoff_steps_are_targeted_select_only_commands(monkeypatch):
    calls = []

    def run(args, **kwargs):
        calls.append((list(args), kwargs))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(actions.tmux_navigation.subprocess, "run", run)

    assert actions.tmux_navigation.select_window_for_pane("%44") is True
    assert actions.tmux_navigation.select_pane_for_pane("%44") is True
    assert actions.tmux_navigation.select_pane_for_pane("not-a-pane") is False

    assert [call[0] for call in calls] == [
        ["tmux", "select-window", "-t", "%44"],
        ["tmux", "select-pane", "-t", "%44"],
    ]
    assert all(call[1]["stdin"] is actions.tmux_navigation.subprocess.DEVNULL for call in calls)
    assert not any("send-keys" in call[0] or "paste-buffer" in call[0] or "kill-pane" in call[0]
                   for call in calls)
