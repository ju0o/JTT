"""Small reusable curses building blocks for the Workspace Launcher wizard.

Kept separate from ``ui/tower.py`` and ``ui/launcher_wizard.py`` so the
wizard's screen-flow code doesn't get tangled up with raw curses cursor
math. Nothing here knows about tmux, agents, or projects.
"""

from __future__ import annotations

import curses
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from ..i18n import t
from .perf_trace import event
from .render import truncate_to_width


def read_key(stdscr):
    """Like ``getch()`` but Unicode-correct.

    ``getch()`` returns one *byte* at a time. For multi-byte UTF-8 input
    (e.g. any Korean character) that splits a single character into
    several meaningless byte values, each individually misdecoded via
    ``chr()`` -- a real bug, caught live when a Korean pane title came out
    as mojibake. ``get_wch()`` assembles a full multi-byte sequence into
    one proper character before returning it (requires
    ``locale.setlocale(locale.LC_ALL, "")`` to have been called before
    curses initialized -- see ``run()`` in ``ui/tower.py``).

    Returns an ``int`` for special keys (arrows, ``KEY_BACKSPACE``, ...)
    or a length-1 ``str`` for everything else, including control
    characters like Enter (``"\\n"``/``"\\r"``), Escape (``"\\x1b"``), and
    Ctrl+C (``"\\x03"``). Returns ``-1`` on timeout, matching ``getch()``.
    """

    try:
        key = stdscr.get_wch()
    except curses.error:
        return -1
    if key in (curses.KEY_UP, curses.KEY_DOWN, curses.KEY_LEFT, curses.KEY_RIGHT):
        category = "Arrow"
    elif is_enter(key):
        category = "Enter"
    elif is_escape(key):
        category = "Esc"
    elif key == " ":
        category = "Space"
    elif matches_letter(key, "j") or matches_letter(key, "k"):
        category = "JK"
    else:
        category = "Other"
    event("KEY_RECEIVED", key=category)
    return key


def is_enter(key) -> bool:
    return key in ("\n", "\r") or key == curses.KEY_ENTER


def is_escape(key) -> bool:
    return key == "\x1b"


def is_ctrl_c(key) -> bool:
    return key == "\x03"


def is_backspace(key) -> bool:
    return key in ("\x7f", "\x08") or key == curses.KEY_BACKSPACE


def matches_letter(key, letter: str) -> bool:
    """True if ``key`` (from ``read_key``) is the given single ASCII
    letter, case-insensitively -- the ``get_wch()`` equivalent of the old
    ``key in (ord("e"), ord("E"))`` pattern, in one call instead of two.
    """

    return isinstance(key, str) and len(key) == 1 and key.lower() == letter.lower()


def safe_add(stdscr, y, x, text, attr=0):
    """Write text clipped to the screen; never raises on an off-screen
    write (which curses does on the bottom-right cell of some terminals).
    The single implementation used by every screen in this package.

    Pre-truncates by *display* column width (see ``render.truncate_to_width``)
    before handing off to ``addnstr``, whose own ``n`` limit counts Python
    characters, not terminal columns -- a string full of double-width
    Korean characters could stay under that character-count limit while
    still overflowing well past the terminal's right edge, wrapping onto
    the next line. Caught live once rows started mixing Korean and English
    text on screen.
    """

    height, width = stdscr.getmaxyx()
    if y < 0 or y >= height or x < 0 or x >= width:
        return
    available = max(0, width - x - 1)
    clipped = truncate_to_width(text, available)
    try:
        stdscr.addnstr(y, x, clipped, len(clipped), attr)
    except curses.error:
        pass


@dataclass
class PickResult:
    cancelled: bool = False
    extra: Optional[str] = None
    selected_key: Any = None
    selected_keys: Set[Any] = field(default_factory=set)


def prompt_text(
    stdscr, label: str, initial: str = "", context_lines: Optional[Sequence[str]] = None
) -> Optional[str]:
    """Blocking single-line text input. Returns None if the user pressed Esc.

    ``context_lines`` (e.g. "Current: JuHome" / "Auto: f") are drawn above
    the input line, on an otherwise fully cleared screen -- earlier this
    only cleared the one input line, leaving whatever screen was open
    before it (a menu, the main list) visible underneath as a cosmetic
    artifact.
    """

    context_lines = list(context_lines or [])
    height, width = stdscr.getmaxyx()
    y = min(height - 2, 2 + len(context_lines) + 1)
    buf = list(initial)

    curses.curs_set(1)
    stdscr.timeout(-1)

    try:
        while True:
            stdscr.erase()
            for i, line in enumerate(context_lines):
                safe_add(stdscr, 2 + i, 0, line, curses.A_DIM)

            text = label + "".join(buf)
            safe_add(stdscr, y, 0, text, curses.A_BOLD)
            stdscr.move(y, min(width - 1, len(label) + len(buf)))
            stdscr.refresh()

            key = read_key(stdscr)

            if is_escape(key):
                return None
            if is_enter(key):
                return "".join(buf).strip()
            if is_backspace(key):
                if buf:
                    buf.pop()
                continue
            if isinstance(key, str) and key.isprintable():
                buf.append(key)
    finally:
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        stdscr.timeout(200)


def show_message_screen(
    stdscr,
    title: str,
    lines: Sequence[str],
    footer: Optional[str] = None,
    *,
    paginate: bool = False,
) -> None:
    """Show a blocking message; optional paging is reserved for long help."""

    stdscr.timeout(-1)
    try:
        height, _ = stdscr.getmaxyx()
        if not paginate:
            # Keep the established one-screen / any-key dismissal behavior
            # for ordinary status and confirmation messages.
            message_footer = footer if footer is not None else t("wizard.press_any_key")
            stdscr.erase()
            safe_add(stdscr, 0, 2, title, curses.A_BOLD)
            for i, line in enumerate(lines):
                safe_add(stdscr, 2 + i, 2, line)
            safe_add(stdscr, height - 1, 2, message_footer, curses.A_DIM)
            stdscr.refresh()
            read_key(stdscr)
            return

        page_size = max(1, height - 4)
        pages = [list(lines[index : index + page_size]) for index in range(0, len(lines), page_size)] or [[]]
        page = 0
        while True:
            stdscr.erase()
            safe_add(stdscr, 0, 2, title, curses.A_BOLD)
            for i, line in enumerate(pages[page]):
                safe_add(stdscr, 2 + i, 2, line)
            if len(pages) > 1:
                message_footer = t("help.page_navigation", page=page + 1, pages=len(pages))
            else:
                message_footer = footer if footer is not None else t("help.close")
            safe_add(stdscr, height - 1, 2, message_footer, curses.A_DIM)
            stdscr.refresh()

            key = read_key(stdscr)
            if is_escape(key):
                return
            if key in (curses.KEY_UP, curses.KEY_PPAGE) and page > 0:
                page -= 1
            elif key in (curses.KEY_DOWN, curses.KEY_NPAGE) and page + 1 < len(pages):
                page += 1
    finally:
        stdscr.timeout(200)


def run_list_picker(
    stdscr,
    title: str,
    items: List[Tuple[Any, str]],
    *,
    multi: bool = False,
    searchable: bool = False,
    checked: Optional[Set[Any]] = None,
    extra_keys: Optional[Dict[str, str]] = None,
    footer_hint: str = "",
    preamble: Optional[Sequence[str]] = None,
) -> PickResult:
    """Generic single/multi-select list picker with optional live search.

    ``items`` is ``[(key, label), ...]``. Filtering (when ``searchable``) is
    a case-insensitive substring match against ``label``. ``extra_keys``
    maps a single lowercase character to a tag string; pressing it
    immediately returns a ``PickResult`` with that tag in ``.extra`` (used
    for e.g. "B" = manual path entry, handled by the caller).
    """

    extra_keys = extra_keys or {}
    checked_keys: Set[Any] = set(checked or set())
    search = ""
    selected_index = 0
    # Two modes so a searchable list's letter hotkeys (e.g. "b" for manual
    # path) don't collide with typing "b" into the search box: "/" enters
    # search-editing mode, where every printable key edits the search text
    # instead of being treated as a hotkey, until Enter/Esc leaves it again
    # (the filter itself stays applied).
    editing_search = False
    first_draw = True

    stdscr.timeout(-1)

    try:
        while True:
            query = search.lower()
            visible = [(k, label) for k, label in items if query in label.lower()] if query else list(items)
            selected_index = max(0, min(selected_index, len(visible) - 1)) if visible else 0

            stdscr.erase()
            height, width = stdscr.getmaxyx()
            safe_add(stdscr, 0, 2, title, curses.A_BOLD)

            row_y = 2
            for line in preamble or []:
                safe_add(stdscr, row_y, 2, line)
                row_y += 1
            if preamble:
                row_y += 1
            if searchable:
                cursor = "_" if editing_search else ""
                safe_add(stdscr, row_y, 2, f'{t("wizard.search_label")} {search}{cursor}', curses.A_DIM)
                row_y += 2

            list_top = row_y
            max_rows = max(1, height - list_top - 3)

            top = 0
            if selected_index >= max_rows:
                top = selected_index - max_rows + 1

            if not visible:
                safe_add(stdscr, list_top, 2, t("wizard.no_matches"), curses.A_DIM)

            for offset, (key, label) in enumerate(visible[top : top + max_rows]):
                real_index = top + offset
                y = list_top + offset
                is_selected = real_index == selected_index
                base_attr = curses.A_REVERSE if is_selected else 0

                prefix = "  "
                if multi:
                    prefix = "[x] " if key in checked_keys else "[ ] "

                marker = "> " if is_selected else "  "
                safe_add(stdscr, y, 0, f"{marker}{prefix}{label}", base_attr)

            hint_y = height - 2
            safe_add(stdscr, hint_y, 2, footer_hint, curses.A_DIM)

            stdscr.refresh()
            if first_draw:
                event("FIRST_DRAW", view="menu")
                first_draw = False
            key_code = read_key(stdscr)

            if editing_search:
                if is_escape(key_code) or is_enter(key_code):
                    editing_search = False
                    continue
                if is_backspace(key_code):
                    search = search[:-1]
                    continue
                if isinstance(key_code, str) and key_code.isprintable():
                    search += key_code
                    selected_index = 0
                continue

            if is_escape(key_code):
                return PickResult(cancelled=True)

            if key_code == curses.KEY_UP or (not searchable and matches_letter(key_code, "k")):
                selected_index -= 1
                continue

            if key_code == curses.KEY_DOWN or (not searchable and matches_letter(key_code, "j")):
                selected_index += 1
                continue

            if is_enter(key_code):
                if not visible:
                    continue
                current_key = visible[selected_index][0]
                if multi:
                    return PickResult(selected_keys=checked_keys)
                return PickResult(selected_key=current_key)

            if multi and key_code == " ":
                if visible:
                    current_key = visible[selected_index][0]
                    if current_key in checked_keys:
                        checked_keys.discard(current_key)
                    else:
                        checked_keys.add(current_key)
                continue

            if searchable and key_code == "/":
                editing_search = True
                continue

            if isinstance(key_code, str) and key_code.lower() in extra_keys:
                return PickResult(
                    extra=extra_keys[key_code.lower()],
                    selected_key=(visible[selected_index][0] if visible else None),
                )
    finally:
        stdscr.timeout(200)
