import json
import urllib.error
import urllib.request

import pytest

from tmux_agent_tower.adapters.base import ResultCandidate
from tmux_agent_tower.detection.result import ResultTracker
from tmux_agent_tower.server import httpapi
from tmux_agent_tower.state.worksets import WorkMember, WorksetStore, new_workset
from tmux_agent_tower.tmux import discovery
from tmux_agent_tower.ui import tower as tower_module

SESSION = "remote-test-session"


def _pane_line(pane_id="%1", title="my-title", command="bash", path="/home/user/project", dead="0"):
    fs = discovery.FIELD_SEP
    return fs.join([SESSION, "@1", "0", "bash", "0", pane_id, title, command, path, "123", dead, "1"])


@pytest.fixture
def isolated_state(tmp_path, monkeypatch):
    """Every test in this file that touches a real ``Tower`` must not read
    or write the actual user's state/config directories -- see
    ``ui/tower.py``'s STATE_DIR/CONFIG_DIR module constants.
    """

    monkeypatch.setattr(tower_module, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(tower_module, "CONFIG_DIR", tmp_path / "config")
    monkeypatch.setattr(tower_module, "HOST_FILE", tmp_path / "config" / "host")
    monkeypatch.setattr(tower_module, "REMOTE_HOSTS_FILE", tmp_path / "config" / "remote-hosts.txt")
    return tmp_path


@pytest.fixture
def one_pane(monkeypatch, isolated_state):
    monkeypatch.setattr(discovery.capture, "run_tmux", lambda args: _pane_line())
    monkeypatch.setattr(discovery.capture, "capture_pane", lambda pane_id, lines=30: ["user@host:~$"])
    monkeypatch.setattr(discovery.process_detection, "cmdline_by_pid", lambda: {})
    monkeypatch.setattr(discovery, "discover_project", lambda path: "my-project")
    monkeypatch.setattr(discovery, "git_project_name", lambda path: "my-project")
    # run_tmux above returns a pane line for every call, so the session
    # liveness check must be answered separately.
    monkeypatch.setattr(discovery.capture, "session_exists", lambda session: session == SESSION)
    return isolated_state


# -- build_status_payload ------------------------------------------------


def test_status_payload_reflects_a_real_pane(one_pane):
    tower = tower_module.Tower(SESSION, own_pane_id="")
    payload = httpapi.build_status_payload(tower)

    assert len(payload["panes"]) == 1
    pane = payload["panes"][0]
    assert pane["key"] == "%1"
    assert pane["project"] == "my-project"
    assert "path" not in pane
    assert pane["remote"] is False


def test_status_payload_never_includes_raw_lines(one_pane):
    tower = tower_module.Tower(SESSION, own_pane_id="")
    payload = httpapi.build_status_payload(tower)
    dumped = json.dumps(payload)
    assert "user@host" not in dumped


def test_tower_default_home_uses_folder_window_task_tree(one_pane):
    tower = tower_module.Tower(SESSION, own_pane_id="")
    tower.load()
    assert tower.view_mode == tower_module.USER_WORK_VIEW
    assert [row["kind"] for row in tower.visible_rows] == ["other_section"]
    assert tower.visible_rows[0]["display_name"] == "기타 터미널 · 자동 발견"
    assert tower.visible_rows[0]["collapsed"] is True
    assert not any(row.get("kind") == "window" for row in tower.visible_rows)

    # Enter on the discovered-terminal section opens its one Pane directly;
    # the Window identity stays attached for details and advanced view.
    tower_module._open_selected_row(None, tower)
    assert [row["kind"] for row in tower.visible_rows] == ["other_section", "pane"]
    assert tower.visible_rows[1]["pane_id"] == "%1"
    assert tower.visible_rows[1]["window_id"] == "@1"
    assert tower.visible_rows[1]["window_ref"]
    assert not any(row.get("kind") == "window" for row in tower.visible_rows)


def test_phone_workset_lists_show_structure_without_project_paths(one_pane, monkeypatch):
    store = WorksetStore(one_pane / "worksets.json")
    project = one_pane / "private-project"
    project.mkdir()
    store.save(new_workset(kind="saved_work", name="Saved Team", members=[
        WorkMember("", "builder", "Codex", display_name="구현", project_name="ExampleProject", project_path=str(project)),
    ]))
    store.save(new_workset(kind="template", name="Reusable Team", members=[
        WorkMember("", "builder", "Codex"),
    ]))
    monkeypatch.setattr(httpapi, "WorksetStore", lambda: store)

    payload = httpapi.build_status_payload(tower_module.Tower(SESSION, own_pane_id=""))
    assert [row["name"] for row in payload["saved_work"]] == ["Saved Team"]
    assert [row["name"] for row in payload["work_templates"]] == ["Reusable Team"]
    assert str(project) not in json.dumps(payload)
    assert "private-project" not in json.dumps(payload)


def test_phone_group_layout_and_membership_actions_are_logical_only(server, one_pane, monkeypatch):
    from types import SimpleNamespace
    from tmux_agent_tower.state.work_groups import WorkGroupStore

    store = WorkGroupStore(one_pane / "groups.json")
    first_target, second_target = "1" * 64, "2" * 64
    first = store.create("Project · Build", [first_target, second_target])
    second = store.create("Project · QA", ["3" * 64])
    rows = [
        {"target_id": first_target, "key": "%1", "pane_id": "%1", "kind": "pane", "remote": False,
         "session": SESSION, "window_id": "@1", "window_index": 0, "window_name": "work", "pane_index": 0,
         "host": "fixture", "tmux_host": "fixture", "execution_host": "fixture", "transport": "",
         "project": "Project", "display_name": "Builder", "task_name": "Builder", "role": "builder",
         "agent": "Codex", "status": "WORKING", "attention": "none", "result_state": "none"},
        {"target_id": second_target, "key": "%2", "pane_id": "%2", "kind": "pane", "remote": False,
         "session": SESSION, "window_id": "@1", "window_index": 0, "window_name": "work", "pane_index": 1,
         "host": "fixture", "tmux_host": "fixture", "execution_host": "fixture", "transport": "",
         "project": "Project", "display_name": "Reviewer", "task_name": "Reviewer", "role": "reviewer",
         "agent": "Cursor", "status": "IDLE", "attention": "none", "result_state": "none"},
    ]
    fake = SimpleNamespace(session=SESSION, rows=rows, work_groups=store, local_host="fixture",
                           remote_hosts=[], load=lambda: None)
    server.make_tower = lambda: fake
    monkeypatch.setattr(httpapi, "WorksetStore", lambda: WorksetStore(one_pane / "worksets.json"))
    token = _post(server, "/api/pair", {"code": server.pairing.current_code()})[1]["token"]

    status, layout_body = _post(server, "/api/groups/action", {
        "action": "layout", "group_id": first["group_id"], "layout": "main-plus-side",
    }, token=token)
    assert status == 200 and layout_body["ok"]
    assert store.all()[0]["layout"] == "main-plus-side"
    assert layout_body["groups"][0]["layout_slots"][first_target] == "main"

    status, move_body = _post(server, "/api/groups/action", {
        "action": "move", "group_id": first["group_id"], "target_id": second_target,
        "destination_group_id": second["group_id"],
    }, token=token)
    assert status == 200 and move_body["ok"]
    assert store.membership()[second_target] == second["group_id"]
    assert rows[0]["window_id"] == "@1" and rows[1]["window_id"] == "@1"


# -- find_prompt_target ---------------------------------------------------


def test_find_prompt_target_success(one_pane):
    tower = tower_module.Tower(SESSION, own_pane_id="")
    ok, reason, pane_id = httpapi.find_prompt_target(tower, "%1", "my-project", "Shell")
    assert ok is True
    assert reason == ""
    assert pane_id == "%1"


def test_find_prompt_target_not_found(one_pane):
    tower = tower_module.Tower(SESSION, own_pane_id="")
    ok, reason, pane_id = httpapi.find_prompt_target(tower, "%999", None, None)
    assert ok is False
    assert reason == "not_found"
    assert pane_id is None


def test_find_prompt_target_rejects_project_mismatch(one_pane):
    tower = tower_module.Tower(SESSION, own_pane_id="")
    ok, reason, pane_id = httpapi.find_prompt_target(tower, "%1", "some-other-project", "Shell")
    assert ok is False
    assert reason == "mismatch"
    assert pane_id is None


def test_find_prompt_target_rejects_agent_mismatch(one_pane):
    tower = tower_module.Tower(SESSION, own_pane_id="")
    ok, reason, pane_id = httpapi.find_prompt_target(tower, "%1", "my-project", "Codex")
    assert ok is False
    assert reason == "mismatch"


def test_find_prompt_target_rejects_dead_pane(monkeypatch, isolated_state):
    monkeypatch.setattr(discovery.capture, "run_tmux", lambda args: _pane_line(dead="1"))
    monkeypatch.setattr(discovery.capture, "capture_pane", lambda pane_id, lines=30: [])
    monkeypatch.setattr(discovery.process_detection, "cmdline_by_pid", lambda: {})
    monkeypatch.setattr(discovery, "discover_project", lambda path: "my-project")
    monkeypatch.setattr(discovery, "git_project_name", lambda path: None)

    tower = tower_module.Tower(SESSION, own_pane_id="")
    ok, reason, pane_id = httpapi.find_prompt_target(tower, "%1", None, None)
    assert ok is False
    assert reason == "dead"


def test_find_prompt_target_rejects_remote_rows(one_pane):
    tower = tower_module.Tower(SESSION, own_pane_id="")
    tower.rows = [{"key": "workstation-b:%1", "remote": True, "status": "IDLE", "project": "p", "agent": "a"}]
    tower.load = lambda: None  # keep the manually-seeded remote row for this test
    ok, reason, pane_id = httpapi.find_prompt_target(tower, "workstation-b:%1", None, None)
    assert ok is False
    assert reason == "remote_unsupported"


# -- HTTP integration (real ThreadingHTTPServer on an ephemeral port) ----


@pytest.fixture
def server(one_pane):
    srv = httpapi.create_server(
        "127.0.0.1",
        0,
        SESSION,
        "",
        allowed_hosts=["localhost", "127.0.0.1"],
        token_path=one_pane / "tokens.json",
        result_state_path=one_pane / "result-state.sqlite3",
    )
    import threading

    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv
    srv.shutdown()
    srv.server_close()
    thread.join(timeout=2)


def _url(server, path):
    port = server.server_address[1]
    return f"http://127.0.0.1:{port}{path}"


def _get(server, path, token=None, host_header=None):
    req = urllib.request.Request(_url(server, path))
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    if host_header:
        req.add_header("Host", host_header)
    try:
        with urllib.request.urlopen(req, timeout=3) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def _post(server, path, body, token=None):
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(_url(server, path), data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=3) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def test_health_requires_no_auth(server):
    status, body = _get(server, "/api/health")
    assert status == 200
    assert body["ok"] is True


def test_page_loads_without_auth(server):
    req = urllib.request.Request(_url(server, "/"))
    with urllib.request.urlopen(req, timeout=3) as resp:
        assert resp.status == 200
        assert b"<!doctype html>" in resp.read().lower()


def test_status_without_token_is_rejected(server):
    status, body = _get(server, "/api/status")
    assert status == 401
    assert body["ok"] is False


def test_workspace_projection_is_authenticated_and_contains_no_paths(server, one_pane):
    status, _body = _get(server, "/api/workspace")
    assert status == 401
    token = _post(server, "/api/pair", {"code": server.pairing.current_code()})[1]["token"]

    status, body = _get(server, "/api/workspace", token=token)
    assert status == 200
    assert body["workspace"]["schema_version"] == 1
    assert body["workspace"]["unfiled_windows"][0]["display_name"] == "my-project"
    assert body["workspace"]["tasks"][0]["project"] == "my-project"
    assert str(one_pane) not in json.dumps(body)
    assert "path" not in json.dumps(body)


def test_pairing_then_status_works(server):
    code = server.pairing.current_code()
    status, body = _post(server, "/api/pair", {"code": code})
    assert status == 200
    token = body["token"]

    status, body = _get(server, "/api/status", token=token)
    assert status == 200
    assert len(body["panes"]) == 1


def test_paired_phone_can_start_template_after_project_path_selection(server, one_pane, monkeypatch):
    from types import SimpleNamespace
    from tmux_agent_tower.launcher import workset_launch

    store = WorksetStore(one_pane / "worksets.json")
    template = new_workset(kind="template", name="기능 고도화 팀", members=[
        WorkMember("", "builder", "Codex"),
    ])
    store.save(template)
    project = one_pane / "Phone Project"
    project.mkdir()
    monkeypatch.setattr(httpapi, "WorksetStore", lambda: store)
    fake_tower = SimpleNamespace(local_host="WORKER", remote_hosts=[], session=SESSION)
    monkeypatch.setattr(server, "make_tower", lambda: fake_tower)
    calls = []
    monkeypatch.setattr(workset_launch, "launch_workset", lambda tower, workset, state_dir, **kwargs:
                        calls.append((tower, workset, kwargs)) or SimpleNamespace(group_name="Phone Project · 기능 고도화 팀", members=({},)))
    code = server.pairing.current_code()
    _, paired = _post(server, "/api/pair", {"code": code})

    status, body = _post(server, "/api/worksets/start", {
        "id": template.workset_id, "kind": "template", "project_path": str(project),
        "execution_target": "auto", "execution_targets": {template.members[0].member_id: "auto"}, "agents": {},
    }, token=paired["token"])

    assert status == 200
    assert body["group_name"] == "Phone Project · 기능 고도화 팀"
    assert calls[0][1] == template
    assert calls[0][2]["project_override"] == ("Phone Project", str(project))
    assert calls[0][2]["execution_target"] == "auto"
    assert calls[0][2]["execution_targets"] == {template.members[0].member_id: "auto"}


def test_phone_workset_start_requires_pairing_token(server):
    status, body = _post(server, "/api/worksets/start", {"id": "0" * 32, "kind": "template"})
    assert status == 401
    assert body["ok"] is False


def test_wrong_pairing_code_rejected(server):
    status, body = _post(server, "/api/pair", {"code": "000000"})
    assert status == 400
    assert body["ok"] is False


def test_repeated_wrong_pairing_attempts_lock_out_the_code(server):
    from tmux_agent_tower.server.auth import MAX_PAIR_ATTEMPTS

    real_code = server.pairing.current_code()

    for _ in range(MAX_PAIR_ATTEMPTS):
        status, body = _post(server, "/api/pair", {"code": "000000"})
        assert status == 400
        assert body["error"] == "invalid_code"

    # The real code no longer works either -- locked out, not just the
    # wrong guesses rejected.
    status, body = _post(server, "/api/pair", {"code": real_code})
    assert status == 400
    assert body["error"] == "invalid_code"


def test_no_http_endpoint_can_regenerate_pairing_code(server):
    # Regenerating is local-only (a terminal keypress in main.py) --
    # there must be no reachable HTTP path to it at all.
    for path in ("/api/pair/regenerate", "/api/regenerate", "/api/pairing/new"):
        status, _ = _get(server, path)
        assert status == 404


def test_invalid_host_header_rejected(server):
    status, body = _get(server, "/api/health", host_header="evil.example.com")
    assert status == 400
    assert body["error"] == "invalid_host"


def test_prompt_send_calls_send_prompt_to_pane(server, monkeypatch):
    calls = []
    monkeypatch.setattr("tmux_agent_tower.control.actions.SUBMIT_CONFIRM_SECONDS", 0.05)
    monkeypatch.setattr(
        "tmux_agent_tower.control.actions.send_prompt_to_pane",
        lambda pane_id, text, submit_key="Enter": calls.append((pane_id, text)) or True,
    )

    code = server.pairing.current_code()
    _, body = _post(server, "/api/pair", {"code": code})
    token = body["token"]

    status, body = _post(
        server, "/api/prompt", {"pane_key": "%1", "text": "hello", "project": "my-project", "agent": "Shell"}, token=token
    )
    assert status == 200
    assert body["ok"] is True
    assert "submitted" in body
    assert calls == [("%1", "hello")]


def test_prompt_send_without_auth_rejected(server, monkeypatch):
    calls = []
    monkeypatch.setattr(
        "tmux_agent_tower.control.actions.send_prompt_to_pane",
        lambda pane_id, text, submit_key="Enter": calls.append((pane_id, text)) or True,
    )

    status, body = _post(server, "/api/prompt", {"pane_key": "%1", "text": "hello"})
    assert status == 401
    assert calls == []


def test_prompt_too_long_is_rejected(server, monkeypatch):
    calls = []
    monkeypatch.setattr("tmux_agent_tower.control.actions.SUBMIT_CONFIRM_SECONDS", 0.05)
    monkeypatch.setattr(
        "tmux_agent_tower.control.actions.send_prompt_to_pane",
        lambda pane_id, text, submit_key="Enter": calls.append(text) or True,
    )
    code = server.pairing.current_code()
    _, body = _post(server, "/api/pair", {"code": code})
    token = body["token"]

    korean = "한글 " * 2000 + "\n\n```python\nprint('x')\n```\n"
    exact = korean + ("가" * (httpapi.MAX_PROMPT_CHARS - len(korean)))
    status, body = _post(server, "/api/prompt", {"pane_key": "%1", "text": exact}, token=token)
    assert status == 200
    assert calls == [exact]
    assert len(exact) == httpapi.MAX_PROMPT_CHARS

    status, body = _post(
        server, "/api/prompt", {"pane_key": "%1", "text": exact + "가"}, token=token
    )
    assert status == 413
    assert body["error"] == "prompt_too_long"
    assert calls == [exact]


def test_non_prompt_routes_keep_the_small_body_cap(server):
    code = server.pairing.current_code()
    _, body = _post(server, "/api/pair", {"code": code})
    token = body["token"]

    status, body = _post(
        server, "/api/panes/%1/identity", {"project": "x" * 9000}, token=token
    )
    assert status == 400
    assert body["error"] == "bad_request"


def test_prompt_wrong_pane_key_rejected(server):
    code = server.pairing.current_code()
    _, body = _post(server, "/api/pair", {"code": code})
    token = body["token"]

    status, body = _post(server, "/api/prompt", {"pane_key": "%does-not-exist", "text": "hello"}, token=token)
    assert status == 409
    assert body["error"] == "not_found"


def test_unknown_path_is_404(server):
    status, body = _get(server, "/api/nonexistent")
    assert status == 404


def test_phone_read_updates_the_same_store_used_by_a_fresh_tower(server, one_pane, monkeypatch):
    identity = {
        "tmux_host": "workstation-b",
        "server_scope": {"host": "workstation-b.local", "socket": "test-server", "server_pid": "1", "socket_inode": 2},
        "session": SESSION,
        "window_id": "@1",
        "pane_id": "%1",
        "pane_pid": "123",
    }
    screen = ["› Return a short answer", "HTTP answer", "Worked for 1s", "› Ask Codex to do anything"]
    monkeypatch.setattr(discovery.capture, "run_tmux", lambda args: _pane_line(command="codex"))
    monkeypatch.setattr(discovery.capture, "capture_pane", lambda pane_id, lines=30: screen)
    monkeypatch.setattr(httpapi, "pane_result_identity", lambda row: identity)
    monkeypatch.setattr(tower_module, "pane_result_identity", lambda row: identity)
    monkeypatch.setattr(server.token_store, "is_valid", lambda token: token == "test-token")

    from tmux_agent_tower.adapters.base import PaneContext
    from tmux_agent_tower.adapters.codex import CodexAdapter

    result = CodexAdapter().extract_result(PaneContext(title="my-title", command="codex", lines=tuple(screen)))
    assert result is not None and result.complete
    assert server.results.observe("%1", "IDLE", result, identity).state == "ready"
    status, body = _post(
        server,
        "/api/panes/%251/result",
        {"fingerprint": result.fingerprint},
        token="test-token",
    )
    assert status == 200
    assert body["state"] == "read"

    fresh_tower = tower_module.Tower(SESSION, results=ResultTracker(one_pane / "result-state.sqlite3"))
    payload = httpapi.build_status_payload(fresh_tower)
    assert payload["panes"][0]["result_state"] == "read"

    status_request = urllib.request.Request(_url(server, "/api/status"))
    status_request.add_header("Authorization", "Bearer test-token")
    with urllib.request.urlopen(status_request) as response:
        raw = response.read().decode("utf-8")
        assert response.headers.get("Cache-Control") == "no-store"
    assert "HTTP answer" not in raw
