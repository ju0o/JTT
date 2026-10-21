from tmux_agent_tower.adapters import resolve_adapter
from tmux_agent_tower.adapters.base import PaneContext
from tmux_agent_tower.adapters.cursor import CursorAdapter
from tmux_agent_tower.adapters.opencode import OpenCodeAdapter
from tmux_agent_tower.detection.status import (
    StatusEngine,
    STATUS_WORKING,
    STATUS_IDLE,
    STATUS_UNKNOWN,
    STATUS_DEAD,
)
from tmux_agent_tower.ui import render


def _ctx(lines, title="", command="codex"):
    return PaneContext(title=title, command=command, lines=tuple(lines))


def test_dead_pane_is_dead_regardless_of_content():
    engine = StatusEngine()
    adapter = resolve_adapter("codex", "")
    ctx = _ctx(["Working (1s * esc to interrupt)"])
    assert engine.evaluate("p1", True, adapter, ctx) == STATUS_DEAD


def test_first_observation_blank_is_unknown_not_idle():
    # Regression target for the "CHECKING should not exist, but an honest
    # first look at nothing on screen must not become a false IDLE" rule.
    engine = StatusEngine()
    adapter = resolve_adapter("bash", "")
    ctx = _ctx([""], command="bash")
    assert engine.evaluate("p1", False, adapter, ctx) == STATUS_UNKNOWN


def test_unvisited_but_actively_working_pane_reports_working_not_checking():
    # The core bug this project fixes: NEW/SEEN must never leak into status.
    engine = StatusEngine()
    adapter = resolve_adapter("codex", "")
    ctx = _ctx(["Working (2m 0s * esc to interrupt)"])
    # No visit/seen state is passed to evaluate() at all -- status is
    # computed the same whether or not a human has looked at this pane yet.
    assert engine.evaluate("never-seen-pane", False, adapter, ctx) == STATUS_WORKING


def test_output_change_is_working():
    engine = StatusEngine()
    adapter = resolve_adapter("bash", "")
    engine.evaluate("p1", False, adapter, _ctx(["line one"], command="bash"))
    status = engine.evaluate("p1", False, adapter, _ctx(["line one", "line two"], command="bash"))
    assert status == STATUS_WORKING


def test_working_holds_briefly_after_output_pauses():
    engine = StatusEngine(hold_seconds=5.0)
    adapter = resolve_adapter("bash", "")
    now = 1000.0
    engine.evaluate("p1", False, adapter, _ctx(["a"], command="bash"), now=now)
    engine.evaluate("p1", False, adapter, _ctx(["a", "b"], command="bash"), now=now + 1)
    # Screen stops changing 2s later, well inside the hold window.
    status = engine.evaluate("p1", False, adapter, _ctx(["a", "b"], command="bash"), now=now + 3)
    assert status == STATUS_WORKING


def test_idle_after_hold_window_expires():
    engine = StatusEngine(hold_seconds=2.0)
    adapter = resolve_adapter("bash", "")
    now = 1000.0
    engine.evaluate("p1", False, adapter, _ctx(["a"], command="bash"), now=now)
    engine.evaluate("p1", False, adapter, _ctx(["a", "b"], command="bash"), now=now + 0.1)
    status = engine.evaluate("p1", False, adapter, _ctx(["a", "b"], command="bash"), now=now + 10)
    assert status == STATUS_UNKNOWN


def test_no_flicker_on_stable_unchanged_screen():
    engine = StatusEngine(hold_seconds=0.01)
    adapter = resolve_adapter("bash", "")
    now = 1000.0
    engine.evaluate("p1", False, adapter, _ctx(["stable"], command="bash"), now=now)
    for i in range(5):
        status = engine.evaluate(
            "p1", False, adapter, _ctx(["stable"], command="bash"), now=now + 1 + i
        )
        assert status == STATUS_UNKNOWN, "a stable screen with no opinion is not idle"


def test_generic_waiting_fallback_for_unknown_adapter():
    engine = StatusEngine()
    adapter = resolve_adapter("some-random-tool", "")
    ctx = _ctx(["Do you want to proceed?", "(y/n)"], command="some-random-tool")
    assert engine.evaluate("p1", False, adapter, ctx) == STATUS_UNKNOWN
    assert adapter.detect_attention(ctx) == "none"


def test_forget_clears_state():
    engine = StatusEngine()
    adapter = resolve_adapter("bash", "")
    engine.evaluate("p1", False, adapter, _ctx(["a"], command="bash"))
    engine.forget("p1")
    # After forgetting, the next observation is treated as the first again.
    status = engine.evaluate("p1", False, adapter, _ctx([""], command="bash"))
    assert status == STATUS_UNKNOWN


# -- status duration ("Task Awareness" v0.2.0) ---------------------------


def test_duration_is_zero_before_any_observation():
    engine = StatusEngine()
    assert engine.duration_seconds("never-observed") == 0.0


def test_duration_starts_at_zero_on_first_observation():
    engine = StatusEngine()
    adapter = resolve_adapter("bash", "")
    now = 1000.0
    engine.evaluate("p1", False, adapter, _ctx(["stable"], command="bash"), now=now)
    assert engine.duration_seconds("p1", now=now) == 0.0


def test_duration_accumulates_while_status_is_unchanged():
    # A real shell prompt line resolves to IDLE from the very first
    # observation (unlike blank/no-signal content, which is honestly
    # UNKNOWN on the first look) -- avoids an UNKNOWN -> IDLE transition
    # on the second tick that would otherwise reset duration itself.
    engine = StatusEngine(hold_seconds=0.01)
    adapter = resolve_adapter("bash", "")
    now = 1000.0
    ctx = _ctx(["user@host:~$"], command="bash")
    engine.evaluate("p1", False, adapter, ctx, now=now)
    engine.evaluate("p1", False, adapter, ctx, now=now + 5)
    engine.evaluate("p1", False, adapter, ctx, now=now + 12)
    assert engine.duration_seconds("p1", now=now + 12) == 12.0


def test_duration_resets_on_status_transition():
    # hold_seconds tiny so a status change takes effect on the next tick
    # instead of being held over by the WORKING hysteresis window.
    engine = StatusEngine(hold_seconds=0.01)
    adapter = resolve_adapter("codex", "")
    now = 1000.0
    working_ctx = _ctx(["Working (1m • esc to interrupt)"])
    engine.evaluate("p1", False, adapter, working_ctx, now=now)
    engine.evaluate("p1", False, adapter, working_ctx, now=now + 30)
    assert engine.duration_seconds("p1", now=now + 30) == 30.0

    # A recognized approval prompt is IDLE execution plus independent
    # attention, and immediately clears the prior working hold.
    waiting_ctx = _ctx(["Allow this command to run?", "1. Yes", "2. No"])
    assert engine.evaluate("p1", False, adapter, waiting_ctx, now=now + 31) == STATUS_IDLE
    assert adapter.detect_attention(waiting_ctx) == "approval_required"
    status = engine.evaluate("p1", False, adapter, waiting_ctx, now=now + 35)
    assert status == STATUS_IDLE
    assert engine.duration_seconds("p1", now=now + 35) == 4.0
    engine.evaluate("p1", False, adapter, waiting_ctx, now=now + 44)
    assert engine.duration_seconds("p1", now=now + 44) == 13.0


def test_dead_duration_accumulates_across_repeated_observations():
    # Regression: an earlier version called forget() on every single dead
    # observation, which reset status_since to "now" every time -- DEAD
    # duration could never grow past one refresh interval.
    engine = StatusEngine()
    adapter = resolve_adapter("bash", "")
    now = 1000.0
    engine.evaluate("p1", True, adapter, _ctx([]), now=now)
    engine.evaluate("p1", True, adapter, _ctx([]), now=now + 10)
    engine.evaluate("p1", True, adapter, _ctx([]), now=now + 25)
    assert engine.duration_seconds("p1", now=now + 25) == 25.0


def test_pane_id_reused_after_dead_resets_baseline_not_duration_semantics():
    # A pane_id can be reused by a brand-new process after the old one
    # died; the stale hash/observation baseline must not leak into the
    # new process's first reading (still an honest first-observation).
    engine = StatusEngine()
    adapter = resolve_adapter("bash", "")
    now = 1000.0
    engine.evaluate("p1", True, adapter, _ctx([]), now=now)
    status = engine.evaluate("p1", False, adapter, _ctx(["fresh process output"], command="bash"), now=now + 5)
    assert status == STATUS_UNKNOWN
    assert engine.duration_seconds("p1", now=now + 5) == 0.0


def test_runtime_identity_change_without_dead_observation_resets_status_history():
    engine = StatusEngine()
    adapter = resolve_adapter("bash", "")
    now = 1000.0
    engine.evaluate("p1", False, adapter, _ctx(["old runtime"]), now=now, runtime_identity="4101")
    assert engine.evaluate(
        "p1", False, adapter, _ctx(["old runtime", "new output"]), now=now + 1,
        runtime_identity="4101",
    ) == STATUS_WORKING

    # A respawn/reused pane id starts with a clean observation baseline,
    # even if Tower did not observe its dead interval.
    status = engine.evaluate(
        "p1", False, adapter, _ctx(["old runtime", "new output"]), now=now + 2,
        runtime_identity="5202",
    )
    assert status == STATUS_UNKNOWN
    assert engine.duration_seconds("p1", now=now + 2) == 0.0


def test_idle_widget_beats_a_ticking_clock():
    engine = StatusEngine(hold_seconds=8.0)
    adapter = resolve_adapter("codex", "")
    now = 1000.0
    first = _ctx(["The answer is ready.", "Worked for 2s", "› Ask Codex to do anything  1s"])
    second = _ctx(["The answer is ready.", "Worked for 2s", "› Ask Codex to do anything  2s"])
    assert engine.evaluate("p1", False, adapter, first, now=now) == STATUS_IDLE
    assert engine.evaluate("p1", False, adapter, second, now=now + 2) == STATUS_IDLE


def test_cursor_current_followup_beats_stale_working_scrollback(fixture_lines):
    adapter = CursorAdapter()
    ctx = _ctx(fixture_lines("cursor-stale-working-idle.txt"), command="cursor-agent")
    assert adapter.classify(ctx).status == STATUS_IDLE
    assert StatusEngine().evaluate("cursor-pane", False, adapter, ctx) == STATUS_IDLE


def test_cursor_current_spinner_still_means_working(fixture_lines):
    adapter = CursorAdapter()
    ctx = _ctx(fixture_lines("cursor-working-spinner.txt"), command="cursor-agent")
    assert adapter.classify(ctx).status == STATUS_WORKING


def test_opencode_current_footer_beats_stale_working_scrollback(fixture_lines):
    adapter = OpenCodeAdapter()
    ctx = _ctx(fixture_lines("opencode-stale-working-idle.txt"), command="opencode")
    assert adapter.classify(ctx).status == STATUS_IDLE
    assert StatusEngine().evaluate("opencode-pane", False, adapter, ctx) == STATUS_IDLE


def test_opencode_current_working_footer_still_means_working(fixture_lines):
    adapter = OpenCodeAdapter()
    ctx = _ctx(fixture_lines("opencode-working.txt"), command="opencode")
    assert adapter.classify(ctx).status == STATUS_WORKING


def test_old_working_history_yields_to_current_idle_fixtures(fixture_lines):
    cases = (
        ("codex", "codex-working.txt", "codex-idle.txt"),
        ("claude", "claude-working.txt", "claude-idle.txt"),
        ("cursor-agent", "cursor-stale-working-idle.txt", None),
        ("opencode", "opencode-stale-working-idle.txt", None),
    )
    for command, old_work, current_idle in cases:
        lines = fixture_lines(old_work)
        if current_idle:
            lines += fixture_lines(current_idle)
        adapter = resolve_adapter(command, "")
        ctx = _ctx(lines, command=command)
        assert adapter.classify(ctx).status == STATUS_IDLE, command
        assert StatusEngine().evaluate(command, False, adapter, ctx) == STATUS_IDLE


def test_agent_specific_current_work_and_input_waiting_labels(fixture_lines):
    cases = (
        ("codex", "codex-working.txt", "codex-idle.txt"),
        ("claude", "claude-working.txt", "claude-idle.txt"),
        ("cursor-agent", "cursor-working-spinner.txt", "cursor-idle-followup.txt"),
        ("opencode", "opencode-working.txt", "opencode-idle.txt"),
        ("grok", "grok-working.txt", "grok-idle.txt"),
    )
    for command, working_fixture, idle_fixture in cases:
        adapter = resolve_adapter(command, "")
        working = _ctx(fixture_lines(working_fixture), command=command)
        idle = _ctx(fixture_lines(idle_fixture), command=command)
        assert StatusEngine().evaluate(command + "-working", False, adapter, working) == STATUS_WORKING
        assert render.execution_badge(STATUS_WORKING, agent_process_alive=True)[1] == "state.working"
        assert StatusEngine().evaluate(command + "-idle", False, adapter, idle) == STATUS_IDLE
        assert render.execution_badge(STATUS_IDLE, agent_process_alive=True)[1] == "state.input_waiting"


def test_recognized_attention_is_idle_execution_on_all_current_prompt_fixtures(fixture_lines):
    cases = (
        ("codex", "codex-waiting.txt"),
        ("claude", "claude-waiting.txt"),
        ("cursor-agent", "cursor-waiting.txt"),
        ("opencode", "opencode-waiting.txt"),
    )
    for command, fixture in cases:
        adapter = resolve_adapter(command, "")
        ctx = _ctx(fixture_lines(fixture), command=command)
        assert adapter.detect_attention(ctx) == "approval_required", command
        assert StatusEngine().evaluate(command, False, adapter, ctx) == STATUS_IDLE, command


def test_current_approval_overrides_older_working_fixture(fixture_lines):
    cases = (
        ("codex", "codex-working.txt", "codex-waiting.txt"),
        ("claude", "claude-working.txt", "claude-waiting.txt"),
        ("cursor-agent", "cursor-working-spinner.txt", "cursor-waiting.txt"),
        # opencode-waiting is an explicitly unverified fixture shape; this
        # tests only that unverified prompts do not become WORKING.
        ("opencode", "opencode-working.txt", "opencode-waiting.txt"),
    )
    for command, old_work, current_prompt in cases:
        lines = fixture_lines(old_work) + fixture_lines(current_prompt)
        adapter = resolve_adapter(command, "")
        ctx = _ctx(lines, command=command)
        assert adapter.detect_attention(ctx) == "approval_required", command
        assert StatusEngine().evaluate(command, False, adapter, ctx) == STATUS_IDLE, command


def test_question_is_idle_execution_with_input_attention():
    adapter = resolve_adapter("codex", "")
    ctx = _ctx(["Which format do you want?"], command="codex")
    assert adapter.detect_attention(ctx) == "input_required"
    assert StatusEngine().evaluate("question", False, adapter, ctx) == STATUS_IDLE


def test_strong_working_evidence_beats_a_stable_screen_hash():
    engine = StatusEngine(hold_seconds=0.01)
    adapter = resolve_adapter("codex", "")
    ctx = _ctx(["Working (1m • esc to interrupt)"])
    now = 1000.0
    for step in range(4):
        status = engine.evaluate("p1", False, adapter, ctx, now=now + step)
        assert status == STATUS_WORKING
