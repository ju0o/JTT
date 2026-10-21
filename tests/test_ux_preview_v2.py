import curses
import time
from types import SimpleNamespace

import pytest

from tmux_agent_tower.ui import ux_preview, ux_preview_v2


class _Screen:
    def __init__(self, height=44, width=160, keys=()):
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

    def noutrefresh(self):
        pass

    def keypad(self, _enabled):
        pass

    def timeout(self, value):
        self.timeouts.append(value)

    def get_wch(self):
        return next(self.keys)


@pytest.mark.parametrize("count", [1, 2, 3, 4])
def test_card_grid_layouts_match_requested_pane_counts(count):
    cards = ux_preview_v2.card_rectangles(count, (10, 4, 60, 28))
    assert [card[0] for card in cards] == list(range(count))
    if count == 1:
        assert cards[0][1:] == (10, 4, 60, 28)
    elif count == 2:
        assert cards[0][2] == cards[1][2]
        assert cards[0][1] < cards[1][1]
    elif count == 3:
        assert cards[2][1] == cards[0][1]
        assert cards[2][3] == 60
        assert cards[2][2] > cards[0][2]
    else:
        assert cards[0][1] == cards[2][1]
        assert cards[1][1] == cards[3][1]
        assert cards[2][2] > cards[0][2]


def test_more_than_four_panes_scroll_as_two_column_cards():
    cards = ux_preview_v2.card_rectangles(9, (4, 3, 28, 15), scroll_row=1)
    assert [card[0] for card in cards] == [2, 3, 4, 5]
    assert cards[0][1] < cards[1][1]


def test_managed_terminal_card_draws_clipped_multiline_cell_snapshot():
    from tmux_agent_tower.terminal import TerminalCell, TerminalScreen

    screen = _Screen(height=24, width=80)
    row = {
        "terminal_kind": "tower_managed",
        "display_name": "Mini PTY",
        "agent": "Generic Terminal",
        "terminal_view_state": "running_ready",
        "mini_screen": TerminalScreen((
            (TerminalCell("한", "red", "default", True, False, False,
                          False, False, False),
             TerminalCell("A", "default", "default", False, False, False,
                          False, False, False)),
            (TerminalCell("SECOND", "default", "default", False, False, False,
                          False, False, False),),
        ), 0, 0, True),
    }
    ux_preview_v2._draw_card(screen, (0, 0, 0, 16, 7), row, True, unicode=True)

    writes = [(x, text) for _y, x, text, _attr in screen.writes]
    assert any("SECOND" in text for _x, text in writes)
    assert any(x == 2 and text == "한" for x, text in writes)
    assert any(x == 4 and text == "A" for x, text in writes)


def test_wide_panel_regions_and_narrow_tabbed_fallback():
    wide_120, is_wide_120 = ux_preview_v2._regions(120, 40, 0)
    wide, is_wide = ux_preview_v2._regions(160, 44, 0)
    narrow, narrow_is_wide = ux_preview_v2._regions(80, 24, 2)
    assert is_wide_120 is True and set(wide_120) == {0, 1, 2}
    assert is_wide is True and set(wide) == {0, 1, 2}
    assert narrow_is_wide is False and set(narrow) == {2}


def test_wide_preview_draws_block_navigation_cards_and_single_live(monkeypatch):
    tower = ux_preview.make_preview_tower()
    state = ux_preview_v2._state(tower)
    screen = _Screen()
    monkeypatch.setattr(ux_preview_v2.curses, "doupdate", lambda: None)
    ux_preview_v2.draw_preview_v2(screen, state)
    frame = " ".join(text for _y, _x, text, _attr in screen.writes)
    assert "내 작업공간" in frame
    assert "Windows" in frame
    assert "Window 1 · #0" in frame and "SSH Client 예시 · #0" in frame
    assert "MainPC" not in frame and "ASUS" not in frame
    assert "원격 연결" not in frame  # default fixture has no registered remote profile
    assert "저장한 작업" in frame
    assert "AI 개발팀" in frame and "코드 리뷰" in frame and "서버 관리" in frame
    assert "구성 저장" in frame and "설정 · 메뉴" in frame
    assert "Pane 배치" in frame
    assert "LIVE" in frame
    assert "배치 편집" in frame and "Pane 추가" in frame
    assert "프로젝트 불러오기" not in frame
    assert "[폴더] 연결 구분 예시" in frame
    assert "FIXTURE ONLY" not in frame
    assert "실제 사용자 작업이나 Agent 대화가 아닙니다." not in frame


def test_jtt_panels_and_cards_use_unicode_borders_while_legacy_stays_ascii(monkeypatch):
    monkeypatch.setattr(ux_preview_v2.curses, "doupdate", lambda: None)
    tower = ux_preview.make_preview_tower()
    jtt_state = ux_preview_v2._state(tower)
    jtt_state["runtime_mode"] = "jtt"
    jtt_screen = _Screen()
    ux_preview_v2.draw_preview_v2(jtt_screen, jtt_state)
    jtt_frame = " ".join(text for _y, _x, text, _attr in jtt_screen.writes)
    assert all(glyph in jtt_frame for glyph in ("┌", "─", "┐", "│", "└", "┘"))

    legacy_state = ux_preview_v2._state(tower)
    legacy_state["runtime_mode"] = "tower"
    legacy_screen = _Screen()
    ux_preview_v2.draw_preview_v2(legacy_screen, legacy_state)
    legacy_frame = " ".join(text for _y, _x, text, _attr in legacy_screen.writes)
    assert "+" in legacy_frame and "-" in legacy_frame
    assert not any(glyph in legacy_frame for glyph in ("┌", "─", "┐", "│", "└", "┘"))


def test_jtt_branding_footer_and_narrow_compact_layout(monkeypatch):
    monkeypatch.setattr(ux_preview_v2.curses, "doupdate", lambda: None)
    tower = ux_preview.make_preview_tower()
    wide_state = ux_preview_v2._state(tower)
    wide_state.update(runtime_mode="jtt", jtt_notice="", jtt_prefix_pending=False)
    wide_screen = _Screen(44, 160)
    ux_preview_v2.draw_preview_v2(wide_screen, wide_state)
    wide_frame = " ".join(text for _y, _x, text, _attr in wide_screen.writes)
    assert "JTT — Ju0 Terminal Tower" in wide_frame
    assert "Ctrl+G 명령(4초)" in wide_frame
    assert "Ctrl+G x 종료" in wide_frame
    assert "Ctrl+G d 분리" in wide_frame
    assert "Q UI 분리" not in wide_frame

    narrow_state = ux_preview_v2._state(tower)
    narrow_state.update(runtime_mode="jtt", active_panel=2, jtt_notice="", jtt_prefix_pending=True,
                        jtt_prefix_deadline=time.monotonic() + 4.0)
    narrow_screen = _Screen(24, 80)
    ux_preview_v2.draw_preview_v2(narrow_screen, narrow_state)
    narrow_frame = " ".join(text for _y, _x, text, _attr in narrow_screen.writes)
    assert "JTT" in narrow_frame
    assert "Ctrl+G 4초" in narrow_frame
    assert "x 종료" in narrow_frame  # Prefix is explicitly active in this footer.
    assert all(0 <= y < 24 and 0 <= x < 80 for y, x, _text, _attr in narrow_screen.writes)

    narrow_state["jtt_prefix_pending"] = False
    ux_preview_v2.draw_preview_v2(narrow_screen, narrow_state)
    compact_frame = " ".join(text for _y, _x, text, _attr in narrow_screen.writes)
    assert "Ctrl+G x 종료" in compact_frame
    assert "Q 분리" not in compact_frame


@pytest.mark.parametrize("runtime_mode", ["fixture", "tower"])
def test_legacy_and_fixture_navigation_quit_keys_keep_existing_exit_behavior(runtime_mode):
    state = ux_preview_v2._state(ux_preview.make_preview_tower())
    state["runtime_mode"] = runtime_mode
    screen = _Screen()

    for key in ("q", "Q", "\x1b"):
        assert ux_preview_v2._handle_key(screen, state, key) is True


def test_jtt_empty_workspace_uses_terminal_wording(monkeypatch):
    monkeypatch.setattr(ux_preview_v2.curses, "doupdate", lambda: None)
    state = ux_preview_v2._state(ux_preview.make_preview_tower())
    state.update(runtime_mode="jtt", active_panel=1, panes=[])
    screen = _Screen(44, 160)
    ux_preview_v2.draw_preview_v2(screen, state)
    frame = " ".join(text for _y, _x, text, _attr in screen.writes)
    assert "이 작업공간에 터미널 없음" in frame
    assert "Pane 없음" not in frame


@pytest.mark.parametrize("width", [100, 109])
def test_jtt_narrow_layout_widths_keep_compact_stop_footer(monkeypatch, width):
    monkeypatch.setattr(ux_preview_v2.curses, "doupdate", lambda: None)
    state = ux_preview_v2._state(ux_preview.make_preview_tower())
    state.update(runtime_mode="jtt", active_panel=2, jtt_notice="", jtt_prefix_pending=False)
    screen = _Screen(24, width)

    ux_preview_v2.draw_preview_v2(screen, state)

    frame = " ".join(text for _y, _x, text, _attr in screen.writes)
    assert "Ctrl+G(4초)" in frame
    assert "Ctrl+G x 종료" in frame
    assert all(0 <= y < 24 and 0 <= x < width for y, x, _text, _attr in screen.writes)


@pytest.mark.parametrize("width", [100, 109])
def test_jtt_narrow_prefix_and_notice_footer_states_are_preserved(monkeypatch, width):
    monkeypatch.setattr(ux_preview_v2.curses, "doupdate", lambda: None)
    state = ux_preview_v2._state(ux_preview.make_preview_tower())
    state.update(runtime_mode="jtt", active_panel=2, jtt_notice="", jtt_prefix_pending=True,
                 jtt_prefix_deadline=time.monotonic() + 4.0)
    screen = _Screen(24, width)

    ux_preview_v2.draw_preview_v2(screen, state)
    prefix_frame = " ".join(text for _y, _x, text, _attr in screen.writes)
    assert "Ctrl+G 4초" in prefix_frame
    assert "x 종료" in prefix_frame

    state.update(jtt_prefix_pending=False, jtt_notice="대상 확인 필요", jtt_notice_until=0.0)
    ux_preview_v2.draw_preview_v2(screen, state)
    notice_frame = " ".join(text for _y, _x, text, _attr in screen.writes)
    assert "JTT — Ju0 Terminal Tower · 대상 확인 필요" in notice_frame


@pytest.mark.parametrize("active_panel", [0, 1, 2])
def test_narrow_preview_draws_one_tabbed_panel_without_clipping(monkeypatch, active_panel):
    tower = ux_preview.make_preview_tower()
    state = ux_preview_v2._state(tower)
    state["active_panel"] = active_panel
    screen = _Screen(24, 80)
    monkeypatch.setattr(ux_preview_v2.curses, "doupdate", lambda: None)
    ux_preview_v2.draw_preview_v2(screen, state)
    assert screen.writes
    assert all(0 <= y < 24 and 0 <= x < 80 for y, x, _text, _attr in screen.writes)


def test_left_window_click_updates_only_that_window_and_pane_click_updates_right(monkeypatch):
    tower = ux_preview.make_preview_tower()
    state = ux_preview_v2._state(tower)
    screen = _Screen()
    monkeypatch.setattr(ux_preview_v2.curses, "doupdate", lambda: None)

    ux_preview_v2.draw_preview_v2(screen, state)
    second_window_choice = next(
        index for index, item in enumerate(state["choices"])
        if item.get("id") == f"window:{tower.window_assets[1]['window_ref']}"
    )
    left_box = next(box for box in state["hit_boxes"] if box[4:] == ("left", second_window_choice))
    assert ux_preview_v2._hit_test(state, left_box[0] + 1, left_box[1]) == "left"
    assert state["workspace_name"] == "Window 2 · #1"
    assert len(state["panes"]) == 1
    assert state["panes"][0]["window_ref"] == tower.window_assets[1]["window_ref"]
    assert state["panes"][0]["transport"] == "local"

    first_window_choice = next(
        index for index, item in enumerate(state["choices"])
        if item.get("id") == f"window:{tower.window_assets[0]['window_ref']}"
    )
    ux_preview_v2._set_choice(state, first_window_choice)
    assert "remote_tmux" not in {row["transport"] for row in state["panes"]}
    assert {row["window_ref"] for row in state["panes"]} == {tower.window_assets[0]["window_ref"]}
    ux_preview_v2.draw_preview_v2(screen, state)
    third_pane_box = next(box for box in state["hit_boxes"] if box[4:] == ("pane", 2))
    assert ux_preview_v2._hit_test(state, third_pane_box[0] + 1, third_pane_box[1] + 1) == "pane"
    assert state["pane_index"] == 2
    assert state["panes"][state["pane_index"]]["pane_id"] == tower.rows[2]["pane_id"]

    ux_preview_v2.draw_preview_v2(screen, state)
    right_box = next(box for box in state["hit_boxes"] if box[4] == "right")
    assert ux_preview_v2._hit_test(state, right_box[0] + 1, right_box[1] + 1) == "right"
    assert state["active_panel"] == 2
    assert state["demo"] is None  # a click focuses; the safe demo needs explicit opt-in

    ssh_ref = tower.window_assets[-2]["window_ref"]
    ssh_choice = next(i for i, item in enumerate(state["choices"])
                      if item.get("id") == f"window:{ssh_ref}")
    ux_preview_v2._set_choice(state, ssh_choice)
    assert len(state["panes"]) == 1
    assert state["panes"][0]["transport"] == "ssh"
    assert state["panes"][0]["window_ref"] == ssh_ref


def test_saved_workspaces_select_their_fixture_window_groups():
    tower = ux_preview.make_preview_tower()
    stored_groups = tower.work_groups.all()
    state = ux_preview_v2._state(tower)
    code_review = next(i for i, item in enumerate(state["choices"])
                       if item.get("id") == "saved:code-review")
    ux_preview_v2._set_choice(state, code_review)
    assert state["workspace_name"] == "코드 리뷰"
    assert len({row["window_ref"] for row in state["panes"]}) == 2
    expected_targets = tower.work_groups.all()[1]["member_order"]
    assert [row["target_id"] for row in state["panes"]] == expected_targets
    assert state["window_refs"] == tuple(dict.fromkeys(row["window_ref"] for row in state["panes"]))
    assert tower.work_groups.all() == stored_groups


def test_project_window_references_render_panes_across_windows_without_store_writes():
    tower = ux_preview.make_preview_tower()
    refs = [tower.window_assets[0]["window_ref"], tower.window_assets[1]["window_ref"]]
    tower.projects = ux_preview._MemoryStore([{
        "project_id": "cross-window", "display_name": "통합 프로젝트", "window_refs": refs,
    }])
    before = tower.projects.all()
    state = ux_preview_v2._state(tower)
    index = next(i for i, item in enumerate(state["choices"])
                 if item.get("id") == "project:cross-window")

    ux_preview_v2._set_choice(state, index)

    assert state["workspace_name"] == "[프로젝트] 통합 프로젝트"
    assert {row["window_ref"] for row in state["panes"]} == set(refs)
    assert all(row.get("live_lines") for row in state["panes"])
    assert tower.projects.all() == before
    assert tower.projects.toggle_calls == 0


def test_saved_screen_uses_full_pane_identity_and_hides_stale_output():
    from tmux_agent_tower.state.control_screens import pane_reference

    tower = ux_preview.make_preview_tower()
    original = tower.rows[0]
    saved_ref = pane_reference(original)
    last_view = {
        "context": {"project_id": None, "window_ref": original["window_ref"]},
        "panes": [saved_ref], "selected": saved_ref, "layout": "focus", "updated_at": 1,
    }

    class ReadOnlyScreens:
        corrupt = False

        def screens(self):
            return []

        def last_view(self):
            return last_view

        def save_last_view(self, *_args):
            raise AssertionError("projection must not write the saved-screen store")

    tower.control_screens = ReadOnlyScreens()
    state = ux_preview_v2._state(tower)
    assert state["workspace_id"] == "screen:last-view"
    assert state["panes"][0]["target_id"] == original["target_id"]
    assert ux_preview_v2._current_selected_row(state)["pane_id"] == original["pane_id"]

    changed = dict(original, pane_pid=str(int(original["pane_pid"]) + 1),
                   target_id="different-pid-target", live_lines=["different pane output"])
    tower.rows[0] = changed
    ux_preview_v2._reconcile_room_state(state)
    selected = state["panes"][state["pane_index"]]
    assert selected.get("stale") is True
    assert selected.get("live_lines") == []
    assert ux_preview_v2._current_selected_row(state) is None


def test_work_group_duplicate_target_identity_fails_closed():
    tower = ux_preview.make_preview_tower()
    target = tower.work_groups.all()[0]["member_order"][0]
    duplicate = dict(next(row for row in tower.rows if row["target_id"] == target),
                     pane_id="%900", pane_pid="99000", window_id="@900", window_ref="other-ref")
    tower.rows.append(duplicate)
    state = ux_preview_v2._state(tower)
    index = next(i for i, item in enumerate(state["choices"])
                 if item.get("id") == "saved:ai-team")
    ux_preview_v2._set_choice(state, index)
    selected = next(row for row in state["panes"] if row.get("target_id") == target)
    assert selected.get("stale") is True
    assert selected.get("live_lines") == []


def test_work_group_target_id_becomes_disconnected_after_pane_pid_change():
    tower = ux_preview.make_preview_tower()
    original = tower.rows[0]
    group = tower.work_groups.all()[0]
    assert original["target_id"] in group["member_target_ids"]
    replacement_pid = str(int(original["pane_pid"]) + 1)
    replacement_target = ux_preview._digest("\0".join((
        original["tmux_host"], original["session_id"], original["pane_id"], replacement_pid,
    )))
    tower.rows[0] = dict(original, pane_pid=replacement_pid, target_id=replacement_target,
                         live_lines=["replacement pane output"])

    state = ux_preview_v2._state(tower)
    group_index = next(i for i, item in enumerate(state["choices"])
                       if item.get("id") == "saved:ai-team")
    ux_preview_v2._set_choice(state, group_index)
    former_member = next(row for row in state["panes"]
                         if row.get("target_id") == original["target_id"])

    assert former_member.get("stale") is True
    assert former_member.get("live_lines") == []
    assert replacement_target not in {row.get("target_id") for row in state["panes"]}


def test_window_pane_without_full_identity_never_displays_live_output():
    tower = ux_preview.make_preview_tower()
    row = tower.rows[0]
    row.pop("pane_pid")
    row["live_lines"] = ["must remain hidden"]
    state = ux_preview_v2._state(tower)
    selected = state["panes"][0]
    assert selected.get("stale") is True
    assert "신원 확인 불가" in selected["display_name"]
    assert selected["live_lines"] == []
    assert ux_preview_v2._current_selected_row(state) is None


def test_actual_l_route_uses_shared_renderer_and_existing_background_snapshot(monkeypatch):
    from tmux_agent_tower.ui import live_view, tower as tower_ui

    tower = ux_preview.make_preview_tower()
    refresh_calls = {"start": 0, "apply": 0}

    def start_refresh():
        refresh_calls["start"] += 1
        return True

    def apply_refresh():
        refresh_calls["apply"] += 1
        if refresh_calls["apply"] == 1:
            tower.rows[0]["live_lines"] = ["fixture refreshed snapshot"]
            return True
        return False

    tower.start_background_refresh = start_refresh
    tower.apply_background_refresh = apply_refresh
    screen = _Screen(keys=("q",))
    monkeypatch.setattr(ux_preview_v2.curses, "doupdate", lambda: None)
    monkeypatch.setattr(ux_preview_v2, "_setup_colors", lambda: None)
    monkeypatch.setattr(ux_preview_v2, "_enable_mouse", lambda: None)
    monkeypatch.setattr(live_view, "_apply_refresh", lambda current: current.apply_background_refresh())

    tower_ui.open_approved_control_room(screen, tower)

    rendered = " ".join(text for _y, _x, text, _attr in screen.writes)
    assert "Pane 배치" in rendered
    assert "LIVE" in rendered
    assert "fixture refreshed snapshot" in rendered
    assert refresh_calls["start"] == 1
    assert refresh_calls["apply"] >= 1
    assert not hasattr(tower, "load")  # the renderer does not fall back to a synchronous load
    assert tower.read_only_preview is True  # fixture metadata is not changed


def test_live_right_click_reuses_existing_focus_confirmation_path(monkeypatch):
    from tmux_agent_tower.ui import control_room

    tower = ux_preview.make_preview_tower()
    state = ux_preview_v2._state(tower)
    state.update(runtime_mode="tower", stdscr=_Screen())
    calls = []
    monkeypatch.setattr(control_room, "_request_right_handoff",
                        lambda _screen, _tower, row, current: calls.append((row, current)))
    ux_preview_v2._handoff_selected(state)
    assert len(calls) == 1
    assert calls[0][0]["target_id"] == state["selected_target_id"]
    assert calls[0][1] is True


@pytest.mark.parametrize("key", [" ", "\n"])
def test_live_tower_right_context_uses_focus_only_handoff(monkeypatch, key):
    from tmux_agent_tower.ui import control_room

    tower = ux_preview.make_preview_tower()
    state = ux_preview_v2._state(tower)
    state.update(runtime_mode="tower", stdscr=_Screen(), active_panel=2)
    calls = []
    monkeypatch.setattr(control_room, "_request_right_handoff",
                        lambda _screen, _tower, row, current: calls.append((row, current)))

    ux_preview_v2._handle_key(_Screen(), state, key)

    assert len(calls) == 1 and calls[0][1] is True
    assert calls[0][0]["target_id"] == state["selected_target_id"]


def test_runtime_y_reuses_existing_result_copy_path(monkeypatch):
    from tmux_agent_tower.ui import control_view, widgets

    tower = ux_preview.make_preview_tower()
    state = ux_preview_v2._state(tower)
    state.update(runtime_mode="tower", stdscr=_Screen(), active_panel=1)
    state["pane_index"] = 1  # %101 is intentionally duplicated by the fixture Peer.
    selected = state["panes"][1]
    state["selected_target_id"] = selected["target_id"]
    state["selected_signature"] = ux_preview_v2._pane_signature(selected)
    copied = []
    monkeypatch.setattr(control_view, "_copy_result",
                        lambda _tower, key, _screen: copied.append(key) or "복사했습니다")
    monkeypatch.setattr(widgets, "show_message_screen", lambda *_a, **_k: None)

    ux_preview_v2._handle_key(_Screen(), state, "y")

    assert copied == [selected["key"]]


def test_tower_home_l_route_delegates_to_actual_v2_control_room(monkeypatch):
    from tmux_agent_tower.ui import tower as tower_ui

    calls = []
    monkeypatch.setattr(ux_preview_v2, "open_tower_control_room",
                        lambda screen, current: calls.append((screen, current)))
    screen, tower = object(), object()
    tower_ui.open_approved_control_room(screen, tower)
    assert calls == [(screen, tower)]


def test_window_names_include_index_and_disambiguate_duplicate_sessions():
    tower = ux_preview.make_preview_tower()
    first = tower.window_assets[0]
    duplicate_ref = "duplicate-window-ref"
    duplicate = dict(first, window_ref=duplicate_ref, window_id="@900", tmux_host="preview-mainpc",
                    session="other-session", session_id="$900", host="Other host",
                    window_index="4")
    tower.window_assets.append(duplicate)
    source = next(row for row in tower.rows if row["window_ref"] == first["window_ref"])
    tower.rows.append(dict(source, window_ref=duplicate_ref, window_id="@900", tmux_host="preview-mainpc",
                           session="other-session", session_id="$900", window_index="4",
                           pane_id="%900", pane_pid="90000", target_id="duplicate-target"))

    choices = ux_preview_v2.navigation_choices(tower)
    labels = [item["label"] for item in choices if item.get("group") == "windows"]
    assert any("Window 1 · #0" in label and "$1" in label for label in labels)
    assert any("Window 1 · #4" in label and "$900" in label for label in labels)


def test_remote_peer_requires_registered_remote_profile_and_is_not_a_local_window():
    tower = ux_preview.make_preview_tower()
    choices = ux_preview_v2.navigation_choices(tower)
    peer_ref = tower.window_assets[-1]["window_ref"]
    assert not any(item.get("group") == "remote" for item in choices)
    assert all(item.get("window_ref") != peer_ref for item in choices if item.get("group") == "windows")

    tower.remote_hosts = [{"alias": "asus-preview-only", "name": "ASUS (fixture)"}]
    choices = ux_preview_v2.navigation_choices(tower)
    remote = [item for item in choices if item.get("group") == "remote" and item.get("kind") == "workspace"]
    assert len(remote) == 1
    assert remote[0]["window_ref"] == peer_ref
    assert remote[0]["id"].startswith("remote-window:")
    assert all(item.get("window_ref") != peer_ref for item in choices if item.get("group") == "windows")
    peer_choice = choices.index(remote[0])
    state = ux_preview_v2._state(tower)
    ux_preview_v2._set_choice(state, peer_choice)
    assert len(state["panes"]) == 1
    assert state["panes"][0]["transport"] == "remote_tmux"
    assert state["panes"][0]["window_ref"] == peer_ref
    assert state["panes"][0]["pane_id"] == "%101"  # duplicate pane number stays host-bound


def test_unregistered_peer_is_filtered_from_saved_group_without_mutating_its_contract():
    tower = ux_preview.make_preview_tower()
    peer = next(row for row in tower.rows if row["transport"] == "remote_tmux")
    groups = tower.work_groups.all()
    groups[0]["member_target_ids"].append(peer["target_id"])
    groups[0]["member_order"].append(peer["target_id"])
    tower.work_groups = ux_preview._MemoryStore(groups)
    before = tower.work_groups.all()

    choices = ux_preview_v2.navigation_choices(tower)
    saved = next(item for item in choices if item.get("id") == "saved:ai-team")
    assert peer["target_id"] in saved["target_ids"]  # preserve saved reference; do not silently delete it
    assert tower.work_groups.all() == before
    state = ux_preview_v2._state(tower)
    saved_index = next(i for i, item in enumerate(state["choices"])
                       if item.get("id") == "saved:ai-team")
    ux_preview_v2._set_choice(state, saved_index)
    peer_row = next(row for row in state["panes"] if row.get("target_id") == peer["target_id"])
    assert peer_row.get("stale") is True and peer_row.get("live_lines") == []

    tower.remote_hosts = [{"alias": "asus-preview-only", "name": "ASUS (fixture)"}]
    registered = next(item for item in ux_preview_v2.navigation_choices(tower)
                      if item.get("id") == "saved:ai-team")
    assert peer["target_id"] in registered["target_ids"]


def test_narrow_window_picker_scroll_keeps_late_window_keyboard_and_mouse_reachable(monkeypatch):
    tower = ux_preview.make_preview_tower()
    state = ux_preview_v2._state(tower)
    state["active_panel"] = 0
    screen = _Screen(24, 80)
    monkeypatch.setattr(ux_preview_v2.curses, "doupdate", lambda: None)
    for _ in range(9):
        ux_preview_v2._handle_key(screen, state, curses.KEY_DOWN)
    ux_preview_v2.draw_preview_v2(screen, state)

    selected = state["choices"][state["nav_index"]]
    assert selected["id"] == f"window:{tower.window_assets[-2]['window_ref']}"
    assert state["choice_scrolls"]["windows"] > 0
    hit = next(box for box in state["hit_boxes"] if box[4:] == ("left", state["nav_index"]))
    assert ux_preview_v2._hit_test(state, hit[0] + 1, hit[1]) == "left"
    assert state["workspace_name"] == "SSH Client 예시 · #0"


@pytest.mark.parametrize("height,width", [(24, 80), (40, 120), (44, 160)])
def test_registered_remote_section_respects_layout_bounds(monkeypatch, height, width):
    tower = ux_preview.make_preview_tower()
    tower.remote_hosts = [{"alias": "asus-preview-only", "name": "ASUS"}]
    state = ux_preview_v2._state(tower)
    state["active_panel"] = 0
    screen = _Screen(height, width)
    monkeypatch.setattr(ux_preview_v2.curses, "doupdate", lambda: None)
    ux_preview_v2.draw_preview_v2(screen, state)
    assert all(0 <= y < height and 0 <= x < width for y, x, _text, _attr in screen.writes)
    assert "원격 연결" in " ".join(text for _y, _x, text, _attr in screen.writes)
    remote_index = next(i for i, item in enumerate(state["choices"])
                        if item.get("group") == "remote" and item.get("kind") == "workspace")
    assert any(box[4:] == ("left", remote_index) for box in state["hit_boxes"])


@pytest.mark.parametrize("action_id,title", [
    ("action:save", "구성 저장"),
    ("action:settings", "설정 · 메뉴"),
])
def test_left_actions_open_preview_dialog_without_persistent_store_writes(action_id, title):
    tower = ux_preview.make_preview_tower()
    state = ux_preview_v2._state(tower)
    index = next(i for i, item in enumerate(state["choices"]) if item.get("id") == action_id)
    state["nav_index"] = index
    state["active_panel"] = 0
    assert ux_preview_v2._handle_key(_Screen(), state, "\n") is False
    assert state["dialog"]["title"] == title
    assert tower.folders.toggle_calls == 0
    assert tower.projects.toggle_calls == 0
    assert tower.work_groups.toggle_calls == 0
    assert ux_preview_v2._handle_key(_Screen(), state, "\x1b") is False
    assert state["dialog"] is None


def test_dialog_mouse_does_not_activate_underlying_workspace(monkeypatch):
    tower = ux_preview.make_preview_tower()
    state = ux_preview_v2._state(tower)
    state["dialog"] = {"title": "구성 저장", "body": "MainPC"}
    screen = _Screen()
    monkeypatch.setattr(ux_preview_v2.curses, "doupdate", lambda: None)
    ux_preview_v2.draw_preview_v2(screen, state)
    window_choice = next(i for i, item in enumerate(state["choices"])
                         if item.get("id") == f"window:{tower.window_assets[0]['window_ref']}")
    underlying = next(box for box in state["hit_boxes"] if box[4:] == ("left", window_choice))
    assert ux_preview_v2._hit_test(state, underlying[0] + 1, underlying[1]) is None
    assert state["dialog"] is not None
    assert state["workspace_name"] == "Window 1 · #0"


def test_click_focus_then_i_is_the_only_path_to_start_local_demo(monkeypatch):
    tower = ux_preview.make_preview_tower()
    state = ux_preview_v2._state(tower)
    state["active_panel"] = 2
    started = []

    class FakeDemo:
        def __init__(self):
            started.append(True)

        def resize(self, _rows, _columns):
            pass

        def close(self):
            pass

    monkeypatch.setattr(ux_preview_v2, "_EchoPty", FakeDemo)
    assert ux_preview_v2._handle_key(_Screen(), state, "i") is False
    assert started == [True]


def test_curses_mouse_event_focuses_right_without_starting_demo(monkeypatch):
    tower = ux_preview.make_preview_tower()
    state = ux_preview_v2._state(tower)
    screen = _Screen()
    monkeypatch.setattr(ux_preview_v2.curses, "doupdate", lambda: None)
    ux_preview_v2.draw_preview_v2(screen, state)
    right_box = next(box for box in state["hit_boxes"] if box[4] == "right")
    mouse = (0, right_box[0] + 1, right_box[1] + 1, 0, curses.BUTTON1_CLICKED)
    monkeypatch.setattr(ux_preview_v2.curses, "getmouse", lambda: mouse)

    assert ux_preview_v2._handle_key(screen, state, curses.KEY_MOUSE) is False
    assert state["active_panel"] == 2
    assert state["demo"] is None


def test_center_selection_can_scroll_to_panes_beyond_four_in_narrow_layout():
    tower = ux_preview.make_preview_tower()
    state = ux_preview_v2._state(tower)
    for _ in range(5):
        ux_preview_v2._move_pane(state, 1, 24, 80)
    assert state["pane_index"] == 5
    assert state["card_scroll"] > 0


def test_center_actions_remain_keyboard_accessible_without_extra_shortcuts():
    tower = ux_preview.make_preview_tower()
    state = ux_preview_v2._state(tower)
    state["active_panel"] = 1
    state["pane_index"] = len(state["panes"]) - 1
    screen = _Screen()
    assert ux_preview_v2._handle_key(screen, state, curses.KEY_DOWN) is False
    assert state["center_actions_focused"] is True
    assert ux_preview_v2._handle_key(screen, state, curses.KEY_RIGHT) is False
    assert state["center_action_index"] == 1
    assert ux_preview_v2._handle_key(screen, state, "\n") is False
    assert state["dialog"]["title"] == "Pane 추가"


@pytest.mark.parametrize("count", [1, 2, 3, 4, 5])
def test_selected_card_identity_is_the_single_live_identity(monkeypatch, count):
    tower = ux_preview.make_preview_tower()
    state = ux_preview_v2._state(tower)
    state["panes"] = list(tower.rows[:count])
    state["pane_index"] = count - 1
    screen = _Screen(40, 120)
    ux_preview_v2._ensure_card_visible(state, 40, 120)
    monkeypatch.setattr(ux_preview_v2.curses, "doupdate", lambda: None)
    ux_preview_v2.draw_preview_v2(screen, state)
    selected = state["panes"][state["pane_index"]]
    assert f"LIVE · {selected['display_name']}" in " ".join(
        text for _y, _x, text, _attr in screen.writes
    )


def test_five_card_layout_shows_first_page_and_scrolled_page():
    first_page = ux_preview_v2.card_rectangles(5, (4, 3, 28, 15), scroll_row=0)
    second_page = ux_preview_v2.card_rectangles(5, (4, 3, 28, 15), scroll_row=1)
    assert [card[0] for card in first_page] == [0, 1, 2, 3]
    assert [card[0] for card in second_page] == [2, 3, 4]


def test_demo_key_mapping_preserves_enter_backspace_and_navigation():
    assert ux_preview_v2._key_bytes("\n") == b"\n"
    assert ux_preview_v2._key_bytes(curses.KEY_BACKSPACE) == b"\x7f"
    assert ux_preview_v2._key_bytes(curses.KEY_UP) == b"\x1b[A"


def test_demo_page_keys_scroll_output_without_sending_to_pty():
    state = {"demo": SimpleNamespace(sent=[]), "live_scroll": 0}
    state["demo"].send = lambda payload: state["demo"].sent.append(payload)
    screen = _Screen()
    assert ux_preview_v2._handle_key(screen, state, curses.KEY_PPAGE) is False
    assert state["live_scroll"] == 8
    assert state["demo"].sent == []


@pytest.mark.skipif(not hasattr(ux_preview_v2, "pty"), reason="local PTY is unavailable")
def test_isolated_echo_pty_handles_text_enter_backspace_and_resize():
    demo = ux_preview_v2._EchoPty()
    try:
        demo.resize(18, 72)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and not any("준비됨" in line for line in demo.visible_lines()):
            demo.read_available()
            time.sleep(0.01)
        assert any("준비됨" in line for line in demo.visible_lines())

        demo.send("helloX\x7f!\n".encode())
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and not any("Echo: hello!" in line for line in demo.visible_lines()):
            demo.read_available()
            time.sleep(0.01)
        assert any("Echo: hello!" in line for line in demo.visible_lines())
        assert not any("Echo: helloX!" in line for line in demo.visible_lines())

        demo.send(b"second line\n")
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and not any("Echo: second line" in line for line in demo.visible_lines()):
            demo.read_available()
            time.sleep(0.01)
        assert any("Echo: hello!" in line for line in demo.visible_lines())
        assert any("Echo: second line" in line for line in demo.visible_lines())

        deadline = time.monotonic() + 1
        while time.monotonic() < deadline and not any("resize 18x72" in line for line in demo.visible_lines()):
            demo.read_available()
            time.sleep(0.01)
        assert any("resize 18x72" in line for line in demo.visible_lines())
    finally:
        demo.close()


def test_preview_restores_curses_mouse_mask_and_terminal_timeout(monkeypatch):
    calls = []

    def mousemask(mask):
        calls.append(mask)
        return curses.BUTTON1_CLICKED, 0x2468

    monkeypatch.setattr(ux_preview_v2.curses, "mousemask", mousemask)
    monkeypatch.setattr(ux_preview_v2.curses, "doupdate", lambda: None)
    screen = _Screen(keys=("q",))
    ux_preview_v2._preview(screen)
    assert calls == [curses.BUTTON1_CLICKED, 0x2468]
    assert screen.timeouts[-1] == 200


def test_fixture_contains_only_synthetic_local_and_remote_examples():
    tower = ux_preview.make_preview_tower()
    assert all(row.get("live_lines") for row in tower.rows)
    assert {row["transport"] for row in tower.rows} == {"local", "ssh", "remote_tmux"}
    assert not any(hasattr(store, "path") for store in (tower.folders, tower.projects, tower.work_groups))
