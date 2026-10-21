"""Prompt delivery is one bracketed paste and one Enter, then a check.

"Sent" and "submitted" are different answers. The phone keeps the text
when the submit was not confirmed, and nothing retries Enter blindly.
"""

import pytest

from tmux_agent_tower.adapters.base import PaneContext, prompt_head
from tmux_agent_tower.adapters.claude import ClaudeAdapter
from tmux_agent_tower.adapters.codex import CodexAdapter
from tmux_agent_tower.adapters.cursor import CursorAdapter
from tmux_agent_tower.adapters.opencode import OpenCodeAdapter
from tmux_agent_tower.adapters.shell import ShellAdapter
from tmux_agent_tower.control import actions
from tmux_agent_tower.server import webui


class _Done:
    returncode = 0


def _record(calls):
    def fake_run(args, **kwargs):
        calls.append((list(args), kwargs.get("input"), kwargs.get("shell", False)))
        return _Done()

    return fake_run


KOREAN = "첫 줄 한글 프롬프트\n둘째 줄 특수문자 !@#$%^&*()[]{}<>|;'\"\\\n```python\nprint(\"hi\")\n```"


def test_send_text_is_one_bracketed_paste_with_the_exact_bytes(monkeypatch):
    calls = []
    monkeypatch.setattr(actions.subprocess, "run", _record(calls))
    assert actions.send_text("%7", KOREAN + "\n") is True
    assert len(calls) == 2
    load, paste = calls
    assert load[0][:3] == ["tmux", "load-buffer", "-b"] and load[0][-1] == "-"
    assert load[1] == (KOREAN + "\n").encode("utf-8")
    assert paste[0][:4] == ["tmux", "paste-buffer", "-d", "-p"]
    assert paste[0][-2:] == ["-t", "%7"]
    assert all(call[2] is False for call in calls)


def test_long_text_is_passed_through_stdin_not_argv(monkeypatch):
    calls = []
    monkeypatch.setattr(actions.subprocess, "run", _record(calls))
    long_text = ("가나다 abc " * 400).strip()
    assert actions.send_text("%7", long_text)
    assert calls[0][1] == long_text.encode("utf-8")
    assert long_text not in " ".join(calls[0][0])


@pytest.mark.parametrize(
    "text",
    ["one line", KOREAN, "가" * 10000],
    ids=["one-line", "korean-multiline-fence", "10000-chars"],
)
def test_send_text_preserves_the_entire_prompt(monkeypatch, text):
    calls = []
    monkeypatch.setattr(actions.subprocess, "run", _record(calls))
    assert actions.send_text("%7", text) is True
    assert calls[0][1] == text.encode("utf-8")


PROMPT_SHAPES = pytest.param(
    ["Explain this result."], id="one-line"
), pytest.param(
    ["First line", "Second line"], id="multiline"
), pytest.param(
    ["한국어로 답해주세요.", "```python", "print('안녕')", "```"], id="korean-code-fence"
), pytest.param(
    ["가" * 10000], id="10000-chars"
)


def _agent_submission_case(name, lines):
    text = "\n".join(lines)
    head = prompt_head(text)
    if name == "Codex":
        return CodexAdapter(), _ctx(["› Ask Codex to do anything"], "codex"), _ctx(
            [f"› {head}", "Worked for 1s", "› Ask Codex to do anything"], "codex"
        )
    if name == "Claude":
        footer = "⏸ manual mode on · ? for shortcuts"
        return ClaudeAdapter(), _ctx(["❯ Try \"fix typecheck errors\"", footer], "claude"), _ctx(
            [f"❯ {head}", "● answer", "✻ Cooked for 2s · done", "❯", footer], "claude"
        )
    if name == "OpenCode":
        return OpenCodeAdapter(), _ctx(["tab agents  ctrl+p commands"], "opencode"), _ctx(
            [f"┃  {head}", "answer", "▣  Build · model · 5.2s", "ctrl+p commands"], "opencode"
        )
    before = _ctx(["→ Plan, search, build anything"], "cursor-agent")
    after = _ctx([head, "⠘⠆ Working", "→ Add a follow-up"], "cursor-agent")
    return CursorAdapter(), before, after


@pytest.mark.parametrize("name", ["Codex", "Claude", "OpenCode", "Cursor"])
@pytest.mark.parametrize("lines", PROMPT_SHAPES)
def test_agent_submission_evidence_is_fresh_and_handles_prompt_shapes(name, lines):
    adapter, before, after = _agent_submission_case(name, lines)
    text = "\n".join(lines)
    assert adapter.confirm_submitted(before, after, text) is True
    assert adapter.confirm_submitted(after, after, text) is None


def test_submit_input_sends_enter_once_and_refuses_other_keys(monkeypatch):
    calls = []
    monkeypatch.setattr(actions.subprocess, "run", _record(calls))
    assert actions.submit_input("%7") is True
    assert calls == [(["tmux", "send-keys", "-t", "%7", "Enter"], None, False)]
    assert actions.submit_input("%7", "C-m") is False
    assert actions.submit_input("%7", "y") is False
    assert len(calls) == 1


def test_send_prompt_to_pane_never_doubles_enter(monkeypatch):
    calls = []
    monkeypatch.setattr(actions.subprocess, "run", _record(calls))
    assert actions.send_prompt_to_pane("%7", "hello")
    enters = [c for c in calls if c[0][:2] == ["tmux", "send-keys"]]
    assert len(enters) == 1
    assert enters[0][0][-1] == "Enter"


def test_control_room_text_submission_revalidates_after_paste_and_never_retries(monkeypatch):
    row = {"kind": "pane", "key": "%7", "pane_id": "%7", "auto_agent": "Codex",
           "auto_agent_source": "process", "agent_process_alive": True, "transport": "local"}
    allowed = actions.FocusTargetValidation(actions.FocusTargetStatus.ALLOWED, "%7", ("identity",))
    stale = actions.FocusTargetValidation(actions.FocusTargetStatus.STALE, "%7", ())
    checks = iter((allowed, stale))
    calls = []
    monkeypatch.setattr(actions, "validate_focus_target", lambda *_a: next(checks))
    monkeypatch.setattr(actions, "send_text", lambda pane, text: calls.append(("paste", pane, text)) or True)
    monkeypatch.setattr(actions, "submit_input", lambda *args: calls.append(("enter", *args)) or True)

    result = actions.submit_user_text_once(object(), row, "user authored text")

    assert result.status is actions.TextSubmissionStatus.PASTED_NOT_SUBMITTED
    assert result.stage == "before_submit"
    assert calls == [("paste", "%7", "user authored text")]


def test_control_room_text_submission_sends_one_paste_and_one_enter(monkeypatch):
    row = {"kind": "pane", "key": "%7", "pane_id": "%7", "auto_agent": "Claude",
           "auto_agent_source": "process", "agent_process_alive": True, "transport": "local"}
    allowed = actions.FocusTargetValidation(actions.FocusTargetStatus.ALLOWED, "%7", ("same",))
    checks, calls = [], []
    monkeypatch.setattr(actions, "validate_focus_target", lambda *_a: checks.append(True) or allowed)
    monkeypatch.setattr(actions, "send_text", lambda pane, text: calls.append(("paste", pane, text)) or True)
    monkeypatch.setattr(actions, "submit_input", lambda *args: calls.append(("enter", *args)) or True)

    result = actions.submit_user_text_once(object(), row, "one request")

    assert result.status is actions.TextSubmissionStatus.SUBMITTED
    assert result.validation.identity == ("same",)
    assert calls == [("paste", "%7", "one request"), ("enter", "%7", "Enter")]
    assert len(checks) == 2


def test_control_room_text_submission_blocks_remote_and_unknown_agent(monkeypatch):
    calls = []
    monkeypatch.setattr(actions, "validate_focus_target",
                        lambda *_a: calls.append("validate") or actions.FocusTargetValidation(
                            actions.FocusTargetStatus.REMOTE_UNSUPPORTED, "%7"))
    monkeypatch.setattr(actions, "send_text", lambda *_a: calls.append("paste") or True)
    monkeypatch.setattr(actions, "submit_input", lambda *_a: calls.append("enter") or True)

    peer = {"kind": "pane", "key": "%7", "pane_id": "%7", "auto_agent": "OpenCode",
            "remote": True, "transport": "remote_tmux"}
    unknown = {"kind": "pane", "key": "%8", "pane_id": "%8", "auto_agent": "Shell",
               "auto_agent_source": "process", "agent_process_alive": True, "transport": "local"}
    ssh_client = {"kind": "pane", "key": "%9", "pane_id": "%9", "auto_agent": "Claude",
                  "auto_agent_source": "process", "agent_process_alive": True, "transport": "ssh"}
    assert actions.submit_user_text_once(object(), peer, "hello").status is actions.TextSubmissionStatus.REFUSED
    assert actions.submit_user_text_once(object(), unknown, "hello").status is actions.TextSubmissionStatus.REFUSED
    assert actions.submit_user_text_once(object(), ssh_client, "hello").status is actions.TextSubmissionStatus.REFUSED
    assert calls == []


def test_control_room_text_submission_rejects_multiline_or_control_text_without_io(monkeypatch):
    calls = []
    monkeypatch.setattr(actions, "validate_focus_target", lambda *_a: calls.append("validate"))
    monkeypatch.setattr(actions, "send_text", lambda *_a: calls.append("paste"))
    monkeypatch.setattr(actions, "submit_input", lambda *_a: calls.append("enter"))
    row = {"kind": "pane", "key": "%7", "pane_id": "%7", "auto_agent": "Codex",
           "auto_agent_source": "process", "agent_process_alive": True, "transport": "local"}

    for value in ("first\nsecond", "first\tsecond", "first\x1bsecond"):
        result = actions.submit_user_text_once(object(), row, value)
        assert result.status is actions.TextSubmissionStatus.REFUSED
        assert result.stage == "invalid_text"

    assert calls == []


def _prompt_flow(monkeypatch, screens, agent_cmd="codex"):
    """Drive send_prompt with a scripted sequence of screens."""

    frames = list(screens)
    sent = []

    def fake_run_tmux(args, capture=True, timeout=3.0):
        if args and args[0] in ("list-panes", "display-message"):
            return f"title\x1f{agent_cmd}"
        return ""

    def fake_capture(pane_id, lines=40):
        if len(frames) > 1:
            return list(frames.pop(0))
        return list(frames[0])

    monkeypatch.setattr(actions.tmux_capture, "run_tmux", fake_run_tmux)
    monkeypatch.setattr(actions.tmux_capture, "capture_pane", fake_capture)
    monkeypatch.setattr(actions, "find_prompt_target", lambda *a: (True, "", "%7"))
    monkeypatch.setattr(actions, "send_prompt_to_pane", lambda pane, text, key="Enter": sent.append((pane, text, key)) or True)
    monkeypatch.setattr(actions, "SUBMIT_CONFIRM_SECONDS", 0.3)
    monkeypatch.setattr(actions, "SUBMIT_POLL_SECONDS", 0.01)
    return sent


def test_submit_confirmed_when_codex_starts_working(monkeypatch):
    before = ["› Ask Codex to do anything"]
    after = ["› Reply with pong", "Working (1s • esc to interrupt)"]
    sent = _prompt_flow(monkeypatch, [before, after])
    out = actions.send_prompt(object(), "%7", "Reply with pong")
    assert out.ok and out.submitted and out.reason == "submitted"
    assert sent == [("%7", "Reply with pong", "Enter")]


def test_confirmed_tower_prompt_records_only_the_pane_history_watermark(monkeypatch):
    from tmux_agent_tower.detection.result import ResultTracker

    identity = {
        "tmux_host": "host", "server_scope": {"socket": "/tmp/tmux-test"},
        "session": "0", "window_id": "@1", "pane_id": "%7", "pane_pid": "1234",
    }

    class Tower:
        results = ResultTracker()
        rows = [{"key": "%7"}]

        def load(self):
            return None

    tower = Tower()
    _prompt_flow(
        monkeypatch,
        [["› Ask Codex to do anything"], ["› Reply with pong", "Working (1s • esc to interrupt)"]],
    )
    monkeypatch.setattr(actions, "pane_result_identity", lambda _row: identity)
    monkeypatch.setattr(actions.tmux_capture, "pane_history_position", lambda _pane: (17, 200))

    result = actions.send_prompt(tower, "%7", "Reply with pong")

    assert result.submitted
    assert tower.results.turn_start_watermark("%7", identity) == (17, 200)
    assert "Reply with pong" not in repr(tower.results._turn_watermarks)


def test_submit_not_confirmed_when_text_stays_in_the_composer(monkeypatch):
    before = ["› Ask Codex to do anything"]
    stuck = ["› Reply with pong"]
    sent = _prompt_flow(monkeypatch, [before, stuck, stuck])
    out = actions.send_prompt(object(), "%7", "Reply with pong")
    assert out.ok is True
    assert out.submitted is False
    assert out.reason == "submit_not_confirmed"
    assert len(sent) == 1


def test_stale_pane_sends_nothing(monkeypatch):
    sent = []
    monkeypatch.setattr(actions, "find_prompt_target", lambda *a: (False, "stale", None))
    monkeypatch.setattr(actions, "send_prompt_to_pane", lambda *a, **k: sent.append(a) or True)
    out = actions.send_prompt(object(), "%7", "hello")
    assert not out.ok and out.reason == "stale"
    assert sent == []


def test_blank_text_sends_nothing(monkeypatch):
    sent = []
    monkeypatch.setattr(actions, "send_prompt_to_pane", lambda *a, **k: sent.append(a) or True)
    out = actions.send_prompt(object(), "%7", "   \n")
    assert not out.ok and out.reason == "missing_text"
    assert sent == []


def _ctx(lines, command):
    return PaneContext(title="", command=command, lines=tuple(lines))


def test_codex_fast_turn_is_confirmed_by_echo_and_worked_for():
    before = _ctx(["› Ask Codex to do anything"], "codex")
    after = _ctx(["› Reply with exactly: pong 1", "• pong 1", "Worked for 1s", "› Ask Codex to do anything"], "codex")
    assert CodexAdapter().confirm_submitted(before, after, "Reply with exactly: pong 1") is True


def test_codex_composer_holding_text_is_not_confirmed():
    before = _ctx(["› Ask Codex to do anything"], "codex")
    stuck = _ctx(["› Reply with exactly: pong 1"], "codex")
    assert CodexAdapter().confirm_submitted(before, stuck, "Reply with exactly: pong 1") is None


def test_long_prompt_submit_confirmation_reads_bounded_history(monkeypatch):
    text = "Read to the last line.\n" + ("x" * 4000)
    before = _ctx(["› Ask Codex to do anything"], "codex")
    after = _ctx(
        [f"› {prompt_head(text)}", "• UX01_LONG_OK", "Worked for 1s", "› Ask Codex to do anything"],
        "codex",
    )
    observations = [before, after]
    requested_lines = []

    def observe(_pane_id, *, lines):
        requested_lines.append(lines)
        return CodexAdapter(), observations.pop(0)

    monkeypatch.setattr(actions, "find_prompt_target", lambda *_args, **_kwargs: (True, "", "%7"))
    monkeypatch.setattr(actions, "_observe", observe)
    monkeypatch.setattr(actions, "send_prompt_to_pane", lambda *_args: True)

    result = actions.send_prompt(object(), "%7", text)

    assert result.ok and result.submitted
    assert requested_lines == [actions.RECOVERY_LINES, actions.RECOVERY_LINES]


def test_claude_two_second_turn_is_confirmed_by_a_new_done_line():
    before = _ctx(["❯ Try \"fix typecheck errors\"", "⏸ manual mode on · ? for shortcuts"], "claude")
    after = _ctx(
        ["❯ Reply with pong", "● pong", "✻ Sautéed for 2s · done 5:19 PM", "❯", "⏸ manual mode on · ? for shortcuts"],
        "claude",
    )
    assert ClaudeAdapter().confirm_submitted(before, after, "Reply with pong") is True


def test_claude_result_is_found_for_any_done_verb_not_only_cooked():
    lines = [
        "❯ Reply with pong",
        "● pong 5",
        "✻ Brewed for 2s · done 5:36 PM",
        "────────",
        "❯ ",
        "────────",
        "  ⏵⏵ auto mode on (shift+tab to cycle) · ← for agents",
    ]
    found = ClaudeAdapter().extract_result(PaneContext(title="✳ Claude Code", command="claude", lines=tuple(lines)))
    assert found is not None and "pong 5" in found.text


def test_claude_ghost_suggestion_in_the_box_is_idle_and_keeps_the_result():
    lines = [
        "● MULTI-1",
        "✻ Baked for 2s · done 5:41 PM",
        "────────",
        "❯ pong 7",
        "────────",
        "  ⏵⏵ auto mode on (shift+tab to cycle) · ← for agents",
    ]
    ctx = PaneContext(title="✳ Claude Code", command="claude", lines=tuple(lines))
    assert ClaudeAdapter().detect_execution(ctx) == "IDLE"
    found = ClaudeAdapter().extract_result(ctx)
    assert found is not None and "MULTI-1" in found.text


def test_claude_conversation_title_glyph_is_static_and_result_is_last_turn_only():
    lines = [
        "❯ Reply with exactly: pong 5",
        "● pong 5",
        "✻ Brewed for 2s · done 5:36 PM",
        "❯ Reply with exactly: TITLE-PROBE then list three colors",
        "● TITLE-PROBE",
        "  Red",
        "  Green",
        "✻ Crunched for 2s · done 5:46 PM",
        "❯ ",
        "  ⏵⏵ auto mode on (shift+tab to cycle) · ← for agents",
    ]
    ctx = PaneContext(title="✳ Pong 1", command="claude", lines=tuple(lines))
    adapter = ClaudeAdapter()
    assert adapter.detect_execution(ctx) == "IDLE"
    assert adapter.detect_attention(ctx) == "none"
    found = adapter.extract_result(ctx)
    assert found is not None
    assert "TITLE-PROBE" in found.text and "pong 5" not in found.text


def test_claude_prose_mentioning_waited_for_is_not_a_turn_end():
    lines = ["❯ explain", "● The job waited for 3s before it ran, then I waited for 2m more.", "❯ "]
    assert ClaudeAdapter().extract_result(PaneContext(title="✳ Claude Code", command="claude", lines=tuple(lines))) is None


def test_claude_text_sitting_in_the_box_is_not_confirmed():
    before = _ctx(["❯ Try \"fix typecheck errors\""], "claude")
    stuck = _ctx(["❯ Reply with pong", "⏸ manual mode on · ? for shortcuts"], "claude")
    assert ClaudeAdapter().confirm_submitted(before, stuck, "Reply with pong") is None


def test_claude_static_title_glyph_is_not_working(fixture_lines):
    ctx = PaneContext(title="✳ Claude Code", command="claude", lines=("❯ Try \"refactor x\"", "⏸ manual mode on · ? for shortcuts"))
    assert ClaudeAdapter().detect_execution(ctx) == "IDLE"
    assert ClaudeAdapter().classify(_ctx(list(fixture_lines("claude-working.txt")), "claude")).status == "WORKING"


def test_opencode_confirmed_by_message_block_or_turn_line():
    before = _ctx(["tab agents  ctrl+p commands"], "opencode")
    running = _ctx(["┃  Reply with pong", "⬝⬝⬝⬝  esc interrupt"], "opencode")
    done = _ctx(["┃  Reply with pong", "pong", "▣  Build · model · 5.2s", "ctrl+p commands"], "opencode")
    adapter = OpenCodeAdapter()
    assert adapter.confirm_submitted(before, running, "Reply with pong") is True
    assert adapter.confirm_submitted(before, done, "Reply with pong") is True


def test_opencode_tall_pane_idle_hint_is_still_idle():
    lines = ["┃  Build · model"] + [""] * 10 + ["tab agents  ctrl+p commands"] + [""] * 40 + ["<path>  1.18.32"]
    assert OpenCodeAdapter().detect_execution(_ctx(lines, "opencode")) == "IDLE"


def test_cursor_second_enter_only_while_the_followup_still_holds_the_text(monkeypatch):
    from tmux_agent_tower.adapters.cursor import CursorAdapter

    phase = {"sent_followup": False}

    def capture(pane_id, lines=40):
        if phase["sent_followup"]:
            return ["Running  1.2k tokens", "→ Add a follow-up"]
        if phase.get("seen"):
            return ["→ Add a follow-up", "hello from tower"]
        phase["seen"] = True
        return ["→ Add a follow-up"]

    keys = []
    monkeypatch.setattr(actions.tmux_capture, "run_tmux", lambda args, capture=True, timeout=3.0: "title\x1fcursor-agent")
    monkeypatch.setattr(actions.tmux_capture, "capture_pane", capture)
    monkeypatch.setattr(actions, "find_prompt_target", lambda *a: (True, "", "%7"))
    monkeypatch.setattr(actions, "send_prompt_to_pane", lambda *a, **k: True)
    monkeypatch.setattr(actions, "submit_input", lambda pane, key="Enter": keys.append(key) or phase.update(sent_followup=True) or True)
    monkeypatch.setattr(actions, "SUBMIT_CONFIRM_SECONDS", 0.05)
    monkeypatch.setattr(actions, "SUBMIT_POLL_SECONDS", 0.01)
    out = actions.send_prompt(object(), "%7", "hello from tower")
    assert keys == ["Enter"]
    assert out.submitted and out.reason == "submitted"
    held = _ctx(["→ Add a follow-up", "hello from tower"], "cursor-agent")
    running = _ctx(["Running  1.2k tokens"], "cursor-agent")
    before = _ctx(["→ Add a follow-up"], "cursor-agent")
    assert CursorAdapter().followup_submit_key(before, held, "hello from tower") == "Enter"
    assert CursorAdapter().followup_submit_key(before, running, "hello from tower") is None


def test_cursor_spinner_working_confirms_without_a_second_enter():
    """Cursor Agent 2026.10: braille Working, follow-up still on screen."""

    from tmux_agent_tower.adapters.cursor import CursorAdapter

    screen = [
        "Reply with only OK.",
        "⠘⠆ Working",
        "→ Add a follow-up                                          ctrl+c to stop",
        "Auto Balance                                              Run Everything",
    ]
    before = _ctx(["→ Plan, search, build anything"], "cursor-agent")
    after = _ctx(screen, "cursor-agent")
    adapter = CursorAdapter()
    assert adapter.classify(after).status == "WORKING"
    assert adapter.confirm_submitted(before, after, "Reply with only OK.") is True
    assert adapter.followup_submit_key(before, after, "Reply with only OK.") is None
    assert adapter.extract_result(after) is None


def test_cursor_fresh_spinner_confirms_submission_even_if_result_body_is_visible():
    from tmux_agent_tower.adapters.cursor import CursorAdapter

    before = _ctx(["→ Plan, search, build anything"], "cursor-agent")
    after = _ctx(
        [
            "hello from tower",
            "⠘⠆ Working",
            "The answer is complete.",
            "→ Add a follow-up",
        ],
        "cursor-agent",
    )
    adapter = CursorAdapter()
    assert adapter.classify(after).status == "IDLE"
    assert adapter.confirm_submitted(before, after, "hello from tower") is True
    assert adapter.followup_submit_key(before, after, "hello from tower") is None


def test_cursor_plan_prompt_is_idle_before_the_first_turn():
    from tmux_agent_tower.adapters.cursor import CursorAdapter

    ctx = _ctx(["→ Plan, search, build anything", "Auto Balance", "Run Everything"], "cursor-agent")
    assert CursorAdapter().classify(ctx).status == "IDLE"


def test_cursor_already_running_does_not_get_a_second_enter(monkeypatch):
    def capture(pane_id, lines=40):
        if not capture.seen:
            capture.seen = True
            return ["→ Add a follow-up"]
        return ["Running  4.9k tokens"]

    capture.seen = False
    keys = []
    monkeypatch.setattr(actions.tmux_capture, "run_tmux", lambda args, capture=True, timeout=3.0: "title\x1fcursor-agent")
    monkeypatch.setattr(actions.tmux_capture, "capture_pane", capture)
    monkeypatch.setattr(actions, "find_prompt_target", lambda *a: (True, "", "%7"))
    monkeypatch.setattr(actions, "send_prompt_to_pane", lambda *a, **k: True)
    monkeypatch.setattr(actions, "submit_input", lambda *a, **k: keys.append(a) or True)
    monkeypatch.setattr(actions, "SUBMIT_CONFIRM_SECONDS", 0.05)
    monkeypatch.setattr(actions, "SUBMIT_POLL_SECONDS", 0.01)
    out = actions.send_prompt(object(), "%7", "hello from tower")
    assert out.submitted and keys == []


def test_cursor_transcript_echo_above_followup_is_not_unsent_composer_text():
    adapter = CursorAdapter()
    before = _ctx(["→ Add a follow-up"], "cursor-agent")
    after = _ctx(["hello from tower", "→ Add a follow-up"], "cursor-agent")
    assert adapter.followup_submit_key(before, after, "hello from tower") is None


def test_cursor_running_marker_already_present_before_send_is_not_new_evidence():
    adapter = CursorAdapter()
    running = _ctx(["Running  1.2k tokens", "→ Add a follow-up"], "cursor-agent")
    assert adapter.confirm_submitted(running, running, "hello from tower") is None
    held_while_running = _ctx(
        ["Running  1.2k tokens", "→ Add a follow-up", "hello from tower"],
        "cursor-agent",
    )
    assert adapter.followup_submit_key(running, held_while_running, "hello from tower") is None


def test_cursor_completed_submit_requires_a_new_result_body():
    adapter = CursorAdapter()
    before = _ctx(["Old answer", "→ Add a follow-up"], "cursor-agent")
    stale = _ctx(["Old answer", "→ Add a follow-up", "Old answer", "→ Add a follow-up"], "cursor-agent")
    fresh = _ctx(["Old answer", "→ Add a follow-up", "New answer", "→ Add a follow-up"], "cursor-agent")
    assert adapter.confirm_submitted(before, stale, "hello from tower") is None
    assert adapter.confirm_submitted(before, fresh, "hello from tower") is True


def test_shell_has_no_submit_evidence_by_default():
    before = _ctx(["user@host:~$"], "bash")
    after = _ctx(["user@host:~$ ls", "a b c", "user@host:~$"], "bash")
    assert ShellAdapter().confirm_submitted(before, after, "ls") is None


def test_capture_pane_drops_trailing_blank_rows(monkeypatch):
    from tmux_agent_tower.tmux import capture

    monkeypatch.setattr(capture, "run_tmux", lambda args, capture=True, timeout=3.0: "a\nb\n\n   \n")
    assert capture.capture_pane("%1") == ["a", "b"]


def test_phone_home_centers_project_agent_state_and_opens_details():
    html = webui.PAGE_HTML
    home = html.split("function render(")[1].split("function pollList")[0]
    assert 'proj.className = "project"' in home
    assert 'agent.className = "agent"' in home
    assert "primaryState(p)" in home
    assert "openDetail(p.key)" in home
    assert "pane_id" not in home and "transport" not in home


def test_pairing_is_not_kicked_back_by_an_unauthenticated_status_poll():
    html = webui.PAGE_HTML
    poll = html.split("function pollList()")[1].split("function fillDetailMeta")[0]
    assert "if (document.hidden || !token) return;" in poll
    assert "if (token !== sentToken) return;" in poll
    boot = html.split("document.addEventListener(\"visibilitychange\"")[1]
    assert boot.count("startTimers();") == 2
    assert "if (token)" in boot


def test_phone_keeps_text_unless_submit_was_confirmed():
    html = webui.PAGE_HTML
    handler = html.split('sendBtn.addEventListener("click"')[1].split("var sendStateTimer")[0]
    confirmed = handler.split("res.body.submitted")[1].split("else if")[0]
    unconfirmed = handler.split("else if (res.status === 200 && res.body.ok)")[1].split("} else {")[0]
    assert 'promptText.value = ""' in confirmed
    assert 'promptText.value = ""' not in unconfirmed
    assert "제출 여부를 확인하지 못했습니다" in unconfirmed
    assert "보내는 중..." in handler
    assert "if (!current || sending) return;" in handler
