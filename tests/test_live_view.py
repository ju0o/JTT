from types import SimpleNamespace

import curses
import pytest

from tmux_agent_tower.i18n import t
from tmux_agent_tower.ui import live_view


def _row(key, **values):
    return {
        "key": key,
        "kind": "pane",
        "pane_id": key,
        "session": "isolated",
        "pane_pid": f"pid-{key}",
        "tmux_host": "workstation-a",
        "execution_host": "workstation-a",
        "transport": "local",
        "transport_target": "",
        "remote": False,
        "project": f"프로젝트-{key}",
        "agent": "Codex",
        "status": "WORKING",
        "attention": "none",
        "interaction": {},
        "result_state": "none",
        **values,
    }


class Screen:
    def __init__(self, height=20, width=100, keys=()):
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

    def refresh(self):
        pass

    def timeout(self, value):
        self.timeouts.append(value)

    def get_wch(self):
        return next(self.keys)


def test_layout_slots_and_sequential_selection_preserve_slot_order(monkeypatch):
    assert live_view.LAYOUTS == ("focus", "split-2", "grid-4", "main-plus-side")
    assert [live_view.layout_slots(name) for name in live_view.LAYOUTS] == [1, 2, 4, 4]
    rows = [_row(str(i)) for i in range(5)]
    tower = SimpleNamespace(visible_rows=rows)
    chosen = iter(("3", "1", "4", "0"))
    prompts = []

    def picker(_screen, title, items, **_kwargs):
        prompts.append(title)
        key = next(chosen)
        assert key in {item[0] for item in items}
        return SimpleNamespace(cancelled=False, selected_key=key)

    monkeypatch.setattr(live_view, "run_list_picker", picker)
    assert live_view.choose_panes(None, tower, "grid-4") == ["3", "1", "4", "0"]
    assert len(prompts) == 4


def test_tile_membership_and_order_stay_fixed_when_rows_change(monkeypatch):
    first = _row("a", attention="approval_required")
    second = _row("b", result_state="ready")

    class Tower:
        session = "isolated"
        own_pane_id = ""

        def __init__(self):
            self.rows = [first, second]
            self.loads = 0

        def load(self):
            self.loads += 1
            if self.loads > 1:
                self.rows = [dict(second, attention="input_required"), dict(first, status="IDLE")]

    tower = Tower()
    frames = []
    monkeypatch.setattr(live_view, "_draw", lambda _s, _t, slots, _f, _l, *_args: frames.append([(r["key"], r["status"], r["attention"], r["result_state"]) for r in slots]))
    keys = iter((curses.KEY_DOWN, "q"))
    monkeypatch.setattr(live_view, "read_key", lambda _s: next(keys))
    live_view._show_live_view(Screen(), tower, ["a", "b"], "side_by_side")
    assert frames[0] == [("a", "WORKING", "approval_required", "none"), ("b", "WORKING", "none", "ready")]
    assert [item[0] for item in frames[0]] == [item[0] for item in frames[1]] == ["a", "b"]
    assert frames[1][0][1:] == ("IDLE", "approval_required", "none")
    assert frames[1][1][1:] == ("WORKING", "input_required", "ready")


def test_reused_pane_key_keeps_original_label_and_hides_previous_output(monkeypatch):
    original = _row("%5", pane_pid="510")

    class Tower:
        session = "isolated"
        own_pane_id = ""

        def __init__(self):
            self.rows = [original]
            self.loads = 0

        def load(self):
            self.loads += 1
            if self.loads > 1:
                self.rows = [_row("%5", pane_pid="999", project="different pane")]

    tower = Tower()
    frames = []
    monkeypatch.setattr(live_view, "_draw", lambda _s, _t, slots, _f, _l, *_args: frames.append(slots))
    keys = iter((curses.KEY_DOWN, "q"))
    monkeypatch.setattr(live_view, "read_key", lambda _s: next(keys))

    live_view._show_live_view(Screen(), tower, ["%5"], "focus")

    assert frames[0] == [original]
    assert frames[1][0]["key"] == "%5"
    assert frames[1][0]["_live_stale"] is True
    assert frames[1][0]["pane_pid"] == "510"


def test_attention_and_new_result_are_highlighted_without_reordering(monkeypatch):
    screen = Screen()
    row = _row("a", attention="approval_required", result_state="ready", interaction={
        "type": "approval", "confidence": "high", "options": [{"key": "1", "safe": True}],
    })
    monkeypatch.setattr(live_view, "_live_lines", lambda *_: ["recent output"])
    live_view._draw_tile(screen, SimpleNamespace(), row, 0, 0, 0, 10, 80, False)
    attention_result = next(write for write in screen.writes if write[2].startswith("! "))
    assert "승인 필요" in attention_result[2]
    assert "새 결과" in attention_result[2]
    assert attention_result[3] & curses.A_BOLD
    assert any(write[2] == "recent output" for write in screen.writes)


def test_no_attention_or_result_does_not_add_a_false_badge(monkeypatch):
    screen = Screen()
    monkeypatch.setattr(live_view, "_live_lines", lambda *_: [])
    live_view._draw_tile(screen, SimpleNamespace(), _row("a"), 0, 0, 0, 10, 80, False)
    assert not any(write[2].startswith(("! ", "✓ ")) for write in screen.writes)


def test_narrow_layout_shows_focused_fallback(monkeypatch):
    screen = Screen(width=50)
    monkeypatch.setattr(live_view, "_live_lines", lambda *_: ["narrow output"])
    monkeypatch.setattr(live_view.curses, "doupdate", lambda: None)
    live_view._draw(screen, SimpleNamespace(), [_row("a"), _row("b")], 1, "side_by_side")
    assert any(y == 0 and text == t("live.title") for y, _x, text, _attr in screen.writes)
    assert any(y == 1 and text.startswith("↑↓←→") for y, _x, text, _attr in screen.writes)
    assert any(y == 3 and "프로젝트-b" in text for y, _x, text, _attr in screen.writes)
    assert any(y == 6 and text == "narrow output" for y, _x, text, _attr in screen.writes)
    assert all(y >= 3 for y, _x, _text, _attr in screen.writes if y not in (0, 1, 19))
    assert any("작은 화면" in write[2] for write in screen.writes)


def test_49x16_cjk_narrow_view_keeps_every_line_inside_screen(monkeypatch):
    screen = Screen(height=16, width=49)
    monkeypatch.setattr(live_view, "_live_lines", lambda *_: ["출력-한글-🙂"])
    monkeypatch.setattr(live_view.curses, "doupdate", lambda: None)
    live_view._draw(screen, SimpleNamespace(), [_row("a"), _row("b")], 1, "grid")
    assert any("프로젝트-b" in text for _y, _x, text, _attr in screen.writes)
    assert any(text == "출력-한글-🙂" for _y, _x, text, _attr in screen.writes)
    assert all(0 <= y < 16 and 0 <= x < 49 for y, x, _text, _attr in screen.writes)


def test_split_tile_text_never_overflows_into_the_next_tile(monkeypatch):
    screen = Screen(height=24, width=80)
    rows = [
        _row("a", project="가나다라마바사아자차카타파하-very-long-project-name"),
        _row("b", project="두번째-프로젝트-very-long-project-name"),
    ]
    monkeypatch.setattr(live_view, "_live_lines", lambda *_: ["긴 출력 줄-한글"])
    monkeypatch.setattr(live_view.curses, "doupdate", lambda: None)
    live_view._draw(screen, SimpleNamespace(), rows, 0, "side_by_side")
    tile_writes = [entry for entry in screen.writes if entry[0] >= 3]
    assert any(x < 40 for _y, x, _text, _attr in tile_writes)
    assert any(x >= 40 for _y, x, _text, _attr in tile_writes)
    for y, x, text, _attr in tile_writes:
        if y == screen.size[0] - 1:
            continue
        boundary = 39 if x < 40 else 79
        assert x + live_view.display_width(text) <= boundary


def test_attention_subtypes_take_priority_and_unmeasured_confirm_stays_unknown():
    safe_options = [
        {"key": "1", "label": "Yes", "safe": True},
        {"key": "2", "label": "No", "safe": True},
    ]
    cases = [
        (_row("a", attention="approval_required", interaction={"type": "unknown"}), "live.attention_unknown"),
        (_row("a", attention="approval_required", interaction={"type": "confirm", "options": [], "confidence": "low"}), "live.attention_unknown"),
        (_row("a", attention="input_required", interaction={"type": "confirm", "options": safe_options, "confidence": "high"}), "state.choice"),
        (_row("a", attention="input_required", interaction={"type": "choice", "options": safe_options, "confidence": "high"}), "state.choice"),
        (_row("a", attention="input_required", interaction={"type": "text", "confidence": "medium"}), "state.answer"),
        (_row("a", attention="approval_required", interaction={"type": "approval", "options": safe_options[:1], "confidence": "high"}), "state.approval"),
        (_row("a", attention="error", interaction={"type": "unknown"}), "state.error"),
    ]
    for row, label in cases:
        assert live_view._attention(row) == t(label)


def test_no_attention_is_separate_from_idle_and_result_none_is_explicit():
    row = _row("a", status="IDLE", attention="none", result_state="none")
    assert live_view._attention(row) == t("live.attention_none")
    assert live_view._attention(_row("a", status="WAITING", attention="none")) == t("live.attention_none")
    assert live_view._execution(row) == ("○", t("status.IDLE"))
    assert live_view._result(row) == t("live.result_none")
    assert live_view._result(_row("a", result_state="ready")) == t("state.result")
    assert live_view._result(_row("a", result_state="read")) == t("control.result_read")


def test_empty_start_choices_do_not_look_like_a_selected_task(monkeypatch):
    from tmux_agent_tower.ui import tower as tower_module

    row = {
        "kind": "zero", "project": "작업 하나 시작", "agent": "", "status": "IDLE",
        "action": "task", "guide": "", "key": "zero:task",
    }
    tower = SimpleNamespace(
        visible_rows=[row], selected=0,
        visual=[{"type": "header", "host": "작업"}, {"type": "data", "row": row, "row_index": 0}],
        access_context=None, filter_text="", notice="", config={},
    )
    screen = Screen(height=14, width=42)
    monkeypatch.setattr(tower_module, "_duration_text", lambda *_: "")
    monkeypatch.setattr(tower_module, "_build_detail_fields", lambda *_: pytest.fail("empty action is not task detail"))
    monkeypatch.setattr(tower_module.curses, "has_colors", lambda: False)
    monkeypatch.setattr(tower_module.curses, "doupdate", lambda: None)

    tower_module.draw(screen, tower)

    text = " ".join(item[2] for item in screen.writes)
    assert "내 AI 작업" in text
    assert "아직 실행 중인 작업이 없습니다." in text
    assert "AI 작업을 한곳에서 보고" in text
    assert "현재 접속 위치를 확인할 수 없습니다" in text
    assert "Enter 시작" in text
    assert "+ 새 작업" in text
    assert "Ctrl+b" not in text
    assert "작업 이름" not in text


@pytest.mark.parametrize("width", [60, 80])
def test_home_footer_keeps_primary_hints_within_two_lines(width):
    from tmux_agent_tower.ui import tower as tower_module
    from tmux_agent_tower.ui.render import display_width

    tower = SimpleNamespace(
        visible_rows=[{"kind": "pane", "pane_id": "%7", "key": "%7"}],
        selected=0,
        view_mode=tower_module.USER_WORK_VIEW,
    )
    lines = tower_module._home_footer_lines(tower, width)
    rendered = " ".join(lines)

    assert len(lines) <= 2
    assert all(display_width(line) <= width - 4 for line in lines)
    for hint in ("↑↓ 이동", "Enter 대화 열기", "Y 최신 답변 복사", "Space 메뉴", "+ 새 작업", "? 도움말"):
        assert hint in rendered


def test_default_home_draws_user_identity_without_physical_inspector_or_legacy_wall(monkeypatch):
    from tmux_agent_tower.ui import tower as tower_module

    group = {
        "kind": "work_group", "key": "group:g", "group_id": "g",
        "display_name": "SamplePortal 개선", "project": "SamplePortal", "member_count": 1,
        "summary": "● 1 작업 중", "summary_compact": "●1", "status": "UNKNOWN",
    }
    task = _row(
        "%42", target_id="target-1", display_name="API 확인",
        project="SamplePortal", role="builder", result_state="ready",
        pane_id="%42", window_id="@4", session="0", window_index=2,
        window_name="main", execution_host="workstation-a",
    )
    tower = SimpleNamespace(
        visible_rows=[group, task], selected=1,
        visual=[
            {"type": "header", "host": "작업"},
            {"type": "data", "row": group, "row_index": 0},
            {"type": "data", "row": task, "row_index": 1},
        ],
        access_context=None, filter_text="", notice="", config={"show_status_duration": False},
        view_mode=tower_module.USER_WORK_VIEW,
    )
    screen = Screen(height=24, width=100)
    monkeypatch.setattr(tower_module.curses, "has_colors", lambda: False)
    monkeypatch.setattr(tower_module.curses, "doupdate", lambda: None)

    tower_module.draw(screen, tower)
    text = " ".join(item[2] for item in screen.writes)

    assert "SamplePortal 개선" in text and "API 확인" in text
    assert "구현 · Codex" in text
    assert any(item[2].startswith("› ") for item in screen.writes)
    assert not any(item[2] == " " * 99 for item in screen.writes)
    assert "실행 위치 workstation-a" in text
    assert "새 결과" in text
    for internal in ("Pane ID", "Window ID", "Session", "%42", "@4", "A 우선순위", "M 휴대폰", "Ctrl+b w"):
        assert internal not in text
    for key in ("Enter 대화 열기", "Space 메뉴", "+ 새 작업", "Y 최신 답변 복사", "? 도움말"):
        assert key in text


def test_tower_v_opens_navigator_and_l_opens_live_view(monkeypatch):
    from tmux_agent_tower.server import service
    from tmux_agent_tower.ui import tower as tower_module

    class FakeTower:
        session = "isolated-test"
        own_pane_id = ""
        visible_rows = [{"kind": "pane", "key": "%7"}]
        selected = 0

        def __init__(self):
            self.navigator_mode = False
            self.loaded = 0

        def load(self):
            self.loaded += 1

        def toggle_navigator(self):
            self.navigator_mode = not self.navigator_mode

    tower = FakeTower()
    screen = Screen()
    events = []
    keys = iter(("v", "l", "\x03"))
    monkeypatch.setattr(tower_module, "Tower", lambda *_args, **_kwargs: tower)
    monkeypatch.setattr(tower_module, "REFRESH_SECONDS", 10**9)
    monkeypatch.setattr(tower_module, "read_key", lambda _screen: next(keys))
    monkeypatch.setattr(tower_module, "draw", lambda _screen, current, **_kwargs: events.append(("draw", current.navigator_mode)))
    monkeypatch.setattr(tower_module, "_remote_state", lambda: "stopped")
    monkeypatch.setattr(service, "maybe_autostart", lambda **_kwargs: None)
    monkeypatch.setattr(live_view, "open_live_view", lambda _screen, current, selected_key=None: events.append(("live", current.navigator_mode, selected_key)))

    tower_module._run_loop(screen, "isolated-test", "")

    assert events[0] == ("draw", False)
    assert ("draw", True) in events
    assert ("live", True, "%7") in events
    assert tower.navigator_mode is True
    assert tower.loaded == 2


def test_live_on_selected_group_opens_group_view(monkeypatch):
    from tmux_agent_tower.server import service
    from tmux_agent_tower.ui import tower as tower_module

    group = {"kind": "work_group", "group_id": "g1", "display_name": "ExampleProject"}

    class FakeTower:
        session = "isolated-test"
        own_pane_id = ""
        visible_rows = [group]
        selected = 0

        def load(self):
            pass

    tower = FakeTower()
    opened = []
    monkeypatch.setattr(tower_module, "Tower", lambda *_a, **_k: tower)
    monkeypatch.setattr(tower_module, "REFRESH_SECONDS", 10**9)
    keys = iter(("l", "\x03"))
    monkeypatch.setattr(tower_module, "read_key", lambda _screen: next(keys))
    monkeypatch.setattr(tower_module, "draw", lambda *_a, **_k: None)
    monkeypatch.setattr(tower_module, "_remote_state", lambda: "stopped")
    monkeypatch.setattr(service, "maybe_autostart", lambda **_k: None)
    monkeypatch.setattr(live_view, "open_group_live_view", lambda _s, _t, row: opened.append(row))

    tower_module._run_loop(Screen(), "isolated-test", "")

    assert opened == [group]


def test_primary_navigation_and_result_keys_route_to_existing_flows(monkeypatch):
    from tmux_agent_tower.server import service
    from tmux_agent_tower.ui import control_view, settings_menu, structure_menu, tower as tower_module, worksets
    from tmux_agent_tower.ui.widgets import show_message_screen

    pane = _row("%7")

    class FakeTower:
        session = "isolated-test"
        own_pane_id = ""
        visible_rows = [pane]
        selected = 0
        notice = ""

        def load(self):
            pass

    tower = FakeTower()
    events = []
    keys = iter(("s", "l", "c", "?", "y", "g", "n", "w", "\x03"))
    monkeypatch.setattr(tower_module, "Tower", lambda *_a, **_k: tower)
    monkeypatch.setattr(tower_module, "REFRESH_SECONDS", 10**9)
    monkeypatch.setattr(tower_module, "read_key", lambda _screen: next(keys))
    monkeypatch.setattr(tower_module, "draw", lambda *_a, **_k: None)
    monkeypatch.setattr(tower_module, "_remote_state", lambda: "stopped")
    monkeypatch.setattr(service, "maybe_autostart", lambda **_k: None)
    monkeypatch.setattr(worksets, "open_saved_collection", lambda *_a: events.append("saved"))
    monkeypatch.setattr(structure_menu, "open_create_hub", lambda *_a: events.append("create"))
    monkeypatch.setattr(live_view, "open_live_view", lambda *_a: events.append("live"))
    monkeypatch.setattr(settings_menu, "open_settings", lambda *_a: events.append("settings"))
    monkeypatch.setattr(control_view, "_copy_result", lambda *_a: events.append("copy") or "복사했습니다")
    monkeypatch.setattr(control_view, "_go", lambda *_a: events.append("terminal") or t("control.focused"))
    monkeypatch.setattr("tmux_agent_tower.ui.widgets.show_message_screen", lambda *_a, **_k: events.append("message"))

    tower_module._run_loop(Screen(), "isolated-test", "")

    assert events == ["saved", "live", "settings", "message", "copy", "message", "terminal", "create", "create"]


def test_focus_side_by_side_and_grid_draw_selected_order(monkeypatch):
    monkeypatch.setattr(live_view.curses, "doupdate", lambda: None)
    for layout, keys in (("focus", ["a"]), ("side_by_side", ["a", "b"]), ("grid", ["a", "b", "c", "d"])):
        screen = Screen()
        cells = []
        monkeypatch.setattr(live_view, "_draw_tile", lambda _s, _t, row, _i, y, x, *_rest: cells.append((row["key"], y, x)))
        live_view._draw(screen, SimpleNamespace(), [_row(key) for key in keys], 0, layout)
        assert [cell[0] for cell in cells] == keys
        assert len({(cell[1], cell[2]) for cell in cells}) == len(keys)


def test_enter_opens_selected_detail_and_keeps_fixed_tile_set(monkeypatch):
    rows = [_row("a"), _row("b")]
    tower = SimpleNamespace(session="isolated", own_pane_id="", rows=rows, load=lambda: None)
    opened = []
    monkeypatch.setattr(live_view, "_draw", lambda *_: None)
    monkeypatch.setattr(live_view, "read_key", lambda _s: next(keys))
    monkeypatch.setattr("tmux_agent_tower.ui.control_view.open_control_view", lambda _s, _t, key: opened.append(key))
    keys = iter((curses.KEY_DOWN, "\n", "q"))
    live_view._show_live_view(Screen(), tower, ["a", "b"], "side_by_side")
    assert opened == ["b"]


def test_remote_rows_and_empty_capture_show_plain_unavailable(monkeypatch):
    remote = _row("workstation-b:%9", remote=True)
    assert live_view._live_lines(SimpleNamespace(), remote) == ["실시간 화면을 사용할 수 없습니다."]
    monkeypatch.setattr(live_view, "get_pane_screen", lambda *_args, **_kwargs: (True, "", {"lines": []}))
    assert live_view._live_lines(SimpleNamespace(session="s", own_pane_id=""), _row("%1")) == ["실시간 화면을 사용할 수 없습니다."]


def test_dead_or_reused_panes_hide_old_output_and_show_uncertainty(monkeypatch):
    tower = SimpleNamespace(session="isolated", own_pane_id="")
    dead = _row("%1", status="DEAD")
    assert live_view._live_lines(tower, dead) == [t("live.ended")]

    calls = []
    monkeypatch.setattr(
        live_view,
        "get_pane_screen",
        lambda *args, **kwargs: calls.append((args, kwargs)) or (False, "stale", {}),
    )
    reused = _row("%1", pane_pid="1234")
    assert live_view._live_lines(tower, reused) == [t("live.changed_output")]
    assert calls[0][1]["expected_pane_pid"] == "1234"


def test_live_view_uses_only_safe_capture_reader_and_has_no_layout_mutation():
    import inspect

    source = inspect.getsource(live_view)
    assert "get_pane_screen" in source
    for mutation in ("select-layout", "select-pane", "move-pane", "split-window", "resize-pane", "break-pane"):
        assert mutation not in source


def test_main_plus_side_uses_main_and_saved_side_order(monkeypatch):
    screen = Screen(height=30, width=120)
    rows = [_row(str(index)) for index in range(4)]
    cells = []
    monkeypatch.setattr(live_view, "_draw_tile", lambda _s, _t, row, _i, y, x, h, w, focused, *_rest: cells.append((row["key"], y, x, h, w, focused)))
    monkeypatch.setattr(live_view.curses, "doupdate", lambda: None)
    live_view._draw(screen, SimpleNamespace(), rows, 0, "main-plus-side")
    assert [cell[0] for cell in cells] == ["0", "1", "2", "3"]
    assert cells[0][2] == 0 and cells[1][2] > 0
    assert cells[0][3] > cells[1][3]
    assert [cell[1] for cell in cells[1:]] == sorted(cell[1] for cell in cells[1:])


def test_group_live_uses_saved_layout_and_shows_stale_members(monkeypatch):
    live_rows = [_row("%1", target_id="t1"), _row("%2", target_id="t2"), _row("%4", target_id="t4"), _row("%5", target_id="t5")]

    class Tower:
        rows = live_rows

        def load(self):
            pass

    group = {
        "member_order": ["t1", "t2", "t3", "t4", "t5"],
        "layout": "main-plus-side",
        "layout_slots": {"t5": "main", "t2": "side-1", "t4": "side-2", "t3": "bottom"},
        "member_labels": {"t3": "QA", "t5": "Builder"},
    }
    opened = []
    monkeypatch.setattr(live_view, "_show_live_view", lambda _s, _t, keys, layout: opened.append((keys, layout)))
    live_view.open_group_live_view(Screen(), Tower(), group)
    assert opened == [(["%5", "%2", "%4", "stale:t3", "%1"], "main-plus-side")]


def test_home_group_summary_resolves_members_from_persistent_store(monkeypatch):
    row = _row("%2", target_id="target-2")
    complete_group = {"group_id": "g", "member_order": ["target-2"], "member_target_ids": ["target-2"], "layout": "focus"}
    tower = SimpleNamespace(
        rows=[row], load=lambda: None,
        work_groups=SimpleNamespace(all=lambda: [complete_group]),
    )
    opened = []
    monkeypatch.setattr(live_view, "_show_live_view", lambda _s, _t, keys, layout: opened.append((keys, layout)))
    live_view.open_group_live_view(Screen(), tower, {"kind": "work_group", "group_id": "g", "display_name": "Group"})
    assert opened == [(["%2"], "focus")]


def test_task_live_entry_skips_layout_picker(monkeypatch):
    opened = []
    monkeypatch.setattr(live_view, "_show_live_view", lambda _s, _t, keys, layout: opened.append((keys, layout)))
    monkeypatch.setattr(live_view, "run_list_picker", lambda *_a, **_k: pytest.fail("task Live should open directly"))
    live_view.open_live_view(Screen(), SimpleNamespace(), "%9")
    assert opened == [(["%9"], "focus")]


def test_live_routes_copy_menu_terminal_and_conversation_to_shared_helpers(monkeypatch):
    from tmux_agent_tower.ui import control_view, structure_menu, widgets

    row = _row("%8")
    tower = SimpleNamespace(rows=[row], load=lambda: None, session="isolated", own_pane_id="")
    events = []
    monkeypatch.setattr(live_view, "_draw", lambda *_args: None)
    keys = iter(("y", " ", "g", "\n", "q"))
    monkeypatch.setattr(live_view, "read_key", lambda _screen: next(keys))
    monkeypatch.setattr(control_view, "_copy_result", lambda *_args: events.append("copy") or "copied")
    monkeypatch.setattr(structure_menu, "open_pane_menu", lambda _s, _t, selected: events.append(("menu", selected["key"])))
    monkeypatch.setattr(control_view, "_go", lambda *_args: events.append("terminal") or t("control.focused"))
    monkeypatch.setattr(control_view, "open_control_view", lambda _s, _t, key: events.append(("conversation", key)))
    monkeypatch.setattr(widgets, "show_message_screen", lambda *_args, **_kwargs: events.append("message"))
    live_view._show_live_view(Screen(), tower, ["%8"], "focus")
    assert events == ["copy", "message", ("menu", "%8"), "terminal", ("conversation", "%8")]


def test_stale_live_card_cannot_copy_open_or_focus_reused_pane(monkeypatch):
    from tmux_agent_tower.ui import control_view, structure_menu, widgets

    original = _row("%8", pane_pid="500")

    class Tower:
        session = "isolated"
        own_pane_id = ""

        def __init__(self):
            self.rows = [original]
            self.loads = 0

        def load(self):
            self.loads += 1
            if self.loads > 1:
                self.rows = [_row("%8", pane_pid="999")]

    tower = Tower()
    actions = []
    monkeypatch.setattr(live_view, "_draw", lambda *_args: None)
    keys = iter((curses.KEY_RESIZE, "y", "g", " ", "\n", "q"))
    monkeypatch.setattr(live_view, "read_key", lambda _screen: next(keys))
    monkeypatch.setattr(control_view, "_copy_result", lambda *_a: pytest.fail("stale result copy"))
    monkeypatch.setattr(control_view, "_go", lambda *_a: pytest.fail("stale terminal focus"))
    monkeypatch.setattr(control_view, "open_control_view", lambda *_a: pytest.fail("stale conversation open"))
    monkeypatch.setattr(structure_menu, "open_pane_menu", lambda *_a: pytest.fail("stale task menu"))
    monkeypatch.setattr(widgets, "show_message_screen", lambda *_a, **_kw: actions.append("blocked"))
    live_view._show_live_view(Screen(), tower, ["%8"], "focus")
    assert actions == ["blocked", "blocked", "blocked"]


@pytest.mark.parametrize("height,width", [(45, 160), (40, 120), (30, 100), (24, 80), (14, 42)])
def test_resize_matrix_has_no_out_of_bounds_writes(monkeypatch, height, width):
    screen = Screen(height=height, width=width)
    monkeypatch.setattr(live_view, "_live_lines", lambda *_: ["한글 출력 " * 20])
    monkeypatch.setattr(live_view.curses, "doupdate", lambda: None)
    live_view._draw(screen, SimpleNamespace(), [_row(str(i), role="builder") for i in range(4)], 0, "grid-4")
    assert all(0 <= y < height and 0 <= x < width for y, x, _text, _attr in screen.writes)
    if width == 42:
        assert any("작은 화면" in text for _y, _x, text, _attr in screen.writes)


def test_live_role_heading_hides_internal_pane_identity():
    row = _row("%15", role="builder", display_name="구현 중인 일")
    assert live_view.pane_label(row) == t("role.builder")
    assert "%15" not in live_view.pane_label(row)
