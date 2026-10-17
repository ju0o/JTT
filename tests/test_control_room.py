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
