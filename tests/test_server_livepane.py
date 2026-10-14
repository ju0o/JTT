"""Live pane view + identity editing over Tower Remote.

The screen endpoint is the only place raw terminal text leaves the PC:
paired token, existing local pane, capture-pane only, hard caps, never
stored. Identity edits go through the same OverrideStore the TUI uses.
"""

import json
import threading
import urllib.error
import urllib.parse
import urllib.request

import pytest

from tmux_agent_tower.server import httpapi, webui
from tmux_agent_tower.state.overrides import OverrideStore
from tmux_agent_tower.tmux import discovery
from tmux_agent_tower.ui import tower as tower_module

SESSION = "live-test-session"
MARKER = "SECRET-SCREEN-MARKER-7f3a"


def _pane_line(pane_id="%1", title="my-title", command="bash", path="/home/user/project", dead="0"):
    fs = discovery.FIELD_SEP
    return fs.join([SESSION, "@1", "0", "win", "0", pane_id, title, command, path, "123", dead, "1"])


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(tower_module, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(tower_module, "CONFIG_DIR", tmp_path / "config")
    monkeypatch.setattr(tower_module, "HOST_FILE", tmp_path / "config" / "host")
    monkeypatch.setattr(tower_module, "REMOTE_HOSTS_FILE", tmp_path / "config" / "remote-hosts.txt")

    calls = []
    screen = {"lines": ["$ pytest", "....", MARKER, "", ""]}
    alive_panes = {"%1"}

    def fake_run_tmux(args, capture=True, timeout=3.0):
        calls.append(list(args))
        if args and args[0] == "list-panes":
            if "#{pane_id}" in args:  # the screen endpoint's cheap membership check
                return "%1"
            return _pane_line()
        return ""

    monkeypatch.setattr(discovery.capture, "run_tmux", fake_run_tmux)
    monkeypatch.setattr(discovery.capture, "capture_pane", lambda pane_id, lines=30: list(screen["lines"]))
    monkeypatch.setattr(discovery.capture, "pane_exists", lambda pane_id: pane_id in alive_panes)
    monkeypatch.setattr(discovery.capture, "session_exists", lambda session: session == SESSION)
    monkeypatch.setattr(discovery.process_detection, "cmdline_by_pid", lambda: {})
    monkeypatch.setattr(discovery, "discover_project", lambda path: "my-project")
    monkeypatch.setattr(discovery, "git_project_name", lambda path: "my-project")

    srv = httpapi.create_server(
        "127.0.0.1", 0, SESSION, "", allowed_hosts=["localhost", "127.0.0.1"], token_path=tmp_path / "tokens.json"
    )
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    code = srv.pairing.current_code()
    token = _post(srv, "/api/pair", {"code": code})[1]["token"]
    yield {"srv": srv, "token": token, "calls": calls, "screen": screen, "alive": alive_panes, "root": tmp_path}
    srv.shutdown()
    srv.server_close()
    thread.join(timeout=2)


def _url(srv, path):
    return f"http://127.0.0.1:{srv.server_address[1]}{path}"


def _get(srv, path, token=None):
    req = urllib.request.Request(_url(srv, path))
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=3) as resp:
            return resp.status, json.loads(resp.read()), dict(resp.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read()), dict(exc.headers)


def _post(srv, path, body, token=None):
    req = urllib.request.Request(_url(srv, path), data=json.dumps(body).encode("utf-8"), method="POST")
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=3) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def _screen_path(key="%1"):
    return f"/api/panes/{urllib.parse.quote(key, safe='')}/screen"


# -- screen endpoint ---------------------------------------------------------


def test_screen_requires_auth(env):
    status, body, _ = _get(env["srv"], _screen_path())
    assert status == 401
    assert "lines" not in body


def test_screen_returns_recent_plain_text_for_a_live_pane(env):
    status, body, headers = _get(env["srv"], _screen_path(), env["token"])
    assert status == 200
    assert body["ok"] is True
    assert body["pane_key"] == "%1"
    assert body["lines"] == ["$ pytest", "....", MARKER]  # trailing blanks dropped
    assert body["truncated"] is False
    assert headers.get("Cache-Control") == "no-store"


def test_screen_rejects_a_nonexistent_pane(env):
    status, body, _ = _get(env["srv"], _screen_path("%999"), env["token"])
    assert status == 404
    assert body["error"] == "not_found"


def test_screen_rejects_a_stale_pane(env):
    env["alive"].clear()  # the row still lists %1, but tmux no longer has it
    status, body, _ = _get(env["srv"], _screen_path(), env["token"])
    assert status == 409
    assert body["error"] == "stale"


def test_screen_for_a_remote_ssh_pane_is_explicitly_unsupported(env):
    # SSH hosts are title-only (docs/ARCHITECTURE.md): there is nothing to
    # capture, and the endpoint must never try to SSH out for it.
    env["calls"].clear()
    status, body, _ = _get(env["srv"], _screen_path("workstation-b:%3"), env["token"])
    assert status == 409
    assert body["error"] == "remote_unsupported"
    assert env["calls"] == []


def test_screen_never_reads_a_pane_outside_the_bound_session(env):
    # %7 exists in tmux (pane_exists says so) but is not in the session's
    # list-panes output -> the endpoint refuses rather than capturing it.
    env["alive"].add("%7")
    status, body, _ = _get(env["srv"], _screen_path("%7"), env["token"])
    assert status == 404
    assert body["error"] == "not_found"


def test_screen_does_not_build_a_full_tower_snapshot(env, monkeypatch):
    """One phone poll must not fan out into capture-pane on every pane or
    an SSH fetch of the remote hosts (that was measured at ~400 ms)."""

    monkeypatch.setattr(tower_module, "fetch_remote", lambda *a, **k: pytest.fail("fetch_remote called"))
    env["calls"].clear()
    status, _, _ = _get(env["srv"], _screen_path(), env["token"])
    assert status == 200
    assert env["calls"] == [["list-panes", "-s", "-t", SESSION, "-F", "#{pane_id}"]]


def test_screen_enforces_max_lines(env):
    env["screen"]["lines"] = [f"line {i}" for i in range(500)]
    status, body, _ = _get(env["srv"], _screen_path(), env["token"])
    assert status == 200
    assert len(body["lines"]) == httpapi.MAX_SCREEN_LINES
    assert body["lines"][-1] == "line 499"  # the bottom of the screen survives
    assert body["truncated"] is True


def test_screen_enforces_max_payload(env):
    env["screen"]["lines"] = ["x" * 5000 for _ in range(60)]
    status, body, _ = _get(env["srv"], _screen_path(), env["token"])
    assert status == 200
    assert all(len(line) <= httpapi.MAX_SCREEN_LINE_CHARS for line in body["lines"])
    assert sum(len(line.encode("utf-8")) + 1 for line in body["lines"]) <= httpapi.MAX_SCREEN_BYTES
    assert body["truncated"] is True


def test_screen_strips_ansi_and_control_bytes():
    raw = ["\x1b[32mgreen\x1b[0m text", "bell\x07 here", "\x1b]0;title\x07after", "tab\tkept"]
    payload = httpapi.build_screen_payload("%1", raw)
    assert payload["lines"] == ["green text", "bell here", "after", "tab\tkept"]


def test_raw_screen_is_never_persisted(env):
    _get(env["srv"], _screen_path(), env["token"])
    for path in env["root"].rglob("*"):
        if path.is_file():
            assert MARKER not in path.read_text(encoding="utf-8", errors="ignore"), path


def test_polling_the_screen_does_not_mutate_the_pane(env):
    env["calls"].clear()
    _get(env["srv"], _screen_path(), env["token"])
    verbs = {call[0] for call in env["calls"]}
    assert verbs <= {"list-panes", "capture-pane"}
    assert not any(call[0] in ("send-keys", "select-pane", "kill-pane", "resize-pane") for call in env["calls"])


def test_status_payload_still_has_no_raw_lines(env):
    status, body, _ = _get(env["srv"], "/api/status", env["token"])
    assert status == 200
    assert MARKER not in json.dumps(body)
    assert body["panes"][0]["auto_project"] == "my-project"


# -- identity editing ---------------------------------------------------------


def _identity_path(key="%1"):
    return f"/api/panes/{urllib.parse.quote(key, safe='')}/identity"


def test_identity_edit_project(env):
    status, body = _post(env["srv"], _identity_path(), {"project": "Renamed"}, env["token"])
    assert status == 200 and body["identity"]["project"] == "Renamed"
    _, status_body, _ = _get(env["srv"], "/api/status", env["token"])
    assert status_body["panes"][0]["project"] == "Renamed"
    assert status_body["panes"][0]["auto_project"] == "my-project"


def test_identity_edit_agent_is_display_only(env):
    env["calls"].clear()
    status, body = _post(env["srv"], _identity_path(), {"agent": "Codex"}, env["token"])
    assert status == 200 and body["identity"]["agent"] == "Codex"
    assert not any(call[0] == "send-keys" for call in env["calls"])


def test_identity_edit_title_pushes_to_the_real_pane(env):
    env["calls"].clear()
    status, body = _post(env["srv"], _identity_path(), {"title": "New Title"}, env["token"])
    assert status == 200 and body["ok"] is True
    # Local titles live in tmux itself (same as the TUI's E menu), so the
    # edit is a select-pane -T on the real pane, never a send-keys.
    assert ["select-pane", "-t", "%1", "-T", "New Title"] in env["calls"]
    assert not any(call[0] == "send-keys" for call in env["calls"])


def test_identity_reset_returns_to_auto(env):
    _post(env["srv"], _identity_path(), {"project": "Renamed", "agent": "Codex"}, env["token"])
    status, body = _post(env["srv"], _identity_path(), {"reset": True}, env["token"])
    assert status == 200
    assert body["identity"]["project"] == "my-project"
    assert body["identity"]["agent"] == "Shell"


def test_identity_null_clears_one_field(env):
    _post(env["srv"], _identity_path(), {"project": "Renamed", "agent": "Codex"}, env["token"])
    status, body = _post(env["srv"], _identity_path(), {"agent": None}, env["token"])
    assert status == 200
    assert body["identity"]["project"] == "Renamed"
    assert body["identity"]["agent"] == "Shell"


def test_identity_requires_auth_and_rejects_junk(env):
    assert _post(env["srv"], _identity_path(), {"project": "x"})[0] == 401
    assert _post(env["srv"], _identity_path("%999"), {"project": "x"}, env["token"])[0] == 404
    assert _post(env["srv"], _identity_path(), {"project": "x" * 500}, env["token"])[1]["error"] == "invalid_project"
    assert _post(env["srv"], _identity_path(), {"unknown": "x"}, env["token"])[1]["error"] == "bad_request"


def test_identity_edit_is_shared_with_the_pc_tower(env):
    """The TUI's OverrideStore re-reads the file another writer changed."""

    tui_store = OverrideStore(tower_module.STATE_DIR / "overrides.json")
    assert tui_store.get_project("%1", SESSION, "123") is None
    _post(env["srv"], _identity_path(), {"project": "From Phone"}, env["token"])
    assert tui_store.get_project("%1", SESSION, "123") == "From Phone"
    assert tui_store.get_project("%1", SESSION, "999") is None


def test_prompt_send_still_works_from_the_detail_flow(env, monkeypatch):
    sent = []
    monkeypatch.setattr("tmux_agent_tower.control.actions.SUBMIT_CONFIRM_SECONDS", 0.05)
    monkeypatch.setattr(
        "tmux_agent_tower.control.actions.send_prompt_to_pane",
        lambda pane_id, text, submit_key="Enter": sent.append((pane_id, text)) or True,
    )
    status, body = _post(
        env["srv"], "/api/prompt", {"pane_key": "%1", "text": "hello", "project": "my-project", "agent": "Shell"}, env["token"]
    )
    assert status == 200 and body["ok"] is True
    assert sent == [("%1", "hello")]


def test_each_get_route_writes_exactly_one_response(env):
    """Regression: routing must not fall through to the 404 after a hit."""

    import socket

    for path in ("/api/health", "/"):
        with socket.create_connection(("127.0.0.1", env["srv"].server_address[1]), timeout=3) as sock:
            sock.sendall(f"GET {path} HTTP/1.0\r\nHost: 127.0.0.1\r\n\r\n".encode())
            chunks = []
            while True:
                data = sock.recv(65536)
                if not data:
                    break
                chunks.append(data)
        raw = b"".join(chunks)
        assert raw.startswith(b"HTTP/1.0 200")
        assert raw.count(b"HTTP/1.0 ") == 1, raw[-200:]
        assert not raw.endswith(b'"not_found"}')


# -- mobile page ---------------------------------------------------------------


def test_mobile_page_has_a_detail_view_with_live_pane_and_explicit_send():
    html = webui.PAGE_HTML
    assert 'id="detail"' in html
    assert 'id="screen"' in html and "작업 화면" in html
    assert "/api/panes/" in html and '"screen"' in html and '"identity"' in html
    assert 'id="send-target"' in html  # target visible above the textarea
    assert 'id="edit-reset"' in html and "자동 이름으로 복원" in html
    assert "openPanel(" not in html  # cards no longer jump straight to sending


def test_mobile_page_pauses_polling_in_background_and_adapts():
    html = webui.PAGE_HTML
    assert "visibilitychange" in html
    assert "document.hidden" in html
    assert str(webui.SCREEN_POLL_MIN_MS) in html and str(webui.SCREEN_POLL_MAX_MS) in html
    assert webui.SCREEN_POLL_MIN_MS >= 500 and webui.SCREEN_POLL_MAX_MS <= 2000
    assert "SCREEN_POLL_SLOW_MS" in html


def test_status_exposes_tmux_location(env):
    status, body, _ = _get(env["srv"], "/api/status", env["token"])
    assert status == 200
    pane = body["panes"][0]
    assert pane["session"] == SESSION
    assert pane["window_index"] == "0"
    assert pane["window_name"] == "win"
    assert pane["pane_id"] == "%1"
    assert pane["pane_index"] == "0"
    assert pane["active"] is True


def test_focus_requires_auth_and_only_moves_the_bound_pane(env, monkeypatch):
    status, body = _post(env["srv"], "/api/panes/%251/focus", {})
    assert status == 401

    from tmux_agent_tower.server import httpapi

    def fake_focus(session, pane_id, _own_pane_id=""):
        env["calls"].extend((["select-window", "-t", pane_id], ["select-pane", "-t", pane_id]))
        return session == SESSION and pane_id == "%1", ""

    monkeypatch.setattr(httpapi, "focus_pane", fake_focus)
    before = len(env["calls"])
    status, body = _post(env["srv"], "/api/panes/%251/focus", {}, env["token"])
    assert status == 200
    assert body["pane_id"] == "%1"
    moved = env["calls"][before:]
    assert ["select-window", "-t", "%1"] in moved
    assert ["select-pane", "-t", "%1"] in moved
    assert not any(call and call[0] == "send-keys" for call in moved)


def test_focus_rejects_stale_remote_and_foreign_panes(env):
    status, body = _post(env["srv"], _screen_path("workstation-b:%0").replace("/screen", "/focus"), {}, env["token"])
    assert status == 409
    assert body["error"] == "remote_unsupported"
    assert body["error"] == "remote_unsupported"

    status, body = _post(env["srv"], "/api/panes/%25999/focus", {}, env["token"])
    assert status == 404
    assert body["error"] == "not_found"

    env["alive"].discard("%1")
    before = len(env["calls"])
    status, body = _post(env["srv"], "/api/panes/%251/focus", {}, env["token"])
    assert status == 409
    assert body["error"] == "stale"
    assert not any(call and call[0] in ("select-pane", "send-keys") for call in env["calls"][before:])


def test_focus_button_is_not_the_card_click():
    html = webui.PAGE_HTML
    assert "컴퓨터에서 이 작업 열기" in html
    assert '"focus"' in html
    card = html.split('card.addEventListener("click"')[1].split("});")[0]
    assert "focus" not in card
    assert "openDetail" in card


def test_mobile_page_keeps_horizontal_scroll_inside_the_pane_box():
    html = webui.PAGE_HTML
    assert "overflow-x: hidden" in html  # page
    assert "#screen-wrap" in html and "overflow-x: auto" in html  # pane box only
    assert "white-space: pre" in html
