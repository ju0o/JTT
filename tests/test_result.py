import json
import multiprocessing
import os
from pathlib import Path
import pty
import socket
import sqlite3
import termios

import pytest

from tmux_agent_tower.adapters.base import PaneContext, ResultCandidate, prose_body
from tmux_agent_tower.adapters.claude import ClaudeAdapter
from tmux_agent_tower.adapters.codex import CodexAdapter
from tmux_agent_tower.adapters.cursor import CursorAdapter
from tmux_agent_tower.adapters.grok import GrokAdapter
from tmux_agent_tower.adapters.opencode import OpenCodeAdapter
from tmux_agent_tower.adapters.shell import ShellAdapter
from tmux_agent_tower.adapters import resolve_adapter
from tmux_agent_tower.detection.result import ResultTracker, pane_result_identity
from tmux_agent_tower.detection.topology import adapter_for_screen
from tmux_agent_tower.server.httpapi import build_status_payload
from tmux_agent_tower.server.webui import PAGE_HTML
from tmux_agent_tower.ui.tower import CAPTURE_LINES, _disable_xon_flow_control

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def test_tower_tui_disables_xon_for_ctrl_s_result_action():
    master, slave = pty.openpty()
    try:
        attrs = termios.tcgetattr(slave)
        attrs[0] |= termios.IXON
        termios.tcsetattr(slave, termios.TCSANOW, attrs)

        _disable_xon_flow_control(slave)

        changed = termios.tcgetattr(slave)
        assert not changed[0] & termios.IXON
        assert changed[0] == attrs[0] & ~termios.IXON
        assert changed[1:] == attrs[1:]
    finally:
        os.close(master)
        os.close(slave)


def lines(name: str):
    return tuple(FIXTURES.joinpath(name).read_text(encoding="utf-8").splitlines())


def ctx(name: str, title: str = "", command: str = ""):
    return PaneContext(title=title, command=command, lines=lines(name))


def test_codex_idle_extracts_the_answer_not_the_finished_bullet():
    result = CodexAdapter().extract_result(ctx("codex-idle.txt"))
    assert result is not None
    assert result.text == "Summary of what changed goes here."
    assert "Finished" not in result.text


def test_codex_result_drops_the_tool_diff_and_keeps_a_code_block():
    screen = [
        "이해가 안됨. HTML로 보여줘",
        "",
        "• 요청하신 대로 작전판으로 정리하겠습니다.",
        "• Added plan.html (+3 -0)",
        "1 +<!doctype html>",
        "2 +<html>",
        "+ Show details",
        "정리한 시각적 작전판입니다.",
        "```python",
        "print('tower')",
        "```",
        "Worked for 2m 22s",
        "› Ask Codex to do anything",
    ]
    result = CodexAdapter().extract_result(PaneContext(title="", command="codex", lines=tuple(screen)))
    assert result is not None
    assert "Added" not in result.text
    assert "<!doctype" not in result.text
    assert "Show details" not in result.text
    assert "이해가 안됨" not in result.text
    assert "작전판으로 정리" in result.text
    assert "print('tower')" in result.text


def test_codex_completion_words_inside_fences_do_not_truncate_2751_byte_answer():
    prefix = ["```text", *(["A" * 29] * 65), "B" * 80, "Worked for 5s", "Worked for 6s"]
    suffix = ["```", *(["C" * 29] * 21), "D" * 50]
    body = prefix + suffix
    body_text = "\n".join(body)
    assert len(body) == 92
    assert len(body_text.encode("utf-8")) == 2751
    assert len("\n".join(suffix).encode("utf-8")) == 684

    history = ["› synthetic request", *body, "Worked for 1s", "› Ask Codex to do anything"]
    candidate = CodexAdapter().extract_result(PaneContext("", "codex", tuple(history)))

    assert candidate is not None and candidate.complete
    assert candidate.body_complete is True and candidate.turn_complete is True
    assert candidate.text == body_text


def test_codex_result_without_a_start_boundary_stays_partial():
    history = ["visible suffix only", "Worked for 1s", "› Ask Codex to do anything"]
    candidate = CodexAdapter().extract_result(PaneContext("", "codex", tuple(history)))

    assert candidate is not None
    assert candidate.turn_complete is True
    assert candidate.body_complete is False
    assert candidate.complete is False


def test_codex_indented_worked_for_line_stays_in_complete_answer():
    history = [
        "› synthetic request",
        "    Worked for 5s",
        "actual answer",
        "Worked for 1s",
        "› Ask Codex to do anything",
    ]

    candidate = CodexAdapter().extract_result(PaneContext("", "codex", tuple(history)))

    assert candidate is not None and candidate.complete
    assert candidate.text == "    Worked for 5s\nactual answer"


def test_codex_capture_starting_inside_code_fails_closed_without_turn_start():
    history = [
        "    code continuation",
        "Worked for 5s",
        "more code continuation",
        "```",
        "another code block",
        "```",
        "actual answer",
        "Worked for 1s",
        "› Ask Codex to do anything",
    ]

    candidate = CodexAdapter().extract_result(PaneContext("", "codex", tuple(history)))

    assert candidate is None or not candidate.complete


def test_codex_fenced_worked_for_and_prompt_text_stay_inside_the_answer():
    history = [
        "› synthetic request",
        "```text",
        "Worked for 5s",
        "› Ask Codex to do anything",
        "actual code continuation",
        "```",
        "actual answer",
        "Worked for 1s",
        "› Ask Codex to do anything",
    ]

    candidate = CodexAdapter().extract_result(PaneContext("", "codex", tuple(history)))

    assert candidate is not None and candidate.complete
    assert candidate.text == "\n".join(history[1:6] + ["actual answer"])


def test_codex_terminal_example_prompt_is_not_a_completion_or_turn_start():
    body = [
        "답변 첫 문단 - 반드시 포함되어야 함",
        "예시 CLI 로그:",
        "Worked for 5s",
        "› example next command",
        "예시 로그 아래의 설명",
        "정상적인 답변 마지막 문단",
    ]
    history = ["› 실제 사용자 요청", *body, "Worked for 1s", "› Ask Codex to do anything"]

    candidate = CodexAdapter().extract_result(PaneContext("", "codex", tuple(history)))

    assert candidate is not None and candidate.complete
    assert candidate.text == "\n".join(body)


def test_codex_mid_answer_capture_with_example_prompt_stays_partial():
    history = [
        "예시 CLI 로그:",
        "Worked for 5s",
        "› example next command",
        "예시 로그 아래의 설명",
        "정상적인 답변 마지막 문단",
        "Worked for 1s",
        "› Ask Codex to do anything",
    ]

    candidate = CodexAdapter().extract_result(PaneContext("", "codex", tuple(history)))

    assert candidate is None or candidate.complete is False


def test_codex_exact_footer_example_is_ambiguous_and_stays_partial():
    history = [
        "› actual user request",
        "Answer before the terminal example.",
        "Worked for 5s",
        "› Ask Codex to do anything",
        "› example next command",
        "Answer after the terminal example.",
        "Worked for 1s",
        "› Ask Codex to do anything",
    ]

    candidate = CodexAdapter().extract_result(PaneContext("", "codex", tuple(history)))

    assert candidate is not None
    assert candidate.complete is False
    assert candidate.body_complete is False


def test_codex_unknown_status_after_prior_completion_is_ambiguous():
    history = [
        "› first user question",
        "FIRST TURN ANSWER",
        "Worked for 5s",
        "› Ask Codex to do anything",
        "Updated status: context 62%",
        "› second user question",
        "SECOND TURN ANSWER",
        "Worked for 1s",
        "› Ask Codex to do anything",
    ]

    candidate = CodexAdapter().extract_result(PaneContext("", "codex", tuple(history)))

    assert candidate is not None
    assert candidate.complete is False
    assert candidate.body_complete is False


def test_get_result_rejects_reused_local_pane_before_recovery(monkeypatch):
    from tmux_agent_tower.control.actions import get_result

    tower = _RecoverTower()
    tower.rows[0].update({"pane_id": "%9", "pane_pid": "current-pid"})
    monkeypatch.setattr(
        "tmux_agent_tower.control.actions._pane_matches_live_identity",
        lambda *_args: False,
    )
    monkeypatch.setattr(
        "tmux_agent_tower.control.actions._recover_result",
        lambda *_args, **_kwargs: pytest.fail("reused pane must not reach result recovery"),
    )

    ok, reason, payload = get_result(tower, "%9", expected_pane_pid="current-pid")

    assert not ok and reason == "stale"
    assert payload["reason_code"] == "UNKNOWN"


def test_get_result_rejects_local_pane_without_expected_pid(monkeypatch):
    from tmux_agent_tower.control.actions import get_result

    tower = _RecoverTower()
    tower.rows[0].update({"pane_id": "%9", "pane_pid": ""})
    monkeypatch.setattr(
        "tmux_agent_tower.control.actions._pane_matches_live_identity",
        lambda *_args: pytest.fail("PID-less pane must fail before identity lookup"),
    )
    monkeypatch.setattr(
        "tmux_agent_tower.control.actions._recover_result",
        lambda *_args, **_kwargs: pytest.fail("PID-less pane must not reach result recovery"),
    )

    ok, reason, payload = get_result(tower, "%9", expected_pane_pid="")

    assert not ok and reason == "stale"
    assert payload["reason_code"] == "UNKNOWN"


def test_get_result_rechecks_local_pid_after_capture(monkeypatch):
    from tmux_agent_tower.control.actions import get_result

    tower = _RecoverTower()
    tower.rows[0].update({"pane_id": "%9", "pane_pid": "1234"})
    _patch_history(monkeypatch, tower, [
        "› user request", "complete answer", "Worked for 1s", "› Ask Codex to do anything",
    ])
    checks = iter((True, True, False))
    monkeypatch.setattr(
        "tmux_agent_tower.control.actions._pane_matches_live_identity",
        lambda *_args: next(checks),
    )

    ok, _reason, payload = get_result(tower, "%9", expected_pane_pid="1234")

    assert ok and payload["complete"] is False
    assert payload["reason_code"] == "UNKNOWN"


def test_result_recovery_reports_when_history_byte_range_was_exceeded(monkeypatch):
    from tmux_agent_tower.control.actions import get_result

    tower = _RecoverTower()
    _patch_history(monkeypatch, tower, ["partial answer", "Worked for 1s", "› Ask Codex to do anything"])
    monkeypatch.setattr("tmux_agent_tower.control.actions.RECOVERY_BYTES", 1)

    ok, _reason, payload = get_result(tower, "%9")

    assert ok and payload["complete"] is False
    assert payload["reason_code"] == "OUTPUT_RANGE_EXCEEDED"


def test_recovery_range_reason_requires_actual_truncation():
    from tmux_agent_tower.control.actions import (
        RECOVERY_BYTES, RECOVERY_LINES, _bounded_history_with_truncation,
    )

    exact_lines, exact_lines_truncated = _bounded_history_with_truncation(["x"] * RECOVERY_LINES)
    over_lines, over_lines_truncated = _bounded_history_with_truncation(["x"] * (RECOVERY_LINES + 1))
    exact_bytes, exact_bytes_truncated = _bounded_history_with_truncation(["x" * RECOVERY_BYTES])
    over_bytes, over_bytes_truncated = _bounded_history_with_truncation(["x" * (RECOVERY_BYTES + 1)])

    assert len(exact_lines) == RECOVERY_LINES and not exact_lines_truncated
    assert len(over_lines) == RECOVERY_LINES and over_lines_truncated
    assert exact_bytes == ["x" * RECOVERY_BYTES] and not exact_bytes_truncated
    assert over_bytes == [] and over_bytes_truncated


@pytest.mark.parametrize(
    ("history_size", "expected_reason"),
    [(800, "UNKNOWN"), (801, "OUTPUT_RANGE_EXCEEDED")],
)
def test_recovery_range_reason_uses_history_size_evidence(monkeypatch, history_size, expected_reason):
    from tmux_agent_tower.control.actions import get_result

    tower = _RecoverTower()
    _patch_history(monkeypatch, tower, ["unrecognized idle output"] * 800)
    monkeypatch.setattr(
        "tmux_agent_tower.control.actions.tmux_capture.pane_history_position",
        lambda _pane: (history_size, 2000),
    )

    ok, _reason, payload = get_result(tower, "%9")

    assert ok and not payload["complete"]
    assert payload["reason_code"] == expected_reason


def test_codex_prose_worked_for_is_not_a_turn_boundary():
    body = [
        "START_MARKER",
        "Section 1: The run worked for several configurations.",
        "",
        "```python",
        "    def preserve():",
        "        return 'all lines'",
        "```",
        "",
        "Section 2: details that must stay in the copied body.",
        "FOUNDER_ACTION_REQUIRED:",
        "- review the complete result",
        "NEXT_ACTION:",
        "- continue from this exact point",
        "END_MARKER",
    ]
    screen = ["› prepare the report", *body, "Worked for 2m 4s • 10:00", "› Ask Codex to do anything"]
    candidate = CodexAdapter().extract_result(PaneContext("", "codex", tuple(screen)))

    assert candidate is not None
    assert candidate.text == "\n".join(body)
    assert candidate.turn_complete is True
    assert candidate.body_complete is True
    assert candidate.complete is True


def test_result_cleanup_preserves_markdown_blank_lines_indentation_and_fenced_chrome():
    body = [
        "START_MARKER",
        "",
        "  ```python",
        "    Worked for this exact string must remain code.",
        "    print('indentation')",
        "  ```",
        "",
        "END_MARKER",
    ]
    screen = ["› write the full body", *body, "Worked for 2m 4s • 10:00", "› Ask Codex to do anything"]

    candidate = CodexAdapter().extract_result(PaneContext("", "codex", tuple(screen)))

    assert candidate is not None and candidate.complete
    assert candidate.text == "\n".join(body)


def test_long_codex_result_outside_visible_screen_never_returns_a_complete_suffix():
    body = ["START_MARKER"]
    body.extend(f"한국어 결과 {index:03d}: code and wrapped content is preserved." for index in range(120))
    body.extend(("FOUNDER_ACTION_REQUIRED:", "- verify every item", "NEXT_ACTION:", "- finish", "END_MARKER"))
    history = ["› produce the full report", *body, "Worked for 3m 1s", "› Ask Codex to do anything"]
    visible = history[-30:]
    visible_candidate = CodexAdapter().extract_result(PaneContext("", "codex", tuple(visible)))
    full_candidate = CodexAdapter().extract_result(PaneContext("", "codex", tuple(history)))

    assert visible_candidate is not None
    assert visible_candidate.turn_complete is True
    assert visible_candidate.body_complete is False
    assert visible_candidate.complete is False
    assert full_candidate is not None and full_candidate.complete is True
    assert full_candidate.text == "\n".join(body)
    assert full_candidate.text.startswith("START_MARKER\n")
    assert full_candidate.text.endswith("END_MARKER")


@pytest.mark.parametrize(
    ("adapter", "command", "prefix", "completion", "idle"),
    [
        (CodexAdapter, "codex", ["› user request"], "Worked for 3m 1s", ["› Ask Codex to do anything"]),
        (ClaudeAdapter, "claude", ["❯ user request"], "✻ Cooked for 3m 1s", ["❯", "⏵⏵ auto mode on"]),
        (CursorAdapter, "cursor-agent", ["→ Add a follow-up"], "→ Add a follow-up", []),
        (OpenCodeAdapter, "opencode", ["▣ Build · model · 1.0s", "┃ user request"], "▣ Build · model · 3m 1s", ["ctrl+p commands"]),
    ],
)
def test_long_result_requires_full_body_boundary_for_each_adapter(adapter, command, prefix, completion, idle):
    body = ["START_MARKER", "Section 1", "", "```python", "    Worked for is code, not a boundary.", "    print('preserve')", "```", ""]
    body.extend(f"Section {index:03d}: 긴 한국어 결과 본문을 화면 밖까지 온전히 복구해야 합니다." for index in range(120))
    body.extend(("FOUNDER_ACTION_REQUIRED:", "- review the complete answer", "NEXT_ACTION:", "- finish the task", "END_MARKER"))
    history = [*prefix, *body, completion, *idle]
    partial = adapter().extract_result(PaneContext("", command, tuple(history[-30:])))
    complete = adapter().extract_result(PaneContext("", command, tuple(history)))

    assert partial is None or partial.body_complete is False
    assert partial is None or partial.complete is False
    assert complete is not None and complete.complete
    assert complete.turn_complete is True and complete.body_complete is True
    assert complete.text == "\n".join(body)


def test_codex_working_is_not_a_result():
    assert CodexAdapter().extract_result(ctx("codex-working.txt", title="⠙ working")) is None


def test_codex_trust_widget_after_old_completion_is_not_a_result():
    screen = (
        "The old answer stays here.",
        "Worked for 2s",
        "› Ask Codex to do anything",
        "Trust this folder?",
        "1. Trust this folder",
        "2. No, exit",
    )
    adapter = CodexAdapter()
    context = PaneContext(title="", command="codex", lines=screen)
    assert adapter.classify(context).status == "IDLE"
    assert adapter.extract_result(context) is None
    assert ResultTracker().observe("%1", "IDLE", adapter.extract_result(context)).state == "none"


@pytest.mark.parametrize(
    ("adapter", "command", "screen", "answer"),
    [
        (CodexAdapter, "codex", ("› user request", "OK", "Worked for 2s", "› Ask Codex to do anything"), "OK"),
        (ClaudeAdapter, "claude", ("❯ user request", "PASS", "✻ Cooked for 2s", "❯", "⏵⏵ auto mode on"), "PASS"),
        (CursorAdapter, "cursor-agent", ("→ Add a follow-up", "완료", "→ Add a follow-up"), "완료"),
        (OpenCodeAdapter, "opencode", ("▣ Build · model · 1.0s", "┃ user request", "OK", "▣ Build · model · 2.0s", "ctrl+p commands"), "OK"),
    ],
)
def test_short_answer_is_kept_with_agent_completion_evidence(adapter, command, screen, answer):
    result = adapter().extract_result(PaneContext(title="", command=command, lines=screen))
    assert result is not None
    assert result.text == answer
    assert result.turn_complete is True
    assert result.body_complete is True
    assert result.complete is True


def test_claude_tool_bullet_alone_is_not_a_result():
    assert ClaudeAdapter().extract_result(ctx("claude-idle-with-last-action.txt")) is None


def test_claude_prose_before_cooked_is_the_result():
    screen = [
        "Session binding now follows the Tower that started it.",
        "✻ Cooked for 4m 2s",
        "❯",
    ]
    result = ClaudeAdapter().extract_result(PaneContext(title="", command="claude", lines=tuple(screen)))
    assert result is not None
    assert result.text.startswith("Session binding")


def test_opencode_tool_summary_is_not_a_result_but_prose_is():
    assert OpenCodeAdapter().extract_result(ctx("opencode-idle.txt")) is None
    screen = [
        "The promo file is in out/SamplePortal_Promo.mp4.",
        "▣  Build · Space Bunny Free · 6.0s",
        "ctrl+p commands",
    ]
    result = OpenCodeAdapter().extract_result(PaneContext(title="", command="opencode", lines=tuple(screen)))
    assert result is not None
    assert "promo file" in result.text
    assert result.complete is False
    assert result.body_complete is False


def test_opencode_first_completed_answer_survives_long_idle_screen():
    screen = list(lines("opencode-stale-working-idle.txt"))
    completion = next(i for i, line in enumerate(screen) if line.lstrip().startswith("▣"))
    screen[completion:completion] = ["┃ synthetic request", "Synthetic complete answer."]
    screen.extend([""] * 10)
    context = PaneContext(title="", command="opencode", lines=tuple(screen))
    adapter = OpenCodeAdapter()

    assert adapter.classify(context).status == "IDLE"
    assert "ctrl+p commands" not in "\n".join(context.lines[-8:]).lower()
    result = adapter.extract_result(context)
    assert result is not None
    assert result.text == "Synthetic complete answer."
    assert result.complete is True
    assert result.turn_complete is True
    assert result.body_complete is True


def test_opencode_working_is_not_a_result():
    assert OpenCodeAdapter().extract_result(ctx("opencode-working.txt")) is None


def test_cursor_followup_keeps_the_answer_and_drops_finished():
    result = CursorAdapter().extract_result(ctx("cursor-idle-followup.txt"))
    assert result is not None
    assert result.text == "All tests passed."


@pytest.mark.parametrize(
    ("adapter", "command", "screen"),
    [
        (ClaudeAdapter, "claude", ["❯ user request", "START_MARKER", "body worked for several cases", "END_MARKER", "✻ Cooked for 2m", "❯", "⏵⏵ auto mode on"]),
        (CursorAdapter, "cursor-agent", ["→ Add a follow-up", "START_MARKER", "manual says Add a follow-up here", "END_MARKER", "→ Add a follow-up"]),
        (OpenCodeAdapter, "opencode", ["▣ Build · model · 1.0s", "┃ prompt", "START_MARKER", "body mentions ▣ Build · model · 3.0s as text", "END_MARKER", "▣ Build · model · 2.0s", "ctrl+p commands"]),
    ],
)
def test_adapter_turn_markers_in_prose_do_not_truncate_complete_bodies(adapter, command, screen):
    result = adapter().extract_result(PaneContext("", command, tuple(screen)))
    assert result is not None
    assert result.text.startswith("START_MARKER")
    assert result.text.endswith("END_MARKER")
    assert result.turn_complete is True
    assert result.body_complete is True
    assert result.complete is True


def test_cursor_still_running_is_not_a_result():
    screen = ["All tests passed.", "→ Add a follow-up", "ctrl+c to stop"]
    assert CursorAdapter().extract_result(PaneContext(title="", command="agent", lines=tuple(screen))) is None


def test_grok_and_shell_do_not_invent_a_result():
    assert GrokAdapter().extract_result(ctx("grok-idle.txt")) is None
    assert GrokAdapter().extract_result(ctx("grok-working.txt")) is None
    assert ShellAdapter().extract_result(ctx("shell-idle.txt")) is None


def test_cline_terminal_output_stays_unsupported_without_result_boundaries(monkeypatch):
    from tmux_agent_tower.control.actions import get_result

    tower = _RecoverTower()
    tower.rows[0].update({"command": "cline", "title": "Cline", "agent": "Cline"})
    _patch_history(monkeypatch, tower, ["Cline session", "assistant-looking output", "Done"])
    monkeypatch.setattr(
        "tmux_agent_tower.control.actions.pane_title_and_command",
        lambda _pane: ("Cline", "cline"),
    )
    monkeypatch.setattr("tmux_agent_tower.control.actions.tmux_capture.pane_history_position", lambda _pane: None)

    ok, reason, payload = get_result(tower, "%9")

    assert ok and reason == ""
    assert payload["text"] == "" and payload["complete"] is False
    assert payload["reason_code"] == "AGENT_UNSUPPORTED"


def test_completion_words_in_the_wrong_place_are_not_a_result():
    assert prose_body(["Finished"]) is None
    assert prose_body(["Done"]) == "Done"
    assert prose_body(["OK"]) == "OK"
    assert prose_body(["완료"]) == "완료"
    readme = PaneContext(title="", command="codex", lines=(
        "README",
        "Status: Finished",
        "› Done please",
        "› Ask Codex to do anything",
    ))
    assert CodexAdapter().extract_result(readme) is None
    working = PaneContext(title="", command="codex", lines=(
        "The answer was complete.",
        "• Working (2s • esc to interrupt)",
        "tab to queue message",
    ))
    assert CodexAdapter().extract_result(working) is None


def test_same_result_does_not_become_ready_again_after_a_new_turn():
    tracker = ResultTracker()
    first = ResultCandidate("complete first answer", "first-answer")
    assert tracker.observe("%1", "IDLE", first).state == "ready"
    again = tracker.observe("%1", "IDLE", first)
    assert again.state == "ready"
    assert again.fingerprint == first.fingerprint
    assert tracker.observe("%1", "WORKING", None).state == "none"
    assert tracker.snapshot("%1").text == ""
    stale = tracker.observe("%1", "IDLE", first)
    assert stale.state == "none"
    assert stale.text == first.text
    assert tracker.snapshot("%1").text == first.text
    newer = ResultCandidate("A second answer that is actually new.", "second-answer")
    assert newer.fingerprint != first.fingerprint
    assert tracker.observe("%1", "IDLE", newer).state == "ready"


@pytest.mark.parametrize("shared", [False, True])
def test_full_history_recovery_restores_a_same_result_after_visible_partial_poll(tmp_path, shared):
    tracker = ResultTracker(tmp_path / "result-state.sqlite3" if shared else None)
    identity = _shared_identity() if shared else None
    body = "START_MARKER\n" + "\n".join(f"line {i:03d}" for i in range(100)) + "\nEND_MARKER"
    complete = ResultCandidate(body, "same-result")
    assert tracker.observe("%9", "IDLE", complete, identity).state == "ready"
    assert tracker.observe("%9", "WORKING", None, identity).state == "none"
    fragment = ResultCandidate("line 090\nline 091\nEND_MARKER", "fragment", confidence="partial")
    assert tracker.observe("%9", "IDLE", fragment, identity).complete is False

    recovered = tracker.observe("%9", "IDLE", complete, identity, full_recovery=True)

    assert (recovered.state, recovered.text, recovered.complete) == ("ready", body, True)
    assert recovered.turn_complete is True and recovered.body_complete is True


@pytest.mark.parametrize("shared", [False, True])
def test_visible_suffix_does_not_hide_a_cached_complete_result(tmp_path, shared):
    tracker = ResultTracker(tmp_path / "result-state.sqlite3" if shared else None)
    identity = _shared_identity() if shared else None
    body = "START_MARKER\nline one\nline two\nEND_MARKER"
    complete = ResultCandidate(body, "complete-body")
    tracker.observe("%9", "IDLE", complete, identity, full_recovery=True)
    suffix = ResultCandidate("line two\nEND_MARKER", "visible-suffix", confidence="partial")

    snapshot = tracker.observe("%9", "IDLE", suffix, identity)

    assert (snapshot.state, snapshot.text, snapshot.complete) == ("ready", body, True)


def test_view_or_copy_marks_read_and_a_mismatch_does_not():
    tracker = ResultTracker()
    found = ResultCandidate("complete answer", "complete-answer")
    snap = tracker.observe("%1", "IDLE", found)
    assert tracker.mark_read("%1", "nope") is False
    assert tracker.mark_read("%1", snap.fingerprint) is True
    assert tracker.snapshot("%1").state == "read"
    assert tracker.observe("%1", "IDLE", found).state == "read"


@pytest.mark.parametrize("shared", [False, True])
def test_partial_candidate_never_advertises_or_preserves_an_older_result(tmp_path, shared):
    from tmux_agent_tower.detection.result import RESULT_NONE, RESULT_READY

    tracker = ResultTracker(tmp_path / "result-state.sqlite3" if shared else None)
    identity = _shared_identity() if shared else None
    old = ResultCandidate("older complete answer", "old-complete")
    assert tracker.observe("%9", "IDLE", old, identity).state == RESULT_READY

    partial = ResultCandidate("new answer fragment", "new-partial", confidence="partial")
    hidden = tracker.observe("%9", "IDLE", partial, identity)
    assert (hidden.state, hidden.text, hidden.complete) == (RESULT_NONE, "", False)
    assert tracker.snapshot("%9", identity).state == RESULT_NONE
    assert tracker.mark_read("%9", old.fingerprint, identity) is False

    complete = ResultCandidate("new complete answer", "new-complete")
    ready = tracker.observe("%9", "IDLE", complete, identity)
    assert (ready.state, ready.text, ready.complete) == (RESULT_READY, complete.text, True)
    repeated = tracker.observe("%9", "IDLE", complete, identity)
    assert repeated.state == RESULT_READY
    assert repeated.generated_at == ready.generated_at


def test_partial_candidate_from_empty_tracker_is_not_new_result_and_can_complete():
    from tmux_agent_tower.detection.result import RESULT_NONE, RESULT_READY

    tracker = ResultTracker()
    partial = ResultCandidate("Cursor fragment", "cursor-fragment", confidence="partial")
    assert tracker.observe("%9", "IDLE", partial).state == RESULT_NONE
    assert tracker.snapshot("%9").state == RESULT_NONE

    complete = ResultCandidate("Cursor full answer", "cursor-full")
    assert tracker.observe("%9", "IDLE", complete).state == RESULT_READY
    assert tracker.observe("%9", "IDLE", complete).state == RESULT_READY


def test_home_live_conversation_and_group_share_complete_result_truth():
    from tmux_agent_tower.detection.result import RESULT_NONE
    from tmux_agent_tower.i18n import t
    from tmux_agent_tower.ui import control_view, live_view, render, work_groups

    tracker = ResultTracker()
    row = {"key": "%9", "target_id": "qa", "kind": "pane", "pane_id": "%9",
           "display_name": "QA", "agent": "Cursor", "role": "qa", "status": "IDLE",
           "attention": "none", "result_state": RESULT_NONE}
    group = {"group_id": "g", "display_name": "Work", "member_target_ids": ["qa"], "member_order": ["qa"]}

    partial = ResultCandidate("fragment", "fragment", confidence="partial")
    row["result_state"] = tracker.observe("%9", "IDLE", partial).state
    assert row["result_state"] == RESULT_NONE
    assert render.primary_badge(row)[1] != "state.result"
    assert live_view._result(row) == t("live.result_none")
    assert control_view._result_label(row["result_state"]) == "-"
    assert work_groups.build_rows([group], [row], set())[0]["summary_counts"]["result"] == 0

    complete = ResultCandidate("full result", "full")
    row["result_state"] = tracker.observe("%9", "IDLE", complete).state
    assert render.primary_badge(row)[1] == "state.result"
    assert live_view._result(row) == t("state.result")
    assert control_view._result_label(row["result_state"]) == t("control.result_ready")
    assert work_groups.build_rows([group], [row], set())[0]["summary_counts"]["result"] == 1


def _shared_identity(*, session="session-1", pane_pid="4101"):
    return {
        "tmux_host": "workstation-b",
        "server_scope": {
            "host": "workstation-b.local",
            "socket": "/tmp/tmux-1000/default",
            "server_pid": "901",
            "socket_device": 8,
            "socket_inode": 12345,
        },
        "session": session,
        "window_id": "@4",
        "pane_id": "%9",
        "pane_pid": pane_pid,
    }


def test_separate_trackers_share_ready_and_read_in_both_directions(tmp_path):
    state_path = tmp_path / "result-state.sqlite3"
    pc = ResultTracker(state_path)
    phone = ResultTracker(state_path)
    identity = _shared_identity()
    first = ResultCandidate("Private answer one", "fingerprint-one")

    assert pc.observe("%9", "IDLE", first, identity).state == "ready"
    visible = phone.snapshot("%9", identity)
    assert visible.state == "ready"
    assert visible.text == ""  # A different process has metadata, not the body.
    assert phone.observe("%9", "IDLE", first, identity).text == first.text
    assert phone.mark_read("%9", first.fingerprint, identity)
    assert pc.snapshot("%9", identity).state == "read"
    assert pc.observe("%9", "WORKING", None, identity).state == "none"
    assert pc.observe("%9", "IDLE", first, identity).state == "read"

    second = ResultCandidate("Private answer two", "fingerprint-two")
    assert pc.observe("%9", "IDLE", second, identity).state == "ready"
    assert phone.snapshot("%9", identity).state == "ready"
    assert pc.mark_read("%9", second.fingerprint, identity)
    assert phone.snapshot("%9", identity).state == "read"


def test_tracker_restart_rehydrates_metadata_but_reextracts_body(tmp_path):
    state_path = tmp_path / "result-state.sqlite3"
    identity = _shared_identity()
    result = ResultCandidate("Restart-only body", "restart-fingerprint")
    first = ResultTracker(state_path)
    assert first.observe("%9", "IDLE", result, identity).state == "ready"

    restarted = ResultTracker(state_path)
    from_disk = restarted.snapshot("%9", identity)
    assert from_disk.state == "ready"
    assert from_disk.fingerprint == result.fingerprint
    assert from_disk.text == ""
    recovered = restarted.observe("%9", "IDLE", result, identity)
    assert recovered.state == "ready"
    assert recovered.text == result.text

    newer = ResultCandidate("A different current answer", "new-fingerprint")
    assert restarted.observe("%9", "IDLE", newer, identity).state == "ready"
    assert first.snapshot("%9", identity).fingerprint == newer.fingerprint


def test_shared_status_poll_tracks_fingerprint_without_caching_result_body(tmp_path):
    from tmux_agent_tower.ui.tower import Tower

    class Adapter:
        def extract_result(self, _context):
            return ResultCandidate("Poll must not cache this body", "poll-fingerprint")

    tower = Tower.__new__(Tower)
    tower.results = ResultTracker(tmp_path / "result-state.sqlite3")
    identity = _shared_identity()
    state = tower._result_state("%9", "IDLE", Adapter(), None, False, identity)
    snapshot = tower.results.snapshot("%9", identity)
    assert state == "ready"
    assert snapshot.state == "ready"
    assert snapshot.text == ""


def test_shared_result_state_does_not_follow_reused_pane_identity(tmp_path):
    tracker = ResultTracker(tmp_path / "result-state.sqlite3")
    identity = _shared_identity()
    result = ResultCandidate("Old pane body", "old-pane-fingerprint")
    assert tracker.observe("%9", "IDLE", result, identity).state == "ready"

    reused_pid = _shared_identity(pane_pid="5202")
    assert tracker.snapshot("%9", reused_pid).state == "none"
    reused_session = _shared_identity(session="session-recreated")
    assert tracker.snapshot("%9", reused_session).state == "none"
    reused_server = _shared_identity()
    reused_server["server_scope"] = {**reused_server["server_scope"], "socket_inode": 67890}
    assert tracker.snapshot("%9", reused_server).state == "none"
    assert tracker.mark_read("%9", result.fingerprint, reused_pid) is False


def test_pane_identity_includes_server_generation_and_required_row_fields(tmp_path, monkeypatch):
    path = tmp_path / "tmux.sock"
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(path))
    try:
        row = {
            "tmux_host": "workstation-b",
            "session": "session-1",
            "window_id": "@4",
            "pane_id": "%9",
            "pane_pid": "4101",
        }
        monkeypatch.setenv("TMUX", f"{path},901,0")
        identity = pane_result_identity(row)
        assert identity is not None
        assert identity["pane_pid"] == "4101"
        assert identity["window_id"] == "@4"
        monkeypatch.setenv("TMUX", f"{path},901,1")
        assert pane_result_identity(row) == identity
        assert pane_result_identity({**row, "pane_pid": "5202"}) != identity
        assert pane_result_identity({key: value for key, value in row.items() if key != "window_id"}) is None
    finally:
        server.close()


def _race_shared_result_state(state_path, identity, action, barrier):
    tracker = ResultTracker(Path(state_path))
    result = ResultCandidate("Private concurrent body", "concurrent-fingerprint")
    barrier.wait(timeout=10)
    if action == "read":
        if not tracker.mark_read("%9", result.fingerprint, identity):
            raise RuntimeError("read transition was lost")
        return
    for _ in range(12):
        tracker.observe("%9", "IDLE", result, identity)


def _phone_tracker_action(state_path, identity, action, fingerprint, output):
    tracker = ResultTracker(Path(state_path))
    snapshot = tracker.snapshot("%9", identity)
    marked = tracker.mark_read("%9", fingerprint, identity) if action == "read" else False
    output.put((snapshot.state, snapshot.text, marked))


def test_phone_process_reads_ready_and_pc_process_observes_read(tmp_path):
    state_path = tmp_path / "result-state.sqlite3"
    identity = _shared_identity()
    pc = ResultTracker(state_path)
    first = ResultCandidate("Not sent to the other process", "phone-read-fingerprint")
    assert pc.observe("%9", "IDLE", first, identity).state == "ready"

    context = multiprocessing.get_context("fork")
    output = context.Queue()
    phone = context.Process(
        target=_phone_tracker_action,
        args=(str(state_path), identity, "read", first.fingerprint, output),
    )
    phone.start()
    phone.join(timeout=20)
    assert not phone.is_alive()
    assert phone.exitcode == 0
    assert output.get(timeout=2) == ("ready", "", True)
    assert pc.snapshot("%9", identity).state == "read"

    second = ResultCandidate("PC read answer", "pc-read-fingerprint")
    assert pc.observe("%9", "IDLE", second, identity).state == "ready"
    assert pc.mark_read("%9", second.fingerprint, identity)
    phone_snapshot = context.Process(
        target=_phone_tracker_action,
        args=(str(state_path), identity, "snapshot", second.fingerprint, output),
    )
    phone_snapshot.start()
    phone_snapshot.join(timeout=20)
    assert not phone_snapshot.is_alive()
    assert phone_snapshot.exitcode == 0
    assert output.get(timeout=2) == ("read", "", False)


def test_shared_tracker_serializes_concurrent_read_and_poll_without_body_bytes(tmp_path):
    state_path = tmp_path / "result-state.sqlite3"
    identity = _shared_identity()
    ResultTracker(state_path).observe(
        "%9", "IDLE", ResultCandidate("Private concurrent body", "concurrent-fingerprint"), identity
    )
    context = multiprocessing.get_context("fork")
    barrier = context.Barrier(2)
    workers = [
        context.Process(target=_race_shared_result_state, args=(str(state_path), identity, action, barrier))
        for action in ("read", "poll")
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=20)
    for worker in workers:
        assert not worker.is_alive()
        assert worker.exitcode == 0

    assert ResultTracker(state_path).snapshot("%9", identity).state == "read"
    with sqlite3.connect(state_path) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        columns = [row[1] for row in connection.execute("PRAGMA table_info(result_state)")]
        assert columns == ["schema_version", "pane_identity", "state", "fingerprint", "generated_at"]
    persisted = state_path.read_bytes()
    assert b"Private concurrent body" not in persisted
    assert b"Private answer" not in persisted
    assert b"PRIVATE PROMPT" not in persisted


def test_status_payload_exposes_state_but_not_result_text():
    class Tower:
        session = "s"

        def load(self):
            return None

        rows = [{
            "key": "%1",
            "host": "workstation-a",
            "project": "demo",
            "agent": "Codex",
            "status": "IDLE",
            "result_state": "ready",
            "result_text": "SECRET ANSWER BODY",
            "remote": False,
            "offline": False,
        }]

    payload = build_status_payload(Tower())
    encoded = json.dumps(payload)
    assert payload["panes"][0]["result_state"] == "ready"
    assert "SECRET ANSWER BODY" not in encoded


def test_phone_page_copies_the_result_not_the_live_pane():
    assert "✓ 새 결과" in PAGE_HTML
    assert "navigator.clipboard" in PAGE_HTML
    assert "결과 복사" in PAGE_HTML
    assert "결과 보기" in PAGE_HTML
    assert "최신 결과 전체를 찾지 못했습니다" in PAGE_HTML
    assert "최신 결과 전체를 이 브라우저에 복사했습니다" in PAGE_HTML
    assert "!res.body.complete || !res.body.text" in PAGE_HTML


def test_ssh_pane_copies_the_agent_on_screen_not_the_ssh_process():
    process = resolve_adapter("ssh", "Codex", "ssh workstation-b", ())
    assert process.name == "Shell"
    assert process.extract_result(ctx("codex-idle.txt")) is None
    screen = adapter_for_screen(process, "Codex", "ui-via-ssh")
    result = screen.extract_result(ctx("codex-idle.txt"))
    assert result is not None
    assert "Summary of what changed" in result.text


def test_result_capture_reaches_the_answer_above_a_long_tool_log():
    prose = "정리한 답이 여기 있습니다."
    tools = [f"• Added file{i}.txt (+1 -0)" for i in range(40)]
    screen = [prose, *tools, "Worked for 1m 1s", "› Ask Codex to do anything"]
    short = CodexAdapter().extract_result(PaneContext(title="", command="codex", lines=tuple(screen[-30:])))
    assert short is None or prose not in (short.text if short else "")
    visible = screen[-800:]
    result = CodexAdapter().extract_result(PaneContext(title="", command="codex", lines=tuple(visible)))
    assert result is not None
    assert prose in result.text
    assert "Added" not in result.text


def _codex_screen(*body: str) -> tuple:
    return (*body, "› Ask Codex to do anything")


def test_newest_turn_is_the_only_recovered_body():
    screen = _codex_screen(
        "› Done",
        "older answer that must stay in history",
        "Worked for 10s",
        "› Ask Codex to do anything",
        "GPT-6-Luna medium · local project · Previous task",
        "← for agents · ? for shortcuts",
        "› next question",
        "한글 최종 답변입니다.",
        "```python",
        "print(1)",
        "```",
        "• Added notes.txt (+1 -0)",
        "12 +<div>",
        "Worked for 3s",
    )
    result = CodexAdapter().extract_result(PaneContext(title="", command="codex", lines=screen))
    assert result is not None
    assert "한글 최종 답변입니다." in result.text
    assert "print(1)" in result.text
    assert "older answer" not in result.text
    assert "Added" not in result.text
    assert "+<div>" not in result.text
    assert result.text.strip() != "Done"


def test_a_working_turn_hides_the_previous_result():
    screen = (
        "older answer that must stay in history",
        "Worked for 10s",
        "› next question",
        "working (esc to interrupt)",
    )
    assert CodexAdapter().extract_result(PaneContext(title="", command="codex", lines=screen)) is None


def test_an_old_working_line_does_not_hide_a_finished_turn():
    screen = _codex_screen(
        "working (esc to interrupt)",
        "› first",
        "older answer that must stay in history",
        "Worked for 10s",
        "› Ask Codex to do anything",
        "GPT-6-Luna medium · local project · Previous task",
        "← for agents · ? for shortcuts",
        "› second",
        "second turn answer is this paragraph",
        "Worked for 2s",
    )
    result = CodexAdapter().extract_result(PaneContext(title="", command="codex", lines=screen))
    assert result is not None
    assert result.text == "second turn answer is this paragraph"


def test_approval_widget_and_readme_are_not_a_result():
    approval = (
        "older answer that must stay in history",
        "Worked for 10s",
        "Allow this command to run?",
        "1. Yes",
        "2. No",
    )
    assert CodexAdapter().extract_result(PaneContext(title="", command="codex", lines=approval)) is None
    readme = ("# Tower", "This README explains the project in detail.", "Done")
    assert CodexAdapter().extract_result(PaneContext(title="", command="codex", lines=readme)) is None


def test_scrollback_without_a_turn_boundary_is_not_the_whole_history():
    old = [f"older paragraph number {i} stays out of the copy" for i in range(80)]
    screen = _codex_screen(*old, "recent final answer is here", "Worked for 1s")
    result = CodexAdapter().extract_result(PaneContext(title="", command="codex", lines=screen))
    assert result is not None
    assert "recent final answer is here" in result.text
    assert "older paragraph number 0" not in result.text
    assert result.complete is False


def test_polling_does_not_read_recovery_history():
    root = Path(__file__).resolve().parents[1]
    tower = (root / "src/tmux_agent_tower/ui/tower.py").read_text(encoding="utf-8")
    assert "CAPTURE_LINES = 30" in tower
    assert "RECOVERY_LINES" not in tower
    assert CAPTURE_LINES == 30


class _RecoverTower:
    def __init__(self, status="IDLE"):
        self.session = "s"
        self.own_pane_id = "%0"
        self.results = ResultTracker()
        self.rows = [{"key": "%9", "status": status, "remote": False}]
        self.captures = []
        self.capture_options = []

    def load(self):
        return None


def _patch_history(monkeypatch, tower, history):
    def run_tmux(args, capture=True, timeout=3):
        if args[:2] == ["list-panes", "-s"]:
            return "%9\n"
        return "Codex\x1fssh\n"

    def capture_pane(pane_id, lines=30, **kwargs):
        tower.captures.append(lines)
        tower.capture_options.append(kwargs)
        return list(history)

    monkeypatch.setattr("tmux_agent_tower.control.actions.tmux_capture.run_tmux", run_tmux)
    monkeypatch.setattr("tmux_agent_tower.control.actions.tmux_capture.pane_exists", lambda pane_id: True)
    monkeypatch.setattr("tmux_agent_tower.control.actions.tmux_capture.capture_pane", capture_pane)


def test_y_recovers_all_300_lines_beyond_the_visible_30(monkeypatch):
    from tmux_agent_tower.control.actions import RECOVERY_LINES, get_result

    body = [f"결과 줄 {index:03d} — 전체 본문" for index in range(300)]
    history = ["› 긴 결과를 작성해 주세요", *body, "Worked for 3m 2s", "› Ask Codex to do anything"]
    visible = history[-30:]
    assert not any(line == body[0] for line in visible)
    tower = _RecoverTower()
    _patch_history(monkeypatch, tower, history)

    ok, reason, payload = get_result(tower, "%9")

    assert ok and reason == ""
    assert payload["complete"] is True
    assert payload["text"] == "\n".join(body)
    assert len(payload["text"].splitlines()) == 300
    assert tower.captures == [RECOVERY_LINES]
    assert tower.capture_options == [{"join_wrapped": True}]


def test_tower_turn_watermark_limits_recovery_to_history_since_send(monkeypatch):
    from tmux_agent_tower.control.actions import get_result

    identity = {
        "tmux_host": "host", "server_scope": {"socket": "/tmp/tmux-test"},
        "session": "0", "window_id": "@1", "pane_id": "%9", "pane_pid": "1234",
    }
    body = [f"turn line {index:03d}" for index in range(100)]
    history = ["› Tower submitted prompt", *body, "Worked for 2m", "› Ask Codex to do anything"]
    tower = _RecoverTower()
    tower.results.record_turn_start("%9", (20, 200), identity)
    _patch_history(monkeypatch, tower, history)
    monkeypatch.setattr("tmux_agent_tower.control.actions.pane_result_identity", lambda _row: identity)
    monkeypatch.setattr("tmux_agent_tower.control.actions.tmux_capture.pane_history_position", lambda _pane: (125, 200))

    ok, _reason, payload = get_result(tower, "%9")

    assert ok and payload["complete"] is True
    assert payload["turn_complete"] is True and payload["body_complete"] is True
    assert payload["text"] == "\n".join(body)
    assert tower.captures == [105]
    assert tower.capture_options == [{"join_wrapped": True}]


def test_partial_poll_is_replaced_by_complete_history_recovery(monkeypatch):
    from tmux_agent_tower.adapters.base import ResultCandidate
    from tmux_agent_tower.control.actions import get_result

    body = [f"line {index:03d}" for index in range(300)]
    history = ["› long prompt", *body, "Worked for 2m", "› Ask Codex to do anything"]
    tower = _RecoverTower()
    partial = ResultCandidate("line 270\nline 271", "partial", confidence="partial")
    assert partial.complete is False
    assert tower.results.observe("%9", "IDLE", partial).complete is False
    _patch_history(monkeypatch, tower, history)

    ok, _reason, payload = get_result(tower, "%9")

    assert ok and payload["complete"] is True
    assert payload["text"] == "\n".join(body)
    assert tower.results.snapshot("%9").complete is True


def test_cursor_partial_history_is_rejected_by_y_and_suppresses_old_result(monkeypatch):
    from tmux_agent_tower.control.actions import get_result

    tower = _RecoverTower()
    tower.results.observe("%9", "IDLE", ResultCandidate("older answer", "older"))
    tower.rows[0].update({"pane_id": "%9", "command": "cursor-agent", "title": "Cursor"})
    screen = ["new Cursor fragment", "→ Add a follow-up"]

    monkeypatch.setattr("tmux_agent_tower.control.actions.tmux_capture.run_tmux", lambda *_a, **_k: "%9\n")
    monkeypatch.setattr("tmux_agent_tower.control.actions.tmux_capture.pane_exists", lambda _pane: True)
    monkeypatch.setattr(
        "tmux_agent_tower.control.actions.pane_title_and_command",
        lambda _pane: ("Cursor", "cursor-agent"),
    )
    monkeypatch.setattr("tmux_agent_tower.control.actions.tmux_capture.capture_pane", lambda *_a, **_k: screen)

    ok, reason, payload = get_result(tower, "%9")

    assert ok and reason == ""
    assert payload["state"] == "none"
    assert payload["text"] == ""
    assert payload["complete"] is False
    assert tower.results.snapshot("%9").state == "none"


def test_complete_result_identity_rebind_survives_same_pane_window_move(tmp_path):
    from tmux_agent_tower.detection.result import RESULT_NONE, RESULT_READY

    tracker = ResultTracker(tmp_path / "result-state.sqlite3")
    old_identity = {
        "tmux_host": "host", "server_scope": {"socket": "/tmp/tmux-test"},
        "session": "0", "window_id": "@1", "pane_id": "%9", "pane_pid": "1234",
    }
    new_identity = {**old_identity, "window_id": "@2"}
    tracker.observe("%9", "IDLE", ResultCandidate("완료 결과", "sha256:complete", "high", True), identity=old_identity)
    assert tracker.snapshot("%9", old_identity).state == RESULT_READY

    assert tracker.rebind_identity("%9", old_identity, new_identity)
    moved = tracker.snapshot("%9", new_identity)
    assert (moved.state, moved.text, moved.fingerprint, moved.complete) == (
        RESULT_READY, "완료 결과", "sha256:complete", True,
    )
    assert tracker.snapshot("%9", old_identity).state == RESULT_NONE


def test_result_identity_rebind_refuses_to_overwrite_another_ready_result(tmp_path):
    tracker = ResultTracker(tmp_path / "result-state.sqlite3")
    old_identity = {
        "tmux_host": "host", "server_scope": {"socket": "/tmp/tmux-test"},
        "session": "0", "window_id": "@1", "pane_id": "%9", "pane_pid": "1234",
    }
    new_identity = {**old_identity, "window_id": "@2"}
    tracker.observe("%9", "IDLE", ResultCandidate("old", "hash-old", "high", True), identity=old_identity)
    other = ResultTracker(tmp_path / "result-state.sqlite3")
    other.observe("%9", "IDLE", ResultCandidate("other", "hash-other", "high", True), identity=new_identity)
    assert not tracker.rebind_identity("%9", old_identity, new_identity)
    assert tracker.snapshot("%9", old_identity).fingerprint == "hash-old"


def test_unbounded_candidate_is_not_a_y_payload(monkeypatch):
    from tmux_agent_tower.control.actions import get_result

    tower = _RecoverTower()
    _patch_history(monkeypatch, tower, ["answer fragment", "Worked for 2s", "› Ask Codex to do anything"])
    ok, _reason, payload = get_result(tower, "%9")
    assert ok
    assert payload["complete"] is False
    assert payload["turn_complete"] is True
    assert payload["body_complete"] is False
    assert payload["text"] == ""


def test_agent_specific_binding_does_not_mislabel_local_history(monkeypatch):
    from tmux_agent_tower.control.actions import get_result

    body = [f"agent source line {index:03d}" for index in range(300)]
    history = ["› return the result", *body, "Worked for 2m", "› Ask Codex to do anything"]
    tower = _RecoverTower()
    tower.rows[0].update(result_provider_type="agent_specific", transport="ssh")
    _patch_history(monkeypatch, tower, history)

    ok, _reason, payload = get_result(tower, "%9")

    assert ok and payload["complete"] is True
    assert payload["source"] == "ssh_visible_history"
    assert payload["text"] == "\n".join(body)


def test_remote_result_provider_returns_the_normalized_complete_candidate(monkeypatch):
    from tmux_agent_tower.control.actions import get_result

    body = [f"원격 결과 {index:03d}" for index in range(300)]
    history = ["› user prompt", *body, "Worked for 2m", "› Ask Codex to do anything"]
    calls = []
    monkeypatch.setattr(
        "tmux_agent_tower.remote.collector.fetch_remote_history",
        lambda host, pane, pid, **kwargs: calls.append((host, pane, pid, kwargs)) or history,
    )
    tower = _RecoverTower()
    tower.rows = [{
        "key": "workstation-b:%9", "remote": True, "result_provider_host": "workstation-b",
        "pane_id": "%9", "pane_pid": "123", "session_id": "$9",
        "result_provider_type": "remote_tmux", "result_provider_endpoint": "workstation-b",
        "result_provider_session_id": "$9", "result_provider_pane_id": "%9",
        "result_provider_pane_pid": "123", "result_provider_liveness": "live",
        "command": "codex", "pane_title": "Codex",
    }]

    ok, reason, payload = get_result(tower, "workstation-b:%9")

    assert ok and reason == ""
    assert payload["complete"] is True
    assert payload["text"] == "\n".join(body)
    assert payload["source"] == "remote_tmux"
    assert payload["turn_identity"].startswith("workstation-b:$9:%9:123:")
    assert payload["timestamp"] > 0
    assert calls == [("workstation-b", "%9", "123", {"lines": 800, "session_id": "$9", "join_wrapped": True})]


@pytest.mark.parametrize(
    "payload",
    [
        {"complete": True, "text": "unbounded suffix"},
        {"complete": True, "turn_complete": True, "body_complete": False, "text": "partial"},
        {"complete": True, "turn_complete": False, "body_complete": True, "text": "not finished"},
    ],
)
def test_result_provider_requires_explicit_turn_and_body_completeness(payload):
    from tmux_agent_tower.control.actions import _RecoveryProvider
    from tmux_agent_tower.detection.providers import ResultTarget

    provider = _RecoveryProvider(lambda _target: payload)

    assert provider.get_latest_complete_result(ResultTarget.from_row({"key": "%9"})) is None


def test_remote_provider_partial_candidate_stays_out_of_y_payload(monkeypatch):
    from tmux_agent_tower.control.actions import get_result

    monkeypatch.setattr(
        "tmux_agent_tower.remote.collector.fetch_remote_history",
        lambda *_args, **_kwargs: ["partial remote tail", "Worked for 2s", "› Ask Codex to do anything"],
    )
    tower = _RecoverTower()
    tower.rows = [{
        "key": "workstation-b:%9", "remote": True, "result_provider_host": "workstation-b",
        "pane_id": "%9", "pane_pid": "123", "session_id": "$9",
        "result_provider_type": "remote_tmux", "result_provider_endpoint": "workstation-b",
        "result_provider_session_id": "$9", "result_provider_pane_id": "%9",
        "result_provider_pane_pid": "123", "result_provider_liveness": "live",
        "command": "codex", "pane_title": "Codex",
    }]

    ok, _reason, payload = get_result(tower, "workstation-b:%9")

    assert ok
    assert payload["complete"] is False
    assert payload["text"] == ""


def test_stale_remote_provider_is_not_called(monkeypatch):
    from tmux_agent_tower.control.actions import get_result

    monkeypatch.setattr(
        "tmux_agent_tower.remote.collector.fetch_remote_history",
        lambda *_args, **_kwargs: pytest.fail("stale provider must not be queried"),
    )
    tower = _RecoverTower()
    tower.rows = [{
        "key": "workstation-b:%9", "remote": True,
        "result_provider_type": "remote_tmux", "result_provider_endpoint": "workstation-b",
        "result_provider_session_id": "$9", "result_provider_pane_id": "%9",
        "result_provider_pane_pid": "123", "result_provider_liveness": "stale",
        "pane_id": "%9", "pane_pid": "123", "session_id": "$9",
        "command": "codex", "pane_title": "Codex",
    }]

    ok, _reason, payload = get_result(tower, "workstation-b:%9")

    assert ok and payload["complete"] is False and payload["text"] == ""


def test_truncated_local_ssh_history_falls_back_to_registered_remote_provider(monkeypatch):
    from tmux_agent_tower.control.actions import get_result

    body = [f"remote full line {index:03d}" for index in range(300)]
    remote_history = ["› remote prompt", *body, "Worked for 3m", "› Ask Codex to do anything"]
    monkeypatch.setattr(
        "tmux_agent_tower.remote.collector.fetch_remote_history",
        lambda host, pane, pid, **kwargs: remote_history if (host, pane, pid) == ("workstation-b", "%4", "444") else None,
    )
    tower = _RecoverTower("IDLE")
    tower.rows[0].update(
        result_provider_type="remote_tmux",
        result_provider_endpoint="workstation-b",
        result_provider_session_id="$4",
        result_provider_pane_id="%4",
        result_provider_pane_pid="444",
        result_provider_liveness="registered",
    )
    _patch_history(monkeypatch, tower, ["truncated local tail", "Worked for 3m", "› Ask Codex to do anything"])

    ok, _reason, payload = get_result(tower, "%9")

    assert ok and payload["complete"] is True
    assert payload["text"] == "\n".join(body)


def test_empty_tracker_recovers_a_visible_final_and_a_buried_one(monkeypatch):
    from tmux_agent_tower.control.actions import RECOVERY_LINES, get_result

    visible = ("› user prompt", *_codex_screen("visible final answer here", "Worked for 1s"))
    tower = _RecoverTower()
    _patch_history(monkeypatch, tower, visible)
    ok, reason, payload = get_result(tower, "%9")
    assert ok and reason == ""
    assert "visible final answer here" in payload["text"]
    assert tower.captures == [RECOVERY_LINES]

    buried = ["noise"] * 40
    buried += [
        "Worked for 2s", "› Ask Codex to do anything",
        "GPT-6-Luna medium · local project · Previous task",
        "← for agents · ? for shortcuts", "› question",
        "buried final answer here", "Worked for 4s", "› Ask Codex to do anything",
    ]
    tower = _RecoverTower()
    _patch_history(monkeypatch, tower, buried)
    ok, _reason, payload = get_result(tower, "%9")
    assert "buried final answer here" in payload["text"]
    assert "noise" not in payload["text"]


def test_a_live_working_screen_does_not_restore_the_old_result(monkeypatch):
    from tmux_agent_tower.control.actions import RECOVERY_LINES, get_result

    history = [
        "stale answer that is still in history",
        "Worked for 1s",
        "› Ask Codex to do anything",
        *["."] * 20,
        "Working (1m • esc to interrupt)",
    ]
    tower = _RecoverTower("WORKING")
    _patch_history(monkeypatch, tower, history)
    ok, _reason, payload = get_result(tower, "%9")
    assert ok
    assert payload["text"] == ""
    assert tower.captures == [RECOVERY_LINES]


def test_false_working_row_still_recovers_an_idle_screen(monkeypatch):
    from tmux_agent_tower.control.actions import get_result

    history = ("› user prompt", *_codex_screen("visible final answer here", "Worked for 1s"))
    tower = _RecoverTower("WORKING")
    _patch_history(monkeypatch, tower, history)
    ok, _reason, payload = get_result(tower, "%9")
    assert ok
    assert "visible final answer here" in payload["text"]


def test_recovery_is_bounded_and_keeps_a_result_the_tracker_already_has(monkeypatch):
    from tmux_agent_tower.adapters.base import ResultCandidate
    from tmux_agent_tower.control.actions import RECOVERY_BYTES, RECOVERY_LINES, _bounded_history, get_result

    oldest = "OLDEST_MARKER_TEXT"
    lines = [oldest] + ["x" * 8000 for _ in range(40)]
    kept = _bounded_history(lines)
    assert oldest not in kept
    assert len(kept) <= RECOVERY_LINES
    assert len("\n".join(kept).encode("utf-8")) <= RECOVERY_BYTES

    tower = _RecoverTower()
    tower.results.observe(
        "%9",
        "IDLE",
        ResultCandidate("exact files fragment only", "frag", confidence="partial"),
    )
    history = (
        "› the question that started this turn",
        "first half of the finished answer stays",
        "second half of the finished answer stays",
        "Worked for 1s",
        "› Ask Codex to do anything",
    )
    _patch_history(monkeypatch, tower, history)
    _ok, _reason, payload = get_result(tower, "%9")
    assert "first half of the finished answer stays" in payload["text"]
    assert "second half of the finished answer stays" in payload["text"]
    assert "exact files fragment" not in payload["text"]
    assert "the question that started" not in payload["text"]
    assert tower.captures == [RECOVERY_LINES]


def test_codex_idle_widget_beats_an_older_working_line_and_a_title_spinner():
    screen = (
        "Working (1m • esc to interrupt)",
        *["older line"] * 20,
        "The finished answer is this sentence.",
        "Worked for 2s",
        "› Ask Codex to do anything",
        "← for agents · ? for shortcuts",
    )
    ctx = PaneContext(title="⠴ Codex", command="ssh", lines=screen)
    assert CodexAdapter().classify(ctx).status == "IDLE"
    assert CodexAdapter().classify(ctx).reason == "idle-widget"
    result = CodexAdapter().extract_result(ctx)
    assert result is not None
    assert "finished answer" in result.text


def test_idle_footer_beats_an_older_active_verb():
    from tmux_agent_tower.detection.topology import resolve_runtime

    screen = (
        "* Synthesizing… (1m 2s · ↓ 1k tokens)",
        "Short answer for the test.",
        "❯ next",
        "⏵⏵ auto mode on (shift+tab to cycle)",
    )
    assert ClaudeAdapter().classify(PaneContext(title="", command="ssh", lines=screen)).status == "IDLE"
    name, source, adapter = resolve_runtime("ssh", "task", "", screen)
    assert (name, source, adapter.name) == ("Claude", "ui-via-ssh", "Claude")
    running = screen[:-1] + ("* Synthesizing… (1m 2s · ↓ 1k tokens)",)
    assert ClaudeAdapter().classify(PaneContext(title="", command="ssh", lines=running)).status == "WORKING"


def test_claude_sautéed_line_stays_claude_when_the_footer_is_clipped():
    from tmux_agent_tower.detection.topology import resolve_runtime

    screen = (
        "READY_FOR_FOUNDER",
        "✻ Sautéed for 1m 41s · done",
        "오전 10:50",
        "❯ /copy",
        "Copied to clipboard",
        "❯",
        "⏵⏵ auto mode on",
    )
    name, source, adapter = resolve_runtime("ssh", "WBS-10", "", screen)
    assert (name, source, adapter.name) == ("Claude", "ui-via-ssh", "Claude")
    result = adapter.extract_result(PaneContext(title="WBS-10", command="ssh", lines=screen))
    assert result is not None
    assert result.text == "READY_FOR_FOUNDER"
    assert "/copy" not in result.text
    assert "clipboard" not in result.text


def test_claude_worked_verb_stays_claude_on_an_ssh_pane():
    from tmux_agent_tower.detection.topology import resolve_runtime

    screen = (
        "Short answer for the test.",
        "✻ Worked for 5s",
        "❯",
        "⏵⏵ auto mode on (shift+tab to cycle) · ← 0 agents",
    )
    name, source, adapter = resolve_runtime("ssh", "task", "", screen)
    assert (name, source, adapter.name) == ("Claude", "ui-via-ssh", "Claude")
    result = adapter.extract_result(PaneContext(title="task", command="ssh", lines=screen))
    assert result is not None
    assert result.text.startswith("Short answer")


def test_codex_prompt_still_wins_over_the_shared_shortcut_footer():
    from tmux_agent_tower.detection.topology import resolve_runtime

    screen = (
        "The finished answer is this sentence.",
        "Worked for 2s",
        "› Ask Codex to do anything",
        "← for agents · ? for shortcuts",
    )
    name, source, adapter = resolve_runtime("ssh", "task", "", screen)
    assert (name, source, adapter.name) == ("Codex", "ui-via-ssh", "Codex")


def test_python_process_is_not_relabeled_from_screen_text():
    from tmux_agent_tower.detection.topology import resolve_runtime

    screen = ("❯ sample", "⏵⏵ auto mode on (shift+tab to cycle)")
    name, source, adapter = resolve_runtime("python3", "", "", screen)
    assert name == "Shell"
    assert adapter.name == "Shell"


def test_tower_glyphs_without_a_done_line_stay_a_shell():
    from tmux_agent_tower.detection.topology import resolve_runtime

    screen = ("❯ sample project", "⏵⏵ waiting")
    name, source, adapter = resolve_runtime("python3", "", "", screen)
    assert name == "Shell"
    assert adapter.name == "Shell"


def test_claude_brewed_line_is_idle_even_without_english_shortcuts():
    from tmux_agent_tower.detection.topology import resolve_runtime

    screen = (
        "noise above the widget",
        "❯ question",
        "Short answer for the test.",
        "✻ Brewed for 5s",
        "❯ next",
        "⏵⏵ bypass · ← 0 agents",
    )
    name, source, adapter = resolve_runtime("ssh", "task", "", screen)
    assert (name, source, adapter.name) == ("Claude", "ui-via-ssh", "Claude")
    ctx = PaneContext(title="task", command="ssh", lines=screen)
    assert adapter.classify(ctx).status == "IDLE"
    result = adapter.extract_result(ctx)
    assert result is not None
    assert result.text.startswith("Short answer")


def test_claude_structural_footer_is_idle_with_a_result():
    screen = (
        "Short answer for the test.",
        "✻ Cooked for 2s",
        "❯",
        "⏵⏵ bypass",
    )
    ctx = PaneContext(title="task", command="ssh", lines=screen)
    assert ClaudeAdapter().classify(ctx).status == "IDLE"
    result = ClaudeAdapter().extract_result(ctx)
    assert result is not None
    assert result.text.startswith("Short answer")


def test_claude_diamond_elapsed_line_marks_a_finished_result():
    screen = (
        "Visible answer stays on screen.",
        "◇ Lookup… 111.8k",
        "❯ Try \"help\"",
        "? for shortcuts",
    )
    ctx = PaneContext(title="", command="ssh", lines=screen)
    assert ClaudeAdapter().classify(ctx).status == "IDLE"
    result = ClaudeAdapter().extract_result(ctx)
    assert result is not None
    assert "Visible answer" in result.text


def test_ssh_pane_uses_the_claude_adapter_for_recovery(monkeypatch):
    from tmux_agent_tower.control.actions import _attention_now, get_result
    from tmux_agent_tower.detection.topology import resolve_runtime

    screen = [
        "❯ user prompt",
        "Recovered answer from the ssh pane.",
        "✻ Cooked for 2s",
        "❯",
        "⏵⏵ bypass",
    ]
    name, source, adapter = resolve_runtime("ssh", "task", "", screen)
    assert (name, source, adapter.name) == ("Claude", "ui-via-ssh", "Claude")

    def run_tmux(args, capture=True, timeout=3):
        if args[:2] == ["list-panes", "-s"]:
            return "%9\n"
        return "task\x1fssh\n"

    def capture_pane(pane_id, lines=30, **_kwargs):
        return list(screen)

    monkeypatch.setattr("tmux_agent_tower.control.actions.tmux_capture.run_tmux", run_tmux)
    monkeypatch.setattr("tmux_agent_tower.control.actions.tmux_capture.pane_exists", lambda pane_id: True)
    monkeypatch.setattr("tmux_agent_tower.control.actions.tmux_capture.capture_pane", capture_pane)
    tower = _RecoverTower("IDLE")
    ok, _reason, payload = get_result(tower, "%9")
    assert ok
    assert "Recovered answer" in payload["text"]
    attention = _attention_now("%9")
    assert attention is not None
    assert attention[0].name == adapter.name


def test_result_recovery_does_not_restore_old_answer_over_codex_trust_widget(monkeypatch):
    from tmux_agent_tower.control.actions import get_result

    history = [
        "The old answer stays here.",
        "Worked for 2s",
        "› Ask Codex to do anything",
        "Trust this folder?",
        "1. Trust this folder",
        "2. No, exit",
    ]
    tower = _RecoverTower("IDLE")
    _patch_history(monkeypatch, tower, history)
    ok, _reason, payload = get_result(tower, "%9")
    assert ok
    assert payload["state"] == "none"
    assert payload["text"] == ""


def test_cursor_followup_chrome_does_not_hide_the_answer():
    combined = ["The finished answer is here.", "→ Add a follow-up  ctrl+c to stop"]
    result = CursorAdapter().extract_result(PaneContext(title="", command="agent", lines=tuple(combined)))
    assert result is not None
    assert "finished answer" in result.text
    assert CursorAdapter().classify(PaneContext(title="", command="agent", lines=tuple(combined))).status == "IDLE"

    running = ["The finished answer is here.", "→ Add a follow-up", "ctrl+c to stop"]
    assert CursorAdapter().extract_result(PaneContext(title="", command="agent", lines=tuple(running))) is None


def test_cursor_spinner_working_fixture_is_not_idle_or_a_result():
    ctx = PaneContext(title="", command="cursor-agent", lines=lines("cursor-working-spinner.txt"))
    assert CursorAdapter().classify(ctx).status == "WORKING"
    assert CursorAdapter().extract_result(ctx) is None


def test_cursor_plan_prompt_fixture_is_idle():
    ctx = PaneContext(title="", command="cursor-agent", lines=lines("cursor-idle-plan.txt"))
    assert CursorAdapter().classify(ctx).status == "IDLE"
    assert CursorAdapter().extract_result(ctx) is None


def test_cursor_running_token_footer_is_not_a_finished_result():
    """The generating footer sits above a follow-up line that also shows stop."""

    screen = (
        "The answer is still being written.",
        "⠘⠤ Running  4.99k tokens",
        "→ Add a follow-up           ctrl+c to stop",
        "Auto Balance · 64.1%        Run Everything",
    )
    ctx = PaneContext(title="", command="cursor-agent", lines=screen)
    assert CursorAdapter().classify(ctx).status == "WORKING"
    assert CursorAdapter().extract_result(ctx) is None

    other_count = screen[1].replace("4.99k", "12.47k")
    still = PaneContext(title="", command="cursor-agent", lines=(screen[0], other_count, screen[2], screen[3]))
    assert CursorAdapter().extract_result(still) is None

    buried = ["The real answer is above the tool log.", *["Finished read"] * 70, "→ Add a follow-up"]
    buried_result = CursorAdapter().extract_result(PaneContext(title="", command="agent", lines=tuple(buried)))
    assert buried_result is not None
    assert "real answer" in buried_result.text


def test_y_copies_nothing_while_cursor_shows_the_running_token_footer(monkeypatch):
    from tmux_agent_tower.control.actions import get_result

    screen = [
        "The answer is still being written.",
        "Running  12.47k tokens",
        "→ Add a follow-up           ctrl+c to stop",
    ]

    def run_tmux(args, capture=True, timeout=3):
        if args[:2] == ["list-panes", "-s"]:
            return "%9\n"
        if args[:2] == ["display-message", "-p"]:
            return "task\x1fcursor-agent"
        return ""

    monkeypatch.setattr("tmux_agent_tower.control.actions.tmux_capture.run_tmux", run_tmux)
    monkeypatch.setattr("tmux_agent_tower.control.actions.tmux_capture.pane_exists", lambda pane_id: True)
    monkeypatch.setattr(
        "tmux_agent_tower.control.actions.tmux_capture.capture_pane",
        lambda pane_id, lines=30, **_kwargs: list(screen),
    )
    ok, _reason, payload = get_result(_RecoverTower("IDLE"), "%9")
    assert ok is True
    assert payload["text"] == ""


def test_y_reads_the_selected_pane_not_the_rest_of_the_window(monkeypatch):
    from tmux_agent_tower.control.actions import get_result

    calls = []

    def run_tmux(args, capture=True, timeout=3):
        calls.append(list(args))
        if args[:2] == ["list-panes", "-s"]:
            return "%9\n"
        if args[:2] == ["display-message", "-p"]:
            return "task\x1fssh"
        return "other\x1fssh\nnoise\x1fpython3"

    screen = [
        "❯ user prompt",
        "Recovered answer from the selected pane.",
        "✻ Cooked for 2s",
        "❯",
        "⏵⏵ bypass",
    ]
    monkeypatch.setattr("tmux_agent_tower.control.actions.tmux_capture.run_tmux", run_tmux)
    monkeypatch.setattr("tmux_agent_tower.control.actions.tmux_capture.pane_exists", lambda pane_id: True)
    monkeypatch.setattr(
        "tmux_agent_tower.control.actions.tmux_capture.capture_pane",
        lambda pane_id, lines=30, **_kwargs: list(screen),
    )
    tower = _RecoverTower("IDLE")
    ok, _reason, payload = get_result(tower, "%9")
    assert ok
    assert "selected pane" in payload["text"]
    assert any(call[:2] == ["display-message", "-p"] and call[3] == "%9" for call in calls)
    assert not any(call[:2] == ["list-panes", "-t"] for call in calls)


def test_y_copies_the_newest_turn_not_a_stored_fragment_or_a_working_screen(monkeypatch):
    from tmux_agent_tower.adapters.base import ResultCandidate
    from tmux_agent_tower.control.actions import get_result

    history = (
        "› first question",
        "Result A stays in history only.",
        "Worked for 10s",
        "› Ask Codex to do anything",
        "GPT-6-Luna medium · local project · Previous task",
        "← for agents · ? for shortcuts",
        "› second question",
        "Result B is the finished answer.",
        "```python",
        "print(1)",
        "```",
        "• Added notes.txt (+1 -0)",
        "Worked for 3s",
        "› Ask Codex to do anything",
    )
    tower = _RecoverTower("IDLE")
    tower.results.observe("%9", "IDLE", ResultCandidate("Result A stays in history only.", "old"))
    _patch_history(monkeypatch, tower, history)
    ok, _reason, payload = get_result(tower, "%9")
    assert ok
    assert payload["text"] == "Result B is the finished answer.\n```python\nprint(1)\n```"
    assert "Result A" not in payload["text"]
    assert "Added" not in payload["text"]

    working = history + ("Working (1m • esc to interrupt)",)
    tower = _RecoverTower("IDLE")
    tower.results.observe("%9", "IDLE", ResultCandidate("Result B is the finished answer.", "kept"))
    _patch_history(monkeypatch, tower, working)
    ok, _reason, payload = get_result(tower, "%9")
    assert ok
    assert payload["text"] == ""


@pytest.mark.parametrize("overflow", ["lines", "bytes"])
def test_codex_range_truncation_does_not_promote_example_tail_to_complete(monkeypatch, overflow):
    from tmux_agent_tower.control.actions import RECOVERY_BYTES, RECOVERY_LINES, get_result

    if overflow == "lines":
        history = ["› actual user request", "required answer beginning"]
        history += ["filler"] * (RECOVERY_LINES - 6)
        history += [
            "Worked for 5s", "› example next command", "required answer ending",
            "Worked for 1s", "› Ask Codex to do anything",
        ]
        assert len(history) == RECOVERY_LINES + 1
    else:
        history = [
            "› actual user request", "x" * RECOVERY_BYTES, "Worked for 5s",
            "› example next command", "required answer ending", "Worked for 1s",
            "› Ask Codex to do anything",
        ]

    tower = _RecoverTower()
    _patch_history(monkeypatch, tower, history)

    ok, _reason, payload = get_result(tower, "%9")

    assert ok and payload["complete"] is False
    assert payload["reason_code"] == "OUTPUT_RANGE_EXCEEDED"
    assert payload["text"] == ""


def test_get_result_rejects_unknown_status_between_codex_turns(monkeypatch):
    from tmux_agent_tower.control.actions import get_result

    history = [
        "› first user question",
        "FIRST TURN ANSWER",
        "Worked for 5s",
        "› Ask Codex to do anything",
        "Updated status: context 62%",
        "› second user question",
        "SECOND TURN ANSWER",
        "Worked for 1s",
        "› Ask Codex to do anything",
    ]
    tower = _RecoverTower()
    _patch_history(monkeypatch, tower, history)

    ok, _reason, payload = get_result(tower, "%9")

    assert ok
    assert payload["complete"] is False
    assert payload["body_complete"] is False
    assert payload["reason_code"] == "SOURCE_PARTIAL"
    assert payload["text"] == ""


def test_a_poll_fragment_suppresses_an_older_result_until_completion():
    from tmux_agent_tower.adapters.base import ResultCandidate

    tracker = ResultTracker()
    full = ResultCandidate("first half stays\nsecond half stays", "full", confidence="high")
    fragment = ResultCandidate("second half stays", "part", confidence="partial")
    assert fragment.complete is False
    assert tracker.observe("%9", "IDLE", full).text == full.text
    partial = tracker.observe("%9", "IDLE", fragment)
    assert (partial.state, partial.text, partial.complete) == ("none", "", False)
    assert tracker.snapshot("%9").state == "none"


def test_codex_question_is_input_required_and_not_an_execution_state():
    ctx = PaneContext(
        title="",
        command="codex",
        lines=("Which file should change?",),
    )
    assert CodexAdapter().classify(ctx).status is None
    assert CodexAdapter().detect_attention(ctx) == "input_required"


def _ssh_runtime(screen, title="task"):
    from tmux_agent_tower.detection.topology import resolve_runtime

    lines = tuple(screen)
    name, source, adapter = resolve_runtime("ssh", title, "", lines)
    context = PaneContext(title=title, command="ssh", lines=lines)
    return name, source, adapter, context


def test_ssh_screens_share_one_adapter_for_status_attention_and_result():
    cases = {
        "Codex": (
            "The finished answer is this sentence.",
            "Worked for 2s",
            "› Ask Codex to do anything",
        ),
        "Claude": (
            "Short answer for the test.",
            "✻ Cooked for 5s",
            "❯",
            "⏵⏵ auto mode on (shift+tab to cycle)",
        ),
        "Cursor": (
            "The patch is applied.",
            "→ Add a follow-up",
        ),
        "OpenCode": (
            "The launcher file is in out/app.",
            "▣  Build · model · 6.0s",
            "ctrl+p commands",
        ),
    }
    for agent, screen in cases.items():
        name, source, adapter, context = _ssh_runtime(screen)
        assert (name, source, adapter.name) == (agent, "ui-via-ssh", agent)
        assert adapter.classify(context).status == "IDLE"
        assert adapter.detect_attention(context) == "none"
        result = adapter.extract_result(context)
        assert result is not None and result.text


def test_short_final_answers_survive_completion_evidence():
    ok = CodexAdapter().extract_result(PaneContext(title="", command="ssh", lines=(
        "OK",
        "Worked for 1s",
        "› Ask Codex to do anything",
    )))
    assert ok is not None and ok.text == "OK"
    done = CodexAdapter().extract_result(PaneContext(title="", command="ssh", lines=(
        "Done",
        "Worked for 1s",
        "› Ask Codex to do anything",
    )))
    assert done is not None and done.text == "Done"
    passed = CodexAdapter().extract_result(PaneContext(title="", command="ssh", lines=(
        "PASS",
        "Worked for 1s",
        "› Ask Codex to do anything",
    )))
    assert passed is not None and passed.text == "PASS"
    korean = ClaudeAdapter().extract_result(PaneContext(title="", command="ssh", lines=(
        "완료",
        "✻ Cooked for 1s",
        "❯",
    )))
    assert korean is not None and korean.text == "완료"
    ne = ClaudeAdapter().extract_result(PaneContext(title="", command="ssh", lines=(
        "네",
        "✻ Cooked for 1s",
        "❯",
    )))
    assert ne is not None and ne.text == "네"
    assert CodexAdapter().extract_result(PaneContext(title="", command="codex", lines=(
        "Finished",
        "› Ask Codex to do anything",
    ))) is None


def test_cursor_trust_dialog_is_not_part_of_the_result():
    screen = (
        "Trust this folder?",
        "[a] Trust",
        "[d] Don't trust",
        "Yes, I trust this folder",
        "The patch is applied.",
        "→ Add a follow-up",
    )
    result = CursorAdapter().extract_result(PaneContext(title="", command="ssh", lines=screen))
    assert result is not None
    assert result.text == "The patch is applied."
    assert "Trust" not in result.text
    only_trust = screen[:-2] + ("→ Add a follow-up",)
    assert CursorAdapter().extract_result(PaneContext(title="", command="ssh", lines=only_trust)) is None


def test_opencode_drops_the_prompt_box_and_reads_a_wrapped_duration():
    screen = (
        "┃  build the widget please",
        "→ Read src/app.py",
        "The launcher is not implemented.",
        "▣  Build · Muse Spark · 2m",
        "24s",
        "┃  build the widget please",
        "ctrl+p commands",
    )
    name, _source, adapter, context = _ssh_runtime(screen)
    assert name == "OpenCode"
    result = adapter.extract_result(context)
    assert result is not None
    assert result.text == "The launcher is not implemented."
    assert "build the widget" not in result.text
    assert "Read" not in result.text


def test_ready_badge_and_y_return_the_same_ssh_body(monkeypatch):
    from tmux_agent_tower.control.actions import get_result

    screen = [
        "→ Add a follow-up",
        "The patch is applied.",
        "→ Add a follow-up",
    ]
    name, _source, adapter, context = _ssh_runtime(screen, title="Routing")
    assert name == "Cursor"
    candidate = adapter.extract_result(context)
    assert candidate is not None
    tower = _RecoverTower("IDLE")
    snap = tower.results.observe("%9", "IDLE", candidate)
    assert snap.state == "ready"

    def run_tmux(args, capture=True, timeout=3):
        if args[:2] == ["list-panes", "-s"]:
            return "%9\n"
        if args[:2] == ["display-message", "-p"]:
            return "Routing\x1fssh"
        return ""

    monkeypatch.setattr("tmux_agent_tower.control.actions.tmux_capture.run_tmux", run_tmux)
    monkeypatch.setattr("tmux_agent_tower.control.actions.tmux_capture.pane_exists", lambda pane_id: True)
    monkeypatch.setattr(
        "tmux_agent_tower.control.actions.tmux_capture.capture_pane",
        lambda pane_id, lines=30, **_kwargs: list(screen),
    )
    ok, reason, payload = get_result(tower, "%9")
    assert ok and reason == ""
    assert payload["text"] == candidate.text == snap.text


def test_y_sends_the_recovered_body_bytes(monkeypatch):
    import hashlib

    from tmux_agent_tower.clipboard import copy_text
    from tmux_agent_tower.control.actions import get_result

    screen = ["› user prompt", "완료\n둘째 줄", "Worked for 1s", "› Ask Codex to do anything"]

    def run_tmux(args, capture=True, timeout=3):
        if args[:2] == ["list-panes", "-s"]:
            return "%9\n"
        if args[:2] == ["display-message", "-p"]:
            return "task\x1fssh"
        return ""

    monkeypatch.setattr("tmux_agent_tower.control.actions.tmux_capture.run_tmux", run_tmux)
    monkeypatch.setattr("tmux_agent_tower.control.actions.tmux_capture.pane_exists", lambda pane_id: True)
    monkeypatch.setattr(
        "tmux_agent_tower.control.actions.tmux_capture.capture_pane",
        lambda pane_id, lines=30, **_kwargs: list(screen),
    )
    ok, _reason, payload = get_result(_RecoverTower("IDLE"), "%9")
    assert ok and payload["text"] == "완료\n둘째 줄"
    expected = payload["text"].encode("utf-8")
    sent = []

    def run(argv, data):
        sent.append(data)
        return 0

    outcome = copy_text(
        payload["text"],
        run=run,
        which=lambda name: f"/bin/{name}" if name in ("wl-copy", "wl-paste") else None,
        environ={"WAYLAND_DISPLAY": "wayland-0"},
        system="linux",
        verify=lambda _tool, _body: True,
    )
    assert outcome.clipboard is True
    assert sent[0] == expected
    assert hashlib.sha256(sent[0]).hexdigest() == hashlib.sha256(expected).hexdigest()
