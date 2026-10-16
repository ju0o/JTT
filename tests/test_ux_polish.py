"""The list is a glance. Enums stay out of the marks a person reads."""

import re

from tmux_agent_tower.i18n import _DEFAULT_LANGUAGE, t
from tmux_agent_tower.i18n import ko
from tmux_agent_tower.server import webui
from tmux_agent_tower.ui import render


def _row(**kwargs):
    base = {"status": "IDLE", "attention": "none", "result_state": "none", "project": "타워", "agent": "Codex"}
    base.update(kwargs)
    return base


def test_primary_badge_uses_words_not_enums():
    cases = [
        (_row(status="WORKING"), "●", "작업 중"),
        (_row(attention="approval_required"), "!", "승인 필요"),
        (_row(attention="input_required", interaction={"type": "choice", "options": [{"key": "1"}]}), "?", "선택 필요"),
        (_row(attention="input_required"), "?", "답변 필요"),
        (_row(result_state="ready"), "✓", "새 결과"),
        (_row(status="IDLE"), "○", "대기 중"),
        (_row(status="WORKING", offline=True), "×", "연결 안 됨"),
        (_row(status="DEAD"), "×", "종료"),
        (_row(status="UNKNOWN"), "◇", "확인 불가"),
        (_row(attention="error"), "!", "오류"),
    ]
    for row, symbol, word in cases:
        mark, key = render.primary_badge(row)
        assert mark == symbol
        assert t(key) == word
        assert t(key) not in {"WORKING", "IDLE", "UNKNOWN", "DEAD", "WAITING"}


def test_primary_status_wording_is_korean_and_keeps_three_state_families_separate():
    assert _DEFAULT_LANGUAGE == "ko"
    assert ko.STRINGS["state.working"] == "작업 중"
    assert ko.STRINGS["state.approval"] == "승인 필요"
    assert ko.STRINGS["state.choice"] == "선택 필요"
    assert ko.STRINGS["state.answer"] == "답변 필요"
    assert ko.STRINGS["state.result"] == "새 결과"
    assert ko.STRINGS["state.idle"] == "대기 중"
    assert ko.STRINGS["state.offline"] == "연결 안 됨"
    assert ko.STRINGS["state.dead"] == "종료"
    assert ko.STRINGS["state.unknown"] == "확인 불가"

    row = _row(status="WORKING", attention="approval_required", result_state="ready")
    assert render.detail_badges(row) == [
        ("!", "state.approval"),
        ("✓", "state.result"),
        ("●", "state.working"),
    ]
    assert render.detail_badges(_row(status="WORKING", offline=True)) == [("×", "state.offline")]


def test_primary_korean_menus_hide_tmux_and_transport_terms():
    keys = (
        "app.title", "home.title", "state.offline",
        "hint.move", "hint.open", "hint.enter_expand", "hint.enter_conversation", "hint.enter_window",
        "hint.enter_start", "hint.enter_unavailable", "hint.home_help", "hint.rename", "hint.refresh", "hint.quit",
        "hint.filter", "hint.filter_clear", "hint.navigator", "hint.actions",
        "hint.create", "hint.remote", "hint.attention", "hint.live", "hint.saved",
        "hint.result", "hint.terminal", "hint.back", "hint.help", "hint.settings",
        "help.title", "help.navigation", "help.actions", "help.saved", "help.results", "help.copy_safety",
        "help.copy_safety_block", "help.remote", "help.remote_limits", "help.manage",
        "help.detail", "help.detail_advanced", "help.detail_reject", "help.troubleshoot", "help.advanced",
        "help.start", "help.existing", "help.destinations",
        "nav.conversation", "nav.show_result", "nav.copy_result", "nav.task_settings",
        "nav.copy_screen", "nav.live_view", "nav.settings", "nav.saved", "nav.terminal_structure", "nav.end_task",
        "menu.advanced_names", "menu.project_name", "menu.agent_name", "menu.pane_title",
        "detail.title", "detail.project", "detail.task_name", "detail.agent", "detail.role",
        "detail.status", "detail.attention", "detail.result", "detail.advanced",
        "nav.window", "nav.window_row", "nav.via_tmux", "nav.peer_tmux", "nav.pane_count", "nav.stale",
        "enter.no_selection", "enter.offline", "enter.stale", "enter.remote_readonly", "enter.window_missing", "enter.unavailable",
        "zero.title", "zero.description", "zero.footer", "zero.task", "zero.group", "zero.saved", "zero.template",
        "wizard.current_folder_title", "wizard.current_folder", "wizard.other_project",
        "wizard.agent_installed", "wizard.agent_missing", "wizard.agent_missing_title",
        "wizard.agent_missing_action", "wizard.agent_login_hint", "wizard.project_missing", "wizard.launch_failed",
        "workset.saved_help", "workset.template_help", "workset.empty_saved", "workset.empty_template",
        "settings.language", "settings.agents",
        "struct.window_title", "struct.pane_menu", "struct.add_pane", "struct.rename",
        "struct.manage_panes", "struct.close_window", "struct.close_window_title", "struct.close_window_ask",
        "struct.close_window_effect", "struct.close_window_yes", "struct.act_prompt", "struct.act_close",
        "control.title", "control.hint", "control.close_title", "control.close_idle", "control.close_working",
        "control.close_waiting", "control.close_yes", "control.tower_protected", "control.approve_unknown",
        "control.copy_requires_pane", "control.result_copy_unavailable", "control.copy_connection_error",
        "settings.keys", "settings.keys.title", "settings.keys.explain_off", "settings.keys.failed",
        "settings.advanced",
    )
    internal_terms = re.compile(r"\b(?:tmux|session|window(?:_id)?|pane(?:_id)?|transport|osc52|binding)\b", re.I)
    unexplained_english = re.compile(r"\b(?:LIVE|Prompt|Close|Workspace|Host)\b", re.I)

    for key in keys:
        value = ko.STRINGS[key]
        assert not internal_terms.search(value), (key, value)
        assert not unexplained_english.search(value), (key, value)

    assert ko.STRINGS["hint.rename"] == "E 이름 바꾸기"
    assert ko.STRINGS["struct.rename"].startswith("고급:")


def test_phone_saved_work_and_templates_share_one_named_entry():
    html = webui.PAGE_HTML
    assert '<h2>저장한 구성</h2><p>전에 하던 프로젝트와 Agent 구성을 다시 엽니다.</p>' in html
    assert '<p>템플릿</p>' in html


def test_wide_primary_row_shows_project_agent_and_state_without_internal_ids():
    row = _row(kind="pane", project="쥬포탈", task_name="로그인 수정", agent="Codex", status="WORKING", pane_id="%42", path="/private/path")
    parts = render.list_row_parts(row, 100, t)

    assert parts["project"] == "로그인 수정"
    assert parts["agent"] == "Codex"
    assert parts["badge"] == "● 작업 중"
    assert "%42" not in " ".join(parts.values())
    assert "/private/path" not in " ".join(parts.values())


def test_detail_can_show_more_than_one_badge():
    row = _row(status="IDLE", attention="approval_required", result_state="ready")
    labels = [t(key) for _symbol, key in render.detail_badges(row)]
    assert labels == ["승인 필요", "새 결과", "대기 중"]


def test_watch_header_hides_zero_counts():
    rows = [
        _row(status="WORKING"),
        _row(status="WORKING"),
        _row(attention="approval_required"),
        _row(status="IDLE"),
        _row(status="DEAD"),
    ]
    text = render.format_watch_header(render.watch_counts(rows), t)
    assert text == "! 승인 필요 1   ● 작업 중 2"
    assert "입력" not in text
    assert "결과" not in text
    assert render.format_watch_header(render.watch_counts([_row()]), t) == ""


def test_long_korean_project_truncates_on_display_width():
    name = "한글프로젝트이름아주길게"
    shown = render.truncate_to_width(name, 8)
    assert render.display_width(shown) <= 8
    assert shown.endswith("…")


def test_control_actions_omit_unavailable_writes_and_ignore_navigation():
    plain = render.control_actions(_row(key="%4", pane_id="%4"))
    assert ("A", True) not in plain
    assert ("N", True) not in plain
    assert ("P", True) in plain
    assert ("G", True) in plain
    remote = dict(render.control_actions(_row(key="workstation-b:%1", remote=True, pane_id="%1")))
    assert remote["G"] is False
    assert remote["X"] is False
    approved = dict(render.control_actions(_row(key="%4", approval_known=True, reject_known=True)))
    assert approved["A"] is True and approved["N"] is True


def test_phone_home_is_compact_and_cards_do_not_write():
    html = webui.PAGE_HTML
    assert 'id="counts"' in html
    assert "if (counts.approval)" in html
    assert "if (counts.choice)" in html
    assert "if (counts.answer)" in html
    assert "if (counts.result)" in html
    assert "if (counts.working)" in html
    render_fn = html.split("function render(")[1].split("function pollList")[0]
    card = render_fn.split('card.addEventListener("click"')[1].split("list.appendChild(card)")[0]
    assert "openDetail(p.key)" in card
    assert "fetch(" not in card
    assert "/api/prompt" not in card
    assert "승인 필요" in html and "◇ 확인 불가" in html
    assert "? 답변 필요" in html and "? 선택 필요" in html
    assert "<title>에이전트 관제탑</title>" in html
    assert "작업 이름" in html and "Agent · 역할" in html
    assert "Tmux Agent Tower" not in html
    assert "최신으로" in html and "followLive" in html
    assert 'id="d-location"' in html
    assert 'id="d-host"' in html and 'id="d-title"' in html


def test_narrow_layout_keeps_agent_and_status_together():
    row = _row(status="WORKING", agent="Codex")
    mark, key = render.primary_badge(row)
    line = render.agent_status_line(row["agent"], f"{mark} {t(key)}")
    assert line == "Codex  ● 작업 중"
    assert render.use_narrow_layout(50)


def test_phone_detail_keeps_technical_location_under_advanced_details():
    html = webui.PAGE_HTML
    section = html.split('<details class="advanced">', 1)[1].split("</details>", 1)[0]
    assert "고급 정보" in section
    assert 'id="d-location"' in section
    assert "터미널 위치" in html and "Session" in html and "Pane" in html
