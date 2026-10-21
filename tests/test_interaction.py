"""Interaction keys come only from the current widget, and only when measured."""

import time

from tmux_agent_tower.adapters.base import PaneContext
from tmux_agent_tower.adapters.claude import ClaudeAdapter
from tmux_agent_tower.adapters.codex import CodexAdapter
from tmux_agent_tower.adapters.cursor import CursorAdapter
from tmux_agent_tower.control import actions
from tmux_agent_tower.detection.interaction import (
    InteractionOption,
    approval,
    choice,
    confirm,
    text_question,
    unknown,
)
from tmux_agent_tower.server import webui
from tmux_agent_tower.ui import control_view
from tmux_agent_tower.ui.control_view import _attention_label
from tmux_agent_tower.ui.tower import _interaction_payload

PAUSE = object()


class ViewScreen:
    def __init__(self, keys, key_delay=0.03):
        self.keys = iter(keys)
        self.written = []
        self.height, self.width = 24, 80
        self.key_delay = key_delay

    def getmaxyx(self):
        return self.height, self.width

    def timeout(self, _ms):
        pass

    def get_wch(self):
        try:
            key = next(self.keys)
            if key is PAUSE:
                raise __import__("curses").error("timeout")
            if self.key_delay:
                time.sleep(self.key_delay)
            return key
        except StopIteration:
            raise __import__("curses").error("timeout")

    def erase(self):
        self.written.clear()

    def addnstr(self, _y, _x, text, _n, _attr=0):
        self.written.append(text)

    def move(self, *_):
        pass

    def noutrefresh(self):
        pass


class ViewTower:
    config = {}
    session = "private-test"
    own_pane_id = "%999"

    def __init__(self, row):
        self.rows = [row]
        self.visible_rows = [row]
        self.selected = 0

    def load(self):
        pass


def _open_view(monkeypatch, row, keys):
    monkeypatch.setattr(control_view, "get_pane_screen", lambda *_: (True, "", {"lines": ["live"]}))
    monkeypatch.setattr(control_view.curses, "doupdate", lambda: None)
    monkeypatch.setattr(control_view, "_set_bracketed_paste", lambda *_: None)
    control_view.open_control_view(ViewScreen(keys), ViewTower(row), row["key"])


def _ctx(lines, command="codex", title=""):
    return PaneContext(title=title, command=command, lines=tuple(lines))


CODEX_MENU = [
    "Would you like to run the following command?",
    "1. Yes, proceed",
    "2. Yes, and don't ask again",
    "3. No",
]


def test_codex_numbered_menu_exposes_only_the_measured_keys():
    found = CodexAdapter().detect_interaction(_ctx(CODEX_MENU))
    assert found.type == "approval"
    assert [item.key for item in found.options] == ["1", "3"]
    assert found.verified_key("2") is None
    assert found.verified_key("1") == "1"


def test_codex_three_visible_choices_do_not_invent_the_middle_key():
    found = CodexAdapter().detect_interaction(_ctx(CODEX_MENU))
    assert "2" not in [item.key for item in found.options]


def test_codex_trust_uses_the_footer_bindings():
    lines = ["Trust this folder?", "1. Trust this folder", "enter continue · esc quit"]
    found = CodexAdapter().detect_interaction(_ctx(lines))
    assert [item.key for item in found.options] == ["Enter", "Escape"]


def test_codex_question_is_text_and_not_a_guessed_key():
    lines = ["What should the function be called?"]
    found = CodexAdapter().detect_interaction(_ctx(lines))
    assert found.type == "text" and found.input_allowed and found.options == ()


def test_working_codex_hides_an_older_menu():
    lines = CODEX_MENU + ["Working (2s • esc to interrupt)"]
    adapter = CodexAdapter()
    ctx = _ctx(lines)
    assert adapter.detect_attention(ctx) == "none"
    assert adapter.detect_interaction(ctx) is None


def test_idle_hint_hides_a_scrolled_menu():
    lines = CODEX_MENU + ["› Ask Codex to do anything"]
    assert CodexAdapter().detect_interaction(_ctx(lines)) is None


def test_claude_proceed_menu_uses_measured_numbers():
    lines = ["Do you want to proceed?", "1. Yes", "3. No", "❯"]
    found = ClaudeAdapter().detect_interaction(_ctx(lines, "claude"))
    assert found is not None
    assert [item.key for item in found.options] == ["1", "3"]


def test_claude_trust_without_a_numbered_key_stays_unknown():
    lines = ["Is this a project you created or one you trust?", "Yes, I trust this folder"]
    found = ClaudeAdapter().detect_interaction(_ctx(lines, "claude"))
    assert found.type == "unknown"
    assert found.options == ()


def test_claude_text_question_reuses_the_composer_path():
    lines = ["Which file should I open?", "❯"]
    found = ClaudeAdapter().detect_interaction(_ctx(lines, "claude"))
    assert found.type == "text" and found.input_allowed


def test_cursor_yn_is_unverified_until_the_key_is_measured():
    lines = ["Run this command?", "(y/n)"]
    found = CursorAdapter().detect_interaction(_ctx(lines, "cursor-agent"))
    assert found.type == "confirm"
    assert found.options == ()
    assert found.verified_key("y") is None
    assert found.verified_key("n") is None


def test_interaction_categories_remain_distinct_and_unmeasured_confirm_has_no_keys():
    yes = InteractionOption("Yes", "1")
    no = InteractionOption("No", "3")
    assert approval("Proceed?", [yes], "measured").type == "approval"
    assert choice("Choose", [yes, InteractionOption("Other", "2")], "measured").type == "choice"
    assert confirm("Proceed?", yes, no, "measured").type == "confirm"
    unmeasured = confirm("Proceed?", None, None, "unmeasured")
    assert unmeasured.type == "confirm" and unmeasured.options == ()
    assert unmeasured.verified_key("y") is None
    assert text_question("Which file?", "measured").type == "text"
    assert unknown("Prompt?", "unmapped").type == "unknown"


def test_respond_interaction_sends_only_a_key_the_fresh_screen_still_lists(monkeypatch):
    sent = []
    monkeypatch.setattr(actions, "find_prompt_target", lambda *a: (True, "", "%7"))
    monkeypatch.setattr(actions, "send_attention_key", lambda pane, key: sent.append(key) or True)
    monkeypatch.setattr(
        actions,
        "_attention_now",
        lambda pane: (CodexAdapter(), _ctx(CODEX_MENU)),
    )
    assert actions.respond_interaction(object(), "%7", "1") == (True, "")
    assert actions.respond_interaction(object(), "%7", "2")[0] is False
    assert sent == ["1"]


def test_cursor_yn_never_sends_a_guessed_key(monkeypatch):
    sent = []
    monkeypatch.setattr(actions, "find_prompt_target", lambda *a: (True, "", "%7"))
    monkeypatch.setattr(actions, "send_attention_key", lambda pane, key: sent.append(key) or True)
    monkeypatch.setattr(
        actions,
        "_attention_now",
        lambda pane: (CursorAdapter(), _ctx(["Run this command?", "(y/n)"], "cursor-agent")),
    )
    assert actions.respond_interaction(object(), "%7", "y") == (False, "interaction_unknown")
    assert actions.respond_interaction(object(), "%7", "n") == (False, "interaction_unknown")
    assert actions.respond_attention(object(), "%7", "approve") == (False, "approval_unknown")
    assert actions.respond_attention(object(), "%7", "reject") == (False, "approval_unknown")
    assert sent == []


def test_attention_without_adapter_evidence_is_projected_as_unknown():
    found = _interaction_payload(
        CursorAdapter(),
        _ctx(["ordinary screen"], "cursor-agent"),
        "approval_required",
    )
    assert found["type"] == "unknown"
    assert found["confidence"] == "low"


def test_unmeasured_confirm_is_shown_as_unverified_in_both_views():
    confirm_row = {"attention": "approval_required", "interaction": {"type": "confirm", "options": []}}
    unknown_row = {"attention": "approval_required", "interaction": {"type": "unknown"}}
    assert _attention_label(confirm_row) == _attention_label(unknown_row)
    assert 'item.type === "confirm"' in webui.PAGE_HTML
    assert "확인이 필요합니다. 실제 터미널에서 선택해주세요." in webui.PAGE_HTML


def test_detail_shows_measured_choices_and_withholds_unknown_actions(monkeypatch):
    class Screen:
        written = []

        def erase(self):
            self.written.clear()

        def getmaxyx(self):
            return 24, 80

        def addnstr(self, _y, _x, text, _n, _attr=0):
            self.written.append(text)

        def move(self, *_):
            pass

        def noutrefresh(self):
            pass

    class Tower:
        config = {}
        session = "private-test"
        own_pane_id = "%999"

    screen = Screen()
    monkeypatch.setattr(control_view, "get_pane_screen", lambda *_: (True, "", {"lines": ["live"]}))
    monkeypatch.setattr(control_view.curses, "doupdate", lambda: None)
    tower = Tower()
    choice_row = {
        "key": "%1", "agent": "Codex", "status": "WAITING", "attention": "input_required",
        "interaction": {"type": "choice", "options": [{"key": "1", "label": "Use plan", "safe": True}]},
    }
    control_view._draw(screen, tower, choice_row, "", False)
    assert any("1: Use plan" in text for text in screen.written)
    assert not any("메시지" in text for text in screen.written)

    unknown_row = {
        "key": "%1", "agent": "Codex", "status": "WAITING", "attention": "approval_required",
        "interaction": {"type": "unknown", "options": [], "input_allowed": False},
    }
    control_view._draw(screen, tower, unknown_row, "", False)
    assert any("Tower가 응답을 확인할 수 없습니다" in text for text in screen.written)
    assert not any("Ctrl+A" in text or "메시지" in text for text in screen.written)

    remote_row = {"key": "workstation-b:%1", "agent": "Codex", "status": "IDLE", "remote": True, "interaction": {}}
    control_view._draw(screen, tower, remote_row, "", False)
    assert any("다른 컴퓨터 작업은 여기서 입력·터미널 조작을 지원하지 않습니다" in text for text in screen.written)
    assert not any("메시지" in text or "Ctrl+G" in text for text in screen.written)


def test_detail_text_question_uses_inline_answer_composer(monkeypatch):
    class Screen:
        written = []

        def erase(self):
            self.written.clear()

        def getmaxyx(self):
            return 24, 80

        def addnstr(self, _y, _x, text, _n, _attr=0):
            self.written.append(text)

        def move(self, *_):
            pass

        def noutrefresh(self):
            pass

    class Tower:
        config = {}
        session = "private-test"
        own_pane_id = "%999"

    monkeypatch.setattr(control_view, "get_pane_screen", lambda *_: (True, "", {"lines": ["live"]}))
    monkeypatch.setattr(control_view.curses, "doupdate", lambda: None)
    screen = Screen()
    control_view._draw(screen, Tower(), {
        "key": "%1", "agent": "Codex", "status": "WAITING", "attention": "input_required",
        "interaction": {"type": "text", "input_allowed": True},
    }, "", False)
    assert any("답변" in text for text in screen.written)


def test_persistent_view_uses_only_a_measured_safe_choice(monkeypatch):
    row = {
        "key": "%1", "agent": "Codex", "status": "WAITING", "attention": "approval_required",
        "interaction": {"type": "approval", "options": [{"key": "1", "label": "Proceed", "safe": True}]},
    }
    sent = []
    monkeypatch.setattr(control_view, "respond_interaction", lambda *_: sent.append("measured-choice") or (True, ""))
    monkeypatch.setattr(control_view, "send_prompt", lambda *_: sent.append("ordinary-text"))

    _open_view(monkeypatch, row, ["1", "\x1b"])

    assert sent == ["measured-choice"]


def test_persistent_view_sends_text_answer_through_shared_action(monkeypatch):
    row = {
        "key": "%1", "agent": "Claude", "status": "WAITING", "attention": "input_required",
        "interaction": {"type": "text", "input_allowed": True},
    }
    sent = []
    drawn = []
    monkeypatch.setattr(control_view, "_draw", lambda *args: drawn.append((args[8].text, args[3])) or 1)
    monkeypatch.setattr(
        control_view,
        "respond_interaction_text",
        lambda _tower, _key, text: sent.append(text) or actions.SubmitResult(True, submitted=False, reason="sent"),
    )

    _open_view(monkeypatch, row, list("answer") + ["\n", PAUSE, "\x1b"])

    assert sent == ["answer"]
    assert drawn[-1] == ("", "답변을 전송했습니다.")


def test_unknown_interaction_blocks_text_and_enter_without_guessing(monkeypatch):
    row = {
        "key": "%1", "agent": "Cursor", "status": "WAITING", "attention": "approval_required",
        "interaction": {"type": "unknown", "options": [], "input_allowed": False},
    }
    sent = []
    monkeypatch.setattr(control_view, "respond_interaction", lambda *_: sent.append("choice"))
    monkeypatch.setattr(control_view, "respond_interaction_text", lambda *_: sent.append("answer"))
    monkeypatch.setattr(control_view, "send_prompt", lambda *_: sent.append("ordinary-text"))

    _open_view(monkeypatch, row, list("guess") + ["\n", "\x1b"])

    assert sent == []


def test_phone_page_uses_the_same_interaction_payload():
    assert "renderInteraction" in webui.PAGE_HTML
    assert 'action: "option"' in webui.PAGE_HTML
    assert "interaction" in webui.PAGE_HTML


def test_phone_clears_only_the_unchanged_interaction_answer_after_success():
    handler = webui.PAGE_HTML.split("function postAttentionBody(body)")[1].split("function fillDetailMeta")[0]
    success = handler.split("if (res.status === 200 && res.body && res.body.ok)")[1].split("} else {")[0]
    failed = handler.split("} else {", 1)[1]
    # A draft changed while the request is pending no longer matches body.text.
    assert 'if (body.action === "text" && promptText && promptText.value === body.text) promptText.value = "";' in success
    assert 'promptText.value = ""' not in failed
