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

    def timeout(self, value):
        self.timeouts.append(value)

    def keypad(self, _value):
        pass

    def get_wch(self):
        return next(self.keys)


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


def test_project_selection_scopes_center_grid_and_right_live_to_same_snapshot(monkeypatch):
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
    assert drawn == ["asus-target", "main-target", "main-target"]


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
    assert t("project_manage.disconnected") in drawn_text


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
    )][-3:] == ["projects", "details", "settings"]


def test_project_creation_and_keyboard_navigation_keep_grid_and_focus_on_exact_remote_pane(
    monkeypatch, tmp_path,
):
    from tmux_agent_tower.state.projects import ProjectStore
    from tmux_agent_tower.ui import projects as project_ui

    main_ref, asus_ref = "3" * 64, "4" * 64
    main = _pane("%1", target="main-target", window_ref=main_ref,
                 tmux_host="mainpc", execution_host="mainpc", live_lines=["MAIN_ONLY"])
    asus = _pane("%1", target="asus-target", window_ref=asus_ref,
                 tmux_host="asus", execution_host="asus", remote=True,
                 transport="remote_tmux", transport_target="asus", live_lines=["ASUS_ONLY"])
    store = ProjectStore(tmp_path / "projects.json")
    tower = SimpleNamespace(
        rows=[main, asus], visible_rows=[main, asus], selected=0,
        window_assets=[
            {"window_ref": main_ref, "display_name": "Main", "tmux_host": "mainpc"},
            {"window_ref": asus_ref, "display_name": "ASUS", "tmux_host": "asus"},
        ],
        projects=store, work_groups=SimpleNamespace(all=lambda: []),
        start_background_refresh=lambda: False, _apply_filter=lambda: None,
    )
    screen = Screen(40, 120, [" "])
    monkeypatch.setattr(live_view.curses, "doupdate", lambda: None)
    monkeypatch.setattr(control_room.live_view, "_start_refresh", lambda _tower: False)
    monkeypatch.setattr(
        control_room, "run_list_picker",
        lambda *_args, **_kwargs: SimpleNamespace(cancelled=False, selected_key="projects"),
    )
    monkeypatch.setattr(
        project_ui, "run_list_picker",
        lambda *_args, **_kwargs: SimpleNamespace(cancelled=False, selected_key="create"),
    )
    monkeypatch.setattr(project_ui, "prompt_text", lambda *_args, **_kwargs: "Cross-host project")
    selected_tiles = []

    def add_remote_window(stdscr, current_tower, project_id):
        store.add_windows(project_id, [asus_ref])
        nav = control_room.navigation_lines(
            [main, asus], main["key"], control_room.pane_identity(main),
            projects=store.all(), window_assets=tower.window_assets,
            hidden_refs=store.hidden_refs(),
        )
        current_cursor = next(i for i, line in enumerate(nav)
                              if line["kind"] == "pane" and line.get("row") is main
                              and not line.get("project_id"))
        target_cursor = next(i for i, line in enumerate(nav)
                             if line["kind"] == "pane" and line.get("row") is main
                             and line.get("project_id") == project_id)
        screen.keys = iter([curses.KEY_DOWN] * (target_cursor - current_cursor)
                           + ["\t", curses.KEY_DOWN, "q"])

    monkeypatch.setattr(project_ui, "open_project_editor", add_remote_window)
    monkeypatch.setattr(
        live_view, "_draw_tile",
        lambda _screen, _tower, row, _index, _y, x, *_args: selected_tiles.append(
            (row["target_id"], x)
        ),
    )

    control_room.open_control_room(screen, tower)

    assert store.all()[0]["display_name"] == "Cross-host project"
    assert store.all()[0]["window_refs"] == [main_ref, asus_ref]
    center_x = control_room.panel_regions(120, 40)[1][0]
    right_x = control_room.panel_regions(120, 40)[2][0]
    assert [target for target, _x in selected_tiles[-3:]] == [
        "main-target", "asus-target", "asus-target",
    ]
    assert all(center_x <= x < right_x for _target, x in selected_tiles[-3:-1])
    assert selected_tiles[-1][1] == right_x
    drawn_text = "\n".join(text for _y, _x, text, _attr in screen.writes)
    assert "Cross-host project" in drawn_text


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
def test_fake_viewports_render_three_read_only_panels_without_fresh_capture(monkeypatch, height, width):
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
    assert "작업 위치" in frame
    assert "관련 Pane LIVE" in frame
    assert "선택한 작업 LIVE" in frame
    assert "Task %2" in frame
    assert "한국어 출력" in frame
    assert "Shift+Tab" in frame
    assert "Space" in frame
    assert "Y 최신 답변 복사" in frame
    assert all(0 <= y < height and 0 <= x < width for y, x, _text, _attr in screen.writes)
    assert all(x + _visible_width(text) <= width for _y, x, text, _attr in screen.writes)


def test_wide_view_uses_side_by_side_panels_but_short_view_stacks_them():
    wide = control_room.panel_regions(120, 40)
    short = control_room.panel_regions(120, 18)

    assert len(wide) == len(short) == 3
    assert len({region[1] for region in wide}) == 1
    assert short[0][1] < short[1][1] < short[2][1]


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
        screen, tower, "%2", control_room.pane_identity(remote), remote,
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
    screen = Screen(24, 80, [curses.KEY_DOWN, "y", "q"])
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
    screen = Screen(24, 80, ["y", "q"])
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
    row = _pane("%6", live_lines=[f"LINE_{index}" for index in range(12)])
    tower = SimpleNamespace(rows=[row])
    screen = Screen(24, 80)
    monkeypatch.setattr(live_view.curses, "doupdate", lambda: None)
    monkeypatch.setattr(control_room.curses, "has_colors", lambda: False)
    assert control_room.draw(
        screen, tower, "%6", control_room.pane_identity(row), row,
        active_panel=1,
        cursor_key=("pane", control_room.pane_identity(row)),
    ) is True
    center_x = control_room.panel_regions(80, 24)[1][0]
    center_heading = next(item for item in screen.writes if item[0] == 8 and item[1] == center_x)
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


def test_left_tree_can_select_and_collapse_a_group_without_persisting_it(monkeypatch):
    first = _pane("%1", target="one", work_group_id="g1", work_group_name="Saved group")
    second = _pane("%2", target="two", pane_index=1, work_group_id="g1", work_group_name="Saved group")
    group = {"group_id": "g1", "member_order": ["one", "two"], "layout": "split-2"}
    before = dict(group)
    tower = SimpleNamespace(
        rows=[first, second], visible_rows=[first, second], selected=0,
        work_groups=SimpleNamespace(all=lambda: [group]),
        start_background_refresh=lambda: False,
    )
    screen = Screen(24, 80, [curses.KEY_LEFT, "\n", "q"])
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

    group_node = next(
        line["node_key"] for line in control_room.navigation_lines(
            [first, second], "%1", control_room.pane_identity(first),
        ) if line["kind"] == "project"
    )
    assert frames[1][0] == group_node
    assert group_node in frames[2][1]
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
    screen = Screen(24, 80, [" ", "q"])
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


def test_enter_from_control_room_opens_metadata_details_not_agent_input(monkeypatch):
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

    assert shown and shown[0][0] == "현재 작업 상세"
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
