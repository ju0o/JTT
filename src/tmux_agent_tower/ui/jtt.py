"""JTT's tmux-independent multi-terminal UI over the approved renderer."""

from __future__ import annotations

import curses
import locale
import os
import queue
import signal
import sys
import threading
import time
from contextlib import contextmanager
from types import SimpleNamespace

from ..state.jtt_keys import (
    DEFAULT_JTT_KEYBINDINGS,
    JTTKeybindings,
    JTTKeybindingsError,
    load_jtt_keybindings,
)
from ..state.jtt_layouts import (
    JTTLayoutStore,
    JTTLayoutStoreCommittedDurabilityError,
    JTTLayoutStoreError,
)
from ..state.jtt_work_kits import (
    JTTWorkKitStore,
    WorkKitStoreCommittedDurabilityError,
    WorkKitStoreError,
)
from ..state.jtt_workspaces import (
    JTTWorkspaceStore,
    WorkspaceStoreCommittedDurabilityError,
    WorkspaceStoreError,
)
from ..terminal import TerminalRegistry, TerminalScreen
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


_MINI_REFRESH_SECONDS = 1.0
_MINI_CACHE_SECONDS = 2.5
_MINI_MAX_VISIBLE_REQUESTS = 16
_MINI_MAX_ROWS = 4
_MINI_MAX_COLUMNS = 64


class _MiniLiveSampler:
    """One bounded background observer; it never polls or acquires input leases."""

    def __init__(self):
        self._requests = queue.Queue(maxsize=1)
        self._results = queue.Queue(maxsize=1)
        self._lock = threading.Lock()
        self._busy = False
        self._stopping = False
        self._thread = threading.Thread(target=self._run, name="jtt-mini-live", daemon=True)
        self._thread.start()

    def submit(self, registry, token, targets) -> bool:
        if not targets:
            return False
        with self._lock:
            if self._busy or self._stopping:
                return False
            self._busy = True
            try:
                self._requests.put_nowait((registry, token, tuple(targets[:_MINI_MAX_VISIBLE_REQUESTS])))
            except queue.Full:
                self._busy = False
                return False
            return True

    def take(self):
        try:
            return self._results.get_nowait()
        except queue.Empty:
            return None

    def close(self) -> None:
        with self._lock:
            self._stopping = True
            try:
                self._requests.get_nowait()
            except queue.Empty:
                pass
            while True:
                try:
                    self._requests.put_nowait(None)
                    break
                except queue.Full:
                    try:
                        self._requests.get_nowait()
                    except queue.Empty:
                        continue
        self._thread.join(timeout=0.5)

    def _run(self) -> None:
        while True:
            request = self._requests.get()
            if request is None:
                return
            registry, token, targets = request
            samples = []
            try:
                for session_id, generation, shell_pid in targets:
                    if self._stopping:
                        break
                    try:
                        client = registry.connect(session_id, timeout=0.25)
                        try:
                            observation = client.observe()
                            valid = (observation.synced
                                     and observation.session_id == session_id
                                     and observation.generation == generation
                                     and observation.shell_pid == shell_pid
                                     and observation.state == "running"
                                     and observation.screen is not None)
                            samples.append({
                                "session_id": session_id,
                                "generation": generation,
                                "shell_pid": shell_pid,
                                "sequence": observation.sequence,
                                "screen": _clip_mini_screen(observation.screen) if valid else None,
                                "synced": valid,
                            })
                        finally:
                            client.close()
                    except (OSError, RuntimeError, ValueError):
                        samples.append({"session_id": session_id, "generation": generation,
                                        "shell_pid": shell_pid, "synced": False, "screen": None})
                if not self._stopping:
                    try:
                        self._results.put_nowait((token, samples))
                    except queue.Full:
                        try:
                            self._results.get_nowait()
                        except queue.Empty:
                            pass
                        self._results.put_nowait((token, samples))
            finally:
                with self._lock:
                    self._busy = False


def _clip_mini_screen(screen) -> TerminalScreen | None:
    if screen is None:
        return None
    rows = tuple(tuple(row[:_MINI_MAX_COLUMNS]) for row in screen.rows[:_MINI_MAX_ROWS])
    return TerminalScreen(rows, int(screen.cursor_x), int(screen.cursor_y),
                          bool(screen.cursor_visible))


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
                "display_name": "터미널 상태 확인 불가", "agent": "Generic Terminal", "status": "UNKNOWN",
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
            "agent": "Generic Terminal", "status": "WORKING" if shell_state == "running" else "DEAD",
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
        parts = []
        for cell in row:
            text = str(getattr(cell, "text", "") or "")
            if text:
                parts.append(text)
        value = "".join(parts).strip()
        if value:
            return value
    return ""


def _default_layout(workspace_id: str, terminal_ids) -> dict:
    ids = list(dict.fromkeys(str(item) for item in terminal_ids if str(item)))
    return {"workspace_id": workspace_id,
            "folders": [{"id": "folder-default", "name": "기본 폴더"}],
            "windows": [{"id": "window-default", "folder_id": "folder-default",
                         "name": "기본 Window", "folder_name": "기본 폴더",
                         "terminal_ids": ids}],
            "placements": {}, "terminal_ids": ids}


def _layout_choices(workspace_id: str, layout: dict,
                    registry: TerminalRegistry | None = None) -> list[dict]:
    """Project every logical Window into one flat, identity-bearing LEFT list."""
    folders = {str(item["id"]): item for item in layout["folders"]}
    display_names = {
        str(window["id"]): ("Window 1" if window["id"] == "window-default"
                            else str(window["name"]))
        for window in layout["windows"]
    }
    duplicate_names: dict[str, list[dict]] = {}
    for window in layout["windows"]:
        duplicate_names.setdefault(display_names[str(window["id"])].casefold(), []).append(window)

    registry_available = registry is not None
    identities = {}
    if registry is not None:
        try:
            identities = {str(item.session_id): item for item in registry.list_sessions()}
        except (OSError, RuntimeError, ValueError, TypeError):
            registry_available = False

    choices = []
    for window in layout["windows"]:
        window_id = str(window["id"])
        folder_id = str(window["folder_id"])
        folder = folders.get(folder_id)
        folder_name = str((folder or {}).get("name") or "폴더")
        name = display_names[window_id]
        suffix = (f" · {folder_name}"
                  if len(duplicate_names[name.casefold()]) > 1 else "")
        session_ids = list(window.get("terminal_ids") or ())
        if not registry_available:
            activity = "상태 UNKNOWN"
            current_count = None
        else:
            current = [identities[item] for item in session_ids if item in identities]
            current_count = len(current)
            running = sum(str(getattr(item, "state", "unknown")).lower()
                          in {"running", "stopping"} for item in current)
            exited = sum(str(getattr(item, "state", "unknown")).lower()
                         in {"exited", "dead", "stopped", "orphaned"} for item in current)
            if not session_ids:
                activity = "비어 있음"
            elif not current:
                activity = "세션 확인 필요"
            else:
                activity = f"실행 {running} · 종료 {exited}"
        count = "UNKNOWN" if current_count is None else str(current_count)
        choices.append({
            "kind": "logical_window", "group": "windows", "scope": "logical_window",
            "id": f"window:{workspace_id}:{window_id}",
            "workspace_id": workspace_id, "folder_id": folder_id,
            "window_id": window_id,
            "label": f"{name}{suffix} · 터미널 {count} · {activity}",
            "window_name": name, "folder_name": folder_name,
            "terminal_count": current_count, "terminal_refs": tuple(session_ids),
        })
    return choices


def _workspace_layout(state: dict, workspace_id: str | None = None):
    """Read current canonical workspace refs before projecting additive layout."""
    workspace_id = str(workspace_id or state.get("workspace_id") or "")
    workspaces = state["jtt_store"].workspaces()
    matches = [item for item in workspaces if str(item.get("id") or "") == workspace_id]
    if len(matches) != 1:
        raise WorkspaceStoreError("Workspace identity is no longer available")
    workspace = dict(matches[0])
    layout = state["jtt_layout_store"].layout(workspace_id, workspace["terminal_ids"])
    return workspace, layout, workspaces


def _replace_workspace_layout_choices(state: dict, workspace: dict, layout: dict,
                                      *, selected_choice_id: str | None = None) -> None:
    choices = [item for item in state["choices"]
               if item.get("kind") not in {"logical_folder", "logical_window"}
               and item.get("kind") != "workspace"]
    logical = _layout_choices(str(workspace["id"]), layout, state.get("jtt_registry"))
    state["choices"] = [*logical, *choices]
    state["jtt_layout"] = layout
    state["jtt_layout_error"] = False
    if selected_choice_id:
        index = next((i for i, item in enumerate(state["choices"])
                      if str(item.get("id") or "") == selected_choice_id), None)
        if index is not None:
            state["nav_index"] = index


def _clear_mini_live(state: dict) -> None:
    state["mini_live_scope"] = None
    state["mini_live_cache"] = {}
    state["mini_live_last_sequence"] = {}
    state["mini_live_activity_until"] = {}
    for row in state.get("panes", ()):
        row.pop("mini_screen", None)
        row.pop("mini_live_state", None)
        row.pop("terminal_activity", None)


def _visible_mini_targets(state: dict, width: int, height: int) -> tuple[tuple, list[tuple[str, str, int]]]:
    """Return exact visible managed-card identities using renderer card geometry."""
    if state.get("work_kit_preview") is not None:
        return (), []
    regions, _wide = renderer._regions(width, height, int(state.get("active_panel", 0)))
    rect = regions.get(1)
    if rect is None:
        return (), []
    x, y, panel_width, panel_height = rect
    ix, iy, iw, ih = x + 1, y + 1, panel_width - 2, panel_height - 2
    card_top = iy + 2
    card_area = (ix + 1, card_top, max(1, iw - 2), max(1, ih - 5))
    geometry = renderer.card_rectangles(
        len(state.get("panes", ())), card_area, scroll_row=int(state.get("card_scroll", 0))
    )
    targets = []
    for index, _x, _y, _width, _height in geometry:
        row = state["panes"][index]
        identity = row.get("managed_identity")
        if row.get("terminal_kind") != "tower_managed" or not isinstance(identity, dict):
            continue
        session_id = str(identity.get("session_id") or "")
        generation = str(identity.get("generation") or "")
        try:
            shell_pid = int(identity.get("shell_pid") or 0)
        except (TypeError, ValueError):
            continue
        if (session_id and generation and shell_pid > 0
                and str(row.get("managed_id") or "") == session_id):
            targets.append((session_id, generation, shell_pid))
    token = (str(state.get("workspace_id") or ""),
             str(state.get("selected_window_id") or ""),
             tuple(targets))
    return token, targets


def _refresh_mini_live(state: dict, stdscr) -> None:
    sampler = state.get("mini_live_sampler")
    if sampler is None:
        return
    height, width = stdscr.getmaxyx()
    renderer._ensure_card_visible(state, height, width)
    token, targets = _visible_mini_targets(state, width, height)
    if token != state.get("mini_live_scope"):
        _clear_mini_live(state)
        state["mini_live_scope"] = token

    now = time.monotonic()
    result = sampler.take()
    if result is not None:
        result_token, samples = result
        if result_token == token:
            current = set(targets)
            for sample in samples:
                identity = (sample["session_id"], sample["generation"], sample["shell_pid"])
                if identity not in current:
                    continue
                row = next((item for item in state["panes"]
                            if str(item.get("managed_id") or "") == identity[0]
                            and item.get("managed_identity", {}).get("generation") == identity[1]
                            and int(item.get("managed_identity", {}).get("shell_pid") or 0) == identity[2]), None)
                if row is None:
                    continue
                if not sample.get("synced") or sample.get("screen") is None:
                    state["mini_live_cache"].pop(identity, None)
                    row.pop("mini_screen", None)
                    row.pop("mini_live_state", None)
                    row.pop("terminal_activity", None)
                    continue
                sequence = int(sample.get("sequence") or 0)
                previous = state["mini_live_last_sequence"].get(identity)
                if previous is not None and sequence != previous:
                    state["mini_live_activity_until"][identity] = now + 1.5
                state["mini_live_last_sequence"][identity] = sequence
                state["mini_live_cache"][identity] = {
                    "screen": sample["screen"], "updated": now,
                }

    for row in state.get("panes", ()):
        identity_dict = row.get("managed_identity") or {}
        try:
            identity = (str(identity_dict.get("session_id") or ""),
                        str(identity_dict.get("generation") or ""),
                        int(identity_dict.get("shell_pid") or 0))
        except (TypeError, ValueError):
            identity = ("", "", 0)
        cached = state["mini_live_cache"].get(identity) if identity in set(targets) else None
        if cached and now - float(cached["updated"]) <= _MINI_CACHE_SECONDS:
            row["mini_screen"] = cached["screen"]
            row["mini_live_state"] = "synced"
            if now < state["mini_live_activity_until"].get(identity, 0.0):
                row["terminal_activity"] = "출력 변화"
            else:
                row.pop("terminal_activity", None)
        else:
            row.pop("mini_screen", None)
            row.pop("mini_live_state", None)
            row.pop("terminal_activity", None)
            if cached:
                state["mini_live_cache"].pop(identity, None)

    if (targets and now - state.get("mini_live_last_submit", 0.0) >= _MINI_REFRESH_SECONDS
            and sampler.submit(state["jtt_registry"], token, targets)):
        state["mini_live_last_submit"] = now


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
    layout_store = JTTLayoutStore(store.root)
    layout_error = False
    try:
        default_layout = layout_store.layout(default["id"], default["terminal_ids"])
    except JTTLayoutStoreError:
        default_layout = _default_layout(default["id"], default["terminal_ids"])
        layout_error = True
    choices = _layout_choices(default["id"], default_layout, registry)
    choices.extend([
        {"kind": "action", "group": "actions", "id": "action:new-window",
         "label": "+ 새 Window", "dialog": "새 Window"},
        {"kind": "action", "group": "actions", "id": "action:archive",
         "label": "보관함", "dialog": "보관함"},
        {"kind": "action", "group": "actions", "id": "action:settings", "label": "설정 · 메뉴",
         "dialog": "설정 · 메뉴"},
    ])
    selected = default
    rows_by_choice = {
        item["id"]: _session_rows(registry, item, clients, attach=False,
                                  keybindings=keybindings)
        for item in [default, *saved]
    }
    default_window = default_layout["windows"][0]
    active_ids = list(default_window["terminal_ids"])
    rows = _session_rows(registry, {**default, "terminal_ids": active_ids}, clients, attach=attach_default,
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
        "selected_folder_id": "folder-default",
        "selected_window_id": "window-default",
        "jtt_layout": default_layout,
        "jtt_layout_store": layout_store,
        "jtt_layout_error": layout_error,
        "window_refs": tuple(active_ids),
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
        "jtt_archive_return_workspace_id": None,
        "jtt_prefix_pending": False,
        "jtt_prefix_deadline": 0.0,
        "jtt_prefix_target": None,
        "mini_live_sampler": None,
        "mini_live_cache": {},
        "mini_live_last_sequence": {},
        "mini_live_activity_until": {},
        "mini_live_scope": None,
        "mini_live_last_submit": 0.0,
        "jtt_pending_auto_acquire": None,
        "jtt_action_handler": _activate_jtt_action,
        "jtt_work_kit_handler": _preview_work_kit,
        "jtt_center_action_handler": lambda stdscr, state, index: (
            _save_configuration(stdscr, state) if index == 0 else _create_terminal(stdscr, state)
        ),
        "jtt_space_handler": _settings_menu,
        "jtt_workspace_handler": _attach_choice,
        "jtt_layout_choice_handler": _attach_layout_choice,
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
    if state.get("work_kit_preview") is None:
        state["jtt_archive_return_workspace_id"] = str(state.get("workspace_id") or "")
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
        state["jtt_archive_return_workspace_id"] = None
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


def _refresh_work_kit_choices(state: dict) -> None:
    work_kits = state["jtt_work_kit_store"].kits()
    state["jtt_work_kits"] = work_kits
    state["jtt_work_kit_error"] = False


def _create_work_kit(stdscr, state: dict) -> None:
    if state.get("jtt_work_kit_error"):
        state["jtt_notice"] = "Work Kit 저장 데이터 오류로 생성을 막았습니다. 기존 파일은 변경하지 않았습니다."
        state["jtt_notice_until"] = 0.0
        return
    name = prompt_text(stdscr, "Work Kit 이름: ")
    if not name:
        state["jtt_notice"] = "Work Kit 생성 취소 · 터미널은 실행되지 않았습니다."
        state["jtt_notice_until"] = 0.0
        return
    description = prompt_text(stdscr, "한 줄 설명 (선택): ")
    if description is None:
        state["jtt_notice"] = "Work Kit 생성 취소 · 터미널은 실행되지 않았습니다."
        state["jtt_notice_until"] = 0.0
        return
    count_text = prompt_text(
        stdscr, "터미널 개수 (1–4): ",
        context_lines=["Work Kit은 템플릿만 저장합니다. 셸이나 명령은 실행되지 않습니다."],
    )
    if count_text is None:
        state["jtt_notice"] = "Work Kit 생성 취소 · 터미널은 실행되지 않았습니다."
        state["jtt_notice_until"] = 0.0
        return
    try:
        count = int(count_text)
        terminals = []
        if count not in (1, 2, 3, 4):
            raise ValueError
        for index in range(count):
            terminal_name = prompt_text(stdscr, f"터미널 {index + 1} 이름: ")
            if not terminal_name:
                state["jtt_notice"] = "Work Kit 생성 취소 · 터미널은 실행되지 않았습니다."
                state["jtt_notice_until"] = 0.0
                return
            role = prompt_text(stdscr, f"터미널 {index + 1} 역할: ")
            if not role:
                state["jtt_notice"] = "Work Kit 생성 취소 · 터미널은 실행되지 않았습니다."
                state["jtt_notice_until"] = 0.0
                return
            working_directory = _prompt_working_directory(
                stdscr, f"터미널 {index + 1} 작업 폴더 (선택): "
            )
            if working_directory is None:
                state["jtt_notice"] = "Work Kit 생성 취소 · 터미널은 실행되지 않았습니다."
                state["jtt_notice_until"] = 0.0
                return
            terminals.append({
                "name": terminal_name,
                "role": role,
                "working_directory": working_directory or None,
            })
        kit = state["jtt_work_kit_store"].create_kit(name, description, terminals)
        _refresh_work_kit_choices(state)
        terminal_count = len(kit["terminals"])
        state["jtt_notice"] = (
            f"{kit['name']} Work Kit 저장 완료 · 터미널 {terminal_count}개 구성 · 아직 실행되지 않음"
        )
        state["jtt_notice_until"] = 0.0
    except WorkKitStoreCommittedDurabilityError:
        _refresh_work_kit_choices(state)
        state["jtt_notice"] = (
            f"Work Kit 저장 완료 · 터미널 {len(terminals)}개 구성 · "
            "디렉터리 동기화 확인 필요 · 아직 실행되지 않음"
        )
        state["jtt_notice_until"] = 0.0
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
    state["jtt_archive_return_workspace_id"] = None
    workspace_id = str(choice.get("id") or "")
    workspaces = state["jtt_store"].workspaces()
    workspace = next((item for item in workspaces if item["id"] == workspace_id), None)
    if workspace is None:
        state["panes"] = []
        state["pane_index"] = 0
        state["selected_target_id"] = ""
        state["selected_signature"] = None
        return

    try:
        layout = state["jtt_layout_store"].layout(workspace_id, workspace["terminal_ids"])
    except JTTLayoutStoreError:
        _release_layout_clients(state)
        _clear_mini_live(state)
        state["jtt_layout_error"] = True
        state["panes"] = []
        state["pane_index"] = 0
        state["selected_target_id"] = ""
        state["selected_signature"] = None
        state["jtt_notice"] = "폴더/Window 배치 오류 · 터미널은 변경하지 않았습니다"
        state["jtt_notice_until"] = 0.0
        return
    default_window = layout["windows"][0]
    _replace_workspace_layout_choices(
        state, workspace, layout,
        selected_choice_id=f"window:{workspace_id}:{default_window['id']}",
    )
    state["selected_folder_id"] = default_window["folder_id"]
    state["selected_window_id"] = default_window["id"]
    expected_ids = set(default_window["terminal_ids"])
    clients = state["managed_terminals"]
    for session_id in tuple(clients):
        if session_id not in expected_ids:
            clients.pop(session_id).close()

    rows = _session_rows(
        state["jtt_registry"], {**workspace, "terminal_ids": default_window["terminal_ids"]},
        clients, attach=True,
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
    state["window_refs"] = tuple(default_window["terminal_ids"])
    state["active_panel"] = 0
    _clear_mini_live(state)


def _release_layout_clients(state: dict, keep: set[str] | None = None) -> None:
    keep = keep or set()
    renderer._release_managed_input(state)
    for session_id in tuple(state.get("managed_terminals", {})):
        if session_id not in keep:
            binding = state["managed_terminals"].pop(session_id)
            binding.close()


def _attach_layout_choice(state: dict, choice: dict) -> None:
    """Select a flat logical Window and attach only its exact canonical refs."""
    workspace_id = str(choice.get("workspace_id") or "")
    active_workspace_id = str(
        state.get("jtt_archive_return_workspace_id")
        if state.get("work_kit_preview") is not None
        else state.get("workspace_id") or ""
    )
    if workspace_id != active_workspace_id:
        state["jtt_notice"] = "작업공간이 변경되어 Window 선택을 차단했습니다."
        state["jtt_notice_until"] = 0.0
        return
    try:
        workspace, layout, workspaces = _workspace_layout(state, workspace_id)
    except (KeyError, OSError, RuntimeError, ValueError, TypeError, WorkspaceStoreError,
            JTTLayoutStoreError):
        _release_layout_clients(state)
        _clear_mini_live(state)
        state["jtt_layout_error"] = True
        state["panes"] = []
        state["pane_index"] = 0
        state["selected_target_id"] = ""
        state["selected_signature"] = None
        state["jtt_notice"] = "폴더/Window 정보를 안전하게 읽지 못했습니다 · 터미널 입력은 차단됨"
        state["jtt_notice_until"] = 0.0
        return

    if choice.get("kind") != "logical_window":
        return
    window_id = str(choice.get("window_id") or "")
    window = next((item for item in layout["windows"] if item["id"] == window_id), None)
    if window is None or window["folder_id"] != str(choice.get("folder_id") or ""):
        return
    folder = next((item for item in layout["folders"] if item["id"] == window["folder_id"]), None)
    if folder is None:
        return
    ids = list(window["terminal_ids"])
    selected_choice_id = str(choice.get("id") or "")

    # The validated choice points back to the canonical workspace. A Work Kit
    # preview temporarily replaces state["workspace_id"] with a template ID;
    # restore the real ID before subsequent Window navigation or creation.
    state["workspace_id"] = str(workspace["id"])
    _replace_workspace_layout_choices(state, workspace, layout,
                                      selected_choice_id=selected_choice_id)
    state["work_kit_preview"] = None
    state["jtt_archive_return_workspace_id"] = None
    state["jtt_workspaces"] = workspaces
    state["workspace_name"] = workspace["name"]
    state["selected_folder_id"] = folder["id"]
    state["selected_window_id"] = window_id
    state["window_refs"] = tuple(ids)
    _release_layout_clients(state, set(ids))
    rows = _session_rows(
        state["jtt_registry"], {**workspace, "terminal_ids": ids}, state["managed_terminals"],
        attach=True, keybindings=state.get("jtt_keybindings", DEFAULT_JTT_KEYBINDINGS),
    )
    state["managed_cards"][workspace_id] = rows
    state["panes"] = [dict(row) for row in rows]
    previous_target = str(state.get("selected_target_id") or "")
    pane_index = next((index for index, row in enumerate(state["panes"])
                       if str(row.get("managed_id") or "") == previous_target), 0)
    state["pane_index"] = pane_index if state["panes"] else 0
    selected = state["panes"][state["pane_index"]] if state["panes"] else None
    state["selected_target_id"] = str((selected or {}).get("target_id") or "")
    state["selected_signature"] = renderer._pane_signature(selected)
    state["card_scroll"] = 0
    state["live_scroll"] = 0
    state["active_panel"] = 0
    state["center_actions_focused"] = False
    _clear_mini_live(state)


def _refresh_workspaces(state: dict, selected_id: str | None = None) -> None:
    stdscr = state.get("stdscr")
    mini_sampler = state.get("mini_live_sampler")
    old_workspace_id = str(
        state.get("jtt_archive_return_workspace_id")
        if state.get("work_kit_preview") is not None
        else state.get("workspace_id") or ""
    )
    old_window_id = str(state.get("selected_window_id") or "")
    refreshed = build_state(state["jtt_store"], state["jtt_registry"],
                            state["managed_terminals"], attach_default=False,
                            keybindings=state.get("jtt_keybindings", DEFAULT_JTT_KEYBINDINGS))
    refreshed["stdscr"] = stdscr
    refreshed["mini_live_sampler"] = mini_sampler
    workspaces = refreshed["jtt_store"].workspaces()
    available_ids = {str(item["id"]) for item in workspaces}
    target_workspace_id = str(selected_id or "")
    if target_workspace_id not in available_ids:
        target_workspace_id = old_workspace_id if old_workspace_id in available_ids else JTTWorkspaceStore.DEFAULT_ID
    _attach_choice(refreshed, {"kind": "workspace", "id": target_workspace_id})
    if target_workspace_id == old_workspace_id and old_window_id:
        choice_id = f"window:{target_workspace_id}:{old_window_id}"
        index = next((i for i, item in enumerate(refreshed["choices"])
                      if item.get("id") == choice_id), None)
        if index is not None:
            renderer._set_choice(refreshed, index)
    refreshed["active_panel"] = 0
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
    except WorkspaceStoreCommittedDurabilityError as exc:
        _refresh_workspaces(state, exc.record_id)
        state["jtt_notice"] = f"{name} 구성 저장 반영됨 · 디렉터리 동기화 확인 필요"
    except WorkspaceStoreError:
        state["jtt_notice"] = "구성을 안전하게 저장하지 못했습니다."


def _selected_choice(state: dict) -> dict | None:
    choices = state.get("choices") or ()
    index = int(state.get("nav_index", -1))
    return choices[index] if 0 <= index < len(choices) else None


def _capture_workspace_delete_target(state: dict, workspace_id: str | None = None) -> dict | None:
    if state.get("work_kit_preview") is not None:
        return None
    archive_target = workspace_id is not None
    choice = _selected_choice(state)
    workspace_id = str(workspace_id or state.get("workspace_id") or "")
    if workspace_id == JTTWorkspaceStore.DEFAULT_ID:
        return None
    if archive_target:
        try:
            matches = [item for item in state["jtt_store"].workspaces()
                       if str(item.get("id") or "") == workspace_id]
        except (KeyError, OSError, RuntimeError, ValueError, TypeError, WorkspaceStoreError):
            return None
        if len(matches) != 1 or not matches[0].get("saved"):
            return None
        return {"workspace": dict(matches[0]), "workspace_id": workspace_id}
    if not choice:
        return None
    try:
        matches = [item for item in state["jtt_store"].workspaces()
                   if str(item.get("id") or "") == workspace_id]
    except (KeyError, OSError, RuntimeError, ValueError, TypeError, WorkspaceStoreError):
        return None
    if len(matches) != 1 or not matches[0].get("saved"):
        return None
    selected_choice_id = str(choice.get("id") or "")
    kind = choice.get("kind")
    if kind == "workspace":
        selected_workspace = str(choice.get("id") or "") == workspace_id
    elif (kind in {"logical_folder", "logical_window"}
          and str(choice.get("workspace_id") or "") == workspace_id):
        try:
            layout = state["jtt_layout_store"].layout(workspace_id, matches[0]["terminal_ids"])
        except (KeyError, OSError, RuntimeError, ValueError, TypeError, JTTLayoutStoreError):
            return None
        if kind == "logical_folder":
            folder_id = str(choice.get("folder_id") or "")
            selected_workspace = (
                selected_choice_id == f"folder:{workspace_id}:{folder_id}"
                and any(item["id"] == folder_id for item in layout["folders"])
            )
        else:
            folder_id = str(choice.get("folder_id") or "")
            window_id = str(choice.get("window_id") or "")
            selected_workspace = (
                selected_choice_id == f"window:{workspace_id}:{window_id}"
                and any(item["id"] == window_id and item["folder_id"] == folder_id
                        for item in layout["windows"])
            )
    else:
        selected_workspace = False
    if not selected_workspace:
        return None
    return {"workspace": dict(matches[0]),
            "workspace_id": workspace_id,
            "selected_choice_id": selected_choice_id,
            "pane_index": int(state.get("pane_index", 0)),
            "selected_target_id": str(state.get("selected_target_id") or ""),
            "selected_signature": state.get("selected_signature")}


def _capture_work_kit_delete_target(state: dict) -> dict | None:
    preview = state.get("work_kit_preview")
    choice = _selected_choice(state)
    if (state.get("jtt_work_kit_error") or not isinstance(preview, dict) or not choice
            or choice.get("kind") != "work_kit" or str(choice.get("id") or "") != preview.get("id")):
        return None
    try:
        matches = [item for item in state["jtt_work_kit_store"].kits()
                   if item["id"] == preview["id"]]
    except (KeyError, OSError, RuntimeError, ValueError, TypeError, WorkKitStoreError):
        return None
    if len(matches) != 1 or matches[0] != preview:
        return None
    return {"kit": matches[0], "pane_index": int(state.get("pane_index", 0)),
            "selected_target_id": str(state.get("selected_target_id") or "")}


def _capture_card_reference_target(state: dict) -> dict | None:
    """Capture an uncertain or explicitly exited card for reference-only cleanup."""
    row = renderer._selected_row(state)
    if not row or row.get("terminal_kind") != "tower_managed":
        return None
    shell_state = str(row.get("terminal_shell_state") or "unknown").lower()
    view_state = str(row.get("terminal_view_state") or "")
    renderer_state = str(row.get("terminal_renderer_state") or "unknown")
    confirmed_exited = (shell_state in {"exited", "dead", "stopped"}
                        and view_state == "exited" and renderer_state == "ready")
    uncertain = (shell_state not in {"running", "active", "exited", "dead", "stopped"}
                 or view_state != "running_ready" or renderer_state != "ready"
                 or bool(row.get("stale")) or bool(row.get("_live_stale")))
    if not (confirmed_exited or uncertain):
        return None

    identity = row.get("managed_identity")
    if not isinstance(identity, dict):
        return None
    session_id = str(identity.get("session_id") or "")
    generation = str(identity.get("generation") or "")
    has_shell_pid = "shell_pid" in identity
    try:
        pane_index = int(state.get("pane_index", 0))
    except (TypeError, ValueError):
        return None
    signature = renderer._pane_signature(row)
    if (not session_id or str(row.get("managed_id") or "") != session_id
            or str(row.get("target_id") or "") != session_id
            or str(state.get("selected_target_id") or "") != session_id):
        return None

    workspace_id = str(state.get("workspace_id") or "")
    try:
        workspaces = state["jtt_store"].workspaces()
        workspace_matches = [item for item in workspaces if item["id"] == workspace_id]
    except (KeyError, OSError, RuntimeError, ValueError, TypeError, WorkspaceStoreError):
        return None
    if (len(workspace_matches) != 1
            or session_id not in workspace_matches[0]["terminal_ids"]):
        return None

    if not generation and not has_shell_pid:
        if (set(identity) != {"session_id"} or signature is not None
                or state.get("selected_signature") is not None
                or shell_state != "unknown"
                or view_state not in {"session_unavailable", "supervisor_connection_failed"}
                or not row.get("stale") or not row.get("_live_stale")):
            return None
        try:
            identities = state["jtt_registry"].list_sessions()
            registry_state = "not_found"
        except (KeyError, OSError, RuntimeError, ValueError, TypeError):
            identities = ()
            registry_state = "lookup_unavailable"
        if any(str(getattr(item, "session_id", "")) == session_id for item in identities):
            return None
        binding = (state.get("managed_terminals") or {}).get(session_id)
        if (binding is not None
                and str(getattr(binding, "session_id", "")) != session_id):
            return None
        return {
            "session_id": session_id, "generation": "", "shell_pid": 0,
            "workspace_id": workspace_id, "workspace_snapshot": dict(workspace_matches[0]),
            "pane_index": pane_index, "target_id": session_id, "signature": None,
            "binding": binding, "registry_state": registry_state,
            "classification": "uncertain", "display_name": str(row.get("display_name") or "터미널"),
            "sparse_identity": True,
        }

    if not generation or not has_shell_pid:
        return None
    try:
        shell_pid = int(identity.get("shell_pid") or 0)
    except (TypeError, ValueError):
        return None
    if (shell_pid <= 0 or state.get("selected_signature") != signature
            or signature != ("jtt", session_id, generation, str(shell_pid))):
        return None
    try:
        identities = [item for item in state["jtt_registry"].list_sessions()
                      if str(getattr(item, "session_id", "")) == session_id]
    except (KeyError, OSError, RuntimeError, ValueError, TypeError):
        return None
    if len(identities) != 1:
        return None
    registered = identities[0]
    try:
        registered_pid = int(getattr(registered, "shell_pid", -1))
    except (TypeError, ValueError):
        return None
    registry_state = str(getattr(registered, "state", "unknown")).lower()
    if (str(getattr(registered, "generation", "")) != generation
            or registered_pid != shell_pid):
        return None
    if confirmed_exited and registry_state not in {"exited", "dead", "stopped"}:
        confirmed_exited = False
        uncertain = True
    binding = (state.get("managed_terminals") or {}).get(session_id)
    if binding is not None:
        try:
            binding_pid = int(getattr(binding, "shell_pid", -1))
        except (TypeError, ValueError):
            return None
        if (str(getattr(binding, "session_id", "")) != session_id
                or str(getattr(binding, "generation", "")) != generation
                or binding_pid != shell_pid):
            return None
    return {
        "session_id": session_id, "generation": generation, "shell_pid": shell_pid,
        "workspace_id": workspace_id, "workspace_snapshot": dict(workspace_matches[0]),
        "pane_index": pane_index, "target_id": session_id, "signature": signature,
        "binding": binding, "registry_state": registry_state,
        "classification": "exited" if confirmed_exited else "uncertain",
        "display_name": str(row.get("display_name") or "터미널"),
    }


def _same_card_reference_target(first: dict | None, second: dict | None) -> bool:
    if first is None or second is None:
        return False
    fields = ("session_id", "generation", "shell_pid", "workspace_id", "workspace_snapshot",
              "pane_index", "target_id", "signature", "registry_state", "classification",
              "sparse_identity")
    return all(first.get(field) == second.get(field) for field in fields) and first.get("binding") is second.get("binding")


def _remove_card_reference(stdscr, state: dict, *, confirmed_exited: bool = False) -> None:
    target = _capture_card_reference_target(state)
    if target is None or (confirmed_exited and target["classification"] != "exited"):
        state["jtt_notice"] = "카드 신원을 확인할 수 없어 참조 정리를 차단했습니다."
        state["jtt_notice_until"] = 0.0
        return
    if target["classification"] == "exited":
        title = "종료된 터미널 카드 제거"
        label = f"{target['display_name']} 카드 제거"
    else:
        title = "터미널 카드 참조 정리"
        label = f"{target['display_name']} 카드 정리"
    confirm = run_list_picker(
        stdscr, title, [("cancel", "취소"), ("remove", label)], centered=True,
        footer_hint=("취소가 기본 선택입니다 · 현재 작업공간 카드 참조만 제거합니다. "
                     "셸·PTY·Supervisor는 종료하지 않습니다."),
        preamble=(
            ["세션 생존 여부는 확인되지 않았습니다.",
             "다른 작업공간 참조는 유지됩니다. 마지막 참조는 기본/복구 작업공간으로 보관합니다."]
            if target["classification"] == "uncertain" else
            ["다른 작업공간 참조는 유지됩니다. 마지막 참조는 기본/복구 작업공간으로 보관합니다."]
        ),
    )
    if confirm.cancelled or confirm.selected_key != "remove":
        return

    current = _capture_card_reference_target(state)
    if not _same_card_reference_target(target, current):
        state["jtt_notice"] = "선택 또는 세션 신원이 변경되어 카드 제거를 차단했습니다."
        state["jtt_notice_until"] = 0.0
        return
    clients = state.get("managed_terminals")
    if not isinstance(clients, dict) or clients.get(target["session_id"]) is not target["binding"]:
        state["jtt_notice"] = "터미널 연결 상태가 변경되어 카드 제거를 차단했습니다."
        state["jtt_notice_until"] = 0.0
        return
    try:
        state["jtt_store"].remove_terminal_reference(
            target["workspace_id"], target["session_id"],
            expected_snapshot=target["workspace_snapshot"],
        )
        _finish_card_reference_removal(state, target)
        state["jtt_notice"] = "카드 참조만 제거했습니다 · 셸과 Supervisor는 종료하지 않았습니다."
    except WorkspaceStoreCommittedDurabilityError:
        _finish_card_reference_removal(state, target)
        state["jtt_notice"] = (
            "카드 참조 정리 반영됨 · 실행 상태는 바뀌지 않음 · 디렉터리 동기화 확인 필요"
        )
    except (KeyError, OSError, RuntimeError, ValueError, TypeError, WorkspaceStoreError):
        state["jtt_notice"] = "카드를 안전하게 정리하지 못했습니다. 작업공간 상태를 다시 확인하세요."
    state["jtt_notice_until"] = 0.0


def _finish_card_reference_removal(state: dict, target: dict) -> None:
    clients = state["managed_terminals"]
    binding = clients.pop(target["session_id"], None)
    if binding is not None:
        binding.close()  # Detach this UI client only; the PTY and supervisor remain.
    _refresh_workspaces(state, target["workspace_id"])


def _confirm_and_delete_workspace(stdscr, state: dict, workspace_id: str | None = None) -> None:
    target = _capture_workspace_delete_target(state, workspace_id)
    if target is None:
        state["jtt_notice"] = "저장 작업공간을 확인할 수 없어 삭제를 차단했습니다."
        state["jtt_notice_until"] = 0.0
        return
    confirm = run_list_picker(
        stdscr, "저장 작업공간 삭제",
        [("cancel", "취소"),
         ("delete", f"{target['workspace']['name']} 삭제 · 포함 터미널 "
                    f"{len(target['workspace']['terminal_ids'])}개")],
        centered=True, footer_hint="취소가 기본 선택입니다 · 셸은 종료하지 않습니다.",
        preamble=["이 작업공간만 삭제합니다. 마지막 터미널 참조는 내 작업공간으로 이동합니다."],
    )
    if confirm.cancelled or confirm.selected_key != "delete":
        return
    current = _capture_workspace_delete_target(state, target["workspace_id"])
    if current is None or current.get("workspace") != target["workspace"]:
        state["jtt_notice"] = "선택 작업공간이 변경되어 삭제를 차단했습니다."
        state["jtt_notice_until"] = 0.0
        return
    try:
        state["jtt_store"].delete_workspace(
            target["workspace_id"], expected_snapshot=target["workspace"],
        )
        selected_workspace_id = (JTTWorkspaceStore.DEFAULT_ID
                                 if str(state.get("workspace_id") or "") == target["workspace_id"]
                                 else str(state.get("workspace_id") or JTTWorkspaceStore.DEFAULT_ID))
        _refresh_workspaces(state, selected_workspace_id)
        state["jtt_notice"] = "작업공간을 삭제했습니다 · 셸은 유지되고 마지막 참조는 내 작업공간으로 이동했습니다."
    except WorkspaceStoreCommittedDurabilityError:
        selected_workspace_id = (JTTWorkspaceStore.DEFAULT_ID
                                 if str(state.get("workspace_id") or "") == target["workspace_id"]
                                 else str(state.get("workspace_id") or JTTWorkspaceStore.DEFAULT_ID))
        _refresh_workspaces(state, selected_workspace_id)
        state["jtt_notice"] = (
            "작업공간 삭제 반영됨 · 마지막 참조는 내 작업공간으로 이동 · "
            "디렉터리 동기화 확인 필요"
        )
    except (KeyError, OSError, RuntimeError, ValueError, TypeError, WorkspaceStoreError):
        state["jtt_notice"] = "작업공간을 안전하게 삭제하지 못했습니다. 상태를 다시 확인하세요."
    state["jtt_notice_until"] = 0.0


def _confirm_and_delete_work_kit(stdscr, state: dict) -> None:
    target = _capture_work_kit_delete_target(state)
    if target is None:
        state["jtt_notice"] = "Work Kit을 확인할 수 없어 삭제를 차단했습니다."
        state["jtt_notice_until"] = 0.0
        return
    kit = target["kit"]
    confirm = run_list_picker(
        stdscr, "Work Kit 삭제",
        [("cancel", "취소"), ("delete", f"{kit['name']} 삭제")],
        centered=True, footer_hint="취소가 기본 선택입니다 · 실행 중인 터미널은 변경하지 않습니다.",
        preamble=["선택한 템플릿 정의만 삭제합니다 · 작업공간과 셸은 변경하지 않습니다."],
    )
    if confirm.cancelled or confirm.selected_key != "delete":
        return
    current = _capture_work_kit_delete_target(state)
    if current != target:
        state["jtt_notice"] = "선택 Work Kit이 변경되어 삭제를 차단했습니다."
        state["jtt_notice_until"] = 0.0
        return
    try:
        state["jtt_work_kit_store"].delete_kit(kit["id"], expected_snapshot=kit)
        _refresh_workspaces(state, JTTWorkspaceStore.DEFAULT_ID)
        state["jtt_notice"] = "Work Kit 템플릿만 삭제했습니다 · 터미널과 작업공간은 변경하지 않았습니다."
    except WorkKitStoreCommittedDurabilityError:
        _refresh_workspaces(state, JTTWorkspaceStore.DEFAULT_ID)
        state["jtt_notice"] = "Work Kit 삭제 반영됨 · 디렉터리 동기화 확인 필요"
    except (KeyError, OSError, RuntimeError, ValueError, TypeError, WorkKitStoreError):
        state["jtt_notice"] = "Work Kit을 안전하게 삭제하지 못했습니다. 템플릿 상태를 다시 확인하세요."
    state["jtt_notice_until"] = 0.0


def _archive_menu(stdscr, state: dict) -> None:
    """Keep saved configurations and Work Kits together in one UI only."""
    if state.get("work_kit_preview") is not None:
        state["jtt_notice"] = "Work Kit 미리보기 중입니다 · Window를 선택하면 작업공간으로 돌아갑니다."
        state["jtt_notice_until"] = 0.0
        return
    workspace_read_ok = True
    try:
        saved = [item for item in state["jtt_store"].workspaces()
                 if item["id"] != JTTWorkspaceStore.DEFAULT_ID and item.get("saved")]
    except (KeyError, OSError, RuntimeError, ValueError, TypeError, WorkspaceStoreError):
        saved = []
        workspace_read_ok = False
        state["jtt_notice"] = "저장 작업공간을 읽을 수 없습니다 · 기존 데이터는 변경하지 않았습니다."
        state["jtt_notice_until"] = 0.0
    if not workspace_read_ok:
        return
    kit_read_ok = True
    try:
        kits = state["jtt_work_kit_store"].kits()
        state["jtt_work_kits"] = kits
        state["jtt_work_kit_error"] = False
    except (KeyError, OSError, RuntimeError, ValueError, TypeError, WorkKitStoreError):
        kits = []
        kit_read_ok = False
        state["jtt_work_kit_error"] = True

    active_workspace_id = str(state.get("workspace_id") or "")
    active_saved_workspace = next((item for item in saved
                                   if str(item.get("id") or "") == active_workspace_id), None)
    actions = []
    if active_saved_workspace is not None:
        actions.append(("return_default", "내 작업공간으로 돌아가기"))
    actions.extend([
        ("save", "현재 터미널 구성 저장"),
        ("load_workspace", "저장한 작업공간 불러오기"),
        ("rename_workspace", "저장한 작업공간 이름 변경"),
        ("delete_workspace", "저장한 작업공간 삭제"),
    ])
    if kit_read_ok:
        actions.append(("create_kit", "Work Kit 템플릿 만들기"))
    if kits:
        actions.extend([
            ("preview_kit", "Work Kit 미리보기"),
            ("rename_kit", "Work Kit 이름 변경"),
            ("delete_kit", "Work Kit 삭제"),
        ])
    result = run_list_picker(
        stdscr, "보관함", actions, centered=True,
        footer_hint="저장 구성은 세션 참조 · Work Kit은 실행되지 않는 템플릿입니다",
    )
    if result.cancelled or result.selected_key is None:
        return
    action = result.selected_key
    if action == "return_default":
        try:
            current_workspaces = state["jtt_store"].workspaces()
            current_active = [item for item in current_workspaces
                              if str(item.get("id") or "") == active_workspace_id]
            default_matches = [item for item in current_workspaces
                               if str(item.get("id") or "") == JTTWorkspaceStore.DEFAULT_ID]
            if (str(state.get("workspace_id") or "") != active_workspace_id
                    or len(current_active) != 1 or not current_active[0].get("saved")
                    or len(default_matches) != 1):
                raise WorkspaceStoreError("Active workspace changed before return")
            _attach_choice(state, {"kind": "workspace", "id": JTTWorkspaceStore.DEFAULT_ID})
            if state.get("workspace_id") != JTTWorkspaceStore.DEFAULT_ID:
                raise WorkspaceStoreError("Default workspace could not be attached")
            state["active_panel"] = 0
            state["jtt_notice"] = "내 작업공간으로 돌아왔습니다 · 기본 Window의 저장 참조를 표시합니다."
        except (KeyError, OSError, RuntimeError, ValueError, TypeError, WorkspaceStoreError):
            state["jtt_notice"] = "내 작업공간으로 돌아가지 못했습니다 · 셸 종료 명령은 실행하지 않았습니다."
        state["jtt_notice_until"] = 0.0
        return
    if action == "save":
        _save_configuration(stdscr, state)
        return
    if action == "create_kit":
        _create_work_kit(stdscr, state)
        return
    if action in {"load_workspace", "rename_workspace", "delete_workspace"}:
        if not saved:
            state["jtt_notice"] = "저장된 작업공간이 없습니다."
            state["jtt_notice_until"] = 0.0
            return
        selected_id = _pick_archive_record(stdscr, "저장한 작업공간", saved)
        if selected_id is None:
            return
        target = next((item for item in saved if item["id"] == selected_id), None)
        if target is None:
            return
        if action == "load_workspace":
            _attach_choice(state, {"kind": "workspace", "id": selected_id})
            state["active_panel"] = 0
            state["jtt_notice"] = f"저장한 작업공간 · {target['name']}"
            state["jtt_notice_until"] = 0.0
        elif action == "rename_workspace":
            name = prompt_text(stdscr, "작업공간 새 이름: ", initial=target["name"])
            if name is None or name == target["name"]:
                return
            try:
                current = next((item for item in state["jtt_store"].workspaces()
                                if item["id"] == selected_id), None)
                if current != target:
                    raise WorkspaceStoreError("Workspace changed before rename")
                state["jtt_store"].rename_workspace(
                    selected_id, name, expected_snapshot=target,
                )
                _refresh_workspaces(state, selected_id if state["workspace_id"] == selected_id
                                    else state["workspace_id"])
                state["jtt_notice"] = "저장한 작업공간 이름을 변경했습니다 · 터미널 참조는 유지됩니다."
            except WorkspaceStoreCommittedDurabilityError as exc:
                _refresh_workspaces(state, exc.record_id)
                state["jtt_notice"] = "이름 변경 반영됨 · 디렉터리 동기화 확인 필요"
            except (KeyError, OSError, RuntimeError, ValueError, TypeError, WorkspaceStoreError):
                state["jtt_notice"] = "작업공간을 변경하지 못했습니다 · 데이터를 다시 확인하세요."
            state["jtt_notice_until"] = 0.0
        else:
            _confirm_and_delete_workspace(stdscr, state, selected_id)
        return
    if action in {"preview_kit", "rename_kit", "delete_kit"}:
        if state.get("jtt_work_kit_error"):
            state["jtt_notice"] = "Work Kit 저장 데이터 오류 · 기존 파일은 변경하지 않았습니다."
            state["jtt_notice_until"] = 0.0
            return
        selected_id = _pick_archive_record(stdscr, "Work Kit", kits)
        if selected_id is None:
            return
        target = next((item for item in kits if item["id"] == selected_id), None)
        if target is None:
            return
        if action == "preview_kit":
            choice = {"kind": "work_kit", "id": selected_id}
            _preview_work_kit(state, choice)
            state["active_panel"] = 0
            return
        if action == "rename_kit":
            name = prompt_text(stdscr, "Work Kit 새 이름: ", initial=target["name"])
            if name is None or name == target["name"]:
                return
            try:
                current = next((item for item in state["jtt_work_kit_store"].kits()
                                if item["id"] == selected_id), None)
                if current != target:
                    raise WorkKitStoreError("Work Kit changed before rename")
                state["jtt_work_kit_store"].rename_kit(
                    selected_id, name, expected_snapshot=target,
                )
                state["jtt_notice"] = "Work Kit 이름을 변경했습니다 · 템플릿 구성은 유지됩니다."
            except WorkKitStoreCommittedDurabilityError:
                state["jtt_notice"] = "Work Kit 이름 변경 반영됨 · 디렉터리 동기화 확인 필요"
            except (KeyError, OSError, RuntimeError, ValueError, TypeError, WorkKitStoreError):
                state["jtt_notice"] = "Work Kit 이름을 변경하지 못했습니다 · 템플릿을 다시 확인하세요."
            state["jtt_notice_until"] = 0.0
            return
        _confirm_and_delete_kit_from_archive(stdscr, state, target)


def _pick_archive_record(stdscr, title: str, records: list[dict]) -> str | None:
    labels = []
    for record in records:
        label = str(record["name"])
        if sum(1 for item in records if str(item["name"]).casefold() == label.casefold()) > 1:
            label += (f" · {len(record.get('terminal_ids', record.get('terminals', ())))}개"
                      f" · {str(record['id'])[-4:]}")
        labels.append((str(record["id"]), label))
    result = run_list_picker(
        stdscr, title, labels, centered=True,
        footer_hint="Esc 취소 · 저장 데이터는 항목별 형식을 유지합니다",
    )
    return None if result.cancelled else result.selected_key


def _confirm_and_delete_kit_from_archive(stdscr, state: dict, target: dict) -> None:
    confirm = run_list_picker(
        stdscr, "Work Kit 삭제",
        [("cancel", "취소"), ("delete", f"{target['name']} 템플릿 삭제")],
        centered=True, footer_hint="취소가 기본 선택입니다 · 셸과 작업공간은 변경하지 않습니다",
        preamble=["Work Kit 템플릿 정의만 삭제합니다 · PTY는 종료하거나 만들지 않습니다."],
    )
    if confirm.cancelled or confirm.selected_key != "delete":
        return
    try:
        current = next((item for item in state["jtt_work_kit_store"].kits()
                        if item["id"] == target["id"]), None)
        if current != target:
            raise WorkKitStoreError("Work Kit changed before deletion")
        state["jtt_work_kit_store"].delete_kit(target["id"], expected_snapshot=target)
        state["jtt_work_kits"] = state["jtt_work_kit_store"].kits()
        state["jtt_notice"] = "Work Kit 템플릿을 삭제했습니다 · 작업공간과 터미널은 변경하지 않았습니다."
    except WorkKitStoreCommittedDurabilityError:
        state["jtt_notice"] = "Work Kit 삭제 반영됨 · 디렉터리 동기화 확인 필요"
    except (KeyError, OSError, RuntimeError, ValueError, TypeError, WorkKitStoreError):
        state["jtt_notice"] = "Work Kit을 안전하게 삭제하지 못했습니다 · 템플릿을 다시 확인하세요."
    state["jtt_notice_until"] = 0.0


def _settings_menu(stdscr, state: dict) -> None:
    actions = [("layout", "폴더 · Window 관리"), ("help", "키 안내")]
    if state.get("work_kit_preview") is None:
        row = renderer._selected_row(state)
        if row and row.get("terminal_kind") == "tower_managed":
            if _capture_card_reference_target(state) is not None:
                actions.append(("cleanup_card", "선택한 터미널 카드 참조 정리"))
            if _capture_stop_target(state) is not None:
                actions.append(("stop", "선택한 터미널 종료"))
    result = run_list_picker(
        stdscr, "설정 · 메뉴", actions,
        centered=True, footer_hint="↑↓ 이동 · Enter 선택 · Esc 취소",
    )
    if result.cancelled or result.selected_key is None:
        return
    if result.selected_key == "layout":
        _layout_menu(stdscr, state)
    elif result.selected_key == "stop":
        _confirm_and_stop_selected_terminal(stdscr, state)
    elif result.selected_key == "cleanup_card":
        _remove_card_reference(stdscr, state)
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


def _layout_menu(stdscr, state: dict) -> None:
    """Manage logical grouping metadata without starting/stopping PTYs."""
    if state.get("work_kit_preview") is not None:
        state["jtt_notice"] = "Work Kit은 템플릿입니다 · Window 배치는 실제 작업공간에서만 변경됩니다."
        state["jtt_notice_until"] = 0.0
        return
    if state.get("jtt_layout_error"):
        state["jtt_notice"] = "폴더/Window 데이터가 손상되었거나 비공개가 아닙니다 · 변경을 차단했습니다."
        state["jtt_notice_until"] = 0.0
        return
    try:
        workspace, layout, _workspaces = _workspace_layout(state)
    except (KeyError, OSError, RuntimeError, ValueError, TypeError, WorkspaceStoreError,
            JTTLayoutStoreError):
        state["jtt_notice"] = "작업공간 폴더/Window를 확인할 수 없습니다 · 변경을 차단했습니다."
        state["jtt_notice_until"] = 0.0
        return

    selected_folder = next((item for item in layout["folders"]
                            if item["id"] == state.get("selected_folder_id")),
                           layout["folders"][0])
    selected_window = next((item for item in layout["windows"]
                            if item["id"] == state.get("selected_window_id")), None)
    actions = [
        ("select_folder", "폴더 선택"),
        ("select_window", "Window 선택"),
        ("create_folder", "폴더 생성"),
        ("create_window", "현재 폴더에 Window 생성"),
    ]
    if selected_folder["id"] != "folder-default":
        actions.extend([("rename_folder", "선택 폴더 이름 변경"),
                        ("delete_folder", "선택 폴더 삭제 · 터미널은 기본 Window로 이동")])
    if selected_window and selected_window["id"] != "window-default":
        actions.extend([("rename_window", "선택 Window 이름 변경"),
                        ("delete_window", "선택 Window 삭제 · 터미널은 기본 Window로 이동")])
    current_row = renderer._selected_row(state)
    movable = bool(current_row and current_row.get("terminal_kind") == "tower_managed"
                   and len(layout["windows"]) > 1)
    if movable:
        actions.append(("move_terminal", "선택 터미널을 Window로 이동"))
    result = run_list_picker(
        stdscr, "폴더 · Window 관리", actions, centered=True,
        footer_hint="논리 그룹만 변경 · PTY와 실행 중인 셸은 유지됩니다",
        preamble=[f"작업공간: {workspace['name']}",
                  "기존 저장 작업공간과 Work Kit 데이터 형식은 변경하지 않습니다."],
    )
    if result.cancelled or result.selected_key is None:
        return
    action = result.selected_key
    if action in {"select_folder", "select_window"}:
        _pick_layout_item(stdscr, state, layout, action)
        return
    if action == "move_terminal":
        _move_selected_terminal(stdscr, state, workspace, layout)
        return
    if action in {"delete_folder", "delete_window"}:
        _delete_layout_item(stdscr, state, workspace, layout, action)
        return

    expected_workspace, expected_layout = dict(workspace), layout
    if action == "create_folder":
        name = prompt_text(stdscr, "새 폴더 이름: ")
        if not name:
            return
        try:
            if not _layout_target_unchanged(state, expected_workspace, expected_layout):
                raise JTTLayoutStoreError("Workspace layout changed")
            folder = state["jtt_layout_store"].create_folder(workspace["id"], name)
            state["jtt_notice"] = f"폴더를 만들었습니다 · {folder['name']}"
            _select_layout_target(state, "logical_folder", folder_id=folder["id"])
        except JTTLayoutStoreCommittedDurabilityError as exc:
            _reconcile_committed_layout(state, exc.workspace_id, "logical_folder", exc.item_id)
            state["jtt_notice"] = _committed_layout_notice("폴더 생성", state)
        except (KeyError, OSError, RuntimeError, ValueError, TypeError, WorkspaceStoreError,
                JTTLayoutStoreError):
            state["jtt_notice"] = "폴더를 만들지 못했습니다 · 작업공간 상태를 다시 확인하세요."
    elif action == "create_window":
        name = prompt_text(
            stdscr, "새 Window 이름: ",
            initial=_suggest_window_name(expected_layout, selected_folder["id"]),
        )
        if not name:
            return
        try:
            if not _layout_target_unchanged(state, expected_workspace, expected_layout):
                raise JTTLayoutStoreError("Workspace layout changed")
            window = state["jtt_layout_store"].create_window(
                workspace["id"], selected_folder["id"], name,
            )
            state["jtt_notice"] = f"빈 Window를 만들었습니다 · {window['name']} · 터미널은 실행하지 않았습니다."
            _select_layout_target(state, "logical_window", folder_id=window["folder_id"],
                                  window_id=window["id"])
        except JTTLayoutStoreCommittedDurabilityError as exc:
            _reconcile_committed_layout(state, exc.workspace_id, "logical_window", exc.item_id)
            state["jtt_notice"] = _committed_layout_notice("Window 생성", state)
        except (KeyError, OSError, RuntimeError, ValueError, TypeError, WorkspaceStoreError,
                JTTLayoutStoreError):
            state["jtt_notice"] = "Window를 만들지 못했습니다 · 기존 터미널은 변경하지 않았습니다."
    elif action in {"rename_folder", "rename_window"}:
        item = selected_folder if action == "rename_folder" else selected_window
        if item is None:
            return
        name = prompt_text(stdscr, f"새 이름 ({item['name']}): ")
        if not name:
            return
        try:
            if not _layout_target_unchanged(state, expected_workspace, expected_layout):
                raise JTTLayoutStoreError("Workspace layout changed")
            if action == "rename_folder":
                state["jtt_layout_store"].rename_folder(workspace["id"], item["id"], name)
                state["jtt_notice"] = "폴더 이름을 변경했습니다."
                _select_layout_target(state, "logical_folder", folder_id=item["id"])
            else:
                state["jtt_layout_store"].rename_window(workspace["id"], item["id"], name)
                state["jtt_notice"] = "Window 이름을 변경했습니다."
                _select_layout_target(state, "logical_window", folder_id=item["folder_id"],
                                      window_id=item["id"])
        except JTTLayoutStoreCommittedDurabilityError as exc:
            kind = "logical_folder" if action == "rename_folder" else "logical_window"
            _reconcile_committed_layout(state, exc.workspace_id, kind, exc.item_id)
            state["jtt_notice"] = _committed_layout_notice(
                "폴더 이름 변경" if action == "rename_folder" else "Window 이름 변경", state,
            )
        except (KeyError, OSError, RuntimeError, ValueError, TypeError, WorkspaceStoreError,
                JTTLayoutStoreError):
            state["jtt_notice"] = "이름을 변경하지 못했습니다 · 작업공간 상태를 다시 확인하세요."
    state["jtt_notice_until"] = 0.0


def _layout_target_unchanged(state: dict, expected_workspace: dict, expected_layout: dict) -> bool:
    workspace, layout, _workspaces = _workspace_layout(state, expected_workspace["id"])
    return workspace == expected_workspace and layout == expected_layout


def _select_layout_target(state: dict, kind: str, *, folder_id: str,
                          window_id: str = "") -> None:
    if kind == "logical_folder":
        try:
            workspace, layout, _workspaces = _workspace_layout(state)
        except (KeyError, OSError, RuntimeError, ValueError, TypeError, WorkspaceStoreError,
                JTTLayoutStoreError):
            state["jtt_layout_error"] = True
            return
        if any(item["id"] == folder_id for item in layout.get("folders", ())):
            _replace_workspace_layout_choices(state, workspace, layout)
            state["selected_folder_id"] = folder_id
            state["active_panel"] = 0
        return
    workspace, layout, _workspaces = _workspace_layout(state)
    choice_id = f"window:{workspace['id']}:{window_id}"
    choice = next((item for item in _layout_choices(workspace["id"], layout,
                                                    state.get("jtt_registry"))
                   if item["id"] == choice_id), None)
    if choice is None:
        state["jtt_layout_error"] = True
        state["jtt_notice"] = "변경된 Window를 다시 확인할 수 없습니다 · 화면을 갱신하세요."
        return
    _attach_layout_choice(state, choice)


def _reconcile_committed_layout(state: dict, workspace_id: str, kind: str,
                                item_id: str) -> bool:
    """Reload only after a committed write whose directory durability is uncertain."""
    try:
        workspace, layout, _workspaces = _workspace_layout(state, workspace_id)
        choice_id = (f"folder:{workspace_id}:{item_id}" if kind == "logical_folder"
                     else f"window:{workspace_id}:{item_id}")
        choices = _layout_choices(workspace_id, layout, state.get("jtt_registry"))
        choice = next((entry for entry in choices if entry["id"] == choice_id), None)
        if choice is None:
            choice = next(entry for entry in choices
                          if entry["id"] == f"window:{workspace_id}:window-default")
        _replace_workspace_layout_choices(state, workspace, layout,
                                          selected_choice_id=choice["id"])
        _attach_layout_choice(state, choice)
        if kind == "logical_folder" and any(
                folder["id"] == item_id for folder in layout["folders"]):
            state["selected_folder_id"] = item_id
            state["active_panel"] = 0
        return not state.get("jtt_layout_error", False)
    except (KeyError, OSError, RuntimeError, ValueError, TypeError, WorkspaceStoreError,
            JTTLayoutStoreError):
        _release_layout_clients(state)
        _clear_mini_live(state)
        state["jtt_layout_error"] = True
        state["panes"] = []
        state["pane_index"] = 0
        state["selected_target_id"] = ""
        state["selected_signature"] = None
        return False


def _committed_layout_notice(action: str, state: dict) -> str:
    notice = f"{action} 반영됨 · 디렉터리 동기화 확인 필요"
    if state.get("jtt_layout_error"):
        return notice + " · 최신 배치를 읽지 못했습니다 · 재시도하지 말고 다시 확인하세요"
    return notice


def _pick_layout_item(stdscr, state: dict, layout: dict, action: str) -> None:
    if action == "select_folder":
        items = [(item["id"], item["name"]) for item in layout["folders"]]
        title = "폴더 선택"
    else:
        labels = {item["window_id"]: item["label"] for item in
                  _layout_choices(state["workspace_id"], layout, state.get("jtt_registry"))}
        items = [(item["id"], labels.get(item["id"], item["name"]))
                 for item in layout["windows"]]
        title = "Window 선택"
    result = run_list_picker(stdscr, title, items, centered=True,
                             footer_hint="선택은 표시 그룹만 변경 · 터미널은 종료하지 않습니다")
    if result.cancelled or result.selected_key is None:
        return
    current = next((item for item in layout["folders"] if item["id"] == result.selected_key), None) \
        if action == "select_folder" else next(
            (item for item in layout["windows"] if item["id"] == result.selected_key), None
        )
    if current is None:
        return
    if action == "select_folder":
        state["selected_folder_id"] = current["id"]
        state["jtt_notice"] = f"폴더 선택 · 새 Window 위치: {current['name']}"
        state["jtt_notice_until"] = 0.0
        return
    choice = next((item for item in _layout_choices(
        state["workspace_id"], layout, state.get("jtt_registry"))
        if item.get("kind") == "logical_window" and item.get("window_id") == current["id"]), None)
    if choice is not None:
        _attach_layout_choice(state, choice)


def _delete_layout_item(stdscr, state: dict, workspace: dict, layout: dict, action: str) -> None:
    is_folder = action == "delete_folder"
    item_id = str(state.get("selected_folder_id") if is_folder else state.get("selected_window_id") or "")
    items = layout["folders"] if is_folder else layout["windows"]
    item = next((entry for entry in items if entry["id"] == item_id), None)
    if item is None or item_id in ({"folder-default"} if is_folder else {"window-default"}):
        state["jtt_notice"] = "기본 폴더/Window는 삭제할 수 없습니다."
        return
    windows = ([entry for entry in layout["windows"] if entry["folder_id"] == item_id]
               if is_folder else [item])
    moved = list(dict.fromkeys(session_id for entry in windows for session_id in entry["terminal_ids"]))
    title = "폴더 삭제" if is_folder else "Window 삭제"
    label = f"{item['name']} 삭제 · 터미널 {len(moved)}개는 기본 Window로 이동"
    confirm = run_list_picker(
        stdscr, title, [("cancel", "취소"), ("delete", label)], centered=True,
        footer_hint="취소가 기본 선택입니다 · PTY·셸은 종료하지 않습니다",
        preamble=["해당 작업공간의 논리 배치만 삭제하고 세션 참조는 기본 복구 Window에 남깁니다."],
    )
    if confirm.cancelled or confirm.selected_key != "delete":
        return
    try:
        if not _layout_target_unchanged(state, workspace, layout):
            raise JTTLayoutStoreError("Workspace layout changed")
        if is_folder:
            state["jtt_layout_store"].delete_folder(workspace["id"], item_id)
        else:
            state["jtt_layout_store"].delete_window(workspace["id"], item_id)
        _select_layout_target(state, "logical_window", folder_id="folder-default",
                              window_id="window-default")
        state["jtt_notice"] = f"{title} 완료 · {len(moved)}개 터미널을 기본 복구 Window에 보존했습니다."
    except JTTLayoutStoreCommittedDurabilityError as exc:
        _reconcile_committed_layout(
            state, exc.workspace_id, "logical_window", "window-default",
        )
        state["jtt_notice"] = _committed_layout_notice(title, state)
    except (KeyError, OSError, RuntimeError, ValueError, TypeError, WorkspaceStoreError,
            JTTLayoutStoreError):
        state["jtt_notice"] = "배치를 안전하게 삭제하지 못했습니다 · 터미널은 종료하지 않았습니다."
    state["jtt_notice_until"] = 0.0


def _move_selected_terminal(stdscr, state: dict, workspace: dict, layout: dict) -> None:
    row = renderer._selected_row(state)
    identity = (row or {}).get("managed_identity") or {}
    session_id = str(identity.get("session_id") or "")
    if (not session_id or str((row or {}).get("managed_id") or "") != session_id
            or session_id not in workspace["terminal_ids"]):
        state["jtt_notice"] = "선택 터미널을 현재 작업공간에서 확인할 수 없습니다."
        return
    items = [(item["id"], f"{item['folder_name']} / {item['name']}") for item in layout["windows"]]
    result = run_list_picker(
        stdscr, f"{row.get('display_name', '터미널')} 이동", items, centered=True,
        footer_hint="논리 배치만 변경 · 터미널 프로세스는 유지됩니다",
    )
    if result.cancelled or result.selected_key is None:
        return
    try:
        if not _layout_target_unchanged(state, workspace, layout):
            raise JTTLayoutStoreError("Workspace layout changed")
        current_workspace, current_layout, _ = _workspace_layout(state, workspace["id"])
        if session_id not in current_workspace["terminal_ids"]:
            raise JTTLayoutStoreError("Terminal reference changed")
        destination = next((item for item in current_layout["windows"]
                            if item["id"] == result.selected_key), None)
        if destination is None:
            raise JTTLayoutStoreError("Destination Window changed")
        state["jtt_layout_store"].move_terminal(
            workspace["id"], session_id, destination["id"], current_workspace["terminal_ids"],
        )
        _select_layout_target(state, "logical_window", folder_id=destination["folder_id"],
                              window_id=destination["id"])
        state["pane_index"] = next((index for index, card in enumerate(state["panes"])
                                     if str(card.get("managed_id") or "") == session_id), 0)
        if state["panes"]:
            selected = state["panes"][state["pane_index"]]
            state["selected_target_id"] = str(selected.get("target_id") or "")
            state["selected_signature"] = renderer._pane_signature(selected)
        state["jtt_notice"] = "터미널을 이동했습니다 · 실행 중인 PTY는 유지됩니다."
    except JTTLayoutStoreCommittedDurabilityError as exc:
        _reconcile_committed_layout(
            state, exc.workspace_id, "logical_window", destination["id"],
        )
        state["jtt_notice"] = _committed_layout_notice("터미널 이동", state)
    except (KeyError, OSError, RuntimeError, ValueError, TypeError, WorkspaceStoreError,
            JTTLayoutStoreError):
        state["jtt_notice"] = "터미널 배치를 변경하지 못했습니다 · 실행 중인 세션은 유지됩니다."
    state["jtt_notice_until"] = 0.0


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
    target = _capture_card_reference_target(state)
    return target if target and target["classification"] == "exited" else None


def _confirm_and_remove_exited_card(stdscr, state: dict) -> None:
    """Remove a confirmed-exited card through the recoverable ref-only path."""
    _remove_card_reference(stdscr, state, confirmed_exited=True)


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
    except WorkspaceStoreCommittedDurabilityError:
        binding = state["managed_terminals"].pop(session_id, None)
        if binding:
            binding.close()
        _refresh_workspaces(state, workspace_id)
        state["jtt_notice"] = (
            "선택 터미널 종료 및 카드 제거 반영됨 · 디렉터리 동기화 확인 필요"
        )
    except (KeyError, OSError, RuntimeError, ValueError, TypeError, WorkspaceStoreError):
        state["jtt_notice"] = "선택한 터미널을 종료하지 못했습니다. 작업 상태는 다시 확인하세요."
    else:
        binding = state["managed_terminals"].pop(session_id, None)
        if binding:
            binding.close()
        _refresh_workspaces(state, workspace_id)
        state["jtt_notice"] = "선택한 터미널을 종료했습니다."
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
        placement_notice = _place_created_terminal(state, workspace_id, session_id)
    except WorkspaceStoreCommittedDurabilityError:
        placement_notice = _place_created_terminal(state, state.get("workspace_id", ""), session_id)
        _select_created_terminal(state, session_id)
        state["jtt_notice"] = (
            "새 로컬 셸 및 작업공간 카드 반영됨 · 디렉터리 동기화 확인 필요"
            + placement_notice
        )
        state["jtt_notice_until"] = 0.0
        return
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
        return

    _select_created_terminal(state, session_id)
    state["jtt_notice"] = "새 로컬 셸 생성 · 화면과 입력 준비 확인 중" + placement_notice
    state["jtt_notice_until"] = 0.0


def _place_created_terminal(state: dict, workspace_id: str, session_id: str) -> str:
    """Place a newly canonical ref; failures leave it in the virtual recovery Window."""
    selected_window_id = str(state.get("selected_window_id") or "window-default")
    if selected_window_id == "window-default":
        return ""
    try:
        workspace, layout, _workspaces = _workspace_layout(state, workspace_id)
        window = next((item for item in layout["windows"]
                       if item["id"] == selected_window_id), None)
        if window is None or session_id not in workspace["terminal_ids"]:
            return " · Window 변경 확인 실패, 기본 복구 Window에 보관"
        state["jtt_layout_store"].move_terminal(
            workspace_id, session_id, selected_window_id, workspace["terminal_ids"],
        )
        return ""
    except (KeyError, OSError, RuntimeError, ValueError, TypeError, WorkspaceStoreError,
            JTTLayoutStoreError):
        return " · Window 배치 실패, 기본 복구 Window에 보관"


def _select_created_terminal(state: dict, session_id: str) -> None:
    workspace_id = state["workspace_id"]
    try:
        workspace, layout, workspaces = _workspace_layout(state, workspace_id)
    except (KeyError, OSError, RuntimeError, ValueError, TypeError, WorkspaceStoreError,
            JTTLayoutStoreError) as exc:
        raise WorkspaceStoreError("Created terminal workspace is unavailable") from exc
    target_window = next((item for item in layout["windows"]
                          if item["id"] == state.get("selected_window_id")
                          and session_id in item["terminal_ids"]), None)
    if target_window is None:
        target_window = layout["windows"][0]
    choice = next((item for item in _layout_choices(workspace_id, layout,
                                                    state.get("jtt_registry"))
                   if item.get("kind") == "logical_window"
                   and item.get("window_id") == target_window["id"]), None)
    if choice is None:
        raise WorkspaceStoreError("Created terminal recovery Window is unavailable")
    state["jtt_workspaces"] = workspaces
    _attach_layout_choice(state, choice)
    pane_index = next((index for index, row in enumerate(state["panes"])
                       if str(row.get("managed_id") or "") == session_id), None)
    if pane_index is None:
        raise WorkspaceStoreError("Created terminal reference is unavailable")
    state["pane_index"] = pane_index
    state["active_panel"] = 2
    state["center_actions_focused"] = False
    row = state["panes"][pane_index]
    state["selected_target_id"] = str(row.get("target_id") or "")
    state["selected_signature"] = renderer._pane_signature(row)
    state["jtt_pending_auto_acquire"] = {
        "session_id": session_id,
        "deadline": time.monotonic() + 2.5,
    }


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
    if choice.get("id") == "action:new-window":
        _create_logical_window(stdscr, state)
    elif choice.get("id") == "action:archive":
        _archive_menu(stdscr, state)
    elif choice.get("id") == "action:work-kit-create":
        _create_work_kit(stdscr, state)
    elif choice.get("id") == "action:save":
        _save_configuration(stdscr, state)
    elif choice.get("id") == "action:settings":
        _settings_menu(stdscr, state)
    elif choice.get("id") == "action:layout":
        _layout_menu(stdscr, state)


def _create_logical_window(stdscr, state: dict) -> None:
    """Create only logical Window metadata; never creates or connects a PTY."""
    if state.get("work_kit_preview") is not None:
        state["jtt_notice"] = (
            "Work Kit 미리보기에서는 Window를 만들 수 없습니다 · 터미널은 실행되지 않았습니다."
        )
        state["jtt_notice_until"] = 0.0
        return
    try:
        workspace, layout, _workspaces = _workspace_layout(state)
    except (KeyError, OSError, RuntimeError, ValueError, TypeError, WorkspaceStoreError,
            JTTLayoutStoreError):
        state["jtt_notice"] = "작업공간을 확인할 수 없어 새 Window를 만들지 않았습니다."
        state["jtt_notice_until"] = 0.0
        return
    folders = {str(item["id"]): item for item in layout["folders"]}
    folder_id = str(state.get("selected_folder_id") or "")
    if folder_id not in folders:
        selected = next((item for item in layout["windows"]
                         if item["id"] == state.get("selected_window_id")), None)
        folder_id = str((selected or {}).get("folder_id") or "folder-default")
    if folder_id not in folders:
        folder_id = str(layout["folders"][0]["id"])
    suggested = _suggest_window_name(layout, folder_id)
    name = prompt_text(stdscr, "새 Window 이름: ", initial=suggested)
    if not name:
        state["jtt_notice"] = "Window 생성을 취소했습니다 · 터미널은 실행되지 않았습니다."
        state["jtt_notice_until"] = 0.0
        return
    try:
        if not _layout_target_unchanged(state, workspace, layout):
            raise JTTLayoutStoreError("Workspace layout changed")
        created = state["jtt_layout_store"].create_window(workspace["id"], folder_id, name)
        _select_layout_target(state, "logical_window", folder_id=created["folder_id"],
                              window_id=created["id"])
        state["active_panel"] = 0
        state["jtt_notice"] = (
            f"Window 생성 완료 · {created['name']} · 빈 논리 공간 · 터미널은 생성되지 않았습니다."
        )
    except JTTLayoutStoreCommittedDurabilityError as exc:
        _reconcile_committed_layout(state, exc.workspace_id, "logical_window", exc.item_id)
        state["jtt_notice"] = _committed_layout_notice("Window 생성", state)
    except (KeyError, OSError, RuntimeError, ValueError, TypeError, WorkspaceStoreError,
            JTTLayoutStoreError):
        state["jtt_notice"] = "Window를 만들지 못했습니다 · 터미널은 생성되지 않았습니다."
    state["jtt_notice_until"] = 0.0


def _suggest_window_name(layout: dict, folder_id: str) -> str:
    used_names = {str(item["name"]).casefold() for item in layout["windows"]
                  if str(item["folder_id"]) == str(folder_id)}
    suffix = 2
    while f"window {suffix}".casefold() in used_names:
        suffix += 1
    return f"Window {suffix}"


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
    state["mini_live_sampler"] = _MiniLiveSampler()
    renderer._setup_colors()
    keypad = getattr(stdscr, "keypad", None)
    if keypad:
        keypad(True)
    stdscr.timeout(80)
    previous_mouse = renderer._enable_mouse()
    try:
        while True:
            _refresh_and_resize(state, stdscr)
            _refresh_mini_live(state, stdscr)
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
        sampler = state.get("mini_live_sampler")
        if sampler is not None:
            sampler.close()
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
