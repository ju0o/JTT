import pytest

from tmux_agent_tower.ui import render


def _label(status):
    return {
        "WORKING": "작업 중",
        "WAITING": "대기 중",
        "IDLE": "대기 중",
        "UNKNOWN": "확인 불가",
        "DEAD": "종료됨",
    }[status]


# -- format_host_summary -----------------------------------------------


def test_host_summary_hides_zero_counts():
    counts = {"WORKING": 3, "WAITING": 0, "IDLE": 4, "UNKNOWN": 0, "DEAD": 0}
    summary = render.format_host_summary(counts, _label)
    assert "작업 중 3" in summary
    assert "대기 중 4" in summary
    assert summary.count("대기") == 1
    assert "확인 불가" not in summary
    assert "종료됨" not in summary


def test_host_summary_all_zero_is_empty_string():
    counts = {s: 0 for s in render.STATUS_ORDER}
    assert render.format_host_summary(counts, _label) == ""


def test_host_summary_preserves_status_order():
    counts = {"WORKING": 1, "WAITING": 1, "IDLE": 1, "UNKNOWN": 1, "DEAD": 1}
    summary = render.format_host_summary(counts, _label)
    order = [render.STATUS_ORDER.index(s) for s in render.STATUS_ORDER]
    assert order == sorted(order)  # sanity: STATUS_ORDER itself is fixed
    assert summary.index("작업 중") < summary.index("대기")


# -- looks_meaningful_title ----------------------------------------------


def test_meaningful_title_rejects_empty():
    assert not render.looks_meaningful_title("", "workstation-a")
    assert not render.looks_meaningful_title(None, "workstation-a")


def test_meaningful_title_rejects_unnamed_placeholder():
    assert not render.looks_meaningful_title("(unnamed)", "workstation-a")


def test_meaningful_title_rejects_bare_hostname():
    assert not render.looks_meaningful_title("workstation-a", "workstation-a")
    assert not render.looks_meaningful_title("workstation-a", "workstation-a")  # case-insensitive


def test_meaningful_title_rejects_numeric_window_indexes():
    assert not render.looks_meaningful_title("0", "workstation-a")
    assert not render.looks_meaningful_title("12", "workstation-a")


def test_meaningful_title_accepts_real_title():
    assert render.looks_meaningful_title("JuHome Dev", "workstation-a")


# -- resolve_display_project ----------------------------------------------


def test_project_priority_custom_wins_over_everything():
    name = render.resolve_display_project(
        custom="Custom", git_name="git-name", title="Some Title",
        basename="basename", local_host="workstation-a", no_name_label="(no name)",
    )
    assert name == "Custom"


def test_project_priority_git_name_wins_over_title_and_basename():
    name = render.resolve_display_project(
        custom=None, git_name="my-repo", title="Some Title",
        basename="basename", local_host="workstation-a", no_name_label="(no name)",
    )
    assert name == "my-repo"


def test_project_priority_low_confidence_git_name_is_skipped():
    # A single-letter "git repo name" (unlikely but possible) must not be
    # trusted over a meaningful title.
    name = render.resolve_display_project(
        custom=None, git_name="f", title="JuHome Dev",
        basename="f", local_host="workstation-a", no_name_label="(no name)",
    )
    assert name == "JuHome Dev"


def test_project_priority_meaningful_title_wins_over_low_confidence_basename():
    name = render.resolve_display_project(
        custom=None, git_name=None, title="Prepare P14 direct pilot",
        basename="f", local_host="workstation-a", no_name_label="(no name)",
    )
    assert name == "Prepare P14 direct pilot"


def test_project_priority_basename_used_when_no_better_option():
    name = render.resolve_display_project(
        custom=None, git_name=None, title=None,
        basename="AI-Agent-Marketplace", local_host="workstation-a", no_name_label="(no name)",
    )
    assert name == "AI-Agent-Marketplace"


def test_project_priority_falls_back_to_no_name_label():
    name = render.resolve_display_project(
        custom=None, git_name=None, title="(unnamed)",
        basename="f", local_host="workstation-a", no_name_label="(no name)",
    )
    assert name == "(no name)"


def test_title_identity_uses_legacy_project_suffix_and_drops_account_suffix():
    assert render.title_identity("Resume the task | SampleProject", "workstation-a") == (
        "SampleProject", "Resume the task", None,
    )
    assert render.title_identity("Review checkout flow | devuser42", "workstation-a") == (
        None, "Review checkout flow", None,
    )
    assert render.title_identity("OC | SampleProject widget UI", "workstation-a") == (
        "SampleProject", "widget UI", None,
    )
    assert render.title_identity("(이름 없음)", "workstation-a") == (None, None, None)


def test_project_priority_generic_basename_is_low_confidence():
    name = render.resolve_display_project(
        custom=None, git_name=None, title=None,
        basename="mnt", local_host="workstation-a", no_name_label="(no name)",
    )
    assert name == "(no name)"


# -- title_secondary_line ----------------------------------------------


def test_title_secondary_line_shown_when_meaningful_and_different():
    line = render.title_secondary_line("JuHome", "JuHome Dev", "workstation-a")
    assert line == "JuHome Dev"


def test_title_secondary_line_hidden_when_duplicate_of_project():
    assert render.title_secondary_line("JuHome", "JuHome", "workstation-a") is None
    assert render.title_secondary_line("JuHome", "juhome", "workstation-a") is None  # case-insensitive


def test_title_secondary_line_hidden_when_not_meaningful():
    assert render.title_secondary_line("JuHome", "", "workstation-a") is None
    assert render.title_secondary_line("JuHome", "workstation-a", "workstation-a") is None


def test_default_row_keeps_task_agent_role_together_and_hides_empty_labels():
    from tmux_agent_tower.i18n import t

    parts = render.list_row_parts(
        {"kind": "pane", "display_name": "작업 이어받기", "project": "(이름 없음)",
         "agent": "Codex", "role": "builder", "status": "WORKING"},
        100,
        t,
    )
    assert parts["project"] == "작업 이어받기"
    assert parts["task"] == ""
    assert parts["agent"] == "구현 · Codex"
    assert "(이름 없음)" not in " ".join(parts.values())
    assert "역할 없음" not in " ".join(parts.values())


@pytest.mark.parametrize(
    "display_name,project,group",
    [
        ("Projects Briping", "Tmux Agent Tower", "릴리스 점검"),
        ("TmuxTower", "TmuxTower PM", "TmuxTower"),
    ],
)
def test_user_named_task_has_one_representative_title(display_name, project, group):
    from tmux_agent_tower.i18n import t

    row = {
        "kind": "pane", "name_origin": "user", "display_name": display_name,
        "task_name": display_name, "project": project, "work_group_name": group,
        "agent": "OpenCode", "role": None, "status": "UNKNOWN",
    }
    parts = render.list_row_parts(row, 120, t)

    assert parts["project"] == display_name
    assert parts["task"] == ""
    assert parts["agent"] == "OpenCode"
    assert project not in " ".join(parts.values())
    assert f" · {group}" not in " ".join(parts.values())


def test_execution_labels_need_agent_process_evidence():
    cases = [
        ({"status": "WORKING", "agent_process_alive": True}, "state.working"),
        ({"status": "IDLE", "agent_process_alive": True}, "state.input_waiting"),
        ({"status": "UNKNOWN", "agent_process_alive": True}, "state.running_unknown"),
        ({"status": "WORKING", "agent_process_ended": True}, "state.dead"),
        ({"status": "IDLE", "agent_status_unverified": True}, "state.unknown"),
        ({"status": "DEAD", "agent_status_unverified": True}, "state.dead"),
    ]
    for row, expected_key in cases:
        _symbol, key = render.execution_badge_for_row(row)
        assert key == expected_key


def test_remote_agent_without_process_evidence_is_not_guessed_waiting():
    _symbol, key = render.execution_badge_for_row({
        "status": "IDLE", "remote": True, "agent_status_unverified": True,
    })
    assert key == "state.unknown"


# -- row_matches_filter ----------------------------------------------


def test_filter_empty_matches_everything():
    row = {"project": "JuHome", "agent": "Claude"}
    assert render.row_matches_filter(row, "")


def test_filter_matches_project_case_insensitively():
    row = {"project": "JuHome", "agent": "Claude"}
    assert render.row_matches_filter(row, "juhome")


def test_filter_matches_agent():
    row = {"project": "JuHome", "agent": "Claude"}
    assert render.row_matches_filter(row, "claude")


def test_filter_matches_task_role_and_group_but_hides_internal_location():
    row = {
        "project": "Tower", "display_name": "Remote 복사 개선", "role": "builder",
        "work_group_name": "RC 준비", "path": "/mnt/private", "host": "workstation-b",
        "window_name": "hidden-window",
    }
    assert render.row_matches_filter(row, "remote")
    assert render.row_matches_filter(row, "구현")
    assert render.row_matches_filter(row, "rc 준비")
    assert not render.row_matches_filter(row, "/mnt/private")
    assert not render.row_matches_filter(row, "workstation-b")
    assert not render.row_matches_filter(row, "hidden-window")


def test_filter_no_match_returns_false():
    row = {"project": "JuHome", "agent": "Claude"}
    assert not render.row_matches_filter(row, "nonexistent")


def test_task_row_puts_role_before_project_context():
    parts = render.list_row_parts(
        {"kind": "pane", "display_name": "API 점검", "project": "SamplePortal", "role": "qa", "agent": "Claude", "status": "IDLE"},
        120, lambda key: {"role.qa": "확인", "state.idle": "대기 중"}[key],
    )
    assert parts["project"] == "API 점검"
    assert parts["task"] == "SamplePortal"
    assert parts["agent"] == "확인 · Claude"


# -- format_detail_panel ----------------------------------------------


def test_detail_panel_aligns_labels():
    lines = render.format_detail_panel([("프로젝트", "JuHome"), ("에이전트", "Claude")])
    assert lines == ["프로젝트  JuHome", "에이전트  Claude"]


def test_detail_panel_skips_none_values():
    lines = render.format_detail_panel([("A", "x"), ("B", None), ("C", "y")])
    assert lines == ["A  x", "C  y"]


def test_detail_panel_empty_input_is_empty_output():
    assert render.format_detail_panel([]) == []


def test_detail_panel_aligns_mixed_korean_and_english_labels_by_display_width():
    # Regression: plain len() undercounts Korean (double-width) chars, so
    # "프로젝트" (len 4) and "Host" (len 4) looked equal-width to len() but
    # are not on screen -- caught live as visibly misaligned columns.
    lines = render.format_detail_panel([("프로젝트", "JuHome"), ("Host", "workstation-a")])
    # "프로젝트" is 8 display columns, "Host" is 4 -- Host's line needs 4
    # extra spaces of padding to line up with 프로젝트's line.
    assert lines[0] == "프로젝트  JuHome"
    assert lines[1] == "Host      workstation-a"


# -- display_width / truncate_to_width ----------------------------------


def test_display_width_ascii_is_length():
    assert render.display_width("Host") == 4


def test_display_width_korean_is_double():
    assert render.display_width("프로젝트") == 8


def test_display_width_mixed():
    # "Pane " (5 ASCII cols) + "이" (2) + "름" (2) = 9
    assert render.display_width("Pane 이름") == 9


def test_narrow_help_wraps_complete_korean_key_hints():
    hints = [
        "↑↓ 이동", "Enter 열기", "Space 메뉴", "+ 새 작업", "/ 검색",
        "Y 최신 답변 복사", "G 실제 터미널", "Esc 뒤로", "? 도움말",
    ]
    lines = render.wrap_items(hints, 45)

    assert len(lines) > 1
    assert all(render.display_width(line) <= 45 for line in lines)
    assert "Space 메뉴" in " ".join(lines)
    assert "Y 최신 답변 복사" in " ".join(lines)
    assert "? 도움말" in " ".join(lines)
    assert all("…" not in line for line in lines)


def test_truncate_to_width_ascii_no_truncation_needed():
    assert render.truncate_to_width("short", 20) == "short"


def test_truncate_to_width_ascii_truncates_with_ellipsis():
    result = render.truncate_to_width("a very long project name", 10)
    assert result.endswith("…")
    assert render.display_width(result) <= 10


def test_truncate_to_width_never_splits_a_wide_character():
    # Truncating mid-character would corrupt the string; must stop before it.
    result = render.truncate_to_width("가나다라마바사", 5)
    assert render.display_width(result) <= 5
    # Every character in the result must be a real, complete character
    # from the source string (no partial/garbled output).
    for ch in result.rstrip("…"):
        assert ch in "가나다라마바사"


def test_truncate_to_width_zero_or_negative_is_empty():
    assert render.truncate_to_width("anything", 0) == ""
    assert render.truncate_to_width("anything", -5) == ""


def test_truncate_to_width_never_raises_at_tiny_widths():
    # Part of the resize-to-1x1 regression: width=1 leaves no room even
    # for the ellipsis alone, which must degrade gracefully, not raise.
    for width in (0, 1, 2):
        for text in ("", "a", "가", "한글 프로젝트 이름"):
            result = render.truncate_to_width(text, width)
            assert render.display_width(result) <= max(width, 0)


# -- row_line_count / agent_status_line ----------------------------------


def test_row_line_count_wide_no_title():
    assert render.row_line_count({"title_line": None}, narrow=False) == 1


def test_row_line_count_title_stays_off_the_list():
    assert render.row_line_count({"title_line": "JuHome Dev"}, narrow=False) == 1
    assert render.row_line_count({"title_line": "JuHome Dev"}, narrow=True) == 2


def test_row_line_count_narrow_is_project_and_state_only():
    assert render.row_line_count({"title_line": None, "activity_text": "테스트 실행 중"}, narrow=True) == 2


def test_narrow_row_uses_one_task_name_then_agent_and_status():
    row = {"project": "SamplePortal", "task_name": "로그인 수정", "activity_text": None}
    assert render.row_line_count(row, narrow=True) == 2
    assert render.list_row_parts(
        {"kind": "pane", **row, "agent": "Codex", "status": "WORKING"},
        58,
        lambda _key: "작업 중",
    ) == {"guide": "", "project": "로그인 수정", "task": "SamplePortal", "agent": "", "badge": ""}


def test_role_appears_concisely_without_replacing_task_or_agent():
    labels = {"role.builder": "구현", "role.assigned": "역할: {role}"}
    row = {
        "kind": "pane", "project": "SamplePortal", "task_name": "로그인 수정",
        "role": "builder", "agent": "Codex", "status": "WORKING",
    }
    parts = render.list_row_parts(row, 100, lambda key: labels.get(key, key))

    assert parts["project"] == "로그인 수정"
    assert parts["task"] == "SamplePortal"
    assert parts["agent"] == "구현 · Codex"
    assert render.row_line_count(row, narrow=True) == 2


def test_role_on_a_task_without_a_distinct_task_name_gets_its_own_line():
    row = {"kind": "pane", "project": "SamplePortal", "task_name": "SamplePortal", "role": "qa"}
    parts = render.list_row_parts(row, 58, lambda key: {"role.qa": "확인"}.get(key, key))
    assert parts["project"] == "SamplePortal"
    assert parts["task"] == ""
    assert parts["agent"] == ""
    assert render.row_line_count(row, narrow=True) == 2


def test_agent_status_line_format():
    assert render.agent_status_line("Codex", "작업 중") == "Codex  작업 중"


def test_agent_status_line_with_duration():
    assert render.agent_status_line("Codex", "작업 중", "12m") == "Codex  작업 중 · 12m"


def test_row_line_count_with_activity():
    assert render.row_line_count({"title_line": None, "activity_text": "테스트 실행 중"}, narrow=False) == 2


def test_row_line_count_with_title_and_activity():
    row = {"title_line": "JuHome Dev", "activity_text": "테스트 실행 중"}
    assert render.row_line_count(row, narrow=False) == 2


def test_row_line_count_empty_activity_does_not_add_a_line():
    assert render.row_line_count({"title_line": None, "activity_text": None}, narrow=False) == 1
    assert render.row_line_count({"title_line": None, "activity_text": ""}, narrow=False) == 1


# -- format_duration ----------------------------------------------


def test_format_duration_seconds():
    assert render.format_duration(0) == "0초"
    assert render.format_duration(45) == "45초"
    assert render.format_duration(59) == "59초"


def test_format_duration_minutes():
    assert render.format_duration(60) == "1분"
    assert render.format_duration(90) == "1분"
    assert render.format_duration(12 * 60) == "12분"
    assert render.format_duration(59 * 60 + 59) == "59분"


def test_format_duration_hours():
    assert render.format_duration(3600) == "1시간"
    assert render.format_duration(3600 + 30 * 60) == "1시간 30분"
    assert render.format_duration(7200) == "2시간"


def test_format_duration_never_negative():
    assert render.format_duration(-5) == "0초"


# -- sort_by_attention ----------------------------------------------


def test_sort_by_attention_priority_order():
    rows = [
        {"key": "idle", "status": "IDLE"},
        {"key": "working", "status": "WORKING"},
        {"key": "waiting", "status": "WAITING"},
        {"key": "dead", "status": "DEAD"},
        {"key": "unknown", "status": "UNKNOWN"},
    ]
    sorted_rows = render.sort_by_attention(rows)
    assert [r["key"] for r in sorted_rows] == ["waiting", "unknown", "working", "idle", "dead"]


def test_sort_by_attention_is_stable_within_same_status():
    rows = [
        {"key": "a", "status": "WAITING"},
        {"key": "b", "status": "IDLE"},
        {"key": "c", "status": "WAITING"},
    ]
    sorted_rows = render.sort_by_attention(rows)
    # Both WAITING rows come first, in their original relative order.
    assert [r["key"] for r in sorted_rows] == ["a", "c", "b"]


def test_sort_by_attention_does_not_mutate_the_input_list():
    rows = [{"key": "a", "status": "IDLE"}, {"key": "b", "status": "WAITING"}]
    original_order = [r["key"] for r in rows]
    render.sort_by_attention(rows)
    assert [r["key"] for r in rows] == original_order


# -- responsive thresholds ----------------------------------------------


def test_narrow_layout_threshold():
    assert render.use_narrow_layout(50)
    assert not render.use_narrow_layout(120)


def test_detail_panel_visibility_threshold():
    assert not render.should_show_detail_panel(10)
    assert render.should_show_detail_panel(30)


def test_layout_decisions_never_raise_across_extreme_sizes():
    # Regression guard for a real resize stress-test (down to 1x1, up to
    # 300x100): every pure layout-decision function must return a plain
    # bool/int/str for any terminal size Tower could plausibly see, never
    # raise. draw() itself needs a real curses screen to test directly, but
    # these are exactly the decisions it makes every resize.
    for width in (1, 2, 10, 40, 70, 80, 120, 200, 500):
        for height in (1, 2, 5, 10, 18, 24, 40, 100):
            assert isinstance(render.use_narrow_layout(width), bool)
            assert isinstance(render.should_show_detail_panel(height), bool)

    for row in (
        {"title_line": None},
        {"title_line": "x"},
        {"title_line": "매우 긴 한글 제목 줄 테스트입니다"},
    ):
        for narrow in (True, False):
            count = render.row_line_count(row, narrow)
            assert isinstance(count, int) and count >= 1
