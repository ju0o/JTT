"""JTT's tmux-independent multi-terminal UI over the approved renderer."""

from __future__ import annotations

import curses
import locale
import os
import signal
import sys
import time
from contextlib import contextmanager
from types import SimpleNamespace

from ..state.jtt_keys import (
    DEFAULT_JTT_KEYBINDINGS,
    JTTKeybindings,
    JTTKeybindingsError,
    load_jtt_keybindings,
)
from ..state.jtt_work_kits import JTTWorkKitStore, WorkKitStoreError
from ..state.jtt_workspaces import JTTWorkspaceStore, WorkspaceStoreError
from ..terminal import TerminalRegistry
from . import ux_preview_v2 as renderer
from .render import display_width, truncate_to_width
from .widgets import (
    is_backspace,
    is_enter,
    is_escape,
    prompt_text,
    read_key,
    run_list_picker,
    safe_add,
    show_message_screen,
)


class _JTTTerminationSignal(Exception):
    """Catchable process signal translated into ordinary curses unwinding."""

    def __init__(self, signum: int):
        super().__init__(signum)
        self.signum = signum


@contextmanager
def _catch_jtt_termination_signals():
    """Raise through `_run` so its finally and curses.wrapper restore the TTY."""

    previous = {}

    def unwind(signum, _frame):
        raise _JTTTerminationSignal(signum)

    candidates = (
        getattr(signal, "SIGHUP", None),
        getattr(signal, "SIGTERM", None),
        getattr(signal, "SIGQUIT", None),
    )
    try:
        for signum in candidates:
            if signum is not None:
                previous[signum] = signal.getsignal(signum)
                signal.signal(signum, unwind)
        yield
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def _matches_configured_byte(key, byte: int) -> bool:
    if isinstance(key, str):
        return len(key) == 1 and ord(key) == byte
    return isinstance(key, int) and not isinstance(key, bool) and key == byte


class ManagedTerminalBinding:
    """Adapts one exact JTT session to the RIGHT panel and input lease."""

    def __init__(self, client, identity, *, keybindings=DEFAULT_JTT_KEYBINDINGS):
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
        self.input_escape_byte = keybindings.input_escape_byte
        self.input_escape_name = keybindings.input_escape
        self._input_state = None

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
            # A lease can be revoked remotely while curses is still in the
            # pass-through termios mode. Restore local terminal controls at
            # the same time the UI drops its input-mode state.
            self._revoke()
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
            self.status = f"입력 중 · {self.input_escape_name} 탐색 복귀"
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
        if not self._restore_input_terminal_mode() and self._input_state is not None:
            self._input_state["jtt_notice"] = "터미널 입력 모드 복구 실패 · UI를 안전 종료합니다"
            self._input_state["jtt_notice_until"] = 0.0
            self._input_state["jtt_terminal_mode_fatal"] = True
        try:
            self.client.release()
        except (OSError, RuntimeError, ValueError):
            pass

    def _restore_input_terminal_mode(self) -> bool:
        input_state = self._input_state
        if input_state is None:
            return True
        if not renderer._set_jtt_control_byte_input(input_state, False):
            return False
        self._input_state = None
        return True

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
            if not renderer._set_jtt_control_byte_input(state, True):
                # Retry the saved mode snapshot once. If it still cannot be
                # restored, do not keep running curses against an unknown TTY
                # mode; _run will exit through curses.wrapper's final guard.
                restored = renderer._set_jtt_control_byte_input(state, False)
                try:
                    self.client.release()
                except (OSError, RuntimeError, ValueError):
                    pass
                self.input_mode = False
                self.status = "터미널 제어 키 모드를 설정할 수 없음 · 입력 차단"
                if not restored:
                    state["jtt_notice"] = "터미널 모드 복구 실패 · UI를 안전 종료합니다"
                    state["jtt_notice_until"] = 0.0
                    state["jtt_terminal_mode_fatal"] = True
                return False
            self._input_state = state
            self.input_mode = True
            self.status = f"입력 중 · {self.input_escape_name} 탐색 복귀"
            return True
        self.input_mode = False
        self.status = "다른 연결이 입력 중이거나 동기화가 불확실함 · 입력 차단"
        return False

    def send_literal_byte(self, state: dict, byte: int, expected_target=None) -> bool:
        """Send one configured control byte only to the unchanged leased target."""

        row = renderer._selected_row(state)
        if (not self.input_mode or not bool(getattr(self.client, "lease", False))
                or not self.synced or self.screen is None
                or self.renderer_state != "ready" or not row or not self.matches(row)
                or expected_target is None
                or renderer._jtt_prefix_target_snapshot(state) != expected_target
                or not isinstance(byte, int) or isinstance(byte, bool) or not 0 <= byte <= 0x1F):
            self.release()
            self.status = "제어 키 전송 조건 불일치 · 입력 차단"
            return False
        try:
            result = self.client.send(bytes((byte,)))
        except (OSError, RuntimeError, ValueError):
            self._stale("제어 키 입력 전달 실패 · 입력 차단")
            return False
        if result != 1:
            self._stale("제어 키 입력 전달 확인 실패 · 입력 차단")
            return False
        return True

    def handle_key(self, key) -> bool:
        if not self.input_mode:
            return False
        # get_wch() normally returns the control byte as a one-character
        # string, but curses implementations may return its integer code.
        if _matches_configured_byte(key, self.input_escape_byte):
            self.release()
            self.status = "관찰 중 · 클릭 또는 Enter로 입력"
            return True
        if key == curses.KEY_RESIZE:
            return False
        payload = renderer._jtt_key_bytes(key)
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
                 clients: dict[str, ManagedTerminalBinding], *, attach: bool = True,
                 keybindings=DEFAULT_JTT_KEYBINDINGS) -> list[dict]:
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
                binding = ManagedTerminalBinding(client, client.identity, keybindings=keybindings)
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
                *, attach_default: bool = True,
                keybindings: JTTKeybindings | None = None) -> dict:
    clients = clients if clients is not None else {}
    keybindings = keybindings or load_jtt_keybindings(store.root)
    workspaces = store.workspaces()
    work_kit_store = JTTWorkKitStore(store.root)
    work_kit_error = False
    try:
        work_kits = work_kit_store.kits()
    except WorkKitStoreError:
        # Work Kits are optional templates; an invalid kit file must not block
        # existing saved-workspace attachment or terminal operation.
        work_kits = []
        work_kit_error = True
    default = next((item for item in workspaces if item["id"] == store.DEFAULT_ID), workspaces[0])
    saved = [item for item in workspaces if item["id"] != store.DEFAULT_ID]
    choices = [{"kind": "workspace", "group": "windows", "scope": "jtt_workspace",
                "id": default["id"], "label": default["name"], "terminal_ids": default["terminal_ids"]}]
    choices.extend({"kind": "workspace", "group": "saved", "scope": "jtt_workspace",
                    "id": item["id"], "label": item["name"], "terminal_ids": item["terminal_ids"]}
                   for item in saved)
    choices.extend(_work_kit_choices(work_kits, unavailable=work_kit_error))
    choices.extend([
        {"kind": "action", "group": "actions", "id": "action:save", "label": "구성 저장",
         "dialog": "구성 저장"},
        {"kind": "action", "group": "actions", "id": "action:settings", "label": "설정 · 메뉴",
         "dialog": "설정 · 메뉴"},
    ])
    selected = default
    rows_by_choice = {
        item["id"]: _session_rows(registry, item, clients, attach=False,
                                  keybindings=keybindings)
        for item in [default, *saved]
    }
    rows = _session_rows(registry, default, clients, attach=attach_default,
                         keybindings=keybindings)
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
        "jtt_work_kit_store": work_kit_store,
        "jtt_work_kits": work_kits,
        "jtt_work_kit_error": work_kit_error,
        "work_kit_preview": None,
        "jtt_registry": registry,
        "jtt_keybindings": keybindings,
        "jtt_workspaces": workspaces,
        "jtt_notice": "",
        "jtt_notice_until": 0.0,
        "jtt_prefix_pending": False,
        "jtt_prefix_deadline": 0.0,
        "jtt_prefix_target": None,
        "jtt_pending_auto_acquire": None,
        "jtt_action_handler": _activate_jtt_action,
        "jtt_work_kit_handler": _preview_work_kit,
        "jtt_center_action_handler": lambda stdscr, state, index: (
            _save_configuration(stdscr, state) if index == 0 else _create_terminal(stdscr, state)
        ),
        "jtt_space_handler": _settings_menu,
        "jtt_workspace_handler": _attach_choice,
        "jtt_prefix_handler": _handle_prefix_command,
    }
    return state


def _work_kit_choices(work_kits: list[dict], *, unavailable: bool = False) -> list[dict]:
    if unavailable:
        return [{"kind": "info", "group": "work_kits", "id": "work-kit-unavailable",
                 "label": "Work Kit 사용 불가 · 저장 데이터 오류"}]
    choices = [{"kind": "work_kit", "group": "work_kits", "id": kit["id"],
                "label": kit["name"], "description": kit["description"]}
               for kit in work_kits]
    choices.append({"kind": "action", "group": "work_kits", "id": "action:work-kit-create",
                    "label": "＋ 새 Work Kit"})
    return choices


def _preview_work_kit(state: dict, choice: dict) -> None:
    """Project one saved template into cards without resolving any processes."""
    renderer._release_managed_input(state)
    state["jtt_pending_auto_acquire"] = None
    state["work_kit_preview"] = None
    try:
        matches = [kit for kit in state["jtt_work_kit_store"].kits()
                   if kit["id"] == str(choice.get("id") or "")]
    except WorkKitStoreError:
        matches = []
    if len(matches) != 1:
        state["panes"] = []
        state["pane_index"] = 0
        state["selected_target_id"] = ""
        state["selected_signature"] = None
        state["jtt_notice"] = "Work Kit을 읽을 수 없습니다. 템플릿 미리보기를 중단했습니다."
        state["jtt_notice_until"] = 0.0
        return
    kit = matches[0]
    state["work_kit_preview"] = kit
    state["workspace_id"] = "work-kit-preview:" + kit["id"]
    state["workspace_name"] = kit["name"]
    state["panes"] = [{
        "kind": "work_kit_template",
        "terminal_kind": "work_kit_template",
        "template_id": terminal["id"],
        "display_name": terminal["name"],
        "agent": terminal["role"],
        "status": "TEMPLATE",
        "terminal_view_state": "not_running",
        "terminal_preview": "템플릿 · 실행되지 않음",
        "working_directory": terminal["working_directory"],
        "live_lines": [],
    } for terminal in kit["terminals"]]
    state["pane_index"] = 0
    state["selected_target_id"] = ""
    state["selected_signature"] = None
    state["card_scroll"] = 0
    state["live_scroll"] = 0
    state["center_actions_focused"] = False
    state["custom_view"] = False
    state["jtt_notice"] = "Work Kit 미리보기 · 터미널은 실행되지 않았습니다."
    state["jtt_notice_until"] = 0.0


def _refresh_work_kit_choices(state: dict, selected_id: str | None = None) -> None:
    work_kits = state["jtt_work_kit_store"].kits()
    choices = [item for item in state["choices"] if item.get("group") != "work_kits"]
    insertion = next((index for index, item in enumerate(choices)
                      if item.get("group") == "actions"), len(choices))
    choices[insertion:insertion] = _work_kit_choices(work_kits)
    state["choices"] = choices
    state["jtt_work_kits"] = work_kits
    state["jtt_work_kit_error"] = False
    if selected_id:
        index = next((i for i, item in enumerate(choices)
                      if item.get("kind") == "work_kit" and item.get("id") == selected_id), None)
        if index is not None:
            state["nav_index"] = index
            _preview_work_kit(state, choices[index])
            state["active_panel"] = 1


def _create_work_kit(stdscr, state: dict) -> None:
    if state.get("jtt_work_kit_error"):
        state["jtt_notice"] = "Work Kit 저장 데이터 오류로 생성을 막았습니다. 기존 파일은 변경하지 않았습니다."
        state["jtt_notice_until"] = 0.0
        return
    name = prompt_text(stdscr, "Work Kit 이름: ")
    if not name:
        return
    description = prompt_text(stdscr, "한 줄 설명 (선택): ")
    if description is None:
        return
    count_text = prompt_text(
        stdscr, "터미널 개수 (1–4): ",
        context_lines=["Work Kit은 템플릿만 저장합니다. 셸이나 명령은 실행되지 않습니다."],
    )
    if count_text is None:
        return
    try:
        count = int(count_text)
        terminals = []
        if count not in (1, 2, 3, 4):
            raise ValueError
        for index in range(count):
            terminal_name = prompt_text(stdscr, f"터미널 {index + 1} 이름: ")
            if not terminal_name:
                return
            role = prompt_text(stdscr, f"터미널 {index + 1} 역할: ")
            if not role:
                return
            working_directory = _prompt_working_directory(
                stdscr, f"터미널 {index + 1} 작업 폴더 (선택): "
            )
            if working_directory is None:
                return
            terminals.append({
                "name": terminal_name,
                "role": role,
                "working_directory": working_directory or None,
            })
        kit = state["jtt_work_kit_store"].create_kit(name, description, terminals)
        _refresh_work_kit_choices(state, kit["id"])
    except (ValueError, WorkKitStoreError):
        state["jtt_notice"] = "Work Kit 저장 상태를 확인할 수 없습니다 · 저장 데이터를 다시 확인하세요."
        state["jtt_notice_until"] = 0.0


def _prompt_working_directory(stdscr, label: str) -> str | None:
    """Collect optional path text verbatim; never inspect or normalize it."""
    height, width = stdscr.getmaxyx()
    y = min(height - 2, 3)
    chars = []
    curses.curs_set(1)
    stdscr.timeout(-1)
    try:
        while True:
            stdscr.erase()
            safe_add(stdscr, y, 0, label + "".join(chars), curses.A_BOLD)
            cursor = display_width(label + "".join(chars))
            try:
                stdscr.move(y, min(max(0, width - 1), cursor))
            except curses.error:
                pass
            stdscr.refresh()
            key = read_key(stdscr)
            if is_escape(key):
                return None
            if is_enter(key):
                return "".join(chars)
            if is_backspace(key):
                if chars:
                    chars.pop()
                continue
            if isinstance(key, str) and key.isprintable():
                chars.append(key)
    finally:
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        stdscr.timeout(200)


def _attach_choice(state: dict, choice: dict) -> None:
    """Attach only the selected workspace's exact sessions to this UI client."""
    state["work_kit_preview"] = None
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

    rows = _session_rows(
        state["jtt_registry"], workspace, clients, attach=True,
        keybindings=state.get("jtt_keybindings", DEFAULT_JTT_KEYBINDINGS),
    )
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
                            state["managed_terminals"], attach_default=False,
                            keybindings=state.get("jtt_keybindings", DEFAULT_JTT_KEYBINDINGS))
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
    if state.get("work_kit_preview") is not None:
        state["jtt_notice"] = "Work Kit 미리보기에서는 작업공간 구성을 저장하지 않습니다."
        state["jtt_notice_until"] = 0.0
        return
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
        _confirm_and_stop_selected_terminal(stdscr, state)
    else:
        keys = state.get("jtt_keybindings", DEFAULT_JTT_KEYBINDINGS)
        show_message_screen(
            stdscr, "JTT 키 안내",
            ["탐색: Tab/Shift+Tab 패널 · Enter 입력 · q/Q/Esc는 JTT 유지",
             f"{keys.prefix} n: 새 터미널 · h/l: 카드 이동 · w: 작업공간",
             f"{keys.prefix} x: 선택 터미널 종료 (취소가 기본 선택)",
             f"{keys.prefix} d: UI 연결 해제 (셸 유지) · ?: 도움",
             f"{keys.prefix} Esc: 접두 취소 · 대화상자 Esc: 닫기",
             "입력 중 q/Q/Esc 및 bare n/Tab은 터미널로 전달됩니다.",
             f"입력 중 {keys.prefix} 두 번 / {keys.prefix} {keys.input_escape}: 해당 키 전송",
             f"{keys.input_escape}는 입력 모드에서 탐색 복귀입니다."],
        )


def _capture_stop_target(state: dict) -> dict | None:
    """Capture a selected JTT session only when card, binding, store and registry agree."""

    row = renderer._selected_row(state)
    if (not row or row.get("terminal_kind") != "tower_managed"
            or row.get("stale") or str(row.get("terminal_shell_state") or "running") != "running"):
        return None
    identity = row.get("managed_identity")
    if not isinstance(identity, dict):
        return None
    session_id = str(identity.get("session_id") or "")
    generation = str(identity.get("generation") or "")
    try:
        shell_pid = int(identity.get("shell_pid") or 0)
    except (TypeError, ValueError):
        return None
    signature = renderer._pane_signature(row)
    target_id = str(row.get("target_id") or "")
    if (not session_id or not generation or shell_pid <= 0
            or str(row.get("managed_id") or "") != session_id
            or target_id != session_id
            or str(state.get("selected_target_id") or "") != target_id
            or state.get("selected_signature") != signature
            or signature != ("jtt", session_id, generation, str(shell_pid))):
        return None
    binding = renderer._managed_binding(state)
    if (binding is None or not binding.matches(row)
            or binding.session_id != session_id
            or binding.generation != generation or binding.shell_pid != shell_pid):
        return None
    workspace_id = str(state.get("workspace_id") or "")
    try:
        workspaces = state["jtt_store"].workspaces()
        workspace = next((item for item in workspaces if str(item.get("id")) == workspace_id), None)
        identities = [item for item in state["jtt_registry"].list_sessions()
                      if str(getattr(item, "session_id", "")) == session_id]
    except (KeyError, OSError, RuntimeError, ValueError, TypeError, WorkspaceStoreError):
        return None
    if (workspace is None or session_id not in {str(item) for item in workspace.get("terminal_ids", ())}
            or len(identities) != 1):
        return None
    registered = identities[0]
    try:
        registered_pid = int(getattr(registered, "shell_pid", -1))
    except (TypeError, ValueError):
        return None
    if (str(getattr(registered, "generation", "")) != generation
            or registered_pid != shell_pid
            or str(getattr(registered, "state", "")).lower() != "running"):
        return None
    return {
        "session_id": session_id,
        "generation": generation,
        "shell_pid": shell_pid,
        "workspace_id": workspace_id,
        "pane_index": int(state.get("pane_index", 0)),
        "target_id": target_id,
        "signature": signature,
        "binding": binding,
        "display_name": str(row.get("display_name") or "터미널"),
    }


def _capture_exited_card_target(state: dict) -> dict | None:
    """Capture an exactly identified, confirmed-exited card for reference removal.

    This path never stops a terminal. It is intentionally stricter than the
    normal running-terminal stop path: renderer/supervisor uncertainty or any
    state other than an explicit registry ``exited`` state blocks cleanup.
    """

    row = renderer._selected_row(state)
    if (not row or row.get("terminal_kind") != "tower_managed"
            or str(row.get("terminal_shell_state") or "").lower() != "exited"
            or str(row.get("terminal_view_state") or "") != "exited"
            or str(row.get("terminal_renderer_state") or "") != "ready"):
        return None
    identity = row.get("managed_identity")
    if not isinstance(identity, dict):
        return None
    session_id = str(identity.get("session_id") or "")
    generation = str(identity.get("generation") or "")
    try:
        shell_pid = int(identity.get("shell_pid") or 0)
        pane_index = int(state.get("pane_index", 0))
    except (TypeError, ValueError):
        return None
    target_id = str(row.get("target_id") or "")
    signature = renderer._pane_signature(row)
    if (not session_id or not generation or shell_pid <= 0
            or str(row.get("managed_id") or "") != session_id
            or target_id != session_id
            or str(state.get("selected_target_id") or "") != target_id
            or state.get("selected_signature") != signature
            or signature != ("jtt", session_id, generation, str(shell_pid))):
        return None

    binding = renderer._managed_binding(state)
    if (binding is None or not binding.matches(row)
            or binding.session_id != session_id
            or binding.generation != generation or binding.shell_pid != shell_pid):
        return None

    workspace_id = str(state.get("workspace_id") or "")
    try:
        workspaces = state["jtt_store"].workspaces()
        workspace_matches = [item for item in workspaces
                             if str(item.get("id")) == workspace_id]
        identities = [item for item in state["jtt_registry"].list_sessions()
                      if str(getattr(item, "session_id", "")) == session_id]
    except (KeyError, OSError, RuntimeError, ValueError, TypeError, WorkspaceStoreError):
        return None
    if len(workspace_matches) != 1 or len(identities) != 1:
        return None
    workspace = workspace_matches[0]
    if session_id not in {str(item) for item in workspace.get("terminal_ids", ())}:
        return None
    registered = identities[0]
    try:
        registered_pid = int(getattr(registered, "shell_pid", -1))
    except (TypeError, ValueError):
        return None
    if (str(getattr(registered, "generation", "")) != generation
            or registered_pid != shell_pid
            or str(getattr(registered, "state", "")).lower() != "exited"):
        return None
    return {
        "session_id": session_id,
        "generation": generation,
        "shell_pid": shell_pid,
        "workspace_id": workspace_id,
        "pane_index": pane_index,
        "target_id": target_id,
        "signature": signature,
        "binding": binding,
        "display_name": str(row.get("display_name") or "터미널"),
    }


def _confirm_and_remove_exited_card(stdscr, state: dict) -> None:
    """Remove only the selected exited session ID from the current workspace."""

    target = _capture_exited_card_target(state)
    if target is None:
        state["jtt_notice"] = "종료 상태와 신원을 확인할 수 없어 카드 제거를 차단했습니다."
        state["jtt_notice_until"] = 0.0
        return
    confirm = run_list_picker(
        stdscr,
        "종료된 터미널 카드 제거",
        [("cancel", "취소"), ("remove", f"{target['display_name']} 카드 제거")],
        centered=True,
        footer_hint="현재 작업공간의 카드 참조만 제거합니다 · 터미널 파일과 다른 작업공간은 유지됩니다.",
    )
    if confirm.cancelled or confirm.selected_key != "remove":
        return

    current = _capture_exited_card_target(state)
    if (current is None
            or current["session_id"] != target["session_id"]
            or current["generation"] != target["generation"]
            or current["shell_pid"] != target["shell_pid"]
            or current["workspace_id"] != target["workspace_id"]
            or current["pane_index"] != target["pane_index"]
            or current["target_id"] != target["target_id"]
            or current["signature"] != target["signature"]
            or current["binding"] is not target["binding"]):
        state["jtt_notice"] = "선택 터미널이 변경되어 카드 제거를 차단했습니다. 다시 확인하세요."
        state["jtt_notice_until"] = 0.0
        return

    session_id = target["session_id"]
    workspace_id = target["workspace_id"]
    try:
        clients = state.get("managed_terminals")
        if (not isinstance(clients, dict)
                or clients.get(session_id) is not target["binding"]):
            raise WorkspaceStoreError("Terminal client binding changed")
        workspaces = state["jtt_store"].workspaces()
        workspace_matches = [item for item in workspaces
                             if str(item.get("id")) == workspace_id]
        if (len(workspace_matches) != 1
                or session_id not in {str(item) for item in
                                      workspace_matches[0].get("terminal_ids", ())}):
            raise WorkspaceStoreError("Workspace membership changed")
        workspace = workspace_matches[0]
        state["jtt_store"].set_terminal_ids(
            workspace_id,
            [item for item in workspace["terminal_ids"] if str(item) != session_id],
        )
        binding = clients.pop(session_id, None)
        if binding:
            binding.close()  # Close this UI client only; never stop/unlink the session.
        _refresh_workspaces(state, workspace_id)
        state["jtt_notice"] = "종료된 터미널 카드를 현재 작업공간에서 제거했습니다."
    except (KeyError, OSError, RuntimeError, ValueError, TypeError, WorkspaceStoreError):
        state["jtt_notice"] = "카드를 제거하지 못했습니다. 작업공간 상태를 다시 확인하세요."
    state["jtt_notice_until"] = 0.0


def _confirm_and_stop_selected_terminal(stdscr, state: dict) -> None:
    """Stop only the exact selected session confirmed by the user; fail closed."""

    selected = renderer._selected_row(state)
    if (selected and selected.get("terminal_kind") == "tower_managed"
            and (str(selected.get("terminal_shell_state") or "").lower() == "exited"
                 or str(selected.get("terminal_view_state") or "") == "exited")):
        _confirm_and_remove_exited_card(stdscr, state)
        return
    target = _capture_stop_target(state)
    if target is None:
        state["jtt_notice"] = "선택 터미널 신원을 확인할 수 없어 종료를 차단했습니다."
        state["jtt_notice_until"] = 0.0
        return
    confirm = run_list_picker(
        stdscr,
        "터미널 종료",
        [("cancel", "취소"), ("stop", f"{target['display_name']} 종료")],
        centered=True,
        footer_hint="취소가 기본 선택입니다 · 종료하면 해당 셸과 자식 프로세스가 끝납니다.",
    )
    if confirm.cancelled or confirm.selected_key != "stop":
        return

    current = _capture_stop_target(state)
    if (current is None
            or current["session_id"] != target["session_id"]
            or current["generation"] != target["generation"]
            or current["shell_pid"] != target["shell_pid"]
            or current["workspace_id"] != target["workspace_id"]
            or current["pane_index"] != target["pane_index"]
            or current["target_id"] != target["target_id"]
            or current["signature"] != target["signature"]
            or current["binding"] is not target["binding"]):
        state["jtt_notice"] = "선택 터미널이 변경되어 종료를 차단했습니다. 다시 선택하고 확인하세요."
        state["jtt_notice_until"] = 0.0
        return

    session_id = target["session_id"]
    workspace_id = target["workspace_id"]
    try:
        state["jtt_registry"].stop(
            session_id,
            expected_generation=target["generation"],
            expected_shell_pid=target["shell_pid"],
        )
        workspace = next(item for item in state["jtt_store"].workspaces()
                         if str(item["id"]) == workspace_id)
        state["jtt_store"].set_terminal_ids(
            workspace_id,
            [item for item in workspace["terminal_ids"] if str(item) != session_id],
        )
        binding = state["managed_terminals"].pop(session_id, None)
        if binding:
            binding.close()
        _refresh_workspaces(state, workspace_id)
        state["jtt_notice"] = "선택한 터미널을 종료했습니다."
    except (KeyError, OSError, RuntimeError, ValueError, TypeError, WorkspaceStoreError):
        state["jtt_notice"] = "선택한 터미널을 종료하지 못했습니다. 작업 상태는 다시 확인하세요."
    state["jtt_notice_until"] = 0.0


def _create_terminal(stdscr, state: dict) -> None:
    if state.get("work_kit_preview") is not None:
        state["jtt_notice"] = "Work Kit 미리보기입니다. 셸을 만들지 않았습니다."
        state["jtt_notice_until"] = 0.0
        return
    renderer._release_managed_input(state)
    state["jtt_pending_auto_acquire"] = None
    session_id = ""
    client = None
    try:
        client = state["jtt_registry"].create()
        session_id = str(client.identity.session_id)
        state["managed_terminals"][session_id] = ManagedTerminalBinding(
            client, client.identity,
            keybindings=state.get("jtt_keybindings", DEFAULT_JTT_KEYBINDINGS),
        )
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
    if command == "x":
        renderer._release_managed_input(state)
        if state.get("jtt_terminal_mode_fatal"):
            return False
        _confirm_and_stop_selected_terminal(stdscr, state)
        return False
    if command == "d":
        renderer._release_managed_input(state)
        state["jtt_pending_auto_acquire"] = None
        renderer._clear_jtt_prefix(state)
        return True
    if command == "?":
        renderer._release_managed_input(state)
        if state.get("jtt_terminal_mode_fatal"):
            return False
        keys = state.get("jtt_keybindings", DEFAULT_JTT_KEYBINDINGS)
        create_help = (f"{keys.prefix} n: 새 터미널 (명시적 생성)"
                       if state.get("work_kit_preview") is None
                       else f"{keys.prefix} n: Work Kit 미리보기 중 생성 차단")
        show_message_screen(
            stdscr, "JTT 키 안내",
            [create_help,
             f"{keys.prefix} h/l: 이전/다음 카드 (입력 권한 해제)",
             f"{keys.prefix} x: 선택 터미널 종료 (취소가 기본 선택)",
             f"{keys.prefix} w: 작업공간 패널", f"{keys.prefix} d: UI 연결 해제 (셸 유지)",
             f"종료된 셸은 {keys.prefix} x로 현재 작업공간 카드 참조만 제거합니다.",
             "탐색 q/Q/Esc는 JTT를 닫지 않으며 입력 중 q/Q/Esc는 터미널로 전달합니다.",
             f"{keys.prefix} ?: 도움 · {keys.prefix} Esc: 접두 취소 · 대화상자 Esc: 닫기",
             f"입력 중 bare n/Tab은 터미널로 전달 · {keys.prefix} 두 번은 접두 키 전송",
             f"{keys.prefix} {keys.input_escape}는 {keys.input_escape} 전송",
             f"{keys.input_escape}는 입력 모드에서 탐색 복귀입니다."],
        )
        return False
    return False


def _activate_jtt_action(stdscr, state: dict, choice: dict) -> None:
    if choice.get("id") == "action:work-kit-create":
        _create_work_kit(stdscr, state)
    elif choice.get("id") == "action:save":
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
                escape_name = state.get("jtt_keybindings", DEFAULT_JTT_KEYBINDINGS).input_escape
                state["jtt_notice"] = f"터미널 준비 완료 · 입력 중 · {escape_name} 탐색 복귀"
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


def _run(stdscr, store: JTTWorkspaceStore, registry: TerminalRegistry,
         keybindings: JTTKeybindings | None = None) -> None:
    clients: dict[str, ManagedTerminalBinding] = {}
    state = build_state(store, registry, clients, keybindings=keybindings)
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
            if state.get("jtt_terminal_mode_fatal"):
                return
            renderer.draw_preview_v2(stdscr, state)
            try:
                key = stdscr.get_wch()
            except curses.error:
                continue
            if key in (-1, None):
                continue
            should_detach = renderer._handle_key(stdscr, state, key)
            if should_detach or state.get("jtt_terminal_mode_fatal"):
                return
    finally:
        renderer._clear_jtt_prefix(state)
        state["jtt_pending_auto_acquire"] = None
        renderer._release_managed_input(state)
        for binding in list(clients.values()):
            binding._restore_input_terminal_mode()
            # close() disconnects the UI client only; the PTY shell remains alive.
            binding.close()
        state["managed_terminals"].clear()
        renderer._restore_mouse(previous_mouse)
        stdscr.timeout(200)


def run_jtt_ui(store: JTTWorkspaceStore | None = None,
               registry: TerminalRegistry | None = None) -> int:
    store = store or JTTWorkspaceStore()
    try:
        keybindings = load_jtt_keybindings(store.root)
    except JTTKeybindingsError:
        print("JTT 키 설정이 안전하지 않거나 올바르지 않습니다. keybindings.json을 확인하세요.",
              file=sys.stderr)
        return 2
    registry = registry or TerminalRegistry(root=store.root / "terminals")
    locale.setlocale(locale.LC_ALL, "")
    os.environ.setdefault("ESCDELAY", "25")
    try:
        with _catch_jtt_termination_signals():
            curses.wrapper(_run, store, registry, keybindings)
    except _JTTTerminationSignal as stopped:
        # Conventional shell status: 128 + signal number (e.g. SIGTERM=143).
        return 128 + int(stopped.signum)
    except ValueError:
        print("JTT curses 종료 복구를 위해 메인 스레드에서 실행해야 합니다.",
              file=sys.stderr)
        return 2
    return 0
