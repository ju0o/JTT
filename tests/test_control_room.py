import curses
import unicodedata
from types import SimpleNamespace

import pytest

from tmux_agent_tower.ui import control_room, live_view


def _pane(key, *, target=None, pid=None, pane_index=0, **values):
    return {
        "key": key,
        "kind": "pane",
        "target_id": target or f"target-{key}",
        "pane_id": key,
        "pane_pid": pid or f"pid-{key}",
        "session": "work",
        "session_id": "$1",
        "window_id": "@1",
        "window_index": "0",
        "window_name": "Main",
        "pane_index": pane_index,
        "tmux_host": "mainpc",
        "execution_host": "mainpc",
        "transport": "local",
        "transport_target": "",
        "remote": False,
        "display_name": f"Task {key}",
        "task_name": f"Task {key}",
        "project": "Project Alpha",
        "agent": "Codex",
        "status": "WORKING",
        "attention": "none",
        "interaction": {},
        "result_state": "none",
        "live_lines": [f"output {key}", "한국어 출력"],
        **values,
    }


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

    def noutrefresh(self):
        pass

    def refresh(self):
        pass

    def timeout(self, value):
        self.timeouts.append(value)

    def keypad(self, _value):
        pass

    def get_wch(self):
        return next(self.keys)


def _mouse_driven_room(monkeypatch, tower, keys, *, target_kind, target, resize_to=None):
    """Drive an isolated fake curses loop and click a rendered identity."""
    screen = Screen(40, 120)
    hit_state = {}
    original_draw = control_room.draw
    mouse_event = {}
    drawn_tiles = []

    def capture_draw(*args, **kwargs):
        result = original_draw(*args, **kwargs)
        hit_state.clear()
        hit_state.update(kwargs.get("hit_test_state") or {})
        return result

    def read_key(_screen):
        key = next(keys)
        if key == curses.KEY_RESIZE and resize_to is not None:
            _screen.size = resize_to
        if key == curses.KEY_MOUSE:
            if target_kind == "window":
                region = hit_state["nav_region"]
                top = hit_state["nav_start"]
                nav = control_room.window_navigation_lines(
                    tower, control_room._visible_panes(tower),
                    str(tower.visible_rows[0].get("window_ref") or ""),
                )
                index = next(i for i, line in enumerate(nav) if line.get("window_ref") == target)
                mouse_event["value"] = (0, region[0], region[1] + index - top, 0,
                                         curses.BUTTON1_CLICKED)
            elif target_kind == "pane":
                region = hit_state["pane_region"]
                top = hit_state["pane_start"]
                rows = [row for row in control_room._visible_panes(tower)
                        if row.get("window_ref") == target[0]]
                index = next(i for i, row in enumerate(rows)
                             if control_room.pane_identity(row) == target[1])
                mouse_event["value"] = (0, region[0], region[1] + (index - top) * 2, 0,
                                         curses.BUTTON1_CLICKED)
            else:
                region = hit_state["right_region"]
                mouse_event["value"] = (0, region[0], region[1], 0, curses.BUTTON1_CLICKED)
        return key

    monkeypatch.setattr(control_room, "draw", capture_draw)
    monkeypatch.setattr(control_room, "read_key", read_key)
    monkeypatch.setattr(control_room.curses, "getmouse", lambda: mouse_event["value"])
    monkeypatch.setattr(control_room, "_enable_room_mouse", lambda: 0)
    monkeypatch.setattr(control_room, "_restore_room_mouse", lambda _mask: None)
    if target_kind == "right":
        monkeypatch.setattr(control_room, "run_list_picker", lambda *_a, **_kw: SimpleNamespace(
            cancelled=True, selected_key="cancel",
        ))
    monkeypatch.setattr(control_room.live_view, "_start_refresh", lambda _tower: False)
    monkeypatch.setattr(control_room.live_view.curses, "doupdate", lambda: None)
    monkeypatch.setattr(control_room.live_view, "_draw_tile",
                        lambda _screen, _tower, row, *_args: drawn_tiles.append(row.get("target_id")))
    monkeypatch.setattr(control_room.control_actions, "submit_user_text_once",
                        lambda *_a: pytest.fail("mouse navigation must never submit Agent text"))
    monkeypatch.setattr(control_room.control_actions, "send_prompt",
                        lambda *_a, **_kw: pytest.fail("mouse navigation must never relay Agent keys"))
    control_room.open_control_room(screen, tower)
    screen.drawn_tiles = drawn_tiles
    return screen


def _visible_width(text):
    return sum(2 if unicodedata.east_asian_width(char) in ("W", "F") else 1 for char in text)


def test_navigation_shows_hierarchy_and_one_representative_task_name():
    row = _pane(
        "%1", display_name="Projects Briping", name_origin="auto",
        project="Tmux Agent Tower", work_group_name="Release team",
    )

    lines = control_room.navigation_lines([row], "%1", control_room.pane_identity(row))
    labels = [line["text"] for line in lines]

    assert labels == [
        "mainpc", "  세션 work", "    창 0: Main", "      프로젝트 묶음 · Release team",
        "Projects Briping · Codex",
    ]
    assert "Projects Briping · Tmux Agent Tower" not in labels
    assert lines[-1]["selected"] is True


def test_related_grid_preserves_group_order_and_always_includes_selected():
    rows = [_pane(f"%{index}", target=f"target-{index}", pane_index=index,
                  work_group_id="g1") for index in range(5)]
    group = {"group_id": "g1", "member_order": [f"target-{i}" for i in range(5)]}

    selected_rows = control_room.related_panes(rows[-1], rows, [group])

    assert len(selected_rows) == 4
    assert [row["target_id"] for row in selected_rows] == [
        "target-1", "target-2", "target-3", "target-4",
    ]
    assert any(control_room.pane_identity(row) == control_room.pane_identity(rows[-1]) for row in selected_rows)


def test_related_grid_does_not_mix_matching_pane_ids_from_another_tmux_host():
    selected = _pane("%1", target="main-target")
    peer = _pane("%1", target="peer-target", tmux_host="asus", execution_host="asus",
                 remote=True, transport="remote_tmux", transport_target="asus")

    related = control_room.related_panes(selected, [selected, peer])

    assert related == [selected]
    assert control_room._host_label(_pane(
        "%5", transport="ssh", transport_target="asus", tmux_host="MainPC", execution_host="ASUS",
    )) == "ASUS (SSH 창 · MainPC)"


def test_project_tree_groups_exact_cross_host_window_refs_and_keeps_duplicate_pane_ids_distinct():
    main_ref, asus_ref = "a" * 64, "b" * 64
    main = _pane("%1", target="main-target", window_ref=main_ref,
                 tmux_host="mainpc", execution_host="mainpc", window_id="@1")
    asus = _pane("%1", target="asus-target", window_ref=asus_ref,
                 tmux_host="asus", execution_host="asus", window_id="@1", remote=True,
                 transport="remote_tmux", transport_target="asus")
    project = {
        "project_id": "project-1", "display_name": "통합 프로젝트",
        "window_refs": [asus_ref, main_ref], "order": 0, "collapsed": False,
    }
    assets = [
        {"window_ref": main_ref, "display_name": "Main", "tmux_host": "mainpc", "execution_host": "mainpc"},
        {"window_ref": asus_ref, "display_name": "Remote", "tmux_host": "asus", "execution_host": "asus"},
    ]

    lines = control_room.navigation_lines(
        [main, asus], "%1", control_room.pane_identity(main),
        projects=[project], window_assets=assets,
    )
    project_line = next(line for line in lines if line["kind"] == "tower_project")
    window_lines = [line for line in lines if line["kind"] == "project_window"]
    pane_lines = [line for line in lines if line["kind"] == "pane" and line.get("project_id")]

    assert project_line["member_rows"] == [asus, main]
    assert [line["window_ref"] for line in window_lines] == [asus_ref, main_ref]
    assert len(pane_lines) == 2
    assert pane_lines[0]["node_key"] != pane_lines[1]["node_key"]
    assert control_room._row_for_nav_line(pane_lines[0], [main, asus]) is asus
    assert control_room._scope_grid_rows(asus, [main, asus], [], project_line) == [asus, main]


def _mouse_tower(rows, assets=None):
    return SimpleNamespace(
        rows=rows, visible_rows=rows, selected=0,
        window_assets=assets or [], own_pane_id="%999", local_host="mainpc",
        projects=SimpleNamespace(all=lambda: [], hidden_refs=lambda: set()),
        work_groups=SimpleNamespace(all=lambda: []),
        start_background_refresh=lambda: False, _apply_filter=lambda: None,
    )


def test_scrolled_window_mouse_hit_uses_rendered_start_and_selects_clicked_identity(monkeypatch):
    rows = [_pane(f"%{index}", target=f"target-{index}", window_ref=f"window-{index}",
                  window_id=f"@{index}", window_index=str(index), window_name=f"Window {index}")
            for index in range(50)]
    tower = _mouse_tower(rows)
    nav = control_room.window_navigation_lines(tower, rows, rows[0]["window_ref"])
    first = next(i for i, line in enumerate(nav) if line.get("window_ref") == rows[0]["window_ref"])
    target = rows[-1]["window_ref"]
    target_index = next(i for i, line in enumerate(nav) if line.get("window_ref") == target)
    keys = iter([curses.KEY_DOWN] * (target_index - first) + [curses.KEY_MOUSE, "q"])

    _mouse_driven_room(monkeypatch, tower, keys, target_kind="window", target=target)

    assert tower.selected == len(rows) - 1


def test_scrolled_pane_mouse_hit_uses_rendered_start_after_resize(monkeypatch):
    window_ref = "pane-window"
    rows = [_pane(f"%{index}", target=f"pane-target-{index}", pane_index=index,
                  window_ref=window_ref, window_id="@1") for index in range(20)]
    tower = _mouse_tower(rows)
    target_identity = control_room.pane_identity(rows[-1])
    keys = iter(["\t"] + [curses.KEY_DOWN] * (len(rows) - 1)
                + [curses.KEY_RESIZE, curses.KEY_MOUSE, "q"])

    _mouse_driven_room(
        monkeypatch, tower, keys, target_kind="pane",
        target=(window_ref, target_identity), resize_to=(24, 80),
    )

    assert tower.selected == len(rows) - 1


def test_mouse_click_after_window_change_selects_first_pane_of_new_window(monkeypatch):
    first = _pane("%1", target="first", window_ref="window-first", window_id="@1")
    second = _pane("%2", target="second", window_ref="window-second", window_id="@2",
                   window_name="Second")
    rows = [first, second]
    tower = _mouse_tower(rows)
    screen = Screen(40, 120)
    state = {}
    mouse_event = {}
    original_draw = control_room.draw

    def capture_draw(*args, **kwargs):
        result = original_draw(*args, **kwargs)
        state.clear()
        state.update(kwargs["hit_test_state"])
        return result

    events = iter([curses.KEY_MOUSE, "\t", curses.KEY_MOUSE, "q"])
    click_count = 0

    def read_key(_screen):
        nonlocal click_count
        key = next(events)
        if key == curses.KEY_MOUSE:
            click_count += 1
            if click_count == 1:
                nav = control_room.window_navigation_lines(tower, rows, first["window_ref"])
                index = next(i for i, line in enumerate(nav)
                             if line.get("window_ref") == second["window_ref"])
                region = state["nav_region"]
                y = region[1] + index - state["nav_start"]
                mouse_event["value"] = (0, region[0], y, 0, curses.BUTTON1_CLICKED)
            else:
                region = state["pane_region"]
                mouse_event["value"] = (0, region[0], region[1], 0, curses.BUTTON1_CLICKED)
        return key

    monkeypatch.setattr(control_room, "draw", capture_draw)
    monkeypatch.setattr(control_room, "read_key", read_key)
    monkeypatch.setattr(control_room.curses, "getmouse", lambda: mouse_event["value"])
    monkeypatch.setattr(control_room, "_enable_room_mouse", lambda: 0)
    monkeypatch.setattr(control_room, "_restore_room_mouse", lambda _mask: None)
    monkeypatch.setattr(control_room.live_view, "_start_refresh", lambda _tower: False)
    monkeypatch.setattr(control_room.live_view.curses, "doupdate", lambda: None)
    monkeypatch.setattr(control_room.live_view, "_draw_tile", lambda *_args: None)

    control_room.open_control_room(screen, tower)

    assert tower.selected == 1


def test_same_window_name_on_different_hosts_clicks_exact_window_identity(monkeypatch):
    main = _pane("%1", target="main-pane", window_ref="main-ref", window_id="@1",
                 window_name="Shared", tmux_host="mainpc", execution_host="mainpc")
    remote = _pane("%1", target="remote-pane", window_ref="remote-ref", window_id="@1",
                   window_name="Shared", tmux_host="asus", execution_host="asus",
                   remote=True, transport="remote_tmux", transport_target="asus")
    tower = _mouse_tower([main, remote])

    _mouse_driven_room(monkeypatch, tower, iter([curses.KEY_MOUSE, "q"]),
                       target_kind="window", target="remote-ref")

    assert tower.selected == 1


def test_room_mouse_mask_is_restored_and_only_single_click_is_actionable(monkeypatch):
    calls = []

    def mousemask(mask):
        calls.append(mask)
        return (mask, 0x1234) if len(calls) == 1 else (mask, 0)

    monkeypatch.setattr(control_room.curses, "mousemask", mousemask)
    previous = control_room._enable_room_mouse()
    control_room._restore_room_mouse(previous)
    assert previous == 0x1234
    assert calls == [curses.BUTTON1_CLICKED, 0x1234]

    monkeypatch.setattr(control_room.curses, "getmouse", lambda: (0, 0, 2, 0, curses.BUTTON1_PRESSED))
    assert control_room._read_room_mouse(None, curses.KEY_MOUSE) is None
    monkeypatch.setattr(control_room.curses, "getmouse", lambda: (0, 0, 2, 0, curses.BUTTON1_DOUBLE_CLICKED))
    assert control_room._read_room_mouse(None, curses.KEY_MOUSE) is None
    monkeypatch.setattr(control_room.curses, "getmouse", lambda: (0, 0, 2, 0, curses.BUTTON1_CLICKED))
    assert control_room._read_room_mouse(None, curses.KEY_MOUSE) == (0, 2)


def test_control_room_restores_preexisting_curses_mouse_mask_on_exit(monkeypatch):
    row = _pane("%3", pid="3003")
    tower = _mouse_tower([row])
    screen = Screen(24, 80, ["q"])
    calls = []

    def mousemask(mask):
        calls.append(mask)
        return curses.BUTTON1_CLICKED, 0x2468

    monkeypatch.setattr(control_room.curses, "mousemask", mousemask)
    monkeypatch.setattr(control_room.live_view, "_start_refresh", lambda _tower: False)
    monkeypatch.setattr(control_room.live_view.curses, "doupdate", lambda: None)

    control_room.open_control_room(screen, tower)

    assert calls == [curses.BUTTON1_CLICKED, 0x2468]


def test_mouse_handoff_cancel_keeps_project_and_saved_screen_catalogs(monkeypatch):
    row = _pane("%8", target="cancel-target", pid="8008", window_ref="window-cancel")
    project_data = [{"project_id": "keep", "display_name": "Keep", "window_refs": ["window-cancel"]}]
    screen_data = [{"screen_id": "saved", "name": "Keep screen"}]
    project_store = SimpleNamespace(
        all=lambda: project_data, hidden_refs=lambda: set(), memberships=lambda: {},
    )

    class ScreenStore:
        corrupt = False

        def last_view(self):
            return None

        def screens(self):
            return screen_data

        def templates(self):
            return []

        def save_last_view(self, *_args):
            return None

    tower = _mouse_tower([row])
    tower.projects = project_store
    tower.control_screens = ScreenStore()
    project_before, screens_before = list(project_data), list(screen_data)

    _mouse_driven_room(monkeypatch, tower, iter([curses.KEY_MOUSE, "q"]),
                       target_kind="right", target=None)

    assert project_data == project_before
    assert screen_data == screens_before


def test_hidden_and_disconnected_project_windows_remain_manageable_without_rebinding():
    old_ref, new_ref = "c" * 64, "d" * 64
    replacement = _pane("%1", target="replacement", window_ref=new_ref,
                        tmux_host="mainpc", execution_host="mainpc", window_id="@1")
    project = {
        "project_id": "project-2", "display_name": "Stale project",
        "window_refs": [old_ref], "order": 0, "collapsed": False,
    }
    hidden_lines = control_room.navigation_lines(
        [replacement], "%1", control_room.pane_identity(replacement),
        projects=[project], window_assets=[{"window_ref": new_ref, "display_name": "New"}],
    )

    stale = next(line for line in hidden_lines if line["kind"] == "project_window_disconnected")
    assert stale["window_ref"] == old_ref
    assert stale["member_rows"] == []
    assert not any(line.get("row") is replacement and line.get("project_id") for line in hidden_lines)


def test_project_selection_never_expands_center_or_right_into_other_windows(monkeypatch):
    main_ref, asus_ref = "e" * 64, "f" * 64
    main = _pane("%1", target="main-target", window_ref=main_ref,
                 tmux_host="mainpc", execution_host="mainpc", live_lines=["MAIN_ONLY"])
    asus = _pane("%1", target="asus-target", window_ref=asus_ref,
                 tmux_host="asus", execution_host="asus", remote=True,
                 transport="remote_tmux", transport_target="asus", live_lines=["ASUS_ONLY"])
    project = {
        "project_id": "project-3", "display_name": "Cross host",
        "window_refs": [asus_ref, main_ref], "order": 0, "collapsed": False,
    }
    tower = SimpleNamespace(
        rows=[main, asus], window_assets=[],
        projects=SimpleNamespace(all=lambda: [project], hidden_refs=lambda: set()),
    )
    screen = Screen(40, 120)
    drawn = []
    monkeypatch.setattr(live_view.curses, "doupdate", lambda: None)
    monkeypatch.setattr(
        live_view, "_draw_tile",
        lambda _screen, _tower, row, *_args: drawn.append(row["target_id"]),
    )

    result = control_room.draw(
        screen, tower, main["key"], control_room.pane_identity(main), main,
        cursor_key=("tower_project", "project-3"),
    )

    assert result is True
    assert drawn == ["main-target"]


def test_final_draw_uses_project_navigation_model_and_hides_empty_project_section(monkeypatch):
    from tmux_agent_tower.i18n import t

    visible_ref, hidden_ref, stale_ref = "a" * 64, "b" * 64, "c" * 64
    visible = _pane("%1", target="visible", window_ref=visible_ref)
    base_tower = SimpleNamespace(
        rows=[visible], window_assets=[],
        projects=SimpleNamespace(all=lambda: [], hidden_refs=lambda: set()),
    )
    screen = Screen(40, 120)
    monkeypatch.setattr(live_view.curses, "doupdate", lambda: None)
    monkeypatch.setattr(live_view, "_draw_tile", lambda *_args: None)

    control_room.draw(screen, base_tower, visible["key"], control_room.pane_identity(visible), visible)
    assert t("control_room.projects_root") not in [text for _y, _x, text, _attr in screen.writes]

    project = {
        "project_id": "saved-project", "display_name": "실제 저장 프로젝트",
        "window_refs": [hidden_ref, visible_ref, stale_ref], "order": 0, "collapsed": False,
    }
    tower = SimpleNamespace(
        rows=[visible],
        window_assets=[
            {"window_ref": visible_ref, "display_name": "현재 창", "tmux_host": "mainpc"},
            {"window_ref": hidden_ref, "display_name": "숨긴 창", "tmux_host": "asus"},
        ],
        projects=SimpleNamespace(all=lambda: [project], hidden_refs=lambda: {hidden_ref}),
    )
    cursor = ("tower_project_window", "saved-project", stale_ref)

    control_room.draw(
        screen, tower, visible["key"], control_room.pane_identity(visible), visible,
        cursor_key=cursor,
    )

    drawn_text = "\n".join(text for _y, _x, text, _attr in screen.writes)
    assert "실제 저장 프로젝트" in drawn_text
    assert "숨긴 창" in drawn_text
    assert "연결 안 됨" in drawn_text


@pytest.mark.parametrize("height,width", [(18, 60), (24, 80), (40, 120)])
def test_project_tree_and_live_panels_fit_required_viewports(monkeypatch, height, width):
    ref = "8" * 64
    pane = _pane("%8", window_ref=ref, display_name="한글과 English 이름을 유지하는 작업",
                 live_lines=["READ_ONLY_SNAPSHOT"])
    project = {"project_id": "project-viewport", "display_name": "장기 프로젝트",
               "window_refs": [ref], "order": 0, "collapsed": False}
    asset = {"window_ref": ref, "display_name": "개발 화면", "tmux_host": "mainpc",
             "execution_host": "mainpc"}
    tower = SimpleNamespace(
        rows=[pane], window_assets=[asset],
        projects=SimpleNamespace(all=lambda: [project], hidden_refs=lambda: set()),
    )
    screen = Screen(height, width)
    monkeypatch.setattr(live_view.curses, "doupdate", lambda: None)
    monkeypatch.setattr(live_view, "_draw_tile", lambda *_args: None)

    assert control_room.draw(
        screen, tower, pane["key"], control_room.pane_identity(pane), pane,
        cursor_key=("tower_project", "project-viewport"),
    ) is True
    assert all(0 <= y < height and 0 <= x < width for y, x, _text, _attr in screen.writes)
    assert all(x + _visible_width(text) <= width for _y, x, text, _attr in screen.writes)


def test_project_window_visibility_excludes_project_panes_but_keeps_management_action():
    hidden_ref, visible_ref = "1" * 64, "2" * 64
    hidden = _pane("%1", target="hidden", window_ref=hidden_ref)
    visible = _pane("%2", target="visible", window_ref=visible_ref)
    project = {
        "project_id": "project-4", "display_name": "Hidden work",
        "window_refs": [hidden_ref, visible_ref], "order": 0, "collapsed": False,
    }
    lines = control_room.navigation_lines(
        [visible], "%2", control_room.pane_identity(visible),
        projects=[project], hidden_refs={hidden_ref},
    )

    assert any(line["kind"] == "project_window_hidden" and line["window_ref"] == hidden_ref for line in lines)
    assert not any(line.get("row") is hidden for line in lines)
    assert [key for key, _ in control_room.action_menu_items(
        visible, project_management=True,
    )][-4:] == ["projects", "details", "settings", "agent_handoff"]


def test_keyboard_window_navigation_keeps_cross_host_pane_identity(monkeypatch):
    main_ref, asus_ref = "3" * 64, "4" * 64
    main = _pane("%1", target="main-target", window_ref=main_ref,
                 tmux_host="mainpc", execution_host="mainpc", live_lines=["MAIN_ONLY"])
    asus = _pane("%1", target="asus-target", window_ref=asus_ref,
                 tmux_host="asus", execution_host="asus", remote=True,
                 transport="remote_tmux", transport_target="asus", live_lines=["ASUS_ONLY"])
    tower = SimpleNamespace(
        rows=[main, asus], visible_rows=[main, asus], selected=0,
        window_assets=[
            {"window_ref": main_ref, "display_name": "Main", "tmux_host": "mainpc"},
            {"window_ref": asus_ref, "display_name": "ASUS", "tmux_host": "asus"},
        ],
        projects=SimpleNamespace(all=lambda: [], hidden_refs=lambda: set()),
        work_groups=SimpleNamespace(all=lambda: []),
        start_background_refresh=lambda: False, _apply_filter=lambda: None,
    )
    screen = Screen(40, 120, [curses.KEY_DOWN, curses.KEY_DOWN, "\t", "\t", "q"])
    monkeypatch.setattr(live_view.curses, "doupdate", lambda: None)
    monkeypatch.setattr(control_room.live_view, "_start_refresh", lambda _tower: False)
    selected_tiles = []
    monkeypatch.setattr(
        live_view, "_draw_tile",
        lambda _screen, _tower, row, _index, _y, x, *_args: selected_tiles.append(
            (row["target_id"], x)
        ),
    )

    control_room.open_control_room(screen, tower)

    right_x = control_room.panel_regions(120, 40)[2][0]
    assert selected_tiles[-1] == ("asus-target", right_x)
    assert tower.selected == 1


def test_hidden_and_disconnected_project_windows_accept_arrow_enter_and_space(monkeypatch):
    from tmux_agent_tower.ui import projects as project_ui

    hidden_ref, visible_ref, stale_ref = "5" * 64, "6" * 64, "7" * 64
    visible = _pane("%2", target="visible", window_ref=visible_ref)
    hidden = _pane("%1", target="hidden", window_ref=hidden_ref)
    project = {
        "project_id": "hidden-project", "display_name": "Hidden project",
        "window_refs": [hidden_ref, stale_ref, visible_ref], "order": 0, "collapsed": False,
    }
    tower = SimpleNamespace(
        rows=[visible, hidden], visible_rows=[visible], selected=0,
        window_assets=[
            {"window_ref": hidden_ref, "display_name": "숨긴 창", "tmux_host": "asus"},
            {"window_ref": visible_ref, "display_name": "현재 창", "tmux_host": "mainpc"},
        ],
        projects=SimpleNamespace(all=lambda: [project], hidden_refs=lambda: {hidden_ref}),
        work_groups=SimpleNamespace(all=lambda: []),
        start_background_refresh=lambda: False,
    )
    # From the physical pane, three Down presses land on the project's hidden
    # Window. Enter toggles its view-local expansion; Space opens its editor.
    screen = Screen(40, 120, [curses.KEY_DOWN, curses.KEY_DOWN, curses.KEY_DOWN, "\n", " ", "q"])
    opened = []
    monkeypatch.setattr(live_view.curses, "doupdate", lambda: None)
    monkeypatch.setattr(control_room.live_view, "_start_refresh", lambda _tower: False)
    monkeypatch.setattr(live_view, "_draw_tile", lambda *_args: None)
    monkeypatch.setattr(
        project_ui, "open_project_editor",
        lambda _screen, _tower, project_id: opened.append(project_id),
    )
    monkeypatch.setattr(
        control_room, "run_list_picker",
        lambda *_args, **_kwargs: SimpleNamespace(cancelled=False, selected_key="edit_project"),
    )

    control_room.open_control_room(screen, tower)

    assert opened == ["hidden-project"]


def test_space_on_project_opens_its_editor_and_delete_can_be_cancelled(monkeypatch, tmp_path):
    from tmux_agent_tower.state.projects import ProjectStore
    from tmux_agent_tower.ui import projects as project_ui

    store = ProjectStore(tmp_path / "projects.json")
    project = store.create("Keep me")
    tower = SimpleNamespace(projects=store, window_assets=[], folders=SimpleNamespace(window=lambda _ref: {}))
    opened = []
    offered = []
    monkeypatch.setattr(project_ui, "open_project_editor", lambda _s, _t, project_id: opened.append(project_id))
    monkeypatch.setattr(
        control_room, "run_list_picker",
        lambda _s, _title, items, **_kwargs: (
            offered.extend(key for key, _label in items)
            or SimpleNamespace(cancelled=False, selected_key="edit_project")
        ),
    )

    control_room._open_action_menu(
        None, tower, None, [], None, "tower_project",
        {"kind": "tower_project", "project_id": project["project_id"]},
    )

    assert opened == [project["project_id"]]
    assert "screens" in offered and "edit_project" in offered

    monkeypatch.setattr(
        project_ui, "run_list_picker",
        lambda *_args, **_kwargs: SimpleNamespace(cancelled=False, selected_key="delete"),
    )
    monkeypatch.setattr(project_ui, "_confirm", lambda *_args: False)
    project_ui.open_project_editor(None, tower, project["project_id"])
    assert store.all()[0]["project_id"] == project["project_id"]


def test_project_creation_from_selected_pane_stores_only_logical_window_ref(monkeypatch, tmp_path):
    from tmux_agent_tower.state.projects import ProjectStore
    from tmux_agent_tower.ui import projects as project_ui

    ref = "9" * 64
    source = {"window_ref": ref, "window_id": "@9", "pane_id": "%9", "pane_pid": "900"}
    physical_before = dict(source)
    store = ProjectStore(tmp_path / "projects.json")
    tower = SimpleNamespace(projects=store, folders=SimpleNamespace(window=lambda _ref: {}),
                            _apply_filter=lambda: None)
    monkeypatch.setattr(project_ui, "prompt_text", lambda *_args, **_kwargs: "Release")
    monkeypatch.setattr(project_ui, "open_project_editor", lambda *_args: None)

    project_ui.create_project(None, tower, source)

    assert store.all()[0]["window_refs"] == [ref]
    assert source == physical_before


def test_opening_from_work_group_keeps_selected_project_member_as_focus():
    first = _pane("%1", target="first")
    member = _pane("%2", target="member", work_group_id="g1")
    rows = [first, member]
    groups = [{"group_id": "g1", "member_order": ["member"]}]

    selected = control_room.initial_pane(
        rows, {"kind": "work_group", "group_id": "g1"}, groups,
    )

    assert selected is member


def test_reused_pane_id_with_new_pid_is_stale_and_old_output_is_hidden():
    old = _pane("%9", pid="900", live_lines=["old body"])
    reused = _pane("%9", pid="901", live_lines=["new body"])

    selected, current = control_room._resolve_selection(
        [reused], "%9", control_room.pane_identity(old), old,
    )

    assert current is False
    assert selected["_live_stale"] is True
    assert selected["live_lines"] == []
    assert selected["pane_pid"] == "900"


@pytest.mark.parametrize("height,width", [(18, 60), (24, 80), (40, 100), (40, 120)])
def test_fake_viewports_render_window_panel_without_fresh_capture(monkeypatch, height, width):
    rows = [_pane("%1", pane_index=0), _pane("%2", pane_index=1)]
    selected = rows[1]
    tower = SimpleNamespace(rows=rows)
    screen = Screen(height, width)
    monkeypatch.setattr(live_view.curses, "doupdate", lambda: None)
    monkeypatch.setattr(control_room.curses, "has_colors", lambda: False)
    monkeypatch.setattr(
        live_view, "get_pane_screen",
        lambda *_args, **_kwargs: pytest.fail("drawing must reuse the refresh snapshot"),
    )

    assert control_room.draw(
        screen, tower, selected["key"], control_room.pane_identity(selected), selected,
    ) is True

    frame = "\n".join(text for _y, _x, text, _attr in screen.writes)
    assert "폴더 · Window" in frame
    if width >= 110 and height >= 22:
        assert "선택 Window의 Pane" in frame
        assert "선택 Pane LIVE" in frame
        assert "Task %2" in frame
        assert "한국어 출력" in frame
    else:
        assert "Tab/Shift+Tab" in frame
        assert "Task %2" not in frame
        assert "한국어 출력" not in frame
    assert "Shift+Tab" in frame
    assert "Space" in frame
    assert "Y 최신 답변 복사" not in frame  # Footer guidance is contextual; Y remains available.
    assert all(0 <= y < height and 0 <= x < width for y, x, _text, _attr in screen.writes)
    assert all(x + _visible_width(text) <= width for _y, x, text, _attr in screen.writes)


def test_wide_view_uses_three_columns_but_narrow_view_reuses_one_panel():
    wide = control_room.panel_regions(120, 40)
    short = control_room.panel_regions(120, 18)

    assert len(wide) == len(short) == 3
    assert len({region[1] for region in wide}) == 1
    assert short[0] == short[1] == short[2]


@pytest.mark.parametrize(
    "panel_width,panel_height,screen_width,expected",
    [(58, 4, 60, 2), (78, 6, 80, 2), (98, 11, 100, 4), (48, 36, 120, 2)],
)
def test_grid_columns_adapt_to_viewport_without_collapsing_tile_rows(
    panel_width, panel_height, screen_width, expected,
):
    assert control_room.grid_columns(panel_width, panel_height, screen_width, 4) == expected


def test_remote_peer_live_is_reported_unavailable_without_capture(monkeypatch):
    remote = _pane("%2", remote=True, tmux_host="asus", execution_host="asus",
                   transport="remote_tmux", transport_target="asus", live_lines=[])
    tower = SimpleNamespace(rows=[remote])
    screen = Screen(24, 80)
    monkeypatch.setattr(live_view.curses, "doupdate", lambda: None)
    monkeypatch.setattr(
        live_view, "get_pane_screen",
        lambda *_args, **_kwargs: pytest.fail("remote peer is metadata-only"),
    )

    assert control_room.draw(
        screen, tower, "%2", control_room.pane_identity(remote), remote, active_panel=2,
    ) is True
    frame = "\n".join(text for _y, _x, text, _attr in screen.writes)
    assert "실시간 화면을 사용할 수 없습니다." in frame


def test_arrow_selection_syncs_identity_and_y_reuses_existing_copy_handler(monkeypatch):
    from tmux_agent_tower.ui import control_view

    first, second = _pane("%1"), _pane("%2", pane_index=1)

    class Tower:
        rows = [first, second]
        visible_rows = [first, second]
        selected = 0
        session = "work"
        own_pane_id = ""
        work_groups = SimpleNamespace(all=lambda: [])

        def start_background_refresh(self):
            return False

        def apply_background_refresh(self):
            return False

    tower = Tower()
    screen = Screen(24, 80, ["\t", curses.KEY_DOWN, "y", "q"])
    copied = []
    monkeypatch.setattr(control_room.live_view, "_start_refresh", lambda _tower: False)
    monkeypatch.setattr(live_view.curses, "doupdate", lambda: None)
    monkeypatch.setattr(control_view, "_copy_result", lambda _tower, key, _screen: copied.append(key) or "copied")
    monkeypatch.setattr(control_room, "show_message_screen", lambda *_args: None)

    control_room.open_control_room(screen, tower)

    assert copied == ["%2"]
    assert tower.selected == 1
    assert screen.timeouts[-1] == 200


def test_y_is_refused_after_refresh_reuses_selected_pane_id(monkeypatch):
    from tmux_agent_tower.ui import control_view

    old = _pane("%8", pid="800", live_lines=["old output"])
    replacement = _pane("%8", pid="801", live_lines=["new output"])

    class Tower:
        rows = [old]
        visible_rows = [old]
        selected = 0
        session = "work"
        own_pane_id = ""
        work_groups = SimpleNamespace(all=lambda: [])

        def start_background_refresh(self):
            return False

        def apply_background_refresh(self):
            self.rows = [replacement]
            return True

    tower = Tower()
    screen = Screen(24, 80, ["\t", "y", "q"])
    copied = []
    notices = []
    monkeypatch.setattr(control_room.live_view, "_start_refresh", lambda _tower: False)
    monkeypatch.setattr(live_view.curses, "doupdate", lambda: None)
    monkeypatch.setattr(control_view, "_copy_result", lambda *_args: copied.append(True) or "copied")
    monkeypatch.setattr(control_room, "show_message_screen", lambda _s, _title, lines: notices.extend(lines))

    control_room.open_control_room(screen, tower)

    assert copied == []
    assert notices == ["작업을 확인할 수 없습니다 · 이전 내용 숨김"]


def test_tab_and_shift_tab_switch_panels_without_changing_selected_pane_identity(monkeypatch):
    row = _pane("%1")

    class Tower:
        rows = [row]
        visible_rows = [row]
        selected = 0
        work_groups = SimpleNamespace(all=lambda: [])

        def start_background_refresh(self):
            return False

    screen = Screen(24, 80, ["\t", getattr(curses, "KEY_BTAB", -1), "q"])
    original_draw = control_room.draw
    observed = []

    def track(*args, **kwargs):
        observed.append((kwargs.get("active_panel"), kwargs.get("cursor_key")))
        return original_draw(*args, **kwargs)

    monkeypatch.setattr(control_room, "draw", track)
    monkeypatch.setattr(live_view.curses, "doupdate", lambda: None)
    monkeypatch.setattr(control_room.live_view, "_start_refresh", lambda _tower: False)

    control_room.open_control_room(screen, Tower())

    assert [panel for panel, _cursor in observed] == [0, 1, 0]
    assert len({cursor for _panel, cursor in observed}) == 1


def test_active_panel_is_reverse_highlighted_and_right_arrows_scroll_snapshot_only(monkeypatch):
    row = _pane("%6", live_lines=[f"LINE_{index}" for index in range(40)])
    tower = SimpleNamespace(rows=[row])
    screen = Screen(40, 120)
    monkeypatch.setattr(live_view.curses, "doupdate", lambda: None)
    monkeypatch.setattr(control_room.curses, "has_colors", lambda: False)
    assert control_room.draw(
        screen, tower, "%6", control_room.pane_identity(row), row,
        active_panel=1,
        cursor_key=("pane", control_room.pane_identity(row)),
    ) is True
    center_x = control_room.panel_regions(120, 40)[1][0]
    center_heading = next(item for item in screen.writes if item[0] == 2 and item[1] == center_x)
    assert center_heading[3] & curses.A_REVERSE

    tower = SimpleNamespace(
        rows=[row], visible_rows=[row], selected=0,
        work_groups=SimpleNamespace(all=lambda: []),
        start_background_refresh=lambda: False,
    )
    screen = Screen(24, 80, ["\t", "\t", curses.KEY_UP, "q"])
    observed_scrolls = []
    original_tile = live_view._draw_tile

    def track_tile(*args, **kwargs):
        observed_scrolls.append(args[-1] if len(args) >= 9 else kwargs.get("scroll", 0))
        return original_tile(*args, **kwargs)

    monkeypatch.setattr(live_view, "_draw_tile", track_tile)
    monkeypatch.setattr(control_room.live_view, "_start_refresh", lambda _tower: False)
    control_room.open_control_room(screen, tower)

    assert observed_scrolls[-1] == 1


def test_center_selection_updates_the_read_only_focus_panel(monkeypatch):
    first = _pane("%1", live_lines=["LIVE_FIRST_ONLY"])
    second = _pane("%2", pane_index=1, live_lines=["LIVE_SECOND_ONLY"])
    tower = SimpleNamespace(
        rows=[first, second], visible_rows=[first, second], selected=0,
        work_groups=SimpleNamespace(all=lambda: []),
        start_background_refresh=lambda: False,
    )
    screen = Screen(40, 120, ["\t", curses.KEY_DOWN, "q"])
    monkeypatch.setattr(live_view.curses, "doupdate", lambda: None)
    monkeypatch.setattr(control_room.live_view, "_start_refresh", lambda _tower: False)

    control_room.open_control_room(screen, tower)

    right_x = control_room.panel_regions(120, 40)[2][0]
    right_text = "\n".join(text for _y, x, text, _attr in screen.writes if x >= right_x)
    assert "LIVE_SECOND_ONLY" in right_text
    assert "LIVE_FIRST_ONLY" not in right_text


def test_left_window_navigation_does_not_expose_work_group_panes(monkeypatch):
    first = _pane("%1", target="one", work_group_id="g1", work_group_name="Saved group")
    second = _pane("%2", target="two", pane_index=1, work_group_id="g1", work_group_name="Saved group")
    group = {"group_id": "g1", "member_order": ["one", "two"], "layout": "split-2"}
    before = dict(group)
    tower = SimpleNamespace(
        rows=[first, second], visible_rows=[first, second], selected=0,
        work_groups=SimpleNamespace(all=lambda: [group]),
        start_background_refresh=lambda: False,
    )
    screen = Screen(24, 80, ["\t", "q"])
    frames = []
    original_draw = control_room.draw

    def track(*args, **kwargs):
        current = original_draw(*args, **kwargs)
        frames.append((kwargs.get("cursor_key"), set(kwargs.get("collapsed") or ())))
        return current

    monkeypatch.setattr(control_room, "draw", track)
    monkeypatch.setattr(live_view.curses, "doupdate", lambda: None)
    monkeypatch.setattr(control_room.live_view, "_start_refresh", lambda _tower: False)

    control_room.open_control_room(screen, tower)

    assert not any(line["kind"] == "pane" for line in control_room.window_navigation_lines(
        tower, [first, second], ""
    ))
    assert tower.selected == 0
    assert group == before


def test_space_menu_reopens_existing_saved_group_window_layout_and_settings_routes(monkeypatch):
    from tmux_agent_tower.ui import control_view

    row = _pane("%2", target="target-2", work_group_id="g1", window_ref="mainpc/work/@1")
    group = {
        "group_id": "g1", "member_order": ["target-2"], "layout": "main-plus-side",
        "layout_slots": {"target-2": "main"},
    }
    tower = SimpleNamespace(rows=[row], work_groups=SimpleNamespace(all=lambda: [group]))
    items = control_room.action_menu_items(row, has_group=True)
    assert [key for key, _label in items] == [
        "screens", "group_live", "window_live", "layout_live", "copy", "details", "settings",
        "agent_handoff",
    ]

    routed = []
    monkeypatch.setattr(live_view, "open_group_live_view", lambda _s, _t, value: routed.append(("group", value)))
    monkeypatch.setattr(live_view, "open_window_live_view", lambda _s, _t, ref: routed.append(("window", ref)))
    monkeypatch.setattr(live_view, "open_live_view", lambda _s, _t: routed.append(("layout", None)))
    monkeypatch.setattr(control_view, "_copy_result", lambda _t, key, _s: routed.append(("copy", key)) or "copied")
    monkeypatch.setattr(control_room, "show_message_screen", lambda *_args: None)
    monkeypatch.setattr("tmux_agent_tower.ui.settings_menu.open_settings", lambda _s, _t: routed.append(("settings", None)))
    monkeypatch.setattr(
        control_room, "run_list_picker",
        lambda _s, _title, choices, **_kw: SimpleNamespace(cancelled=False, selected_key="group_live"),
    )

    control_room._open_action_menu(None, tower, row, [group], None, "pane")
    control_room._run_action(None, tower, "window_live", row)
    control_room._run_action(None, tower, "layout_live", row)
    control_room._run_action(None, tower, "settings", row)

    assert routed == [
        ("group", group), ("window", "mainpc/work/@1"), ("layout", None), ("settings", None),
    ]


def test_space_key_opens_visible_one_step_menu_for_selected_window_live(monkeypatch):
    row = _pane("%5", window_ref="mainpc/work/@4")
    tower = SimpleNamespace(
        rows=[row], visible_rows=[row], selected=0,
        work_groups=SimpleNamespace(all=lambda: []),
        start_background_refresh=lambda: False,
    )
    screen = Screen(24, 80, ["\t", " ", "q"])
    picked = []
    routed = []
    monkeypatch.setattr(live_view.curses, "doupdate", lambda: None)
    monkeypatch.setattr(control_room.live_view, "_start_refresh", lambda _tower: False)
    monkeypatch.setattr(
        control_room, "run_list_picker",
        lambda _s, _title, items, **_kw: (
            picked.extend(key for key, _label in items)
            or SimpleNamespace(cancelled=False, selected_key="window_live")
        ),
    )
    monkeypatch.setattr(
        live_view, "open_window_live_view",
        lambda _s, _tower, ref: routed.append(ref),
    )

    control_room.open_control_room(screen, tower)

    assert "window_live" in picked
    assert routed == ["mainpc/work/@4"]


def test_menu_result_copy_reuses_existing_handler_and_remote_menu_has_no_input_action(monkeypatch):
    from tmux_agent_tower.ui import control_view

    remote = _pane("%3", target="remote-3", remote=True, tmux_host="asus",
                   execution_host="asus", transport="remote_tmux", transport_target="asus")
    tower = SimpleNamespace(rows=[remote])
    calls = []
    monkeypatch.setattr(
        control_room, "run_list_picker",
        lambda _s, _title, _items, **_kw: SimpleNamespace(cancelled=False, selected_key="copy"),
    )
    monkeypatch.setattr(control_view, "_copy_result", lambda _t, key, _s: calls.append(key) or "copied")
    monkeypatch.setattr(control_room, "show_message_screen", lambda *_args: None)

    control_room._open_action_menu(None, tower, remote, [], None, "pane")

    remote_keys = [key for key, _label in control_room.action_menu_items(remote)]
    assert calls == ["%3"]
    assert "layout_live" not in remote_keys
    assert "window_live" not in remote_keys
    assert "agent_handoff" not in remote_keys
    assert "agent_handoff_unavailable" in remote_keys
    assert "send_prompt" not in remote_keys
    assert "terminal_input" not in remote_keys


def test_duplicate_pane_ids_on_different_hosts_keep_selection_identity_and_block_ambiguous_copy(monkeypatch):
    from tmux_agent_tower.ui import control_view

    local = _pane("%1", target="local-target", live_lines=["LOCAL_ONLY"])
    remote = _pane("%1", target="remote-target", tmux_host="asus", execution_host="asus",
                   remote=True, transport="remote_tmux", transport_target="asus",
                   live_lines=["REMOTE_ONLY"])
    selected, current = control_room._resolve_selection(
        [local, remote], "%1", control_room.pane_identity(remote), remote,
    )

    assert current is True
    assert selected is remote
    assert control_room._copy_key_is_unambiguous([local, remote], remote) is False
    assert all(key != "copy" for key, _label in control_room.action_menu_items(
        remote, copy_safe=False,
    ))
    called = []
    notices = []
    monkeypatch.setattr(control_view, "_copy_result", lambda *_args: called.append(True) or "copied")
    monkeypatch.setattr(control_room, "show_message_screen", lambda _s, _title, lines: notices.extend(lines))
    control_room._run_action(
        None, SimpleNamespace(rows=[local, remote]), "copy", remote,
    )
    assert called == []
    assert notices == ["Pane 식별이 겹쳐 안전하게 복사할 수 없습니다."]


def test_navigation_keeps_sessions_windows_and_hosts_distinct():
    rows = [
        _pane("%1", target="host-a-one", session="alpha", session_id="$1", window_id="@1"),
        _pane("%2", target="host-a-two", session="beta", session_id="$2", window_id="@2"),
        _pane("%1", target="host-b-one", session="alpha", session_id="$1", window_id="@1",
              tmux_host="asus", execution_host="asus", remote=True,
              transport="remote_tmux", transport_target="asus"),
    ]

    lines = control_room.navigation_lines(rows, "%1", control_room.pane_identity(rows[0]))

    assert sum(line["kind"] == "host" for line in lines) == 2
    assert sum(line["kind"] == "session" for line in lines) == 3
    assert sum(line["kind"] == "window" for line in lines) == 3
    panes = [line for line in lines if line["kind"] == "pane"]
    assert [line["identity"] for line in panes] == [control_room.pane_identity(row) for row in rows]
    assert panes[0]["selected"] is True
    assert panes[2]["selected"] is False


def test_enter_on_pane_details_is_metadata_only():
    row = _pane("%9", live_lines=["PRIVATE_TERMINAL_BODY"])

    lines = control_room.pane_detail_lines(row)

    assert any("Task %9" in line for line in lines)
    assert any("mainpc" in line for line in lines)
    assert not any("PRIVATE_TERMINAL_BODY" in line for line in lines)


def test_enter_from_window_panel_does_not_send_agent_input(monkeypatch):
    from tmux_agent_tower.ui import control_view

    row = _pane("%8", live_lines=["PRIVATE_AGENT_OUTPUT"])
    tower = SimpleNamespace(
        rows=[row], visible_rows=[row], selected=0,
        work_groups=SimpleNamespace(all=lambda: []),
        start_background_refresh=lambda: False,
    )
    screen = Screen(24, 80, ["\n", "q"])
    shown = []
    monkeypatch.setattr(live_view.curses, "doupdate", lambda: None)
    monkeypatch.setattr(control_room.live_view, "_start_refresh", lambda _tower: False)
    monkeypatch.setattr(
        control_room, "show_message_screen",
        lambda _s, title, lines: shown.append((title, list(lines))),
    )
    monkeypatch.setattr(
        control_view, "open_control_view",
        lambda *_args, **_kwargs: pytest.fail("Control Room Enter must not open Agent input"),
    )

    control_room.open_control_room(screen, tower)

    assert not shown
    assert not any("PRIVATE_AGENT_OUTPUT" in line for _title, lines in shown for line in lines)


def test_group_header_scope_uses_saved_member_order_without_changing_store():
    rows = [
        _pane("%1", target="first", work_group_id="g1", work_group_name="Saved", pane_index=0),
        _pane("%2", target="second", work_group_id="g1", work_group_name="Saved", pane_index=1),
    ]
    group = {"group_id": "g1", "member_order": ["second", "first"], "layout": "grid-4"}
    before = {"group_id": "g1", "member_order": ["second", "first"], "layout": "grid-4"}
    node = next(
        line for line in control_room.navigation_lines(rows, "%1", control_room.pane_identity(rows[0]))
        if line["kind"] == "project"
    )

    scoped = control_room._scope_grid_rows(rows[0], rows, [group], node)

    assert [row["target_id"] for row in scoped] == ["second", "first"]
    assert group == before


def test_window_navigation_is_folder_first_and_never_duplicates_panes_or_cross_host_keys(tmp_path):
    from tmux_agent_tower.state.folders import FolderStore
    from tmux_agent_tower.state.projects import ProjectStore

    local_ref, peer_ref = "a" * 64, "b" * 64
    local = _pane("%1", target="local-1", window_ref=local_ref,
                  transport="ssh", transport_target="asus", auto_agent="Codex")
    peer = _pane("%1", target="peer-1", window_ref=peer_ref, remote=True,
                 tmux_host="asus", execution_host="asus", transport="remote_tmux",
                 transport_target="asus", auto_agent="OpenCode")
    folders = FolderStore(tmp_path / "folders.json")
    folder = folders.create("업무 폴더")
    folders.move_window(local_ref, folder["folder_id"])
    projects = ProjectStore(tmp_path / "projects.json")
    project = projects.create("Remote project", [local_ref, peer_ref])
    tower = SimpleNamespace(
        rows=[local, peer], folders=folders, projects=projects,
        window_assets=[
            {"window_ref": local_ref, "display_name": "MainPC Window", "tmux_host": "mainpc"},
            {"window_ref": peer_ref, "display_name": "ASUS Window", "tmux_host": "asus", "remote": True},
        ],
    )

    lines = control_room.window_navigation_lines(tower, [local, peer])
    windows = [line for line in lines if line["kind"].startswith("window")]

    assert [line["window_ref"] for line in windows] == [local_ref, peer_ref]
    assert not any(line["kind"] == "pane" for line in lines)
    assert "MainPC SSH 창 → asus" in windows[0]["text"]
    assert "ASUS 원격 Window" in windows[1]["text"]
    assert "폴더 업무 폴더" in windows[0]["text"]
    assert project["project_id"] in [line.get("project_id") for line in windows]
    assert control_room.pane_identity(windows[0]["member_rows"][0]) != control_room.pane_identity(
        windows[1]["member_rows"][0]
    )


def test_collapsed_folder_and_project_keep_disconnected_window_references_hidden(tmp_path):
    from tmux_agent_tower.state.folders import FolderStore
    from tmux_agent_tower.state.projects import ProjectStore

    folder_ref, project_ref = "c" * 64, "d" * 64
    folders = FolderStore(tmp_path / "folders.json")
    folder = folders.create("Closed folder")
    folders.move_window(folder_ref, folder["folder_id"])
    folders.toggle_folder(folder["folder_id"])
    projects = ProjectStore(tmp_path / "projects.json")
    project = projects.create("Closed project", [project_ref])
    projects.toggle_project(project["project_id"])
    tower = SimpleNamespace(rows=[], folders=folders, projects=projects, window_assets=[])

    lines = control_room.window_navigation_lines(tower, [])

    assert not any(line.get("window_ref") in {folder_ref, project_ref} for line in lines)
    assert all(line["kind"] != "pane" for line in lines)

    projects.toggle_project(project["project_id"])
    expanded = control_room.window_navigation_lines(tower, [])
    disconnected = [line for line in expanded if line["kind"] == "window_disconnected"]
    assert [line["window_ref"] for line in disconnected] == [project_ref]


def test_window_navigation_fold_changes_use_existing_folder_and_project_stores(tmp_path):
    from tmux_agent_tower.state.folders import FolderStore
    from tmux_agent_tower.state.projects import ProjectStore

    folder_ref, project_ref = "e" * 64, "f" * 64
    folders = FolderStore(tmp_path / "folders.json")
    folder = folders.create("Folder")
    folders.move_window(folder_ref, folder["folder_id"])
    projects = ProjectStore(tmp_path / "projects.json")
    project = projects.create("Project", [project_ref])
    tower = SimpleNamespace(rows=[], folders=folders, projects=projects, window_assets=[])
    collapsed = control_room._initial_navigation_collapsed(tower)

    for kind, key, item_id in (
        ("folder_header", ("folder", folder["folder_id"]), folder["folder_id"]),
        ("project_header", ("project", project["project_id"]), project["project_id"]),
    ):
        store = folders if kind == "folder_header" else projects
        line = {"kind": kind, "node_key": key,
                "folder_id" if kind == "folder_header" else "project_id": item_id}
        assert control_room._set_navigation_collapsed(tower, line, collapsed, True)
        assert item_id in {item.get("folder_id") or item.get("project_id")
                           for item in store.all() if item.get("collapsed")}
        assert control_room._set_navigation_collapsed(tower, line, collapsed, False)
        assert item_id not in {item.get("folder_id") or item.get("project_id")
                               for item in store.all() if item.get("collapsed")}


def test_final_draw_lists_only_selected_window_panes_and_one_live_tile(monkeypatch):
    first_ref, second_ref = "c" * 64, "d" * 64
    first = _pane("%1", target="first", window_ref=first_ref,
                  display_name="창 A 작업", live_lines=["FIRST_PRIVATE_OUTPUT"])
    first_peer = _pane("%2", target="first-peer", pane_index=1, window_ref=first_ref,
                       display_name="창 A 보조", live_lines=["PEER_PRIVATE_OUTPUT"])
    second = _pane("%1", target="other-host", window_ref=second_ref,
                   tmux_host="asus", execution_host="asus", remote=True,
                   transport="remote_tmux", transport_target="asus",
                   display_name="창 B 작업", live_lines=["OTHER_WINDOW_OUTPUT"])
    tower = SimpleNamespace(rows=[first, first_peer, second], window_assets=[
        {"window_ref": first_ref, "display_name": "MainPC 창 A", "tmux_host": "mainpc"},
        {"window_ref": second_ref, "display_name": "ASUS 창 B", "tmux_host": "asus", "remote": True},
    ], projects=SimpleNamespace(all=lambda: [], hidden_refs=lambda: set()))
    screen = Screen(40, 120)
    tiles = []
    monkeypatch.setattr(live_view.curses, "doupdate", lambda: None)
    monkeypatch.setattr(live_view, "_draw_tile", lambda _s, _t, row, *_a: tiles.append(row["target_id"]))

    assert control_room.draw(
        screen, tower, first["key"], control_room.pane_identity(first), first,
        selected_window_ref=first_ref,
    ) is True

    frame = "\n".join(text for _y, _x, text, _attr in screen.writes)
    assert "창 A 작업" in frame and "창 A 보조" in frame
    assert "창 B 작업" not in frame
    assert "FIRST_PRIVATE_OUTPUT" not in frame
    assert "PEER_PRIVATE_OUTPUT" not in frame
    assert tiles == ["first"]


def test_switching_to_empty_window_hides_old_live_and_shows_disconnected_placeholder(monkeypatch):
    previous = _pane("%1", target="previous", window_ref="window-previous",
                     live_lines=["OLD_PRIVATE_OUTPUT"])
    tower = SimpleNamespace(
        rows=[previous], window_assets=[
            {"window_ref": "window-previous", "display_name": "이전 창"},
            {"window_ref": "window-empty", "display_name": "연결 끊긴 창"},
        ], projects=SimpleNamespace(all=lambda: [], hidden_refs=lambda: set()),
    )
    screen = Screen(40, 120)
    tiles = []
    monkeypatch.setattr(live_view.curses, "doupdate", lambda: None)
    monkeypatch.setattr(live_view, "_draw_tile", lambda _s, _t, row, *_a: tiles.append(dict(row)))

    control_room.draw(
        screen, tower, previous["key"], control_room.pane_identity(previous), previous,
        selected_window_ref="window-empty",
    )

    frame = "\n".join(text for _y, _x, text, _attr in screen.writes)
    assert "이 Window에는 연결된 Pane이 없습니다." in frame
    assert tiles[0]["display_name"] == "연결 끊긴 창"
    assert tiles[0]["live_lines"]
    assert "OLD_PRIVATE_OUTPUT" not in " ".join(tiles[0]["live_lines"])


def test_keyboard_center_selection_updates_right_live_within_selected_window(monkeypatch):
    ref_a, ref_b = "e" * 64, "f" * 64
    first = _pane("%1", target="one", window_ref=ref_a, live_lines=["LIVE_ONE"])
    second = _pane("%2", target="two", pane_index=1, window_ref=ref_a, live_lines=["LIVE_TWO"])
    other = _pane("%3", target="other", window_ref=ref_b, live_lines=["LIVE_OTHER"])
    tower = SimpleNamespace(
        rows=[first, second, other], visible_rows=[first, second, other], selected=0,
        window_assets=[{"window_ref": ref_a, "display_name": "Window A"},
                       {"window_ref": ref_b, "display_name": "Window B"}],
        projects=SimpleNamespace(all=lambda: [], hidden_refs=lambda: set()),
        work_groups=SimpleNamespace(all=lambda: []), start_background_refresh=lambda: False,
    )
    screen = Screen(40, 120, ["\t", curses.KEY_DOWN, "q"])
    shown = []
    monkeypatch.setattr(live_view.curses, "doupdate", lambda: None)
    monkeypatch.setattr(control_room.live_view, "_start_refresh", lambda _tower: False)
    monkeypatch.setattr(live_view, "_draw_tile", lambda _s, _t, row, *_a: shown.append(row["target_id"]))

    control_room.open_control_room(screen, tower)

    assert shown[-1] == "two"
    assert "other" not in shown
    assert tower.selected == 1


def test_action_menu_never_offers_direct_text_submission():
    local = _pane("%4", target="local", auto_agent="Codex",
                   auto_agent_source="process", agent_process_alive=True)
    remote = _pane("%4", target="peer", remote=True, tmux_host="asus", execution_host="asus",
                   transport="remote_tmux", transport_target="asus", auto_agent="OpenCode")
    ssh_client = _pane("%5", target="ssh-client", transport="ssh", transport_target="asus",
                       auto_agent="Claude", auto_agent_source="process", agent_process_alive=True)
    for row in (local, remote, ssh_client):
        keys = [key for key, _label in control_room.action_menu_items(row)]
        assert not any(key.startswith("right_input") for key in keys)


def test_narrow_keyboard_right_enter_opens_cancelled_focus_handoff(monkeypatch):
    row = _pane("%4", target="local", pid="4404", auto_agent="Codex", window_ref="window-a",
                auto_agent_source="process", agent_process_alive=True)
    tower = SimpleNamespace(
        rows=[row], visible_rows=[row], selected=0,
        own_pane_id="%99", local_host="mainpc",
        work_groups=SimpleNamespace(all=lambda: []),
        start_background_refresh=lambda: False,
    )
    screen = Screen(24, 80, ["\t", "\t", "\n", "q"])
    pickers, focus = [], []
    monkeypatch.setattr(live_view.curses, "doupdate", lambda: None)
    monkeypatch.setattr(control_room.live_view, "_start_refresh", lambda _tower: False)
    monkeypatch.setattr(control_room, "run_list_picker", lambda *_a, **_k: pickers.append(True)
                        or SimpleNamespace(cancelled=True, selected_key="cancel"))
    monkeypatch.setattr(control_room.control_actions, "submit_user_text_once",
                        lambda *_a: pytest.fail("Control Room must not submit text"))
    monkeypatch.setattr(control_room.control_actions, "handoff_focus_target",
                        lambda *_a: focus.append(True) or pytest.fail("cancel must not focus"))
    monkeypatch.setattr(control_room, "_enable_room_mouse", lambda: 0)
    monkeypatch.setattr(control_room, "_restore_room_mouse", lambda _mask: None)
    monkeypatch.setattr(control_room, "show_message_screen", lambda *_a: None)

    control_room.open_control_room(screen, tower)

    assert len(pickers) == 1
    assert focus == []
    assert screen.timeouts[-1] == 200


def test_mouse_center_selection_updates_the_single_right_live(monkeypatch):
    first = _pane("%1", target="first", window_ref="window-a", live_lines=["FIRST_LIVE"])
    second = _pane("%2", target="second", pane_index=1, window_ref="window-a",
                   live_lines=["SECOND_LIVE"])
    tower = SimpleNamespace(
        rows=[first, second], visible_rows=[first, second], selected=0,
        window_assets=[{"window_ref": "window-a", "display_name": "Window A"}],
        projects=SimpleNamespace(all=lambda: [], hidden_refs=lambda: set()),
        work_groups=SimpleNamespace(all=lambda: []),
        start_background_refresh=lambda: False,
    )
    screen = _mouse_driven_room(
        monkeypatch, tower, iter([curses.KEY_MOUSE, "q"]), target_kind="pane",
        target=("window-a", control_room.pane_identity(second)),
    )

    assert screen.drawn_tiles[-1] == "second"
    assert tower.selected == 1


@pytest.mark.parametrize("height,width", [(24, 80), (22, 110), (40, 120)])
def test_window_pane_live_layout_keeps_all_writes_inside_viewport(monkeypatch, height, width):
    ref = "9" * 64
    rows = [_pane(f"%{index}", target=f"target-{index}", pane_index=index,
                  window_ref=ref, display_name=f"업무 작업 {index}",
                  live_lines=[f"LIVE_{index}"]) for index in range(1, 7)]
    tower = SimpleNamespace(
        rows=rows, window_assets=[{"window_ref": ref, "display_name": "한국어 작업 창"}],
        projects=SimpleNamespace(all=lambda: [], hidden_refs=lambda: set()),
    )
    screen = Screen(height, width)
    monkeypatch.setattr(live_view.curses, "doupdate", lambda: None)
    monkeypatch.setattr(live_view, "_draw_tile", lambda *_args: None)

    assert control_room.draw(
        screen, tower, rows[0]["key"], control_room.pane_identity(rows[0]), rows[0],
        selected_window_ref=ref, active_panel=1,
    ) is True
    assert all(0 <= y < height and 0 <= x < width for y, x, _text, _attr in screen.writes)
    assert all(x + _visible_width(text) <= width for _y, x, text, _attr in screen.writes)
    frame = "\n".join(text for _y, _x, text, _attr in screen.writes)
    assert "업무 작업 6" in frame
