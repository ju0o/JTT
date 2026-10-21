import curses
from types import SimpleNamespace

import pytest

from tmux_agent_tower.ui import control_room, ux_preview


class _Screen:
    def __init__(self, height=40, width=120, keys=()):
        self.size = (height, width)
        self.keys = iter(keys)
        self.timeouts = []

    def getmaxyx(self):
        return self.size

    def erase(self):
        pass

    def addnstr(self, *_args):
        pass

    def noutrefresh(self):
        pass

    def timeout(self, value):
        self.timeouts.append(value)

    def keypad(self, _value):
        pass

    def get_wch(self):
        return next(self.keys)


def _install_fake_terminal(monkeypatch):
    monkeypatch.setattr(control_room.curses, "has_colors", lambda: False)
    monkeypatch.setattr(control_room.live_view.curses, "doupdate", lambda: None)
    monkeypatch.setattr(control_room.live_view, "_draw_tile", lambda *_args: None)
    monkeypatch.setattr(control_room, "_enable_room_mouse", lambda: None)
    monkeypatch.setattr(control_room, "_restore_room_mouse", lambda _mask: None)


def test_preview_fixture_is_synthetic_and_has_no_persistent_store_paths():
    tower = ux_preview.make_preview_tower()

    assert tower.read_only_preview is True
    assert len(tower.rows) >= 16
    assert all(row.get("live_lines") for row in tower.rows)
    assert len(tower.folders.all()) == 2
    assert tower.projects.all() == []
    assert tower.control_screens is None
    assert not any(hasattr(store, "path") for store in (tower.folders, tower.projects, tower.work_groups))
    assert {row["transport"] for row in tower.rows} >= {"local", "ssh", "remote_tmux"}


def test_preview_never_starts_or_applies_live_refresh_even_after_refresh_interval(monkeypatch):
    _install_fake_terminal(monkeypatch)
    tower = ux_preview.make_preview_tower()
    # The preview fixture intentionally has no Tower.load(). Advance the room
    # clock beyond multiple refresh intervals while keeping the UI interactive.
    ticks = iter((0.0, 0.0, 3.0, 6.0))
    monkeypatch.setattr(control_room.time, "monotonic", lambda: next(ticks, 6.0))
    screen = _Screen(keys=(curses.KEY_DOWN, "q"))
    refresh_calls = []
    start_refresh = control_room.live_view._start_refresh

    def forbidden_refresh(tower_arg):
        refresh_calls.append(tower_arg)
        return start_refresh(tower_arg)

    monkeypatch.setattr(control_room.live_view, "_start_refresh", forbidden_refresh)
    monkeypatch.setattr(
        control_room.live_view, "_apply_refresh",
        lambda tower_arg: pytest.fail("read-only preview must not apply async refresh"),
    )

    control_room.open_control_room(screen, tower, read_only_preview=True)

    assert refresh_calls == []
    assert screen.timeouts[-1] == 200


def test_normal_control_room_retains_initial_and_periodic_async_refresh(monkeypatch):
    _install_fake_terminal(monkeypatch)
    tower = ux_preview.make_preview_tower()
    ticks = iter((0.0, 0.0, 3.0, 6.0))
    monkeypatch.setattr(control_room.time, "monotonic", lambda: next(ticks, 6.0))
    screen = _Screen(keys=(curses.KEY_DOWN, "q"))
    started = []
    applied = []
    tower.start_background_refresh = lambda: started.append(True) or True
    tower.apply_background_refresh = lambda: applied.append(True) or False

    control_room.open_control_room(screen, tower)

    assert len(started) == 2  # initial load and one refresh after the interval
    assert len(applied) >= 2  # async results are still applied in normal Tower mode


def test_normal_control_room_retains_periodic_load_fallback(monkeypatch):
    _install_fake_terminal(monkeypatch)
    tower = ux_preview.make_preview_tower()
    ticks = iter((0.0, 0.0, 3.0, 6.0))
    monkeypatch.setattr(control_room.time, "monotonic", lambda: next(ticks, 6.0))
    screen = _Screen(keys=(curses.KEY_DOWN, "q"))
    loads = []
    tower.load = lambda: loads.append(True)

    control_room.open_control_room(screen, tower)

    assert len(loads) == 1


def test_preview_confirmation_is_visible_but_never_calls_focus_handoff(monkeypatch):
    _install_fake_terminal(monkeypatch)
    tower = ux_preview.make_preview_tower()
    screen = _Screen(keys=("\t", "\t", "\n", "q"))
    picker_calls = []
    notices = []
    monkeypatch.setattr(control_room, "run_list_picker", lambda *_args, **kwargs: (
        picker_calls.append(kwargs) or SimpleNamespace(cancelled=False, selected_key="confirm")
    ))
    monkeypatch.setattr(control_room, "show_message_screen", lambda _s, _title, lines: notices.extend(lines))
    monkeypatch.setattr(
        control_room.control_actions, "handoff_focus_target",
        lambda *_args: pytest.fail("preview must never change tmux focus"),
    )
    monkeypatch.setattr(
        control_room.control_actions, "submit_user_text_once",
        lambda *_args: pytest.fail("preview must never submit Agent text"),
    )
    monkeypatch.setattr(
        control_room.control_actions, "send_prompt",
        lambda *_args, **_kwargs: pytest.fail("preview must never send Agent keys"),
    )

    control_room.open_control_room(screen, tower, read_only_preview=True)

    assert len(picker_calls) == 1
    assert any("미리보기" in line and "차단" in line for line in picker_calls[0]["preamble"])
    assert any("포커스 이동을 실행하지 않습니다" in line for line in notices)
    assert screen.timeouts[-1] == 200


def test_preview_right_mouse_click_opens_confirm_then_blocks_focus(monkeypatch):
    _install_fake_terminal(monkeypatch)
    tower = ux_preview.make_preview_tower()
    screen = _Screen(keys=())
    hit_state = {}
    mouse_event = {}
    picker_calls = []
    notices = []
    original_draw = control_room.draw

    def capture_draw(*args, **kwargs):
        result = original_draw(*args, **kwargs)
        hit_state.update(kwargs.get("hit_test_state") or {})
        return result

    def read_key(_screen):
        if not mouse_event.get("sent"):
            region = hit_state["right_region"]
            mouse_event["value"] = (0, region[0] + 1, region[1] + 1, 0, curses.BUTTON1_CLICKED)
            mouse_event["sent"] = True
            return curses.KEY_MOUSE
        return "q"

    monkeypatch.setattr(control_room, "draw", capture_draw)
    monkeypatch.setattr(control_room, "read_key", read_key)
    monkeypatch.setattr(control_room.curses, "getmouse", lambda: mouse_event["value"])
    monkeypatch.setattr(control_room, "run_list_picker", lambda *_args, **kwargs: (
        picker_calls.append(kwargs) or SimpleNamespace(cancelled=False, selected_key="confirm")
    ))
    monkeypatch.setattr(control_room, "show_message_screen", lambda _s, _title, lines: notices.extend(lines))
    monkeypatch.setattr(
        control_room.control_actions, "handoff_focus_target",
        lambda *_args: pytest.fail("Preview mouse confirmation must not move tmux focus"),
    )

    control_room.open_control_room(screen, tower, read_only_preview=True)

    assert len(picker_calls) == 1
    assert any("실제 tmux 이동은 차단됩니다" in line for line in picker_calls[0]["preamble"])
    assert any("포커스 이동을 실행하지 않습니다" in line for line in notices)


def test_preview_y_and_space_are_blocked_and_folder_fold_is_memory_only(monkeypatch):
    _install_fake_terminal(monkeypatch)
    tower = ux_preview.make_preview_tower()
    initial_folders = tower.folders.all()
    initial_toggle_count = tower.folders.toggle_calls
    screen = _Screen(keys=(curses.KEY_UP, "\n", " ", "\t", "y", "q"))
    notices = []
    monkeypatch.setattr(control_room, "show_message_screen", lambda _s, _title, lines: notices.extend(lines))
    monkeypatch.setattr(
        control_room, "_open_action_menu",
        lambda *_args, **_kwargs: pytest.fail("preview Space must not open mutating menus"),
    )
    from tmux_agent_tower.ui import control_view
    monkeypatch.setattr(
        control_view, "_copy_result",
        lambda *_args: pytest.fail("preview must never access Clipboard"),
    )

    control_room.open_control_room(screen, tower, read_only_preview=True)

    assert tower.folders.all() == initial_folders
    assert tower.folders.toggle_calls == initial_toggle_count
    assert any("프로젝트·화면 설정은 저장되지 않습니다" in line for line in notices)
    assert any("Clipboard 접근이 차단됩니다" in line for line in notices)


def test_remote_peer_preview_does_not_open_focus_confirmation(monkeypatch):
    tower = ux_preview.make_preview_tower()
    peer = next(row for row in tower.rows if row["transport"] == "remote_tmux")
    notices = []
    monkeypatch.setattr(control_room, "run_list_picker", lambda *_a, **_kw: (
        pytest.fail("remote peer must not open a focus confirmation")
    ))
    monkeypatch.setattr(control_room, "show_message_screen", lambda _s, _title, lines: notices.extend(lines))

    control_room._request_right_handoff(
        _Screen(), tower, peer, True, read_only_preview=True,
    )

    assert notices


@pytest.mark.parametrize("size", [(24, 80), (40, 120)])
def test_preview_keyboard_path_runs_at_requested_fixture_sizes(monkeypatch, size):
    _install_fake_terminal(monkeypatch)
    tower = ux_preview.make_preview_tower()
    screen = _Screen(*size, keys=("\t", "\n", "q"))

    control_room.open_control_room(screen, tower, read_only_preview=True)

    assert screen.getmaxyx() == size
    assert screen.timeouts[-1] == 200
