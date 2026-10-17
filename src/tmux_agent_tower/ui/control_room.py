"""Read-only, three-panel view over Tower's existing pane snapshot.

This screen never captures a pane during drawing. It renders only the
``live_lines`` already carried by the Tower refresh snapshot and delegates
tile rendering to the established LIVE view renderer.
"""

from __future__ import annotations

import curses
import math
import time

from ..i18n import t
from . import live_view, render
from .perf_trace import draw as trace_draw
from .widgets import is_escape, read_key, safe_add, show_message_screen

MAX_GRID_PANES = 4
_IDENTITY_FIELDS = (
    "target_id", "tmux_host", "session", "session_id", "window_id", "pane_id",
    "pane_pid", "transport", "transport_target",
)


def pane_identity(row: dict | None) -> tuple | None:
    """Stable pane identity for selection continuity, not a user-facing ID."""

    if not isinstance(row, dict) or row.get("kind") != "pane":
        return None
    values = tuple(str(row.get(field) or "") for field in _IDENTITY_FIELDS)
    if not all(values[index] for index in (0, 2, 5, 6)):
        return None
    return values + (bool(row.get("remote")),)


def pane_rows(rows) -> list[dict]:
    """Pane rows in discovery order; Tower's own runtime is never a work item."""

    return [
        row for row in rows or ()
        if isinstance(row, dict) and row.get("kind") == "pane"
        and row.get("pane_id") and row.get("key") and not row.get("tower_runtime")
    ]


def _host_label(row: dict) -> str:
    execution = str(row.get("execution_host") or row.get("host") or "")
    tmux_host = str(row.get("tmux_host") or execution)
    if row.get("transport") == "ssh" and execution and tmux_host and execution.casefold() != tmux_host.casefold():
        return t("control_room.ssh_host").format(execution=execution, tmux=tmux_host)
    return execution or tmux_host or t("state.unknown")


def navigation_lines(rows, selected_key: str, selected_identity: tuple | None) -> list[dict]:
    """Flat drawing model for Host → Session → Window → project → Pane."""

    result: list[dict] = []
    previous = None
    for row in rows:
        window_key = (
            str(row.get("tmux_host") or ""), str(row.get("session_id") or row.get("session") or ""),
            str(row.get("window_id") or row.get("window_index") or ""), bool(row.get("remote")),
        )
        group_name = str(row.get("work_group_name") or row.get("project") or "").strip()
        host = _host_label(row)
        session = str(row.get("session") or t("state.unknown"))
        window_index = row.get("window_index")
        window_name = str(row.get("window_name") or "").strip()
        window = f"{window_index}: {window_name}" if window_index is not None and window_name else (
            window_name or str(window_index) if window_index is not None else t("state.unknown")
        )

        if previous is None or previous[0] != host:
            result.append({"kind": "host", "text": host})
        if previous is None or previous[1] != (host, session):
            result.append({"kind": "session", "text": t("control_room.session").format(session=session)})
        if previous is None or previous[2] != window_key:
            result.append({"kind": "window", "text": t("control_room.window").format(window=window)})
        if group_name and (previous is None or previous[3] != (window_key, group_name)):
            result.append({"kind": "project", "text": t("control_room.project").format(project=group_name)})

        name = str(row.get("display_name") or row.get("task_name") or row.get("project") or t("task.terminal"))
        agent = str(row.get("agent") or "").strip()
        text = f"{name} · {agent}" if agent else name
        result.append({
            "kind": "pane", "text": text, "key": row.get("key"),
            "identity": pane_identity(row), "row": row,
            "selected": row.get("key") == selected_key and pane_identity(row) == selected_identity,
        })
        previous = (host, (host, session), window_key, (window_key, group_name) if group_name else None)
    return result


def related_panes(selected: dict, rows, groups=(), limit: int = MAX_GRID_PANES) -> list[dict]:
    """Use the selected Work Group or physical window without reordering tmux."""

    selected_id = pane_identity(selected)
    candidates: list[dict] = []
    group_id = str(selected.get("work_group_id") or "")
    group = next((item for item in groups or () if str(item.get("group_id") or "") == group_id), None)
    if group:
        by_target = {str(row.get("target_id") or ""): row for row in pane_rows(rows)}
        for target in group.get("member_order") or group.get("member_target_ids") or ():
            target = str(target)
            row = by_target.get(target)
            if row:
                candidates.append(row)
            else:
                label = (group.get("member_labels") or {}).get(target) or t("group.stale_member")
                candidates.append({
                    "key": f"stale:{target}", "target_id": target,
                    "kind": "work_group_stale", "display_name": str(label),
                    "status": "UNKNOWN", "remote": False, "live_lines": [],
                })
    else:
        selected_window = (
            str(selected.get("tmux_host") or ""),
            str(selected.get("session_id") or selected.get("session") or ""),
            str(selected.get("window_id") or selected.get("window_index") or ""),
            bool(selected.get("remote")),
        )
        candidates = [row for row in pane_rows(rows) if (
            str(row.get("tmux_host") or ""),
            str(row.get("session_id") or row.get("session") or ""),
            str(row.get("window_id") or row.get("window_index") or ""),
            bool(row.get("remote")),
        ) == selected_window]

    if not any(pane_identity(row) == selected_id for row in candidates):
        candidates.insert(0, selected)
    selected_index = next((index for index, row in enumerate(candidates) if pane_identity(row) == selected_id), 0)
    if len(candidates) > limit:
        start = max(0, min(selected_index - 1, len(candidates) - limit))
        candidates = candidates[start:start + limit]
    return candidates


def _resolve_selection(rows, key: str, identity: tuple | None, original: dict) -> tuple[dict, bool]:
    current = next((row for row in pane_rows(rows) if row.get("key") == key), None)
    if identity is None or current is None or pane_identity(current) != identity:
        return {**original, "_live_stale": True, "live_lines": []}, False
    return current, True


def _render_row(row: dict) -> dict:
    """Ensure the shared tile renderer cannot trigger a fresh pane capture."""

    value = dict(row)
    value["live_lines"] = list(row.get("live_lines") or ())
    # Task name is the tile title; Agent remains a separate secondary line.
    value["role"] = None
    return value


def panel_regions(width: int, height: int) -> tuple[tuple[int, int, int, int], ...]:
    """Three columns when roomy, three stacked panels when space is tight."""

    top = 2
    total_height = max(0, height - top - 1)  # reserve the final row for the quiet footer
    if width >= 110 and height >= 22:
        usable = max(0, width - 4)  # side margins and two one-cell gutters
        left = max(22, usable * 24 // 100)
        center = max(36, usable * 42 // 100)
        if left + center > usable:
            left = usable // 4
            center = usable * 2 // 5
        right = max(0, usable - left - center)
        x_left = 1
        x_center = x_left + left + 1
        x_right = x_center + center + 1
        return (
            (x_left, top, left, total_height),
            (x_center, top, center, total_height),
            (x_right, top, right, total_height),
        )

    first = total_height // 3
    second = total_height // 3
    third = total_height - first - second
    width = max(0, width - 2)
    return (
        (1, top, width, first),
        (1, top + first, width, second),
        (1, top + first + second, width, third),
    )


def _heading_attr() -> int:
    try:
        return (curses.color_pair(4) if curses.has_colors() else 0) | curses.A_BOLD
    except curses.error:
        return curses.A_BOLD


def _draw_navigation(stdscr, region, rows, selected_key, selected_identity) -> None:
    x, y, width, height = region
    lines = navigation_lines(rows, selected_key, selected_identity)
    pane_lines = [index for index, line in enumerate(lines) if line["kind"] == "pane"]
    selected_line = next((index for index in pane_lines if lines[index].get("selected")), pane_lines[0] if pane_lines else 0)
    capacity = max(0, height)
    top = max(0, selected_line - capacity + 1)
    visible = lines[top:top + capacity]
    for offset, line in enumerate(visible):
        kind = line["kind"]
        prefix = "▶ " if kind == "pane" and line.get("selected") else "  " if kind == "pane" else ""
        attr = (curses.A_REVERSE | curses.A_BOLD) if kind == "pane" and line.get("selected") else (
            _heading_attr() if kind in {"host", "project"} else curses.A_DIM
        )
        text = prefix + str(line.get("text") or "")
        safe_add(stdscr, y + offset, x, render.truncate_to_width(text, max(0, width)), attr)


def _draw_grid(stdscr, tower, region, rows, selected_identity, screen_width) -> None:
    x, y, width, height = region
    rows = list(rows[:MAX_GRID_PANES])
    if not rows:
        safe_add(stdscr, y, x, t("live.unavailable"), curses.A_DIM)
        return
    cols = grid_columns(width, height, screen_width, len(rows))
    row_count = (len(rows) + cols - 1) // cols
    cell_width = width // cols
    cell_height = max(0, height // row_count)
    for index, row in enumerate(rows):
        cell_y = y + (index // cols) * cell_height
        cell_x = x + (index % cols) * cell_width
        current_width = width - (index % cols) * cell_width if index % cols == cols - 1 else cell_width
        if index % cols:
            safe_add(stdscr, cell_y, cell_x, "│", curses.A_DIM)
        safe_row = _render_row(row)
        focused = pane_identity(row) == selected_identity
        live_view._draw_tile(
            stdscr, tower, safe_row, index, cell_y, cell_x, cell_height,
            current_width, focused,
        )


def grid_columns(panel_width: int, panel_height: int, screen_width: int, item_count: int) -> int:
    """Keep enough vertical room for each LIVE card while using 1–4 columns."""

    if item_count <= 1:
        return 1
    maximum = max(1, min(4, panel_width // 20))
    preferred = min(2, max(1, panel_width // 26)) if screen_width < 100 else maximum
    rows_that_fit = max(1, panel_height // 2)
    needed = math.ceil(item_count / rows_that_fit)
    return min(maximum, max(1, preferred, needed))


@trace_draw("control_room")
def draw(stdscr, tower, selected_key: str, selected_identity: tuple | None,
         original: dict, groups=()) -> bool:
    """Draw a read-only room and return whether the selection is still current."""

    stdscr.erase()
    height, width = stdscr.getmaxyx()
    safe_add(stdscr, 0, 2, t("control_room.title"), curses.A_BOLD)
    safe_add(
        stdscr, 1, 2,
        render.truncate_to_width(t("control_room.read_only"), max(0, width - 4)),
        curses.A_DIM,
    )
    panes = pane_rows(getattr(tower, "rows", ()))
    selected, current = _resolve_selection(panes, selected_key, selected_identity, original)
    display_rows = list(panes)
    if not current:
        display_rows.append(selected)
    regions = panel_regions(width, height)
    headings = (t("control_room.panel_location"), t("control_room.panel_grid"), t("control_room.panel_focus"))
    for region, heading in zip(regions, headings):
        x, y, region_width, region_height = region
        safe_add(stdscr, y, x, render.truncate_to_width(heading, max(0, region_width)), _heading_attr())

    _draw_navigation(
        stdscr, (regions[0][0], regions[0][1] + 1, regions[0][2], max(0, regions[0][3] - 1)),
        display_rows, selected_key, selected_identity,
    )
    if current:
        grid_rows = related_panes(selected, panes, groups)
    else:
        grid_rows = [selected]
    _draw_grid(
        stdscr, tower,
        (regions[1][0], regions[1][1] + 1, regions[1][2], max(0, regions[1][3] - 1)),
        grid_rows, selected_identity if current else None, width,
    )
    _draw_grid(
        stdscr, tower,
        (regions[2][0], regions[2][1] + 1, regions[2][2], max(0, regions[2][3] - 1)),
        [selected], selected_identity if current else None, width,
    )

    footer = t("control_room.footer")
    safe_add(stdscr, max(0, height - 1), 2, render.truncate_to_width(footer, max(0, width - 4)), curses.A_DIM)
    stdscr.noutrefresh()
    curses.doupdate()
    return current


def _sync_home_selection(tower, key: str, identity: tuple | None) -> None:
    for index, row in enumerate(getattr(tower, "visible_rows", ())):
        if row.get("key") == key and pane_identity(row) == identity:
            tower.selected = index
            return


def initial_pane(panes, selected_home: dict | None, groups=()) -> dict | None:
    """Keep the existing Home scope when LIVE is opened from a group or window."""

    if not panes:
        return None
    kind = (selected_home or {}).get("kind")
    if kind == "pane":
        identity = pane_identity(selected_home)
        return next((row for row in panes if row.get("key") == selected_home.get("key")
                     and identity is not None and pane_identity(row) == identity), panes[0])
    if kind == "work_group":
        group_id = str(selected_home.get("group_id") or "")
        group = next((item for item in groups or () if str(item.get("group_id") or "") == group_id), None)
        by_target = {str(row.get("target_id") or ""): row for row in panes}
        for target in (group or {}).get("member_order") or (group or {}).get("member_target_ids") or ():
            if str(target) in by_target:
                return by_target[str(target)]
    if kind in {"folder", "other_section"}:
        refs = set(selected_home.get("window_refs") or ())
        row = next((item for item in panes if item.get("window_ref") in refs), None)
        if row:
            return row
    if kind == "window_asset":
        ref = selected_home.get("window_ref")
        row = next((item for item in panes if ref and item.get("window_ref") == ref), None)
        if row:
            return row
    if kind == "window":
        session = str(selected_home.get("session") or "")
        window_id = str(selected_home.get("window_id") or "")
        host = str(selected_home.get("tmux_host") or "")
        row = next((item for item in panes if (
            str(item.get("session") or "") == session
            and str(item.get("window_id") or "") == window_id
            and (not host or str(item.get("tmux_host") or "") == host)
        )), None)
        if row:
            return row
    return panes[0]


def open_control_room(stdscr, tower) -> None:
    """Open the quiet room from the existing LIVE entry; no Agent input is sent."""

    panes = pane_rows(getattr(tower, "rows", ()))
    if not panes:
        show_message_screen(stdscr, t("control_room.title"), [t("live.empty")])
        return
    visible = getattr(tower, "visible_rows", ())
    selected_home = visible[tower.selected] if visible and 0 <= tower.selected < len(visible) else None
    groups = getattr(getattr(tower, "work_groups", None), "all", lambda: [])()
    selected = initial_pane(panes, selected_home, groups) or panes[0]
    selected_key = str(selected.get("key") or "")
    selected_identity = pane_identity(selected)
    original = dict(selected)

    keypad = getattr(stdscr, "keypad", None)
    if keypad:
        keypad(True)
    stdscr.timeout(25)
    async_refresh = callable(getattr(tower, "start_background_refresh", None))
    last_refresh = time.monotonic()
    needs_draw = True
    if async_refresh:
        live_view._start_refresh(tower)
    try:
        while True:
            if async_refresh and live_view._apply_refresh(tower):
                needs_draw = True
            now = time.monotonic()
            if now - last_refresh >= live_view.REFRESH_SECONDS:
                live_view._start_refresh(tower)
                last_refresh = now
            if needs_draw:
                current = draw(stdscr, tower, selected_key, selected_identity, original, groups)
                if current:
                    _sync_home_selection(tower, selected_key, selected_identity)
                needs_draw = False
            key = read_key(stdscr)
            if key == -1:
                continue
            if key == curses.KEY_RESIZE:
                needs_draw = True
                continue
            if is_escape(key) or key in ("q", "Q"):
                return
            if key in (curses.KEY_UP, curses.KEY_DOWN):
                current_panes = pane_rows(getattr(tower, "rows", ()))
                if not current_panes:
                    needs_draw = True
                    continue
                index = next((i for i, row in enumerate(current_panes)
                              if row.get("key") == selected_key and pane_identity(row) == selected_identity), None)
                if index is None:
                    index = -1 if key == curses.KEY_DOWN else 0
                index = (index + (1 if key == curses.KEY_DOWN else -1)) % len(current_panes)
                selected = current_panes[index]
                selected_key = str(selected.get("key") or "")
                selected_identity = pane_identity(selected)
                original = dict(selected)
                _sync_home_selection(tower, selected_key, selected_identity)
                needs_draw = True
                continue
            if key in ("y", "Y"):
                selected, current = _resolve_selection(
                    getattr(tower, "rows", ()), selected_key, selected_identity, original,
                )
                if not current:
                    message = t("live.changed_output")
                else:
                    from .control_view import _copy_result

                    message = _copy_result(tower, selected_key, stdscr)
                show_message_screen(stdscr, t("detail.result"), [message or t("control.no_result")])
                needs_draw = True
    finally:
        stdscr.timeout(200)
