"""OpenCode adapter.

Verified 2026-09-30 against a real, live OpenCode 1.18.32 session (see
``fixtures/opencode-working.txt`` / ``opencode-idle.txt``, sanitized):

* Actively working: the bottom status bar shows a block/dot progress
  indicator followed by ``esc interrupt`` (e.g. ``⬝⬝⬝⬝⬝⬝⬝⬝  esc interrupt``).
  This text is absent whenever OpenCode is idle — confirmed by comparing
  multiple idle captures before and after a real task.
* Idle / ready: bottom bar instead reads ``... ctrl+p commands`` (no
  ``esc interrupt``), and/or a just-finished summary line like
  ``▣  Build · <mode> · 6.0s`` (past tense — the turn is over).

NOT verified live: an explicit permission/confirmation prompt. The
session tested was configured in a mode that auto-approved file writes,
so no such prompt ever appeared on screen to capture. WAITING therefore
still falls back entirely to the generic waiting-pattern detector for
this adapter — treat that as an open item if OpenCode's own approval UI
turns out to need a specific pattern (see CONTRIBUTING.md).
"""

from __future__ import annotations

import re
from typing import Optional

from .base import (
    Activity,
    AgentAdapter,
    AdapterResult,
    PaneContext,
    ResultCandidate,
    count_matches,
    count_prompt_echoes,
    prose_body,
    result_fingerprint,
)

# "▣  Build · <model> · 5.2s" closes a turn. A narrow pane wraps the
# duration onto the next line ("· 2m" / "24s") or stops at minutes.
_TURN_DONE_RE = re.compile(r"^\s*▣.*\d+(?:\.\d+)?\s*[smh]\b", re.IGNORECASE)
_ELAPSED_RE = re.compile(r"\d+(?:\.\d+)?\s*[smh]\b", re.IGNORECASE)
_PROMPT_BOX_RE = re.compile(r"^\s*[┃│]")


def _done_marks(lines) -> list:
    """Indexes of ▣ lines whose duration is on that line or the next."""

    marks = []
    for index, line in enumerate(lines):
        if not re.match(r"^\s*▣", line):
            continue
        blob = line if index + 1 >= len(lines) else f"{line} {lines[index + 1]}"
        if _ELAPSED_RE.search(blob):
            marks.append(index)
    return marks


_WORKING_RE = re.compile(r"\besc interrupt\b", re.IGNORECASE)
_IDLE_HINT_RE = re.compile(r"ctrl\+p commands", re.IGNORECASE)
# "~ Preparing write…" -- a tool call actively being prepared/run.
_PREPARING_RE = re.compile(r"^\s*~\s*(.+?)\s*$")
# "→ Read ." -- a tool invocation already dispatched (may be finished by
# the time this is read, hence lower confidence than the "~" form).
_ARROW_RE = re.compile(r"^\s*→\s*(.+?)\s*$")


class OpenCodeAdapter(AgentAdapter):
    name = "OpenCode"
    command_names = ("opencode",)
    title_hints = ("opencode",)

    def classify(self, ctx: PaneContext) -> AdapterResult:
        # OpenCode runs on the alternate screen, so the whole capture is
        # the current frame. Its welcome layout centers the prompt box,
        # which put the idle hint 41 rows above the bottom of a tall pane
        # (caught live). The status region is one widget either way.
        working_lines = [i for i, line in enumerate(ctx.lines) if _WORKING_RE.search(line)]
        idle_lines = [i for i, line in enumerate(ctx.lines) if _IDLE_HINT_RE.search(line)]

        # The live working footer contains both phrases on one line. An
        # older "esc interrupt" row followed by the current idle footer is
        # stale scrollback and must not win merely because it exists.
        last_working = working_lines[-1] if working_lines else -1
        last_idle = idle_lines[-1] if idle_lines else -1
        if last_working >= 0 and (last_working >= last_idle):
            return AdapterResult("WORKING", "interrupt-hint")

        if last_idle >= 0:
            return AdapterResult("IDLE", "commands-hint-no-interrupt")

        return AdapterResult(None)

    def _widget(self, ctx: PaneContext) -> str:
        return "\n".join(ctx.lines[-8:])

    def detect_attention(self, ctx: PaneContext) -> str:
        # The live approval widget was never captured. This matches only the
        # synthesized fixture shape, and approve() stays None on purpose.
        widget = self._widget(ctx)
        if re.search(r"allow this tool call\?", widget, re.IGNORECASE) and re.search(r"\(y/n\)", widget, re.IGNORECASE):
            lines = widget.split("\n")
            prompt_at = max(i for i, line in enumerate(lines) if "?" in line)
            current_at = max(
                [i for i, line in enumerate(lines) if _WORKING_RE.search(line) or _IDLE_HINT_RE.search(line)]
                + [-1]
            )
            if current_at <= prompt_at:
                return "approval_required"
        return "none"

    def extract_attention_prompt(self, ctx: PaneContext) -> str:
        from ..detection.attention import clamp_prompt

        if self.detect_attention(ctx) == "none":
            return ""
        for line in reversed(self._widget(ctx).split("\n")):
            if "?" in line:
                return clamp_prompt(line.strip())
        return ""

    def confirm_submitted(self, before: PaneContext, after: PaneContext, text: str) -> Optional[bool]:
        # Measured on OpenCode 1.18: the submitted message is drawn as a
        # "┃  <text>" block and "esc interrupt" appears while it runs; a
        # finished turn adds a "▣ … Ns" line.
        was_working = self.detect_execution(before) == "WORKING"
        is_working = self.detect_execution(after) == "WORKING"
        if is_working and not was_working:
            return True
        echoed = count_prompt_echoes(after.lines, _PROMPT_BOX_RE, text) > count_prompt_echoes(
            before.lines, _PROMPT_BOX_RE, text
        )
        if echoed and count_matches(after.lines, _TURN_DONE_RE) > count_matches(before.lines, _TURN_DONE_RE):
            return True
        return None

    def extract_activity(self, ctx: PaneContext) -> Optional[Activity]:
        # Deliberately the FULL capture, not ctx.tail(20): a short response
        # can leave a lot of blank padding between the activity/tool-call
        # text and the bottom status bar, pushing it out of the last 20
        # lines (caught live via a fixture with exactly this shape).
        lines = ctx.lines
        full = "\n".join(lines)

        for line in reversed(lines):
            m = _PREPARING_RE.match(line)
            if m:
                return Activity(text=m.group(1), confidence="high", source="agent-output")

        # A "→ Tool" line by itself is NOT reliable evidence of *current*
        # activity: it stays on screen after the turn finishes too (caught
        # live -- the idle fixture is literally a finished turn that still
        # shows its own "→ Read ." line). Only trust it as a fallback when
        # the working indicator confirms we're still actually mid-turn.
        if _WORKING_RE.search(full):
            for line in reversed(lines):
                m = _ARROW_RE.match(line)
                if m:
                    return Activity(text=m.group(1), confidence="medium", source="agent-output")

        return None

    def extract_result(self, ctx: PaneContext) -> Optional[ResultCandidate]:
        lines = list(ctx.lines)
        if self.classify(ctx).status != "IDLE":
            return None
        marks = _done_marks(lines)
        if not marks:
            return None
        if _WORKING_RE.search("\n".join(lines[marks[-1]:])):
            return None
        floor = marks[-2] + 1 if len(marks) > 1 else 0
        prompt_lines = [
            i for i, line in enumerate(lines[: marks[-1]])
            if _PROMPT_BOX_RE.match(line) and line.strip().strip("┃│").strip()
        ]
        if prompt_lines and prompt_lines[-1] >= floor:
            floor = prompt_lines[-1] + 1
            bounded = True
        else:
            bounded = len(marks) > 1
        if len(marks) < 2:
            if not bounded:
                floor = max(floor, marks[-1] - 60)
        body = [
            line for line in lines[floor:marks[-1]]
            if not _PROMPT_BOX_RE.match(line)
        ]
        text = prose_body(body)
        if text is None:
            return None
        return ResultCandidate(
            text=text,
            fingerprint=result_fingerprint(text),
            confidence="high" if bounded else "partial",
            complete=bounded,
            turn_complete=True,
            body_complete=bounded,
        )
