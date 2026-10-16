"""Pure, curses-free rendering/formatting decisions for the Tower TUI.

Kept separate from ``ui/tower.py`` so what-to-show-and-when logic (project
name priority, when a pane title is worth a second line, responsive
thresholds, search filtering) can be unit tested without a real curses
screen. Nothing here imports ``curses``.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from ..detection.attention import card_rank
from ..i18n import t

from ..detection.identity import title_is_agent_label
from ..state.overrides import ROLE_IDS
from ..detection.project import is_low_confidence_name


def display_width(text: str) -> int:
    """Terminal column width of ``text``, counting East Asian Wide/Fullwidth
    characters (Korean, CJK, many box-drawing/symbol glyphs) as 2 columns.
    Plain ``len()`` undercounts these, which silently broke right-alignment
    and truncation math throughout this Korean-first UI -- a real bug
    caught live (host summary text clipped, detail-panel labels
    misaligned) once rows started mixing Korean and English text on the
    same line.
    """

    width = 0
    for ch in text:
        width += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return width


def truncate_to_width(text: str, max_width: int) -> str:
    """Truncates ``text`` to at most ``max_width`` terminal columns,
    appending an ellipsis if anything was cut -- width-aware, so it never
    cuts a wide character in half or undercounts them (see
    ``display_width``).
    """

    if max_width <= 0:
        return ""

    if display_width(text) <= max_width:
        return text

    budget = max_width - 1  # room for the ellipsis
    out = []
    used = 0
    for ch in text:
        w = 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
        if used + w > budget:
            break
        out.append(ch)
        used += w

    return "".join(out) + "…"


def wrap_items(items: Sequence[str], max_width: int, separator: str = "   ") -> List[str]:
    """Pack help labels into visible terminal lines without cutting a key hint."""

    lines: List[str] = []
    current = ""
    for item in items:
        candidate = f"{current}{separator if current else ''}{item}"
        if current and display_width(candidate) > max_width:
            lines.append(current)
            current = item
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines

STATUS_SYMBOL = {
    "WORKING": "●",
    "WAITING": "!",
    "IDLE": "○",
    "UNKNOWN": "◇",
    "DEAD": "×",
}

STATUS_ORDER = ["WORKING", "WAITING", "IDLE", "UNKNOWN", "DEAD"]

# Below this width, agent + status move to their own line under the
# project name instead of sharing the first line.
NARROW_WIDTH_THRESHOLD = 70

# Below this height, the selected-item detail panel is hidden entirely so
# the pane list itself keeps as much room as possible.
SHORT_HEIGHT_THRESHOLD = 18
HOME_SIDE_PANEL_MIN_WIDTH = 110
HOME_SIDE_PANEL_MIN_HEIGHT = 30


def use_narrow_layout(width: int) -> bool:
    return width < NARROW_WIDTH_THRESHOLD


def should_show_detail_panel(height: int) -> bool:
    return height >= SHORT_HEIGHT_THRESHOLD


def use_home_side_panel(width: int, height: int) -> bool:
    """Use a selected-task side panel only when it cannot crowd the work list."""

    return width >= HOME_SIDE_PANEL_MIN_WIDTH and height >= HOME_SIDE_PANEL_MIN_HEIGHT


def home_list_width(width: int) -> int:
    """Keep the task list dominant while reserving room for a readable summary."""

    return max(1, min(width - 44, (width * 3) // 5))


def primary_badge(row: Dict) -> Tuple[str, str]:
    """The one mark a list row leads with.

    Returns ``(symbol, i18n key)``. The key is never an execution enum,
    so the UI cannot print ``WORKING`` at the user. Priority matches
    ``card_rank``: approval, input, a new result, an error, then execution.
    """

    if row.get("offline"):
        return "×", "state.offline"
    attention = row.get("attention") or "none"
    if attention == "approval_required":
        return "!", "state.approval"
    if attention == "input_required" or row.get("status") == "WAITING":
        interaction = row.get("interaction") or {}
        if interaction.get("type") == "choice" or (
            interaction.get("type") == "confirm" and interaction.get("options")
        ):
            return "?", "state.choice"
        return "?", "state.answer"
    if row.get("result_state") == "ready":
        return "✓", "state.result"
    if attention == "error":
        return "!", "state.error"
    return execution_badge_for_row(row)


def execution_badge(
    status: str,
    *,
    agent_process_alive: bool = False,
    agent_process_ended: bool = False,
    agent_status_unverified: bool = False,
) -> Tuple[str, str]:
    """Execution only, for the detail line next to attention and result."""

    if agent_process_ended:
        return "×", "state.dead"
    if status in {"IDLE", "WAITING"}:
        if agent_process_alive:
            return "○", "state.input_waiting"
        if agent_status_unverified:
            return "◇", "state.unknown"
    if status == "UNKNOWN" and agent_process_alive:
        return "◇", "state.running_unknown"
    return _execution_badge(status)


def execution_badge_for_row(row: Dict) -> Tuple[str, str]:
    """Render execution status without treating a live pane as a live Agent."""

    return execution_badge(
        str(row.get("status") or ""),
        agent_process_alive=bool(row.get("agent_process_alive")),
        agent_process_ended=bool(row.get("agent_process_ended")),
        agent_status_unverified=bool(row.get("agent_status_unverified")),
    )


def _execution_badge(status: str) -> Tuple[str, str]:
    return {
        "WORKING": ("●", "state.working"),
        "IDLE": ("○", "state.idle"),
        "DEAD": ("×", "state.dead"),
        "UNKNOWN": ("◇", "state.unknown"),
        "WAITING": ("○", "state.idle"),
    }.get(status or "", ("◇", "state.unknown"))


def detail_badges(row: Dict) -> List[Tuple[str, str]]:
    """Every mark worth showing once a row is selected. A list row uses
    ``primary_badge`` alone so the scan stays one glance.
    """

    if row.get("offline"):
        return [("×", "state.offline")]
    badges: List[Tuple[str, str]] = []
    attention = row.get("attention") or "none"
    if attention == "approval_required":
        badges.append(("!", "state.approval"))
    if attention == "input_required" or row.get("status") == "WAITING":
        interaction = row.get("interaction") or {}
        if interaction.get("type") == "choice" or (
            interaction.get("type") == "confirm" and interaction.get("options")
        ):
            badges.append(("?", "state.choice"))
        else:
            badges.append(("?", "state.answer"))
    if row.get("result_state") == "ready":
        badges.append(("✓", "state.result"))
    if attention == "error":
        badges.append(("!", "state.error"))
    badges.append(execution_badge_for_row(row))
    return badges


def watch_counts(rows: Sequence[Dict]) -> Dict[str, int]:
    """Header counters. A pane counts once, as the reason to look at it."""

    counts = {"approval": 0, "choice": 0, "answer": 0, "result": 0, "working": 0}
    for row in rows:
        if row.get("placeholder") or row.get("offline") or row.get("kind") in {
            "window", "work_group", "folder", "other_section", "window_asset",
        }:
            continue
        attention = row.get("attention") or "none"
        if attention == "approval_required":
            counts["approval"] += 1
        elif attention == "input_required" or row.get("status") == "WAITING":
            interaction = row.get("interaction") or {}
            category = "choice" if interaction.get("type") == "choice" or (
                interaction.get("type") == "confirm" and interaction.get("options")
            ) else "answer"
            counts[category] += 1
        elif row.get("result_state") == "ready":
            counts["result"] += 1
        elif row.get("status") == "WORKING" and not row.get("agent_process_ended"):
            counts["working"] += 1
    return counts


WATCH_HEADER = (
    ("approval", "!", "header.approval"),
    ("choice", "?", "header.choice"),
    ("answer", "?", "header.answer"),
    ("result", "✓", "header.result"),
    ("working", "●", "header.working"),
)


def format_watch_header(counts: Dict[str, int], label: Callable[[str], str]) -> str:
    """``! 승인 2   ? 입력 1``. A zero count is omitted."""

    parts = [
        f"{symbol} {label(key)} {counts.get(name, 0)}"
        for name, symbol, key in WATCH_HEADER
        if counts.get(name, 0) > 0
    ]
    return "   ".join(parts)


def control_actions(row: Dict) -> List[Tuple[str, bool]]:
    """Keys the control view offers, and whether each one can do something.

    Navigation keys are not in this list. Opening the view is not a write.
    """

    remote = bool(row.get("remote"))
    local_pane = (not remote) and str(row.get("key") or "").startswith("%")
    actions = [("P", True)]
    interaction = row.get("interaction") or {}
    choosable = [item for item in (interaction.get("options") or []) if item.get("safe") and item.get("key")]
    if choosable or interaction.get("input_allowed") or row.get("approval_known"):
        actions.append(("A", True))
    if row.get("reject_known"):
        actions.append(("N", True))
    actions.extend(
        [
            ("Y", True),
            ("S", True),
            ("E", True),
            ("G", local_pane),
            ("X", local_pane),
            ("Esc", True),
        ]
    )
    return actions


def format_host_summary(counts: Dict[str, int], status_label: Callable[[str], str]) -> str:
    """``status_label(status) -> translated word``. A status with a zero
    count is omitted entirely -- a host with nothing waiting shouldn't
    have to say so.
    """

    parts = [
        f"{STATUS_SYMBOL[s]} {status_label(s)} {counts.get(s, 0)}"
        for s in STATUS_ORDER
        if counts.get(s, 0) > 0
    ]
    return "   ".join(parts)


def looks_meaningful_title(title: Optional[str], local_host: str) -> bool:
    """False for an empty title, the placeholder "(unnamed)", or a title
    that's just the host's own name (common for a freshly-opened shell
    pane, e.g. an SSH session titled after the remote hostname) -- none of
    those say anything about what the pane is actually for.
    """

    if not title:
        return False

    stripped = title.strip()

    if not stripped or stripped.casefold() in {"(unnamed)", "(no name)", "(이름 없음)"}:
        return False

    if stripped.casefold() in {"bash", "zsh", "sh", "fish", "shell", "terminal", "powershell", "cmd"}:
        return False

    if stripped.isdecimal():
        return False

    if local_host and stripped.lower() == local_host.strip().lower():
        return False

    if stripped.startswith(("%", "@")) or stripped.lower().startswith("session "):
        return False

    return True


_TITLE_ROLES = {
    "pm": "orchestrator", "orchestrator": "orchestrator", "조율": "orchestrator",
    "planner": "planner", "계획": "planner",
    "builder": "builder", "구현": "builder",
    "reviewer": "reviewer", "검수": "reviewer",
    "qa": "qa", "확인": "qa",
    "dogfood": "dogfood", "실사용": "dogfood",
    "e2e": "e2e", "전체 흐름": "e2e",
}


def title_identity(title: Optional[str], local_host: str) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """Infer ``(project, task, role)`` from a human title without saving it.

    A right-hand project slug after ``|`` is a useful legacy signal. User
    handles and agent labels are not treated as projects. Unknown titles stay
    task names, so existing work never degrades to an unnamed terminal.
    """

    if not looks_meaningful_title(title, local_host) or title_is_agent_label(title):
        return None, None, None

    value = title.strip()
    while value and value[0] in "✳✻✽∗*· ":
        value = value[1:].lstrip()
    if " | " not in value:
        return None, value, None

    left, right = (part.strip() for part in value.rsplit(" | ", 1))
    if not left or not right:
        return None, value, None

    role = _TITLE_ROLES.get(right.casefold())
    if role:
        project_like = "-" in left or any(a.islower() and b.isupper() for a, b in zip(left, left[1:]))
        return (left, None, role) if project_like else (None, left, role)

    # Compact leading labels such as "OC | SampleProject ..." are commonly an
    # agent abbreviation, followed by the actual project/task title.
    if len(left) <= 3 and left.replace(".", "").isupper():
        words = right.split(maxsplit=1)
        first = words[0]
        project_like = "-" in first or any(a.islower() and b.isupper() for a, b in zip(first, first[1:]))
        if project_like:
            return first, words[1] if len(words) > 1 else None, None
        return None, right, None

    if title_is_agent_label(right):
        return None, left, None

    project_like = "-" in right or any(a.islower() and b.isupper() for a, b in zip(right, right[1:]))
    if project_like:
        return right, left, None

    # A lower-case account-like suffix is decoration, not project identity.
    if re.fullmatch(r"[a-z][a-z0-9_]{2,}", right):
        return None, left, None

    return None, value, None


def resolve_project_identity(
    custom: Optional[str],
    git_name: Optional[str],
    title: Optional[str],
    basename: Optional[str],
    local_host: str,
    no_name_label: str,
    binding_name: Optional[str] = None,
    process_git_name: Optional[str] = None,
) -> tuple:
    """``(project name, source)``.

    Source is ``override``, ``binding``, ``process``, ``git``, ``title``,
    ``path``, or ``none``. A title that only names an agent is not a
    project. ``process`` is the git repo of the agent process cwd.
    ``git`` is the tmux pane cwd. Neither is invented from a title.
    """

    if custom:
        return custom, "override"

    if binding_name and not is_low_confidence_name(binding_name):
        return binding_name, "binding"

    if process_git_name and not is_low_confidence_name(process_git_name):
        return process_git_name, "process"

    if git_name and not is_low_confidence_name(git_name):
        return git_name, "git"

    if looks_meaningful_title(title, local_host) and not title_is_agent_label(title):
        return title.strip(), "title"

    if basename and not is_low_confidence_name(basename):
        return basename, "path"

    return no_name_label, "none"


def resolve_display_project(
    custom: Optional[str],
    git_name: Optional[str],
    title: Optional[str],
    basename: Optional[str],
    local_host: str,
    no_name_label: str,
) -> str:
    """The project name shown as a row's primary text.

    Priority: an explicit user override > a launcher binding > the agent
    process cwd's git repo > the tmux pane cwd's git repo > a meaningful
    pane title that is not an agent label > the raw directory basename >
    an honest "no name" placeholder. A single-letter or generic
    mount-point-ish basename (see ``detection.project.is_low_confidence_name``)
    is never shown as-is if something better is available.
    """

    name, _source = resolve_project_identity(
        custom, git_name, title, basename, local_host, no_name_label
    )
    return name


def suggest_task_name(
    project: Optional[str],
    role: Optional[str],
    agent: Optional[str],
    title: Optional[str],
    local_host: str,
    no_name_label: str,
    role_label: Callable[[str], str],
    terminal_label: str,
    fallback_label: str,
) -> str:
    """Build the one user-facing name without using tmux identifiers."""

    has_project = bool(project and project.strip() and project != no_name_label)
    base = project.strip() if has_project else ""
    if not base and looks_meaningful_title(title, local_host) and not title_is_agent_label(title):
        candidate = title.strip()
        if not candidate.startswith(("%", "@")) and not candidate.lower().startswith("session "):
            base = candidate

    role_name = role_label(role) if role in ROLE_IDS else ""
    is_terminal = bool(agent and agent.strip().lower() in {"shell", "terminal"})
    if not base:
        base = (agent or "").strip() if agent and not is_terminal else (terminal_label if is_terminal else role_name)
    qualifier = role_name or ((terminal_label if is_terminal else agent) if has_project else "")
    if qualifier and qualifier.casefold() != base.casefold():
        return f"{base} · {qualifier}"
    return base or fallback_label


def format_identity_sense(
    agent_source: Optional[str],
    project_source: Optional[str],
    auto_agent_source: Optional[str] = None,
    auto_project_source: Optional[str] = None,
) -> str:
    """``override→process / git`` so a stuck edit still shows what
    detection would have said.
    """

    def part(shown: Optional[str], auto: Optional[str]) -> str:
        label = shown or "-"
        if label == "override" and auto and auto != "override":
            return f"override→{auto}"
        return label

    return f"{part(agent_source, auto_agent_source)} / {part(project_source, auto_project_source)}"


def title_secondary_line(project: str, title: Optional[str], local_host: str) -> Optional[str]:
    """The pane title shown as a secondary line under the project name --
    only when it's meaningful AND tells the user something the project
    name doesn't already say (never a duplicate line).
    """

    if not looks_meaningful_title(title, local_host):
        return None

    stripped = title.strip()

    if stripped.lower() == (project or "").lower():
        return None

    return stripped


def row_matches_filter(row: Dict, filter_text: str) -> bool:
    """Case-insensitive substring match across the fields a user would
    actually recognise a pane by. An empty filter matches everything.
    """

    if not filter_text:
        return True

    needle = filter_text.strip().lower()
    if not needle:
        return True

    role = row.get("role") or ""
    haystacks = (
        row.get("display_name") or "",
        row.get("task_name") or "",
        row.get("project") or "",
        row.get("folder_name") or "",
        row.get("window_display_name") or "",
        row.get("agent") or "",
        t(f"role.{role}") if role else "",
        row.get("work_group_name") or "",
    )

    return any(needle in h.lower() for h in haystacks)


# WAITING and UNKNOWN need a decision from the user; DEAD is worth
# noticing; WORKING is already progressing fine; IDLE is the least
# interesting. Lower number = more attention-worthy.
ATTENTION_PRIORITY = {"WAITING": 0, "UNKNOWN": 1, "DEAD": 2, "WORKING": 3, "IDLE": 4}


def format_duration(seconds: float) -> str:
    """"Tower has continuously observed this status for ~N" -- never framed
    as "the agent started this N ago" (Tower cannot know that; see
    ``StatusEngine.duration_seconds``'s docstring). Coarse on purpose: a
    control tower doesn't need seconds-level precision once something's
    been running for minutes or hours.
    """

    seconds = max(0, int(seconds))

    if seconds < 60:
        return f"{seconds}초"

    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}분"

    hours = minutes // 60
    remaining_minutes = minutes % 60
    if remaining_minutes:
        return f"{hours}시간 {remaining_minutes}분"
    return f"{hours}시간"


def sort_by_attention(rows: List[Dict]) -> List[Dict]:
    """Attention view and the phone list share ``card_rank``.

    A stable sort, so equal ranks keep their existing order. The default
    list does not use this.
    """

    return sorted(rows, key=card_rank)


_SUMMARY_ORDER = ("!", "?", "✓", "●", "◇")


def window_summary(panes: Sequence[Dict]) -> str:
    """Non-zero marks inside one window. Idle is omitted so a quiet
    window stays a name. Each pane contributes its one primary badge.
    """

    counts = {symbol: 0 for symbol in _SUMMARY_ORDER}
    for pane in panes:
        if not pane.get("pane_id"):
            continue
        symbol, _key = primary_badge(pane)
        if symbol in counts:
            counts[symbol] += 1
    return " ".join(f"{symbol}{counts[symbol]}" for symbol in _SUMMARY_ORDER if counts[symbol])


def list_row_parts(row: Dict, width: int, badge_text: Callable[[str], str], duration: str = "") -> Dict[str, str]:
    """What one tree row is allowed to show, using one task identity."""

    narrow = use_narrow_layout(width)
    guide = row.get("guide") or ""
    if row.get("kind") == "work_group":
        return {
            "guide": guide,
            "project": row.get("display_name") or "",
            "task": badge_text("group.label") + (" · " + row["project"] if row.get("project") else ""),
            "agent": "",
            "badge": "" if narrow else (row.get("summary_compact") or ""),
        }
    if row.get("kind") in {"window", "window_asset"}:
        return {
            "guide": guide,
            "project": row.get("display_name") or row.get("project") or "",
            "task": "",
            "agent": "",
            "badge": row.get("summary_compact") if narrow else (row.get("summary") or ""),
        }
    if row.get("kind") in {"folder", "other_section"}:
        return {
            "guide": guide,
            "project": row.get("display_name") or "",
            "task": "",
            "agent": "",
            "badge": row.get("summary_compact") if narrow else (row.get("summary") or ""),
        }
    if row.get("kind") == "zero":
        return {"guide": "", "project": row.get("project") or "", "task": "", "agent": "", "badge": ""}
    symbol, key = primary_badge(row)
    badge = f"{symbol} {badge_text(key)}"
    if duration and not narrow:
        badge = f"{badge} · {duration}"
    project_name = row.get("project") or ""
    if project_name == t("project.no_name"):
        project_name = ""
    project = row.get("display_name") or row.get("task_name") or project_name or t("task.terminal")
    role = badge_text(f'role.{row["role"]}') if row.get("role") in ROLE_IDS else ""
    agent = (row.get("agent") or "").strip()
    if role and role.casefold() not in project.casefold():
        agent = f"{role} · {agent}" if agent else role
    return {
        "guide": guide,
        "project": project,
        # The primary heading is the only task name in the simple list.
        # Project and work-group metadata remain on the row for filters and
        # detail/advanced views, but must not compete with a pane title.
        "task": "",
        "agent": truncate_to_width(agent, 24) if not narrow else "",
        "badge": badge if not narrow else "",
    }


def row_line_count(row: Dict, narrow: bool) -> int:
    """Physical lines for one pane in the main list.

    Narrow terminals reserve a second line for agent/status. Wide terminals
    add current activity underneath when there is room.
    Pane title, path, and pane id stay in selected details.
    """

    if narrow:
        return 2
    if not row.get("activity_text"):
        return 1
    return 2


def agent_status_line(
    agent: str,
    status_label: str,
    duration_text: str = "",
    role: str = "",
    host: str = "",
    max_width: Optional[int] = None,
) -> str:
    """Secondary row context: Agent/location identity followed by status."""

    identity = " · ".join(part for part in (agent or "-", role, host) if part)
    state = status_label
    if duration_text:
        state += f" · {duration_text}"
    if max_width is not None:
        identity_budget = max(0, max_width - display_width("  " + state))
        identity = truncate_to_width(identity, identity_budget)
    return f"{identity}  {state}" if identity else state


def format_detail_panel(fields: Sequence[Tuple[str, str]]) -> List[str]:
    """``fields`` is ``[(translated_label, value), ...]``; returns
    label-aligned display lines, e.g. ``"프로젝트    JuHome"``.

    Aligned by *display* width (see ``display_width``), not character
    count -- labels here routinely mix Korean ("프로젝트") and English
    ("Host"), which have different character-count-to-column ratios.
    """

    fields = [(label, value) for label, value in fields if value is not None]
    if not fields:
        return []

    label_width = max(display_width(label) for label, _ in fields)
    lines = []
    for label, value in fields:
        pad = " " * max(0, label_width - display_width(label))
        lines.append(f"{label}{pad}  {value}")
    return lines
