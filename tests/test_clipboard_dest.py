"""Copy destination follows the client operating Tower."""

from tmux_agent_tower.adapters.base import ResultCandidate
from tmux_agent_tower.clipboard import CopyOutcome
from tmux_agent_tower.clipboard_dest import (
    COPY_TEST,
    LOCAL_HOST,
    CURRENT_TERMINAL,
    TMUX_BUFFER,
    CopyClient,
    SessionChoice,
    load_preference,
    override_applies,
    preference_configured,
    resolve_plan,
    run_copy_test,
    save_preference,
    should_offer_copy_setup,
    copy_test_notice,
)
from tmux_agent_tower.detection.result import ResultTracker
from tmux_agent_tower.i18n import t
from tmux_agent_tower.ui.control_view import _copy_notice, _copy_result, _copy_screen, _result_copy_notice
import pytest
from tmux_agent_tower.ui.settings_menu import copy_menu_items


BODY = "한글 결과\n둘째\n"


class _Tower:
    def __init__(self):
        self.results = ResultTracker()
        self.results.observe("%9", "IDLE", ResultCandidate(BODY, "fp-1"))
        self.rows = [{
            "key": "%9", "pane_id": "%9", "pane_pid": "900", "session": "isolated",
            "result_state": "ready", "remote": False,
        }]
        self.local_host = "workstation-a"
        self.copy_override = None

    def load(self):
        return None


def _clients(*pairs):
    return tuple(pairs)


def _mock_complete_result(monkeypatch, text=BODY):
    payload = {
        "state": "ready", "text": text, "complete": True,
        "turn_complete": True, "body_complete": True, "fingerprint": "fp-1",
    }
    monkeypatch.setattr(
        "tmux_agent_tower.ui.control_view.get_result",
        lambda *_args, **_kwargs: (True, "", payload),
    )
    return payload


def _remote_workstation_access_context():
    from tmux_agent_tower.access_context import AccessContext

    return AccessContext("a" * 32, "workstation-b", "linux-wayland", "ssh", "workstation-b", "workstation-a-WSL", "bridge", 1.0, "live")


def test_local_access_context_uses_only_a_readback_capable_native_provider(monkeypatch):
    from types import SimpleNamespace
    from tmux_agent_tower.clipboard_dest import observe

    monkeypatch.setattr(
        "tmux_agent_tower.clipboard_dest.resolve_terminal_target",
        lambda *_args, **_kwargs: SimpleNamespace(tty="/dev/pts/7", ambiguous=False, ssh=False),
    )
    monkeypatch.setattr("tmux_agent_tower.clipboard_dest.context_for_process", lambda _pid: None)
    monkeypatch.setattr("tmux_agent_tower.clipboard_dest.native_clipboard_provider", lambda: "wl-copy")
    monkeypatch.setattr("tmux_agent_tower.clipboard_dest.socket.gethostname", lambda: "access-host")

    client, clients = observe(["/dev/pts/7\t77\tattached"])

    assert clients == (("/dev/pts/7", 77),)
    assert client.access_context.transport == "local"
    assert client.access_context.access_host == "access-host"
    assert client.access_context.tower_host == "access-host"
    assert client.access_context.clipboard_capability == "host"


def test_missing_config_is_auto(tmp_path):
    path = tmp_path / "config.toml"
    assert load_preference(path) == "auto"
    assert preference_configured(path) is False
    path.write_text('language = "ko"\n', encoding="utf-8")
    assert load_preference(path) == "auto"
    assert preference_configured(path) is False


def test_save_keeps_other_lines_and_replaces_the_destination(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('language = "ko"\nproject_roots = ["/work"]\n', encoding="utf-8")
    save_preference(LOCAL_HOST, path)
    text = path.read_text(encoding="utf-8")
    assert 'language = "ko"' in text
    assert 'project_roots = ["/work"]' in text
    assert load_preference(path) == LOCAL_HOST
    save_preference("not-a-place", path)
    assert load_preference(path) == LOCAL_HOST
    save_preference("auto", path)
    assert load_preference(path) == "auto"
    assert text.count("language") == 1 or path.read_text(encoding="utf-8").count('language = "ko"') == 1


def test_existing_user_is_not_sent_through_copy_setup():
    assert should_offer_copy_setup(False, False) is False
    assert should_offer_copy_setup(True, False) is True
    assert should_offer_copy_setup(True, True) is False


def test_auto_local_client_uses_this_computer():
    client = CopyClient("/dev/pts/0", 10, False, False)
    plan = resolve_plan("auto", client, _clients(("/dev/pts/0", 10)))
    assert plan.destination == LOCAL_HOST and plan.ask is False and plan.source == "auto"


def test_auto_ssh_client_without_access_client_provider_asks():
    client = CopyClient("/dev/pts/3", 9, True, False)
    plan = resolve_plan("auto", client, _clients(("/dev/pts/3", 9)))
    assert plan.destination == "auto" and plan.ask is True and plan.source == "auto"


def test_auto_ssh_client_with_verified_bridge_uses_only_the_terminal():
    client = CopyClient("/dev/pts/3", 9, True, False, "a" * 32, _remote_workstation_access_context())
    plan = resolve_plan("auto", client, _clients(("/dev/pts/3", 9)))
    assert plan.destination == CURRENT_TERMINAL and plan.ask is False


def test_auto_ssh_copy_test_without_provider_writes_nothing(monkeypatch):
    captured = []

    monkeypatch.setattr("tmux_agent_tower.clipboard_dest.copy_text", lambda *a, **k: captured.append(k))
    monkeypatch.setattr(
        "tmux_agent_tower.clipboard_dest.observe",
        lambda: (CopyClient("/dev/pts/3", 9, True, False), _clients(("/dev/pts/3", 9))),
    )
    assert run_copy_test("auto") == t("copy.choose_destination")
    assert captured == []


def test_auto_bridge_copy_test_passes_only_the_registered_endpoint(monkeypatch):
    from tmux_agent_tower.clipboard_bridge import BridgeRegistration

    captured = {}

    def fake_copy(text, **kwargs):
        captured["text"] = text
        captured.update(kwargs)
        return CopyOutcome(True, False, "terminal")

    monkeypatch.setattr("tmux_agent_tower.clipboard_dest.copy_text", fake_copy)
    monkeypatch.setattr(
        "tmux_agent_tower.clipboard_dest.observe",
        lambda: (CopyClient("/dev/pts/3", 9, True, False, "a" * 32, _remote_workstation_access_context()), _clients(("/dev/pts/3", 9))),
    )
    monkeypatch.setattr(
        "tmux_agent_tower.clipboard_dest.load_bridge_client",
        lambda _instance: BridgeRegistration(43210, "private-token-012345678901234567890"),
    )
    assert run_copy_test("auto") == t("copy.test_sent")
    assert captured["text"] == COPY_TEST
    assert captured["destination"] == CURRENT_TERMINAL
    assert captured["bridge_port"] == 43210
    assert captured["bridge_token"] == "private-token-012345678901234567890"


def test_auto_ambiguous_client_asks_and_does_not_guess():
    client = CopyClient(None, None, False, True)
    clients = _clients(("/dev/pts/1", 1), ("/dev/pts/2", 2))
    plan = resolve_plan("auto", client, clients)
    assert plan.ask is True and plan.destination == "auto"


def test_ambiguous_copy_test_requires_an_explicit_safe_destination(monkeypatch):
    called = []
    clients = _clients(("/dev/pts/1", 1), ("/dev/pts/2", 2))
    monkeypatch.setattr(
        "tmux_agent_tower.clipboard_dest.copy_text",
        lambda *args, **kwargs: called.append(kwargs) or CopyOutcome(False, True, "tmux"),
    )
    monkeypatch.setattr(
        "tmux_agent_tower.clipboard_dest.observe",
        lambda: (CopyClient(None, None, False, True), clients),
    )
    assert run_copy_test("auto") == t("copy.choose_destination")
    assert called == []
    assert run_copy_test("auto", manual_destination=TMUX_BUFFER, manual_clients=clients) == t("control.copied_buffer")
    assert called[-1]["destination"] == TMUX_BUFFER
    assert run_copy_test("auto", manual_destination=LOCAL_HOST, manual_clients=()) == t("copy.choose_destination")


def test_auto_with_no_client_asks():
    plan = resolve_plan("auto", CopyClient(None, None, False, False), ())
    assert plan.ask is True


def test_saved_choice_overrides_what_auto_would_do():
    ssh = CopyClient("/dev/pts/3", 9, True, False)
    clients = _clients(("/dev/pts/3", 9))
    assert resolve_plan(LOCAL_HOST, ssh, clients).destination == LOCAL_HOST
    assert resolve_plan(CURRENT_TERMINAL, ssh, clients).destination == CURRENT_TERMINAL
    assert resolve_plan(TMUX_BUFFER, ssh, clients).destination == TMUX_BUFFER
    assert resolve_plan(LOCAL_HOST, ssh, clients).ask is False


def test_stale_client_override_is_ignored():
    clients = _clients(("/dev/pts/1", 1), ("/dev/pts/2", 2))
    choice = SessionChoice(clients, LOCAL_HOST)
    assert override_applies(choice, clients) is True
    gone = _clients(("/dev/pts/1", 1))
    assert override_applies(choice, gone) is False
    replaced = _clients(("/dev/pts/1", 99), ("/dev/pts/2", 2))
    assert override_applies(choice, replaced) is False
    plan = resolve_plan("auto", CopyClient(None, None, False, True), gone, choice)
    assert plan.ask is True and plan.source == "auto"
    fresh = resolve_plan("auto", CopyClient(None, None, False, True), clients, choice)
    assert fresh.destination == LOCAL_HOST and fresh.ask is False and fresh.source == "session"


def test_messages_name_one_place_and_hide_the_mechanism():
    host = _copy_notice(CopyOutcome(True, False, "clip.exe"), "workstation-a")
    terminal = _copy_notice(CopyOutcome(True, False, "terminal"), "workstation-a")
    box = _copy_notice(CopyOutcome(False, True, "tmux"), "workstation-a")
    failed = _copy_notice(CopyOutcome(False, False, "none"), "workstation-a")
    assert host == "✓ workstation-a 클립보드에 복사했습니다"
    assert terminal == "✓ 현재 접속한 터미널에 복사했습니다"
    assert box == "✓ Tower 복사함에 저장했습니다"
    assert failed == "복사 위치를 확인해주세요"
    for notice in (host, terminal, box, failed):
        assert "OSC" not in notice
        assert "clip.exe" not in notice
        assert "tmux" not in notice.lower()
        assert "Windows" not in notice


def test_result_copy_notice_distinguishes_verified_windows_from_terminal_send():
    verified = _result_copy_notice(CopyOutcome(True, False, "clip.exe"))
    sent = _result_copy_notice(CopyOutcome(False, False, "terminal", terminal_requested=True))

    assert verified == "최신 답변을 Windows 클립보드에 복사했습니다."
    assert sent == "클립보드로 전송했습니다. 붙여넣기로 확인해 주세요."


def test_copy_test_does_not_claim_a_paste(monkeypatch):
    sent = {}

    def fake_copy(text, **kwargs):
        sent["text"] = text
        sent["destination"] = kwargs["destination"]
        return CopyOutcome(True, False, "clip.exe")

    monkeypatch.setattr("tmux_agent_tower.clipboard_dest.copy_text", fake_copy)
    monkeypatch.setattr(
        "tmux_agent_tower.clipboard_dest.observe",
        lambda: (CopyClient("/dev/pts/0", 10, False, False), _clients(("/dev/pts/0", 10))),
    )
    notice = run_copy_test("auto", host_label="workstation-a")
    assert sent["text"] == COPY_TEST
    assert sent["text"] != BODY
    assert sent["destination"] == LOCAL_HOST
    assert notice == t("copy.test_sent")
    assert "확인됨" not in notice
    assert "붙여넣기" in notice


def test_copy_test_does_not_guess_an_ambiguous_client(monkeypatch):
    called = []
    monkeypatch.setattr(
        "tmux_agent_tower.clipboard_dest.copy_text",
        lambda *args, **kwargs: called.append(kwargs),
    )
    monkeypatch.setattr(
        "tmux_agent_tower.clipboard_dest.observe",
        lambda: (CopyClient(None, None, False, True), _clients(("/dev/pts/1", 1), ("/dev/pts/2", 2))),
    )
    notice = run_copy_test("auto")
    assert called == []
    assert notice == t("copy.choose_destination")


def test_settings_menu_leads_with_automatic_and_offers_a_test():
    items = copy_menu_items()
    assert items[0] == ("auto", "자동으로 선택 (추천)")
    labels = [label for _key, label in items]
    assert labels[:4] == [
        "자동으로 선택 (추천)",
        "이 컴퓨터",
        "현재 접속한 터미널",
        "Tower 복사함",
    ]
    assert "복사 테스트" in labels


def test_manual_destination_is_what_y_writes(monkeypatch):
    captured = {}

    def fake_copy(text, **kwargs):
        captured["text"] = text
        captured.update(kwargs)
        return CopyOutcome(False, True, "tmux")

    tower = _Tower()
    _mock_complete_result(monkeypatch)
    monkeypatch.setattr("tmux_agent_tower.ui.control_view.copy_text", fake_copy)
    monkeypatch.setattr("tmux_agent_tower.ui.control_view.load_preference", lambda: TMUX_BUFFER)
    monkeypatch.setattr(
        "tmux_agent_tower.ui.control_view.observe",
        lambda: (CopyClient("/dev/pts/3", 9, True, False), _clients(("/dev/pts/3", 9))),
    )
    notice = _copy_result(tower, "%9")
    assert captured["text"] == BODY
    assert captured["destination"] == TMUX_BUFFER
    assert notice == "✓ 최신 결과 전체를 Tower 복사함에 저장했습니다"
    assert tower.results.snapshot("%9").state == "ready"


def test_y_blocks_partial_result_before_any_clipboard_write(monkeypatch):
    from tmux_agent_tower.ui import control_view

    tower = _Tower()
    tower.rows[0].update({"pane_id": "%9", "pane_pid": "900", "session": "isolated"})
    writes = []
    monkeypatch.setattr(
        control_view, "get_result",
        lambda *_a, **kwargs: (True, "", {
            "state": "none", "text": "private suffix", "complete": False,
            "turn_complete": True, "body_complete": False, "reason_code": "SOURCE_PARTIAL",
        }),
    )
    monkeypatch.setattr(control_view, "copy_text", lambda *args, **kwargs: writes.append(args) or None)

    notice = _copy_result(tower, "%9")

    assert notice == t("control.result_copy_unavailable")
    assert writes == []
    assert tower.last_result_copy_diagnostic["stage"] == "not_entered"
    assert tower.last_result_copy_diagnostic["outcome"] == "blocked"
    assert "text" not in tower.last_result_copy_diagnostic


def test_y_blocks_remote_opencode_result_when_clipboard_sanitizer_changes_bytes(monkeypatch):
    from tmux_agent_tower.ui import control_view

    body = "synthetic \x1b[31mred\x1b[0m result\n"
    assert control_view.plain_clipboard_text(body).encode("utf-8") != body.encode("utf-8")
    payload = {
        "state": "ready", "text": body, "complete": True,
        "turn_complete": True, "body_complete": True, "source": "remote_opencode",
    }
    tower = _Tower()
    tower.rows[0]["remote"] = True
    writes = []
    monkeypatch.setattr(control_view, "get_result", lambda *_a, **_k: (True, "", payload))
    monkeypatch.setattr(
        control_view, "observe",
        lambda: (CopyClient("/dev/pts/1", 1, False, False), (("/dev/pts/1", 1),)),
    )
    monkeypatch.setattr(control_view, "load_preference", lambda: LOCAL_HOST)
    monkeypatch.setattr(
        control_view, "copy_text",
        lambda *args, **kwargs: writes.append((args, kwargs)) or CopyOutcome(True, False, "clip.exe"),
    )

    notice = _copy_result(tower, "%9")

    assert notice == t("control.result_copy_unavailable")
    assert tower.last_result_copy_diagnostic["reason_code"] == "CLIPBOARD_TEXT_TRANSFORMED"
    assert writes == []
    assert tower.last_result_copy_diagnostic["stage"] == "not_entered"
    assert tower.last_result_copy_diagnostic["outcome"] == "blocked"
    assert tower.last_result_copy_diagnostic["reason_code"] == "CLIPBOARD_TEXT_TRANSFORMED"


def test_y_reports_clipboard_delivery_failure_separately(monkeypatch):
    from tmux_agent_tower.ui import control_view

    tower = _Tower()
    payload = {"complete": True, "text": "synthetic complete result", "state": "ready", "turn_complete": True, "body_complete": True}
    monkeypatch.setattr(control_view, "get_result", lambda *_a, **_k: (True, "", payload))
    monkeypatch.setattr(control_view, "_route_clipboard", lambda *_a, **_k: (CopyOutcome(False, False, "none"), ""))

    notice = _copy_result(tower, "%9")

    assert notice == t("control.clipboard_failed")
    assert tower.last_result_copy_diagnostic["reason_code"] == "CLIPBOARD_FAILED"
    assert tower.last_result_copy_diagnostic["stage"] == "entered"
    assert tower.last_result_copy_diagnostic["reason_code"] == "CLIPBOARD_FAILED"


def test_y_explains_a_changed_pane_identity_without_routing(monkeypatch):
    from tmux_agent_tower.ui import control_view

    tower = _Tower()
    routed = []
    monkeypatch.setattr(
        control_view, "get_result",
        lambda *_args, **_kwargs: (False, "stale", {"reason_code": "UNKNOWN"}),
    )
    monkeypatch.setattr(
        control_view, "_route_clipboard", lambda *_args, **_kwargs: routed.append(True),
    )

    notice = _copy_result(tower, "%9")

    assert notice == t("control.copy_pane_changed")
    assert tower.last_result_copy_diagnostic["reason_code"] == "PANE_IDENTITY_CHANGED"
    assert routed == []


def test_y_opt_in_trace_records_only_copy_metadata(tmp_path, monkeypatch):
    import hashlib
    import json

    from tmux_agent_tower.ui import control_view

    body = "합성 한국어\n```py\nprint('safe')\n```\n"
    payload = {
        "state": "ready", "text": body, "complete": True,
        "turn_complete": True, "body_complete": True, "source": "remote_opencode",
    }
    tower = _Tower()
    trace = tmp_path / "copy-trace.jsonl"
    monkeypatch.setenv("TOWER_RESULT_COPY_TRACE_FILE", str(trace))
    monkeypatch.setattr(control_view, "get_result", lambda *_a, **_k: (True, "", payload))
    monkeypatch.setattr(
        control_view, "observe",
        lambda: (CopyClient("/dev/pts/1", 1, False, False), (("/dev/pts/1", 1),)),
    )
    monkeypatch.setattr(control_view, "load_preference", lambda: LOCAL_HOST)

    def fake_copy(_text, **kwargs):
        callback = kwargs["diagnostic_callback"]
        assert callback("local_host", "clip.exe", len(body.encode("utf-8")), hashlib.sha256(body.encode("utf-8")).hexdigest())
        return CopyOutcome(True, False, "clip.exe")

    monkeypatch.setattr(control_view, "copy_text", fake_copy)
    assert "복사했습니다" in control_view._copy_result(tower, "%9")

    records = [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()]
    names = [record["event"] for record in records]
    assert names == [
        "Y_HANDLER_ENTERED", "GET_RESULT_COMPLETE", "ROUTER_ENTERED", "ACCESS_CLIENT_STATE",
        "SELECTED_DESTINATION", "CLIPBOARD_WRITE_ATTEMPTED", "Y_RETURN_REASON",
    ]
    expected_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
    result = next(record for record in records if record["event"] == "GET_RESULT_COMPLETE")
    router = next(record for record in records if record["event"] == "ROUTER_ENTERED")
    write = next(record for record in records if record["event"] == "CLIPBOARD_WRITE_ATTEMPTED")
    assert result["result_utf8_bytes"] == len(body.encode("utf-8"))
    assert result["result_sha256"] == router["router_payload_sha256"] == expected_hash
    assert write["clipboard_payload_sha256"] == expected_hash
    assert trace.stat().st_mode & 0o777 == 0o600
    assert body not in trace.read_text(encoding="utf-8")


def test_y_trace_failure_blocks_result_lookup_and_copy(tmp_path, monkeypatch):
    from tmux_agent_tower.ui import control_view

    tower = _Tower()
    monkeypatch.setenv("TOWER_RESULT_COPY_TRACE_FILE", str(tmp_path))
    monkeypatch.setattr(control_view, "get_result", lambda *_a, **_k: pytest.fail("trace failure must stop before lookup"))
    monkeypatch.setattr(control_view, "copy_text", lambda *_a, **_k: pytest.fail("trace failure must block copy"))

    notice = control_view._copy_result(tower, "%9")

    assert notice == t("control.clipboard_failed")
    assert tower.last_result_copy_diagnostic["reason_code"] == "DIAGNOSTIC_TRACE_FAILED"


def test_y_trace_does_not_record_or_route_a_partial_result(tmp_path, monkeypatch):
    import json

    from tmux_agent_tower.ui import control_view

    body = "synthetic partial body"
    tower = _Tower()
    trace = tmp_path / "partial-trace.jsonl"
    monkeypatch.setenv("TOWER_RESULT_COPY_TRACE_FILE", str(trace))
    monkeypatch.setattr(control_view, "get_result", lambda *_a, **_k: (True, "", {
        "state": "none", "text": body, "complete": False,
        "turn_complete": True, "body_complete": False, "source": "SOURCE_PARTIAL",
    }))
    monkeypatch.setattr(control_view, "_route_clipboard", lambda *_a, **_k: pytest.fail("partial result routed"))

    notice = control_view._copy_result(tower, "%9")

    records = [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()]
    result = next(record for record in records if record["event"] == "GET_RESULT_COMPLETE")
    assert notice == t("control.result_copy_unavailable")
    assert result["result_complete"] is False
    assert result["result_utf8_bytes"] == 0
    assert result["result_sha256"] == ""
    assert all(record["event"] != "CLIPBOARD_WRITE_ATTEMPTED" for record in records)
    assert body not in trace.read_text(encoding="utf-8")


def test_y_fails_closed_even_with_a_live_multi_client_override(monkeypatch):
    calls = []

    def fake_copy(text, **kwargs):
        calls.append(kwargs["destination"])
        return CopyOutcome(True, False, "clip.exe")

    clients = _clients(("/dev/pts/1", 1), ("/dev/pts/2", 2))
    tower = _Tower()
    _mock_complete_result(monkeypatch)
    tower.copy_override = SessionChoice(clients, LOCAL_HOST)
    monkeypatch.setattr("tmux_agent_tower.ui.control_view.copy_text", fake_copy)
    monkeypatch.setattr("tmux_agent_tower.ui.control_view.load_preference", lambda: "auto")
    monkeypatch.setattr(
        "tmux_agent_tower.ui.control_view.observe",
        lambda: (CopyClient(None, None, False, True), clients),
    )
    notice = _copy_result(tower, "%9")
    assert calls == []
    assert notice == "복사할 위치를 선택해주세요"

    monkeypatch.setattr(
        "tmux_agent_tower.ui.control_view.observe",
        lambda: (CopyClient(None, None, False, True), _clients(("/dev/pts/1", 1))),
    )
    notice = _copy_result(tower, "%9")
    assert calls == []
    assert notice == "복사할 위치를 선택해주세요"


def test_y_prompts_a_safe_destination_when_multiple_clients_are_attached(monkeypatch):
    calls = []
    prompts = []

    def fake_copy(text, **kwargs):
        calls.append(kwargs["destination"])
        return CopyOutcome(True, False, "clip.exe")

    tower = _Tower()
    _mock_complete_result(monkeypatch)
    monkeypatch.setattr("tmux_agent_tower.ui.control_view.copy_text", fake_copy)
    monkeypatch.setattr("tmux_agent_tower.ui.control_view.load_preference", lambda: "auto")
    monkeypatch.setattr(
        "tmux_agent_tower.ui.control_view._ask_copy_destination",
        lambda stdscr, *, allow_terminal=True: prompts.append((stdscr, allow_terminal)) or "local_host",
    )
    clients = _clients(("/dev/pts/1", 1), ("/dev/pts/2", 2))
    monkeypatch.setattr(
        "tmux_agent_tower.ui.control_view.observe",
        lambda: (CopyClient(None, None, False, True), clients),
    )
    screen = object()
    notice = _copy_result(tower, "%9", stdscr=screen)
    assert calls == ["local_host"]
    assert prompts == [(screen, False)]
    assert notice == t("control.copied_complete_windows")
    assert tower.copy_override == SessionChoice(clients, LOCAL_HOST)


def test_y_uses_the_registered_remote_workstation_bridge_and_preserves_result_hash(monkeypatch):
    import hashlib

    from tmux_agent_tower.clipboard_bridge import BridgeRegistration

    captured = {}

    def fake_copy(text, **kwargs):
        captured["text"] = text
        captured.update(kwargs)
        return CopyOutcome(True, False, "terminal")

    tower = _Tower()
    _mock_complete_result(monkeypatch)
    clients = _clients(("/dev/pts/3", 9))
    monkeypatch.setattr("tmux_agent_tower.ui.control_view.copy_text", fake_copy)
    monkeypatch.setattr("tmux_agent_tower.ui.control_view.load_preference", lambda: "auto")
    monkeypatch.setattr(
        "tmux_agent_tower.ui.control_view.observe",
        lambda: (CopyClient("/dev/pts/3", 9, True, False, "a" * 32, _remote_workstation_access_context()), clients),
    )
    monkeypatch.setattr(
        "tmux_agent_tower.ui.control_view.load_bridge_client",
        lambda _instance: BridgeRegistration(43210, "private-token-012345678901234567890"),
    )
    notice = _copy_result(tower, "%9")
    assert captured["destination"] == CURRENT_TERMINAL
    assert captured["bridge_port"] == 43210
    assert captured["text"] == BODY
    assert hashlib.sha256(captured["text"].encode()).hexdigest() == hashlib.sha256(BODY.encode()).hexdigest()
    assert notice == "✓ 최신 결과 전체를 현재 접속한 터미널에 복사했습니다"
    assert tower.results.snapshot("%9").state == "read"


def test_remote_result_source_still_routes_to_local_access_clipboard(monkeypatch):
    from tmux_agent_tower.access_context import AccessContext

    payload = {
        "complete": True,
        "text": "원격 결과\n```python\nprint('workstation-b')\n```",
        "fingerprint": "remote-fingerprint",
        "source": "remote_tmux",
        "turn_identity": "workstation-b:$1:%9:123:remote-fingerprint",
        "timestamp": 1.0,
    }
    context = AccessContext(
        "b" * 32, "workstation-a", "windows-wsl", "local", "workstation-a", "workstation-a-WSL", "host", 1.0, "live"
    )
    captured = {}
    tower = _Tower()
    tower.rows = [{
        "key": "workstation-b:%9", "pane_id": "%9", "remote": True,
        "result_provider_type": "remote_tmux", "pane_pid": "",
    }]
    get_result_calls = []
    monkeypatch.setattr(
        "tmux_agent_tower.ui.control_view.get_result",
        lambda *_args, **_kwargs: get_result_calls.append((_args, _kwargs)) or (True, "", payload),
    )
    monkeypatch.setattr(
        "tmux_agent_tower.ui.control_view.observe",
        lambda: (CopyClient("/dev/pts/1", 4, False, False, access_context=context), _clients(("/dev/pts/1", 4))),
    )
    monkeypatch.setattr("tmux_agent_tower.ui.control_view.load_preference", lambda: "auto")

    def fake_copy(text, **kwargs):
        captured["text"] = text
        captured.update(kwargs)
        return CopyOutcome(True, False, "clip.exe")

    monkeypatch.setattr("tmux_agent_tower.ui.control_view.copy_text", fake_copy)
    notice = _copy_result(tower, "workstation-b:%9")

    assert captured["text"] == payload["text"]
    assert captured["destination"] == LOCAL_HOST
    assert notice == t("control.copied_complete_windows")
    assert get_result_calls and get_result_calls[0][1] == {}


def test_y_does_not_route_a_partial_tracker_candidate(monkeypatch):
    tower = _Tower()
    routed = []
    monkeypatch.setattr(
        "tmux_agent_tower.ui.control_view.get_result",
        lambda *_args, **_kwargs: (True, "", {
            "state": "none", "text": "", "complete": False,
            "turn_complete": False, "body_complete": False, "reason_code": "TURN_NOT_COMPLETE",
        }),
    )
    monkeypatch.setattr(
        "tmux_agent_tower.ui.control_view._route_clipboard",
        lambda *_args, **_kwargs: routed.append(True),
    )

    notice = _copy_result(tower, "%9")

    assert notice == t("control.result_copy_unavailable")
    assert routed == []


@pytest.mark.parametrize(
    ("source", "message_key"),
    [
        ("REMOTE_PROVIDER_UNBOUND", "control.opencode_no_active_session"),
        ("REMOTE_PROVIDER_UNAVAILABLE", "control.copy_connection_error"),
        ("REMOTE_PROVIDER_TIMEOUT", "control.copy_connection_error"),
        ("TURN_NOT_COMPLETE", "control.result_copy_unavailable"),
    ],
)
def test_y_displays_friendly_provider_failure_without_copying(monkeypatch, source, message_key):
    routed = []
    tower = _Tower()
    monkeypatch.setattr(
        "tmux_agent_tower.ui.control_view.get_result",
        lambda *_args, **_kwargs: (True, "", {"complete": False, "text": "", "source": source}),
    )
    monkeypatch.setattr(
        "tmux_agent_tower.ui.control_view._route_clipboard",
        lambda *_args, **_kwargs: routed.append(True),
    )

    notice = _copy_result(tower, "%9")

    assert notice == t(message_key)
    assert source not in notice
    assert routed == []
    assert tower.last_result_copy_diagnostic["reason_code"] == source


def test_y_fails_closed_when_local_pane_pid_is_missing(monkeypatch):
    from tmux_agent_tower.ui import control_view

    tower = _Tower()
    tower.rows[0]["pane_pid"] = ""
    routed = []
    monkeypatch.setattr(control_view, "get_result", lambda *_a, **_k: pytest.fail("missing PID must not resolve a Result"))
    monkeypatch.setattr(control_view, "_route_clipboard", lambda *_a, **_k: routed.append(True))

    notice = _copy_result(tower, "%9")

    assert notice == t("control.result_copy_unavailable")
    assert routed == []
    assert tower.last_result_copy_diagnostic["outcome"] == "blocked"


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("REMOTE_PROVIDER_UNBOUND", "REMOTE_PROVIDER_UNBOUND"),
        ("REMOTE_PROVIDER_UNAVAILABLE", "REMOTE_PROVIDER_UNAVAILABLE"),
        ("REMOTE_RESULT_INCOMPLETE", "REMOTE_RESULT_INCOMPLETE"),
        ("REMOTE_UNRECOGNIZED_STATUS", "UNKNOWN"),
    ],
)
def test_remote_provider_failure_reasons_remain_distinct(source, expected):
    from tmux_agent_tower.ui.control_view import _result_failure_code

    assert _result_failure_code({}, {"source": source}) == expected


def test_screen_copy_is_a_separate_explicit_payload(monkeypatch):
    routed = []
    tower = _Tower()
    tower.rows[0]["pane_pid"] = "123"
    captured = []
    monkeypatch.setattr(
        "tmux_agent_tower.ui.control_view.get_pane_screen",
        lambda *args, **kwargs: captured.append((args, kwargs)) or (True, "", {"lines": ["visible one", "visible two"], "truncated": False}),
    )
    monkeypatch.setattr(
        "tmux_agent_tower.ui.control_view._route_clipboard",
        lambda _tower, text, _stdscr: routed.append(text) or (CopyOutcome(True, False, "clip.exe"), ""),
    )

    notice = _copy_screen(tower, "%9")

    assert routed == ["visible one\nvisible two"]
    assert notice == "✓ workstation-a 클립보드에 복사했습니다 · 최근 터미널 출력 2줄이며 전체 답변이 아닐 수 있습니다"
    assert captured[0][1]["expected_pane_pid"] == "123"


def test_screen_copy_says_when_earlier_output_was_omitted(monkeypatch):
    routed = []
    tower = _Tower()
    tower.rows[0]["pane_pid"] = "123"
    monkeypatch.setattr(
        "tmux_agent_tower.ui.control_view.get_pane_screen",
        lambda *_args, **_kwargs: (True, "", {"lines": ["visible one", "visible two"], "truncated": True}),
    )
    monkeypatch.setattr(
        "tmux_agent_tower.ui.control_view._route_clipboard",
        lambda _tower, text, _stdscr: routed.append(text) or (CopyOutcome(True, False, "clip.exe"), ""),
    )

    notice = _copy_screen(tower, "%9")

    assert routed == ["visible one\nvisible two"]
    assert notice == "✓ workstation-a 클립보드에 복사했습니다 · 최근 터미널 출력 중 마지막 2줄만 포함했으며 앞부분은 생략되었습니다"


def test_screen_copy_names_tower_buffer_instead_of_claiming_clipboard(monkeypatch):
    tower = _Tower()
    tower.rows[0]["pane_pid"] = "123"
    monkeypatch.setattr(
        "tmux_agent_tower.ui.control_view.get_pane_screen",
        lambda *_args, **_kwargs: (True, "", {"lines": ["visible one"], "truncated": False}),
    )
    monkeypatch.setattr(
        "tmux_agent_tower.ui.control_view._route_clipboard",
        lambda *_args, **_kwargs: (CopyOutcome(False, True, "tmux"), ""),
    )

    notice = _copy_screen(tower, "%9")

    assert notice == "✓ Tower 복사함에 저장했습니다 · 최근 터미널 출력 1줄이며 전체 답변이 아닐 수 있습니다"


def test_screen_copy_rejects_a_stale_pane_before_routing(monkeypatch):
    routed = []
    tower = _Tower()
    tower.rows[0]["pane_pid"] = "123"
    monkeypatch.setattr(
        "tmux_agent_tower.ui.control_view.get_pane_screen",
        lambda *_args, **_kwargs: (False, "stale", {}),
    )
    monkeypatch.setattr(
        "tmux_agent_tower.ui.control_view._route_clipboard",
        lambda *_args, **_kwargs: routed.append(True),
    )

    notice = _copy_screen(tower, "%9")

    assert notice == "작업이 사라져 목록을 새로고침했습니다."
    assert routed == []


def test_screen_copy_rejects_pane_pid_changed_since_selection(monkeypatch):
    routed = []
    captured = []
    tower = _Tower()
    tower.rows[0]["pane_pid"] = "123"
    monkeypatch.setattr(
        "tmux_agent_tower.ui.control_view.get_pane_screen",
        lambda *args, **kwargs: captured.append((args, kwargs)) or (True, "", {"lines": ["output"]}),
    )
    monkeypatch.setattr(
        "tmux_agent_tower.ui.control_view._route_clipboard",
        lambda *_args, **_kwargs: routed.append(True),
    )

    notice = _copy_screen(tower, "%9", expected_pane_pid="456")

    assert notice == "작업이 사라져 목록을 새로고침했습니다."
    assert captured == []
    assert routed == []


def test_terminal_send_is_not_described_as_a_confirmed_paste():
    notice = copy_test_notice(CopyOutcome(False, False, "terminal", True, False))
    assert "확인됨" not in notice
    assert "붙여넣기" in notice
