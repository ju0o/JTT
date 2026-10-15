"""Tower-only live display for explicitly selected panes."""

from __future__ import annotations

import curses
import time

from ..control.actions import get_pane_screen
from ..i18n import t
from ..state.worksets import LAYOUTS
from .perf_trace import draw as trace_draw
from .render import display_width, truncate_to_width, use_narrow_layout
from .widgets import is_escape, read_key, run_list_picker, safe_add

REFRESH_SECONDS = 0.7
_PANE_IDENTITY_FIELDS = (
    "pane_id", "session", "pane_pid", "tmux_host", "execution_host", "transport",
)


def layout_slots(layout: str) -> int:
    return {"focus": 1, "split-2": 2, "side_by_side": 2,
            "grid-4": 4, "grid": 4, "main-plus-side": 4}[layout]


def choose_panes(stdscr, tower, layout: str):
    """Pick panes in displayed order, one slot at a time."""
    rows = [row for row in tower.visible_rows if row.get("kind") == "pane" and not row.get("placeholder")]
    count = min(layout_slots(layout), len(rows))
    selected = []
    for slot in range(count):
        remaining = [row for row in rows if row.get("key") not in selected]
        if not remaining:
            return None
        items = [(row["key"], pane_label(row)) for row in remaining if row.get("key")]
        pick = run_list_picker(
            stdscr,
            t("live.pick_slot").format(slot=slot + 1, count=count),
            items,
            searchable=True,
            footer_hint=t("wizard.hint_single_search"),
        )
        if pick.cancelled or pick.selected_key is None:
            return None
        selected.append(pick.selected_key)
    return selected


def pane_label(row: dict) -> str:
    role = row.get("role")
    if role:
        from ..state.overrides import ROLE_IDS

        if role in ROLE_IDS:
            return t(f"role.{role}")
    return row.get("display_name") or row.get("task_name") or row.get("project") or t("project.no_name")


def open_live_view(stdscr, tower, selected_key: str | None = None) -> None:
    if selected_key:
        _show_live_view(stdscr, tower, [selected_key], "focus")
        return
    layout_pick = run_list_picker(
        stdscr,
        t("live.layout_title"),
        [(key, t("layout." + key)) for key in LAYOUTS],
        footer_hint=t("wizard.hint_list"),
    )
    if layout_pick.cancelled or layout_pick.selected_key not in LAYOUTS:
        return
    layout = layout_pick.selected_key
    selected_keys = choose_panes(stdscr, tower, layout)
    if not selected_keys:
        return
    if len(selected_keys) == 1:
        layout = "focus"
    _show_live_view(stdscr, tower, selected_keys, layout)


def open_group_live_view(stdscr, tower, group: dict) -> None:
    """Reuse the normal live surface, scoped and ordered by one Work Group."""
    if not group.get("member_order"):
        store = getattr(tower, "work_groups", None)
        if store:
            group = next((item for item in store.all() if item.get("group_id") == group.get("group_id")), group)
    by_target = {str(row.get("target_id")): row for row in tower.rows if row.get("target_id")}
    order = list(group.get("member_order") or [])
    member_rank = {target: index for index, target in enumerate(order)}
    slots = group.get("layout_slots") or {}
    slot_rank = {"main": 0, "side-1": 1, "side-2": 2, "bottom": 3}
    layout_kind = group.get("layout") or "focus"
    if layout_kind == "main-plus-side":
        order.sort(key=lambda target: (slot_rank.get(slots.get(target), 4), member_rank.get(target, len(order))))
    rows = [by_target.get(target) or {
        "key": f"stale:{target}", "target_id": target, "kind": "work_group_stale",
        "display_name": (group.get("member_labels") or {}).get(target) or t("group.stale_member"),
        "status": "UNKNOWN", "stale": True,
    } for target in order]
    if not rows:
        from .widgets import show_message_screen

        show_message_screen(stdscr, t("live.title"), [t("live.empty")])
        return
    layout = layout_kind if layout_kind in LAYOUTS else "focus"
    if len(rows) == 1:
        layout = "focus"
    _show_live_view(stdscr, tower, [row["key"] for row in rows], layout)


def open_window_live_view(stdscr, tower, ref: str) -> None:
    keys = [row["key"] for row in tower.rows
            if row.get("pane_id") and not row.get("tower_runtime") and row.get("window_ref") == ref]
    if not keys:
        from .widgets import show_message_screen

        show_message_screen(stdscr, t("live.title"), [t("live.empty")])
        return
    _show_live_view(stdscr, tower, keys, "focus")


def open_folder_live_view(stdscr, tower, folder: dict) -> None:
    tasks = [row for row in tower.rows if row.get("pane_id") and not row.get("tower_runtime")]
    workspace = tower.folders.workspace(tower.window_assets, tasks)
    if folder.get("kind") == "other_section":
        refs = set(folder.get("window_refs") or ())
        windows = [window for window in workspace["unfiled_windows"]
                   if not refs or window.get("window_ref") in refs]
    else:
        item = next((value for value in workspace["folders"]
                     if value["folder_id"] == folder.get("folder_id")), None)
        windows = item.get("windows", []) if item else []
    choices = [(window["window_ref"], window["display_name"])
               for window in windows if not window.get("stale") and window.get("task_target_ids")]
    if not choices:
        from .widgets import show_message_screen

        show_message_screen(stdscr, t("live.title"), [t("live.empty")])
        return
    pick = run_list_picker(stdscr, t("folder.pick_window_live"), choices,
                           footer_hint=t("wizard.hint_list"))
    if not pick.cancelled and pick.selected_key:
        open_window_live_view(stdscr, tower, pick.selected_key)


def _show_live_view(stdscr, tower, selected_keys, layout: str) -> None:
    """Membership/order are fixed for this view; refreshed rows only update content."""
    initial_rows = {row.get("key"): row for row in tower.rows}
    identities = {key: _pane_identity(initial_rows.get(key)) for key in selected_keys}
    focused = 0
    scroll_offsets = {key: 0 for key in selected_keys}
    async_refresh = callable(getattr(tower, "start_background_refresh", None))
    keypad = getattr(stdscr, "keypad", None)
    if keypad:
        keypad(True)
    stdscr.timeout(25)
    if async_refresh:
        _start_refresh(tower)
    last_refresh = time.monotonic()
    needs_draw = True
    try:
        while True:
            if async_refresh:
                if _apply_refresh(tower):
                    needs_draw = True
            else:
                tower.load()
                needs_draw = True
            now = time.monotonic()
            if now - last_refresh >= REFRESH_SECONDS:
                _start_refresh(tower)
                last_refresh = now
            if needs_draw:
                rows = {row.get("key"): row for row in tower.rows}
                current_rows = []
                for key in selected_keys:
                    row = rows.get(key)
                    if identities.get(key) is None or _pane_identity(row) != identities[key]:
                        original = initial_rows.get(key)
                        row = {**(original or {"key": key, "display_name": t("group.stale_member")}), "_live_stale": True}
                    current_rows.append(row)
                capacity = layout_slots(layout)
                if layout == "focus":
                    visible_indices = [focused]
                    local_focus = 0
                else:
                    page_start = (focused // capacity) * capacity
                    visible_indices = list(range(page_start, min(len(selected_keys), page_start + capacity)))
                    local_focus = focused - page_start
                slots = [current_rows[index] for index in visible_indices]
                page_info = (focused + 1, len(selected_keys)) if len(selected_keys) > capacity or layout == "focus" and len(selected_keys) > 1 else None
                _draw(stdscr, tower, slots, local_focus, layout, scroll_offsets, page_info)
                needs_draw = False
            stdscr.timeout(25)
            key = read_key(stdscr)
            if key == -1:
                continue
            if key == curses.KEY_RESIZE:
                needs_draw = True
                continue
            needs_draw = True
            if is_escape(key) or key in ("q", "Q"):
                return
            if key in (curses.KEY_UP, curses.KEY_LEFT) and layout != "focus":
                focused = max(0, focused - 1)
            elif key in (curses.KEY_DOWN, curses.KEY_RIGHT) and layout != "focus":
                focused = min(len(selected_keys) - 1, focused + 1)
            elif key in (curses.KEY_UP, curses.KEY_PPAGE) and layout == "focus":
                scroll_offsets[selected_keys[focused]] += 5
            elif key in (curses.KEY_DOWN, curses.KEY_NPAGE) and layout == "focus":
                scroll_offsets[selected_keys[focused]] = max(0, scroll_offsets[selected_keys[focused]] - 5)
            elif key in (curses.KEY_PPAGE,) or key in ("[",):
                scroll_offsets[selected_keys[focused]] += 5
            elif key in (curses.KEY_NPAGE,) or key in ("]",):
                scroll_offsets[selected_keys[focused]] = max(0, scroll_offsets[selected_keys[focused]] - 5)
            elif key == curses.KEY_HOME:
                scroll_offsets[selected_keys[focused]] = 0
            elif key in ("n", "N"):
                focused = min(len(selected_keys) - 1, focused + (1 if layout == "focus" else capacity))
            elif key in ("p", "P"):
                focused = max(0, focused - (1 if layout == "focus" else capacity))
            elif key in ("\n", "\r", curses.KEY_ENTER):
                row = current_rows[focused]
                if row and not row.get("placeholder") and not row.get("_live_stale"):
                    from .control_view import open_control_view

                    open_control_view(stdscr, tower, selected_keys[focused])
            elif key in ("y", "Y"):
                from .control_view import _copy_result
                from .widgets import show_message_screen

                row = current_rows[focused]
                message = t("live.changed_output") if row.get("_live_stale") else _copy_result(tower, selected_keys[focused], stdscr)
                show_message_screen(stdscr, t("detail.result"), [message or t("control.no_result")])
            elif key == " ":
                row = current_rows[focused]
                if row.get("_live_stale"):
                    from .widgets import show_message_screen

                    show_message_screen(stdscr, t("live.title"), [t("live.changed_output")])
                else:
                    from .structure_menu import open_pane_menu

                    open_pane_menu(stdscr, tower, row)
            elif key in ("g", "G"):
                from .control_view import _go
                from .widgets import show_message_screen

                row = current_rows[focused]
                message = t("live.changed_output") if row.get("_live_stale") else _go(tower, row)
                if message != t("control.focused"):
                    show_message_screen(stdscr, t("nav.terminal_structure"), [message])
    finally:
        stdscr.timeout(200)


def _pane_identity(row):
    if not isinstance(row, dict) or row.get("kind") != "pane":
        return None
    values = tuple(str(row.get(field) or "") for field in _PANE_IDENTITY_FIELDS)
    if not all(values):
        return None
    return values + (bool(row.get("remote")), str(row.get("transport_target") or ""))


@trace_draw("live")
def _draw(stdscr, tower, slots, focused: int, layout: str, scroll_offsets=None, page_info=None) -> None:
    stdscr.erase()
    height, width = stdscr.getmaxyx()
    safe_add(stdscr, 0, 2, t("live.title"), curses.A_BOLD)
    hint = t("live.hint")
    safe_add(stdscr, 1, 2, truncate_to_width(hint, max(0, width - 4)), curses.A_DIM)
    narrow = use_narrow_layout(width) or height < 18
    if narrow and (len(slots) > 1 or layout != "focus"):
        row = slots[focused]
        offset = (scroll_offsets or {}).get(row.get("key"), 0) if row else 0
        _draw_tile(stdscr, tower, row, focused, 3, 0, height - 5, width, True, offset)
        footer = t("live.narrow")
        if page_info:
            footer += " · " + t("live.page").format(current=page_info[0], count=page_info[1])
        if offset:
            footer += " · " + t("live.follow_paused")
        safe_add(stdscr, height - 1, 2, truncate_to_width(footer, max(0, width - 4)), curses.A_DIM)
        stdscr.noutrefresh()
        curses.doupdate()
        return

    if layout == "focus":
        cells = [(3, 0, height - 5, width)]
    elif layout in ("split-2", "side_by_side"):
        half = width // 2
        cells = [(3, 0, height - 5, half), (3, half, height - 5, width - half)]
    elif layout in ("grid-4", "grid"):
        half_w, half_h = width // 2, max(1, (height - 3) // 2)
        cells = [
            (3, 0, half_h, half_w), (3, half_w, half_h, width - half_w),
            (3 + half_h, 0, max(0, height - 5 - half_h), half_w),
            (3 + half_h, half_w, max(0, height - 5 - half_h), width - half_w),
        ]
    else:
        top, body_height = 3, max(0, height - 5)
        main_width = max(1, (width * 2) // 3)
        side_width = width - main_width
        side_count = max(1, min(3, len(slots) - 1))
        side_height = body_height // side_count
        cells = [(top, 0, body_height, main_width)] + [
            (top + side_height * index, main_width, side_height if index < side_count - 1 else body_height - side_height * index, side_width)
            for index in range(side_count)
        ]
    for index, (y, x, h, w) in enumerate(cells[:len(slots)]):
        row = slots[index]
        offset = (scroll_offsets or {}).get(row.get("key"), 0) if row else 0
        _draw_tile(stdscr, tower, row, index, y, x, h, w, index == focused, offset)
    footer = t("live.page").format(current=page_info[0], count=page_info[1]) if page_info else t("live.footer")
    if any((scroll_offsets or {}).get(row.get("key"), 0) for row in slots if row):
        footer += " · " + t("live.follow_paused")
    safe_add(stdscr, height - 1, 2, truncate_to_width(footer, max(0, width - 4)), curses.A_DIM)
    stdscr.noutrefresh()
    curses.doupdate()


def _draw_tile(stdscr, tower, row, index: int, y: int, x: int, height: int, width: int, focused: bool, scroll: int = 0) -> None:
    if height <= 0 or width <= 2:
        return
    attr = curses.A_REVERSE if focused else curses.A_NORMAL
    title = pane_label(row) if row else t("live.missing")
    _tile_add(stdscr, y, x + 1, width, x, ("› " if focused else "  ") + title, attr | curses.A_BOLD)
    if row and row.get("_live_stale"):
        _tile_add(stdscr, y + 1, x + 2, width, x, t("live.changed"), curses.A_BOLD)
        _tile_add(
            stdscr, y + 2, x + 2, width, x,
            f'{t("detail.attention")}: {t("live.attention_unknown")}', curses.A_BOLD,
        )
        lines = [t("live.changed_output")]
    elif row:
        symbol, execution = _execution(row)
        attention = _attention(row)
        result = _result(row)
        attention_attr = curses.A_BOLD if attention != t("live.attention_none") else curses.A_NORMAL
        result_attr = curses.A_BOLD if row.get("result_state") == "ready" else curses.A_NORMAL
        _tile_add(
            stdscr, y + 1, x + 2, width, x,
            f'{row.get("agent") or t("state.unknown")} · {symbol} {execution}', curses.A_BOLD,
        )
        badge = []
        if attention != t("live.attention_none"):
            badge.append(f'! {attention}')
        if row.get("result_state") == "ready":
            badge.append(f'✓ {result}')
        if badge:
            _tile_add(stdscr, y + 2, x + 2, width, x, " · ".join(badge), (attention_attr | result_attr))
        lines = _live_lines(tower, row)
    else:
        _tile_add(stdscr, y + 1, x + 2, width, x, t("live.unavailable"))
        lines = []
    available = max(0, height - 3)
    end = max(0, len(lines) - scroll)
    start = max(0, end - available)
    for offset, line in enumerate(lines[start:end]):
        _tile_add(stdscr, y + 3 + offset, x + 2, width, x, line, curses.A_DIM)


def _tile_add(stdscr, y: int, column: int, tile_width: int, tile_x: int, text: str, attr=0) -> None:
    """Clip to the tile boundary before the screen-wide safe writer."""
    remaining = tile_width - (column - tile_x) - 1
    if remaining > 0:
        safe_add(stdscr, y, column, truncate_to_width(text, remaining), attr)


def _live_lines(tower, row: dict) -> list:
    if row.get("remote"):
        return [t("live.unavailable")]
    if row.get("status") == "DEAD":
        return [t("live.ended")]
    if "live_lines" in row:
        return row.get("live_lines") or [t("live.unavailable")]
    ok, reason, payload = get_pane_screen(
        tower.session,
        row.get("key") or "",
        tower.own_pane_id,
        expected_pane_pid=str(row.get("pane_pid") or ""),
    )
    if ok:
        return payload.get("lines") or [t("live.unavailable")]
    if reason == "stale":
        return [t("live.changed_output")]
    if reason == "not_found":
        return [t("live.ended")]
    return [t("live.unavailable")]


def _start_refresh(tower) -> bool:
    start = getattr(tower, "start_background_refresh", None)
    if callable(start):
        return bool(start())
    tower.load()
    return True


def _apply_refresh(tower) -> bool:
    apply = getattr(tower, "apply_background_refresh", None)
    return bool(apply()) if callable(apply) else False


def _execution(row: dict):
    status = row.get("status")
    labels = {"WORKING": ("●", "status.WORKING"), "WAITING": ("!", "status.WAITING"),
              "IDLE": ("○", "status.IDLE"), "DEAD": ("×", "status.DEAD")}
    mark, key = labels.get(status, ("?", "status.UNKNOWN"))
    return mark, t(key)


def _attention(row: dict) -> str:
    kind = row.get("attention") or "none"
    interaction = row.get("interaction") or {}
    interaction_type = interaction.get("type")
    options = [
        option for option in (interaction.get("options") or [])
        if option.get("safe") and option.get("key")
    ]
    measured = interaction.get("confidence") == "high"
    if interaction_type == "choice":
        return t("state.choice") if measured and len(options) >= 2 else t("live.attention_unknown")
    if interaction_type == "confirm":
        return t("state.choice") if measured and len(options) >= 2 else t("live.attention_unknown")
    if interaction_type == "text":
        return t("state.answer")
    if interaction_type == "approval":
        return t("state.approval") if measured and options else t("live.attention_unknown")
    if kind == "error":
        return t("state.error")
    if interaction_type == "unknown":
        return t("live.attention_unknown")
    if kind == "input_required":
        return t("state.answer")
    return t("live.attention_none") if kind == "none" else t("live.attention_unknown")


def _result(row: dict) -> str:
    state = row.get("result_state")
    return t("state.result") if state == "ready" else t("control.result_read") if state == "read" else t("live.result_none") if state == "none" else t("state.unknown")
