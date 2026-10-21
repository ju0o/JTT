"""Create, move, and close tmux windows and panes by id.

Every write below was checked against tmux 3.6 (``tmux list-commands``
and a detached session). Targets are ``window_id`` (``@12``) and
``pane_id`` (``%34``). A window name is display text passed to ``-n``,
never to ``-t``.

Commands that would steal the client use ``-d``. Nothing here starts a
shell of the user's choosing, passes ``-a`` (which kills every other
pane or window), or deletes a session.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from typing import List, Optional

_WINDOW_ID = re.compile(r"^@\d+$")
_PANE_ID = re.compile(r"^%\d+$")
_TIMEOUT = 3.0

# tmux 3.6 select-layout names. Confirmed with list-commands / a live call.
LAYOUTS = (
    "tiled",
    "even-horizontal",
    "even-vertical",
    "main-horizontal",
    "main-vertical",
)

LAYOUT_PREVIEW = {
    "tiled": "┌┬┐",
    "even-horizontal": "││",
    "even-vertical": "┬",
    "main-horizontal": "┌─┐",
    "main-vertical": "├┤",
}


@dataclass(frozen=True)
class StructureResult:
    ok: bool
    window_id: str = ""
    pane_id: str = ""
    detail: str = ""


def _run(args: List[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["tmux", *args],
        capture_output=True,
        text=True,
        timeout=_TIMEOUT,
        check=False,
    )


def valid_window_id(value: str) -> bool:
    return bool(_WINDOW_ID.match(str(value or "")))


def valid_pane_id(value: str) -> bool:
    return bool(_PANE_ID.match(str(value or "")))


def clean_name(name: str, limit: int = 48) -> str:
    """A display name. Colons are removed so a name cannot look like a target."""

    cleaned = "".join(ch if ch.isprintable() and ch not in ":\n\r\t" else " " for ch in (name or ""))
    return " ".join(cleaned.split())[:limit]


def _ids(output: str) -> List[str]:
    return [part for part in (output or "").replace("\t", " ").split() if part]


def _fail(detail: str) -> StructureResult:
    return StructureResult(False, detail=detail)


def window_ids(session: str) -> List[str]:
    if not session:
        return []
    try:
        result = _run(["list-windows", "-t", session, "-F", "#{window_id}"])
    except Exception:
        return []
    if result.returncode != 0:
        return []
    return [line for line in (result.stdout or "").split("\n") if valid_window_id(line)]


def pane_ids(session: str) -> List[str]:
    if not session:
        return []
    try:
        result = _run(["list-panes", "-s", "-t", session, "-F", "#{pane_id}"])
    except Exception:
        return []
    if result.returncode != 0:
        return []
    return [line for line in (result.stdout or "").split("\n") if valid_pane_id(line)]


def pane_title_identity(session: str, pane_id: str) -> Optional[dict]:
    """Read one live pane's session, id, pid, liveness, and title together."""

    if not session or not valid_pane_id(pane_id):
        return None
    if pane_id not in pane_ids(session):
        return None
    separator = "\t"
    format_string = separator.join(
        ("#{pane_id}", "#{pane_pid}", "#{pane_dead}", "#{pane_title}")
    )
    try:
        result = _run(["display-message", "-p", "-t", pane_id, format_string])
    except Exception:
        return None
    if result.returncode != 0:
        return None
    output = (result.stdout or "").removesuffix("\n")
    parts = output.split(separator, 3)
    if len(parts) != 4:
        return None
    live_pane_id, pane_pid, pane_dead, title = parts
    if live_pane_id != pane_id or pane_dead != "0" or not pane_pid.isdigit():
        return None
    if pane_id not in pane_ids(session):
        return None
    return {"session": session, "pane_id": live_pane_id, "pane_pid": pane_pid, "pane_title": title}


def panes_of_window(window_id: str) -> List[str]:
    if not valid_window_id(window_id):
        return []
    try:
        result = _run(["list-panes", "-t", window_id, "-F", "#{pane_id}"])
    except Exception:
        return []
    if result.returncode != 0:
        return []
    return [line for line in (result.stdout or "").split("\n") if valid_pane_id(line)]


def current_window_id(session: str) -> str:
    """The session's current window. Does not change which window that is."""

    if not session:
        return ""
    try:
        result = _run(["display-message", "-p", "-t", session, "#{window_id}"])
    except Exception:
        return ""
    text = (result.stdout or "").strip()
    return text if valid_window_id(text) else ""


def pane_window_id(pane_id: str) -> str:
    if not valid_pane_id(pane_id):
        return ""
    try:
        result = _run(["display-message", "-p", "-t", pane_id, "#{window_id}"])
    except Exception:
        return ""
    text = (result.stdout or "").strip()
    return text if valid_window_id(text) else ""


def list_windows(session: str) -> List[dict]:
    if not session:
        return []
    try:
        result = _run(
            [
                "list-windows",
                "-t",
                session,
                "-F",
                "#{window_id}\t#{window_index}\t#{window_name}\t#{window_panes}",
            ]
        )
    except Exception:
        return []
    if result.returncode != 0:
        return []
    rows = []
    for line in (result.stdout or "").split("\n"):
        parts = line.split("\t")
        if len(parts) < 4 or not valid_window_id(parts[0]):
            continue
        rows.append(
            {
                "window_id": parts[0],
                "window_index": parts[1],
                "window_name": parts[2],
                "pane_count": parts[3],
            }
        )
    return rows


def window_layout(session: str, window_id: str) -> str:
    """Current layout token for one verified window, for safe rollback."""

    if not valid_window_id(window_id) or window_id not in window_ids(session):
        return ""
    try:
        result = _run(["display-message", "-p", "-t", window_id, "#{window_layout}"])
    except Exception:
        return ""
    return (result.stdout or "").strip() if result.returncode == 0 else ""


def create_window(session: str, name: str = "", cwd: str = "") -> StructureResult:
    """``new-window -d -P``. The client stays put. Returns both ids."""

    if not session or any(ch in session for ch in "\n\r\t"):
        return _fail("bad_session")
    args = ["new-window", "-d", "-P", "-F", "#{window_id}\t#{pane_id}", "-t", f"{session}:"]
    cleaned = clean_name(name)
    if cleaned:
        args += ["-n", cleaned]
    if cwd and "\n" not in cwd and "\r" not in cwd:
        args += ["-c", cwd]
    try:
        result = _run(args)
    except Exception:
        return _fail("create_failed")
    fields = _ids(result.stdout)
    if result.returncode != 0 or len(fields) < 2:
        return _fail("create_failed")
    window_id, pane_id = fields[0], fields[1]
    if not valid_window_id(window_id) or not valid_pane_id(pane_id):
        return _fail("create_failed")
    if window_id not in window_ids(session):
        return _fail("create_failed")
    return StructureResult(True, window_id, pane_id)


def create_pane(window_id: str, direction: str = "auto", cwd: str = "") -> StructureResult:
    """``split-window -d -P -t <window_id>``.

    ``horizontal`` is ``-h`` (left/right). ``vertical`` is ``-v`` (top/bottom).
    ``auto`` omits both and lets tmux choose. The printed window id must be
    the one we asked for.
    """

    if not valid_window_id(window_id):
        return _fail("bad_window")
    if direction not in ("auto", "horizontal", "vertical"):
        return _fail("bad_direction")
    args = ["split-window", "-d"]
    if direction == "horizontal":
        args.append("-h")
    elif direction == "vertical":
        args.append("-v")
    args += ["-P", "-F", "#{window_id}\t#{pane_id}", "-t", window_id]
    if cwd and "\n" not in cwd and "\r" not in cwd:
        args += ["-c", cwd]
    try:
        result = _run(args)
    except Exception:
        return _fail("create_failed")
    fields = _ids(result.stdout)
    if result.returncode != 0 or len(fields) < 2:
        return _fail("create_failed")
    got_window, pane_id = fields[0], fields[1]
    if got_window != window_id or not valid_pane_id(pane_id):
        return _fail("wrong_window")
    return StructureResult(True, window_id, pane_id)


def move_pane(session: str, pane_id: str, window_id: str) -> StructureResult:
    """``move-pane -d -s <pane_id> -t <window_id>`` on tmux 3.6.

    ``-t`` accepts the destination window id. The pane id does not change.
    """

    if not valid_pane_id(pane_id) or not valid_window_id(window_id):
        return _fail("bad_target")
    if pane_id not in pane_ids(session) or window_id not in window_ids(session):
        return _fail("not_found")
    source = pane_window_id(pane_id)
    if source == window_id:
        return _fail("already_there")
    try:
        result = _run(["move-pane", "-d", "-s", pane_id, "-t", window_id])
    except Exception:
        return _fail("move_failed")
    if result.returncode != 0:
        return _fail("move_failed")
    if pane_window_id(pane_id) != window_id:
        return _fail("move_failed")
    return StructureResult(True, window_id, pane_id)


def break_pane(session: str, pane_id: str, name: str = "") -> StructureResult:
    """``break-pane -d -P -s <pane_id>``. A new window id is required.

    tmux 3.6 keeps the same window when the pane is already alone. That is
    not a split, so it is reported as ``already_alone`` and nothing is renamed.
    """

    if not valid_pane_id(pane_id) or pane_id not in pane_ids(session):
        return _fail("not_found")
    source = pane_window_id(pane_id)
    siblings = panes_of_window(source)
    if len(siblings) <= 1:
        return _fail("already_alone")
    # Without -t, tmux 3.6 creates the window in the attached client's
    # session, not in the pane's session. -t session: keeps it here.
    args = [
        "break-pane", "-d", "-P", "-F", "#{window_id}\t#{pane_id}",
        "-s", pane_id, "-t", f"{session}:",
    ]
    cleaned = clean_name(name)
    if cleaned:
        args += ["-n", cleaned]
    try:
        result = _run(args)
    except Exception:
        return _fail("break_failed")
    fields = _ids(result.stdout)
    if result.returncode != 0 or len(fields) < 2:
        return _fail("break_failed")
    window_id, got_pane = fields[0], fields[1]
    if got_pane != pane_id or not valid_window_id(window_id) or window_id == source:
        return _fail("break_failed")
    if pane_window_id(pane_id) != window_id:
        return _fail("break_failed")
    return StructureResult(True, window_id, pane_id)


def apply_layout(session: str, window_id: str, layout: str) -> StructureResult:
    """``select-layout -t <window_id> <layout>``. No other window is named."""

    if layout not in LAYOUTS or not valid_window_id(window_id):
        return _fail("bad_layout")
    if window_id not in window_ids(session):
        return _fail("not_found")
    try:
        result = _run(["select-layout", "-t", window_id, layout])
    except Exception:
        return _fail("layout_failed")
    if result.returncode != 0:
        return _fail("layout_failed")
    return StructureResult(True, window_id)


def restore_layout(session: str, window_id: str, layout_token: str) -> StructureResult:
    """Restore one captured tmux layout token after a failed safe operation."""

    if (not valid_window_id(window_id) or window_id not in window_ids(session)
            or not isinstance(layout_token, str) or len(layout_token) > 4096
            or "," not in layout_token or any(ch not in "0123456789abcdefx," for ch in layout_token)):
        return _fail("bad_layout")
    try:
        result = _run(["select-layout", "-t", window_id, layout_token])
    except Exception:
        return _fail("layout_failed")
    return StructureResult(result.returncode == 0, window_id, detail="" if result.returncode == 0 else "layout_failed")


def rename_window(session: str, window_id: str, name: str) -> StructureResult:
    """``rename-window -t <window_id>``. The id stays the identity."""

    if not valid_window_id(window_id) or window_id not in window_ids(session):
        return _fail("not_found")
    cleaned = clean_name(name)
    if not cleaned:
        return _fail("bad_name")
    try:
        result = _run(["rename-window", "-t", window_id, cleaned])
    except Exception:
        return _fail("rename_failed")
    if result.returncode != 0:
        return _fail("rename_failed")
    return StructureResult(True, window_id)


def rename_pane(
    session: str,
    pane_id: str,
    title: str,
    expected_pane_pid: Optional[str] = None,
    *,
    allow_empty: bool = False,
) -> StructureResult:
    """``select-pane -t <pane_id> -T <title>``.

    tmux 3.6's ``-d`` on select-pane disables input, so it is not passed.
    A live check showed ``-T`` alone does not move the client.
    """

    identity = pane_title_identity(session, pane_id)
    if identity is None:
        return _fail("not_found")
    if expected_pane_pid and identity["pane_pid"] != str(expected_pane_pid).strip():
        return _fail("stale")
    cleaned = "" if allow_empty and title == "" else clean_name(title, limit=80)
    if not cleaned and not (allow_empty and title == ""):
        return _fail("bad_name")
    if identity["pane_title"] == cleaned:
        return StructureResult(True, pane_id=pane_id)
    try:
        result = _run(["select-pane", "-t", pane_id, "-T", cleaned])
    except Exception:
        return _fail("rename_failed")
    if result.returncode != 0:
        return _fail("rename_failed")
    return StructureResult(True, pane_id=pane_id)


def kill_pane(session: str, pane_id: str, own_pane_id: str = "") -> StructureResult:
    """``kill-pane -t <pane_id>`` for one pane. Never ``-a``.

    Refuses Tower's pane and the last pane in the session (that would
    destroy the session).
    """

    if own_pane_id and pane_id == own_pane_id:
        return _fail("tower_pane")
    if not valid_pane_id(pane_id):
        return _fail("bad_target")
    living = pane_ids(session)
    if pane_id not in living:
        return _fail("not_found")
    if len(living) <= 1:
        return _fail("last_pane")
    try:
        result = _run(["kill-pane", "-t", pane_id])
    except Exception:
        return _fail("close_failed")
    if result.returncode != 0:
        return _fail("close_failed")
    return StructureResult(True, pane_id=pane_id)


def kill_window(session: str, window_id: str, own_pane_id: str = "") -> StructureResult:
    """``kill-window -t <window_id>``. Never ``-a``.

    Refuses a window that contains Tower's pane, and the last window in
    the session.
    """

    if not valid_window_id(window_id):
        return _fail("bad_window")
    windows = window_ids(session)
    if window_id not in windows:
        return _fail("not_found")
    inside = panes_of_window(window_id)
    if own_pane_id and own_pane_id in inside:
        return _fail("tower_pane")
    if len(windows) <= 1:
        return _fail("last_window")
    try:
        result = _run(["kill-window", "-t", window_id])
    except Exception:
        return _fail("close_failed")
    if result.returncode != 0:
        return _fail("close_failed")
    return StructureResult(True, window_id)


def window_close_block(pane_ids_in_window: List[str], own_pane_id: str, window_count: int) -> Optional[str]:
    """Why a window must not be closed, or None when a confirmed close may proceed."""

    if own_pane_id and own_pane_id in pane_ids_in_window:
        return "tower_pane"
    if window_count <= 1:
        return "last_window"
    return None
