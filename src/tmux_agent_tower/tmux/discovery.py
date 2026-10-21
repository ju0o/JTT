"""Enumerate local tmux panes into plain-dict "observations".

This module only *reads* tmux state (list-panes, capture-pane) and the
process table. It never renames, kills, or sends input to anything.
"""

from __future__ import annotations

import shlex
from pathlib import Path
from typing import Dict, List

from . import capture
from ..detection import process as process_detection
from ..detection.identity import agent_process_cwd, evidence_cmdline
from ..detection.sshdest import find_ssh_client
from ..detection.project import discover_project, git_project_name

FIELD_SEP = "\x1f"

_PANE_FORMAT = FIELD_SEP.join(
    [
        "#{session_name}",
        "#{window_id}",
        "#{window_index}",
        "#{window_name}",
        "#{pane_index}",
        "#{pane_id}",
        "#{pane_title}",
        "#{pane_current_command}",
        "#{pane_current_path}",
        "#{pane_pid}",
        "#{pane_dead}",
        "#{pane_active}",
    ]
)

_EXPECTED_FIELDS = _PANE_FORMAT.count(FIELD_SEP) + 1
_WINDOW_FORMAT = FIELD_SEP.join(
    ["#{session_name}", "#{session_id}", "#{window_id}", "#{window_index}",
     "#{window_name}", "#{window_created}"]
)


def list_windows(session: str, *, all_sessions: bool = False) -> List[Dict]:
    """Read window assets in one session, or server-wide for an isolated sidecar."""
    if all_sessions:
        args = ["list-windows", "-a", "-F", _WINDOW_FORMAT]
    else:
        args = ["list-windows", "-t", session, "-F", _WINDOW_FORMAT] if session else []
    output = capture.run_tmux(args) if args else ""
    rows = []
    for line in (output or "").splitlines():
        parts = line.replace(r"\037", FIELD_SEP).split(FIELD_SEP)
        if len(parts) != 6 or not parts[2].startswith("@"):
            continue
        session_name, session_id, window_id, index, name, created = parts
        rows.append({"session": session_name, "session_id": session_id, "window_id": window_id,
                     "window_index": index, "window_name": name, "window_created": created})
    return rows


def window_for_pane(pane_id: str) -> str:
    """Read the owning window id for a pane, or return empty if it vanished."""
    if not pane_id:
        return ""
    return capture.run_tmux(["display-message", "-p", "-t", pane_id, "#{window_id}"]).strip()


def _has_tower_process(pane_pid: str, cmdline_map: Dict[str, str], ppid_map: Dict[str, str]) -> bool:
    children: Dict[str, List[str]] = {}
    for pid, parent in ppid_map.items():
        children.setdefault(parent, []).append(pid)

    stack = [str(pane_pid)]
    seen = set()
    while stack:
        pid = stack.pop()
        if pid in seen:
            continue
        seen.add(pid)
        args = cmdline_map.get(pid, "")
        try:
            parts = shlex.split(args)
        except ValueError:
            parts = args.split()
        if parts:
            executable = Path(parts[0]).name.casefold()
            script = Path(parts[1]).name.casefold() if len(parts) > 1 else ""
            if executable in {"tower", "tmux-agent-tower"} or script in {"tower", "tmux-agent-tower"}:
                return True
            if "-m" in parts:
                module_index = parts.index("-m") + 1
                if module_index < len(parts) and parts[module_index].casefold() == "tmux_agent_tower.main":
                    return True
        stack.extend(children.get(pid, []))
    return False


def list_panes(
    session: str, exclude_pane_id: str = "", capture_lines: int = 30, *, all_sessions: bool = False,
) -> List[Dict]:
    """Return one dict per pane, excluding Tower's own pane (by pane_id).

    Deliberately NOT a window-name match: a window's name is just text
    anyone can reuse or rename, with no real connection to what's running
    in it (a real bug: a window that used to host Tower got reused for an
    unrelated session, kept its old name, and got wrongly excluded/matched
    by name). ``exclude_pane_id`` is the one thing that's actually stable.

    Malformed lines (wrong field count -- e.g. a tmux version with a
    slightly different format quirk) are skipped rather than raising, so one
    bad line can't take down the whole refresh.
    """

    if all_sessions:
        args = ["list-panes", "-a", "-F", _PANE_FORMAT]
    else:
        args = ["list-panes", "-s", "-t", session, "-F", _PANE_FORMAT]
    output = capture.run_tmux(args)

    if not output:
        return []

    cmdline_map = process_detection.cmdline_by_pid()
    ppid_map = process_detection.ppid_by_pid()

    rows: List[Dict] = []

    for line in output.split("\n"):
        if not line:
            continue

        # tmux 3.4 escapes control characters in format output as octal text;
        # tmux 3.6 emits the unit separator byte directly.
        parts = line.replace(r"\037", FIELD_SEP).split(FIELD_SEP)
        if len(parts) != _EXPECTED_FIELDS:
            continue

        (
            session_name,
            window_id,
            window_index,
            window_name,
            pane_index,
            pane_id,
            title,
            command,
            current_path,
            pane_pid,
            dead,
            pane_active,
        ) = parts

        if exclude_pane_id and pane_id == exclude_pane_id:
            continue

        lines = capture.capture_pane(pane_id, lines=capture_lines) if dead != "1" else []
        agent_cwd = "" if dead == "1" else (agent_process_cwd(pane_pid, cmdline_map, ppid_map) or "")
        ssh_target, ssh_stale = ("", False) if dead == "1" else find_ssh_client(pane_pid, cmdline_map, ppid_map)

        rows.append(
            {
                "session": session_name,
                "window_id": window_id,
                "window_index": window_index,
                "window_name": window_name,
                "pane_index": pane_index,
                "pane_id": pane_id,
                "pane_pid": pane_pid,
                "title": title or "(unnamed)",
                "command": command,
                "cmdline": evidence_cmdline(pane_pid, command, cmdline_map, ppid_map),
                "tower_runtime": _has_tower_process(pane_pid, cmdline_map, ppid_map),
                "agent_cwd": agent_cwd,
                "ssh_target": ssh_target,
                "ssh_stale": ssh_stale,
                "process_git": git_project_name(agent_cwd) if agent_cwd else None,
                "path": current_path,
                "auto_project": discover_project(current_path),
                "git_project": git_project_name(current_path),
                "path_basename": Path(current_path).name or current_path if current_path else "",
                "dead": dead == "1",
                "pane_active": pane_active == "1",
                "lines": lines,
            }
        )

    return rows
