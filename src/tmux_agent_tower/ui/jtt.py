"""JTT's tmux-independent multi-terminal UI over the approved renderer."""

from __future__ import annotations

import curses
import locale
import os
import time
from types import SimpleNamespace

from ..state.jtt_workspaces import JTTWorkspaceStore, WorkspaceStoreError
from ..terminal import TerminalRegistry
from . import ux_preview_v2 as renderer
from .render import display_width, truncate_to_width
from .widgets import prompt_text, run_list_picker, safe_add, show_message_screen


class ManagedTerminalBinding:
    """Adapts one exact JTT session to the RIGHT panel and input lease."""

    def __init__(self, client, identity):
        self.client = client
        self.identity = identity
        self.session_id = str(identity.session_id)
        self.generation = str(identity.generation)
        self.shell_pid = int(identity.shell_pid)
        self.input_mode = False
        self.synced = False
        self.closed = False
        self.screen = None
        self.status = "연결 중"
        self.shell_state = str(getattr(identity, "state", "unknown")).lower()
        self.renderer_state = str(getattr(identity, "renderer_state", "unknown"))
        self.terminal_view_state = "starting"
        self.last_resize: tuple[int, int] | None = None

    def matches(self, row: dict) -> bool:
        identity = row.get("managed_identity") or {}
        return (
            not self.closed
            and str(row.get("managed_id") or "") == self.session_id
            and str(identity.get("session_id") or "") == self.session_id
            and str(identity.get("generation") or "") == self.generation
            and int(identity.get("shell_pid") or -1) == self.shell_pid
            and str(getattr(self.client.identity, "session_id", "")) == self.session_id
            and str(getattr(self.client.identity, "generation", "")) == self.generation
            and int(getattr(self.client.identity, "shell_pid", -1)) == self.shell_pid
        )

    def refresh(self) -> None:
        if self.closed:
            self._stale("세션 연결 종료")
            return
        try:
            update = self.client.poll()
        except (OSError, RuntimeError, ValueError):
            self._stale("Supervisor 연결 실패 · 입력 차단")
            return
        renderer_ready = bool(getattr(update, "renderer_available", True))
        self.renderer_state = str(getattr(update, "renderer_state", "ready" if renderer_ready else "dependency_missing"))
        self.synced = (bool(getattr(update, "synced", False))
                       and not bool(getattr(update, "gap", False)) and renderer_ready)
        lease_lost = self.input_mode and not bool(getattr(self.client, "lease", True))
        if lease_lost:
            self.input_mode = False
        self.screen = getattr(update, "screen", None)
        self.shell_state = str(getattr(update, "state", "unknown")).lower()
        if self.shell_state not in {"running", "active"}:
            self.synced = False
            self._revoke()
            if self.shell_state in {"exited", "dead", "stopped"}:
                self.terminal_view_state = "exited"
                self.status = "셸 종료"
            else:
                self.terminal_view_state = "session_unavailable"
                self.status = "셸 상태 확인 불가 · 입력 차단"
        elif not renderer_ready:
            self._revoke()
            self.terminal_view_state = (
                "renderer_missing" if self.renderer_state == "dependency_missing"
                else "renderer_error"
            )
            self.status = ("셸 실행 중 · VT 렌더러 없음 · 입력 차단"
                           if self.terminal_view_state == "renderer_missing"
                           else "셸 실행 중 · VT 렌더러 오류 · 입력 차단")
        elif not self.synced:
            self._revoke()
            self.terminal_view_state = "resyncing"
            self.status = "화면 재동기화 중 · 입력 차단"
        elif lease_lost:
            self.terminal_view_state = "running_ready"
            self.status = "입력 권한 만료 · 입력 차단"
        elif self.input_mode:
            self.terminal_view_state = "running_ready"
            self.status = "입력 중 · Ctrl+] 탐색 복귀"
        else:
            self.terminal_view_state = "running_ready"
            self.status = "관찰 중 · 클릭 또는 Enter로 입력"

    def _stale(self, message: str, view_state: str = "supervisor_connection_failed") -> None:
        self.synced = False
        self.screen = None
        self.status = message
        self.terminal_view_state = view_state
        self._revoke()

    def _revoke(self) -> None:
        self.input_mode = False
        try:
            self.client.release()
        except (OSError, RuntimeError, ValueError):
            pass

    def release(self) -> None:
        self._revoke()

    def enter(self, _stdscr, state: dict) -> bool:
        row = renderer._selected_row(state)
        if (not row or not self.matches(row) or not self.synced or self.screen is None
                or self.renderer_state != "ready"):
            self.status = "세션 신원 또는 화면 동기화 확인 실패 · 입력 차단"
            self.terminal_view_state = "resyncing"
            self.release()
            return False
        try:
            acquired = self.client.acquire()
        except (OSError, RuntimeError, ValueError):
            acquired = False
        if acquired:
            self.input_mode = True
            self.status = "입력 중 · Ctrl+] 탐색 복귀"
            return True
        self.input_mode = False
        self.status = "다른 연결이 입력 중이거나 동기화가 불확실함 · 입력 차단"
        return False

    def send_literal_ctrl_g(self, state: dict) -> bool:
        """Send one Ctrl+G only to this exact, synchronized leased terminal."""

        row = renderer._selected_row(state)
        if (not self.input_mode or not bool(getattr(self.client, "lease", False))
                or not self.synced or self.screen is None
                or self.renderer_state != "ready" or not row or not self.matches(row)):
            self.release()
            self.status = "Ctrl+G 전송 조건 불일치 · 입력 차단"
            return False
        try:
            result = self.client.send(b"\x07")
        except (OSError, RuntimeError, ValueError):
            self._stale("Ctrl+G 입력 전달 실패 · 입력 차단")
            return False
        if result != 1:
            self._stale("Ctrl+G 입력 전달 확인 실패 · 입력 차단")
            return False
        return True

    def handle_key(self, key) -> bool:
        if not self.input_mode:
            return False
        # get_wch() normally returns the control byte as a one-character
        # string, but curses implementations may return its integer code.
        if key in ("\x1d", 29):  # Ctrl+] is reserved by JTT as a mode escape.
            self.release()
            self.status = "관찰 중 · 클릭 또는 Enter로 입력"
            return True
        if key == curses.KEY_RESIZE:
            return False
        payload = renderer._key_bytes(key)
        if payload is None:
            return True
        try:
            result = self.client.send(payload) if self.synced else False
            if result is False or result == 0:
                self._stale("입력 전달 확인 실패 · 입력 차단")
        except (OSError, RuntimeError, ValueError):
            self._stale("입력 연결 실패 · 입력 차단")
        return True

    def resize(self, columns: int, lines: int) -> None:
        size = (max(1, int(columns)), max(1, int(lines)))
        if self.closed or size == self.last_resize:
            return
        try:
            self.client.resize(*size)
            self.last_resize = size
        except (OSError, RuntimeError, ValueError):
            self._stale("크기 변경 실패 · 입력 차단")

    def draw(self, stdscr, rect, _active: bool) -> None:
        x, y, width, height = rect
        screen = self.screen
        if not self.synced or screen is None:
            label = self.status or "화면 동기화 중 · 입력 차단"
            safe_add(stdscr, y, x, truncate_to_width(label, width))
            return
        rows = getattr(screen, "rows", ())
        visible_height = max(0, height - 1)
        for row_index, row in enumerate(rows[:visible_height]):
            self._draw_row(stdscr, x, y + row_index, width, row)
        status_attr = curses.A_BOLD if self.input_mode else curses.A_DIM
        safe_add(stdscr, y + height - 1, x, truncate_to_width(self.status, width), status_attr)
        cursor_y = int(getattr(screen, "cursor_y", -1))
        cursor_x = int(getattr(screen, "cursor_x", -1))
        if (self.input_mode and getattr(screen, "cursor_visible", False)
                and 0 <= cursor_y < visible_height and 0 <= cursor_x < width):
            try:
                curses.curs_set(1)
                stdscr.move(y + cursor_y, x + cursor_x)
            except curses.error:
                pass
        else:
            try:
                curses.curs_set(0)
            except curses.error:
                pass

    @staticmethod
    def _draw_row(stdscr, x: int, y: int, width: int, cells) -> None:
        cursor_x = 0
        run_text = []
        run_style = None

        def flush():
            nonlocal run_text, run_style
            if run_text:
                text = "".join(run_text)
                safe_add(stdscr, y, x + cursor_x - display_width(text), text,
                         _cell_attr(run_style))
            run_text = []
            run_style = None

        for cell in cells:
            text = str(getattr(cell, "text", " ") or "")
            style = (getattr(cell, "foreground", getattr(cell, "fg", "default")),
                     getattr(cell, "background", getattr(cell, "bg", "default")),
                     bool(getattr(cell, "bold", False)),
                     bool(getattr(cell, "italic", getattr(cell, "italics", False))),
                     bool(getattr(cell, "underline", False)), bool(getattr(cell, "reverse", False)),
                     bool(getattr(cell, "blink", False)), bool(getattr(cell, "strikethrough", False)))
            cell_width = display_width(text) if text else 0
            if run_style is not None and style != run_style:
                flush()
            if run_style is None:
                run_style = style
            if text:
                run_text.append(text)
            cursor_x += cell_width
            if cursor_x >= width:
                break
        flush()

    def close(self) -> None:
        if not self.closed:
            self._revoke()
            try:
                self.client.close()
            except (OSError, RuntimeError, ValueError):
                pass
            self.closed = True


def _cell_attr(style) -> int:
    if not style:
        return curses.A_NORMAL
    fg, bg, bold, italics, underline, reverse, blink, strike = style
    attr = curses.A_NORMAL
    if bold:
        attr |= curses.A_BOLD
    if italics and hasattr(curses, "A_ITALIC"):
        attr |= curses.A_ITALIC
    if underline:
        attr |= curses.A_UNDERLINE
    if reverse:
        attr |= curses.A_REVERSE
    if blink and hasattr(curses, "A_BLINK"):
        attr |= curses.A_BLINK
    if strike and hasattr(curses, "A_STRIKEOUT"):
        attr |= curses.A_STRIKEOUT
    fg_index, fg_bright = _basic_color_index(fg)
    bg_index, _bg_bright = _basic_color_index(bg)
    if fg_bright:
        attr |= curses.A_BOLD
    if fg_index is not None or bg_index is not None:
        fg_slot = fg_index if fg_index is not None else 7
        bg_slot = bg_index if bg_index is not None else 7
        pair = 8 + fg_slot * 8 + bg_slot
        try:
            if pair < curses.COLOR_PAIRS:
                curses.init_pair(pair, fg_index if fg_index is not None else -1,
                                 bg_index if bg_index is not None else -1)
                attr |= curses.color_pair(pair)
        except curses.error:
            pass
    return attr


def _basic_color_index(value) -> int | None:
    if isinstance(value, int):
        return (value, False) if 0 <= value <= 7 else (None, False)
    names = {"black": 0, "red": 1, "green": 2, "brown": 3,
             "yellow": 3, "blue": 4, "magenta": 5, "cyan": 6, "white": 7}
    name = str(value or "default").lower()
    bright = name.startswith("bright")
    if bright:
        name = name[6:]
    return names.get(name), bright


def _session_rows(registry: TerminalRegistry, workspace: dict,
                 clients: dict[str, ManagedTerminalBinding], *, attach: bool = True) -> list[dict]:
    lookup_failed = False
    try:
        identities = {str(item.session_id): item for item in registry.list_sessions()}
    except (OSError, RuntimeError, ValueError):
        identities = {}
        lookup_failed = True
    rows = []
    for session_id in workspace.get("terminal_ids", ()):
        identity = identities.get(str(session_id))
        if identity is None:
            rows.append({
                "kind": "pane", "key": str(session_id), "target_id": str(session_id),
                "display_name": "터미널 상태 확인 불가", "agent": "Shell", "status": "UNKNOWN",
                "stale": True, "_live_stale": True, "live_lines": [],
                "terminal_kind": "tower_managed", "managed_id": str(session_id),
                "managed_identity": {"session_id": str(session_id)},
                "terminal_shell_state": "unknown", "terminal_renderer_state": "unknown",
                "terminal_view_state": "supervisor_connection_failed" if lookup_failed else "session_unavailable",
                "terminal_preview": "세션 조회 불가",
            })
            continue
        binding = clients.get(str(session_id))
        if attach and (binding is None or not binding.matches({
            "managed_id": str(session_id),
            "managed_identity": {"session_id": str(identity.session_id),
                                 "generation": str(identity.generation),
                                 "shell_pid": int(identity.shell_pid)},
        })):
            try:
                client = registry.connect(str(session_id))
                binding = ManagedTerminalBinding(client, client.identity)
                clients[str(session_id)] = binding
            except (OSError, RuntimeError, ValueError):
                binding = None
        if not attach:
            binding = None
        shell_state = str(getattr(identity, "state", "unknown")).lower()
        renderer_state = str(getattr(identity, "renderer_state", "unknown"))
        if shell_state in {"exited", "dead", "stopped"}:
            view_state = "exited"
        elif not attach:
            view_state = "starting" if shell_state == "running" else "session_unavailable"
        elif binding is None:
            view_state = "supervisor_connection_failed"
        elif renderer_state == "dependency_missing":
            view_state = "renderer_missing"
        elif renderer_state not in {"ready", "unknown"}:
            view_state = "renderer_error"
        elif shell_state == "running":
            view_state = "resyncing"
        else:
            view_state = "session_unavailable"
        row = {
            "kind": "pane", "key": str(session_id), "target_id": str(session_id),
            "display_name": str(getattr(identity, "name", "") or "터미널"),
            "agent": "Shell", "status": "WORKING" if shell_state == "running" else "DEAD",
            "stale": shell_state != "running", "_live_stale": binding is None,
            "live_lines": [], "terminal_kind": "tower_managed", "managed_id": str(session_id),
            "managed_identity": {"session_id": str(identity.session_id),
                                 "generation": str(identity.generation),
                                 "shell_pid": int(identity.shell_pid)},
            "pane_id": str(session_id), "pane_pid": str(identity.shell_pid),
            "terminal_shell_state": shell_state,
            "terminal_renderer_state": renderer_state,
            "terminal_view_state": view_state,
            "terminal_preview": "터미널 화면 미리보기 없음",
        }
        if binding is not None and binding.synced and binding.screen is not None:
            row["terminal_preview"] = _screen_preview(binding.screen) or "터미널 화면이 비어 있음"
        rows.append(row)
    return rows


def _screen_preview(screen) -> str:
    for row in getattr(screen, "rows", ()):
        text = "".join(str(getattr(cell, "text", "") or "") for cell in row).strip()
        if text:
            return text
    return ""


def build_state(store: JTTWorkspaceStore, registry: TerminalRegistry,
                clients: dict[str, ManagedTerminalBinding] | None = None,
                *, attach_default: bool = True) -> dict:
    clients = clients if clients is not None else {}
    workspaces = store.workspaces()
    default = next((item for item in workspaces if item["id"] == store.DEFAULT_ID), workspaces[0])
    saved = [item for item in workspaces if item["id"] != store.DEFAULT_ID]
    choices = [{"kind": "workspace", "group": "windows", "scope": "jtt_workspace",
                "id": default["id"], "label": default["name"], "terminal_ids": default["terminal_ids"]}]
    choices.extend({"kind": "workspace", "group": "saved", "scope": "jtt_workspace",
                    "id": item["id"], "label": item["name"], "terminal_ids": item["terminal_ids"]}
                   for item in saved)
    choices.extend([
        {"kind": "action", "group": "actions", "id": "action:save", "label": "구성 저장",
         "dialog": "구성 저장"},
        {"kind": "action", "group": "actions", "id": "action:settings", "label": "설정 · 메뉴",
         "dialog": "설정 · 메뉴"},
    ])
    selected = default
    rows_by_choice = {
        item["id"]: _session_rows(registry, item, clients, attach=False)
        for item in [default, *saved]
    }
    rows = _session_rows(registry, default, clients, attach=attach_default)
    rows_by_choice[default["id"]] = rows
    tower = SimpleNamespace(rows=[], visible_rows=[], selected=-1)
    panes = list(rows)
    selected_row = panes[0] if panes else None
    state = {
        "tower": tower,
        "choices": choices,
        "nav_index": 0,
        "workspace_id": selected["id"],
        "workspace_name": selected["name"],
        "window_refs": (),
        "panes": panes,
        "pane_index": 0,
        "selected_target_id": str((selected_row or {}).get("target_id") or ""),
        "selected_signature": renderer._pane_signature(selected_row),
        "card_scroll": 0,
        "live_scroll": 0,
        "active_panel": 0,
        "center_actions_focused": False,
        "center_action_index": 0,
        "choice_scrolls": {},
        "hit_boxes": [],
        "demo": None,
        "dialog": None,
        "runtime_mode": "jtt",
        "custom_view": False,
        "last_size": (24, 80),
        "managed_cards": rows_by_choice,
        "managed_terminals": clients,
        "jtt_store": store,
        "jtt_registry": registry,
        "jtt_workspaces": workspaces,
        "jtt_notice": "",
        "jtt_notice_until": 0.0,
        "jtt_prefix_pending": False,
        "jtt_prefix_deadline": 0.0,
        "jtt_pending_auto_acquire": None,
        "jtt_action_handler": _activate_jtt_action,
        "jtt_center_action_handler": lambda stdscr, state, index: (
            _save_configuration(stdscr, state) if index == 0 else _create_terminal(stdscr, state)
        ),
        "jtt_space_handler": _settings_menu,
        "jtt_workspace_handler": _attach_choice,
        "jtt_prefix_handler": _handle_prefix_command,
    }
    return state


def _attach_choice(state: dict, choice: dict) -> None:
    """Attach only the selected workspace's exact sessions to this UI client."""
    workspace_id = str(choice.get("id") or "")
    workspaces = state["jtt_store"].workspaces()
    workspace = next((item for item in workspaces if item["id"] == workspace_id), None)
    if workspace is None:
        state["panes"] = []
        state["pane_index"] = 0
        state["selected_target_id"] = ""
        state["selected_signature"] = None
        return

    expected_ids = {str(item) for item in workspace.get("terminal_ids", ())}
    clients = state["managed_terminals"]
    for session_id in tuple(clients):
        if session_id not in expected_ids:
            clients.pop(session_id).close()

    rows = _session_rows(state["jtt_registry"], workspace, clients, attach=True)
    state["managed_cards"][workspace_id] = rows
    state["jtt_workspaces"] = workspaces
    state["workspace_id"] = workspace_id
    state["workspace_name"] = workspace["name"]
    state["panes"] = [dict(row) for row in rows]
    state["pane_index"] = 0
    selected = state["panes"][0] if state["panes"] else None
    state["selected_target_id"] = str((selected or {}).get("target_id") or "")
    state["selected_signature"] = renderer._pane_signature(selected)
    state["card_scroll"] = 0
    state["live_scroll"] = 0


def _refresh_workspaces(state: dict, selected_id: str | None = None) -> None:
    stdscr = state.get("stdscr")
    refreshed = build_state(state["jtt_store"], state["jtt_registry"],
                            state["managed_terminals"], attach_default=False)
    refreshed["stdscr"] = stdscr
    index = next((i for i, choice in enumerate(refreshed["choices"])
                 if choice.get("id") == selected_id), None) if selected_id else None
    if index is None:
        index = next((i for i, choice in enumerate(refreshed["choices"])
                      if choice.get("kind") == "workspace"), None)
    if index is not None:
        renderer._set_choice(refreshed, index)
        refreshed["active_panel"] = 1
    state.clear()
    state.update(refreshed)


def _save_configuration(stdscr, state: dict) -> None:
    name = prompt_text(stdscr, "구성 이름: ")
    if not name:
        return
    choice = state["choices"][state["nav_index"]] if state["choices"] else {}
    try:
        saved = state["jtt_store"].save_configuration(
            "", name, [str(row.get("managed_id") or "") for row in state["panes"]
                       if row.get("terminal_kind") == "tower_managed" and not row.get("stale")],
        )
        state["jtt_notice"] = f"{saved['name']} 구성을 저장했습니다."
        _refresh_workspaces(state, saved["id"])
    except WorkspaceStoreError:
        state["jtt_notice"] = "구성을 안전하게 저장하지 못했습니다."


def _settings_menu(stdscr, state: dict) -> None:
    result = run_list_picker(
        stdscr, "설정 · 메뉴", [("new", "새 작업공간"), ("help", "키 안내"),
                                ("stop", "선택한 터미널 종료")],
        centered=True, footer_hint="↑↓ 이동 · Enter 선택 · Esc 취소",
    )
    if result.cancelled or result.selected_key is None:
        return
    if result.selected_key == "new":
        name = prompt_text(stdscr, "새 작업공간 이름: ")
        if not name:
            return
        try:
            item = state["jtt_store"].create_workspace(name)
            _refresh_workspaces(state, item["id"])
            state["jtt_notice"] = "새 빈 작업공간을 만들었습니다."
        except WorkspaceStoreError:
            state["jtt_notice"] = "작업공간을 안전하게 저장하지 못했습니다."
    elif result.selected_key == "stop":
        row = renderer._selected_row(state)
        if not row or row.get("stale"):
            state["jtt_notice"] = "종료할 연결된 터미널이 없습니다."
            return
        confirm = run_list_picker(
            stdscr, "터미널 종료", [("stop", f"{row.get('display_name')} 종료"), ("cancel", "취소")],
            centered=True, footer_hint="종료는 해당 셸 프로세스를 끝냅니다.",
        )
        if not confirm.cancelled and confirm.selected_key == "stop":
            session_id = str(row.get("managed_id") or "")
            try:
                state["jtt_registry"].stop(session_id)
                workspace = next(item for item in state["jtt_store"].workspaces()
                                 if item["id"] == state["workspace_id"])
                state["jtt_store"].set_terminal_ids(
                    state["workspace_id"],
                    [item for item in workspace["terminal_ids"] if item != session_id],
                )
                binding = state["managed_terminals"].pop(session_id, None)
                if binding:
                    binding.close()
                _refresh_workspaces(state, state["workspace_id"])
                state["jtt_notice"] = "선택한 터미널을 종료했습니다."
            except (OSError, RuntimeError, ValueError, WorkspaceStoreError):
                state["jtt_notice"] = "선택한 터미널을 종료하지 못했습니다."
    else:
        show_message_screen(
            stdscr, "JTT 키 안내",
            ["탐색: Tab/Shift+Tab 패널 · Enter 입력 · Q 연결 해제",
             "Ctrl+G n: 새 터미널 · h/l: 카드 이동 · w: 작업공간",
             "Ctrl+G d: UI 연결 해제 (셸 유지) · ?: 도움",
             "Ctrl+G Esc: 취소 · Ctrl+G 두 번: 입력 중 Ctrl+G 전달",
             "입력 중 bare n/q/Tab은 터미널로 전달됩니다.",
             "Ctrl+]는 입력 모드에서 탐색 복귀입니다."],
        )


def _create_terminal(stdscr, state: dict) -> None:
    renderer._release_managed_input(state)
    state["jtt_pending_auto_acquire"] = None
    session_id = ""
    client = None
    try:
        client = state["jtt_registry"].create()
        session_id = str(client.identity.session_id)
        state["managed_terminals"][session_id] = ManagedTerminalBinding(client, client.identity)
        workspace_id = state["workspace_id"]
        workspace = next(item for item in state["jtt_store"].workspaces() if item["id"] == workspace_id)
        state["jtt_store"].set_terminal_ids(workspace_id, [*workspace["terminal_ids"], session_id])
        _refresh_workspaces(state, workspace_id)
        state["pane_index"] = max(0, len(state["panes"]) - 1)
        state["active_panel"] = 2
        state["center_actions_focused"] = False
        state["selected_target_id"] = str(state["panes"][state["pane_index"]].get("target_id") or "")
        state["selected_signature"] = renderer._pane_signature(state["panes"][state["pane_index"]])
        state["jtt_pending_auto_acquire"] = {
            "session_id": session_id,
            "deadline": time.monotonic() + 2.5,
        }
        state["jtt_notice"] = "새 로컬 셸 생성 · 화면과 입력 준비 확인 중"
        state["jtt_notice_until"] = 0.0
    except (OSError, RuntimeError, ValueError, WorkspaceStoreError) as exc:
        if session_id:
            try:
                state["jtt_registry"].stop(session_id)
            except (OSError, RuntimeError, ValueError):
                pass
        if client is not None:
            try:
                client.close()
            except (OSError, RuntimeError, ValueError):
                pass
        state["managed_terminals"].pop(session_id, None)
        detail = str(exc).strip()
        state["jtt_notice"] = (detail[:120] if detail else
                                "새 터미널을 만들지 못했습니다. 기존 작업은 변경되지 않았습니다.")
        state["jtt_notice_until"] = 0.0


def _handle_prefix_command(stdscr, state: dict, command: str) -> bool:
    if command == "n":
        _create_terminal(stdscr, state)
        return False
    if command in {"h", "l"}:
        renderer._release_managed_input(state)
        state["jtt_pending_auto_acquire"] = None
        if state["panes"]:
            delta = -1 if command == "h" else 1
            state["pane_index"] = (state["pane_index"] + delta) % len(state["panes"])
            row = renderer._selected_row(state)
            state["selected_target_id"] = str((row or {}).get("target_id") or "")
            state["selected_signature"] = renderer._pane_signature(row)
            state["active_panel"] = 1
            state["center_actions_focused"] = False
            renderer._ensure_card_visible(state, *state.get("last_size", (24, 80)))
            state["jtt_notice"] = "카드 이동 · 입력 모드 해제"
        else:
            state["jtt_notice"] = "이동할 터미널 카드가 없습니다."
        state["jtt_notice_until"] = 0.0
        return False
    if command == "w":
        renderer._release_managed_input(state)
        state["jtt_pending_auto_acquire"] = None
        state["active_panel"] = 0
        state["center_actions_focused"] = False
        state["jtt_notice"] = "작업공간 탐색"
        state["jtt_notice_until"] = 0.0
        return False
    if command == "d":
        renderer._release_managed_input(state)
        state["jtt_pending_auto_acquire"] = None
        renderer._clear_jtt_prefix(state)
        return True
    if command == "?":
        show_message_screen(
            stdscr, "JTT 키 안내",
            ["Ctrl+G n: 새 터미널 (명시적 생성)",
             "Ctrl+G h/l: 이전/다음 카드 (입력 권한 해제)",
             "Ctrl+G w: 작업공간 패널", "Ctrl+G d: UI 연결 해제 (셸 유지)",
             "Ctrl+G ?: 이 도움말", "Ctrl+G Esc: 접두 취소 · Ctrl+G 두 번: 입력 중 Ctrl+G 전송",
             "입력 모드에서는 bare n/q/Tab을 터미널로 전달합니다.",
             "Ctrl+]는 입력 모드에서 탐색 복귀입니다."],
        )
        return False
    return False


def _activate_jtt_action(stdscr, state: dict, choice: dict) -> None:
    if choice.get("id") == "action:save":
        _save_configuration(stdscr, state)
    elif choice.get("id") == "action:settings":
        _settings_menu(stdscr, state)


def _refresh_and_resize(state: dict, stdscr) -> None:
    height, width = stdscr.getmaxyx()
    state["last_size"] = (height, width)
    renderer._ensure_card_visible(state, height, width)
    binding = renderer._managed_binding(state)
    row = renderer._selected_row(state)
    if binding is not None:
        binding.refresh()
        row = renderer._selected_row(state)
        if row is not None:
            row.update(
                status="DEAD" if binding.terminal_view_state == "exited" else "WORKING",
                stale=binding.terminal_view_state == "exited",
                _live_stale=not binding.synced,
                terminal_shell_state=binding.shell_state,
                terminal_renderer_state=binding.renderer_state,
                terminal_view_state=binding.terminal_view_state,
                terminal_preview=(_screen_preview(binding.screen)
                                  if binding.synced and binding.screen is not None
                                  else "터미널 화면 미리보기 없음"),
                live_lines=[],
            )
    _resolve_pending_auto_acquire(state, binding, row)
    if binding is None:
        return
    regions, wide = renderer._regions(width, height, 2)
    rect = regions.get(2, (0, 0, width, height))
    inner_width = max(1, rect[2] - 2)
    inner_height = max(1, rect[3] - 3)
    if binding.input_mode:
        binding.resize(inner_width, inner_height)


def _resolve_pending_auto_acquire(state: dict, binding, row) -> None:
    pending = state.get("jtt_pending_auto_acquire")
    if not pending:
        return
    if str((row or {}).get("managed_id") or "") != pending["session_id"]:
        state["jtt_pending_auto_acquire"] = None
        return
    if binding is not None and binding.matches(row):
        if (binding.terminal_view_state == "running_ready" and binding.synced
                and binding.renderer_state == "ready"):
            # Clear before attempting so refresh can never retry a failed lease.
            state["jtt_pending_auto_acquire"] = None
            if binding.enter(state.get("stdscr"), state):
                state["jtt_notice"] = "터미널 준비 완료 · 입력 중 · Ctrl+] 탐색 복귀"
            else:
                state["jtt_notice"] = binding.status
            state["jtt_notice_until"] = 0.0
            return
        if binding.terminal_view_state in {
            "renderer_missing", "renderer_error", "exited",
            "supervisor_connection_failed", "session_unavailable",
        }:
            state["jtt_pending_auto_acquire"] = None
            state["jtt_notice"] = binding.status
            state["jtt_notice_until"] = 0.0
            return
    if time.monotonic() >= float(pending["deadline"]):
        state["jtt_pending_auto_acquire"] = None
        state["jtt_notice"] = (binding.status if binding is not None
                                else "Supervisor 연결 실패 · 자동 입력을 시작하지 않았습니다.")
        state["jtt_notice_until"] = 0.0


def _run(stdscr, store: JTTWorkspaceStore, registry: TerminalRegistry) -> None:
    clients: dict[str, ManagedTerminalBinding] = {}
    state = build_state(store, registry, clients)
    state["stdscr"] = stdscr
    renderer._setup_colors()
    keypad = getattr(stdscr, "keypad", None)
    if keypad:
        keypad(True)
    stdscr.timeout(80)
    previous_mouse = renderer._enable_mouse()
    try:
        while True:
            _refresh_and_resize(state, stdscr)
            renderer.draw_preview_v2(stdscr, state)
            try:
                key = stdscr.get_wch()
            except curses.error:
                continue
            if key in (-1, None):
                continue
            if renderer._handle_key(stdscr, state, key):
                return
    finally:
        renderer._clear_jtt_prefix(state)
        state["jtt_pending_auto_acquire"] = None
        renderer._release_managed_input(state)
        for binding in list(clients.values()):
            # close() disconnects the UI client only; the PTY shell remains alive.
            binding.close()
        state["managed_terminals"].clear()
        renderer._restore_mouse(previous_mouse)
        stdscr.timeout(200)


def run_jtt_ui(store: JTTWorkspaceStore | None = None,
               registry: TerminalRegistry | None = None) -> int:
    store = store or JTTWorkspaceStore()
    registry = registry or TerminalRegistry(root=store.root / "terminals")
    locale.setlocale(locale.LC_ALL, "")
    os.environ.setdefault("ESCDELAY", "25")
    curses.wrapper(_run, store, registry)
    return 0
