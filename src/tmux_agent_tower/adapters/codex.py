"""Codex CLI adapter.

Evidence gathered by capturing a live Codex session (see
``evidence/baseline/`` notes, sanitized into ``fixtures/codex-*.txt``):

* Actively working: a line shaped like ``Working (12m 3s • esc to
  interrupt)`` appears above the input box, and/or the pane title starts
  with a braille spinner frame (e.g. ``⠙ Prepare P14 direct pilot``).
* Idle / ready for input: the bottom input box (``›``) is empty and shows a
  ``tab to queue message`` hint with no ``Working (...)`` line above it.
* Waiting on the user: an explicit approval/confirmation prompt is shown
  (falls back to the generic waiting-pattern detector).
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
    BRAILLE_SPINNER_CHARS,
    count_prompt_echoes,
    prompt_head,
    drop_scrolled_user_prompt,
    prose_body,
    result_fingerprint,
    title_has_spinner,
)

_WORKING_RE = re.compile(r"\bworking\s*\([^)]*esc to interrupt", re.IGNORECASE)
# Codex lists its own steps as bullet lines, e.g. "• Explored",
# "• Called some_tool.run_query", "• Calling some_tool.get_thing" -- the
# present-tense "Calling ..." form specifically marks a tool call that's
# still in flight (evidence: a real captured session, see
# fixtures/codex-working.txt).
_BULLET_RE = re.compile(r"^\s*•\s*(.+?)\s*$")
_IN_PROGRESS_RE = re.compile(r"^calling\b", re.IGNORECASE)
# The bullet-shaped status/summary lines that are NOT an action
# description and must never be surfaced as "current activity":
# "• Working (...)" (that's the status line itself, not a task) and
# "• Finished ..." (a past-tense turn-complete summary, misleading if
# shown while idle).
_NOT_AN_ACTIVITY_RE = re.compile(r"^(working\s*\(|finished\b)", re.IGNORECASE)
# Shown only once the current turn is done and the input box is the
# placeholder-empty "Ask Codex to do anything" prompt. While a turn is
# still running the same box instead reads "tab to queue message", which is
# deliberately NOT treated as an idle signal.
_IDLE_HINT_RE = re.compile(r"ask codex to do anything|for agents.*for shortcuts", re.IGNORECASE)
_IDLE_FOOTER_RE = re.compile(r"^\s*[›>]\s*Ask Codex to do anything\s*$", re.IGNORECASE)
_MODEL_STATUS_RE = re.compile(r"^\s*(?:GPT|Codex)\b[^\n]* · [^\n]+ · [^\n]+$", re.IGNORECASE)
_SHORTCUT_HINT_RE = re.compile(r"^\s*[←↵].*for agents.*for shortcuts\s*$", re.IGNORECASE)
_ALLOW_RE = re.compile(r"allow this command to run\?", re.IGNORECASE)
_WOULD_RUN_RE = re.compile(r"would you like to run the following command\?", re.IGNORECASE)
_YES_PROCEED_RE = re.compile(r"1\.\s+yes,\s+proceed\b", re.IGNORECASE)
_TRUST_RE = re.compile(r"trust this folder\?", re.IGNORECASE)
_OPTION_YES_RE = re.compile(r"^\s*[›>]?\s*1\.\s+yes\b", re.IGNORECASE | re.MULTILINE)
_OPTION_TRUST_RE = re.compile(r"^\s*[›>]?\s*1\.\s+trust\b", re.IGNORECASE | re.MULTILINE)
_OPTION_NO_RE = re.compile(r"^\s*[›>]?\s*3\.\s+no\b", re.IGNORECASE | re.MULTILINE)
_USER_LINE_RE = re.compile(r"^\s*[›>]\s+\S")
# A body sentence can contain "worked for". Only Codex's standalone elapsed
# summary line is a turn marker; matching prose here truncated a real Result.
_WORKED_FOR_RE = re.compile(
    r"^\s*Worked for \d+(?:\.\d+)?\s*[smh]\b"
    r"(?:\s+\d+(?:\.\d+)?\s*[smh]\b)?(?:\s*[•·].*)?$",
    re.IGNORECASE,
)
_FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
# Live trust widget (Codex CLI 0.159): the digit does not select the row.
# The footer is the key binding: "enter continue · esc quit".
_ENTER_CONTINUE_RE = re.compile(r"enter continue", re.IGNORECASE)
_ESC_QUIT_RE = re.compile(r"esc quit", re.IGNORECASE)


class CodexAdapter(AgentAdapter):
    name = "Codex"
    command_names = ("codex",)
    title_hints = ("codex",)

    def classify(self, ctx: PaneContext) -> AdapterResult:
        # The current widget wins. An older "Working" line or a stale
        # title spinner above an idle prompt is not this turn.
        widget = self._widget(ctx)
        idle_at = self._last(widget, _IDLE_HINT_RE, _WORKED_FOR_RE)
        work_at = self._last(widget, _WORKING_RE)
        if idle_at >= 0 and idle_at > work_at:
            return AdapterResult("IDLE", "idle-widget")
        if work_at >= 0:
            return AdapterResult("WORKING", "working-widget")
        if title_has_spinner(ctx.title, BRAILLE_SPINNER_CHARS):
            return AdapterResult("WORKING", "title-spinner")
        return AdapterResult(None)

    @staticmethod
    def _last(text: str, *patterns) -> int:
        found = -1
        for pattern in patterns:
            for match in pattern.finditer(text):
                found = max(found, match.start())
        return found

    def _widget(self, ctx: PaneContext) -> str:
        """The current bottom widget, not older scrollback above it.

        Eighteen lines covers a command preview between the question and
        the numbered choices. Idle and working chrome in that window still
        suppress a menu that has already scrolled away.
        """

        return "\n".join(ctx.lines[-18:])

    def detect_attention(self, ctx: PaneContext) -> str:
        widget = self._widget(ctx)
        command_menu = _OPTION_YES_RE.search(widget) and (
            _ALLOW_RE.search(widget) or _WOULD_RUN_RE.search(widget) or _YES_PROCEED_RE.search(widget)
        )
        trust_menu = _TRUST_RE.search(widget) and _OPTION_TRUST_RE.search(widget)
        approval_at = -1
        if command_menu:
            approval_at = max(
                self._last(widget, _ALLOW_RE, _WOULD_RUN_RE, _YES_PROCEED_RE),
                self._last(widget, _OPTION_YES_RE),
            )
        if trust_menu:
            approval_at = max(approval_at, self._last(widget, _TRUST_RE, _OPTION_TRUST_RE))

        lines = widget.split("\n")
        input_at = -1
        for index in range(len(lines) - 1, -1, -1):
            line = lines[index]
            if line.strip():
                if line.strip().endswith("?") and not _USER_LINE_RE.match(line):
                    input_at = index
                break

        attention_at = max(approval_at, input_at)
        current_at = max(
            self._last(widget, _WORKING_RE),
            self._last(widget, _IDLE_HINT_RE, _WORKED_FOR_RE),
        )
        if attention_at < 0 or current_at > attention_at:
            return "none"
        if approval_at >= input_at:
            return "approval_required"
        if input_at >= 0:
            return "input_required"
        return "none"

    def extract_attention_prompt(self, ctx: PaneContext) -> str:
        from ..detection.attention import clamp_prompt

        if self.detect_attention(ctx) == "none":
            return ""
        widget = self._widget(ctx)
        if _TRUST_RE.search(widget):
            return clamp_prompt("Trust this folder?")
        if _WOULD_RUN_RE.search(widget):
            return clamp_prompt("Would you like to run the following command?")
        if _ALLOW_RE.search(widget) or _YES_PROCEED_RE.search(widget):
            return clamp_prompt("Allow this command to run?")
        for line in reversed(widget.split("\n")):
            stripped = line.strip()
            if stripped.endswith("?"):
                return clamp_prompt(stripped)
        return ""

    def approve(self, ctx: PaneContext) -> Optional[str]:
        if self.detect_attention(ctx) != "approval_required":
            return None
        widget = self._widget(ctx)
        if _OPTION_YES_RE.search(widget) and (
            _ALLOW_RE.search(widget) or _WOULD_RUN_RE.search(widget) or _YES_PROCEED_RE.search(widget)
        ):
            return "1"
        # Trust: only the footer binding. A bare "1" was sent at this widget
        # and the menu did not move.
        if _TRUST_RE.search(widget) and _ENTER_CONTINUE_RE.search(widget):
            return "Enter"
        return None

    def detect_interaction(self, ctx: PaneContext):
        from ..detection.interaction import InteractionOption, approval, text_question, unknown

        attention = self.detect_attention(ctx)
        if attention == "none":
            return None
        widget = self._widget(ctx)
        prompt = self.extract_attention_prompt(ctx)
        if attention == "input_required":
            return text_question(prompt, "codex-question")
        command = _ALLOW_RE.search(widget) or _WOULD_RUN_RE.search(widget) or _YES_PROCEED_RE.search(widget)
        if command and _OPTION_YES_RE.search(widget):
            options = [InteractionOption("예", "1")]
            if _OPTION_NO_RE.search(widget):
                options.append(InteractionOption("아니오", "3"))
            return approval(prompt, options, "codex-command-menu")
        if _TRUST_RE.search(widget) and _ENTER_CONTINUE_RE.search(widget):
            options = [InteractionOption("진행", "Enter")]
            if _ESC_QUIT_RE.search(widget):
                options.append(InteractionOption("취소", "Escape"))
            return approval(prompt, options, "codex-trust")
        if attention == "approval_required":
            return unknown(prompt, "codex-unmapped")
        return None

    def reject(self, ctx: PaneContext) -> Optional[str]:
        if self.detect_attention(ctx) != "approval_required":
            return None
        widget = self._widget(ctx)
        if _OPTION_NO_RE.search(widget) and (
            _ALLOW_RE.search(widget) or _WOULD_RUN_RE.search(widget) or _YES_PROCEED_RE.search(widget)
        ):
            return "3"
        if _TRUST_RE.search(widget) and _ESC_QUIT_RE.search(widget):
            return "Escape"
        return None

    def confirm_submitted(self, before: PaneContext, after: PaneContext, text: str) -> Optional[bool]:
        # Measured on Codex 0.159: a bracketed paste leaves "› <text>" in the
        # composer with no idle hint. Once Enter is taken the same line
        # stays as the transcript echo and either a Working line or the
        # finished "Worked for" + idle hint follows.
        was_working = self.detect_execution(before) == "WORKING"
        is_working = self.detect_execution(after) == "WORKING"
        if is_working and not was_working:
            return True
        echoed = count_prompt_echoes(after.lines, _USER_LINE_RE, text) > count_prompt_echoes(
            before.lines, _USER_LINE_RE, text
        )
        tail = after.tail(30)
        if echoed and (_IDLE_HINT_RE.search(tail) or _WORKED_FOR_RE.search(tail)):
            return True
        return None

    def extract_activity(self, ctx: PaneContext) -> Optional[Activity]:
        # Bullets are only trustworthy as "current activity" while there is
        # direct evidence we're still mid-turn (the "Working (... esc to
        # interrupt)" line). Once a turn finishes, Codex leaves its own
        # summary bullets sitting in scrollback right above the idle
        # "Ask Codex to do anything" prompt -- a naive "last bullet found
        # anywhere" scan would report that stale, completed summary as if
        # it were still happening (caught live against a real idle pane,
        # not a synthetic fixture: a "그럴듯한 오답").
        full = "\n".join(ctx.lines)
        if not _WORKING_RE.search(full):
            return None

        # Full capture, not ctx.tail(20): a short reply can leave enough
        # blank padding below it that the last real bullet line falls
        # outside the last 20 lines (caught live).
        bullets = [
            m.group(1)
            for line in ctx.lines
            if (m := _BULLET_RE.match(line)) and not _NOT_AN_ACTIVITY_RE.match(m.group(1))
        ]

        if not bullets:
            return None

        for text in reversed(bullets):
            if _IN_PROGRESS_RE.match(text):
                return Activity(text=text, confidence="high", source="agent-output")

        return Activity(text=bullets[-1], confidence="medium", source="agent-output")

    def extract_result(self, ctx: PaneContext) -> Optional[ResultCandidate]:
        # A finished turn shows "Worked for ..." and the idle prompt.
        # "Finished" alone, or the same words inside a user prompt, is
        # not that evidence. A Working line means this screen is not final.
        lines = list(ctx.lines)
        if not _IDLE_HINT_RE.search(ctx.tail(15)):
            return None
        worked = _completion_markers(lines)
        if not worked:
            return None
        end = worked[-1]
        after_completion = "\n".join(lines[end + 1:])
        if _TRUST_RE.search(after_completion) and _OPTION_TRUST_RE.search(after_completion):
            return None
        # A Working line from an older turn is above this marker. A new
        # turn that is still running sits below it, or in the title.
        if _WORKING_RE.search("\n".join(lines[end:])):
            return None
        # A stale title spinner must not hide a finished idle widget.
        if title_has_spinner(ctx.title, BRAILLE_SPINNER_CHARS) and not _IDLE_HINT_RE.search(ctx.tail(15)):
            return None
        prior_transitions = [
            (marker, *_prior_turn_transition(lines, marker, end))
            for marker in worked[:-1]
        ]
        proven_prior = [marker for marker, proven, _has_prompt in prior_transitions if proven]
        last_proven = max(proven_prior, default=-1)
        ambiguous_transition = any(
            has_prompt and not proven and marker > last_proven
            for marker, proven, has_prompt in prior_transitions
        )
        floor = last_proven + 1 if proven_prior else 0
        start = floor
        found_user = False
        for i in range(floor, end):
            line = lines[i]
            if not line.strip() or _IDLE_FOOTER_RE.match(line):
                continue
            if _MODEL_STATUS_RE.match(line) or _SHORTCUT_HINT_RE.match(line):
                continue
            if _WORKING_RE.match(line) or (
                (bullet := _BULLET_RE.match(line)) and _NOT_AN_ACTIVITY_RE.match(bullet.group(1))
            ):
                continue
            if _USER_LINE_RE.match(line):
                start = i + 1
                found_user = True
                break
            # Output before a prompt boundary could be an answer suffix, not
            # proof that the prompt-shaped lines later in this capture are real.
            break
        bounded = found_user and not ambiguous_transition
        if not bounded:
            # A completion marker alone cannot prove where a captured suffix
            # began, even when older completion-looking lines are present.
            start = max(start, end - 60)
        text = prose_body(
            drop_scrolled_user_prompt(lines[start:end]),
            preserve_indented_code=True, preserve_elapsed_examples=True,
        )
        if text is None:
            return None
        confidence = "high" if bounded else "partial"
        return ResultCandidate(
            text=text,
            fingerprint=result_fingerprint(text),
            confidence=confidence,
            complete=bounded,
            turn_complete=True,
            body_complete=bounded,
        )


def _completion_markers(lines: list[str]) -> list[int]:
    """Return elapsed-time footers followed by Codex's exact idle prompt."""

    markers = []
    fence_char = ""
    fence_size = 0
    for index, line in enumerate(lines):
        fence = _FENCE_RE.match(line)
        if fence:
            run, suffix = fence.groups()
            if not fence_char:
                if run[0] == "`" and "`" in suffix:
                    continue
                fence_char, fence_size = run[0], len(run)
            elif run[0] == fence_char and len(run) >= fence_size and not suffix.strip():
                fence_char, fence_size = "", 0
            continue
        if fence_char or line.startswith(("    ", "\t")) or not _WORKED_FOR_RE.match(line):
            continue
        following = next((item for item in lines[index + 1:] if item.strip()), "")
        if _IDLE_FOOTER_RE.match(following):
            markers.append(index)
    return markers


def _prior_turn_transition(lines: list[str], marker: int, end: int) -> tuple[bool, bool]:
    """Require Codex's idle chrome before using an earlier marker as a boundary."""

    footer = next(
        (index for index in range(marker + 1, end) if lines[index].strip()),
        end,
    )
    if footer == end or not _IDLE_FOOTER_RE.match(lines[footer]):
        return False, False
    model_status = shortcut_hint = False
    for index in range(footer + 1, end):
        line = lines[index]
        if not line.strip():
            continue
        if _MODEL_STATUS_RE.match(line):
            model_status = True
        elif _SHORTCUT_HINT_RE.match(line):
            shortcut_hint = True
        elif _USER_LINE_RE.match(line):
            return model_status and shortcut_hint, True
        else:
            return False, any(_USER_LINE_RE.match(item) for item in lines[index + 1:end])
    return False, False
