"""Initial native Windows three-panel JTT UI over the ConPTY registry.

This screen intentionally uses the Windows terminal registry as its source of
truth and does not import the POSIX curses UI or the POSIX workspace stores.
Workspace and Work Kit persistence are not wired into this first UI slice.
"""

from __future__ import annotations

import asyncio
import re
import sys
from typing import Any

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, ScrollableContainer, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Static
from rich.style import Style
from rich.text import Text

from ..terminal.manager import TerminalError, TerminalIdentity, TerminalScreen


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
    #window-note { height: auto; color: #a8b7c3; margin-top: 1; }
    #new-terminal { width: 100%; height: 3; margin-top: 1; }
    #left-limits { height: 1fr; color: #81919d; margin-top: 1; }
    #terminal-summary { height: 1; color: #a8b7c3; }
    #terminal-cards { height: 1fr; }
    .card-row { width: 100%; height: 5; margin-bottom: 1; }
    .terminal-card { width: 1fr; height: 5; border: round #355d68; content-align: left middle; }
    .terminal-card.selected { border: round #35cbbd; background: #102a35; }
    #live { height: 1fr; width: 100%; overflow-x: hidden; overflow-y: hidden;
            border: round #294550; padding: 0 1; background: #050b13; }
    #terminal-status { height: 1; color: #96aab5; }
    #statusline { height: 1; color: #c4d2d8; background: #10212a; padding: 0 1; }
    """

    def __init__(self, registry, *, auto_select: bool = True):
        super().__init__()
        self.registry = registry
        self.auto_select = auto_select
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

    def compose(self) -> ComposeResult:
        with Horizontal(id="panels"):
            with Vertical(id="left", classes="panel"):
                yield Static("JTT · JU0 TERMINAL TOWER", id="brand")
                yield Static("작업공간", classes="panel-title")
                yield Static("내 작업공간\nWindows · 로컬", id="workspace")
                yield Static(
                    "등록된 로컬 세션을 기본 공간에서 보여줍니다.\n"
                    "저장 작업공간·Window 편집·Work Kit은 아직 연결되지 않았습니다.",
                    id="window-note",
                )
                yield Button("+ 새 터미널", id="new-terminal", variant="primary")
                yield Static("시작 시 셸을 자동 생성하지 않습니다.", id="left-limits")
            with Vertical(id="center", classes="panel"):
                yield Static("터미널", classes="panel-title")
                yield Static("세션을 불러오는 중", id="terminal-summary")
                with ScrollableContainer(id="terminal-cards"):
                    yield Static("등록된 터미널이 없습니다.", id="empty-cards", classes="dim")
            with Vertical(id="right", classes="panel"):
                yield Static("LIVE · 선택한 터미널", classes="panel-title")
                yield TerminalView("터미널을 선택하거나 새로 만드세요.", id="live")
                yield Static("관찰 중", id="terminal-status")
        yield Static("탐색 · Ctrl+G n 새 터미널 · Tab 패널 이동 · Enter 입력 · Ctrl+G d 분리", id="statusline")

    async def on_mount(self) -> None:
        self.set_interval(1.0, self._refresh_sessions)
        self.set_interval(0.2, self._refresh_live)
        await self._refresh_sessions()

    async def on_unmount(self) -> None:
        await self._close_selected_client()

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
        if self._busy:
            return
        try:
            values = await self._backend_call(self.registry.list_sessions)
        except Exception as exc:
            self.sessions = {}
            await self._close_selected_client()
            self.selected_identity = None
            self._clear_live("Supervisor 연결 실패 · 세션 목록을 읽을 수 없습니다.")
            self._set_notice(f"Supervisor 연결 실패: {exc}", 8.0)
            await self._render_cards()
            return

        self.sessions = {item.session_id: item for item in sorted(
            values, key=lambda item: (item.name.casefold(), item.session_id)
        )}
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

        await self._render_cards()
        self._update_status()

    async def _render_cards(self) -> None:
        if not self.is_mounted:
            return
        values = list(self.sessions.values())
        signature = tuple(
            (item.session_id, item.generation, item.shell_pid, item.state, item.name,
             item.session_id == self.selected_id)
            for item in values
        )
        if signature == self._card_signature:
            return
        self._card_signature = signature
        container = self.query_one("#terminal-cards", ScrollableContainer)
        await container.remove_children()
        if not values:
            await container.mount(Static("등록된 터미널이 없습니다.\n+ 새 터미널로 시작하세요."))
            self.query_one("#terminal-summary", Static).update("터미널 0개")
            return
        self.query_one("#terminal-summary", Static).update(f"터미널 {len(values)}개")
        for start in range(0, len(values), 2):
            row = Horizontal(classes="card-row")
            await container.mount(row)
            for identity in values[start:start + 2]:
                selected = identity.session_id == self.selected_id
                state = self._state_label(identity.state)
                label = Text(f"{identity.name}\n{state} · PID {identity.shell_pid}")
                button = Button(
                    label,
                    id="terminal-" + identity.session_id,
                    classes="terminal-card selected" if selected else "terminal-card",
                    variant="primary" if selected else "default",
                )
                await row.mount(button)

    async def _select_session(self, identity: TerminalIdentity, *, focus: bool = True) -> None:
        if self._busy:
            return
        self._busy = True
        try:
            await self._close_selected_client()
            self.input_mode = False
            self.selected_id = identity.session_id
            self.selected_identity = identity
            self._clear_live()
            if identity.state == "running":
                client = await self._backend_call(self.registry.connect, identity.session_id)
                if not _same_identity(identity, client.identity):
                    await self._backend_call(client.close)
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
                if self.client.lease:
                    await self._backend_call(self.client.release)
                self._clear_live("화면 재동기화 중 · 입력 차단")
            else:
                self._render_terminal(observation.screen)
                self._update_status()
        except Exception as exc:
            await self._close_selected_client()
            self.input_mode = False
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

    async def create_terminal(self) -> None:
        if self._busy:
            return
        self._busy = True
        try:
            name = f"PowerShell {len(self.sessions) + 1}"
            client = await self._backend_call(self.registry.create, name)
            identity = client.identity
            current = await self._backend_call(self.registry.list_sessions)
            found = next((item for item in current if item.session_id == identity.session_id), None)
            if found is None or not _same_identity(identity, found):
                await self._backend_call(client.close)
                self._set_notice("새 세션 신원을 확인하지 못해 입력을 시작하지 않았습니다.", 8.0)
                return
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
                self.input_mode = True
                await self._resize_selected_terminal()
                self._panel_index = 2
                self._focus_panel(2)
                await self._refresh_live()
                self._set_notice("새 PowerShell 준비 완료 · RIGHT 입력 모드", 5.0)
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
                self.query_one("#new-terminal", Button).focus()
            elif self._panel_index == 1:
                target = (self.query_one("#terminal-" + self.selected_id, Button)
                          if self.selected_id else self.query_one("#terminal-cards"))
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
        if self.prefix_pending:
            self._clear_prefix()
            self._set_notice("Prefix 명령을 취소했습니다.")
        elif self.input_mode:
            await self.leave_input_mode()

    async def action_send_ctrl_c(self) -> None:
        if self.input_mode:
            await self._send_bytes(b"\x03")

    async def action_send_ctrl_q(self) -> None:
        if self.input_mode:
            await self._send_bytes(b"\x11")

    async def action_panel_next(self) -> None:
        if self.prefix_pending:
            self._clear_prefix()
            self._set_notice("Prefix 명령을 취소했습니다.")
        elif self.input_mode:
            await self._send_bytes(b"\t")
        else:
            self._focus_panel(self._panel_index + 1)
            self._update_status()

    async def action_panel_previous(self) -> None:
        if self.input_mode:
            await self._send_bytes(b"\x1b[Z")
        else:
            self._focus_panel(self._panel_index - 1)
            self._update_status()

    async def action_enter(self) -> None:
        if self.input_mode:
            await self._send_bytes(b"\r")
        elif self._panel_index in (1, 2):
            await self.enter_input_mode()

    async def action_backspace(self) -> None:
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
        if key in ("up", "left") and self._panel_index == 1:
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

        registry = TerminalRegistry()
        app = WindowsJTTApp(registry)
        result = app.run()
        return result if isinstance(result, int) else 0
    except TerminalError as exc:
        print(f"Windows JTT terminal backend unavailable: {exc}", file=sys.stderr)
        return 2
