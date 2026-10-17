"""Read-only tmux identity probes and pane navigation actions.

Nothing in this module sends keystrokes into a pane, kills a pane/process,
or restarts anything. Identity reads do not focus a pane. Focus helpers only
move the user's cursor between windows/panes that already exist -- the same
as the user pressing ``Ctrl+b`` themselves.

An identity read is point-in-time. tmux provides no atomic "select only if
this full identity still matches" operation, so a future focus action must
read and compare the identity again immediately before selecting. A race can
still occur between that read and selection; callers must stop on any
mismatch and must not send input as part of the handoff.
"""

from __future__ import annotations

from dataclasses import dataclass
import subprocess

from . import capture

_IDENTITY_FIELD_SEP = "\x1f"
_LOCAL_PANE_IDENTITY_FORMAT = _IDENTITY_FIELD_SEP.join((
    "#{session_name}", "#{session_id}", "#{window_id}",
    "#{pane_id}", "#{pane_pid}", "#{pane_dead}",
))


@dataclass(frozen=True)
class LocalPaneIdentity:
    """Physical identity read from the current local tmux server."""

    session: str
    session_id: str
    window_id: str
    pane_id: str
    pane_pid: str
    pane_dead: str


def read_local_pane_identities() -> tuple[LocalPaneIdentity, ...] | None:
    """Read all local tmux pane identities without changing tmux state.

    ``None`` means the query or its output could not be verified; an empty
    tuple is a successful query with no panes. This distinction lets callers
    fail closed instead of treating a tmux outage as a missing target.
    """

    try:
        result = subprocess.run(
            ["tmux", "list-panes", "-a", "-F", _LOCAL_PANE_IDENTITY_FORMAT],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=3.0,
            check=False,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None

    output = result.stdout or ""
    if not output:
        return ()
    rows = []
    for line in output.splitlines():
        if not line:
            continue
        values = line.replace(r"\037", _IDENTITY_FIELD_SEP).split(_IDENTITY_FIELD_SEP)
        if len(values) != 6:
            return None
        session, session_id, window_id, pane_id, pane_pid, pane_dead = values
        if (
            not session or not session_id or not window_id.startswith("@")
            or not pane_id.startswith("%") or not pane_pid.isdecimal()
            or pane_dead not in {"0", "1"}
        ):
            return None
        rows.append(LocalPaneIdentity(
            session, session_id, window_id, pane_id, pane_pid, pane_dead,
        ))
    return tuple(rows)


def _panes_in_session(session: str) -> set:
    output = capture.run_tmux(["list-panes", "-s", "-t", session, "-F", "#{pane_id}"])
    return {line for line in output.split("\n") if line}


def focus_local_pane(session: str, pane_id: str) -> bool:
    """Move the attached client to ``pane_id`` inside ``session``.

    The pane id is the target. A renamed or rearranged window does not
    matter, and a stale id is refused: nothing is selected, and no keys
    are sent into the pane.
    """

    if not session or not pane_id or not pane_id.startswith("%"):
        return False
    if pane_id not in _panes_in_session(session):
        return False
    if not capture.pane_exists(pane_id):
        return False
    try:
        for args in (["select-window", "-t", pane_id], ["select-pane", "-t", pane_id]):
            result = subprocess.run(
                ["tmux", *args], stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=3.0, check=False,
            )
            if result.returncode != 0:
                return False
    except Exception:
        return False
    return True


def active_pane_in_window(session: str, window_index: str) -> str:
    """The live active pane id of ``session:window_index``, or ``""``.

    Does not move the client. A missing window is an empty string.
    """

    if not session or window_index is None or window_index == "":
        return ""
    output = capture.run_tmux(
        [
            "list-panes",
            "-t",
            f"{session}:{window_index}",
            "-F",
            "#{pane_id}\t#{pane_active}",
        ]
    )
    active = ""
    first = ""
    for line in output.split("\n"):
        parts = line.split("\t")
        if len(parts) < 2 or not parts[0].startswith("%"):
            continue
        if not first:
            first = parts[0]
        if parts[1] == "1":
            active = parts[0]
    return active or first


def focus_window(session: str, window_index: str) -> bool:
    """Move to the active pane of ``session:window_index``.

    Refuses a window that is already gone. The active pane id is resolved
    at move time, then handed to ``focus_local_pane``.
    """

    target = active_pane_in_window(session, window_index)
    if not target:
        return False
    return focus_local_pane(session, target)


def open_pane(session: str, window_index: str, pane_id: str) -> bool:
    """Enter on a pane. ``window_index`` is not the target; the pane id is."""

    del window_index
    return focus_local_pane(session, pane_id)
