"""Identity: process beats title, git beats an agent-shaped title, overrides stick."""

from tmux_agent_tower.adapters import resolve_adapter
from tmux_agent_tower.detection.identity import (
    evidence_cmdline,
    identify_agent,
    prefer_override,
)
from tmux_agent_tower.server import webui
from tmux_agent_tower.ui import render

CURSOR_ARGS = (
    "/home/user/.local/bin/agent --use-system-ca "
    "/home/user/.local/share/cursor-agent/versions/2026.09.28-64d2043/index.js"
)


def test_cursor_process_beats_a_claude_title():
    name, source = identify_agent("agent", "Claude Tmux Control", CURSOR_ARGS)
    assert (name, source) == ("Cursor", "process")
    assert resolve_adapter("agent", "Claude Tmux Control", CURSOR_ARGS).name == "Cursor"


def test_shell_child_mentioning_claude_does_not_become_claude():
    name, source = identify_agent("bash", "Claude Tmux Control", "bash -c echo claude")
    assert (name, source) == ("Shell", "process")


def test_title_claude_on_a_real_shell_stays_shell():
    name, source = identify_agent("bash", "Claude Tmux Control", "-bash")
    assert (name, source) == ("Shell", "process")
    assert resolve_adapter("bash", "Claude Tmux Control", "-bash").name == "Shell"


def test_claude_inside_a_cursor_terminal_is_claude():
    cmdline = {"1": "/usr/bin/cursor", "2": "/usr/local/bin/claude"}
    ppid = {"1": "0", "2": "1"}
    evidence = evidence_cmdline("1", "cursor", cmdline, ppid)
    name, source = identify_agent("claude", "Cursor", evidence)
    assert evidence.endswith("claude")
    assert (name, source) == ("Claude", "process")


def test_codex_started_from_a_shell_is_codex():
    cmdline = {"10": "-bash", "11": "/usr/bin/codex"}
    ppid = {"10": "1", "11": "10"}
    evidence = evidence_cmdline("10", "codex", cmdline, ppid)
    name, source = identify_agent("codex", "bash", evidence)
    assert (name, source) == ("Codex", "process")


def test_cursor_agent_keeps_its_name_when_a_tool_shell_is_the_child():
    cmdline = {
        "10": "-bash",
        "11": CURSOR_ARGS,
        "12": "bash -c echo claude",
    }
    ppid = {"10": "1", "11": "10", "12": "11"}
    evidence = evidence_cmdline("10", "agent", cmdline, ppid)
    name, source = identify_agent("agent", "Claude Tmux Control", evidence)
    assert (name, source) == ("Cursor", "process")


def test_agent_exit_returns_to_shell_on_the_next_look():
    running = {"10": "-bash", "11": "/usr/bin/claude"}
    running_ppid = {"10": "1", "11": "10"}
    exited = {"10": "-bash"}
    exited_ppid = {"10": "1"}
    while_running = evidence_cmdline("10", "claude", running, running_ppid)
    after_exit = evidence_cmdline("10", "bash", exited, exited_ppid)
    assert identify_agent("claude", "Claude Tmux Control", while_running) == ("Claude", "process")
    assert identify_agent("bash", "Claude Tmux Control", after_exit) == ("Shell", "process")


def test_ui_signature_is_used_only_without_a_process():
    lines = ("❯", "? for shortcuts")
    assert identify_agent("", "notes", "", lines) == ("Claude", "ui")
    assert identify_agent("bash", "Claude Tmux Control", "-bash", lines)[0] == "Shell"


def test_git_project_beats_a_different_pane_title():
    name, source = render.resolve_project_identity(
        None, "tmux-agent-tower", "Claude Tmux Control", "f", "HOST", "(이름 없음)"
    )
    assert (name, source) == ("tmux-agent-tower", "git")


def test_agent_shaped_title_is_not_a_project():
    name, source = render.resolve_project_identity(
        None, None, "Claude Tmux Control", "f", "HOST", "(이름 없음)"
    )
    assert name == "(이름 없음)"
    assert source == "none"
    assert "Claude" not in name


def test_task_title_still_fills_in_when_the_directory_says_nothing():
    name = render.resolve_display_project(
        custom=None, git_name=None, title="Prepare P14 direct pilot",
        basename="f", local_host="workstation-a", no_name_label="(no name)",
    )
    assert name == "Prepare P14 direct pilot"


def test_manual_override_is_not_replaced_by_detection():
    assert prefer_override("Cursor", "process", "Claude") == ("Claude", "override")
    assert prefer_override("(이름 없음)", "none", None) == ("(이름 없음)", "none")


def test_sense_line_shows_the_auto_source_behind_an_override():
    text = render.format_identity_sense("override", "override", "process", "none")
    assert text == "override→process / override→none"


def test_phone_detail_has_a_sense_line_and_home_cards_do_not():
    html = webui.PAGE_HTML
    assert 'id="d-sense"' in html
    assert "override→" in html


def test_cline_cli_is_identified_without_a_result_adapter():
    name, source = identify_agent("cline", "", "/usr/local/bin/cline")
    assert (name, source) == ("Cline", "process")
    assert resolve_adapter("cline", "", "/usr/local/bin/cline").name == "Shell"
