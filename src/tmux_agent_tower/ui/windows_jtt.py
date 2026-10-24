"""Native Windows three-panel JTT UI over ConPTY and shared JTT state stores.

This screen uses the Windows terminal registry and common versioned JTT
workspace/layout schemas with Windows ACL checks. It does not import the POSIX
curses UI or POSIX PTY backend.
"""

from __future__ import annotations

import asyncio
import re
import sys
import time
from typing import Any

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, ScrollableContainer, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Static
from rich.style import Style
from rich.text import Text

from ..terminal.manager import TerminalError, TerminalIdentity, TerminalScreen
from ..state.jtt_layouts import JTTLayoutStore, JTTLayoutStoreError
from ..state.jtt_workspaces import (
    JTTWorkspaceStore,
    WorkspaceStoreError,
    default_jtt_root,
)
from .windows_mini_live import WindowsMiniLiveSampler, identity_key as _mini_identity_key


_COLOR_NAMES = {
    "black": "black", "red": "red", "green": "green", "brown": "yellow",
    "yellow": "yellow", "blue": "blue", "magenta": "magenta", "cyan": "cyan",
    "white": "white", "brightblack": "bright_black", "brightred": "bright_red",
    "brightgreen": "bright_green", "brightyellow": "bright_yellow",
    "brightblue": "bright_blue", "brightmagenta": "bright_magenta",
    "brightcyan": "bright_cyan", "brightwhite": "bright_white",
}
_HEX_COLOR = re.compile(r"#[0-9a-fA-F]{6}\Z")
_KEY_BYTES = {
    "enter": b"\r", "return": b"\r", "backspace": b"\x7f", "tab": b"\t",
    "shift+tab": b"\x1b[Z", "escape": b"\x1b", "up": b"\x1b[A",
    "down": b"\x1b[B", "right": b"\x1b[C", "left": b"\x1b[D",
    "home": b"\x1b[H", "end": b"\x1b[F", "delete": b"\x1b[3~",
    "insert": b"\x1b[2~", "pageup": b"\x1b[5~", "pagedown": b"\x1b[6~",
}


def _rich_color(value: str) -> str | None:
    lowered = str(value).lower()
    if lowered in ("", "default", "none"):
        return None
    if lowered in _COLOR_NAMES:
        return _COLOR_NAMES[lowered]
    if _HEX_COLOR.fullmatch(str(value)):
        return str(value)
    return None


def _screen_text(screen: TerminalScreen | None, columns: int, lines: int) -> Text:
    """Build a bounded Rich view without persisting or mutating VT cells."""

    if screen is None:
        return Text("화면 동기화를 기다리는 중…", style="dim")
    width = max(0, min(int(columns), max((len(row) for row in screen.rows), default=0)))
    height = max(0, min(int(lines), len(screen.rows)))
    output = Text(no_wrap=True, overflow="crop")
    for y, row in enumerate(screen.rows[:height]):
        limit = min(width, len(row))
        for x, cell in enumerate(row[:limit]):
            # pyte represents the trailing cell of a wide glyph with an empty
            # string. Emitting a space here would visibly split Korean and
            # other double-width characters in the RIGHT view.
            if not cell.text:
                continue
            value = cell.text
            style = Style(
                color=_rich_color(cell.foreground),
                bgcolor=_rich_color(cell.background),
                bold=cell.bold,
                italic=cell.italic,
                underline=cell.underline,
                reverse=cell.reverse or (
                    screen.cursor_visible and x == screen.cursor_x and y == screen.cursor_y
                ),
                blink=cell.blink,
                strike=cell.strikethrough,
            )
            output.append(value, style=style)
        if y + 1 < height:
            output.append("\n")
    return output


def _same_identity(left: Any, right: Any) -> bool:
    fields = (
        "session_id", "generation", "supervisor_pid", "supervisor_start",
        "shell_pid", "shell_start",
    )
    return all(getattr(left, field, None) == getattr(right, field, None) for field in fields)


def _key_bytes(event) -> bytes | None:
    key = str(event.key).lower()
    special = _KEY_BYTES.get(key)
    if special is not None:
        return special
    if key.startswith("ctrl+") and len(key) == 6 and "a" <= key[-1] <= "z":
        return bytes((ord(key[-1]) - ord("a") + 1,))
    if key in ("ctrl+space", "ctrl+@"):  # NUL is meaningful to some full-screen programs.
        return b"\x00"
    character = getattr(event, "character", None)
    if isinstance(character, str) and character:
        try:
            return character.encode("utf-8", errors="strict")
        except UnicodeEncodeError:
            return None
    return None


class TerminalView(Static):
    """Clickable RIGHT terminal surface; a click requests an input lease."""

    can_focus = True

    async def on_click(self, event) -> None:
        event.stop()
        await self.app.enter_input_mode()


class ConfirmStopScreen(ModalScreen[bool]):
    """Cancel-first confirmation for stopping one identity-checked session."""

    CSS = """
    ConfirmStopScreen { align: center middle; background: $background 70%; }
    #stop-dialog { width: 56; height: auto; max-height: 10; border: round $primary;
                   background: $surface; padding: 1 2; }
    #stop-actions { height: 3; align-horizontal: right; }
    #stop-actions Button { margin-left: 1; }
    """

    def __init__(self, terminal_name: str):
        super().__init__()
        self.terminal_name = terminal_name

    def compose(self) -> ComposeResult:
        with Vertical(id="stop-dialog"):
            yield Static(f"'{self.terminal_name}' 터미널을 종료할까요?\n선택한 세션만 종료합니다.")
            with Horizontal(id="stop-actions"):
                yield Button("취소", id="cancel-stop", variant="primary")
                yield Button("터미널 종료", id="confirm-stop", variant="error")

    def on_mount(self) -> None:
        self.query_one("#cancel-stop", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "confirm-stop":
            self.dismiss(True)
        elif event.button.id == "cancel-stop":
            self.dismiss(False)

    def on_key(self, event) -> None:
        if str(event.key).lower() == "escape":
            event.stop()
            self.dismiss(False)


class CreateWindowScreen(ModalScreen[str | None]):
    """Collect a logical Window name; it never creates a PTY."""

    CSS = """
    CreateWindowScreen { align: center middle; background: $background 70%; }
    #window-dialog { width: 56; height: auto; max-height: 10; border: round $primary;
                     background: $surface; padding: 1 2; }
    #window-actions { height: 3; align-horizontal: right; }
    #window-actions Button { margin-left: 1; }
    """

    def __init__(self, suggested_name: str):
        super().__init__()
        self.suggested_name = suggested_name

    def compose(self) -> ComposeResult:
        with Vertical(id="window-dialog"):
            yield Static("새 논리 Window 이름\n터미널은 자동 생성되지 않습니다.")
            yield Input(value=self.suggested_name, max_length=64, id="window-name")
            with Horizontal(id="window-actions"):
                yield Button("취소", id="cancel-window")
                yield Button("Window 만들기", id="confirm-window", variant="primary")

    def on_mount(self) -> None:
        self.query_one("#window-name", Input).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "confirm-window":
            self.dismiss(self.query_one("#window-name", Input).value)
        elif event.button.id == "cancel-window":
            self.dismiss(None)

    def on_key(self, event) -> None:
        if str(event.key).lower() == "escape":
            event.stop()
            self.dismiss(None)


class WindowsJTTApp(App[None]):
    """Windows terminal UI backed only by a supplied ConPTY registry."""

    TITLE = "JTT — Ju0 Terminal Tower"
    BINDINGS = [
        Binding("ctrl+g", "prefix", show=False, priority=True),
        Binding("ctrl+]", "input_escape", show=False, priority=True),
        Binding("ctrl+c", "send_ctrl_c", show=False, priority=True),
        Binding("ctrl+q", "send_ctrl_q", show=False, priority=True),
        Binding("tab", "panel_next", show=False, priority=True),
        Binding("shift+tab", "panel_previous", show=False, priority=True),
        Binding("enter", "enter", show=False, priority=True),
        Binding("backspace", "backspace", show=False, priority=True),
    ]
    CSS = """
    Screen { background: #08111f; color: #dbe8ed; }
    #panels { height: 1fr; width: 100%; }
    .panel { height: 1fr; border: round #24515a; padding: 0 1; }
    #left { width: 22%; min-width: 15; }
    #center { width: 34%; min-width: 21; }
    #right { width: 44%; min-width: 26; }
    .panel-title { height: 1; color: #75d9d0; text-style: bold; }
    #brand { height: 2; color: #91ece2; text-style: bold; }
    #workspace { height: 3; border: round #355d68; padding: 0 1; margin-top: 1; }
    #window-list { height: 1fr; margin-top: 1; }
    .window-item { width: 100%; height: 3; margin-bottom: 1; }
    .window-item.selected { border: round #35cbbd; background: #102a35; }
    #new-window { width: 100%; height: 3; }
    #terminal-summary { height: 1; color: #a8b7c3; }
    #current-window { height: 1; color: #75d9d0; text-style: bold; }
    #new-terminal { width: 100%; height: 3; margin-top: 1; }
    #terminal-cards { height: 1fr; }
    .card-row { width: 100%; height: 6; margin-bottom: 1; }
    .terminal-card { width: 1fr; height: 6; border: round #355d68; content-align: left middle; }
    .terminal-card.selected { border: round #35cbbd; background: #102a35; }
    #live { height: 1fr; width: 100%; overflow-x: hidden; overflow-y: hidden;
            border: round #294550; padding: 0 1; background: #050b13; }
    #terminal-status { height: 1; color: #96aab5; }
    #statusline { height: 1; color: #c4d2d8; background: #10212a; padding: 0 1; }
    """

    def __init__(self, registry, *, auto_select: bool = True,
                 workspace_store: JTTWorkspaceStore | None = None,
                 layout_store: JTTLayoutStore | None = None):
        super().__init__()
        self.registry = registry
        self.auto_select = auto_select
        self.workspace_store = workspace_store
        self.layout_store = layout_store
        self.workspace_id = (workspace_store.DEFAULT_ID if workspace_store is not None
                             else "workspace-default")
        self.windows = [{"id": "window-default", "name": "기본 Window",
                         "folder_id": "folder-default", "terminal_ids": []}]
        self.selected_window_id = "window-default"
        self._canonical_terminal_ids: list[str] = []
        self._runtime_terminal_placements: dict[str, str] = {}
        self._registry_sessions: dict[str, TerminalIdentity] = {}
        self.sessions: dict[str, TerminalIdentity] = {}
        self.selected_id: str | None = None
        self.selected_identity: TerminalIdentity | None = None
        self.client = None
        self.input_mode = False
        self.prefix_pending = False
        self._prefix_generation = 0
        self._panel_index = 1
        self._io_lock = asyncio.Lock()
        self._busy = False
        self._notice = ""
        self._notice_until = 0.0
        self._card_signature = None
        self._mini_sampler: WindowsMiniLiveSampler | None = None
        self._mini_cache: dict[str, tuple[tuple, int, float, TerminalScreen]] = {}
        self._right_observation: tuple[tuple, int, float, TerminalScreen] | None = None
        self._mini_last_submit = 0.0

    def compose(self) -> ComposeResult:
        with Horizontal(id="panels"):
            with Vertical(id="left", classes="panel"):
                yield Static("JTT · JU0 TERMINAL TOWER", id="brand")
                yield Static("작업공간", classes="panel-title")
                yield Static("내 작업공간\nWindows · 로컬", id="workspace")
                yield Static("Windows", classes="panel-title")
                with ScrollableContainer(id="window-list"):
                    yield Static("Window 목록을 불러오는 중", id="empty-windows", classes="dim")
                yield Button("+ 새 Window", id="new-window")
            with Vertical(id="center", classes="panel"):
                yield Static("기본 Window", id="current-window", classes="panel-title")
                yield Static("세션을 불러오는 중", id="terminal-summary")
                yield Button("+ 새 터미널", id="new-terminal", variant="primary")
                with ScrollableContainer(id="terminal-cards"):
                    yield Static("등록된 터미널이 없습니다.", id="empty-cards", classes="dim")
            with Vertical(id="right", classes="panel"):
                yield Static("LIVE · 선택한 터미널", classes="panel-title")
                yield TerminalView("터미널을 선택하거나 새로 만드세요.", id="live")
                yield Static("관찰 중", id="terminal-status")
        yield Static("탐색 · Ctrl+G n 새 터미널 · Tab 패널 이동 · Enter 입력 · Ctrl+G d 분리", id="statusline")

    async def on_mount(self) -> None:
        self._mini_sampler = WindowsMiniLiveSampler()
        self.set_interval(1.0, self._refresh_sessions)
        self.set_interval(0.2, self._refresh_live)
        self.set_interval(0.25, self._refresh_mini_live)
        await self._refresh_sessions()

    async def on_unmount(self) -> None:
        await self._close_selected_client()
        if self._mini_sampler is not None:
            self._mini_sampler.close()
            self._mini_sampler = None

    async def _backend_call(self, function, *args, **kwargs):
        async with self._io_lock:
            return await asyncio.to_thread(lambda: function(*args, **kwargs))

    def _set_notice(self, text: str, duration: float = 4.0) -> None:
        self._notice = str(text)
        self._notice_until = asyncio.get_running_loop().time() + duration
        self._update_status()

    def _update_status(self) -> None:
        if not self.is_mounted:
            return
        if self._notice and asyncio.get_running_loop().time() < self._notice_until:
            message = self._notice
        elif self.prefix_pending:
            message = "명령 대기 · n 새 터미널 · h/l 카드 · w 공간 · d 분리 · x 종료 · Esc 취소"
        elif self.input_mode:
            message = "RIGHT 입력 중 · Ctrl+] 탐색 복귀 · Ctrl+G 명령 · Ctrl+G Ctrl+G 전달"
        else:
            message = "탐색 · Ctrl+G n 새 터미널 · Tab 패널 이동 · Enter 입력 · Ctrl+G d 분리"
        self.query_one("#statusline", Static).update(message)
        status = "입력 권한 보유" if self.input_mode else "관찰 중"
        if self.selected_identity is None:
            status = "터미널을 선택하세요"
        elif self.selected_identity.state != "running":
            status = self._state_label(self.selected_identity.state)
        self.query_one("#terminal-status", Static).update(status)

    @staticmethod
    def _state_label(state: str) -> str:
        return {
            "running": "실행 중", "exited": "셸 종료", "orphaned": "연결 확인 필요",
            "identity_mismatch": "신원 불일치",
        }.get(str(state), "상태 확인 필요")

    async def _refresh_sessions(self) -> None:
        # Session refresh updates widgets on the base screen. Like LIVE
        # refresh, defer it while a modal owns the active screen so polling
        # cannot query or mutate the hidden widget tree.
        if self._busy or isinstance(self.screen, ModalScreen):
            return
        try:
            values = await self._backend_call(self.registry.list_sessions)
        except Exception as exc:
            self._registry_sessions = {}
            self.sessions = {}
            self._mini_cache.clear()
            self._right_observation = None
            await self._close_selected_client()
            self.selected_identity = None
            self._clear_live("Supervisor 연결 실패 · 세션 목록을 읽을 수 없습니다.")
            self._set_notice(f"Supervisor 연결 실패: {exc}", 8.0)
            await self._render_cards()
            return

        self._registry_sessions = {item.session_id: item for item in sorted(
            values, key=lambda item: (item.name.casefold(), item.session_id)
        )}
        try:
            await self._apply_window_projection()
            self._prune_mini_cache()
        except Exception as exc:
            self.sessions = {}
            self._mini_cache.clear()
            self._right_observation = None
            await self._close_selected_client()
            self.selected_id = None
            self.selected_identity = None
            self.input_mode = False
            self._clear_live("Window 저장 상태를 확인할 수 없습니다.")
            self._set_notice(f"Window 목록 연결 실패: {exc}", 8.0)
            await self._render_windows()
            await self._render_cards()
            return

        selection_invalidated = False
        if self.selected_id is not None:
            current = self.sessions.get(self.selected_id)
            if current is None or (self.selected_identity is not None
                                   and not _same_identity(self.selected_identity, current)):
                await self._close_selected_client()
                self.selected_id = None
                self.selected_identity = None
                self.input_mode = False
                selection_invalidated = True
                self._clear_live("선택 세션 신원이 바뀌었습니다. 다시 선택하세요.")
                self._set_notice("세션 신원 변경 · 입력을 차단했습니다.")
            elif current is not None:
                self.selected_identity = current

        if self.selected_id is None and self.auto_select and not selection_invalidated:
            candidate = next((item for item in self.sessions.values()
                              if item.state == "running"), None)
            if candidate is not None:
                await self._select_session(candidate, focus=False)

        await self._render_windows()
        await self._render_cards()
        self._update_status()

    async def _apply_window_projection(self) -> None:
        """Project persisted workspace placements onto verified registry sessions."""
        if self.workspace_store is None or self.layout_store is None:
            self.sessions = dict(self._registry_sessions)
            self._canonical_terminal_ids = list(self.sessions)
            if not any(item["id"] == self.selected_window_id for item in self.windows):
                self.selected_window_id = self.windows[0]["id"]
            return

        def read_projection():
            workspaces = self.workspace_store.workspaces()
            workspace = next((item for item in workspaces
                              if item["id"] == self.workspace_id), None)
            if workspace is None:
                raise TerminalError("selected JTT workspace no longer exists")
            canonical = list(workspace["terminal_ids"])
            # Older Windows UI builds did not persist references. Keep those
            # already-running sessions discoverable in the default Window in
            # memory; opening the UI does not migrate or write user data.
            if self.workspace_id == self.workspace_store.DEFAULT_ID:
                canonical.extend(item for item in self._registry_sessions
                                 if item not in canonical)
            projection = self.layout_store.layout(self.workspace_id, canonical)
            return canonical, projection

        canonical, projection = await self._backend_call(read_projection)
        self._canonical_terminal_ids = canonical
        windows = [dict(item) for item in projection["windows"]]
        window_ids = {item["id"] for item in windows}
        placements = dict(projection["placements"])
        for session_id, window_id in tuple(self._runtime_terminal_placements.items()):
            if session_id not in canonical:
                self._runtime_terminal_placements.pop(session_id, None)
            elif window_id in window_ids:
                placements[session_id] = window_id
            else:
                self._runtime_terminal_placements.pop(session_id, None)

        default_window_id = next(
            item["id"] for item in windows if item["id"] == "window-default"
        )
        for window in windows:
            window["terminal_ids"] = [
                session_id for session_id in canonical
                if placements.get(session_id, default_window_id) == window["id"]
            ]
        self.windows = windows
        if self.selected_window_id not in window_ids:
            self.selected_window_id = default_window_id
        selected_window = next(item for item in windows
                               if item["id"] == self.selected_window_id)
        self.sessions = {
            session_id: self._registry_sessions[session_id]
            for session_id in selected_window["terminal_ids"]
            if session_id in self._registry_sessions
        }

    async def _render_windows(self) -> None:
        if not self.is_mounted:
            return
        container = self.query_one("#window-list", ScrollableContainer)
        await container.remove_children()
        if not self.windows:
            await container.mount(Static("Window가 없습니다.", id="empty-windows"))
            return
        for window in self.windows:
            selected = window["id"] == self.selected_window_id
            label = Text(f"{window['name']}\n터미널 {len(window['terminal_ids'])}개")
            button = Button(
                label,
                id="window-" + window["id"],
                classes="window-item selected" if selected else "window-item",
                variant="primary" if selected else "default",
            )
            await container.mount(button)
        self.query_one("#current-window", Static).update(
            next(item["name"] for item in self.windows
                 if item["id"] == self.selected_window_id)
        )

    async def _render_cards(self) -> None:
        if not self.is_mounted:
            return
        values = list(self.sessions.values())
        now = time.monotonic()
        signature = tuple(
            (item.session_id, item.generation, item.shell_pid, item.state, item.name,
             item.session_id == self.selected_id,
             self._mini_signature(item, now))
            for item in values
        ) + (("window", self.selected_window_id, self.size.width, self.size.height),)
        if signature == self._card_signature:
            return
        self._card_signature = signature
        container = self.query_one("#terminal-cards", ScrollableContainer)
        await container.remove_children()
        if not values:
            await container.mount(Static("이 Window에는 터미널이 없습니다.\n"
                                         "+ 새 터미널을 선택하면 PTY가 생성됩니다."))
            self.query_one("#terminal-summary", Static).update("터미널 0개")
            return
        self.query_one("#terminal-summary", Static).update(f"터미널 {len(values)}개")
        compact = self.size.height < 30
        preview_lines = 1 if compact else 2
        card_height = preview_lines + 4
        card_columns = max(8, (container.size.width - 4) // 2)
        for start in range(0, len(values), 2):
            row = Horizontal(classes="card-row")
            row.styles.height = card_height
            await container.mount(row)
            for identity in values[start:start + 2]:
                selected = identity.session_id == self.selected_id
                state = self._state_label(identity.state)
                label = Text(f"{identity.name}\n{state} · PID {identity.shell_pid}")
                cached = self._fresh_mini_screen(identity, now)
                if cached is None:
                    label.append("\n미니 화면 대기 중", style="dim")
                else:
                    label.append("\n")
                    label.append(_screen_text(cached[3], card_columns, preview_lines))
                button = Button(
                    label,
                    id="terminal-" + identity.session_id,
                    classes="terminal-card selected" if selected else "terminal-card",
                    variant="primary" if selected else "default",
                )
                button.styles.height = card_height
                await row.mount(button)

    def _mini_signature(self, identity: TerminalIdentity, now: float) -> tuple:
        entry = self._mini_cache.get(identity.session_id)
        if entry is None or entry[0] != _mini_identity_key(identity):
            return ("none",)
        if identity.state != "running" or now - entry[2] > 2.5:
            return ("stale", entry[1])
        return ("live", entry[1], int((now - entry[2]) / 2.5))

    def _fresh_mini_screen(self, identity: TerminalIdentity, now: float):
        entry = self._mini_cache.get(identity.session_id)
        if (entry is None or identity.state != "running"
                or entry[0] != _mini_identity_key(identity)
                or now - entry[2] > 2.5):
            return None
        return entry

    def _prune_mini_cache(self) -> None:
        for session_id, entry in tuple(self._mini_cache.items()):
            identity = self.sessions.get(session_id)
            if (identity is None or identity.state != "running"
                    or _mini_identity_key(identity) != entry[0]):
                self._mini_cache.pop(session_id, None)
        if (self._right_observation is not None and (
                self.selected_identity is None
                or self._right_observation[0] != _mini_identity_key(self.selected_identity))):
            self._right_observation = None

    def _visible_mini_targets(self) -> tuple[TerminalIdentity, ...]:
        if not self.is_mounted:
            return ()
        container = self.query_one("#terminal-cards", ScrollableContainer)
        values = list(self.sessions.values())
        card_height = 5 if self.size.height < 30 else 6
        row_pitch = card_height + 1
        first_row = max(0, int(container.scroll_y) // row_pitch)
        viewport_height = max(1, int(container.size.height))
        last_row = max(first_row + 1,
                       (int(container.scroll_y) + viewport_height + row_pitch - 1)
                       // row_pitch)
        visible = values[first_row * 2:last_row * 2]
        return tuple(item for item in visible
                     if item.state == "running" and item.session_id != self.selected_id)[:16]

    async def _refresh_mini_live(self) -> None:
        if (not self.is_mounted or self._busy or self._mini_sampler is None
                or isinstance(self.screen, ModalScreen)):
            return
        result = self._mini_sampler.take()
        if result is not None:
            token, samples = result
            if token == self._mini_scope():
                for sample in samples:
                    identity = self.sessions.get(sample["session_id"])
                    if (identity is None
                            or sample["identity"] != _mini_identity_key(identity)):
                        continue
                    if (sample["synced"] and sample["screen"] is not None
                            and identity.state == "running"):
                        self._mini_cache[identity.session_id] = (
                            sample["identity"], int(sample["sequence"]),
                            time.monotonic(), sample["screen"],
                        )
                    else:
                        self._mini_cache.pop(identity.session_id, None)
                await self._render_cards()

        now = time.monotonic()
        if self._right_observation is not None and self.selected_identity is not None:
            key, sequence, updated, screen = self._right_observation
            if (key == _mini_identity_key(self.selected_identity)
                    and self.selected_identity.state == "running"
                    and now - updated <= 0.5):
                self._mini_cache[self.selected_identity.session_id] = (
                    key, sequence, updated, screen,
                )
        if now - self._mini_last_submit < 1.0 or self._mini_sampler.busy:
            return
        targets = self._visible_mini_targets()
        if self._mini_sampler.submit(self.registry, self._mini_scope(), targets):
            self._mini_last_submit = now
        await self._render_cards()

    def _mini_scope(self) -> tuple:
        return (self.selected_window_id,
                tuple(_mini_identity_key(item) for item in self.sessions.values()))

    async def _select_session(self, identity: TerminalIdentity, *, focus: bool = True) -> None:
        if self._busy:
            return
        self._busy = True
        try:
            await self._close_selected_client()
            self.input_mode = False
            self.selected_id = None
            self.selected_identity = None
            self._clear_live()
            visible = self.sessions.get(identity.session_id)
            if visible is None or not _same_identity(identity, visible):
                raise TerminalError("terminal is no longer in the selected Window")
            latest = await self._backend_call(self.registry.list_sessions)
            current = next((item for item in latest
                            if item.session_id == identity.session_id), None)
            if current is None or not _same_identity(identity, current):
                raise TerminalError("terminal identity changed before selection")
            self.selected_id = identity.session_id
            self.selected_identity = current
            if current.state == "running":
                client = await self._backend_call(self.registry.connect, identity.session_id)
                if not _same_identity(current, client.identity):
                    await self._backend_call(client.close)
                    self.selected_id = None
                    self.selected_identity = None
                    self._clear_live("세션 신원 불일치 · 출력을 숨겼습니다.")
                    self._set_notice("선택한 카드의 세션 신원을 확인하지 못했습니다.", 8.0)
                    return
                self.client = client
                await self._refresh_live()
            else:
                self._clear_live(self._state_label(identity.state))
        except Exception as exc:
            self.client = None
            self.input_mode = False
            self._clear_live("세션에 연결할 수 없습니다. 이전 화면은 숨겼습니다.")
            self._set_notice(f"Supervisor 연결 실패: {exc}", 8.0)
        finally:
            self._busy = False
            await self._render_cards()
            if focus:
                self._panel_index = 1
                self._focus_panel(1)
            self._update_status()

    async def _refresh_live(self) -> None:
        if not self.is_mounted or self._busy or self.selected_identity is None:
            return
        # Live refresh targets widgets on the base screen. While a modal is
        # active Textual scopes App.query_one() to that modal, so polling must
        # pause until the confirmation flow returns to the terminal screen.
        if isinstance(self.screen, ModalScreen):
            return
        if self.selected_identity.state != "running":
            if self.client is not None:
                await self._close_selected_client()
            self.input_mode = False
            self._right_observation = None
            self._mini_cache.pop(self.selected_identity.session_id, None)
            self._clear_live(self._state_label(self.selected_identity.state))
            self._update_status()
            return
        try:
            if self.client is None:
                self.client = await self._backend_call(
                    self.registry.connect, self.selected_identity.session_id
                )
            if not _same_identity(self.selected_identity, self.client.identity):
                raise TerminalError("terminal identity changed")
            observation = await self._backend_call(self.client.observe)
            if (observation.session_id != self.selected_identity.session_id
                    or observation.generation != self.selected_identity.generation
                    or observation.shell_pid != self.selected_identity.shell_pid):
                raise TerminalError("terminal observation identity changed")
            if observation.state != "running":
                self.input_mode = False
                if self.client.lease:
                    await self._backend_call(self.client.release)
                self._clear_live(self._state_label(observation.state))
            elif not observation.synced or observation.screen is None:
                self.input_mode = False
                self._right_observation = None
                self._mini_cache.pop(self.selected_identity.session_id, None)
                if self.client.lease:
                    await self._backend_call(self.client.release)
                self._clear_live("화면 재동기화 중 · 입력 차단")
            else:
                self._render_terminal(observation.screen)
                self._right_observation = (
                    _mini_identity_key(self.selected_identity), observation.sequence,
                    time.monotonic(), observation.screen,
                )
                self._update_status()
        except Exception as exc:
            await self._close_selected_client()
            self.input_mode = False
            if self.selected_identity is not None:
                self._mini_cache.pop(self.selected_identity.session_id, None)
            self._right_observation = None
            if not self.is_mounted:
                return
            self._clear_live("세션 확인 실패 · 오래된 출력을 숨겼습니다.")
            self._set_notice(f"입력을 차단했습니다: {exc}", 8.0)

    def _render_terminal(self, screen: TerminalScreen) -> None:
        view = self.query_one("#live", TerminalView)
        size = view.content_size
        columns = max(1, size.width)
        lines = max(1, size.height)
        view.update(_screen_text(screen, columns, lines))

    def _clear_live(self, message: str = "") -> None:
        if self.is_mounted:
            self.query_one("#live", TerminalView).update(Text(message or "터미널을 선택하세요.", style="dim"))

    async def _prompt_create_window(self) -> None:
        # The dialog belongs to navigation mode. Revoke any terminal input
        # lease before moving focus into UI controls.
        if self.input_mode:
            await self.leave_input_mode()
        self.prefix_pending = False
        suggested = f"Window {len(self.windows) + 1}"
        self.push_screen(
            CreateWindowScreen(suggested),
            callback=self._create_window_result,
        )

    def _create_window_result(self, name: str | None) -> None:
        if name is None:
            self._set_notice("Window 만들기를 취소했습니다.")
            return
        self.run_worker(
            self.create_window(name), name="create-logical-window",
            group="window-create", exclusive=True,
        )

    async def create_window(self, name: str) -> None:
        """Create and select a logical Window without starting a process."""
        if self._busy:
            return
        if self.workspace_store is None or self.layout_store is None:
            self._set_notice("Window 저장소를 사용할 수 없습니다.", 8.0)
            return
        self._busy = True
        try:
            workspace = await self._backend_call(
                lambda: next((item for item in self.workspace_store.workspaces()
                              if item["id"] == self.workspace_id), None)
            )
            if workspace is None:
                raise TerminalError("selected JTT workspace no longer exists")
            created = await self._backend_call(
                self.layout_store.create_window,
                self.workspace_id, "folder-default", name,
            )
            latest = await self._backend_call(
                self.layout_store.layout, self.workspace_id,
                workspace["terminal_ids"],
            )
            matches = [item for item in latest["windows"]
                       if item["id"] == created["id"]]
            if len(matches) != 1 or matches[0]["name"] != created["name"]:
                raise TerminalError("new Window identity could not be revalidated")
            await self._close_selected_client()
            self.input_mode = False
            self.selected_id = None
            self.selected_identity = None
            self.selected_window_id = created["id"]
            self._card_signature = None
            self._clear_live("빈 논리 Window · 셸은 아직 생성되지 않았습니다.")
            self._set_notice(f"{created['name']} 생성 · 터미널은 아직 없습니다.")
        except Exception as exc:
            self._set_notice(f"Window를 만들지 않았습니다: {exc}", 8.0)
        finally:
            self._busy = False
            await self._refresh_sessions()
            self._panel_index = 0
            self._focus_panel(0)

    async def select_window(self, window_id: str) -> None:
        if self._busy:
            return
        target = next((item for item in self.windows if item["id"] == window_id), None)
        if target is None:
            self._set_notice("Window가 갱신되었거나 더 이상 존재하지 않습니다.")
            await self._refresh_sessions()
            return
        if window_id == self.selected_window_id:
            self._panel_index = 0
            self._focus_panel(0)
            return
        self._busy = True
        try:
            # Release the old session's lease before changing the visible set.
            await self._close_selected_client()
            self.input_mode = False
            self.prefix_pending = False
            self._prefix_generation += 1
            self.selected_id = None
            self.selected_identity = None
            self.selected_window_id = window_id
            self._card_signature = None
            self._clear_live("Window 변경 · 읽기 전용 연결을 준비합니다.")
            self._panel_index = 0
        finally:
            self._busy = False
        await self._refresh_sessions()
        self._focus_panel(0)

    async def _move_window(self, offset: int) -> None:
        if not self.windows:
            return
        current = next((index for index, item in enumerate(self.windows)
                        if item["id"] == self.selected_window_id), 0)
        target = self.windows[(current + offset) % len(self.windows)]
        await self.select_window(target["id"])

    async def create_terminal(self) -> None:
        if self._busy:
            return
        self._busy = True
        requested_window_id = self.selected_window_id
        placement_notice = ""
        try:
            # Only the newly created terminal may receive the automatic input
            # lease after it has passed its identity and renderer checks.
            await self._close_selected_client()
            self.input_mode = False
            name = f"PowerShell {len(self.sessions) + 1}"
            client = await self._backend_call(self.registry.create, name)
            identity = client.identity
            current = await self._backend_call(self.registry.list_sessions)
            found = next((item for item in current if item.session_id == identity.session_id), None)
            if found is None or not _same_identity(identity, found):
                await self._backend_call(client.close)
                self._set_notice("새 세션 신원을 확인하지 못해 입력을 시작하지 않았습니다.", 8.0)
                return
            if self.workspace_store is not None and self.layout_store is not None:
                try:
                    workspace = await self._backend_call(
                        lambda: next((item for item in self.workspace_store.workspaces()
                                      if item["id"] == self.workspace_id), None)
                    )
                    if workspace is None:
                        raise TerminalError("selected JTT workspace no longer exists")
                    projection = await self._backend_call(
                        self.layout_store.layout, self.workspace_id,
                        workspace["terminal_ids"],
                    )
                    if requested_window_id not in {item["id"] for item in projection["windows"]}:
                        raise TerminalError("selected Window no longer exists")
                    updated = await self._backend_call(
                        self.workspace_store.add_terminal_reference,
                        self.workspace_id, identity.session_id,
                    )
                    if requested_window_id != "window-default":
                        await self._backend_call(
                            self.layout_store.move_terminal,
                            self.workspace_id, identity.session_id, requested_window_id,
                            updated["terminal_ids"],
                        )
                except Exception as exc:
                    # The process is already owned by the registry. Preserve it
                    # and keep it visible in memory; do not attempt a second
                    # shell creation or claim its placement was persisted.
                    self._runtime_terminal_placements[identity.session_id] = (
                        requested_window_id if any(
                            item["id"] == requested_window_id for item in self.windows
                        ) else "window-default"
                    )
                    placement_notice = f"세션은 생성됐지만 Window 저장 실패: {exc}"
                else:
                    self.selected_window_id = requested_window_id
            self.sessions[identity.session_id] = found
            await self._close_selected_client()
            self.client = client
            self.selected_id, self.selected_identity = identity.session_id, found
            self._clear_live()
            observation = await self._backend_call(client.observe)
            if (observation.synced and observation.screen is not None
                    and observation.session_id == identity.session_id
                    and observation.generation == identity.generation
                    and observation.shell_pid == identity.shell_pid
                    and await self._backend_call(client.acquire)):
                self._render_terminal(observation.screen)
                self.input_mode = True
                await self._resize_selected_terminal()
                self._panel_index = 2
                self._focus_panel(2)
                self._set_notice(
                    placement_notice or "새 PowerShell 준비 완료 · RIGHT 입력 모드", 5.0
                )
            else:
                if client.lease:
                    await self._backend_call(client.release)
                self.input_mode = False
                self._clear_live("새 터미널은 실행 중이나 화면 동기화가 확인되지 않았습니다.")
                self._set_notice("렌더러 준비 실패 · 안전을 위해 입력하지 않았습니다.", 8.0)
        except Exception as exc:
            self.input_mode = False
            self._clear_live("터미널 생성에 실패했습니다.")
            self._set_notice(f"터미널 생성 실패: {exc}", 8.0)
        finally:
            self._busy = False
            await self._refresh_sessions()
            await self._render_cards()
            self._update_status()

    async def enter_input_mode(self) -> None:
        if self._busy or self.selected_identity is None or self.selected_identity.state != "running":
            self._set_notice("실행 중인 로컬 터미널을 선택하세요.")
            return
        self._busy = True
        try:
            if self.client is None:
                self.client = await self._backend_call(
                    self.registry.connect, self.selected_identity.session_id
                )
            if not _same_identity(self.selected_identity, self.client.identity):
                raise TerminalError("terminal identity changed")
            observation = await self._backend_call(self.client.observe)
            if (not observation.synced or observation.screen is None
                    or observation.session_id != self.selected_identity.session_id
                    or observation.generation != self.selected_identity.generation
                    or observation.shell_pid != self.selected_identity.shell_pid):
                raise TerminalError("terminal is not synchronized")
            if not await self._backend_call(self.client.acquire):
                raise TerminalError("input lease is busy")
            self.input_mode = True
            await self._resize_selected_terminal()
            self._panel_index = 2
            self._focus_panel(2)
            await self._refresh_live()
        except Exception as exc:
            self.input_mode = False
            if self.client is not None and self.client.lease:
                await self._backend_call(self.client.release)
            self._set_notice(f"입력 권한을 얻지 못했습니다: {exc}", 8.0)
        finally:
            self._busy = False
            self._update_status()

    async def leave_input_mode(self) -> None:
        self.prefix_pending = False
        self._prefix_generation += 1
        if self.client is not None and self.client.lease:
            try:
                await self._backend_call(self.client.release)
            except Exception as exc:
                self._set_notice(f"입력 임대 해제 확인 실패: {exc}", 8.0)
        self.input_mode = False
        self._panel_index = 1
        self._focus_panel(1)
        self._update_status()

    async def _resize_selected_terminal(self) -> None:
        if self.client is None or not self.client.lease:
            return
        size = self.query_one("#live", TerminalView).content_size
        columns, lines = max(20, size.width), max(5, size.height)
        if columns <= 300 and lines <= 120:
            await self._backend_call(self.client.resize, columns, lines)

    async def _send_bytes(self, data: bytes) -> None:
        client = self.client
        identity = self.selected_identity
        if (not self.input_mode or client is None or identity is None
                or not client.lease or not _same_identity(identity, client.identity)):
            self.input_mode = False
            self._set_notice("입력 대상 신원을 확인할 수 없어 키를 차단했습니다.", 8.0)
            return
        try:
            await self._backend_call(client.send, data)
        except Exception as exc:
            await self._close_selected_client()
            self.input_mode = False
            self._clear_live("입력 실패 · 세션 화면을 다시 선택하세요.")
            self._set_notice(f"키 입력이 차단되었습니다: {exc}", 8.0)
        else:
            await self._refresh_live()
        self._update_status()

    async def _close_selected_client(self) -> None:
        client, self.client = self.client, None
        if client is None:
            return

        def close_client():
            try:
                client.release()
            except Exception:
                pass
            client.close()

        try:
            await self._backend_call(close_client)
        except Exception:
            # The server-side lease expires if a disconnected client cannot
            # acknowledge release; never keep sending through this client.
            pass

    def _focus_panel(self, index: int) -> None:
        if not self.is_mounted:
            return
        self._panel_index = index % 3
        try:
            if self._panel_index == 0:
                target = (self.query_one("#window-" + self.selected_window_id, Button)
                          if self.windows else self.query_one("#new-window", Button))
                target.focus()
            elif self._panel_index == 1:
                target = (self.query_one("#terminal-" + self.selected_id, Button)
                          if self.selected_id else self.query_one("#new-terminal", Button))
                target.focus()
            else:
                self.query_one("#live", TerminalView).focus()
        except Exception:
            self.query_one("#live", TerminalView).focus()

    async def _handle_prefix_key(self, key: str, character: str | None) -> None:
        self._clear_prefix()
        token = (character or "").lower()
        if token == "n":
            await self.create_terminal()
        elif token in ("h", "l"):
            values = list(self.sessions.values())
            if not values:
                self._set_notice("전환할 터미널이 없습니다.")
                return
            current = next((i for i, item in enumerate(values)
                            if item.session_id == self.selected_id), 0)
            next_index = (current + (-1 if token == "h" else 1)) % len(values)
            await self._select_session(values[next_index])
        elif token == "w":
            if self.input_mode:
                await self.leave_input_mode()
            self._focus_panel(0)
        elif token == "d":
            await self.action_detach()
        elif token == "x":
            await self._stop_selected()
        elif token == "?":
            self._set_notice("Ctrl+G n 새 터미널 · h/l 카드 · w 공간 · x 종료 확인 · d 분리")
        elif key.lower() == "escape":
            self._set_notice("JTT 명령을 취소했습니다.")
        else:
            self._set_notice("알 수 없는 JTT 명령 · 키를 소비했습니다.")

    def _clear_prefix(self) -> None:
        self.prefix_pending = False
        self._prefix_generation += 1
        self._update_status()

    def _expire_prefix(self, generation: int) -> None:
        if self.prefix_pending and generation == self._prefix_generation:
            self.prefix_pending = False
            self._set_notice("Prefix 시간 초과 · 키를 터미널에 보내지 않았습니다.")

    async def action_prefix(self) -> None:
        if isinstance(self.screen, ModalScreen):
            return
        if self.prefix_pending:
            self._clear_prefix()
            if self.input_mode:
                await self._send_bytes(b"\x07")
            else:
                self._set_notice("Prefix 명령 대기를 취소했습니다.")
            return
        self.prefix_pending = True
        self._prefix_generation += 1
        generation = self._prefix_generation
        self.set_timer(4.0, lambda: self._expire_prefix(generation))
        self._update_status()

    async def action_input_escape(self) -> None:
        if isinstance(self.screen, ModalScreen):
            return
        if self.prefix_pending:
            self._clear_prefix()
            self._set_notice("Prefix 명령을 취소했습니다.")
        elif self.input_mode:
            await self.leave_input_mode()

    async def action_send_ctrl_c(self) -> None:
        if self.input_mode and not isinstance(self.screen, ModalScreen):
            await self._send_bytes(b"\x03")

    async def action_send_ctrl_q(self) -> None:
        if self.input_mode and not isinstance(self.screen, ModalScreen):
            await self._send_bytes(b"\x11")

    async def action_panel_next(self) -> None:
        if isinstance(self.screen, ModalScreen):
            self.screen.focus_next()
            return
        if self.prefix_pending:
            self._clear_prefix()
            self._set_notice("Prefix 명령을 취소했습니다.")
        elif self.input_mode:
            await self._send_bytes(b"\t")
        else:
            self._focus_panel(self._panel_index + 1)
            self._update_status()

    async def action_panel_previous(self) -> None:
        if isinstance(self.screen, ModalScreen):
            self.screen.focus_previous()
            return
        if self.input_mode:
            await self._send_bytes(b"\x1b[Z")
        else:
            self._focus_panel(self._panel_index - 1)
            self._update_status()

    async def action_enter(self) -> None:
        if isinstance(self.screen, CreateWindowScreen):
            self.screen.dismiss(self.screen.query_one("#window-name", Input).value)
            return
        if isinstance(self.screen, ConfirmStopScreen):
            focused_id = getattr(self.focused, "id", "")
            self.screen.dismiss(focused_id == "confirm-stop")
            return
        if isinstance(self.screen, ModalScreen):
            return
        if self.input_mode:
            await self._send_bytes(b"\r")
        elif self._panel_index == 0:
            focused = self.focused
            focused_id = getattr(focused, "id", "")
            if focused_id == "new-window":
                await self._prompt_create_window()
            elif focused_id and focused_id.startswith("window-"):
                await self.select_window(focused_id[len("window-"):])
        elif self._panel_index == 1 and getattr(self.focused, "id", "") == "new-terminal":
            await self.create_terminal()
        elif self._panel_index in (1, 2):
            await self.enter_input_mode()

    async def action_backspace(self) -> None:
        if isinstance(self.screen, CreateWindowScreen):
            self.screen.query_one("#window-name", Input).action_delete_left()
            return
        if isinstance(self.screen, ModalScreen):
            return
        if self.input_mode:
            await self._send_bytes(b"\x7f")

    async def on_key(self, event) -> None:
        key = str(event.key).lower()
        character = getattr(event, "character", None)
        if self.prefix_pending:
            event.stop()
            await self._handle_prefix_key(key, character)
            return
        if self.input_mode:
            data = _key_bytes(event)
            if data is not None:
                event.stop()
                await self._send_bytes(data)
            return
        if key in ("up", "down") and self._panel_index == 0:
            event.stop()
            await self._move_window(-1 if key == "up" else 1)
        elif key in ("up", "left") and self._panel_index == 1:
            event.stop()
            await self._move_card(-1)
        elif key in ("down", "right") and self._panel_index == 1:
            event.stop()
            await self._move_card(1)
        elif key == "escape":
            event.stop()

    async def _move_card(self, offset: int) -> None:
        values = list(self.sessions.values())
        if not values:
            return
        current = next((i for i, item in enumerate(values)
                        if item.session_id == self.selected_id), 0)
        await self._select_session(values[(current + offset) % len(values)])

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id or ""
        if button_id == "new-terminal":
            await self.create_terminal()
            return
        if button_id == "new-window":
            await self._prompt_create_window()
            return
        if button_id.startswith("window-"):
            await self.select_window(button_id[len("window-"):])
            return
        if button_id.startswith("terminal-"):
            session_id = button_id[len("terminal-"):]
            identity = self.sessions.get(session_id)
            if identity is not None:
                await self._select_session(identity)

    async def action_detach(self) -> None:
        await self._close_selected_client()
        self.input_mode = False
        self.prefix_pending = False
        self.exit()

    async def _stop_selected(self) -> None:
        identity = self.selected_identity
        if identity is None:
            self._set_notice("종료할 터미널을 선택하세요.")
            return
        await self.leave_input_mode()
        self.push_screen(
            ConfirmStopScreen(identity.name),
            callback=lambda confirmed: self._stop_confirmation_result(identity, confirmed),
        )

    def _stop_confirmation_result(self, identity: TerminalIdentity, confirmed: bool | None) -> None:
        if confirmed is not True:
            self._set_notice("터미널 종료를 취소했습니다.")
            return
        self.run_worker(
            self._finish_stop(identity),
            name="stop-selected-terminal",
            group="terminal-stop",
            exclusive=True,
        )

    async def _finish_stop(self, identity: TerminalIdentity) -> None:
        try:
            latest = await self._backend_call(self.registry.list_sessions)
            current = next((item for item in latest if item.session_id == identity.session_id), None)
            if current is None or not _same_identity(identity, current):
                raise TerminalError("terminal identity changed before stop")
            await self._close_selected_client()
            await self._backend_call(
                lambda: self.registry.stop(
                    identity.session_id,
                    expected_generation=identity.generation,
                    expected_shell_pid=identity.shell_pid,
                )
            )
            self.selected_id = None
            self.selected_identity = None
            self._clear_live("터미널이 종료되었습니다.")
            self._set_notice("확인된 세션만 종료했습니다.")
            await self._refresh_sessions()
        except Exception as exc:
            self._set_notice(f"세션을 종료하지 않았습니다: {exc}", 8.0)


def run_jtt_windows_ui() -> int:
    """Launch Windows UI without importing curses or starting a shell."""

    if sys.platform != "win32":
        print("Windows JTT UI can run only on native Windows.", file=sys.stderr)
        return 2
    try:
        from ..terminal import TerminalRegistry

        root = default_jtt_root()
        registry = TerminalRegistry(root=root / "terminals")
        app = WindowsJTTApp(
            registry,
            workspace_store=JTTWorkspaceStore(root),
            layout_store=JTTLayoutStore(root),
        )
        result = app.run()
        return result if isinstance(result, int) else 0
    except (TerminalError, WorkspaceStoreError, JTTLayoutStoreError) as exc:
        print(f"Windows JTT terminal backend unavailable: {exc}", file=sys.stderr)
        return 2
