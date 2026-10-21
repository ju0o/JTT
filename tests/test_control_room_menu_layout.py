import curses
from types import SimpleNamespace

import pytest

from tmux_agent_tower.i18n import t
from tmux_agent_tower.ui import control_room, projects, widgets
from tmux_agent_tower.ui.render import display_width


class Screen:
    def __init__(self, height, width, keys=()):
        self.size = (height, width)
        self.keys = iter(keys)
        self.writes = []
        self.frames = []

    def getmaxyx(self):
        return self.size

    def erase(self):
        self.writes.clear()

    def addnstr(self, y, x, text, _n, attr=0):
        self.writes.append((y, x, text, attr))

    def timeout(self, _value):
        pass

    def refresh(self):
        self.frames.append(list(self.writes))

    def get_wch(self):
        return next(self.keys)


@pytest.mark.parametrize("height,width", [(24, 80), (40, 120), (44, 160)])
def test_control_room_picker_is_compact_when_wide_and_safe_when_narrow(height, width):
    screen = Screen(height, width, ["\x1b"])
    result = widgets.run_list_picker(
        screen,
        "이 작업에서 할 일",
        [("copy", "최신 답변 복사"), ("details", "현재 Pane 상세 정보"), ("cancel", "취소")],
        footer_hint="↑↓ 선택 · Enter 선택 · Esc 취소",
        centered=True,
    )

    assert result.cancelled is True
    frame = screen.frames[-1]
    assert frame
    for y, x, text, _attr in frame:
        assert 0 <= y < height
        assert 0 <= x < width
        assert display_width(text) <= width - x - 1

    is_wide = width >= 110 and height >= 32
    borders = [(y, x, text) for y, x, text, _attr in frame if text.startswith("+") and text.endswith("+")]
    if is_wide:
        assert len(borders) == 2
        top, left, top_line = borders[0]
        bottom, _left, _bottom_line = borders[1]
        assert 0 < left < width // 2
        assert top < height // 2 < bottom
        assert top_line.count("-") >= 50
        assert any(y == top + 1 and x == left + 2 and "이 작업에서 할 일" in text
                   for y, x, text, _attr in frame)
        assert any(attr & curses.A_REVERSE for _y, _x, _text, attr in frame)
        assert any("Esc 취소" in text for _y, _x, text, _attr in frame)
    else:
        assert not borders
        assert any(y == 0 and x == 2 and "이 작업에서 할 일" in text
                   for y, x, text, _attr in frame)
        assert any("Esc 취소" in text for _y, _x, text, _attr in frame)


def test_confirmation_picker_mouse_click_only_selects_and_enter_remains_required(monkeypatch):
    screen = Screen(44, 160, [curses.KEY_MOUSE, "\x1b"])

    def clicked_confirmation_row():
        frame = screen.frames[-1]
        y, x, _text, _attr = max(
            (item for item in frame if "조작 화면으로 이동" in item[2]),
            key=lambda item: item[0],
        )
        return (0, x + 2, y, 0, curses.BUTTON1_CLICKED)

    monkeypatch.setattr(widgets.curses, "getmouse", clicked_confirmation_row)
    result = widgets.run_list_picker(
        screen, "Agent 조작 화면으로 이동",
        [("cancel", "취소"), ("confirm", "조작 화면으로 이동")],
        preamble=["선택한 터미널로 이동합니다."], centered=True,
    )

    assert result.cancelled is True
    selected = max((item for item in screen.frames[-1] if "조작 화면으로 이동" in item[2]),
                   key=lambda item: item[0])
    assert selected[3] & curses.A_REVERSE


def test_saved_screen_menu_explains_screen_vs_reusable_layout():
    assert "작업 구성" in t("control_room.screens_help_screen")
    assert "분할 모양만" in t("control_room.screens_help_layout")


def test_saved_screen_menu_passes_explanation_into_picker(monkeypatch):
    screen = Screen(44, 160, ["\x1b"])
    store = SimpleNamespace(screens=lambda: [], templates=lambda: [], last_view=lambda: None, corrupt=False)
    tower = SimpleNamespace(control_screens=store)
    state = {}

    control_room._open_screen_menu(screen, tower, state, [], [], None)

    frame = "\n".join(text for _y, _x, text, _attr in screen.writes)
    assert "작업 구성" in frame
    assert "분할 모양만" in frame


def test_control_room_project_picker_uses_centered_picker_variant(monkeypatch):
    calls = []
    store = SimpleNamespace(all=lambda: [])
    tower = SimpleNamespace(projects=store)
    monkeypatch.setattr(
        control_room, "run_list_picker",
        lambda *_args, **_kwargs: SimpleNamespace(cancelled=False, selected_key="projects"),
    )
    monkeypatch.setattr(
        projects, "run_list_picker",
        lambda *_args, **kwargs: calls.append(kwargs) or SimpleNamespace(cancelled=True),
    )

    control_room._open_action_menu(Screen(40, 120, []), tower, None, [], None, "tower_projects_root")

    assert calls and calls[0].get("centered") is True
