"""Thin, defensive wrapper around the ``tmux`` binary.

Every call swallows exceptions and returns an empty result on failure
(missing binary, no server running, pane vanished mid-call, etc.) so a
transient tmux hiccup degrades a single row to UNKNOWN instead of crashing
the whole TUI.
"""

from __future__ import annotations

import os
import subprocess
from typing import Sequence

DEFAULT_TIMEOUT = 3.0


def run_tmux(args: Sequence[str], capture: bool = True, timeout: float = DEFAULT_TIMEOUT) -> str:
    try:
        result = subprocess.run(
            ["tmux", *args],
            stdin=subprocess.DEVNULL,
            text=True,
            capture_output=capture,
            timeout=timeout,
            check=False,
        )
    except Exception:
        return ""

    if capture:
        return (result.stdout or "").rstrip("\n")
    return ""


def capture_pane(pane_id: str, lines: int = 30, *, join_wrapped: bool = False) -> list:
    """Recent terminal output with trailing blank rows removed.

    A tall pane whose TUI draws in its upper part leaves dozens of empty
    rows below it; adapters read the bottom N lines, so those rows would
    hide the real widget (caught live: a 94-row OpenCode pane read as
    UNKNOWN while its idle footer sat 41 lines above the bottom).
    """

    args = ["capture-pane", "-p"]
    if join_wrapped:
        args.append("-J")
    args.extend(("-t", pane_id, "-S", f"-{lines}"))
    output = run_tmux(args)
    if not output:
        return []
    rows = output.split("\n")
    while rows and not rows[-1].strip():
        rows.pop()
    return rows


def pane_history_position(pane_id: str):
    """Return numeric history size/limit for a target pane, without its text."""

    raw = run_tmux([
        "display-message", "-p", "-t", pane_id,
        "#{history_size}\t#{history_limit}",
    ])
    try:
        size, limit = raw.strip().split("\t", 1)
        size, limit = int(size), int(limit)
    except (TypeError, ValueError):
        return None
    if size < 0 or limit < 1 or size > limit:
        return None
    return size, limit


def select_pane(session: str, window_index: str, pane_id: str) -> None:
    run_tmux(["select-window", "-t", f"{session}:{window_index}"], capture=False)
    run_tmux(["select-pane", "-t", pane_id], capture=False)


def list_windows(session: str) -> list:
    output = run_tmux(["list-windows", "-t", session, "-F", "#{window_name}"])
    return [line for line in output.split("\n") if line]


def current_session() -> str:
    pane_id = os.environ.get("TMUX_PANE", "").strip()
    if pane_id:
        return run_tmux(["display-message", "-p", "-t", pane_id, "#{session_name}"]).strip()
    return run_tmux(["display-message", "-p", "#S"]).strip()


def current_pane_id() -> str:
    """The pane_id of whichever pane *this process itself* is running in.

    Reads ``$TMUX_PANE``, which tmux sets in every pane's own shell
    environment -- NOT ``tmux display-message -p '#{pane_id}'`` without a
    ``-t``, which resolves to the *attached client's currently active
    pane*. Those are usually the same pane, but not always: a real bug
    surfaced this when a script switched the client to a different window
    right before this process started -- `display-message` then reported
    that other, unrelated pane as "current", registering the wrong pane_id
    entirely. `$TMUX_PANE` has no such ambiguity.
    """

    return os.environ.get("TMUX_PANE", "").strip()


def pane_exists(pane_id: str) -> bool:
    output = run_tmux(["list-panes", "-a", "-F", "#{pane_id}"])
    return pane_id in output.split("\n")


def pane_is_live_in_session(pane_id: str, session: str) -> bool:
    """True only when ``pane_id`` is a living pane of ``session``.

    ``list-panes -s`` is limited to that session, so a pane that merely
    exists somewhere else on the server does not count. ``#{pane_dead}``
    drops a pane whose process has already exited.
    """

    if not pane_id or not session:
        return False
    output = run_tmux(["list-panes", "-s", "-t", session, "-F", "#{pane_id}\t#{pane_dead}"])
    for line in output.split("\n"):
        parts = line.split("\t")
        if len(parts) >= 2 and parts[0] == pane_id:
            return parts[1] != "1"
    return False


def session_exists(session: str) -> bool:
    """Exact-name check. ``tmux has-session -t NAME`` also accepts prefix
    and glob matches, so a deleted ``scratch`` would still "exist" while
    ``scratch-2`` is around; comparing against the full list avoids that.
    """

    if not session:
        return False
    output = run_tmux(["list-sessions", "-F", "#{session_name}"])
    return session in output.split("\n")


def set_session_option(session: str, name: str, value: str) -> None:
    run_tmux(["set-option", "-t", session, name, value], capture=False)


def get_session_option(session: str, name: str) -> str:
    return run_tmux(["show-options", "-t", session, "-v", name]).strip()


def unset_session_option(session: str, name: str) -> None:
    run_tmux(["set-option", "-u", "-t", session, name], capture=False)


def display_message(message: str) -> None:
    run_tmux(["display-message", message], capture=False)
