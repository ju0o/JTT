"""Who is running in a pane, and which project that cwd belongs to.

Agent names come from the process tree first. A pane title is a label
someone typed, so it loses to an executable and it loses to a shell that
is actually what is running. A parent terminal does not get to name the
agent its child is running.

Project names come from a git repo when there is one. A title that is
just an agent or tool name is not a project.
"""

from __future__ import annotations

import os
import re
import shlex
from pathlib import Path
from typing import Callable, Dict, Optional, Sequence, Tuple

_SHELLS = {"bash", "zsh", "fish", "sh", "ssh", "dash"}

# argv0 (or a path that *is* the program) → display name.
_BY_BASENAME = {
    "claude": "Claude",
    "codex": "Codex",
    "opencode": "OpenCode",
    "grok": "Grok",
    "cursor-agent": "Cursor",
    "cline": "Cline",
}

# Strong, unique screen markers. Used only when the process table has
# nothing to say. A shell we already identified is never relabeled by these.
_UI_MARKERS = (
    ("Cursor", ("add a follow-up",)),
    ("Claude", ("? for shortcuts", "← for agents", "shift+tab to cycle")),
    ("Codex", ("ask codex",)),
    ("OpenCode", ("esc interrupt",)),
)


def argv0(args: str) -> str:
    """Basename of the program, with a leading login dash stripped."""

    text = (args or "").strip()
    if not text:
        return ""
    try:
        parts = shlex.split(text)
    except ValueError:
        parts = text.split()
    if not parts:
        return ""
    return Path(parts[0]).name.lower().lstrip("-")


def agent_name_from_args(args: str) -> Optional[str]:
    """The agent this command line *is*, or None.

    Matching is on the executable, not on words inside a shell script.
    ``bash -c ...claude...`` stays a shell. Cursor's CLI is a binary
    named ``agent`` whose arguments point at ``cursor-agent/.../index.js``.
    """

    base = argv0(args)
    if not base or base in _SHELLS:
        return None
    if base in _BY_BASENAME:
        return _BY_BASENAME[base]
    low = (args or "").lower()
    # The program path itself contains the marker. A later argument that
    # merely mentions it does not count unless argv0 is the known wrapper.
    try:
        parts = shlex.split(args or "")
    except ValueError:
        parts = (args or "").split()
    program = (parts[0] if parts else "").lower()
    if "cursor-agent" in program:
        return "Cursor"
    if base in {"agent", "node", "nodejs"} and "cursor-agent" in low:
        return "Cursor"
    return None


def _deepest_agent(
    pane_pid: str,
    cmdline_map: Dict[str, str],
    ppid_map: Dict[str, str],
) -> Tuple[str, str]:
    """``(pid, args)`` of the deepest agent under the pane.

    A tool shell under that agent is not an agent, so it does not win.
    ``("", "")`` when the tree has no agent executable.
    """

    children: Dict[str, list] = {}
    for pid, parent in ppid_map.items():
        children.setdefault(parent, []).append(pid)

    best_depth = -1
    best: Tuple[str, str] = ("", "")
    stack = [(str(pane_pid), 0)]
    seen = set()
    while stack:
        pid, depth = stack.pop()
        if pid in seen:
            continue
        seen.add(pid)
        args = cmdline_map.get(pid, "")
        if agent_name_from_args(args) and depth >= best_depth:
            best_depth = depth
            best = (pid, args)
        for child in children.get(pid, []):
            stack.append((child, depth + 1))
    return best


def evidence_cmdline(
    pane_pid: str,
    command: str,
    cmdline_map: Dict[str, str],
    ppid_map: Dict[str, str],
) -> str:
    """Command line of the deepest agent under this pane.

    Shell children of an agent (tool calls) do not replace it. When no
    agent is in the tree, the pane's own command line is returned so a
    real shell can beat a misleading title.
    """

    _pid, args = _deepest_agent(pane_pid, cmdline_map, ppid_map)
    if args:
        return args
    own = cmdline_map.get(str(pane_pid), "")
    if own:
        return own
    return (command or "").strip()


def read_process_cwd(pid: str) -> Optional[str]:
    """``/proc/<pid>/cwd``. None when the process is gone or not readable."""

    if not pid or not str(pid).isdigit():
        return None
    try:
        return os.readlink(f"/proc/{pid}/cwd")
    except OSError:
        return None


def agent_process_cwd(
    pane_pid: str,
    cmdline_map: Dict[str, str],
    ppid_map: Dict[str, str],
    read_cwd: Callable[[str], Optional[str]] = read_process_cwd,
) -> Optional[str]:
    """Cwd of the active agent process, not of a tool shell it spawned.

    tmux ``pane_current_path`` stays ``/mnt/f`` when the agent never
    changed the pane directory. The agent's own cwd can still be the repo.
    A child that merely ``cd``'d somewhere is not used: that is a tool,
    not the project binding.
    """

    pid, _args = _deepest_agent(pane_pid, cmdline_map, ppid_map)
    if not pid:
        return None
    cwd = read_cwd(pid)
    return cwd or None


def _title_agent(title: str) -> Optional[str]:
    low = (title or "").strip().lower()
    if not low:
        return None
    # Drop a leading spinner glyph Claude and others put on the title.
    while low and low[0] in "✳✻✽∗*· ":
        low = low[1:].lstrip()
    for base, name in _BY_BASENAME.items():
        if low == base or low.startswith(base + " "):
            return name
    if low == "cursor" or low.startswith("cursor "):
        return "Cursor"
    return None


def title_is_agent_label(title: Optional[str]) -> bool:
    """True when the title names an agent or tool, not a project."""

    return _title_agent(title or "") is not None


def _ui_agent(lines: Sequence[str]) -> Optional[str]:
    """Name the agent from the current widget, not from old scrollback.

    The last lines are the live UI. A finished Claude turn can use a
    localized footer, so the prompt glyph and the done line count too.
    Codex's own prompt wins when its footer also contains Claude's
    shortcut words.
    """

    tail = list(lines or ())[-24:]
    text = "\n".join(tail)
    if not text.strip():
        return None
    lowered = text.lower()
    by_name = {name: markers for name, markers in _UI_MARKERS}

    def hit(name: str) -> bool:
        return any(marker in lowered for marker in by_name.get(name, ()))

    # "Ask Codex" is the Codex prompt. A bare "Worked for" is also the
    # verb Claude uses for a finished turn, so it must not win over
    # Claude's own footer.
    if hit("Codex"):
        return "Codex"
    if hit("Cursor"):
        return "Cursor"
    if hit("OpenCode") or "ctrl+p commands" in lowered:
        return "OpenCode"
    if hit("Claude"):
        return "Claude"
    # ❯ plus ⏵ also appears in Tower's own list. A finished verb
    # ("Brewed for 5s", "Sautéed for 1m") is the Claude signal.
    # The verb can include an accent, and a narrow pane can clip the
    # "shift+tab" footer down to "auto mode on".
    if re.search(r"^\s*❯", text, re.MULTILINE) and (
        re.search(r"(?:^|\s)[a-zà-ÿ]+ed for \d+[smh]", lowered)
        or "auto mode on" in lowered
    ):
        return "Claude"
    if "worked for" in lowered:
        return "Codex"
    return None


def _is_shell_command(command: str) -> bool:
    return (command or "").strip().lower().lstrip("-") in _SHELLS


def identify_agent(
    command: str,
    title: str,
    cmdline: str = "",
    lines: Sequence[str] = (),
) -> Tuple[str, str]:
    """``(agent name, source)``.

    Source is ``process``, ``ui``, ``title``, or ``shell``. An explicit
    user override is applied by the caller and reported as ``override``.
    """

    from_args = agent_name_from_args(cmdline)
    if from_args:
        return from_args, "process"

    from_command = _BY_BASENAME.get((command or "").strip().lower())
    if from_command:
        return from_command, "process"

    if _is_shell_command(command) or _is_shell_command(argv0(cmdline)):
        return "Shell", "process"

    # A real program is not renamed from text on its screen. The Tower
    # process is python3 and can display another agent's words. Screen
    # identity is only for an empty command, or for a shell via
    # agent_through_ssh.
    if (command or "").strip() or (cmdline or "").strip():
        return "Shell", "shell"

    from_ui = _ui_agent(lines)
    if from_ui:
        return from_ui, "ui"

    from_title = _title_agent(title)
    if from_title:
        return from_title, "title"

    return "Shell", "shell"


def prefer_override(auto_value: str, auto_source: str, override: Optional[str]) -> Tuple[str, str]:
    """A saved edit wins. Reset is the only way back to ``auto_source``."""

    if override:
        return override, "override"
    return auto_value, auto_source
