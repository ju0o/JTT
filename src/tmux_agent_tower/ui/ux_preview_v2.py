"""Approved three-panel renderer shared by legacy Tower, JTT, and fixtures.

The fixture entrypoint uses synthetic in-memory data and an optional local
echo PTY. Legacy Tower projects its collected tmux snapshot and retains its
focus-handoff contract. JTT binds the same cards and RIGHT region to its own
managed local PTY sessions; only that explicit runtime forwards terminal keys.
"""

from __future__ import annotations

import curses
import errno
import fcntl
import locale
import os
import pty
import re
import select
import signal
import struct
import subprocess
import sys
import termios
import time
from pathlib import Path

from .render import display_width, truncate_to_width
from .widgets import safe_add
from .ux_preview import EXPECTED_BASE_SHA, make_preview_tower

_SOURCE_ROOT = Path(__file__).resolve().parents[3]
_ANSI_RE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")
_DEMO_CODE = """\
import os, signal, sys
def resized(_signum, _frame):
    try:
        size = os.get_terminal_size(0)
        print(f'[resize {size.lines}x{size.columns}]', flush=True)
    except OSError:
        pass
signal.signal(signal.SIGWINCH, resized)
print('격리 Echo PTY 준비됨', flush=True)
for line in sys.stdin:
    print('Echo: ' + line.rstrip('\\r\\n'), flush=True)
"""


def navigation_choices(tower) -> list[dict]:
    """Project the already-collected Tower snapshot and existing stores for this UI.

    This function performs no tmux, SSH, capture, or store-write operation. A
    Window ref, a WorkGroup target id, and a saved-screen identity remain
    distinct so stale references cannot be rebound by a similar label.
    """

    assets = [dict(asset) for asset in getattr(tower, "window_assets", ()) or ()]
    rows = [dict(row) for row in getattr(tower, "rows", ()) or ()
            if isinstance(row, dict) and row.get("kind") == "pane"
            and row.get("pane_id") and row.get("key") and not row.get("tower_runtime")]
    local_host = str(getattr(tower, "local_host", "") or "")
    remote_hosts = {
        str(host.get("alias") or ""): host
        for host in getattr(tower, "remote_hosts", ()) or ()
        if isinstance(host, dict) and str(host.get("alias") or "")
    }
    eligible_rows = [row for row in rows if _row_is_visible(row, local_host, remote_hosts)]
    eligible_targets = {str(row.get("target_id") or "") for row in eligible_rows
                        if row.get("target_id")}
    eligible_assets = []
    for asset in assets:
        ref = str(asset.get("window_ref") or "")
        if not ref:
            continue
        members = [row for row in rows if str(row.get("window_ref") or "") == ref]
        sample = members[0] if members else asset
        if _row_is_visible(sample, local_host, remote_hosts):
            eligible_assets.append((asset, members, sample))

    hidden_refs = set()
    project_store = getattr(tower, "projects", None)
    try:
        hidden_refs = set(project_store.hidden_refs()) if project_store is not None else set()
    except (OSError, ValueError):
        hidden_refs = set()
    local_windows = [(asset, members, sample) for asset, members, sample in eligible_assets
                     if not sample.get("remote")
                     and str(sample.get("transport") or "local") != "remote_tmux"
                     and str(sample.get("tmux_host") or asset.get("tmux_host") or "") == local_host
                     and str(asset.get("window_ref") or "") not in hidden_refs]
    remote_windows = [(asset, members, sample) for asset, members, sample in eligible_assets
                      if sample.get("remote") and str(sample.get("transport") or "") == "remote_tmux"
                      and str(sample.get("transport_target") or "") in remote_hosts
                      and str(asset.get("window_ref") or "") not in hidden_refs]

    all_named = local_windows + remote_windows
    name_counts: dict[str, int] = {}
    name_hosts: dict[str, set[str]] = {}
    name_sessions: dict[str, set[str]] = {}
    for asset, members, sample in all_named:
        name = _actual_window_name(asset, sample)
        name_counts[name] = name_counts.get(name, 0) + 1
        host = str(asset.get("tmux_host") or sample.get("tmux_host") or "")
        session = str(sample.get("session_id") or asset.get("session_id") or sample.get("session") or "")
        name_hosts.setdefault(name, set()).add(host)
        name_sessions.setdefault(name, set()).add(session)

    def window_choice(asset: dict, sample: dict, *, group: str, prefix: str = "") -> dict:
        ref = str(asset["window_ref"])
        name = _actual_window_name(asset, sample)
        index = asset.get("window_index", sample.get("window_index", ""))
        label = f"{prefix}{name} · #{index}" if str(index) else f"{prefix}{name}"
        if name_counts.get(name, 0) > 1:
            host_id = str(asset.get("tmux_host") or sample.get("tmux_host") or "")
            host_name = "MainPC" if host_id == local_host else host_id or "Host"
            session = str(sample.get("session_id") or asset.get("session_id")
                          or sample.get("session") or "Session")
            disambiguators = []
            if len(name_hosts.get(name, ())) > 1:
                disambiguators.append(host_name)
            if len(name_sessions.get(name, ())) > 1 or not disambiguators:
                disambiguators.append(session)
            label += f" · {'/'.join(disambiguators)}"
        return {"kind": "workspace", "group": group,
                "id": f"{'remote-window' if group == 'remote' else 'window'}:{ref}",
                "label": label, "refs": [ref], "scope": "windows", "window_ref": ref}

    choices = [{"kind": "section", "group": "windows", "label": "Windows"}]
    choices.extend(window_choice(asset, sample, group="windows")
                   for asset, _members, sample in local_windows)

    folders = getattr(tower, "folders", None)
    try:
        folder_values = folders.all() if folders is not None else []
    except (OSError, ValueError):
        folder_values = []
    for folder in folder_values:
        refs = [str(ref) for ref in folder.get("window_refs", ())
                if str(ref) and str(ref) not in hidden_refs]
        choices.append({"kind": "workspace", "group": "windows",
                        "id": f"folder:{folder.get('folder_id')}",
                        "label": f"[폴더] {folder.get('display_name') or '폴더'}",
                        "refs": refs, "scope": "folder", "folder_id": str(folder.get("folder_id") or "")})

    try:
        project_values = project_store.all() if project_store is not None else []
    except (OSError, ValueError):
        project_values = []
    for project in project_values:
        refs = [str(ref) for ref in project.get("window_refs", ())
                if str(ref) and str(ref) not in hidden_refs]
        choices.append({"kind": "workspace", "group": "windows",
                        "id": f"project:{project.get('project_id')}",
                        "label": f"[프로젝트] {project.get('display_name') or '프로젝트'}",
                        "refs": refs, "scope": "project", "project_id": str(project.get("project_id") or "")})

    unqueried_aliases = (
        list(remote_hosts.values())
        if getattr(tower, "remote_aliases_unqueried", False)
        else []
    )
    if remote_windows or unqueried_aliases:
        choices.append({"kind": "section", "group": "remote", "label": "원격 연결"})
        choices.extend(window_choice(asset, sample, group="remote", prefix="[Peer] ")
                       for asset, _members, sample in remote_windows)
        choices.extend({
            "kind": "info", "group": "remote",
            "id": f"remote-unqueried:{host.get('alias')}",
            "label": f"{host.get('display_name') or host.get('name') or host.get('alias')} · 미조회",
        } for host in unqueried_aliases)

    choices.append({"kind": "section", "group": "saved", "label": "저장한 작업"})
    groups_store = getattr(tower, "work_groups", None)
    try:
        groups = groups_store.all() if groups_store is not None else []
    except (OSError, ValueError):
        groups = []
    for group in groups:
        target_ids = [str(target) for target in
                      (group.get("member_order") or group.get("member_target_ids") or ()) if str(target)]
        choices.append({
            "kind": "workspace", "group": "saved", "scope": "work_group",
            "id": f"saved:{group.get('group_id')}",
            "label": str(group.get("display_name") or "저장한 작업"),
            "target_ids": target_ids, "member_labels": dict(group.get("member_labels") or {}),
        })

    screen_store = getattr(tower, "control_screens", None)
    try:
        screens = screen_store.screens() if screen_store is not None else []
        last_view = screen_store.last_view() if screen_store is not None else None
        if screen_store is not None and screen_store.corrupt:
            screens, last_view = [], None
    except (OSError, ValueError):
        screens, last_view = [], None
    if last_view:
        choices.append({"kind": "workspace", "group": "saved", "scope": "saved_screen",
                        "id": "screen:last-view", "label": "이어서 보기", "view": last_view})
    for screen in screens:
        choices.append({"kind": "workspace", "group": "saved", "scope": "saved_screen",
                        "id": f"screen:{screen.get('screen_id')}",
                        "label": str(screen.get("name") or "저장 화면"), "view": screen})

    choices.extend([
        {"kind": "action", "group": "actions", "id": "action:save", "label": "구성 저장",
         "dialog": "구성 저장"},
        {"kind": "action", "group": "actions", "id": "action:settings", "label": "설정 · 메뉴",
         "dialog": "설정 · 메뉴"},
    ])
    return choices


def _row_is_visible(row: dict, local_host: str, remote_hosts: dict) -> bool:
    transport = str(row.get("transport") or "local")
    if transport == "remote_tmux" or row.get("remote"):
        return transport == "remote_tmux" and str(row.get("transport_target") or "") in remote_hosts
    return bool(local_host and str(row.get("tmux_host") or "") == local_host)


def _actual_window_name(asset: dict, row: dict) -> str:
    return str(asset.get("window_name") or row.get("window_name")
               or asset.get("display_name") or row.get("display_name") or "Window")


def card_rectangles(count: int, region: tuple[int, int, int, int], *, scroll_row: int = 0):
    """Return (pane-index, x, y, width, height) card geometry for a panel."""

    x, y, width, height = region
    if count <= 0 or width < 4 or height < 3:
        return []
    if count == 1:
        return [(0, x, y, width, height)]

    left_width = max(1, (width - 1) // 2)
    right_x = x + left_width + 1
    right_width = max(1, width - left_width - 1)
    if count == 2:
        return [(0, x, y, left_width, height), (1, right_x, y, right_width, height)]

    top_height = max(1, (height - 1) // 2)
    bottom_y = y + top_height + 1
    bottom_height = max(1, height - top_height - 1)
    if count == 3:
        return [
            (0, x, y, left_width, top_height),
            (1, right_x, y, right_width, top_height),
            (2, x, bottom_y, width, bottom_height),
        ]
    if count == 4:
        return [
            (0, x, y, left_width, top_height),
            (1, right_x, y, right_width, top_height),
            (2, x, bottom_y, left_width, bottom_height),
            (3, right_x, bottom_y, right_width, bottom_height),
        ]

    tile_height = max(6, min(9, (height - 1) // 3))
    pitch = tile_height + 1
    visible_rows = max(1, (height + 1) // pitch)
    total_rows = (count + 1) // 2
    first_row = max(0, min(scroll_row, max(0, total_rows - visible_rows)))
    result = []
    for pane_index in range(first_row * 2, min(count, (first_row + visible_rows) * 2)):
        row = pane_index // 2 - first_row
        column = pane_index % 2
        card_x = x if column == 0 else right_x
        card_width = left_width if column == 0 else right_width
        result.append((pane_index, card_x, y + row * pitch, card_width, tile_height))
    return result


def _regions(width: int, height: int, active_panel: int):
    """Wide terminals show three columns; narrow terminals show one tabbed panel."""

    if width < 110 or height < 22:
        return {active_panel: (1, 0, max(1, width - 2), max(3, height - 1))}, False
    available = max(3, width - 4)
    left = max(24, min(36, int(available * 0.24)))
    center = max(34, min(56, int(available * 0.35)))
    if left + center > available - 1:
        center = max(30, available - left - 1)
    right = max(24, available - left - center - 2)
    x0 = 1
    x1 = x0 + left + 1
    x2 = x1 + center + 1
    panel_height = max(3, height - 1)
    return {
        0: (x0, 0, left, panel_height),
        1: (x1, 0, center, panel_height),
        2: (x2, 0, right, panel_height),
    }, True


_COLOR_ACTIVE = 1
_COLOR_SELECTED = 2
_COLOR_WORKING = 3
_COLOR_WAITING = 4
_COLOR_ERROR = 5


def _setup_colors() -> None:
    try:
        if not curses.has_colors():
            return
        curses.start_color()
        try:
            curses.use_default_colors()
            background = -1
        except curses.error:
            background = curses.COLOR_BLACK
        pairs = (
            (_COLOR_ACTIVE, curses.COLOR_CYAN, background),
            (_COLOR_SELECTED, curses.COLOR_CYAN, background),
            (_COLOR_WORKING, curses.COLOR_GREEN, background),
            (_COLOR_WAITING, curses.COLOR_CYAN, background),
            (_COLOR_ERROR, curses.COLOR_RED, background),
        )
        for pair, foreground, back in pairs:
            try:
                curses.init_pair(pair, foreground, back)
            except curses.error:
                if pair in (_COLOR_ACTIVE, _COLOR_SELECTED):
                    raise
    except (AttributeError, curses.error):
        return


def _color(pair: int, fallback: int = 0) -> int:
    try:
        return curses.color_pair(pair)
    except (AttributeError, curses.error):
        return fallback


def _put(stdscr, y: int, x: int, width: int, text: str, attr: int = 0) -> None:
    if width <= 0:
        return
    safe_add(stdscr, y, x, truncate_to_width(text, width), attr)


def _draw_box(stdscr, rect, title: str, active: bool = False) -> tuple[int, int, int, int]:
    x, y, width, height = rect
    if width < 4 or height < 3:
        return x + 1, y + 1, max(0, width - 2), max(0, height - 2)
    edge_attr = (_color(_COLOR_ACTIVE) | curses.A_BOLD) if active else curses.A_DIM
    _put(stdscr, y, x, width, "+" + "-" * (width - 2) + "+", edge_attr)
    _put(stdscr, y + height - 1, x, width, "+" + "-" * (width - 2) + "+", edge_attr)
    for row in range(y + 1, y + height - 1):
        _put(stdscr, row, x, 1, "|", edge_attr)
        _put(stdscr, row, x + width - 1, 1, "|", edge_attr)
    title_attr = (_color(_COLOR_ACTIVE) | curses.A_BOLD) if active else curses.A_BOLD
    safe_add(stdscr, y, x + 2, " " + truncate_to_width(title, max(1, width - 5)) + " ", title_attr)
    return x + 1, y + 1, width - 2, height - 2


def _status(row: dict) -> str:
    if row.get("_live_stale") or row.get("stale"):
        return "× 연결 끊김"
    return {
        "WORKING": "● 작업 중",
        "WAITING": "○ 입력 대기",
        "IDLE": "◇ 상태 미확인",
        "UNKNOWN": "◇ 상태 미확인",
        "DEAD": "× 종료",
    }.get(str(row.get("status") or "UNKNOWN").upper(), "◇ 상태 미확인")


def _status_attr(row: dict) -> int:
    status = str(row.get("status") or "UNKNOWN").upper()
    pair = _COLOR_WORKING if status == "WORKING" else _COLOR_WAITING if status == "WAITING" else _COLOR_ERROR if status == "DEAD" else 0
    return _color(pair, curses.A_DIM) if pair else curses.A_DIM


def _append_hit(state: dict, rect, kind: str, index: int = -1) -> None:
    x, y, width, height = rect
    state["hit_boxes"].append((x, y, width, height, kind, index))


def _draw_left(stdscr, rect, state: dict, active: bool) -> None:
    ix, iy, iw, ih = _draw_box(stdscr, rect, "내 작업공간", active)
    choices = state["choices"]
    windows = [(i, item) for i, item in enumerate(choices)
               if item.get("group") == "windows" and item.get("kind") == "workspace"]
    remote = [(i, item) for i, item in enumerate(choices)
              if item.get("group") == "remote" and item.get("kind") in ("workspace", "info")]
    saved = [(i, item) for i, item in enumerate(choices)
             if item.get("group") == "saved" and item.get("kind") == "workspace"]
    actions = [(i, item) for i, item in enumerate(choices) if item.get("group") == "actions"]

    compact = ih < 30
    gap = 1
    action_height = 1 if compact else 3
    windows_height = min(7 if compact else 14, max(4 if compact else 5, len(windows) + 2))
    remote_height = min(3 if compact else 6, max(3, len(remote) + 2)) if remote else 0
    saved_height = min(5 if compact else 9, max(3 if compact else 5, len(saved) + 2))
    section_count = 2 + bool(remote) + len(actions)
    total_height = (windows_height + remote_height + saved_height + action_height * len(actions)
                    + gap * max(0, section_count - 1))
    if total_height > ih:
        windows_height = max(3, windows_height - (total_height - ih))
        total_height = (windows_height + remote_height + saved_height + action_height * len(actions)
                        + gap * max(0, section_count - 1))
    cursor = iy + max(0, (ih - total_height) // 2)
    windows_rect = (ix, cursor, iw, windows_height)
    _draw_choice_group(stdscr, windows_rect,
                       "작업공간" if state.get("runtime_mode") == "jtt" else "Windows",
                       windows, state, active)
    cursor += windows_height + gap
    if remote:
        remote_rect = (ix, cursor, iw, remote_height)
        _draw_choice_group(stdscr, remote_rect, "원격 연결", remote, state, active)
        cursor += remote_height + gap
    saved_rect = (ix, cursor, iw, saved_height)
    _draw_choice_group(stdscr, saved_rect, "저장한 작업", saved, state, active)
    cursor += saved_height + gap
    for index, item in actions:
        action_rect = (ix, cursor, iw, action_height)
        _draw_action_choice(stdscr, action_rect, index, item, state, active)
        cursor += action_height + gap


def _draw_choice_group(stdscr, rect, title, items, state, active):
    ix, iy, iw, ih = _draw_box(stdscr, rect, title, active and any(i == state["nav_index"] for i, _ in items))
    visible = max(0, min(len(items), ih))
    group = next((item.get("group") for _choice_index, item in items), title)
    selected_position = next((position for position, (choice_index, _item) in enumerate(items)
                              if choice_index == state["nav_index"]), None)
    scrolls = state.setdefault("choice_scrolls", {})
    first = max(0, min(int(scrolls.get(group, 0)), max(0, len(items) - visible)))
    if selected_position is not None:
        if selected_position < first:
            first = selected_position
        elif selected_position >= first + visible:
            first = selected_position - visible + 1
    scrolls[group] = first
    visible_items = items[first:first + visible]
    row_start = max(0, (ih - len(visible_items)) // 2)
    for row_offset, (choice_index, item) in enumerate(visible_items):
        selected = choice_index == state["nav_index"]
        prefix = "▶ " if selected else "  "
        label = truncate_to_width(prefix + item["label"], max(1, iw - 2))
        attr = (_color(_COLOR_SELECTED) | curses.A_BOLD) if selected else curses.A_NORMAL
        line_y = iy + row_start + row_offset
        if item.get("kind") == "info":
            attr = curses.A_DIM
        _put(stdscr, line_y, ix + 1, max(0, iw - 2), label, attr)
        if item.get("kind") != "info":
            _append_hit(state, (ix, line_y, iw, 1), "left", choice_index)


def _draw_action_choice(stdscr, rect, choice_index, item, state, active):
    x, y, width, height = rect
    selected = choice_index == state["nav_index"]
    if height >= 3:
        _draw_box(stdscr, rect, "", active and selected)
        label_y = y + height // 2
    else:
        label_y = y
    label = f"▶ {item['label']}" if selected else item["label"]
    attr = (_color(_COLOR_SELECTED) | curses.A_BOLD) if selected else curses.A_DIM
    text_width = display_width(label)
    label_x = x + max(1, (width - text_width) // 2)
    _put(stdscr, label_y, label_x, max(1, width - (label_x - x) - 1), label, attr)
    _append_hit(state, rect, "left", choice_index)


def _draw_card(stdscr, rect, row: dict, selected: bool) -> None:
    index, x, y, width, height = rect
    attr = (_color(_COLOR_SELECTED) | curses.A_BOLD) if selected else curses.A_DIM
    _put(stdscr, y, x, width, "+" + "-" * max(0, width - 2) + "+", attr)
    for line_y in range(y + 1, y + height - 1):
        _put(stdscr, line_y, x, 1, "|", attr)
        _put(stdscr, line_y, x + width - 1, 1, "|", attr)
    _put(stdscr, y + height - 1, x, width, "+" + "-" * max(0, width - 2) + "+", attr)
    name = str(row.get("display_name") or row.get("task_name") or "Pane")
    marker = "▶ " if selected else "  "
    _put(stdscr, y + 1, x + 1, width - 2, marker + name,
         (_color(_COLOR_SELECTED) | curses.A_BOLD) if selected else curses.A_BOLD)
    if height > 4:
        agent = str(row.get("agent") or "Agent")
        status = _status(row)
        _put(stdscr, y + 2, x + 1, width - 2, f"{agent}  ·  {status}", _status_attr(row))
    if height > 5:
        preview = ("연결 끊김" if row.get("_live_stale") or row.get("stale") else
                   next((str(line).strip() for line in row.get("live_lines", ()) if str(line).strip()),
                        "미리보기 없음"))
        _put(stdscr, y + 3, x + 1, width - 2, preview, curses.A_DIM)


def _draw_center(stdscr, rect, state: dict, active: bool) -> None:
    center_title = "터미널 배치" if state.get("runtime_mode") == "jtt" else "Pane 배치"
    inner = _draw_box(stdscr, rect, center_title, active)
    ix, iy, iw, ih = inner
    workspace_name = state.get("workspace_name") or "Windows"
    _put(stdscr, iy, ix + 1, max(1, iw - 2), workspace_name, curses.A_BOLD)
    panes = state["panes"]
    if not panes:
        _put(stdscr, iy + 2, ix + 1, max(0, iw - 2), "Pane 없음", curses.A_DIM)
    else:
        card_area = (ix + 1, iy + 2, max(1, iw - 2), max(1, ih - 5))
        cards = card_rectangles(len(panes), card_area, scroll_row=state["card_scroll"])
        for item in cards:
            pane_index, x, y, width, height = item
            _draw_card(stdscr, item, panes[pane_index], pane_index == state["pane_index"])
            _append_hit(state, (x, y, width, height), "pane", pane_index)
    _draw_center_actions(stdscr, (ix + 1, iy + ih - 1, max(1, iw - 2), 1), state)


def _draw_center_actions(stdscr, rect, state):
    x, y, width, height = rect
    if state.get("runtime_mode") == "jtt":
        first, second = "[ 구성 저장 ]", "[ 새 터미널 ]"
    else:
        first, second = "[ 배치 편집 ]", "[ Pane 추가 ]"
    first_width = min(14, max(8, width // 2))
    second_width = max(1, width - first_width - 1)
    first_attr = (_color(_COLOR_SELECTED) | curses.A_BOLD) if state.get("center_actions_focused") and state.get("center_action_index") == 0 else curses.A_DIM
    second_attr = (_color(_COLOR_SELECTED) | curses.A_BOLD) if state.get("center_actions_focused") and state.get("center_action_index") == 1 else curses.A_DIM
    _put(stdscr, y, x, first_width, first, first_attr)
    _put(stdscr, y, x + first_width + 1, second_width, second, second_attr)
    _append_hit(state, (x, y, first_width, height), "center_action", 0)
    _append_hit(state, (x + first_width + 1, y, second_width, height), "center_action", 1)


def _visible_live_lines(state: dict) -> list[str]:
    demo = state.get("demo")
    if demo is not None:
        demo.read_available()
        return demo.visible_lines()
    row = state["panes"][state["pane_index"]] if state["panes"] else None
    if row and (row.get("_live_stale") or row.get("stale")):
        return ["연결되지 않은 Pane입니다."]
    return [str(line) for line in (row or {}).get("live_lines", ())]


def _managed_binding(state: dict):
    """Optional owned-terminal binding; never resolve it as a tmux Pane."""
    if state.get("runtime_mode") == "local_read_only_preview":
        return None
    row = _selected_row(state)
    if not row or row.get("terminal_kind") != "tower_managed":
        return None
    binding = state.get("managed_terminals", {}).get(row.get("managed_id"))
    return binding if binding is not None and binding.matches(row) else None


def _release_managed_input(state: dict) -> None:
    for binding in state.get("managed_terminals", {}).values():
        binding.release()


def _draw_right(stdscr, rect, state: dict, active: bool) -> None:
    panes = state["panes"]
    row = panes[state["pane_index"]] if panes else None
    pane_name = str((row or {}).get("display_name") or (row or {}).get("pane_id") or "LIVE")
    right_title = "터미널" if state.get("runtime_mode") == "jtt" else "LIVE"
    inner = _draw_box(stdscr, rect, f"{right_title} · {pane_name}", active)
    if row and row.get("terminal_kind") == "tower_managed":
        binding = _managed_binding(state)
        if binding is not None:
            binding.draw(stdscr, inner, active)
        else:
            _put(stdscr, inner[1], inner[0], inner[2], "연결되지 않음 · 입력 차단")
        _append_hit(state, rect, "right", 0)
        return
    ix, iy, iw, ih = inner
    content_y = iy
    content_height = max(0, ih - 2)
    lines = _visible_live_lines(state)
    max_scroll = max(0, len(lines) - content_height)
    state["live_scroll"] = min(max(0, state["live_scroll"]), max_scroll)
    start = max_scroll - state["live_scroll"]
    shown = lines[start:start + content_height]
    for offset, line in enumerate(shown):
        _put(stdscr, content_y + offset, ix + 1, max(0, iw - 2), line)
    if ih > 0:
        status = (f"격리 Echo · {row.get('agent') or 'Agent'}" if state.get("demo") and row
                  else "읽기 전용 Preview · 입력/포커스 차단"
                  if state.get("runtime_mode") == "local_read_only_preview"
                  else f"{row.get('agent') or 'Agent'} · {_status(row)} · 관찰 중" if row
                  else "관찰 중")
        _put(stdscr, iy + ih - 1, ix + 1, max(0, iw - 2), status,
             _status_attr(row) if row and not state.get("demo") else curses.A_DIM)
    _append_hit(state, rect, "right", 0)


def _draw_dialog(stdscr, state):
    dialog = state.get("dialog")
    if not dialog:
        return
    height, width = stdscr.getmaxyx()
    box_width = max(12, min(46, width - 4))
    box_height = min(7, max(5, height - 2))
    x = max(0, (width - box_width) // 2)
    y = max(0, (height - box_height) // 2)
    inner = _draw_box(stdscr, (x, y, box_width, box_height), dialog["title"], True)
    ix, iy, iw, ih = inner
    body = dialog.get("body") or ""
    if body and ih > 1:
        _put(stdscr, iy, ix + 1, max(0, iw - 2), body, curses.A_DIM)
    close_y = iy + ih - 1
    close_label = "[ 닫기 ]"
    close_x = x + max(1, (box_width - len(close_label)) // 2)
    _put(stdscr, close_y, close_x, max(1, box_width - 2), close_label, _color(_COLOR_SELECTED) | curses.A_BOLD)
    _append_hit(state, (close_x, close_y, max(1, box_width - 2), 1), "dialog_close", 0)


def draw_preview_v2(stdscr, state: dict) -> None:
    """Render only the approved workspace launcher, pane layout, and single LIVE."""

    stdscr.erase()
    height, width = stdscr.getmaxyx()
    regions, wide = _regions(width, height, state["active_panel"])
    state["hit_boxes"] = []
    if wide:
        _draw_left(stdscr, regions[0], state, state["active_panel"] == 0)
        _draw_center(stdscr, regions[1], state, state["active_panel"] == 1)
        _draw_right(stdscr, regions[2], state, state["active_panel"] == 2)
    elif state["active_panel"] == 0:
        _draw_left(stdscr, regions[0], state, True)
    elif state["active_panel"] == 1:
        _draw_center(stdscr, regions[1], state, True)
    else:
        _draw_right(stdscr, regions[2], state, True)
    _draw_dialog(stdscr, state)
    try:
        stdscr.noutrefresh()
        curses.doupdate()
    except curses.error:
        pass


_PANE_IDENTITY_FIELDS = (
    "target_id", "tmux_host", "session", "session_id", "window_id", "pane_id",
    "pane_pid", "transport", "transport_target", "remote",
)


def _pane_signature(row: dict | None) -> tuple | None:
    if not isinstance(row, dict) or not row.get("pane_id"):
        return None
    if row.get("terminal_kind") == "tower_managed":
        identity = row.get("managed_identity") or {}
        session_id = str(identity.get("session_id") or "")
        generation = str(identity.get("generation") or "")
        shell_pid = str(identity.get("shell_pid") or "")
        if not all((session_id, generation, shell_pid)):
            return None
        return ("jtt", session_id, generation, shell_pid)
    values = tuple(str(row.get(field) or "") for field in _PANE_IDENTITY_FIELDS[:-1])
    # Fail closed when any identity field required by the existing saved-screen
    # contract is absent. transport_target may legitimately be empty locally,
    # but remains part of the exact comparison tuple.
    if not all(values[index] for index in (0, 1, 2, 3, 4, 5, 6, 7)):
        return None
    return values + (bool(row.get("remote")),)


def _reference_signature(reference: dict | None) -> tuple | None:
    identity = reference.get("identity") if isinstance(reference, dict) else None
    if not isinstance(identity, dict):
        return None
    return _pane_signature({**identity, "kind": "pane"})


def _live_rows(tower) -> list[dict]:
    hosts = {str(host.get("alias") or ""): host for host in getattr(tower, "remote_hosts", ()) or ()
             if isinstance(host, dict) and host.get("alias")}
    local_host = str(getattr(tower, "local_host", "") or "")
    return [dict(row) for row in getattr(tower, "rows", ()) or ()
            if isinstance(row, dict) and row.get("kind") == "pane" and row.get("pane_id")
            and row.get("key") and not row.get("tower_runtime")
            and _row_is_visible(row, local_host, hosts)]


def _stale_work_group_row(target_id: str, label: str) -> dict:
    return {
        "kind": "pane", "key": f"stale-work-group:{target_id}", "target_id": target_id,
        "display_name": label or "연결되지 않은 Pane", "agent": "", "status": "UNKNOWN",
        "stale": True, "_live_stale": True, "live_lines": [],
    }


def _stale_screen_row(reference: dict, index: int) -> dict:
    identity = dict(reference.get("identity") or {})
    return {
        **identity, "kind": "pane", "key": f"saved-screen-stale:{index}",
        "display_name": str(reference.get("display_name") or "저장한 Pane"),
        "status": "UNKNOWN", "stale": True, "_live_stale": True, "live_lines": [],
    }


def _unverified_live_row(row: dict) -> dict:
    """Keep a listed Pane visible but never display output without full identity."""

    if _pane_signature(row) is not None:
        return dict(row)
    result = dict(row)
    result.update(stale=True, _live_stale=True, status="UNKNOWN", live_lines=[])
    name = str(result.get("display_name") or result.get("pane_id") or "Pane")
    if "신원 확인 불가" not in name:
        result["display_name"] = f"{name} · 신원 확인 불가"
    return result


def _resolve_screen_rows(tower, references) -> list[dict]:
    live = _live_rows(tower)
    resolved = []
    for index, reference in enumerate(references or ()):
        signature = _reference_signature(reference)
        window_ref = str(reference.get("window_ref") or "") if isinstance(reference, dict) else ""
        matches = [row for row in live if signature is not None
                   and _pane_signature(row) == signature
                   and str(row.get("window_ref") or "") == window_ref]
        resolved.append(dict(matches[0]) if len(matches) == 1 else _stale_screen_row(reference, index))
    return resolved


def _resolve_choice_rows(tower, choice: dict) -> list[dict]:
    scope = str(choice.get("scope") or "windows")
    live = _live_rows(tower)
    if scope == "work_group":
        by_target: dict[str, list[dict]] = {}
        for row in live:
            if row.get("target_id"):
                by_target.setdefault(str(row["target_id"]), []).append(row)
        labels = choice.get("member_labels") or {}
        rows = []
        for target in choice.get("target_ids") or ():
            matches = by_target.get(str(target), ())
            rows.append(dict(matches[0]) if len(matches) == 1 and _pane_signature(matches[0]) is not None
                         else _stale_work_group_row(str(target), str(labels.get(str(target)) or "")))
        return rows
    if scope == "saved_screen":
        view = choice.get("view") or {}
        rows = _resolve_screen_rows(tower, view.get("panes") or ())
        # Saved screens keep their own exact selection; an absent or changed
        # reference stays stale instead of falling through to a reused Pane ID.
        selected = view.get("selected")
        selected_sig = _reference_signature(selected)
        if selected_sig is not None:
            choice["selected_target_id"] = str((selected or {}).get("identity", {}).get("target_id") or "")
            choice["selected_signature"] = selected_sig
        return rows

    refs = [str(ref) for ref in choice.get("refs", ()) if str(ref)]
    by_ref: dict[str, list[dict]] = {}
    for row in live:
        ref = str(row.get("window_ref") or "")
        if ref in refs:
            by_ref.setdefault(ref, []).append(row)
    rows = []
    assets = {str(asset.get("window_ref") or ""): asset
              for asset in getattr(tower, "window_assets", ()) or () if asset.get("window_ref")}
    folders = getattr(tower, "folders", None)
    for ref in refs:
        members = sorted(by_ref.get(ref, ()), key=lambda row: (
            int(row.get("pane_index") or 0) if str(row.get("pane_index") or "0").isdigit() else 0,
            str(row.get("pane_id") or ""),
        ))
        if members:
            rows.extend(_unverified_live_row(row) for row in members)
            continue
        asset = assets.get(ref, {})
        try:
            saved_window = folders.window(ref) if folders is not None else {}
        except (OSError, ValueError):
            saved_window = {}
        name = (asset.get("window_name") or saved_window.get("display_name")
                or asset.get("display_name") or "Window")
        rows.append({
            "kind": "pane", "key": f"stale-window:{ref}", "window_ref": ref,
            "display_name": f"{name} · 연결되지 않음", "agent": "", "status": "UNKNOWN",
            "stale": True, "_live_stale": True, "live_lines": [],
        })
    return rows


def _state(tower) -> dict:
    choices = navigation_choices(tower)
    # Control Room historically resumes its last observed screen on entry.
    # Keep that behavior, but resolve every saved Pane through the same strict
    # identity path used by an explicitly opened saved screen.
    first_choice = next((index for index, item in enumerate(choices)
                         if item.get("id") == "screen:last-view"), -1)
    visible = getattr(tower, "visible_rows", ()) or ()
    selected_home = visible[getattr(tower, "selected", -1)] if (
        0 <= getattr(tower, "selected", -1) < len(visible)
    ) else None
    selected_ref = str((selected_home or {}).get("window_ref") or "")
    home_kind = str((selected_home or {}).get("kind") or "")
    home_id = str((selected_home or {}).get("group_id") or (selected_home or {}).get("project_id")
                  or (selected_home or {}).get("folder_id") or "")
    preferred_id = (f"saved:{home_id}" if home_kind == "work_group" else
                    f"project:{home_id}" if home_kind == "project" else
                    f"folder:{home_id}" if home_kind == "folder" else "")
    if first_choice < 0:
        first_choice = next((index for index, item in enumerate(choices)
                             if preferred_id and item.get("id") == preferred_id), -1)
    if first_choice < 0:
        first_choice = next((index for index, item in enumerate(choices)
                             if item.get("kind") == "workspace" and selected_ref
                             and selected_ref in (item.get("refs") or ())), -1)
    if first_choice < 0:
        first_choice = next((index for index, item in enumerate(choices)
                             if item.get("group") == "windows" and item.get("kind") == "workspace"), -1)
    if first_choice < 0:
        first_choice = next((index for index, item in enumerate(choices)
                             if item.get("kind") == "workspace"), -1)
    first_item = choices[first_choice] if first_choice >= 0 else {}
    rows = _resolve_choice_rows(tower, first_item) if first_item else []
    selected_target = str(first_item.get("selected_target_id") or "")
    selected_signature = first_item.get("selected_signature")
    if home_kind == "pane" and selected_home:
        selected_target = str(selected_home.get("target_id") or "")
        selected_signature = _pane_signature(selected_home)
    pane_index = next((index for index, row in enumerate(rows)
                       if selected_target and str(row.get("target_id") or "") == selected_target
                       and (selected_signature is None or _pane_signature(row) == selected_signature)), 0)
    selected_row = rows[pane_index] if rows else None
    state = {
        "tower": tower,
        "choices": choices,
        "nav_index": first_choice,
        "workspace_id": first_item.get("id", ""),
        "workspace_name": first_item.get("label", "Windows"),
        "window_refs": tuple(first_item.get("refs", ())),
        "panes": rows,
        "pane_index": pane_index,
        "selected_target_id": str((selected_row or {}).get("target_id") or ""),
        "selected_signature": _pane_signature(selected_row),
        "card_scroll": 0,
        "live_scroll": 0,
        "active_panel": 0,
        "center_actions_focused": False,
        "center_action_index": 0,
        "choice_scrolls": {},
        "hit_boxes": [],
        "demo": None,
        "dialog": None,
        "runtime_mode": "fixture",
        "custom_view": False,
        "last_size": (24, 80),
    }
    return state


def _set_choice(state: dict, choice_index: int) -> None:
    if choice_index < 0 or choice_index >= len(state["choices"]):
        return
    choice = state["choices"][choice_index]
    if choice.get("kind") != "workspace":
        return
    _release_managed_input(state)
    state["nav_index"] = choice_index
    state["workspace_id"] = choice["id"]
    state["workspace_name"] = choice["label"]
    state["window_refs"] = tuple(choice.get("refs") or ())
    state["panes"] = _resolve_choice_rows(state["tower"], choice)
    state["panes"] = [dict(row) for row in state.get("managed_cards", {}).get(choice["id"], ())] + state["panes"]
    selected_target = str(choice.get("selected_target_id") or "")
    selected_signature = choice.get("selected_signature")
    state["pane_index"] = next((index for index, row in enumerate(state["panes"])
                                if selected_target and str(row.get("target_id") or "") == selected_target
                                and (selected_signature is None or _pane_signature(row) == selected_signature)), 0)
    selected = state["panes"][state["pane_index"]] if state["panes"] else None
    state["selected_target_id"] = str((selected or {}).get("target_id") or "")
    state["selected_signature"] = _pane_signature(selected)
    state["window_refs"] = tuple(dict.fromkeys(str(row.get("window_ref") or "")
                                                for row in state["panes"] if row.get("window_ref"))) or state["window_refs"]
    state["center_actions_focused"] = False
    state["card_scroll"] = 0
    state["live_scroll"] = 0
    state["custom_view"] = False
    if state.get("runtime_mode") == "jtt":
        handler = state.get("jtt_workspace_handler")
        if callable(handler):
            handler(state, choice)


def _activate_choice(state: dict, choice_index: int) -> None:
    if choice_index < 0 or choice_index >= len(state["choices"]):
        return
    choice = state["choices"][choice_index]
    state["nav_index"] = choice_index
    if choice.get("kind") == "workspace":
        _set_choice(state, choice_index)
        state["active_panel"] = 1
    elif choice.get("kind") == "action":
        if state.get("runtime_mode") == "jtt":
            handler = state.get("jtt_action_handler")
            if callable(handler):
                handler(state.get("stdscr"), state, choice)
            return
        state["dialog"] = {
            "title": choice.get("dialog") or choice["label"],
            "body": ("읽기 전용 Preview · 변경 불가"
                     if state.get("runtime_mode") == "local_read_only_preview"
                     else state.get("workspace_name", "")),
        }


def _ensure_card_visible(state: dict, height: int, width: int) -> None:
    count = len(state["panes"])
    if count <= 4:
        state["card_scroll"] = 0
        return
    regions, wide = _regions(width, height, 1)
    if wide:
        region = regions[1]
        inner_h = max(1, region[3] - 9)
    else:
        inner_h = max(1, height - 10)
    card_h = max(6, min(9, (inner_h - 1) // 3))
    visible_rows = max(1, (inner_h + 1) // (card_h + 1))
    row = state["pane_index"] // 2
    if row < state["card_scroll"]:
        state["card_scroll"] = row
    elif row >= state["card_scroll"] + visible_rows:
        state["card_scroll"] = row - visible_rows + 1


def _hit_test(state: dict, x: int, y: int) -> str | None:
    if state.get("dialog"):
        for bx, by, width, height, kind, _index in reversed(state["hit_boxes"]):
            if kind == "dialog_close" and bx <= x < bx + width and by <= y < by + height:
                state["dialog"] = None
                return kind
        return None
    for bx, by, width, height, kind, index in reversed(state["hit_boxes"]):
        if bx <= x < bx + width and by <= y < by + height:
            if kind == "left":
                choice = state["choices"][index]
                if choice.get("kind") == "workspace":
                    _set_choice(state, index)
                    state["active_panel"] = 0
                else:
                    state["nav_index"] = index
                    if state.get("runtime_mode") == "tower":
                        _open_tower_choice(state, choice)
                    else:
                        _activate_choice(state, index)
            elif kind == "pane":
                if 0 <= index < len(state["panes"]):
                    _release_managed_input(state)
                    state["pane_index"] = index
                    row = state["panes"][index]
                    state["selected_target_id"] = str(row.get("target_id") or "")
                    state["selected_signature"] = _pane_signature(row)
                    state["center_actions_focused"] = False
                    state["active_panel"] = 1
                    state["live_scroll"] = 0
            elif kind == "right":
                state["active_panel"] = 2
                binding = _managed_binding(state)
                if binding is not None:
                    binding.enter(state.get("stdscr"), state)
                elif (_selected_row(state) or {}).get("terminal_kind") == "tower_managed":
                    pass
                elif state.get("runtime_mode") == "tower":
                    _handoff_selected(state)
            elif kind == "center_action":
                state["center_action_index"] = index
                if state.get("runtime_mode") == "tower":
                    _activate_tower_center_action(state, index)
                elif state.get("runtime_mode") == "jtt":
                    handler = state.get("jtt_center_action_handler")
                    if callable(handler):
                        handler(state.get("stdscr"), state, index)
                elif state.get("runtime_mode") == "local_read_only_preview":
                    state["dialog"] = {
                        "title": "읽기 전용 Preview",
                        "body": "저장·추가 변경은 차단됨",
                    }
                else:
                    title = "배치 편집" if index == 0 else "Pane 추가"
                    state["dialog"] = {"title": title, "body": state.get("workspace_name", "")}
            elif kind == "dialog_close":
                state["dialog"] = None
            return kind
    return None


def _enable_mouse() -> int | None:
    try:
        mask = getattr(curses, "BUTTON1_CLICKED", 0)
        if mask:
            available, previous = curses.mousemask(mask)
            if available & mask:
                return previous
            curses.mousemask(previous)
    except (AttributeError, TypeError, ValueError, curses.error):
        pass
    return None


def _restore_mouse(previous: int | None) -> None:
    if previous is None:
        return
    try:
        curses.mousemask(previous)
    except (AttributeError, curses.error):
        pass


class _EchoPty:
    """A small, file/network-free echo REPL attached to a private PTY."""

    def __init__(self):
        self.pid, self.fd = pty.fork()
        if self.pid == 0:
            env = {"PATH": os.defpath, "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "TERM": "xterm"}
            try:
                os.execve(sys.executable, [sys.executable, "-I", "-u", "-c", _DEMO_CODE], env)
            except OSError:
                os._exit(127)
        os.set_blocking(self.fd, False)
        self.lines: list[str] = []
        self.pending = ""
        self.closed = False
        self._ready = False
        self._requested_size: tuple[int, int] | None = None
        self._signaled_size: tuple[int, int] | None = None
        self.read_available()

    def resize(self, rows: int, columns: int) -> None:
        if self.closed:
            return
        self._requested_size = (max(1, rows), max(1, columns))
        fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", *self._requested_size, 0, 0))
        self._notify_resize()

    def _notify_resize(self) -> None:
        if not self._ready or not self._requested_size or self._requested_size == self._signaled_size:
            return
        try:
            os.kill(self.pid, signal.SIGWINCH)
        except ProcessLookupError:
            return
        self._signaled_size = self._requested_size

    def send(self, data: bytes) -> None:
        if self.closed or not data:
            return
        try:
            os.write(self.fd, data)
        except (BlockingIOError, OSError):
            pass

    def read_available(self) -> None:
        if self.closed:
            return
        while True:
            ready, _, _ = select.select([self.fd], [], [], 0)
            if not ready:
                break
            try:
                raw = os.read(self.fd, 8192)
            except BlockingIOError:
                break
            except OSError as exc:
                if exc.errno == errno.EIO:
                    break
                raise
            if not raw:
                break
            text = _ANSI_RE.sub("", raw.decode("utf-8", errors="replace")).replace("\r\n", "\n").replace("\r", "\n")
            for char in text:
                if char == "\n":
                    self.lines.append(self.pending)
                    self.pending = ""
                elif char in ("\b", "\x7f"):
                    self.pending = self.pending[:-1]
                elif char == "\t" or ord(char) >= 32:
                    self.pending += char
            if len(self.lines) > 400:
                del self.lines[:-400]
        if self.pending:
            visible = self.pending
            if not self.lines or self.lines[-1] != visible:
                # Show live input without committing it as a response line.
                self.preview_pending = visible
        else:
            self.preview_pending = ""
        self._ready = any("준비됨" in line for line in self.lines)
        self._notify_resize()

    def visible_lines(self) -> list[str]:
        pending = getattr(self, "preview_pending", "")
        return self.lines + ([pending] if pending else [])

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            os.kill(self.pid, signal.SIGHUP)
        except ProcessLookupError:
            pass
        deadline = time.monotonic() + 0.4
        while time.monotonic() < deadline:
            try:
                waited, _status = os.waitpid(self.pid, os.WNOHANG)
            except ChildProcessError:
                waited = self.pid
            if waited:
                break
            time.sleep(0.01)
        else:
            try:
                os.kill(self.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                os.waitpid(self.pid, 0)
            except ChildProcessError:
                pass
        try:
            os.close(self.fd)
        except OSError:
            pass


def _key_bytes(key) -> bytes | None:
    if key in (curses.KEY_BACKSPACE, "\x7f", "\b"):
        return b"\x7f"
    if key in (curses.KEY_ENTER, "\n", "\r"):
        return b"\n"
    arrows = {
        curses.KEY_UP: b"\x1b[A", curses.KEY_DOWN: b"\x1b[B",
        curses.KEY_RIGHT: b"\x1b[C", curses.KEY_LEFT: b"\x1b[D",
        getattr(curses, "KEY_HOME", -100): b"\x1b[H",
        getattr(curses, "KEY_END", -101): b"\x1b[F",
        getattr(curses, "KEY_DC", -102): b"\x1b[3~",
    }
    if key in arrows:
        return arrows[key]
    if isinstance(key, str) and len(key) == 1:
        return key.encode("utf-8")
    if isinstance(key, str):
        return None
    if isinstance(key, int) and 0 <= key < 32:
        return bytes((key,))
    return None


def _move_pane(state: dict, delta: int, height: int, width: int) -> None:
    if not state["panes"]:
        return
    _release_managed_input(state)
    state["center_actions_focused"] = False
    state["pane_index"] = max(0, min(len(state["panes"]) - 1, state["pane_index"] + delta))
    row = state["panes"][state["pane_index"]]
    state["selected_target_id"] = str(row.get("target_id") or "")
    state["selected_signature"] = _pane_signature(row)
    state["live_scroll"] = 0
    _ensure_card_visible(state, height, width)


def _selected_row(state: dict) -> dict | None:
    rows = state.get("panes") or ()
    index = int(state.get("pane_index", 0))
    return rows[index] if 0 <= index < len(rows) else None


def _current_selected_row(state: dict) -> dict | None:
    selected = _selected_row(state)
    if not selected or selected.get("stale") or selected.get("_live_stale"):
        return None
    signature = _pane_signature(selected)
    matches = [row for row in _live_rows(state["tower"])
               if signature is not None and _pane_signature(row) == signature
               and str(row.get("window_ref") or "") == str(selected.get("window_ref") or "")]
    return dict(matches[0]) if len(matches) == 1 else None


def _handoff_selected(state: dict) -> None:
    from .control_room import _request_right_handoff
    from .widgets import show_message_screen

    selected = _selected_row(state)
    if not selected:
        return
    if selected.get("terminal_kind") == "tower_managed":
        return
    current = _current_selected_row(state)
    _request_right_handoff(
        state["stdscr"], state["tower"], current or selected, current is not None,
    )


def _screen_menu_state(state: dict) -> dict:
    selected = _selected_row(state)
    rows = list(state.get("panes") or ())
    current = _current_selected_row(state)
    selected = current or selected
    choice = state["choices"][state["nav_index"]] if 0 <= state["nav_index"] < len(state["choices"]) else {}
    layout = "focus" if len(rows) <= 1 else "split-2" if len(rows) == 2 else "main-plus-side" if len(rows) == 3 else "grid-4"
    return {
        "layout": layout, "layout_pinned": True,
        "view_rows": rows, "selected_row": dict(selected or {}),
        "selected_key": str((selected or {}).get("key") or ""),
        "selected_identity": _pane_signature(selected), "original": dict(selected or {}),
        "cursor_key": None,
        "context": {
            "project_id": choice.get("project_id") or None,
            "window_ref": str((selected or {}).get("window_ref") or "") or None,
        },
    }


def _apply_screen_menu_result(state: dict, room: dict) -> None:
    rows = list(room.get("view_rows") or ())
    selected = room.get("selected_row") or {}
    signature = _pane_signature(selected)
    state["panes"] = rows
    state["pane_index"] = next((i for i, row in enumerate(rows)
                                if signature is not None and _pane_signature(row) == signature), 0)
    current = _selected_row(state)
    state["selected_target_id"] = str((current or {}).get("target_id") or "")
    state["selected_signature"] = _pane_signature(current)
    state["custom_view"] = True
    state["window_refs"] = tuple(dict.fromkeys(str(row.get("window_ref") or "")
                                                for row in rows if row.get("window_ref")))
    selected_ref = str((current or {}).get("window_ref") or room.get("context", {}).get("window_ref") or "")
    asset = next((item for item in getattr(state["tower"], "window_assets", ()) or ()
                  if str(item.get("window_ref") or "") == selected_ref), None)
    state["workspace_name"] = str((asset or {}).get("window_name")
                                  or (asset or {}).get("display_name") or "저장한 화면")
    state["live_scroll"] = state["card_scroll"] = 0


def _open_existing_screen_menu(state: dict) -> None:
    from .control_room import _open_screen_menu, _visible_panes

    tower = state["tower"]
    room = _screen_menu_state(state)
    groups = getattr(getattr(tower, "work_groups", None), "all", lambda: [])()
    _open_screen_menu(state["stdscr"], tower, room, _visible_panes(tower), groups, None)
    _apply_screen_menu_result(state, room)


def _add_existing_pane(state: dict) -> None:
    from .widgets import run_list_picker, show_message_screen

    tower = state["tower"]
    current_signatures = {_pane_signature(row) for row in state.get("panes", ())}
    candidates = [row for row in _live_rows(tower)
                  if _pane_signature(row) not in current_signatures]
    if not candidates:
        show_message_screen(state["stdscr"], "Pane 추가", ["추가할 수 있는 연결된 Pane이 없습니다."])
        return
    items = []
    for row in candidates:
        name = str(row.get("display_name") or row.get("pane_id") or "Pane")
        host = str(row.get("execution_host") or row.get("tmux_host") or "")
        window = str(row.get("window_name") or row.get("window_index") or "Window")
        items.append((str(row.get("target_id") or ""), f"{name} · {host} · {window}"))
    pick = run_list_picker(state["stdscr"], "Pane 추가", items, searchable=True)
    if pick.cancelled or not pick.selected_key:
        return
    chosen = [row for row in candidates if str(row.get("target_id") or "") == str(pick.selected_key)]
    if len(chosen) != 1:
        return
    state["panes"].append(dict(chosen[0]))
    state["pane_index"] = len(state["panes"]) - 1
    state["selected_target_id"] = str(chosen[0].get("target_id") or "")
    state["selected_signature"] = _pane_signature(chosen[0])
    state["custom_view"] = True
    state["live_scroll"] = state["card_scroll"] = 0


def _open_tower_choice(state: dict, choice: dict) -> None:
    from .widgets import show_message_screen

    stdscr, tower = state["stdscr"], state["tower"]
    choice_id = str(choice.get("id") or "")
    if choice_id == "action:save" or choice.get("scope") == "saved_screen":
        _open_existing_screen_menu(state)
    elif choice_id == "action:settings":
        from .settings_menu import open_settings

        open_settings(stdscr, tower)
    elif choice.get("scope") == "project":
        from .projects import open_project_editor

        open_project_editor(stdscr, tower, str(choice.get("project_id") or ""))
    elif choice.get("scope") == "folder":
        from .structure_menu import open_folder_menu

        folder = next((item for item in tower.folders.all()
                       if str(item.get("folder_id") or "") == str(choice.get("folder_id") or "")), None)
        if folder:
            open_folder_menu(stdscr, tower, {**folder, "kind": "folder"})
    elif choice.get("scope") == "windows":
        from .structure_menu import open_window_asset_menu

        ref = str(choice.get("window_ref") or "")
        asset = next((item for item in tower.window_assets
                      if str(item.get("window_ref") or "") == ref), None)
        if asset:
            open_window_asset_menu(stdscr, tower, {**asset, "kind": "window_asset"})
    elif choice.get("scope") == "work_group":
        from .structure_menu import open_work_group_menu

        group_id = choice_id.removeprefix("saved:")
        group = next((item for item in tower.work_groups.all()
                      if str(item.get("group_id") or "") == group_id), None)
        if group:
            open_work_group_menu(stdscr, tower, {**group, "kind": "work_group"})
    elif choice.get("kind") == "workspace":
        _set_choice(state, state["nav_index"])
        state["active_panel"] = 1
    else:
        show_message_screen(stdscr, "내 작업공간", ["선택한 항목의 기존 메뉴를 사용할 수 없습니다."])


def _open_selected_context(state: dict) -> None:
    from . import structure_menu

    choice = state["choices"][state["nav_index"]] if 0 <= state["nav_index"] < len(state["choices"]) else {}
    tower, stdscr = state["tower"], state["stdscr"]
    scope = choice.get("scope")
    if state["active_panel"] == 2:
        _handoff_selected(state)
    elif state["active_panel"] == 1:
        row = _current_selected_row(state)
        if row:
            structure_menu.open_pane_menu(stdscr, tower, row)
    elif choice.get("kind") == "action":
        _open_tower_choice(state, choice)
    elif scope == "folder":
        folder = next((item for item in tower.folders.all()
                       if str(item.get("folder_id") or "") == str(choice.get("folder_id") or "")), None)
        if folder:
            structure_menu.open_folder_menu(stdscr, tower, {**folder, "kind": "folder"})
    elif scope == "project":
        from .projects import open_project_editor

        open_project_editor(stdscr, tower, str(choice.get("project_id") or ""))
    elif scope == "windows":
        asset = next((item for item in tower.window_assets
                      if str(item.get("window_ref") or "") == str(choice.get("window_ref") or "")), None)
        if asset:
            structure_menu.open_window_asset_menu(stdscr, tower, {**asset, "kind": "window_asset"})
    elif scope == "work_group":
        _open_tower_choice(state, choice)
    elif scope == "saved_screen":
        _open_existing_screen_menu(state)


def _activate_tower_center_action(state: dict, index: int) -> None:
    if index == 0:
        _open_existing_screen_menu(state)
    elif index == 1:
        _add_existing_pane(state)


def _copy_selected_result(state: dict) -> None:
    from .control_room import _copy_key_is_unambiguous
    from .control_view import _copy_result
    from .widgets import show_message_screen

    selected = _current_selected_row(state)
    if selected is None:
        message = "선택한 Pane이 바뀌었거나 연결이 끊겨 결과를 복사할 수 없습니다."
    elif not _copy_key_is_unambiguous(state["tower"].rows, selected):
        message = "선택한 Pane을 하나로 확인할 수 없습니다."
    else:
        message = _copy_result(state["tower"], str(selected.get("key") or ""), state["stdscr"])
    show_message_screen(state["stdscr"], "결과 복사", [message or "결과를 복사하지 못했습니다."])


def _reconcile_room_state(state: dict) -> None:
    """Adopt a refreshed Tower snapshot by complete identity, never by label."""

    old_choice_id = str(state.get("workspace_id") or "")
    old_target_id = str(state.get("selected_target_id") or "")
    old_signature = state.get("selected_signature")
    old_selected = dict(_selected_row(state) or {})
    if state.get("custom_view"):
        rebuilt = []
        live = _live_rows(state["tower"])
        for index, old in enumerate(state.get("panes", ())):
            signature = _pane_signature(old)
            window_ref = str(old.get("window_ref") or "")
            matches = [row for row in live if signature is not None
                       and _pane_signature(row) == signature
                       and str(row.get("window_ref") or "") == window_ref]
            rebuilt.append(dict(matches[0]) if len(matches) == 1 else
                           _stale_screen_row({"identity": {field: old.get(field) for field in _PANE_IDENTITY_FIELDS},
                                              "window_ref": window_ref,
                                              "display_name": old.get("display_name") or "저장한 Pane"}, index))
        state["panes"] = rebuilt
        state["pane_index"] = next((i for i, row in enumerate(rebuilt)
                                    if old_target_id and row.get("target_id") == old_target_id
                                    and _pane_signature(row) == old_signature), 0)
        selected = _selected_row(state)
        state["selected_target_id"] = str((selected or {}).get("target_id") or "")
        state["selected_signature"] = _pane_signature(selected)
        return

    state["choices"] = navigation_choices(state["tower"])
    choice_index = next((i for i, item in enumerate(state["choices"])
                         if item.get("id") == old_choice_id and item.get("kind") == "workspace"), -1)
    if choice_index < 0:
        state["panes"] = [_stale_work_group_row(old_target_id, "연결이 끊긴 선택") ] if old_target_id else []
        state["pane_index"] = 0
        state["selected_target_id"] = old_target_id
        state["selected_signature"] = old_signature
        state["workspace_name"] = "연결되지 않음"
        return
    state["nav_index"] = choice_index
    _set_choice(state, choice_index)
    if old_target_id and old_signature is None:
        stale_match = next((i for i, row in enumerate(state["panes"])
                            if str(row.get("target_id") or "") == old_target_id
                            and (row.get("stale") or row.get("_live_stale"))), None)
        if stale_match is not None:
            state["pane_index"] = stale_match
            state["selected_target_id"] = old_target_id
            state["selected_signature"] = None
            _ensure_card_visible(state, *state.get("last_size", (24, 80)))
            return
    if old_target_id and old_signature:
        match = next((i for i, row in enumerate(state["panes"])
                      if str(row.get("target_id") or "") == old_target_id
                      and _pane_signature(row) == old_signature), None)
        if match is not None:
            state["pane_index"] = match
            state["selected_target_id"] = old_target_id
            state["selected_signature"] = old_signature
        else:
            stale_match = next((i for i, row in enumerate(state["panes"])
                                if str(row.get("target_id") or "") == old_target_id
                                and (row.get("stale") or row.get("_live_stale"))), None)
            if stale_match is not None:
                state["pane_index"] = stale_match
                state["selected_target_id"] = old_target_id
                state["selected_signature"] = old_signature
                _ensure_card_visible(state, *state.get("last_size", (24, 80)))
                return
            # Keep the selected identity visible as disconnected rather than
            # silently switching the user's LIVE panel to another Pane.
            stale = _stale_screen_row({
                "identity": {field: old_selected.get(field, "") for field in _PANE_IDENTITY_FIELDS},
                "window_ref": str(old_selected.get("window_ref") or ""),
                "display_name": old_selected.get("display_name") or "선택한 Pane",
            }, len(state["panes"]))
            state["panes"].append(stale)
            state["pane_index"] = len(state["panes"]) - 1
            state["selected_target_id"] = old_target_id
            state["selected_signature"] = old_signature
    _ensure_card_visible(state, *state.get("last_size", (24, 80)))


def _discard_local_preview_output(state: dict) -> None:
    """Never keep a previous LIVE frame after the read-only source fails."""

    tower = state.get("tower")
    for rows in (getattr(tower, "rows", ()) or (), state.get("panes", ()) or ()):
        for row in rows:
            if isinstance(row, dict) and row.get("pane_id"):
                row.update(stale=True, _live_stale=True, status="UNKNOWN", live_lines=[])


def _handle_key(stdscr, state: dict, key) -> bool:
    """Apply one keyboard action; return True only when preview should quit."""

    height, width = stdscr.getmaxyx()
    binding = _managed_binding(state)
    if binding is not None and binding.handle_key(key):
        return False
    demo: _EchoPty | None = state.get("demo")
    if key == curses.KEY_RESIZE:
        if demo is not None:
            regions, _wide = _regions(width, height, 2)
            rect = regions.get(2, (0, 0, width, height))
            demo.resize(max(1, rect[3] - 6), max(1, rect[2] - 2))
        _ensure_card_visible(state, height, width)
        return False

    if state.get("dialog"):
        if key in ("\x1b", "\n", "\r", curses.KEY_ENTER):
            state["dialog"] = None
        elif key == curses.KEY_MOUSE:
            try:
                _device, x, y, _z, flags = curses.getmouse()
            except (AttributeError, TypeError, ValueError, curses.error):
                return False
            if flags & getattr(curses, "BUTTON1_CLICKED", 0):
                _hit_test(state, x, y)
        return False

    if demo is not None:
        if key == "\x1d":  # Ctrl+] exits only this local POC, never a user tmux pane.
            demo.close()
            state["demo"] = None
            state["live_scroll"] = 0
            return False
        if key == curses.KEY_PPAGE:
            state["live_scroll"] += 8
        elif key == curses.KEY_NPAGE:
            state["live_scroll"] = max(0, state["live_scroll"] - 8)
        else:
            payload = _key_bytes(key)
            if payload:
                demo.send(payload)
        return False

    if key in ("q", "Q", "\x1b"):
        return True
    if key == "\t":
        state["active_panel"] = (state["active_panel"] + 1) % 3
        return False
    if key == getattr(curses, "KEY_BTAB", object()):
        state["active_panel"] = (state["active_panel"] - 1) % 3
        return False
    if key == "i" and state.get("runtime_mode") == "fixture":
        if (_selected_row(state) or {}).get("terminal_kind") == "tower_managed":
            return False
        if state["active_panel"] == 2:
            state["demo"] = _EchoPty()
            regions, _wide = _regions(width, height, 2)
            rect = regions.get(2, (0, 0, width, height))
            state["demo"].resize(max(1, rect[3] - 6), max(1, rect[2] - 2))
            state["live_scroll"] = 0
        return False
    if key == curses.KEY_MOUSE:
        try:
            _device, x, y, _z, flags = curses.getmouse()
        except (AttributeError, TypeError, ValueError, curses.error):
            return False
        if flags & getattr(curses, "BUTTON1_CLICKED", 0):
            _hit_test(state, x, y)
        return False

    if key == " ":
        if state.get("runtime_mode") == "tower":
            _open_selected_context(state)
        elif state.get("runtime_mode") == "jtt":
            handler = state.get("jtt_space_handler")
            if callable(handler):
                handler(stdscr, state)
        elif state.get("runtime_mode") == "local_read_only_preview":
            state["dialog"] = {
                "title": "읽기 전용 Preview",
                "body": "저장·입력·포커스 이동은 차단됨",
            }
        return False
    if key in ("y", "Y"):
        if state.get("runtime_mode") == "tower" and state.get("active_panel") in (1, 2):
            _copy_selected_result(state)
        elif state.get("runtime_mode") == "local_read_only_preview":
            state["dialog"] = {"title": "읽기 전용 Preview", "body": "Clipboard 변경은 차단됨"}
        return False

    if key in (curses.KEY_UP, curses.KEY_DOWN):
        delta = -1 if key == curses.KEY_UP else 1
        if state["active_panel"] == 0:
            selectable = [i for i, item in enumerate(state["choices"])
                          if item.get("kind") in ("workspace", "action")]
            if selectable:
                current = selectable.index(state["nav_index"]) if state["nav_index"] in selectable else 0
                next_choice = selectable[max(0, min(len(selectable) - 1, current + delta))]
                state["nav_index"] = next_choice
                if state["choices"][next_choice].get("kind") == "workspace":
                    _set_choice(state, next_choice)
        elif state["active_panel"] == 1:
            if state.get("center_actions_focused"):
                if delta < 0:
                    state["center_actions_focused"] = False
            elif delta > 0 and (
                not state["panes"]
                or state["pane_index"] >= max(0, len(state["panes"]) - 2)
            ):
                state["center_actions_focused"] = True
            else:
                _move_pane(state, delta * (2 if len(state["panes"]) >= 3 else 1), height, width)
        else:
            state["live_scroll"] = max(0, state["live_scroll"] + delta * 3)
        return False
    if key in (curses.KEY_LEFT, curses.KEY_RIGHT):
        delta = -1 if key == curses.KEY_LEFT else 1
        if state["active_panel"] == 1:
            if state.get("center_actions_focused"):
                state["center_action_index"] = max(0, min(1, state.get("center_action_index", 0) + delta))
            else:
                _move_pane(state, delta, height, width)
        return False
    if key in (curses.KEY_PPAGE, curses.KEY_NPAGE):
        delta = 8 if key == curses.KEY_PPAGE else -8
        if state["active_panel"] == 0:
            return False
        elif state["active_panel"] == 1:
            state["card_scroll"] = max(0, state["card_scroll"] + (1 if key == curses.KEY_PPAGE else -1))
        else:
            state["live_scroll"] = max(0, state["live_scroll"] + delta)
        return False
    if key in ("\n", "\r", curses.KEY_ENTER):
        if state["active_panel"] == 0:
            choice = state["choices"][state["nav_index"]] if 0 <= state["nav_index"] < len(state["choices"]) else {}
            if choice.get("kind") == "action" and state.get("runtime_mode") == "tower":
                _open_tower_choice(state, choice)
            else:
                _activate_choice(state, state["nav_index"])
        elif state["active_panel"] == 1:
            if state.get("center_actions_focused"):
                if state.get("runtime_mode") == "tower":
                    _activate_tower_center_action(state, state.get("center_action_index", 0))
                elif state.get("runtime_mode") == "jtt":
                    handler = state.get("jtt_center_action_handler")
                    if callable(handler):
                        handler(stdscr, state, state.get("center_action_index", 0))
                elif state.get("runtime_mode") == "local_read_only_preview":
                    state["dialog"] = {
                        "title": "읽기 전용 Preview",
                        "body": "저장·추가 변경은 차단됨",
                    }
                else:
                    title = "배치 편집" if state.get("center_action_index", 0) == 0 else "Pane 추가"
                    state["dialog"] = {"title": title, "body": state.get("workspace_name", "")}
            else:
                state["active_panel"] = 2
        elif state["active_panel"] == 2:
            if binding is not None:
                binding.enter(stdscr, state)
            elif (_selected_row(state) or {}).get("terminal_kind") == "tower_managed":
                pass
            elif state.get("runtime_mode") == "tower":
                _handoff_selected(state)
            elif state.get("runtime_mode") == "local_read_only_preview":
                state["dialog"] = {
                    "title": "읽기 전용 Preview",
                    "body": "포커스 이동은 차단됨",
                }
        return False
    return False


def _preview(stdscr) -> None:
    tower = make_preview_tower()
    state = _state(tower)
    _setup_colors()
    keypad = getattr(stdscr, "keypad", None)
    if keypad:
        keypad(True)
    stdscr.timeout(80)
    previous_mouse = _enable_mouse()
    try:
        while True:
            draw_preview_v2(stdscr, state)
            demo = state.get("demo")
            if demo is not None:
                demo.read_available()
            try:
                key = stdscr.get_wch()
            except curses.error:
                continue
            if key == -1:
                continue
            if _handle_key(stdscr, state, key):
                return
    finally:
        if state.get("demo") is not None:
            state["demo"].close()
            state["demo"] = None
        _restore_mouse(previous_mouse)
        stdscr.timeout(200)


def _last_view_layout(rows: list[dict]) -> str:
    return "focus" if len(rows) <= 1 else "split-2" if len(rows) == 2 else "main-plus-side" if len(rows) == 3 else "grid-4"


def _save_last_observed_view(state: dict) -> None:
    """Preserve the established last-view behavior without persisting output."""

    store = getattr(state.get("tower"), "control_screens", None)
    selected = _current_selected_row(state)
    rows = list(state.get("panes", ()))
    if store is None or selected is None or not 1 <= len(rows) <= 4:
        return
    if any(row.get("stale") or row.get("_live_stale") or _pane_signature(row) is None for row in rows):
        return
    if not any(_pane_signature(row) == _pane_signature(selected)
               and str(row.get("window_ref") or "") == str(selected.get("window_ref") or "")
               for row in rows):
        return
    choice = state["choices"][state["nav_index"]] if 0 <= state["nav_index"] < len(state["choices"]) else {}
    context = {
        "project_id": choice.get("project_id") if choice.get("scope") == "project" else None,
        "window_ref": str(selected.get("window_ref") or "") or None,
    }
    try:
        store.save_last_view(rows, selected, _last_view_layout(rows), context)
    except (OSError, ValueError):
        state["tower"].notice = "지난 화면을 저장하지 못했습니다."


def open_tower_control_room(stdscr, tower) -> None:
    """Open the approved renderer over Tower's existing collected snapshot."""

    from . import live_view
    from .tower import REFRESH_SECONDS

    state = _state(tower)
    state.update(runtime_mode="tower", stdscr=stdscr, demo=None)
    _setup_colors()
    keypad = getattr(stdscr, "keypad", None)
    if keypad:
        keypad(True)
    stdscr.timeout(80)
    previous_mouse = _enable_mouse()
    start_refresh = getattr(tower, "start_background_refresh", None)
    if callable(start_refresh):
        start_refresh()
    last_refresh = time.monotonic()
    try:
        while True:
            if live_view._apply_refresh(tower):
                _reconcile_room_state(state)
            now = time.monotonic()
            if now - last_refresh >= REFRESH_SECONDS:
                if callable(start_refresh):
                    start_refresh()
                last_refresh = now
            state["last_size"] = stdscr.getmaxyx()
            _ensure_card_visible(state, *state["last_size"])
            draw_preview_v2(stdscr, state)
            try:
                key = stdscr.get_wch()
            except curses.error:
                continue
            if key == -1:
                continue
            if _handle_key(stdscr, state, key):
                return
    finally:
        _restore_mouse(previous_mouse)
        stdscr.timeout(200)
        _save_last_observed_view(state)


def open_local_read_only_preview(stdscr, tower, refresh_snapshot) -> None:
    """Use the approved renderer without Tower lifecycle or write paths."""

    state = _state(tower)
    state.update(runtime_mode="local_read_only_preview", stdscr=stdscr, demo=None)
    _setup_colors()
    keypad = getattr(stdscr, "keypad", None)
    if keypad:
        keypad(True)
    stdscr.timeout(80)
    previous_mouse = _enable_mouse()
    last_refresh = time.monotonic()
    try:
        while True:
            now = time.monotonic()
            if now - last_refresh >= 4.0:
                selected = _selected_row(state)
                try:
                    refreshed = refresh_snapshot(selected)
                except Exception:
                    refreshed = None
                    _discard_local_preview_output(state)
                    state["dialog"] = {
                        "title": "읽기 전용 Preview",
                        "body": "스냅샷을 갱신하지 못했습니다.",
                    }
                if refreshed is not None:
                    state["tower"] = refreshed
                    _reconcile_room_state(state)
                last_refresh = now
            state["last_size"] = stdscr.getmaxyx()
            _ensure_card_visible(state, *state["last_size"])
            draw_preview_v2(stdscr, state)
            try:
                key = stdscr.get_wch()
            except curses.error:
                continue
            if key == -1:
                continue
            if _handle_key(stdscr, state, key):
                return
    finally:
        _restore_mouse(previous_mouse)
        stdscr.timeout(200)


def _source_head() -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(_SOURCE_ROOT), "rev-parse", "HEAD"],
            check=False, capture_output=True, text=True, timeout=3,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def main() -> int:
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print("Tower UX Preview V2는 대화형 터미널에서 실행해야 합니다.", file=sys.stderr)
        return 2
    head = _source_head()
    if head != EXPECTED_BASE_SHA:
        print(f"Preview source mismatch: expected {EXPECTED_BASE_SHA}, found {head or 'unavailable'}.", file=sys.stderr)
        return 2
    try:
        locale.setlocale(locale.LC_ALL, "")
    except locale.Error:
        pass
    os.environ.setdefault("ESCDELAY", "25")
    curses.wrapper(_preview)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
