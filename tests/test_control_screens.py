import curses
import stat
from types import SimpleNamespace

import pytest

from tmux_agent_tower.state.control_screens import ControlScreenError, ControlScreenStore
from tmux_agent_tower.ui import control_room, live_view


def _pane(key, *, host="mainpc", session="work", window="@1", pid=None, target=None,
          window_ref="", remote=False, transport="local", transport_target="", **extra):
    return {
        "key": key, "kind": "pane", "target_id": target or f"{host}:{key}",
        "tmux_host": host, "session": session, "session_id": f"${session}",
        "window_id": window, "pane_id": key, "pane_pid": pid or f"{key[1:]}01",
        "transport": transport, "transport_target": transport_target, "remote": remote,
        "window_ref": window_ref, "display_name": f"작업 {key}", "agent": "Codex",
        "live_lines": ["PRIVATE_TERMINAL_OUTPUT_MUST_NOT_BE_SAVED"], **extra,
    }


class Screen:
    def __init__(self, height, width, keys=()):
        self.size = (height, width)
        self.keys = iter(keys)
        self.writes = []

    def getmaxyx(self):
        return self.size

    def erase(self):
        self.writes.clear()

    def addnstr(self, y, x, text, _n, attr=0):
        self.writes.append((y, x, text, attr))

    def noutrefresh(self):
        pass

    def timeout(self, _value):
        pass

    def keypad(self, _value):
        pass

    def get_wch(self):
        return next(self.keys)


def test_saved_screen_round_trips_four_exact_panes_without_terminal_output(tmp_path):
    path = tmp_path / "control-screens.json"
    rows = [_pane(f"%{i}", target=f"target-{i}", pid=f"{100+i}", window_ref="main-window")
            for i in range(1, 5)]
    store = ControlScreenStore(path)
    screen = store.save_screen("한글 작업 화면", rows, rows[2], "grid-4",
                               {"project_id": "project-stable-id", "window_ref": "main-window"})

    restarted = ControlScreenStore(path)
    restored = restarted.get_screen(screen["screen_id"])
    assert restored["layout"] == "grid-4"
    assert restored["context"]["project_id"] == "project-stable-id"
    assert [item["identity"]["target_id"] for item in restored["panes"]] == [f"target-{i}" for i in range(1, 5)]
    assert restored["selected"]["identity"]["target_id"] == "target-3"
    contents = path.read_text(encoding="utf-8")
    assert "PRIVATE_TERMINAL_OUTPUT_MUST_NOT_BE_SAVED" not in contents
    assert "live_lines" not in contents
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_layout_template_is_project_and_pane_independent_and_can_target_another_window(monkeypatch, tmp_path):
    from tmux_agent_tower.i18n import t

    store = ControlScreenStore(tmp_path / "control-screens.json")
    template = store.save_template("두 영역", "split-2")
    first = [_pane("%1", window="@1", window_ref="window-one")]
    second = [
        _pane("%1", host="asus", session="remote", window="@8", pid="800", target="asus-1",
              window_ref="window-two", remote=True, transport="remote_tmux", transport_target="asus"),
        _pane("%2", host="asus", session="remote", window="@8", pid="801", target="asus-2",
              window_ref="window-two", remote=True, transport="remote_tmux", transport_target="asus"),
    ]
    state = {
        "layout": "focus", "layout_pinned": False, "view_rows": [second[0]], "selected_row": second[0],
        "selected_key": second[0]["key"], "selected_identity": control_room.pane_identity(second[0]),
        "original": second[0], "cursor_key": None,
        "context": {"project_id": "renamed-but-same-id", "window_ref": "window-two"},
    }
    selections = iter(["template_apply", template["template_id"]])
    monkeypatch.setattr(control_room, "run_list_picker", lambda *_a, **_k: SimpleNamespace(
        cancelled=False, selected_key=next(selections),
    ))
    monkeypatch.setattr(control_room, "show_message_screen", lambda *_a: None)
    tower = SimpleNamespace(control_screens=store, projects=None)
    control_room._open_screen_menu(None, tower, state, first + second, [], None)

    assert state["layout"] == "split-2"
    assert state["context"]["window_ref"] == "window-two"
    assert {row["target_id"] for row in state["view_rows"]} == {"asus-1", "asus-2"}
    assert "panes" not in template and "window_ref" not in template


def test_restored_screen_never_rebinds_on_host_pid_or_window_mismatch(tmp_path):
    saved = _pane("%4", host="mainpc", pid="444", target="saved-target", window_ref="saved-window")
    store = ControlScreenStore(tmp_path / "control-screens.json")
    store.save_screen("safe", [saved], saved, "focus")
    reference = store.screens()[0]["panes"]
    replacements = [
        _pane("%4", host="asus", pid="444", target="saved-target", window_ref="saved-window"),
        _pane("%4", host="mainpc", pid="445", target="saved-target", window_ref="saved-window"),
        _pane("%4", host="mainpc", pid="444", target="saved-target", window_ref="another-window"),
    ]
    for replacement in replacements:
        resolved = control_room._resolve_saved_rows(reference, [replacement])[0]
        assert resolved["_live_stale"] is True
        assert resolved["live_lines"] == []
        assert resolved["key"].startswith("saved-screen-stale:")

    old = saved | {"live_lines": ["OLD_PANE_OUTPUT_MUST_STAY_HIDDEN"]}
    new = replacements[1]
    tile_rows = []
    screen = Screen(24, 80)
    tower = SimpleNamespace(rows=[new], projects=None, window_assets=[])
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(live_view.curses, "doupdate", lambda: None)
    monkeypatch.setattr(live_view, "_draw_tile", lambda _s, _t, row, *_a: tile_rows.append(row))
    try:
        control_room.draw(
            screen, tower, old["key"], control_room.pane_identity(old), old,
            layout="focus", grid_rows_override=[old], active_panel=2,
        )
    finally:
        monkeypatch.undo()
    assert tile_rows and all(row.get("live_lines") == [] for row in tile_rows)
    assert all("OLD_PANE_OUTPUT_MUST_STAY_HIDDEN" not in text for _y, _x, text, _attr in screen.writes)


def test_stale_selection_rebinds_only_exact_identity_and_window_reference(tmp_path):
    saved = _pane("%4", host="mainpc", pid="444", target="saved-target", window_ref="saved-window")
    store = ControlScreenStore(tmp_path / "control-screens.json")
    store.save_last_view([saved], saved, "focus")
    stale = control_room._resolve_saved_rows(store.last_view()["panes"], [])[0]
    exact = dict(saved, live_lines=["CURRENT_OUTPUT"])

    selected, current = control_room._resolve_selection(
        [exact], stale["key"], control_room.pane_identity(stale), stale,
    )
    assert current is True
    assert selected is exact

    replacements = [
        _pane("%4", host="asus", pid="444", target="saved-target", window_ref="saved-window"),
        _pane("%4", host="mainpc", pid="445", target="saved-target", window_ref="saved-window"),
        _pane("%4", host="mainpc", pid="444", target="saved-target", window_ref="another-window"),
    ]
    for replacement in replacements:
        selected, current = control_room._resolve_selection(
            [replacement], stale["key"], control_room.pane_identity(stale), stale,
        )
        assert current is False
        assert selected["live_lines"] == []
        assert selected["_live_stale"] is True

    saved_without_window_ref = _pane(
        "%6", pid="666", target="no-window-reference", window_ref="",
    )
    store.save_last_view([saved_without_window_ref], saved_without_window_ref, "focus")
    reference_without_window_ref = store.last_view()["panes"]
    stale_without_window_ref = control_room._resolve_saved_rows(reference_without_window_ref, [])[0]
    appeared_in_different_window = dict(saved_without_window_ref, window_ref="new-window")
    resolved = control_room._resolve_saved_rows(
        reference_without_window_ref, [appeared_in_different_window],
    )[0]
    assert resolved["_live_stale"] is True
    selected, current = control_room._resolve_selection(
        [appeared_in_different_window], stale_without_window_ref["key"],
        control_room.pane_identity(stale_without_window_ref), stale_without_window_ref,
    )
    assert current is False
    assert selected["live_lines"] == []


def test_partial_saved_screen_reconnect_keeps_unmatched_pane_disconnected(tmp_path):
    first = _pane("%1", target="target-1", pid="101", window_ref="window-one")
    second = _pane("%2", target="target-2", pid="202", window_ref="window-two")
    store = ControlScreenStore(tmp_path / "control-screens.json")
    store.save_last_view([first, second], first, "split-2")
    replacement = _pane("%2", target="target-2", pid="999", window_ref="window-two",
                         live_lines=["UNRELATED_OUTPUT"])

    rows = control_room._resolve_saved_rows(store.last_view()["panes"], [first, replacement])

    assert rows[0] is first
    assert rows[1]["key"].startswith("saved-screen-stale:")
    assert rows[1]["live_lines"] == []
    assert rows[1]["_live_stale"] is True


@pytest.mark.parametrize("height,width", [(24, 80), (40, 120)])
def test_last_view_reconnect_updates_center_and_right_after_exact_pane_returns(
    monkeypatch, tmp_path, height, width,
):
    from tmux_agent_tower.ui import control_view

    pane = _pane("%4", host="mainpc", session="work", window="@7", pid="704",
                 target="mainpc:work:@7:%4", window_ref="saved-window",
                 live_lines=["CURRENT_PANE_OUTPUT"])
    missing = _pane("%5", host="mainpc", session="work", window="@7", pid="705",
                    target="mainpc:work:@7:%5", window_ref="saved-window",
                    live_lines=["MUST_NOT_BE_RESTORED"])
    store = ControlScreenStore(tmp_path / "control-screens.json")
    store.save_last_view([pane, missing], pane, "split-2", {"window_ref": "saved-window"})
    tower = SimpleNamespace(
        rows=[], visible_rows=[], selected=0, projects=None, window_assets=[],
        work_groups=SimpleNamespace(all=lambda: []), control_screens=store,
        start_background_refresh=lambda: None,
    )
    refresh_count = 0

    def apply_refresh(current_tower):
        nonlocal refresh_count
        refresh_count += 1
        if refresh_count == 2:
            current_tower.rows = [pane]
            current_tower.visible_rows = [pane]
            return True
        return False

    monkeypatch.setattr(live_view, "_start_refresh", lambda _tower: False)
    monkeypatch.setattr(live_view, "_apply_refresh", apply_refresh)
    monkeypatch.setattr(live_view.curses, "doupdate", lambda: None)
    monkeypatch.setattr(control_room, "show_message_screen", lambda *_args: None)
    copied = []
    monkeypatch.setattr(control_view, "_copy_result",
                        lambda _tower, key, _screen: copied.append(key) or "copied")
    draw_calls = []
    pane_list_calls = []
    draw_grid = control_room._draw_grid
    regions = control_room.panel_regions(width, height)

    def capture_grid(screen, current_tower, region, rows, selected_identity, current_width,
                     *args, **kwargs):
        panel = next(
            (index for index, expected in enumerate(regions)
             if region[0] == expected[0] and region[1] == expected[1] + 1),
            None,
        )
        draw_calls.append((panel, [dict(row) for row in rows], selected_identity))
        return draw_grid(screen, current_tower, region, rows, selected_identity, current_width,
                         *args, **kwargs)

    monkeypatch.setattr(control_room, "_draw_grid", capture_grid)
    monkeypatch.setattr(control_room, "_draw_pane_list",
                        lambda _screen, _region, rows, selected_identity, *_a:
                        pane_list_calls.append(([dict(row) for row in rows], selected_identity)))
    agent_starts = []
    tower.start_agent = lambda *args, **kwargs: agent_starts.append((args, kwargs))

    control_room.open_control_room(Screen(height, width, [-1, "\t", "\t", "y", "q"]), tower)

    assert draw_calls
    assert pane_list_calls
    right = draw_calls[-1][1]
    center = pane_list_calls[-1][0]
    assert len(center) == len(right) == 1
    assert center[0]["key"] == right[0]["key"] == pane["key"]
    assert control_room.pane_identity(center[0]) == control_room.pane_identity(right[0])
    assert center[0]["live_lines"] == right[0]["live_lines"] == ["CURRENT_PANE_OUTPUT"]
    assert agent_starts == []
    assert copied == [pane["key"]]
    persisted = (tmp_path / "control-screens.json").read_text(encoding="utf-8")
    assert "CURRENT_PANE_OUTPUT" not in persisted
    assert "MUST_NOT_BE_RESTORED" not in persisted


def test_last_view_restores_its_selected_window_not_the_previously_open_window(monkeypatch, tmp_path):
    first = _pane("%1", host="mainpc", session="work", window="@1", pid="101",
                  target="first-window-pane", window_ref="window-first",
                  display_name="첫 창 작업", live_lines=["FIRST_WINDOW_OUTPUT"])
    saved = _pane("%2", host="mainpc", session="work", window="@2", pid="202",
                  target="saved-window-pane", window_ref="window-saved",
                  display_name="저장된 창 작업", live_lines=["SAVED_WINDOW_OUTPUT"])
    store = ControlScreenStore(tmp_path / "control-screens.json")
    store.save_last_view([saved], saved, "focus", {"window_ref": "window-saved"})
    tower = SimpleNamespace(
        rows=[first, saved], visible_rows=[first, saved], selected=0,
        projects=None, window_assets=[
            {"window_ref": "window-first", "display_name": "첫 창"},
            {"window_ref": "window-saved", "display_name": "저장된 창"},
        ], work_groups=SimpleNamespace(all=lambda: []), control_screens=store,
        start_background_refresh=lambda: None,
    )
    shown = []
    monkeypatch.setattr(live_view, "_start_refresh", lambda _tower: False)
    monkeypatch.setattr(live_view, "_apply_refresh", lambda _tower: False)
    monkeypatch.setattr(live_view.curses, "doupdate", lambda: None)
    monkeypatch.setattr(live_view, "_draw_tile", lambda _s, _t, row, *_a: shown.append(row["target_id"]))

    control_room.open_control_room(Screen(40, 120, ["q"]), tower)

    assert shown == ["saved-window-pane"]
    assert tower.selected == 1


def test_duplicate_pane_ids_across_hosts_restore_only_matching_host(tmp_path):
    main = _pane("%2", host="mainpc", target="main-2", pid="222", window_ref="window-main")
    asus = _pane("%2", host="asus", target="asus-2", pid="222", window_ref="window-asus",
                 remote=True, transport="remote_tmux", transport_target="asus")
    store = ControlScreenStore(tmp_path / "control-screens.json")
    saved = store.save_screen("cross host", [main, asus], asus, "split-2")
    resolved = control_room._resolve_saved_rows(saved["panes"], [main, asus])
    assert [row["target_id"] for row in resolved] == ["main-2", "asus-2"]
    assert all("_live_stale" not in row for row in resolved)


def test_project_rename_keeps_saved_context_bound_by_project_id(tmp_path):
    from tmux_agent_tower.state.projects import ProjectStore

    window_ref = "a" * 64
    pane = _pane("%5", target="stable-target", pid="555", window_ref=window_ref)
    projects = ProjectStore(tmp_path / "projects.json")
    project = projects.create("Before rename", [window_ref])
    screens = ControlScreenStore(tmp_path / "control-screens.json")
    saved = screens.save_screen(
        "Project view", [pane], pane, "focus",
        {"project_id": project["project_id"], "window_ref": window_ref},
    )
    projects.rename(project["project_id"], "After rename")
    restored = control_room._saved_screen_state(saved, SimpleNamespace(
        projects=projects, window_assets=[],
    ), [pane])
    assert projects.all()[0]["display_name"] == "After rename"
    assert restored["context"]["project_id"] == project["project_id"]
    assert restored["selected_row"]["target_id"] == "stable-target"


def test_corrupt_future_or_unknown_schema_is_not_overwritten(tmp_path):
    path = tmp_path / "control-screens.json"
    original = b'{"schema_version":2,"screens":[],"templates":[],"last_view":null,"future":"keep"}\n'
    path.write_bytes(original)
    store = ControlScreenStore(path)
    assert store.corrupt
    with pytest.raises(ControlScreenError):
        store.save_template("layout", "focus")
    assert path.read_bytes() == original


def test_concurrent_external_edit_blocks_write_and_preserves_other_writer(tmp_path):
    path = tmp_path / "control-screens.json"
    first, second = ControlScreenStore(path), ControlScreenStore(path)
    first.screens()
    second.save_template("other writer", "split-2")
    external = path.read_bytes()
    with pytest.raises(ControlScreenError):
        first.save_template("stale writer", "focus")
    assert path.read_bytes() == external


def test_failed_atomic_replace_keeps_the_previous_saved_screen(tmp_path, monkeypatch):
    from tmux_agent_tower.state import control_screens

    path = tmp_path / "control-screens.json"
    row = _pane("%6", pid="606", window_ref="window-6")
    store = ControlScreenStore(path)
    store.save_screen("Original", [row], row, "focus")
    original = path.read_bytes()
    monkeypatch.setattr(control_screens.os, "replace", lambda *_a: (_ for _ in ()).throw(OSError("replace failed")))
    with pytest.raises(OSError):
        store.save_template("Never committed", "grid-4")
    assert path.read_bytes() == original
    assert not list(tmp_path.glob("*.tmp"))


def test_menu_save_then_restart_restores_four_panes_and_selected_right_live(monkeypatch, tmp_path):
    path = tmp_path / "control-screens.json"
    protected_files = {
        name: f"preserve:{name}\n".encode()
        for name in ("folders.json", "work-groups.json", "projects.json", "worksets.json")
    }
    for name, content in protected_files.items():
        (tmp_path / name).write_bytes(content)
    panes = [_pane(f"%{i}", target=f"target-{i}", pid=f"{300+i}", window_ref="shared-window")
             for i in range(1, 5)]
    store = ControlScreenStore(path)
    tower = SimpleNamespace(
        rows=panes, visible_rows=panes, selected=0,
        work_groups=SimpleNamespace(all=lambda: []), control_screens=store,
        start_background_refresh=lambda: False,
    )
    agent_starts = []
    tower.start_agent = lambda *args, **kwargs: agent_starts.append((args, kwargs))
    monkeypatch.setattr(live_view.curses, "doupdate", lambda: None)
    monkeypatch.setattr(control_room.live_view, "_start_refresh", lambda _tower: False)
    menu_choices = iter(["screens", "save"])
    monkeypatch.setattr(control_room, "run_list_picker", lambda *_a, **_k: SimpleNamespace(
        cancelled=False, selected_key=next(menu_choices),
    ))
    monkeypatch.setattr(control_room, "prompt_text", lambda *_a, **_k: "Four Pane screen")
    monkeypatch.setattr(control_room, "show_message_screen", lambda *_a: None)
    captured = []
    monkeypatch.setattr(live_view, "_draw_tile", lambda _s, _t, row, _i, _y, x, *_a:
                        captured.append((row["target_id"], x)))

    control_room.open_control_room(Screen(24, 80, ["\t", curses.KEY_DOWN, " ", "q"]), tower)
    assert agent_starts == []
    saved = store.screens()[0]
    assert len(saved["panes"]) == 4
    assert saved["selected"]["identity"]["target_id"] == "target-2"
    persisted = path.read_text(encoding="utf-8")
    assert "PRIVATE_TERMINAL_OUTPUT_MUST_NOT_BE_SAVED" not in persisted

    restarted = ControlScreenStore(path)
    next_tower = SimpleNamespace(
        rows=panes, visible_rows=panes, selected=0,
        work_groups=SimpleNamespace(all=lambda: []), control_screens=restarted,
        start_background_refresh=lambda: False,
    )
    captured.clear()
    screen = Screen(24, 80, ["\t", "\t", "q"])
    control_room.open_control_room(screen, next_tower)
    right_x = control_room.panel_regions(80, 24)[2][0]
    assert captured[-1] == ("target-2", right_x)
    assert len(captured) == 1  # The new Right panel observes only the selected Pane.
    assert {name: (tmp_path / name).read_bytes() for name in protected_files} == protected_files


def test_overwrite_cancel_and_delete_cancel_preserve_saved_screen(monkeypatch, tmp_path):
    store = ControlScreenStore(tmp_path / "control-screens.json")
    row = _pane("%7", pid="777", window_ref="window-7")
    saved = store.save_screen("Keep", [row], row, "focus")
    original = store.get_screen(saved["screen_id"])
    store_choices = iter(["save", "cancel"])
    monkeypatch.setattr(control_room, "run_list_picker", lambda *_a, **_k: SimpleNamespace(
        cancelled=False, selected_key=next(store_choices),
    ))
    monkeypatch.setattr(control_room, "prompt_text", lambda *_a, **_k: "Keep")
    monkeypatch.setattr(control_room, "show_message_screen", lambda *_a: None)
    state = {"layout": "split-2", "view_rows": [row], "selected_row": row,
             "context": {"project_id": None, "window_ref": "window-7"}}
    control_room._open_screen_menu(None, SimpleNamespace(control_screens=store), state, [row], [], None)
    assert store.get_screen(saved["screen_id"]) == original

    picks = iter(["manage", saved["screen_id"], "cancel"])
    monkeypatch.setattr(control_room, "run_list_picker", lambda *_a, **_k: SimpleNamespace(
        cancelled=False, selected_key=next(picks),
    ))
    control_room._open_screen_menu(None, SimpleNamespace(control_screens=store), state, [row], [], None)
    assert store.get_screen(saved["screen_id"]) == original


@pytest.mark.parametrize("height,width", [(18, 60), (24, 80), (40, 120)])
def test_saved_grid_layouts_fit_common_terminal_sizes_and_show_unused_slots(monkeypatch, height, width):
    rows = [_pane("%8", target="target-8", pid="808", window_ref="window-8",
                  display_name="매우 긴 한글 English 작업 이름이 좁은 화면에서도 겹치지 않아야 합니다")]
    tower = SimpleNamespace(rows=rows, projects=None, window_assets=[])
    screen = Screen(height, width)
    monkeypatch.setattr(live_view.curses, "doupdate", lambda: None)
    monkeypatch.setattr(live_view, "_draw_tile", lambda *_a: None)
    tiles = []
    monkeypatch.setattr(live_view, "_draw_tile", lambda _s, _t, row, *_a: tiles.append(row["target_id"]))
    assert control_room.draw(
        screen, tower, "%8", control_room.pane_identity(rows[0]), rows[0],
        layout="grid-4", grid_rows_override=rows, active_panel=2,
        selected_window_ref="window-8",
    ) is True
    assert all(0 <= y < height and 0 <= x < width for y, x, _text, _attr in screen.writes)
    assert tiles == ["target-8"]
