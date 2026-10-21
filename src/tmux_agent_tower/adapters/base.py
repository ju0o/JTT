"""Base adapter contract and shared, generic detection helpers.

Adapters give *opinions*, not final answers. Each adapter looks at a pane's
title/command/captured content and returns a status hint (or ``None`` when it
has no opinion). The status engine (see ``detection/status.py``) combines
that hint with generic signals such as output-hash change detection and
applies hysteresis. This keeps agent-specific pattern matching isolated so a
single wrong regex cannot silently corrupt every other agent's status.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Optional, Sequence, Tuple

# Braille spinner frames used by several Node/Ink-based CLIs (Codex, and
# others built on similar spinner libraries).
BRAILLE_SPINNER_CHARS = set("⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏")

# Spinner glyphs observed in Claude Code's own status line.
CLAUDE_SPINNER_CHARS = set("✳✻✽∗*·")

GENERIC_WAIT_PATTERNS = [
    # Deliberately NOT a bare r"\bapprove\b": several agents show a static
    # mode label like "always-approve" in their idle UI chrome, which is
    # not a question. Only an actual "approve?"-shaped prompt counts.
    r"\bapprove\s*\?",
    r"\bapproval\b",
    r"permission required",
    r"allow this",
    r"\ballow\b.*\?",
    r"\bconfirm\b",
    r"continue\?",
    r"press enter",
    r"enter to continue",
    r"\by\s*/\s*n\b",
    r"\byes\s*/\s*no\b",
    r"do you want",
    r"would you like",
    r"trust this (folder|directory|project|repo)",
    r"waiting for input",
    r"choose an option",
    r"select an option",
    r"^\s*\d+\.\s+(yes|no)\b",
]

_GENERIC_WAIT_RE = re.compile("|".join(GENERIC_WAIT_PATTERNS), re.IGNORECASE | re.MULTILINE)

SHELL_PROMPT_RE = re.compile(r"[\$#%>]\s*$")


@dataclass(frozen=True)
class PaneContext:
    """Everything an adapter needs to classify a single pane observation."""

    title: str
    command: str
    lines: Sequence[str] = field(default_factory=tuple)
    pane_pid: str = ""
    turn_watermark: Optional[Tuple[str, int]] = None

    def tail(self, n: int = 15) -> str:
        return "\n".join(self.lines[-n:])

    def last_nonblank(self) -> str:
        for line in reversed(self.lines):
            if line.strip():
                return line.strip()
        return ""


@dataclass(frozen=True)
class AdapterResult:
    """An adapter's opinion. ``status`` is ``None`` when it has no opinion."""

    status: Optional[str] = None
    reason: str = ""


_NO_OPINION = AdapterResult(status=None)


@dataclass(frozen=True)
class ResultCandidate:
    """An adapter-extracted final body with independent turn/body evidence.

    ``confidence`` remains for compatibility; ``complete`` gates result
    display/copy and is derived from ``turn_complete`` and ``body_complete``.
    ``fingerprint`` identifies the text so the same answer is not announced
    again.
    """

    text: str
    fingerprint: str
    confidence: str = "high"
    complete: bool = True
    source: str = ""
    turn_identity: str = ""
    timestamp: float = 0.0
    turn_complete: Optional[bool] = None
    body_complete: Optional[bool] = None

    def __post_init__(self) -> None:
        # Legacy callers supplied one flag. Treat it as evidence for both
        # dimensions, except that "partial" never proves a complete body.
        turn_complete = self.turn_complete
        body_complete = self.body_complete
        if turn_complete is None:
            turn_complete = self.complete and self.confidence != "partial"
        if body_complete is None:
            body_complete = self.complete and self.confidence != "partial"
        complete = bool(turn_complete and body_complete)
        object.__setattr__(self, "turn_complete", bool(turn_complete))
        object.__setattr__(self, "body_complete", bool(body_complete))
        object.__setattr__(self, "complete", complete)


_CHROME_LINE = re.compile(
    r"^(?:"
    r"worked for\b|cooked for\b|esc to interrupt\b|ctrl\+c to stop\b|"
    r"ctrl\+p commands\b|ask codex\b|add a follow-up\b|tab to queue\b|"
    r"send a message\b|auto balance\b|"
    r"[•∙]\s*(?:finished|explored|called|working|read)\b|"
    r"finished\b|empty\s+[—-]|^\s*[→~]|"
    r"plan,\s*search,\s*build anything\b|"
    r"trust this (?:folder|directory|project|repo|workspace)\b|"
    r"\[a\]\s*trust\b|\[d\]\s*don'?t trust\b|"
    r"yes,\s*i trust this folder\b"
    r")",
    re.IGNORECASE,
)
# Status chrome, not an answer. "Done", "OK", "완료", "PASS", and "네"
# can be the whole final body once an adapter has completion evidence.
_ONLY_COMPLETION_WORD = re.compile(
    r"^(?:finished|complete|completed|작업끝)[.!]?\s*$",
    re.IGNORECASE,
)
_BOX_ONLY = re.compile(r"^[\s─┃▀▁▂▃▄▅▆▇█░▒▓·•│╭╮╯╰▶═\-_=]+$")
# A tool row is not the answer: "• Added file (+3 -0)", a numbered diff
# gutter ("1 +<html>"), or the collapsed "Show details" control.
_TOOL_LINE = re.compile(
    r"^(?:"
    r"[•∙]\s*(?:added|edited|ran|read|explored|called|updated|deleted|"
    r"created|wrote|opened|searched|applied|removed|patched|listed)\b"
    r"|\d+\s+[+-]"
    r"|\+\s+show details\b"
    r"|-\s+show details\b"
    r"|show details"
    r")",
    re.IGNORECASE,
)


def result_fingerprint(text: str) -> str:
    return hashlib.sha256(text.strip().encode("utf-8", "replace")).hexdigest()[:16]


def drop_scrolled_user_prompt(lines: Sequence[str]) -> list:
    """Drop a user prompt whose ``›`` marker has scrolled off screen.

    That tail sits above the first answer bullet and is split from it by
    a blank line. A code fence in the head is part of the answer.
    """

    first = None
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not (stripped.startswith("•") or stripped.startswith("∙")):
            continue
        if _TOOL_LINE.match(stripped) or _CHROME_LINE.match(stripped):
            continue
        first = index
        break
    if first is None:
        return list(lines)
    head = lines[:first]
    if any("```" in line for line in head):
        return list(lines)
    if not any(not line.strip() for line in head):
        return list(lines)
    return list(lines[first:])


def prose_body(
    lines: Sequence[str], *, preserve_indented_code: bool = False,
    preserve_elapsed_examples: bool = False,
) -> Optional[str]:
    """Drop tool logs and status chrome while preserving answer formatting.

    "Finished" alone is still chrome. Length is not evidence: the caller
    has already found that agent's completion marker. Fenced code and blank
    lines are preserved; callers may preserve indentation when it denotes code.
    """

    kept = []
    fence = ""
    for line in lines:
        stripped = line.strip()
        if not stripped:
            kept.append("")
            continue
        raw = line.rstrip()
        if fence:
            kept.append(raw)
            if stripped.startswith(fence * 3):
                fence = ""
            continue
        if stripped.startswith(("```", "~~~")):
            fence = stripped[0]
            kept.append(raw)
            continue
        if preserve_indented_code and line.startswith(("    ", "\t")):
            kept.append(raw)
            continue
        if preserve_elapsed_examples and re.match(r"^(?:Worked|Cooked) for \d", stripped, re.IGNORECASE):
            kept.append(raw)
            continue
        if _BOX_ONLY.match(stripped) or _CHROME_LINE.match(stripped) or _TOOL_LINE.match(stripped):
            continue
        kept.append(stripped)
    while kept and not kept[0].strip():
        kept.pop(0)
    while kept and not kept[-1].strip():
        kept.pop()
    text = "\n".join(kept)
    if not text or _ONLY_COMPLETION_WORD.match(text.strip()):
        return None
    return text


@dataclass(frozen=True)
class Activity:
    """A best-effort, one-line description of what an agent looks like
    it's doing right now, extracted purely from terminal output patterns
    -- never from an LLM call (see docs/STATUS_ENGINE.md's "Task
    Awareness" section). ``confidence`` is informational ("high"/"medium"/"low");
    the UI shows activity only alongside an independently confirmed WORKING
    status so stale output is not presented as current work.

    Never persisted to disk: an activity string is a fragment of the
    pane's own terminal output, which can contain private project
    content, so it only ever exists in memory for the current refresh
    -- see ``state/`` for what Tower *does* persist (never this).
    """

    text: str
    confidence: str = "medium"
    source: str = "agent-output"


class AgentAdapter:
    """Default adapter: identifies nothing, classifies with generic rules only."""

    name = "Agent"
    command_names: Sequence[str] = ()
    title_hints: Sequence[str] = ()

    def matches(self, command: str, title: str, cmdline: str = "") -> bool:
        cmd = (command or "").strip().lower()
        low_title = (title or "").strip().lower()
        low_cmdline = (cmdline or "").strip().lower()

        for needle in self.command_names:
            if cmd == needle or cmd.startswith(needle):
                return True
            # tmux's #{pane_current_command} is often a generic wrapper name
            # (e.g. cursor-agent's binary reports as just "agent"), so also
            # check the full process command line when available.
            if needle in low_cmdline:
                return True

        for hint in self.title_hints:
            if hint in low_title:
                return True

        return False

    def classify(self, ctx: PaneContext) -> AdapterResult:
        return _NO_OPINION

    def extract_activity(self, ctx: PaneContext) -> Optional[Activity]:
        """What does this pane's output say the agent is doing right now?

        Returns ``None`` when there isn't enough evidence -- never
        fabricates a plausible-sounding guess. The default (this base
        implementation) never has an opinion; only agents with a verified,
        real-capture-based pattern override it.
        """

        return None

    def detect_execution(self, ctx: PaneContext) -> Optional[str]:
        """WORKING or IDLE when this agent is sure. Never an attention value."""

        status = self.classify(ctx).status
        if status in ("WORKING", "IDLE"):
            return status
        return None

    def detect_attention(self, ctx: PaneContext) -> str:
        """``none``, ``approval_required``, ``input_required``, or ``error``.

        The default never guesses. A shared regex is not an attention detector.
        """

        return "none"

    def extract_attention_prompt(self, ctx: PaneContext) -> str:
        """The current question only, already clamped. Empty when there is none."""

        return ""

    def detect_result(self, ctx: PaneContext) -> Optional[ResultCandidate]:
        return self.extract_result(ctx)

    def approve(self, ctx: PaneContext) -> Optional[str]:
        """One tmux key this agent's current approval widget documents, or None.

        None means Tower must not send a key. Callers must not invent Enter or y.
        """

        return None

    def reject(self, ctx: PaneContext) -> Optional[str]:
        """One tmux key for the labeled reject choice, or None."""

        return None

    def detect_interaction(self, ctx: PaneContext):
        """Current widget only. None when this agent has nothing to ask.

        The default never invents a numbered menu from old scrollback.
        """

        return None

    def followup_submit_key(self, before: PaneContext, after: PaneContext, text: str) -> Optional[str]:
        """One more submit key, only when the screen still shows the text unsent.

        The default never sends a second Enter. An adapter returns a key
        only from evidence on ``after``.
        """

        return None

    def extract_result(self, ctx: PaneContext) -> Optional[ResultCandidate]:
        """Final answer body, or None. Silence is not a result. The
        default has no final-response concept (shells and unknown
        commands stay None).
        """

        return None

    def submit_key(self, ctx: PaneContext) -> str:
        """The one tmux key that submits this agent's composer. Measured
        live for Codex, Claude Code, and OpenCode: a bracketed paste
        followed by a single Enter. Never sent twice by the caller."""

        return "Enter"

    def confirm_submitted(self, before: PaneContext, after: PaneContext, text: str) -> Optional[bool]:
        """Did the agent take the text as a new turn?

        True when there is evidence, None when unknown. The default only
        trusts a WORKING signal; agents with a transcript echo override
        this so a turn that finishes in two seconds is still confirmed.
        """

        if self.detect_execution(after) == "WORKING":
            return True
        return None


def looks_like_generic_waiting(text: str) -> bool:
    return bool(_GENERIC_WAIT_RE.search(text))


def prompt_head(text: str, width: int = 24) -> str:
    """The start of the first non-blank line, used to find the echoed
    user turn in a transcript without matching on the whole prompt."""

    for line in (text or "").split("\n"):
        stripped = line.strip()
        if stripped:
            return stripped[:width]
    return ""


def count_prompt_echoes(lines: Sequence[str], marker: re.Pattern, text: str) -> int:
    """Count transcript rows that contain this prompt's first line."""

    head = prompt_head(text)
    if not head:
        return 0
    return sum(1 for line in lines if marker.match(line) and head in line)


def count_matches(lines: Sequence[str], pattern: "re.Pattern[str]") -> int:
    return sum(1 for line in lines if pattern.search(line))


def title_has_spinner(title: str, chars: set) -> bool:
    stripped = (title or "").lstrip()
    return bool(stripped) and stripped[0] in chars
