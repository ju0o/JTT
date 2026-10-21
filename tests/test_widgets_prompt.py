import curses
import pytest

from tmux_agent_tower.i18n import t
from tmux_agent_tower.ui import widgets


@pytest.fixture(autouse=True)
def _no_real_curses(monkeypatch):
    # prompt_text calls curses.curs_set(), which touches the real terminal
    # and requires initscr() to have run -- irrelevant to what these tests
    # check (the screen-clearing/input-buffer logic), so it's stubbed out.
    monkeypatch.setattr(widgets.curses, "curs_set", lambda *_: None)


class FakeStdscr:
    """Minimal fake curses window supporting exactly what prompt_text uses."""

    def __init__(self, keys):
        self._keys = list(keys)
        self.erase_calls = 0
        self.written = []

    def getmaxyx(self):
        return (24, 80)

    def erase(self):
        self.erase_calls += 1

    def addnstr(self, y, x, text, n, attr=0):
        self.written.append((y, x, text))

    def move(self, y, x):
        pass

    def refresh(self):
        pass

    def timeout(self, ms):
        pass

    def get_wch(self):
        return self._keys.pop(0)


class FakeMessageStdscr:
    def __init__(self, height, width, keys):
        self.size = (height, width)
        self._keys = list(keys)
        self.frame = []
        self.frames = []
        self.timeouts = []

    def getmaxyx(self):
        return self.size

    def erase(self):
        self.frame = []

    def addnstr(self, y, x, text, _n, _attr=0):
        self.frame.append((y, x, text))

    def refresh(self):
        self.frames.append(list(self.frame))

    def timeout(self, value):
        self.timeouts.append(value)

    def get_wch(self):
        return self._keys.pop(0)


def test_prompt_text_erases_the_screen_each_redraw():
    # Regression: prompt_text used to only clear the single input line via
    # move()+clrtoeol(), leaving whatever screen was open before it (a
    # menu, the main list) visible underneath -- a real cosmetic bug.
    fake = FakeStdscr(list("hi") + ["\n"])
    result = widgets.prompt_text(fake, "Label: ")
    assert result == "hi"
    assert fake.erase_calls >= 1


def test_prompt_text_draws_context_lines_above_input():
    fake = FakeStdscr(["\n"])
    widgets.prompt_text(fake, "Label: ", context_lines=["Current: X", "Auto: Y"])
    texts = [text for _, _, text in fake.written]
    assert "Current: X" in texts
    assert "Auto: Y" in texts


def test_prompt_text_escape_returns_none():
    fake = FakeStdscr(["\x1b"])
    assert widgets.prompt_text(fake, "Label: ") is None


def test_prompt_text_backspace_removes_last_char():
    fake = FakeStdscr(list("ab") + ["\x7f", "\n"])
    assert widgets.prompt_text(fake, "Label: ") == "a"


def test_prompt_text_still_submits_on_the_first_newline():
    fake = FakeStdscr(list("ab") + ["\n", "c", "\n"])
    assert widgets.prompt_text(fake, "Label: ") == "ab"


def test_help_pages_expose_every_entry_at_small_and_standard_sizes():
    from tmux_agent_tower.ui import tower
    from tmux_agent_tower.ui.render import display_width

    help_lines = tower._help_lines()
    assert len(help_lines) == 18
    assert all(display_width(line) <= 56 for line in help_lines)
    small = FakeMessageStdscr(18, 60, [curses.KEY_NPAGE, curses.KEY_PPAGE, curses.KEY_NPAGE, "\x1b"])
    widgets.show_message_screen(small, "기본 사용법", help_lines, paginate=True)

    assert len(small.frames) == 4
    assert t("help.page_navigation", page=1, pages=2) in {text for _, _, text in small.frames[0]}
    assert t("help.page_navigation", page=2, pages=2) in {text for _, _, text in small.frames[1]}
    visible_small = {text for frame in small.frames for _, _, text in frame}
    assert all(line in visible_small for line in help_lines)

    standard = FakeMessageStdscr(24, 80, ["\x1b"])
    widgets.show_message_screen(standard, "기본 사용법", help_lines, paginate=True)
    assert len(standard.frames) == 1
    visible_standard = {text for _, _, text in standard.frames[0]}
    assert all(line in visible_standard for line in help_lines)
    assert t("help.close") in visible_standard


def test_regular_message_screen_keeps_any_key_dismissal():
    screen = FakeMessageStdscr(18, 60, ["x"])
    widgets.show_message_screen(screen, "알림", ["완료"])
    assert len(screen.frames) == 1
    assert t("wizard.press_any_key") in {text for _, _, text in screen.frames[0]}
    assert screen._keys == []
    assert screen.timeouts == [-1, 200]
