"""In-Tower pane control. Enter stays here; Ctrl+G is the only focus escape.

Live text, prompts, identity, focus, result, and close all call
``control.actions``. This module does not talk to tmux itself.
"""

from __future__ import annotations

import curses
import hashlib
import json
import os
import re
import stat
import time

from ..clipboard import copy_text, plain_clipboard_text
from ..clipboard_dest import (
    MANUAL,
    SessionChoice,
    load_preference,
    observe,
    resolve_plan,
    route_for_access_client,
)
from ..clipboard_bridge import load_bridge_client
from ..control.actions import (
    MAX_PROMPT_CHARS,
    close_choices,
    close_notice_lines,
    close_pane,
    focus_pane,
    find_row,
    get_pane_screen,
    get_result,
    respond_attention,
    respond_interaction,
    respond_interaction_text,
    send_prompt,
)
from ..detection.result import pane_result_identity
from ..i18n import t
from ..state.overrides import ROLE_IDS
from . import render
from .prompt_composer import Composer, _handle, _set_bracketed_paste, _take, layout_text, scroll_to_cursor
from .perf_trace import draw as trace_draw, event
from .widgets import is_escape, read_key, run_list_picker, safe_add

# Inside the 0.5–1s band. Recent screen only; the list stays underneath
# in the process, and the client is not moved.
CONTROL_REFRESH_SECONDS = 0.7
_LOGIN_RE = re.compile(r"\b(?:log\s*in|sign\s*in|authentication required|authenticate|oauth)\b", re.I)
_COPY_TRACE_ENV = "TOWER_RESULT_COPY_TRACE_FILE"


def open_control_view(stdscr, tower, pane_key: str, *, show_result_on_open: bool = False) -> None:
    """Keep one pane's live view and composer open until Esc."""

    notice = ""
    show_result = False
    show_advanced = False
    follow = True
    scroll = 0
    composer = Composer(max_chars=MAX_PROMPT_CHARS)
    drafts = getattr(tower, "composer_drafts", None)
    if drafts is None:
        drafts = tower.composer_drafts = {}
    draft = drafts.get(pane_key)
    if draft:
        composer.text, composer.cursor = draft
    composer_scroll = 0
    first_draw = True
    last_refresh = time.monotonic()
    needs_draw = True
    async_refresh = callable(getattr(tower, "start_background_refresh", None))
    keypad = getattr(stdscr, "keypad", None)
    if keypad:
        keypad(True)
    _set_input_delay()
    if async_refresh:
        _start_refresh(tower)
    if show_result_on_open:
        notice, show_result = _show_result(tower, pane_key)
    _set_bracketed_paste(True)
    stdscr.timeout(25)
    try:
        room = 1
        while True:
            if async_refresh:
                if _apply_refresh(tower):
                    needs_draw = True
            else:
                tower.load()
                needs_draw = True
            row = next((item for item in tower.rows if item.get("key") == pane_key), None)
            if row is None or row.get("placeholder"):
                tower.notice = "stale"
                return
            now = time.monotonic()
            if now - last_refresh >= CONTROL_REFRESH_SECONDS:
                _start_refresh(tower)
                last_refresh = now
            if needs_draw:
                room = _draw(stdscr, tower, row, notice, show_result, follow, scroll, show_advanced, composer, composer_scroll)
                if first_draw:
                    event("FIRST_DRAW", view="conversation")
                    first_draw = False
                notice = ""
                needs_draw = False
            stdscr.timeout(25)
            key = read_key(stdscr)
            if key == -1 or key == curses.KEY_RESIZE:
                needs_draw = key == curses.KEY_RESIZE
                continue
            needs_draw = True
            interaction = row.get("interaction") or {}
            # ESC may begin a bracketed paste marker. Let the existing
            # Composer parser consume that sequence before treating a lone
            # ESC as the view's back action.
            if composer.pasting or composer._esc is not None or key == "\x1b":
                _feed_composer(stdscr, composer, key)
                if composer.closed == "cancel":
                    return
                if composer.closed == "submit":
                    outcome = _submit_composer(tower, pane_key, row, composer)
                    answer = (interaction.get("type") == "text" or interaction.get("input_allowed"))
                    if outcome.ok and (outcome.submitted or answer):
                        composer = Composer(max_chars=MAX_PROMPT_CHARS)
                        composer_scroll = 0
                        notice = t("control.answer_sent") if answer else t("control.prompt_sent")
                        _start_refresh(tower)
                    else:
                        composer.closed = None
                        notice = t("control.prompt_unconfirmed") if outcome.ok else t("control.prompt_failed")
                continue
            if is_escape(key):
                event("VIEW_STATE_CHANGED", view="home")
                return
            if _control_shortcut(key, "y"):
                notice = _copy_result(tower, pane_key, stdscr)
                continue
            if _control_shortcut(key, "s"):
                notice, show_result = _show_result(tower, pane_key)
                continue
            if show_advanced and _control_shortcut(key, "l"):
                notice = _copy_screen(
                    tower, pane_key, stdscr,
                    expected_pane_pid=str(row.get("pane_pid") or ""),
                )
                continue
            if _control_shortcut(key, "e"):
                _select(tower, pane_key)
                tower.edit_selected(stdscr)
                continue
            if _control_shortcut(key, "g"):
                notice = _go(tower, row)
                continue
            if _control_shortcut(key, "x"):
                if _close(stdscr, tower, row):
                    return
                continue
            if _control_shortcut(key, "n") and row.get("reject_known") and not _unknown_interaction(interaction):
                notice = _attention_action(tower, row, "reject")
                continue
            if row.get("remote"):
                notice = t("control.remote_unsupported")
                continue
            if _unknown_interaction(interaction):
                notice = t("attention.unknown")
                continue
            if interaction.get("options") and interaction.get("type") in ("approval", "choice", "confirm"):
                option = next((item for item in interaction["options"] if item.get("safe") and item.get("key") == _key_char(key)), None)
                if option:
                    ok, reason = respond_interaction(tower, pane_key, option["key"])
                    notice = t("control.option_sent") if ok else t("attention.unknown") if reason == "interaction_unknown" else (reason or t("control.prompt_failed"))
                continue
            if interaction.get("type") == "approval" and row.get("approval_known") and _key_char(key) == "a":
                notice = _attention_action(tower, row, "approve")
                continue
            if key in (curses.KEY_UP, curses.KEY_PPAGE):
                if composer.text:
                    composer.feed(key)
                    composer_scroll = scroll_to_cursor(layout_text(composer.text, composer.cursor, max(1, stdscr.getmaxyx()[1] - 4)).cursor_row, 2, composer_scroll)
                    continue
                follow = False
                scroll += room if key == curses.KEY_PPAGE else 1
                continue
            if key in (curses.KEY_DOWN, curses.KEY_NPAGE) and composer.text and key == curses.KEY_DOWN:
                composer.feed(key)
                continue
            if key in (curses.KEY_DOWN, curses.KEY_NPAGE):
                step = room if key == curses.KEY_NPAGE else 1
                scroll = max(0, scroll - step)
                follow = scroll == 0
                continue
            if key == curses.KEY_END:
                follow = True
                scroll = 0
                continue
            if _control_shortcut(key, "i"):
                show_advanced = not show_advanced
                continue
            if key == " " and not composer.text:
                from .structure_menu import open_pane_menu

                if open_pane_menu(stdscr, tower, row) == "closed":
                    return
                continue
            _feed_composer(stdscr, composer, key)
            if composer.closed == "cancel":
                return
            if composer.closed != "submit":
                continue
            if not composer.text.strip() and not (interaction.get("type") == "text" or interaction.get("input_allowed")):
                composer.closed = None
                continue
            outcome = _submit_composer(tower, pane_key, row, composer)
            answer = (interaction.get("type") == "text" or interaction.get("input_allowed"))
            if outcome.ok and (outcome.submitted or answer):
                composer = Composer(max_chars=MAX_PROMPT_CHARS)
                composer_scroll = 0
                notice = t("control.answer_sent") if answer else t("control.prompt_sent")
                _start_refresh(tower)
            else:
                composer.closed = None
                notice = (t("control.prompt_unconfirmed") if outcome.ok else t("control.prompt_failed"))
    finally:
        if composer.text:
            drafts[pane_key] = (composer.text, composer.cursor)
        else:
            drafts.pop(pane_key, None)
        _set_bracketed_paste(False)
        stdscr.timeout(200)


def _select(tower, pane_key: str) -> None:
    for index, row in enumerate(tower.visible_rows):
        if row.get("key") == pane_key:
            tower.selected = index
            return


@trace_draw("conversation")
def _draw(stdscr, tower, row: dict, notice: str, show_result: bool, follow: bool = True, scroll: int = 0, show_advanced: bool = False, composer=None, composer_scroll: int = 0) -> int:
    """Paint the control view. Returns how many live lines fit, for paging."""

    stdscr.erase()
    height, width = stdscr.getmaxyx()
    display_name = row.get("display_name") or row.get("task_name") or row.get("project") or t("task.unnamed")

    symbol, key = render.execution_badge(row.get("status") or "")
    duration = ""
    seconds = row.get("duration_seconds") or 0
    if seconds and tower.config.get("show_status_duration"):
        duration = " · " + render.format_duration(seconds)
    # Execution is already on the line above. Repeating it here crowded
    # the control view with the same mark twice.
    execution = (symbol, key)
    extra = [badge for badge in render.detail_badges(row) if badge != execution]
    badges = "   ".join(f"{mark} {t(name)}" for mark, name in extra)
    safe_add(stdscr, 0, 2, f'{display_name}   {symbol} {t(key)}{duration}', curses.A_BOLD)
    role = t(f'role.{row["role"]}') if row.get("role") in ROLE_IDS else ""
    if row.get("work_group_name"):
        identity = f'{row["work_group_name"]} › ' + " · ".join(part for part in (role, row.get("agent") or "-") if part)
    else:
        identity = " · ".join(part for part in (row.get("agent") or "-", role) if part)
    safe_add(stdscr, 1, 2, identity)
    safe_add(stdscr, 2, 2, f'{t("detail.execution_host")}: {row.get("execution_host") or row.get("host") or "-"}', curses.A_DIM)
    safe_add(stdscr, 3, 2, badges)
    safe_add(stdscr, 4, 2, f'{t("detail.activity")}: {row.get("activity_text") or "-"}', curses.A_DIM)
    from .tower import location_lines

    y = 5
    if show_advanced:
        safe_add(stdscr, y, 2, t("detail.advanced"), curses.A_BOLD)
        y += 1
        for line in location_lines(row):
            safe_add(stdscr, y, 2, line, curses.A_DIM)
            y += 1
        sense = render.format_identity_sense(
            row.get("agent_source"),
            row.get("project_source"),
            row.get("auto_agent_source"),
            row.get("auto_project_source"),
        )
        safe_add(stdscr, y, 2, f'{t("detail.sense")}: {sense}', curses.A_DIM)
        y += 1

    live_label_y = y + 1
    login_required = _login_required(row)
    if row.get("attention_prompt") or login_required:
        prompt = t("control.login_required") if login_required else row.get("attention_prompt") or ""
        safe_add(stdscr, live_label_y, 2, prompt, curses.A_BOLD)
        live_label_y += 1
    interaction = row.get("interaction") or {}
    for option in (interaction.get("options") or [])[:4]:
        safe_add(stdscr, live_label_y, 2, f'  {option.get("label") or ""}')
        live_label_y += 1

    live_top = live_label_y + 1
    action_lines = _action_hint_lines(row, width, show_advanced) if not notice else []
    footer_y = height - max(1, len(action_lines))
    result_lines: list = []
    if show_result:
        ok, _reason, payload = get_result(tower, row.get("key"))
        body = (payload.get("text") or "") if ok else ""
        result_lines = (body.split("\n") if body else [t("control.no_result")])[:6]

    interaction = row.get("interaction") or {}
    input_active = interaction.get("type") == "text" or interaction.get("input_allowed")
    choice_active = interaction.get("type") in ("approval", "choice", "confirm") and interaction.get("options")
    gated = login_required or _unknown_interaction(interaction) or choice_active or interaction.get("type") == "approval" or interaction.get("type") == "confirm"
    composer_rows = 0 if gated or row.get("remote") else 5
    bottom_hint_rows = 1 if not composer_rows and (
        row.get("remote") or login_required or _unknown_interaction(interaction) or choice_active
        or (interaction.get("type") == "approval" and row.get("approval_known"))
    ) else 0
    live_bottom = footer_y - composer_rows - bottom_hint_rows - (len(result_lines) + 1 if result_lines else 0)
    live_mark = t("control.live_follow") if follow else t("control.live_paused")
    safe_add(stdscr, live_label_y, 2, f'{t("control.live")}  {live_mark}', curses.A_BOLD)
    lines = [_fit_live_line(line, width) for line in _live_lines(tower, row)]
    room = max(1, live_bottom - live_top)
    if follow:
        view = lines[-room:]
    else:
        start = max(0, len(lines) - room - scroll)
        view = lines[start : start + room]
    for offset, line in enumerate(view):
        safe_add(stdscr, live_top + offset, 2, line)

    y = live_bottom
    for line in result_lines:
        safe_add(stdscr, y, 2, line)
        y += 1

    if composer_rows:
        _draw_inline_composer(stdscr, footer_y - composer_rows, row, composer or Composer(), composer_scroll, answer=input_active)
    elif row.get("remote"):
        safe_add(stdscr, footer_y - 1, 2, t("control.remote_unsupported"), curses.A_BOLD)
    elif login_required:
        safe_add(stdscr, footer_y - 1, 2, t("control.login_hint"), curses.A_BOLD)
    elif _unknown_interaction(interaction):
        safe_add(stdscr, footer_y - 1, 2, t("attention.unknown"), curses.A_BOLD)
    elif choice_active:
        keys = "   ".join(f'{item.get("key")}: {item.get("label") or item.get("key")}' for item in interaction["options"] if item.get("safe") and item.get("key"))
        safe_add(stdscr, footer_y - 1, 2, keys or t("attention.unknown"), curses.A_BOLD)
    elif interaction.get("type") == "approval" and row.get("approval_known"):
        safe_add(stdscr, footer_y - 1, 2, t("control.key_approve"), curses.A_BOLD)
    if notice:
        safe_add(stdscr, footer_y, 2, notice, curses.A_BOLD)
    else:
        _draw_actions(stdscr, footer_y, row, show_advanced, lines=action_lines)
    if composer_rows and composer is not None and not gated:
        _place_composer_cursor(stdscr, footer_y - composer_rows, composer, composer_scroll)
    stdscr.noutrefresh()
    curses.doupdate()
    return room


def _unknown_interaction(interaction: dict) -> bool:
    return interaction.get("type") == "unknown" or (interaction.get("type") == "confirm" and not interaction.get("options"))


def _login_required(row: dict) -> bool:
    if row.get("attention") not in {"approval_required", "input_required"}:
        return False
    interaction = row.get("interaction") or {}
    text = " ".join(
        str(value or "")
        for value in (row.get("attention_prompt"), interaction.get("prompt"), interaction.get("question"))
    )
    return bool(_LOGIN_RE.search(text))


def _key_char(key) -> str:
    return key.lower() if isinstance(key, str) and len(key) == 1 and key.isprintable() else ""


def _control_shortcut(key, letter: str) -> bool:
    return key == chr(ord(letter.lower()) - 96)


def _feed_composer(stdscr, composer: Composer, key) -> None:
    queued: list = []
    _handle(stdscr, queued, composer, key, time.monotonic())
    while composer.closed is None:
        next_key = _take(stdscr, queued, wait_ms=0)
        if next_key == -1:
            break
        _handle(stdscr, queued, composer, next_key, time.monotonic())
    stdscr.timeout(25)


def _submit_composer(tower, pane_key: str, row: dict, composer: Composer):
    """Submit text through the current measured interaction or prompt path."""

    interaction = row.get("interaction") or {}
    if interaction.get("type") == "text" or interaction.get("input_allowed"):
        return respond_interaction_text(tower, pane_key, composer.text)
    return send_prompt(tower, pane_key, composer.text, row.get("project"), row.get("agent"))


def _draw_inline_composer(stdscr, top: int, row: dict, composer: Composer, scroll: int, answer: bool = False) -> None:
    height, width = stdscr.getmaxyx()
    label = t("control.answer_label") if answer else t("control.command_label") if _is_shell_row(row) else t("control.message_label")
    safe_add(stdscr, top, 2, f"{label}  {t('control.composer_hint')}", curses.A_BOLD)
    input_rows = max(1, min(2, height - top - 4))
    laid = layout_text(composer.text, composer.cursor, max(1, width - 5))
    scroll = scroll_to_cursor(laid.cursor_row, input_rows, scroll)
    for offset in range(input_rows):
        index = scroll + offset
        safe_add(stdscr, top + 1 + offset, 2, laid.rows[index] if index < len(laid.rows) else "", 0)
    count = t("control.composer_count", used=f"{len(composer.text):,}", limit=f"{composer.max_chars:,}")
    safe_add(stdscr, top + 3, 2, count, curses.A_DIM)
    hint = t("control.composer_too_long", limit=f"{composer.max_chars:,}") if composer.notice == "too_long" else t("control.composer_paste_hint") if composer.paste_ready or composer.unbracketed_paste else ""
    if hint:
        safe_add(stdscr, top + 4, 2, hint, curses.A_BOLD if composer.notice else curses.A_DIM)


def _is_shell_row(row: dict) -> bool:
    """Use resolved identity, falling back only to trustworthy auto-detection."""

    agent = row.get("agent")
    if agent:
        return agent.lower() == "shell"
    return (row.get("auto_agent") or "").lower() == "shell" and row.get(
        "auto_agent_source"
    ) in ("process", "shell")


def _place_composer_cursor(stdscr, top: int, composer: Composer, scroll: int) -> None:
    height, width = stdscr.getmaxyx()
    input_rows = max(1, min(2, height - top - 4))
    laid = layout_text(composer.text, composer.cursor, max(1, width - 5))
    scroll = scroll_to_cursor(laid.cursor_row, input_rows, scroll)
    y = top + 1 + laid.cursor_row - scroll
    if 0 <= y < height - 1:
        try:
            stdscr.move(y, min(width - 1, 2 + laid.cursor_col))
        except curses.error:
            pass


def _fit_live_line(line: str, width: int) -> str:
    return render.truncate_to_width(line, max(4, width - 4))


def _draw_actions(stdscr, y: int, row: dict, show_advanced: bool = False, *, lines=None) -> None:
    """Shortcuts remain available while printable keys belong to the composer."""
    if lines is None:
        _, width = stdscr.getmaxyx()
        lines = _action_hint_lines(row, width, show_advanced)
    for offset, line in enumerate(lines):
        safe_add(stdscr, y + offset, 2, line, curses.A_BOLD)


def _action_hint_lines(row: dict, width: int, show_advanced: bool = False) -> list[str]:
    compact = width < 28
    items = (
        ["Ctrl+Y 복사", "Ctrl+S 결과", "Ctrl+E 이름", "Esc 뒤로"]
        if compact else
        ["Ctrl+Y 결과 복사", "Ctrl+S 결과 보기", "Ctrl+E 이름", "Esc 뒤로"]
    )
    if row.get("pane_id") and not row.get("remote"):
        items.extend(["Ctrl+G 이동", "Ctrl+X 닫기"])
    if row.get("reject_known") and not _unknown_interaction(row.get("interaction") or {}):
        items.append("Ctrl+N 거절")
    if show_advanced:
        items.extend(["Ctrl+L 최근 터미널 출력 복사", "Ctrl+I 간단히"])
    else:
        items.append("Ctrl+I 자세히")
    if compact and show_advanced:
        items[-2] = "Ctrl+L 출력"
    return render.wrap_items(items, max(1, width - 4))


def _result_label(state: str) -> str:
    if state == "ready":
        return t("control.result_ready")
    if state == "read":
        return t("control.result_read")
    return "-"


def _live_lines(tower, row: dict) -> list:
    if row.get("remote"):
        return [t("control.remote_live")]
    if row.get("status") == "DEAD":
        return [t("control.dead_live")]
    if "live_lines" in row:
        return row.get("live_lines") or [t("control.empty_live")]
    ok, reason, payload = get_pane_screen(tower.session, row.get("key") or "", tower.own_pane_id)
    if not ok:
        return [reason or "not_found"]
    return payload.get("lines") or [t("control.empty_live")]


def _set_input_delay() -> None:
    setter = getattr(curses, "set_escdelay", None)
    if setter:
        try:
            setter(25)
        except curses.error:
            pass


def _start_refresh(tower) -> bool:
    start = getattr(tower, "start_background_refresh", None)
    if callable(start):
        return bool(start())
    tower.load()
    return True


def _apply_refresh(tower) -> bool:
    apply = getattr(tower, "apply_background_refresh", None)
    return bool(apply()) if callable(apply) else False


def _attention_label(row: dict) -> str:
    interaction = row.get("interaction") or {}
    if interaction.get("type") == "choice":
        return t("attention.choice")
    if interaction.get("type") == "text":
        return t("attention.answer")
    if interaction.get("type") == "unknown" or (
        interaction.get("type") == "confirm" and not interaction.get("options")
    ):
        return t("attention.unknown")
    kind = row.get("attention") or "none"
    if kind == "approval_required":
        return t("attention.approval")
    if kind == "input_required":
        return t("attention.input")
    if kind == "error":
        return t("attention.error")
    return ""


def _attention_action(tower, row: dict, action: str) -> str:
    if row.get("attention") != "approval_required":
        return ""
    ok, reason = respond_attention(tower, row.get("key"), action)
    if reason == "approval_unknown":
        return t("control.approve_unknown")
    if not ok:
        return t("nav.stale") if reason in ("stale", "not_found", "not_approval") else (reason or "")
    return t("control.approved") if action == "approve" else t("control.rejected")


def _mark_shown(tower, pane_key: str, payload: dict) -> None:
    tracker = getattr(tower, "results", None)
    fingerprint = payload.get("fingerprint") or ""
    if tracker is not None and fingerprint:
        row = next((item for item in tower.rows if item.get("key") == pane_key and not item.get("placeholder")), None)
        tracker.mark_read(pane_key, fingerprint, identity=pane_result_identity(row) if row else None)


def _ask_copy_destination(stdscr, *, allow_terminal: bool = True) -> str:
    """Ask which one place should receive this copy. Cancel writes nothing."""

    choices = [(MANUAL[0], t("copy.computer"))]
    if allow_terminal:
        choices.append((MANUAL[1], t("copy.terminal_short")))
    choices.append((MANUAL[2], t("copy.tower")))
    pick = run_list_picker(
        stdscr,
        t("copy.choose_destination") if not allow_terminal else t("copy.ask_title"),
        choices,
        preamble=[t("copy.choose_destination") if not allow_terminal else t("copy.ask_session")],
        footer_hint=t("wizard.hint_list"),
    )
    allowed = {key for key, _label in choices}
    if pick.cancelled or pick.selected_key not in allowed:
        return ""
    return str(pick.selected_key)


def _copy_result(tower, pane_key: str, stdscr=None) -> str:
    """Copy the extracted final body. The live screen is not included.

    The Tower copy box is not a computer clipboard, so it does not mark
    the result read. An unclear client is asked, never guessed.
    """

    tower._result_copy_trace_failed = False
    tower.last_result_copy_diagnostic = {
        "stage": "entered", "outcome": "pending", "reason_code": "UNKNOWN",
    }
    if not _result_copy_trace(tower, "Y_HANDLER_ENTERED"):
        return t("control.clipboard_failed")
    try:
        notice = _copy_result_impl(tower, pane_key, stdscr)
    except Exception as exc:
        _result_copy_trace(tower, "Y_RETURN_REASON", reason_code="UNKNOWN", error_type=type(exc).__name__)
        raise
    last = getattr(tower, "last_result_copy_diagnostic", {}) or {}
    ok = _result_copy_trace(
        tower, "Y_RETURN_REASON",
        reason_code=str(last.get("reason_code") or "NONE"), outcome=str(last.get("outcome") or "UNKNOWN"),
    )
    return notice if ok and not getattr(tower, "_result_copy_trace_failed", False) else t("control.clipboard_failed")


def _copy_result_impl(tower, pane_key: str, stdscr=None) -> str:

    row = find_row(tower, pane_key)
    if row is None or row.get("placeholder"):
        return t("nav.stale")
    pane_pid = str(row.get("pane_pid") or "")
    if not row.get("remote") and not pane_pid:
        payload = {"state": row.get("result_state") or "none", "reason_code": "UNKNOWN"}
        _remember_result_copy(tower, row, payload, "not_entered", "blocked", "UNKNOWN")
        return t("control.result_copy_unavailable").format(code="UNKNOWN")
    result = get_result(tower, pane_key) if row.get("remote") else get_result(
        tower, pane_key, expected_pane_pid=pane_pid,
    )
    ok, reason, payload = result
    text = payload.get("text") if isinstance(payload, dict) else None
    complete = bool(
        ok and isinstance(payload, dict) and payload.get("complete") is True
        and isinstance(text, str) and bool(text)
    )
    raw = text.encode("utf-8", "strict") if complete else b""
    if not _result_copy_trace(
        tower, "GET_RESULT_COMPLETE", result_complete=complete,
        turn_complete=payload.get("turn_complete") is True if isinstance(payload, dict) else False,
        body_complete=payload.get("body_complete") is True if isinstance(payload, dict) else False,
        result_utf8_bytes=len(raw) if complete else 0,
        result_sha256=hashlib.sha256(raw).hexdigest() if complete else "",
        provider=str(payload.get("source") or payload.get("result_provider_type") or "UNKNOWN")[:48]
        if isinstance(payload, dict) else "UNKNOWN",
        reason_code=_result_failure_code(row, payload if isinstance(payload, dict) else {}, reason) if not complete else "NONE",
    ):
        _remember_result_copy(tower, row, payload if isinstance(payload, dict) else {}, "not_entered", "failed", "DIAGNOSTIC_TRACE_FAILED")
        return t("control.clipboard_failed")
    if not ok:
        code = _result_failure_code(row, payload, reason)
        _remember_result_copy(tower, row, payload, "not_entered", "blocked", code)
        if reason == "not_found":
            return t("nav.stale")
        return _result_copy_block_notice(code, payload) if reason == "stale" else reason or ""
    text = payload.get("text") or ""
    if not payload.get("complete") or not text:
        reason_code = _result_failure_code(row, payload)
        _remember_result_copy(tower, row, payload, "not_entered", "blocked", reason_code)
        return _result_copy_block_notice(reason_code, payload)
    if payload.get("source") == "remote_opencode" and plain_clipboard_text(text).encode("utf-8", "strict") != raw:
        reason_code = "CLIPBOARD_TEXT_TRANSFORMED"
        _remember_result_copy(tower, row, payload, "not_entered", "blocked", reason_code)
        return t("control.result_copy_unavailable").format(code=reason_code)
    outcome, reason = _route_clipboard(tower, text, stdscr)
    if outcome is None:
        if reason == "diagnostic_trace_failed":
            _remember_result_copy(tower, row, payload, "entered", "failed", "DIAGNOSTIC_TRACE_FAILED")
            return t("control.clipboard_failed")
        if reason == t("copy.choose_destination"):
            _remember_result_copy(tower, row, payload, "not_entered", "destination_required", "DESTINATION_REQUIRED")
            return reason
        _remember_result_copy(tower, row, payload, "entered", "failed", "CLIPBOARD_FAILED")
        return t("control.clipboard_failed")
    if getattr(tower, "_result_copy_trace_failed", False):
        _remember_result_copy(tower, row, payload, "entered", "failed", "DIAGNOSTIC_TRACE_FAILED")
        return t("control.clipboard_failed")
    if outcome.clipboard:
        _mark_shown(tower, pane_key, payload)
    clipboard_result = (
        "readback_verified" if outcome.clipboard else
        "tmux_buffer" if outcome.buffer else
        "terminal_requested" if outcome.terminal_requested else
        "failed"
    )
    _remember_result_copy(
        tower, row, payload, "entered", clipboard_result,
        str(payload.get("fallback_reason_code") or "")
        if clipboard_result != "failed" else "CLIPBOARD_FAILED",
    )
    if clipboard_result == "failed":
        return t("control.clipboard_failed")
    access = getattr(tower, "last_copy_access_context", None)
    host = access.display_name if access else getattr(tower, "local_host", "") or ""
    notice = _result_copy_notice(outcome, host)
    if payload.get("fallback_reason_code") == "OPENCODE_PLUGIN_NOT_INSTALLED":
        notice += " · " + t("control.opencode_screen_fallback")
    return notice


def pane_diagnostic_lines(tower, selected_row: dict) -> list[str]:
    """Read metadata for this exact pane; never expose prompt or Result text."""

    pane_key = str(selected_row.get("key") or "")
    row = next((item for item in getattr(tower, "rows", ()) if item.get("key") == pane_key), None)
    if row is None or row.get("placeholder") or _pane_binding(row) != _pane_binding(selected_row):
        return [t("diagnostics.stale")]
    pane_pid = str(row.get("pane_pid") or "")
    last = getattr(tower, "last_result_copy_diagnostic", {}) or {}
    same_binding = (
        last.get("pane_key"), last.get("pane_id"), last.get("pane_pid"), last.get("session")
    ) == _pane_binding(row)
    if same_binding:
        turn_complete = last.get("turn_complete")
        body_complete = last.get("body_complete")
        code = _copy_diagnostic_reason_code(last)
        if code == "-" and not (turn_complete is True and body_complete is True):
            code = "UNKNOWN"
        clipboard = f'{last.get("stage", "UNKNOWN")} / {last.get("outcome", "UNKNOWN")}'
    else:
        turn_complete = row.get("turn_complete")
        body_complete = row.get("body_complete")
        code = _result_failure_code(row, {})
        clipboard = "not_attempted"
    return [
        t("diagnostics.pane").format(pane=row.get("pane_id") or "-", pid=pane_pid or "-"),
        t("diagnostics.session").format(tmux=row.get("tmux_host") or "-", session=row.get("session") or "-"),
        t("diagnostics.host").format(
            transport=row.get("transport") or "-", execution=row.get("execution_host") or row.get("host") or "-",
        ),
        t("diagnostics.agent").format(
            detected=row.get("auto_detected_agent") or row.get("auto_agent") or "-",
            adapter=row.get("result_adapter") or "UNKNOWN",
        ),
        t("diagnostics.provider").format(
            provider=row.get("result_provider_type") or "UNKNOWN",
            state=(last.get("result_state") if same_binding else "") or row.get("result_state") or "UNKNOWN",
        ),
        t("diagnostics.completeness").format(
            turn=_truth(turn_complete), body=_truth(body_complete),
        ),
        t("diagnostics.reason").format(code=code),
        t("diagnostics.clipboard").format(result=clipboard),
    ]


def result_copy_status_lines(tower) -> list[str]:
    """Display only the current copy stage, outcome, and reason code."""

    last = getattr(tower, "last_result_copy_diagnostic", {}) or {}
    stage = str(last.get("stage") or "UNKNOWN")
    outcome = str(last.get("outcome") or "UNKNOWN")
    return [
        t("diagnostics.clipboard").format(result=f"{stage} / {outcome}"),
        t("diagnostics.reason").format(code=_copy_diagnostic_reason_code(last)),
    ]


def _copy_diagnostic_reason_code(diagnostic: dict) -> str:
    reason = diagnostic.get("reason_code")
    if reason:
        return str(reason)
    if diagnostic.get("outcome") in {"readback_verified", "tmux_buffer", "terminal_requested"}:
        return "NONE"
    return "UNKNOWN"


def _pane_binding(row: dict) -> tuple[str, str, str, str]:
    return (
        str(row.get("key") or ""), str(row.get("pane_id") or ""),
        str(row.get("pane_pid") or ""), str(row.get("session") or ""),
    )


def _truth(value) -> str:
    return "true" if value is True else "false" if value is False else "UNKNOWN"


def _result_failure_code(row: dict, payload: dict, reason: str = "") -> str:
    code = str(payload.get("reason_code") or payload.get("source") or "")
    if code in {
        "AGENT_UNSUPPORTED", "AGENT_UNBOUND", "PROVIDER_UNBOUND", "TURN_NOT_COMPLETE", "SOURCE_PARTIAL",
        "OUTPUT_RANGE_EXCEEDED", "CLIPBOARD_FAILED", "UNKNOWN",
        "REMOTE_PROVIDER_UNBOUND", "REMOTE_PROVIDER_UNAVAILABLE", "REMOTE_RESULT_INCOMPLETE",
        "AGENT_SESSION_UNBOUND", "REMOTE_PROVIDER_TIMEOUT", "OPENCODE_BINDING_INVALID",
        "OPENCODE_NO_ACTIVE_SESSION", "SESSION_BINDING_CHANGED",
    }:
        return code
    if reason == "stale":
        return "UNKNOWN"
    if row.get("result_adapter") == "Shell":
        return "AGENT_UNSUPPORTED"
    if payload.get("turn_complete") is False:
        return "TURN_NOT_COMPLETE"
    if payload.get("body_complete") is False:
        return "SOURCE_PARTIAL"
    return "UNKNOWN"


def _result_copy_block_notice(code: str, payload: dict) -> str:
    if payload.get("fallback_reason_code") == "OPENCODE_PLUGIN_NOT_INSTALLED":
        return t("control.opencode_screen_fallback_blocked").format(code=code)
    message_key = {
        "OPENCODE_BINDING_INVALID": "control.opencode_binding_invalid",
        "OPENCODE_NO_ACTIVE_SESSION": "control.opencode_no_active_session",
        "SESSION_BINDING_CHANGED": "control.opencode_session_changed",
    }.get(code)
    if message_key:
        return t(message_key)
    return t("control.result_copy_unavailable").format(code=code)


def _remember_result_copy(tower, row: dict, payload: dict, stage: str, outcome: str, reason_code: str) -> None:
    """Store copy diagnostics only; never retain body text or clipboard data."""

    tower.last_result_copy_diagnostic = {
        "pane_key": str(row.get("key") or ""),
        "pane_id": str(row.get("pane_id") or ""),
        "pane_pid": str(row.get("pane_pid") or ""),
        "session": str(row.get("session") or ""),
        "stage": stage,
        "outcome": outcome,
        "provider_type": str(row.get("result_provider_type") or "UNKNOWN"),
        "result_state": str(payload.get("state") or row.get("result_state") or "UNKNOWN"),
        "turn_complete": payload.get("turn_complete"),
        "body_complete": payload.get("body_complete"),
        "reason_code": reason_code or "-",
        "fallback_reason_code": str(payload.get("fallback_reason_code") or "-"),
    }


def _copy_screen(tower, pane_key: str, stdscr=None, *, expected_pane_pid: str = "") -> str:
    """Copy the explicitly requested recent terminal output, not a Result."""

    row = find_row(tower, pane_key)
    if row is None or row.get("placeholder"):
        return t("nav.stale")
    if row.get("remote"):
        return t("control.remote_unsupported")
    pane_pid = str(row.get("pane_pid") or "")
    if not pane_pid or (expected_pane_pid and pane_pid != expected_pane_pid):
        return t("nav.stale")
    ok, reason, payload = get_pane_screen(
        getattr(tower, "session", "") or "",
        pane_key,
        getattr(tower, "own_pane_id", "") or "",
        expected_pane_pid=expected_pane_pid or pane_pid,
    )
    if not ok:
        return t("control.remote_unsupported") if reason == "remote_unsupported" else t("nav.stale")
    lines = payload.get("lines") or []
    text = "\n".join(lines)
    if not text:
        return t("control.no_screen")
    outcome, reason = _route_clipboard(tower, text, stdscr)
    if outcome is None:
        return reason or t("control.copy_failed")
    host = (
        getattr(getattr(tower, "last_copy_access_context", None), "display_name", "")
        or getattr(tower, "local_host", "") or ""
    )
    notice = _copy_notice(outcome, host)
    if outcome.clipboard or outcome.buffer or outcome.terminal_requested:
        key = "control.copied_screen_truncated" if payload.get("truncated") else "control.copied_screen"
        notice += " · " + t(key).format(lines=len(lines))
    return notice


def _route_clipboard(tower, text: str, stdscr=None):
    """Route an already-resolved payload to exactly one Access Client."""

    raw = text.encode("utf-8", "strict")
    if not _result_copy_trace(
        tower, "ROUTER_ENTERED", router_payload_utf8_bytes=len(raw),
        router_payload_sha256=hashlib.sha256(raw).hexdigest(),
    ):
        return None, "diagnostic_trace_failed"
    client, clients = observe()
    tower.last_copy_access_context = client.access_context
    if not _result_copy_trace(
        tower, "ACCESS_CLIENT_STATE", access_client_ambiguous=bool(client.ambiguous),
        access_client_tty_present=bool(client.tty),
    ):
        return None, "diagnostic_trace_failed"
    destination = ""
    if client.ambiguous or not client.tty:
        choice = _ask_copy_destination(stdscr, allow_terminal=False) if stdscr is not None else ""
        if choice not in (MANUAL[0], MANUAL[2]):
            return None, t("copy.choose_destination")
        if clients:
            tower.copy_override = SessionChoice(clients, choice)
        destination = choice
    else:
        plan = resolve_plan(
            load_preference(),
            client,
            clients,
            getattr(tower, "copy_override", None),
        )
        destination = plan.destination
        if plan.ask:
            choice = _ask_copy_destination(stdscr) if stdscr is not None else ""
            if choice not in MANUAL:
                return None, t("copy.choose_destination")
            if clients:
                tower.copy_override = SessionChoice(clients, choice)
            destination = choice
    bridge = load_bridge_client(client.bridge_id) if client.bridge_id else None
    if client.bridge_id and bridge is None:
        return None, t("control.copy_failed")
    destination = route_for_access_client(destination, client)
    if not _result_copy_trace(tower, "SELECTED_DESTINATION", destination=destination):
        return None, "diagnostic_trace_failed"

    def before_clipboard_write(kind: str, tool: str, byte_count: int, digest: str) -> bool:
        return _result_copy_trace(
            tower, "CLIPBOARD_WRITE_ATTEMPTED", destination=kind,
            tool=os.path.basename(tool)[:48], clipboard_payload_utf8_bytes=byte_count,
            clipboard_payload_sha256=digest,
        )

    copy_kwargs = dict(
        destination=destination,
        client=None if client.ambiguous else client.tty,
        ambiguous=client.ambiguous,
        ssh=client.ssh,
        bridge_port=bridge.port if bridge else None,
        bridge_token=bridge.token if bridge else None,
    )
    if os.environ.get(_COPY_TRACE_ENV):
        copy_kwargs["diagnostic_callback"] = before_clipboard_write
    outcome = copy_text(text, **copy_kwargs)
    return outcome, ""


def _result_copy_trace(tower, event_name: str, **fields) -> bool:
    """Append only allowlisted Result-copy metadata when tracing is opted in."""

    path = os.environ.get(_COPY_TRACE_ENV)
    if not path:
        return True
    allowed = {
        "result_complete", "turn_complete", "body_complete", "result_utf8_bytes", "result_sha256",
        "provider", "reason_code", "router_payload_utf8_bytes", "router_payload_sha256",
        "access_client_ambiguous", "access_client_tty_present", "destination", "tool",
        "clipboard_payload_utf8_bytes", "clipboard_payload_sha256", "outcome", "error_type",
    }
    record = {"event": event_name, "time": time.time()}
    record.update({key: value for key, value in fields.items() if key in allowed})
    try:
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags, 0o600)
        try:
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o077
                or info.st_size > 512 * 1024
            ):
                raise OSError("unsafe diagnostic trace file")
            data = (json.dumps(record, ensure_ascii=True, separators=(",", ":")) + "\n").encode("utf-8")
            offset = 0
            while offset < len(data):
                offset += os.write(fd, data[offset:])
            os.fsync(fd)
        finally:
            os.close(fd)
        return True
    except Exception as exc:
        tower._result_copy_trace_failed = True
        tower.last_result_copy_diagnostic = {
            "stage": "trace_failed", "outcome": "failed", "reason_code": "DIAGNOSTIC_TRACE_FAILED",
            "trace_error_type": type(exc).__name__,
        }
        return False


def _result_copy_notice(outcome, host_label: str = "") -> str:
    """Confirm a complete result copy and name the single destination."""

    if outcome.tool == "terminal" and outcome.clipboard:
        return t("control.copied_complete_terminal")
    if outcome.terminal_requested:
        return t("control.sent_complete_terminal")
    if outcome.clipboard:
        host = (host_label or "").strip()
        if host:
            return t("control.copied_complete_host").format(host=host)
        return t("control.copied_complete")
    if outcome.buffer:
        return t("control.copied_complete_buffer")
    return t("control.copy_failed")


def _copy_notice(outcome, host_label: str = "") -> str:
    """Name the one destination this copy used, in plain words.

    A computer success is claimed only after that clipboard reads back.
    A terminal send that was not read back does not claim the computer.
    """

    if outcome.tool == "terminal" and outcome.clipboard:
        return t("control.copied_terminal")
    if outcome.terminal_requested:
        return t("control.copied_terminal_requested")
    if outcome.clipboard:
        host = (host_label or "").strip()
        if host:
            return t("control.copied_windows").format(host=host)
        return t("control.copied_host")
    if outcome.buffer:
        return t("control.copied_buffer")
    return t("control.copy_failed")


def _show_result(tower, pane_key: str) -> tuple:
    """Show the extracted body. A visible body is then marked read."""

    ok, reason, payload = get_result(tower, pane_key)
    if not ok:
        return reason or "", False
    if not payload.get("complete") or not payload.get("text"):
        return t("control.result_incomplete"), False
    _mark_shown(tower, pane_key, payload)
    return t("control.result_shown"), True


def _go(tower, row: dict) -> str:
    if row.get("remote") or not row.get("pane_id"):
        return t("control.remote_unsupported")
    ok, reason = focus_pane(tower.session, row["pane_id"], tower.own_pane_id)
    if not ok:
        tower.notice = "stale"
        return t("nav.stale")
    return t("control.focused")


def _close(stdscr, tower, row: dict) -> bool:
    """True when the view should close because the pane is gone."""

    if row.get("pane_id") and row.get("pane_id") == tower.own_pane_id:
        _message(stdscr, t("control.tower_protected"))
        return False
    if row.get("remote") or not str(row.get("key") or "").startswith("%"):
        _message(stdscr, t("control.remote_unsupported"))
        return False
    if row.get("status") == "DEAD":
        tower.load()
        return True

    pick = run_list_picker(
        stdscr,
        t("control.close_title"),
        close_choices(),
        footer_hint=t("wizard.hint_list"),
        preamble=close_notice_lines(row.get("status") or "", row.get("attention") or "none"),
    )
    if pick.cancelled or pick.selected_key != "close":
        return False
    ok, reason = close_pane(tower.session, row["pane_id"], tower.own_pane_id)
    if not ok:
        _message(stdscr, t("nav.stale") if reason in ("stale", "not_found") else t("control.tower_protected") if reason == "tower_pane" else reason)
        return False
    tower.load()
    return True


def _message(stdscr, text: str) -> None:
    height, _width = stdscr.getmaxyx()
    safe_add(stdscr, height - 2, 2, text, curses.A_BOLD)
    stdscr.refresh()
    time.sleep(0.6)
