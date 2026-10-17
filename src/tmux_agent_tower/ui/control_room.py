"""Read-only, three-panel view over Tower's existing pane snapshot.

This screen never captures a pane during drawing. It renders only the
``live_lines`` already carried by the Tower refresh snapshot and delegates
tile rendering to the established LIVE view renderer.
"""

from __future__ import annotations

import copy
import curses
import math
import time

from ..i18n import t
from . import live_view, render
from .perf_trace import draw as trace_draw
from .widgets import is_enter, is_escape, read_key, run_list_picker, safe_add, show_message_screen
from .widgets import prompt_text

MAX_GRID_PANES = 4
_SAVED_SCREEN_STALE_PREFIX = "saved-screen-stale:"
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


def _project_store(tower):
    return getattr(tower, "projects", None)


def _project_values(tower) -> list[dict]:
    store = _project_store(tower)
    return store.all() if store is not None else []


def _hidden_project_refs(tower) -> set[str]:
    store = _project_store(tower)
    return store.hidden_refs() if store is not None else set()


def _screen_store(tower):
    return getattr(tower, "control_screens", None)


def _reference_identity(reference: dict) -> tuple:
    identity = reference["identity"]
    return tuple(str(identity[field] or "") for field in _IDENTITY_FIELDS) + (bool(identity["remote"]),)


def _same_reference(row: dict, reference: dict) -> bool:
    if pane_identity(row) != _reference_identity(reference):
        return False
    saved_window = str(reference.get("window_ref") or "")
    return str(row.get("window_ref") or "") == saved_window


def _stale_reference_row(reference: dict, index: int) -> dict:
    identity = reference["identity"]
    return {
        **identity,
        "kind": "pane",
        "key": f"{_SAVED_SCREEN_STALE_PREFIX}{index}",
        "window_ref": reference.get("window_ref") or "",
        "display_name": reference.get("display_name") or t("control_room.screens_disconnected"),
        "live_lines": [],
        "_live_stale": True,
    }


def _resolve_saved_rows(references, panes) -> list[dict]:
    rows = []
    for index, reference in enumerate(references or ()):
        match = next((row for row in pane_rows(panes) if _same_reference(row, reference)), None)
        rows.append(match if match is not None else _stale_reference_row(reference, index))
    return rows


def _saved_view_selection(view: dict, rows: list[dict]) -> dict:
    reference = view.get("selected")
    if reference:
        match = next((row for row in rows if _same_reference(row, reference)), None)
        if match is not None:
            return match
    return rows[0]


def _layout_capacity(layout: str) -> int:
    return 1 if layout == "focus" else 2 if layout == "split-2" else MAX_GRID_PANES


def _default_layout(rows) -> str:
    count = min(MAX_GRID_PANES, len(rows or ()))
    return "focus" if count <= 1 else "split-2" if count == 2 else "grid-4"


def _layout_rows(rows, selected, layout: str) -> list[dict]:
    candidates = list(rows or ())
    if not candidates and selected:
        candidates = [selected]
    capacity = _layout_capacity(layout)
    if selected and not any(pane_identity(row) == pane_identity(selected) for row in candidates):
        candidates.insert(0, selected)
    candidates = candidates[:capacity]
    if selected and not any(pane_identity(row) == pane_identity(selected) for row in candidates):
        candidates[-1:] = [selected]
    return candidates


def _context_for(row: dict | None, nav_line: dict | None, tower) -> dict:
    project_id = str((nav_line or {}).get("project_id") or "")
    window_ref = str((nav_line or {}).get("window_ref") or (row or {}).get("window_ref") or "")
    if not project_id and window_ref:
        memberships = getattr(_project_store(tower), "memberships", lambda: {})()
        project_id = str(memberships.get(window_ref) or "")
    return {"project_id": project_id or None, "window_ref": window_ref or None}


def _cursor_for_view(tower, panes, selected, context) -> tuple:
    identity = pane_identity(selected)
    lines = navigation_lines(
        panes, str(selected.get("key") or ""), identity,
        projects=_project_values(tower),
        window_assets=getattr(tower, "window_assets", ()) or (),
        hidden_refs=_hidden_project_refs(tower),
    )
    matching = [line for line in lines if line.get("kind") == "pane"
                and pane_identity(line.get("row") or {}) == identity]
    project_id = str((context or {}).get("project_id") or "")
    preferred = next((line for line in matching if line.get("project_id") == project_id), None) if project_id else None
    line = preferred or next((item for item in matching if not item.get("project_id")), None) or (matching[0] if matching else None)
    return line.get("node_key") if line else _initial_cursor(panes, selected, None, ())


def _saved_screen_state(view: dict, tower, panes) -> dict:
    rows = _resolve_saved_rows(view["panes"], panes)
    selected = _saved_view_selection(view, rows)
    context = copy.deepcopy(view["context"])
    return {
        "view_rows": rows,
        "selected_row": dict(selected),
        "selected_key": str(selected.get("key") or ""),
        "selected_identity": pane_identity(selected),
        "original": dict(selected),
        "cursor_key": _cursor_for_view(tower, panes + [row for row in rows if row.get("_live_stale")], selected, context),
        "layout": view["layout"],
        "layout_pinned": True,
        "context": context,
    }


def _visible_panes(tower) -> list[dict]:
    hidden = _hidden_project_refs(tower)
    return [row for row in pane_rows(getattr(tower, "rows", ()))
            if str(row.get("window_ref") or "") not in hidden]


def _host_label(row: dict) -> str:
    execution = str(row.get("execution_host") or row.get("host") or "")
    tmux_host = str(row.get("tmux_host") or execution)
    if row.get("transport") == "ssh" and execution and tmux_host and execution.casefold() != tmux_host.casefold():
        return t("control_room.ssh_host").format(execution=execution, tmux=tmux_host)
    return execution or tmux_host or t("state.unknown")


def _navigation_path(row: dict) -> tuple[tuple, tuple, tuple, tuple | None, tuple]:
    host = _host_label(row)
    host_key = (
        "host", host, str(row.get("tmux_host") or ""), str(row.get("transport") or ""),
        str(row.get("transport_target") or ""),
    )
    session = str(row.get("session") or t("state.unknown"))
    session_id = str(row.get("session_id") or session)
    window_id = str(row.get("window_id") or row.get("window_index") or "")
    session_key = ("session", host_key, session_id, session)
    window_key = ("window", host_key, session_id, window_id, bool(row.get("remote")))
    group_name = str(row.get("work_group_name") or row.get("project") or "").strip()
    group_key = ("project", window_key, str(row.get("work_group_id") or group_name)) if group_name else None
    pane_key = ("pane", pane_identity(row) or (str(row.get("target_id") or row.get("key") or ""),))
    return host_key, session_key, window_key, group_key, pane_key


def navigation_lines(rows, selected_key: str, selected_identity: tuple | None,
                     collapsed=(), cursor_key: tuple | None = None,
                     projects=(), window_assets=(), hidden_refs=()) -> list[dict]:
    """Flat keyboard tree for computer locations plus optional logical projects.

    Collapse state and cursor are view-local. Every non-leaf carries the exact
    current rows in its scope. Tower Project collapse/order state comes from
    ProjectStore; legacy Folder/Work Group state is never rewritten here.
    """

    rows = list(rows or ())
    paths = {id(row): _navigation_path(row) for row in rows}
    collapsed = set(collapsed or ())
    members = {}
    for row in rows:
        for key in paths[id(row)]:
            if key is not None:
                members.setdefault(key, []).append(row)

    result: list[dict] = []
    previous = None

    def add(kind, key, parents, text, row=None, metadata=None):
        if any(parent in collapsed for parent in parents):
            return
        item = {
            "kind": kind, "text": text, "node_key": key, "parent_keys": parents,
            "member_rows": list(members.get(key, ())), "cursor": key == cursor_key,
            "collapsed": key in collapsed,
        }
        if metadata:
            item.update(metadata)
        if row is not None:
            item.update({
                "key": row.get("key"), "identity": pane_identity(row), "row": row,
                "selected": row.get("key") == selected_key and pane_identity(row) == selected_identity,
                "member_rows": [row],
            })
        result.append(item)

    for row in rows:
        host_key, session_key, window_key, group_key, pane_key = paths[id(row)]
        host = _host_label(row)
        session = str(row.get("session") or t("state.unknown"))
        window_index = row.get("window_index")
        window_name = str(row.get("window_name") or "").strip()
        window = f"{window_index}: {window_name}" if window_index is not None and window_name else (
            window_name or str(window_index) if window_index is not None else t("state.unknown")
        )
        if previous is None or previous[0] != host_key:
            add("host", host_key, (), host)
        if previous is None or previous[1] != session_key:
            add("session", session_key, (host_key,), t("control_room.session").format(session=session))
        if previous is None or previous[2] != window_key:
            add("window", window_key, (host_key, session_key), t("control_room.window").format(window=window))
        if group_key and (previous is None or previous[3] != group_key):
            group_name = str(row.get("work_group_name") or row.get("project") or "").strip()
            add("project", group_key, (host_key, session_key, window_key),
                t("control_room.project").format(project=group_name))

        name = str(row.get("display_name") or row.get("task_name") or row.get("project") or t("task.terminal"))
        agent = str(row.get("agent") or "").strip()
        text = f"{name} · {agent}" if agent else name
        parents = (host_key, session_key, window_key) + ((group_key,) if group_key else ())
        add("pane", pane_key, parents, text, row)
        previous = (host_key, session_key, window_key, group_key)

    project_list = list(projects or ())
    hidden_refs = set(hidden_refs or ())
    if project_list:
        root_key = ("tower_projects_root",)
        root_members = [row for row in rows if any(
            str(row.get("window_ref") or "") in project.get("window_refs", ())
            for project in project_list
        )]
        add("tower_projects_root", root_key, (), t("control_room.projects_root"),
            metadata={"member_rows": root_members})
        if root_key not in collapsed:
            asset_map = {str(asset.get("window_ref")): asset for asset in window_assets or ()
                         if asset.get("window_ref")}
            rows_by_window = {}
            for row in pane_rows(rows):
                ref = str(row.get("window_ref") or "")
                if ref:
                    rows_by_window.setdefault(ref, []).append(row)
            for project in sorted(project_list, key=lambda item: (item.get("order", 0), item.get("project_id", ""))):
                project_id = str(project.get("project_id") or "")
                project_key = ("tower_project", project_id)
                refs = list(project.get("window_refs") or ())
                project_members = [row for ref in refs for row in rows_by_window.get(str(ref), ())
                                   if str(ref) not in hidden_refs]
                add("tower_project", project_key, (root_key,), str(project.get("display_name") or ""),
                    metadata={"project_id": project_id, "member_rows": project_members,
                              "collapsed": bool(project.get("collapsed") or project_key in collapsed)})
                if project.get("collapsed") or project_key in collapsed:
                    continue
                for ref in refs:
                    ref = str(ref)
                    asset = asset_map.get(ref)
                    project_window_members = rows_by_window.get(ref, []) if ref not in hidden_refs else []
                    source = asset or (project_window_members[0] if project_window_members else None)
                    hidden = ref in hidden_refs
                    if source is None:
                        window_name = t("project_manage.disconnected")
                        host = ""
                        kind = "project_window_hidden" if hidden else "project_window_disconnected"
                    else:
                        window_name = str(source.get("display_name") or source.get("window_name") or t("folder.terminal"))
                        host = _host_label(source)
                        kind = "project_window_hidden" if hidden else "project_window"
                    window_key = ("tower_project_window", project_id, ref)
                    label_key = {
                        "project_window": "control_room.project_window",
                        "project_window_hidden": "control_room.project_window_hidden",
                        "project_window_disconnected": "control_room.project_window_disconnected",
                    }[kind]
                    label = t(label_key).format(host=host, window=window_name)
                    add(kind, window_key, (root_key, project_key), label,
                        metadata={"project_id": project_id, "window_ref": ref,
                                  "member_rows": project_window_members, "stale": source is None,
                                  "collapsed": bool(hidden or source is None or not project_window_members
                                                    or window_key in collapsed)})
                    if hidden or source is None or window_key in collapsed:
                        continue
                    for row in project_window_members:
                        name = str(row.get("display_name") or row.get("task_name") or row.get("project") or t("task.terminal"))
                        agent = str(row.get("agent") or "").strip()
                        text = f"{name} · {agent}" if agent else name
                        pane_key = ("tower_project_pane", project_id, pane_identity(row))
                        add("pane", pane_key, (root_key, project_key, window_key), text, row,
                            metadata={"project_id": project_id, "window_ref": ref,
                                      "project_member_rows": project_members})
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
            str(selected.get("execution_host") or selected.get("host") or ""),
            str(selected.get("tmux_host") or ""),
            str(selected.get("session_id") or selected.get("session") or ""),
            str(selected.get("window_id") or selected.get("window_index") or ""),
            str(selected.get("transport") or ""),
            str(selected.get("transport_target") or ""),
            bool(selected.get("remote")),
        )
        candidates = [row for row in pane_rows(rows) if (
            str(row.get("execution_host") or row.get("host") or ""),
            str(row.get("tmux_host") or ""),
            str(row.get("session_id") or row.get("session") or ""),
            str(row.get("window_id") or row.get("window_index") or ""),
            str(row.get("transport") or ""),
            str(row.get("transport_target") or ""),
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
    matching_keys = [row for row in pane_rows(rows) if row.get("key") == key]
    current = next((row for row in matching_keys if pane_identity(row) == identity), None)
    if (current is None and str(key).startswith(_SAVED_SCREEN_STALE_PREFIX)
            and identity is not None and pane_identity(original) == identity):
        saved_window_ref = str(original.get("window_ref") or "")
        current = next((
            row for row in pane_rows(rows)
            if pane_identity(row) == identity
            and str(row.get("window_ref") or "") == saved_window_ref
        ), None)
    if identity is None or current is None:
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
    total_height = max(0, height - top - 2)  # reserve two rows for contextual keyboard help
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


def _draw_navigation(stdscr, region, lines) -> None:
    x, y, width, height = region
    selected_line = next((index for index, line in enumerate(lines) if line.get("cursor")), 0)
    capacity = max(0, height)
    top = max(0, selected_line - capacity + 1)
    visible = lines[top:top + capacity]
    for offset, line in enumerate(visible):
        kind = line["kind"]
        pane_cursor = kind == "pane" and line.get("cursor")
        prefix = "▶ " if pane_cursor else (
            "▾ " if kind != "pane" and not line.get("collapsed") else "▸ "
        )
        active = line.get("cursor")
        attr = (curses.A_REVERSE | curses.A_BOLD) if active else (
            _heading_attr() if kind in {"host", "project", "tower_projects_root", "tower_project"}
            else curses.A_DIM
        )
        text = prefix + str(line.get("text") or "")
        safe_add(stdscr, y + offset, x, render.truncate_to_width(text, max(0, width)), attr)


def _layout_rectangles(region, layout: str) -> list[tuple[int, int, int, int]]:
    x, y, width, height = region
    if layout == "focus":
        return [(x, y, width, height)]
    if layout == "split-2":
        left = width // 2
        return [(x, y, left, height), (x + left, y, width - left, height)]
    if layout == "main-plus-side":
        main_width = max(1, width * 3 // 5)
        side_width = max(0, width - main_width)
        heights = [height // 3, height // 3, height - 2 * (height // 3)]
        result = [(x, y, main_width, height)]
        current_y = y
        for slot_height in heights:
            result.append((x + main_width, current_y, side_width, slot_height))
            current_y += slot_height
        return result
    half_width, half_height = width // 2, height // 2
    return [
        (x, y, half_width, half_height),
        (x + half_width, y, width - half_width, half_height),
        (x, y + half_height, half_width, height - half_height),
        (x + half_width, y + half_height, width - half_width, height - half_height),
    ]


def _draw_grid(stdscr, tower, region, rows, selected_identity, screen_width,
               scroll: int = 0, layout: str = "grid-4") -> None:
    x, y, width, height = region
    rows = _layout_rows(rows, next((row for row in rows if pane_identity(row) == selected_identity), None), layout)
    if not rows:
        safe_add(stdscr, y, x, t("live.unavailable"), curses.A_DIM)
        return
    rectangles = _layout_rectangles(region, layout)
    for index, rect in enumerate(rectangles):
        cell_x, cell_y, current_width, cell_height = rect
        if index >= len(rows):
            if current_width > 2 and cell_height > 0:
                safe_add(stdscr, cell_y + cell_height // 2, cell_x + 1,
                         t("control_room.screens_slot"), curses.A_DIM)
            continue
        row = rows[index]
        safe_row = _render_row(row)
        focused = pane_identity(row) == selected_identity
        live_view._draw_tile(
            stdscr, tower, safe_row, index, cell_y, cell_x, cell_height,
            current_width, focused, scroll if focused else 0,
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


def _find_nav_line(rows, selected_key, selected_identity, cursor_key, collapsed=(),
                   projects=(), window_assets=(), hidden_refs=()):
    return next((line for line in navigation_lines(
        rows=rows,
        selected_key=selected_key,
        selected_identity=selected_identity,
        collapsed=collapsed,
        cursor_key=cursor_key,
        projects=projects,
        window_assets=window_assets,
        hidden_refs=hidden_refs,
    ) if line.get("node_key") == cursor_key), None)


def _scope_grid_rows(selected, panes, groups, nav_line=None):
    if nav_line and nav_line.get("kind") == "pane" and nav_line.get("project_id"):
        scoped = [row for row in nav_line.get("project_member_rows", ())
                  if any(pane_identity(row) == pane_identity(current) for current in panes)]
        if scoped and not any(pane_identity(row) == pane_identity(selected) for row in scoped):
            scoped.insert(0, selected)
        if len(scoped) > MAX_GRID_PANES:
            selected_index = next((index for index, row in enumerate(scoped)
                                   if pane_identity(row) == pane_identity(selected)), 0)
            start = max(0, min(selected_index - 1, len(scoped) - MAX_GRID_PANES))
            scoped = scoped[start:start + MAX_GRID_PANES]
        return scoped[:MAX_GRID_PANES]
    if nav_line and nav_line.get("kind") != "pane":
        scoped = [row for row in nav_line.get("member_rows", ())
                  if any(pane_identity(row) == pane_identity(current) for current in panes)]
        if nav_line.get("kind") in {"tower_projects_root", "tower_project", "project_window",
                                     "project_window_hidden", "project_window_disconnected"}:
            return scoped[:MAX_GRID_PANES]
        group_id = str(selected.get("work_group_id") or "")
        group = next((item for item in groups or () if group_id and str(item.get("group_id") or "") == group_id), None)
        if group and nav_line.get("kind") == "project":
            return related_panes(selected, panes, groups)
        if scoped:
            selected_identity = pane_identity(selected)
            if not any(pane_identity(row) == selected_identity for row in scoped):
                scoped.insert(0, selected)
            return scoped[:MAX_GRID_PANES]
    return related_panes(selected, panes, groups)


def _footer_lines(width, active_panel, cursor_kind):
    movement = t("control_room.footer_navigation")
    if cursor_kind != "pane":
        actions = t("control_room.footer_group")
    elif active_panel == 2:
        actions = t("control_room.footer_focus")
    else:
        actions = t("control_room.footer")
    available = max(0, width - 4)
    return (
        render.truncate_to_width(movement, available),
        render.truncate_to_width(actions, available),
    )


@trace_draw("control_room")
def draw(stdscr, tower, selected_key: str, selected_identity: tuple | None,
         original: dict, groups=(), *, active_panel: int = 0, cursor_key=None,
         collapsed=(), focus_scroll: int = 0, layout: str = "grid-4",
         grid_rows_override: list[dict] | None = None) -> bool:
    """Draw a read-only room and return whether the selection is still current."""

    stdscr.erase()
    height, width = stdscr.getmaxyx()
    safe_add(stdscr, 0, 2, t("control_room.title"), curses.A_BOLD)
    safe_add(
        stdscr, 1, 2,
        render.truncate_to_width(t("control_room.read_only"), max(0, width - 4)),
        curses.A_DIM,
    )
    projects = _project_values(tower)
    hidden_refs = _hidden_project_refs(tower)
    panes = [row for row in pane_rows(getattr(tower, "rows", ()))
             if str(row.get("window_ref") or "") not in hidden_refs]
    selected, current = _resolve_selection(panes, selected_key, selected_identity, original)
    display_rows = list(panes)
    if not current and str(selected.get("window_ref") or "") not in hidden_refs:
        display_rows.append(selected)
    window_assets = getattr(tower, "window_assets", ()) or ()
    nav_lines = navigation_lines(
        display_rows, selected_key, selected_identity, collapsed, cursor_key,
        projects, window_assets, hidden_refs,
    )
    nav_line = next((line for line in nav_lines if line.get("node_key") == cursor_key), None)
    if nav_line is None and cursor_key is None:
        nav_line = next((line for line in nav_lines if line.get("kind") == "pane" and line.get("selected")), None)
    cursor_kind = nav_line.get("kind", "pane") if nav_line else "pane"
    regions = panel_regions(width, height)
    headings = (t("control_room.panel_location"), t("control_room.panel_grid"), t("control_room.panel_focus"))
    for index, (region, heading) in enumerate(zip(regions, headings)):
        x, y, region_width, region_height = region
        heading_attr = curses.A_REVERSE | curses.A_BOLD if index == active_panel else _heading_attr()
        safe_add(stdscr, y, x, render.truncate_to_width(heading, max(0, region_width)), heading_attr)

    _draw_navigation(
        stdscr, (regions[0][0], regions[0][1] + 1, regions[0][2], max(0, regions[0][3] - 1)),
        nav_lines,
    )
    if grid_rows_override is not None:
        grid_rows = _resolve_saved_rows(
            [
                {"identity": {field: row.get(field) for field in (
                    "target_id", "tmux_host", "session", "session_id", "window_id", "pane_id",
                    "pane_pid", "transport", "transport_target", "remote",
                )}, "window_ref": row.get("window_ref") or "", "display_name": row.get("display_name") or "Pane"}
                for row in grid_rows_override
            ], panes,
        )
    elif current:
        grid_rows = _scope_grid_rows(selected, panes, groups, nav_line)
    else:
        grid_rows = [selected]
    grid_total_count = len(grid_rows)
    grid_rows = _layout_rows(grid_rows, selected, layout)
    if len(grid_rows) < grid_total_count:
        grid_heading = t("control_room.panel_grid_count").format(
            shown=len(grid_rows), total=grid_total_count,
        )
        safe_add(stdscr, regions[1][1], regions[1][0],
                 render.truncate_to_width(grid_heading, max(0, regions[1][2])),
                 curses.A_REVERSE | curses.A_BOLD if active_panel == 1 else _heading_attr())
    _draw_grid(
        stdscr, tower,
        (regions[1][0], regions[1][1] + 1, regions[1][2], max(0, regions[1][3] - 1)),
        grid_rows, selected_identity, width, layout=layout,
    )
    _draw_grid(
        stdscr, tower,
        (regions[2][0], regions[2][1] + 1, regions[2][2], max(0, regions[2][3] - 1)),
        [selected], selected_identity if current else None, width, focus_scroll, "focus",
    )

    footer_lines = _footer_lines(width, active_panel, cursor_kind)
    safe_add(stdscr, max(0, height - 2), 2, footer_lines[0], curses.A_DIM)
    safe_add(stdscr, max(0, height - 1), 2, footer_lines[1], curses.A_DIM)
    stdscr.noutrefresh()
    curses.doupdate()
    return current


def _sync_home_selection(tower, key: str, identity: tuple | None) -> None:
    for index, row in enumerate(getattr(tower, "visible_rows", ())):
        if row.get("key") == key and pane_identity(row) == identity:
            tower.selected = index
            return


def _row_for_nav_line(line, panes, groups=()) -> dict | None:
    if not line:
        return None
    members = line.get("member_rows") or ()
    current = [row for row in members if any(pane_identity(row) == pane_identity(item) for item in panes)]
    if line.get("kind") == "pane":
        return current[0] if current else None
    if line.get("kind") in {"tower_projects_root", "tower_project", "project_window"}:
        return current[0] if current else None
    if line.get("kind") == "project":
        group_id = next((str(row.get("work_group_id") or "") for row in current if row.get("work_group_id")), "")
        if group_id and any(str(group.get("group_id") or "") == group_id for group in groups or ()):
            return initial_pane(panes, {"kind": "work_group", "group_id": group_id}, groups)
    return current[0] if current else None


def _initial_cursor(panes, selected, selected_home, groups):
    if selected_home and selected_home.get("kind") == "work_group":
        group_id = str(selected_home.get("group_id") or "")
        for line in navigation_lines(panes, selected.get("key", ""), pane_identity(selected)):
            if line.get("kind") == "project" and any(
                str(row.get("work_group_id") or "") == group_id for row in line.get("member_rows", ())
            ):
                return line.get("node_key")
    identity = pane_identity(selected)
    return ("pane", identity or (str(selected.get("target_id") or selected.get("key") or ""),))


def pane_detail_lines(row: dict) -> list[str]:
    """Identity-only detail; never includes the captured terminal body."""

    return [
        f'{t("control_room.detail_name")}: {row.get("display_name") or row.get("task_name") or t("task.terminal")}',
        f'{t("control_room.detail_agent")}: {row.get("agent") or t("state.unknown")}',
        f'{t("control_room.detail_host")}: {_host_label(row)}',
        f'{t("control_room.detail_session")}: {row.get("session") or t("state.unknown")}',
        f'{t("control_room.detail_window")}: {row.get("window_name") or row.get("window_index") or t("state.unknown")}',
        f'{t("control_room.detail_pane")}: {row.get("pane_id") or t("state.unknown")} · PID {row.get("pane_pid") or t("state.unknown")}',
    ]


def _copy_key_is_unambiguous(rows, row: dict | None) -> bool:
    if not row or not row.get("key"):
        return False
    matching = [
        item for item in pane_rows(rows)
        if str(item.get("key") or "") == str(row.get("key") or "")
    ]
    return len(matching) == 1 and pane_identity(matching[0]) is not None


def action_menu_items(row: dict | None, *, cursor_kind: str = "pane", has_group: bool = False,
                      has_folder: bool = False, copy_safe: bool = True,
                      live_routes_safe: bool = True,
                      project_management: bool = False) -> list[tuple[str, str]]:
    """Visible routes to established features; no new Agent-control actions."""

    items: list[tuple[str, str]] = [("screens", t("control_room.menu_screens"))]
    if has_group and live_routes_safe:
        items.append(("group_live", t("control_room.menu_group_live")))
    if has_folder and live_routes_safe:
        items.append(("folder_live", t("control_room.menu_folder_live")))
    if row and row.get("window_ref") and not row.get("remote") and live_routes_safe:
        items.append(("window_live", t("control_room.menu_window_live")))
    if row and not row.get("remote") and live_routes_safe:
        items.append(("layout_live", t("control_room.menu_layout_live")))
    if row and cursor_kind == "pane" and copy_safe:
        items.append(("copy", t("control_room.menu_copy")))
    if project_management:
        items.append(("projects", t("control_room.menu_projects")))
    items.extend([
        ("details", t("control_room.menu_details")),
        ("settings", t("control_room.menu_settings")),
    ])
    return items


def _screen_confirm(stdscr, title: str, message: str) -> bool:
    pick = run_list_picker(
        stdscr, title,
        [("cancel", t("menu.cancel")), ("confirm", t("project_manage.confirm_yes"))],
        footer_hint=t("wizard.hint_list"), preamble=[message],
    )
    return not pick.cancelled and pick.selected_key == "confirm"


def _screen_picker(stdscr, title: str, records: list[dict], *, key_field: str,
                   label_field: str, empty_key: str = "control_room.screens_empty"):
    if not records:
        show_message_screen(stdscr, title, [t(empty_key)])
        return None
    pick = run_list_picker(
        stdscr, title,
        [(str(item[key_field]), str(item[label_field])) for item in records],
        footer_hint=t("wizard.hint_list"),
    )
    if pick.cancelled:
        return None
    return next((item for item in records if str(item[key_field]) == str(pick.selected_key)), None)


def _screen_context_view_rows(state: dict, tower, panes, groups, nav_line) -> None:
    selected = state.get("selected_row")
    state["view_rows"] = _scope_grid_rows(selected, panes, groups, nav_line) if selected else []
    state["context"] = _context_for(selected, nav_line, tower)


def _open_screen_menu(stdscr, tower, state: dict, current_panes, groups, nav_line) -> None:
    store = _screen_store(tower)
    if store is None:
        show_message_screen(stdscr, t("control_room.screens_title"), [t("control_room.screens_save_failed")])
        return
    try:
        screens = store.screens()
        templates = store.templates()
        last_view = store.last_view()
        if store.corrupt:
            raise ValueError("saved view is unreadable")
    except (OSError, ValueError):
        show_message_screen(stdscr, t("control_room.screens_title"), [t("control_room.screens_save_failed")])
        return
    items = [
        ("resume", t("control_room.screens_resume")),
        ("open", t("control_room.screens_open")),
        ("save", t("control_room.screens_save")),
        ("layout", t("control_room.screens_layout")),
        ("template_save", t("control_room.screens_template_save")),
        ("template_apply", t("control_room.screens_template_apply")),
        ("manage", t("control_room.screens_manage")),
        ("cancel", t("menu.cancel")),
    ]
    pick = run_list_picker(stdscr, t("control_room.screens_title"), items,
                           footer_hint=t("wizard.hint_list"))
    if pick.cancelled or not pick.selected_key or pick.selected_key == "cancel":
        return
    choice = str(pick.selected_key)
    if choice in {"resume", "open"}:
        if choice == "resume":
            if last_view is None:
                show_message_screen(stdscr, t("control_room.screens_title"), [t("control_room.screens_resume_missing")])
                return
            view = last_view
        else:
            selected_screen = _screen_picker(
                stdscr, t("control_room.screens_open"), screens,
                key_field="screen_id", label_field="name",
            )
            if selected_screen is None:
                return
            view = selected_screen
        state.update(_saved_screen_state(view, tower, current_panes))
        return
    if choice == "save":
        name = prompt_text(stdscr, t("control_room.screens_name_prompt"))
        if not name:
            return
        existing = next((item for item in screens if item["name"].casefold() == name.strip().casefold()), None)
        if existing and not _screen_confirm(
            stdscr, t("control_room.screens_save"),
            t("control_room.screens_overwrite_confirm"),
        ):
            return
        rows = _layout_rows(state.get("view_rows", ()), state.get("selected_row"), state["layout"])
        try:
            saved = store.save_screen(
                name, rows, state.get("selected_row"), state["layout"], state.get("context"),
                screen_id=existing["screen_id"] if existing else None,
            )
            show_message_screen(
                stdscr, t("control_room.screens_title"),
                [t("control_room.screens_saved").format(name=saved["name"])],
            )
        except (OSError, ValueError):
            show_message_screen(stdscr, t("control_room.screens_title"), [t("control_room.screens_save_failed")])
        return
    if choice == "layout":
        pick = run_list_picker(
            stdscr, t("control_room.screens_layout"),
            [(layout, t(f"layout.{layout}")) for layout in ("focus", "split-2", "grid-4", "main-plus-side")],
            footer_hint=t("wizard.hint_list"),
        )
        if not pick.cancelled and pick.selected_key:
            _screen_context_view_rows(state, tower, current_panes, groups, nav_line)
            state["layout"] = str(pick.selected_key)
            state["layout_pinned"] = True
        return
    if choice == "template_save":
        name = prompt_text(stdscr, t("control_room.screens_template_name_prompt"))
        if not name:
            return
        existing = next((item for item in templates if item["name"].casefold() == name.strip().casefold()), None)
        if existing and not _screen_confirm(stdscr, t("control_room.screens_template_save"),
                                            t("control_room.screens_template_overwrite_confirm")):
            return
        try:
            saved = store.save_template(name, state["layout"],
                                        template_id=existing["template_id"] if existing else None)
            show_message_screen(
                stdscr, t("control_room.screens_title"),
                [t("control_room.screens_template_saved").format(name=saved["name"])],
            )
        except (OSError, ValueError):
            show_message_screen(stdscr, t("control_room.screens_title"), [t("control_room.screens_save_failed")])
        return
    if choice == "template_apply":
        template = _screen_picker(
            stdscr, t("control_room.screens_template_apply"), templates,
            key_field="template_id", label_field="name",
            empty_key="control_room.screens_template_empty",
        )
        if template:
            _screen_context_view_rows(state, tower, current_panes, groups, nav_line)
            state["layout"] = template["layout"]
            state["layout_pinned"] = True
        return
    if choice == "manage":
        screen = _screen_picker(
            stdscr, t("control_room.screens_manage"), screens,
            key_field="screen_id", label_field="name",
        )
        if screen is None:
            return
        action = run_list_picker(
            stdscr, screen["name"],
            [("rename", t("control_room.screens_rename")),
             ("delete", t("control_room.screens_delete")), ("cancel", t("menu.cancel"))],
            footer_hint=t("wizard.hint_list"),
        )
        if action.cancelled or action.selected_key == "cancel":
            return
        if action.selected_key == "rename":
            name = prompt_text(stdscr, t("control_room.screens_name_prompt"), initial=screen["name"])
            if not name or name == screen["name"]:
                return
            try:
                store.rename_screen(screen["screen_id"], name)
                show_message_screen(stdscr, t("control_room.screens_title"), [t("control_room.screens_renamed")])
            except (OSError, ValueError):
                show_message_screen(stdscr, t("control_room.screens_title"), [t("control_room.screens_save_failed")])
        elif action.selected_key == "delete" and _screen_confirm(
            stdscr, t("control_room.screens_title"), t("control_room.screens_delete_confirm"),
        ):
            try:
                store.delete_screen(screen["screen_id"])
                show_message_screen(stdscr, t("control_room.screens_title"), [t("control_room.screens_deleted")])
            except (OSError, ValueError):
                show_message_screen(stdscr, t("control_room.screens_title"), [t("control_room.screens_save_failed")])


def _run_action(stdscr, tower, action: str, row: dict | None, groups=(),
                selected_home=None, *, copy_allowed=True) -> None:
    if action == "copy":
        if not copy_allowed or not row:
            show_message_screen(stdscr, t("detail.result"), [t("control_room.copy_select_pane")])
            return
        if not _copy_key_is_unambiguous(getattr(tower, "rows", ()), row):
            show_message_screen(stdscr, t("detail.result"), [t("control_room.copy_ambiguous")])
            return
        _selected, current = _resolve_selection(
            getattr(tower, "rows", ()), str(row.get("key") or ""), pane_identity(row), row,
        )
        if not current:
            message = t("live.changed_output")
        else:
            from .control_view import _copy_result

            message = _copy_result(tower, str(row.get("key") or ""), stdscr)
        show_message_screen(stdscr, t("detail.result"), [message or t("control.no_result")])
    elif action == "group_live" and row:
        if not _pane_keys_unique(getattr(tower, "rows", ())):
            show_message_screen(stdscr, t("control_room.title"), [t("control_room.live_ambiguous")])
            return
        group_id = str(row.get("work_group_id") or "")
        group = next((item for item in groups or () if str(item.get("group_id") or "") == group_id), None)
        if group:
            live_view.open_group_live_view(stdscr, tower, group)
        else:
            show_message_screen(stdscr, t("live.title"), [t("live.empty")])
    elif action == "folder_live" and selected_home:
        if not _pane_keys_unique(getattr(tower, "rows", ())):
            show_message_screen(stdscr, t("control_room.title"), [t("control_room.live_ambiguous")])
            return
        live_view.open_folder_live_view(stdscr, tower, selected_home)
    elif action == "window_live" and row and row.get("window_ref"):
        if not _pane_keys_unique(getattr(tower, "rows", ())):
            show_message_screen(stdscr, t("control_room.title"), [t("control_room.live_ambiguous")])
            return
        live_view.open_window_live_view(stdscr, tower, str(row["window_ref"]))
    elif action == "layout_live":
        if not _pane_keys_unique(getattr(tower, "rows", ())):
            show_message_screen(stdscr, t("control_room.title"), [t("control_room.live_ambiguous")])
            return
        live_view.open_live_view(stdscr, tower)
    elif action == "details" and row:
        show_message_screen(stdscr, t("control_room.detail_title"), pane_detail_lines(row))
    elif action == "settings":
        from .settings_menu import open_settings

        open_settings(stdscr, tower)
    elif action == "projects":
        from .projects import open_project_picker

        open_project_picker(stdscr, tower, row)


def _open_action_menu(stdscr, tower, row, groups, selected_home, cursor_kind, nav_line=None,
                      screen_state: dict | None = None):
    if cursor_kind in {"tower_project", "project_window", "project_window_hidden",
                       "project_window_disconnected"}:
        items = [
            ("screens", t("control_room.menu_screens")),
            ("edit_project", t("control_room.menu_edit_project")),
            ("cancel", t("menu.cancel")),
        ]
    elif cursor_kind == "tower_projects_root":
        items = [
            ("screens", t("control_room.menu_screens")),
            ("projects", t("control_room.menu_projects")),
            ("cancel", t("menu.cancel")),
        ]
    else:
        group_id = str((row or {}).get("work_group_id") or "")
        has_group = bool(
            cursor_kind in {"pane", "project"} and group_id
            and any(str(group.get("group_id") or "") == group_id for group in groups or ())
        )
        has_folder = bool(selected_home and selected_home.get("kind") in {"folder", "other_section"})
        items = action_menu_items(
            row, cursor_kind=cursor_kind, has_group=has_group, has_folder=has_folder,
            copy_safe=_copy_key_is_unambiguous(getattr(tower, "rows", ()), row),
            live_routes_safe=_pane_keys_unique(getattr(tower, "rows", ())),
            project_management=_project_store(tower) is not None,
        )
    pick = run_list_picker(
        stdscr, t("control_room.menu_title"), items,
        footer_hint=t("wizard.hint_list"),
    )
    if not pick.cancelled and pick.selected_key:
        if pick.selected_key == "screens":
            current_panes = _visible_panes(tower)
            if screen_state is not None:
                _open_screen_menu(stdscr, tower, screen_state, current_panes, groups, nav_line)
            return
        if pick.selected_key == "edit_project":
            from .projects import open_project_editor

            open_project_editor(stdscr, tower, str((nav_line or {}).get("project_id") or ""))
            return
        if cursor_kind == "tower_projects_root":
            if pick.selected_key == "projects":
                from .projects import open_project_picker

                open_project_picker(stdscr, tower)
            return
        if cursor_kind in {"tower_project", "project_window", "project_window_hidden", "project_window_disconnected"}:
            return
        _run_action(
            stdscr, tower, str(pick.selected_key), row, groups, selected_home,
            copy_allowed=cursor_kind == "pane",
        )


def _pane_keys_unique(rows) -> bool:
    keys = [str(row.get("key") or "") for row in pane_rows(rows)]
    return bool(keys) and len(keys) == len(set(keys))


def _set_selection(row, *, cursor_key=None):
    identity = pane_identity(row)
    key = str(row.get("key") or "")
    return key, identity, dict(row), cursor_key or ("pane", identity or (str(row.get("target_id") or key),))


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
    """Open the read-only room; panel keys are consumed here, never sent to an Agent."""

    panes = _visible_panes(tower)
    screen_store = _screen_store(tower)
    try:
        last_view = screen_store.last_view() if screen_store is not None else None
    except (OSError, ValueError):
        last_view = None
    if not panes and last_view is None:
        if _project_values(tower):
            from .projects import open_project_picker

            open_project_picker(stdscr, tower)
            return
        show_message_screen(stdscr, t("control_room.title"), [t("live.empty")])
        return
    visible = getattr(tower, "visible_rows", ())
    selected_home = visible[tower.selected] if visible and 0 <= tower.selected < len(visible) else None
    groups = getattr(getattr(tower, "work_groups", None), "all", lambda: [])()
    selected = initial_pane(panes, selected_home, groups) if panes else None
    selected = selected or (panes[0] if panes else None)
    if last_view is not None:
        restored = _saved_screen_state(last_view, tower, panes)
        selected = restored["selected_row"]
        selected_key = restored["selected_key"]
        selected_identity = restored["selected_identity"]
        original = restored["original"]
        cursor_key = restored["cursor_key"]
        layout = restored["layout"]
        layout_pinned = restored["layout_pinned"]
        view_rows = restored["view_rows"]
        view_context = restored["context"]
    else:
        if selected is None:
            return
        selected_key = str(selected.get("key") or "")
        selected_identity = pane_identity(selected)
        cursor_key = _initial_cursor(panes, selected, selected_home, groups)
        original = dict(selected)
        initial_line = _find_nav_line(
            panes, selected_key, selected_identity, cursor_key,
            projects=_project_values(tower), window_assets=getattr(tower, "window_assets", ()) or (),
            hidden_refs=_hidden_project_refs(tower),
        )
        view_rows = _scope_grid_rows(selected, panes, groups, initial_line)
        view_context = _context_for(selected, initial_line, tower)
        layout = _default_layout(view_rows)
        layout_pinned = False
    home_scope_identity = selected_identity
    collapsed: set[tuple] = set()
    active_panel = 0
    focus_scroll = 0

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
                screen_height, screen_width = stdscr.getmaxyx()
                selected_snapshot, selected_current = _resolve_selection(
                    _visible_panes(tower), selected_key, selected_identity, original,
                )
                if selected_current and selected_key.startswith(_SAVED_SCREEN_STALE_PREFIX):
                    # Rebind only the internal placeholder to the exact Pane
                    # identity that returned. Future copy/selection actions use
                    # the real current key, never the saved-screen placeholder.
                    selected_key = str(selected_snapshot.get("key") or "")
                    original = dict(selected_snapshot)
                right_region_height = max(0, panel_regions(screen_width, screen_height)[2][3] - 1)
                visible_output_lines = max(0, right_region_height - 3)
                output_length = len(selected_snapshot.get("live_lines") or ()) if selected_current else 0
                focus_scroll = min(focus_scroll, max(0, output_length - visible_output_lines))
                view_rows = _resolve_saved_rows(
                    [{
                        "identity": {field: row.get(field) for field in (
                            "target_id", "tmux_host", "session", "session_id", "window_id", "pane_id",
                            "pane_pid", "transport", "transport_target", "remote",
                        )},
                        "window_ref": row.get("window_ref") or "",
                        "display_name": row.get("display_name") or "Pane",
                    } for row in view_rows],
                    _visible_panes(tower),
                )
                current = draw(
                    stdscr, tower, selected_key, selected_identity, original, groups,
                    active_panel=active_panel, cursor_key=cursor_key,
                    collapsed=collapsed, focus_scroll=focus_scroll,
                    layout=layout, grid_rows_override=view_rows,
                )
                current_cursor = _find_nav_line(
                    _visible_panes(tower), selected_key, selected_identity,
                    cursor_key, collapsed, _project_values(tower),
                    getattr(tower, "window_assets", ()) or (), _hidden_project_refs(tower),
                )
                cursor_kind = current_cursor.get("kind", "pane") if current_cursor else "pane"
                if current and cursor_kind == "pane":
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
            if key == "\t":
                active_panel = (active_panel + 1) % 3
                needs_draw = True
                continue
            if key == getattr(curses, "KEY_BTAB", object()):
                active_panel = (active_panel - 1) % 3
                needs_draw = True
                continue

            current_panes = _visible_panes(tower)
            selected, is_current = _resolve_selection(
                current_panes, selected_key, selected_identity, original,
            )
            projects = _project_values(tower)
            hidden_refs = _hidden_project_refs(tower)
            window_assets = getattr(tower, "window_assets", ()) or ()
            nav_items = navigation_lines(
                rows=(current_panes if is_current or str(selected.get("window_ref") or "") in hidden_refs
                      else current_panes + [selected]),
                selected_key=selected_key,
                selected_identity=selected_identity,
                collapsed=collapsed,
                cursor_key=cursor_key,
                projects=projects,
                window_assets=window_assets,
                hidden_refs=hidden_refs,
            )
            nav_line = next((line for line in nav_items if line.get("node_key") == cursor_key), None)
            cursor_kind = nav_line.get("kind", "pane") if nav_line else "pane"

            if key in (curses.KEY_UP, curses.KEY_DOWN, curses.KEY_LEFT, curses.KEY_RIGHT):
                if active_panel == 0:
                    if key in (curses.KEY_UP, curses.KEY_DOWN):
                        visible_items = nav_items
                        index = next((i for i, line in enumerate(visible_items)
                                      if line.get("node_key") == cursor_key), None)
                        if index is None:
                            index = -1 if key == curses.KEY_DOWN else 0
                        step = 1 if key == curses.KEY_DOWN else -1
                        if visible_items:
                            target = visible_items[(index + step) % len(visible_items)]
                            row = _row_for_nav_line(target, current_panes, groups)
                            if row:
                                selected_key, selected_identity, original, cursor_key = _set_selection(
                                    row, cursor_key=target.get("node_key"),
                                )
                                view_rows = _scope_grid_rows(row, current_panes, groups, target)
                                view_context = _context_for(row, target, tower)
                                if not layout_pinned:
                                    layout = _default_layout(view_rows)
                                focus_scroll = 0
                            else:
                                cursor_key = target.get("node_key")
                    elif nav_line and nav_line.get("kind") != "pane":
                        node_key = nav_line.get("node_key")
                        if key == curses.KEY_RIGHT:
                            collapsed.discard(node_key)
                        else:
                            collapsed.add(node_key)
                    elif key == curses.KEY_LEFT and nav_line:
                        parent = next((parent for parent in reversed(nav_line.get("parent_keys", ()))
                                       if any(line.get("node_key") == parent for line in nav_items)), None)
                        if parent is not None:
                            parent_line = next(line for line in nav_items if line.get("node_key") == parent)
                            row = _row_for_nav_line(parent_line, current_panes, groups)
                            if row:
                                selected_key, selected_identity, original = _set_selection(row)[:3]
                                cursor_key = parent
                                view_rows = _scope_grid_rows(row, current_panes, groups, parent_line)
                                view_context = _context_for(row, parent_line, tower)
                                if not layout_pinned:
                                    layout = _default_layout(view_rows)
                                focus_scroll = 0
                            else:
                                cursor_key = parent
                    elif key == curses.KEY_RIGHT and cursor_kind == "pane":
                        active_panel = 1
                elif active_panel == 1:
                    grid_rows = list(view_rows or _scope_grid_rows(selected, current_panes, groups, nav_line))
                    grid_rows = [row for row in grid_rows if pane_identity(row) is not None]
                    index = next((i for i, row in enumerate(grid_rows)
                                  if pane_identity(row) == selected_identity), None)
                    if grid_rows:
                        if index is None:
                            index = 0
                        step = -1 if key in (curses.KEY_UP, curses.KEY_LEFT) else 1
                        row = grid_rows[(index + step) % len(grid_rows)]
                        project_id = str((nav_line or {}).get("project_id") or "")
                        if not project_id and nav_line and nav_line.get("kind") == "tower_projects_root":
                            project_id = next((str(line.get("project_id") or "") for line in nav_items
                                               if line.get("kind") == "pane"
                                               and pane_identity(line.get("row") or {}) == pane_identity(row)
                                               and line.get("project_id")), "")
                        project_pane = next((line for line in nav_items
                                             if line.get("kind") == "pane"
                                             and pane_identity(line.get("row") or {}) == pane_identity(row)
                                             and (not project_id or line.get("project_id") == project_id)), None)
                        selected_key, selected_identity, original, cursor_key = _set_selection(
                            row, cursor_key=(project_pane or {}).get("node_key"),
                        )
                        focus_scroll = 0
                elif active_panel == 2 and key in (curses.KEY_UP, curses.KEY_DOWN):
                    focus_scroll = max(0, focus_scroll + (1 if key == curses.KEY_UP else -1))
                needs_draw = True
                continue

            if is_enter(key):
                if active_panel == 0 and nav_line and cursor_kind != "pane":
                    node_key = nav_line.get("node_key")
                    if cursor_kind == "tower_project":
                        try:
                            store = _project_store(tower)
                            if store is not None:
                                store.toggle_project(str(nav_line.get("project_id") or ""))
                        except (OSError, KeyError):
                            show_message_screen(stdscr, t("project_manage.title"), [t("project_manage.save_failed")])
                    elif node_key in collapsed:
                        collapsed.discard(node_key)
                    else:
                        collapsed.add(node_key)
                elif is_current:
                    show_message_screen(stdscr, t("control_room.detail_title"), pane_detail_lines(selected))
                else:
                    show_message_screen(stdscr, t("control_room.detail_title"), [t("live.changed_output")])
                needs_draw = True
                continue

            if key == " ":
                room_state = {
                    "layout": layout,
                    "layout_pinned": layout_pinned,
                    "view_rows": list(view_rows),
                    "selected_row": dict(selected),
                    "selected_key": selected_key,
                    "selected_identity": selected_identity,
                    "original": dict(original),
                    "cursor_key": cursor_key,
                    "context": copy.deepcopy(view_context),
                }
                if not is_current:
                    _open_screen_menu(stdscr, tower, room_state, current_panes, groups, nav_line)
                else:
                    menu_home = selected_home if pane_identity(selected) == home_scope_identity else None
                    _open_action_menu(
                        stdscr, tower, selected, groups, menu_home, cursor_kind, nav_line,
                        screen_state=room_state,
                    )
                layout = room_state["layout"]
                layout_pinned = room_state["layout_pinned"]
                view_rows = room_state["view_rows"]
                view_context = room_state["context"]
                selected_key = room_state["selected_key"]
                selected_identity = room_state["selected_identity"]
                original = room_state["original"]
                cursor_key = room_state["cursor_key"]
                focus_scroll = 0
                needs_draw = True
                continue

            if key in ("y", "Y"):
                if cursor_kind != "pane":
                    message = t("control_room.copy_select_pane")
                elif not is_current:
                    message = t("live.changed_output")
                elif not _copy_key_is_unambiguous(getattr(tower, "rows", ()), selected):
                    message = t("control_room.copy_ambiguous")
                else:
                    from .control_view import _copy_result

                    message = _copy_result(tower, selected_key, stdscr)
                show_message_screen(stdscr, t("detail.result"), [message or t("control.no_result")])
                needs_draw = True
    finally:
        stdscr.timeout(200)
        try:
            store = _screen_store(tower)
            if store is not None:
                selected_for_save, _ = _resolve_selection(
                    _visible_panes(tower), selected_key, selected_identity, original,
                )
                rows_for_save = _layout_rows(view_rows, selected_for_save, layout)
                store.save_last_view(rows_for_save, selected_for_save, layout, view_context)
        except (OSError, ValueError):
            tower.notice = t("control_room.screens_save_failed")
