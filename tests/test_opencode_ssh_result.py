import base64
import hashlib
import json
import os
import socket
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from tmux_agent_tower.remote.opencode import _REMOTE_OPENCODE_SCRIPT, fetch_remote_opencode_result


CLIENT = ("198.51.100.23", "43123", "203.0.113.31", "22")
SESSION_ID = "ses_0123456789abcdef0123456789"
OTHER_SESSION_ID = "ses_abcdef0123456789abcdef0123"
USER_ID = "msg_user_0123456789"
ASSISTANT_ID = "msg_assistant_0123456789"


def _export(cwd, *, complete=True, finish="stop", session_id=SESSION_ID, body_parts=None):
    old_user = {"id": "msg_user_old", "sessionID": session_id, "role": "user", "time": {"created": 1}}
    old_assistant = {
        "id": "msg_assistant_old", "sessionID": session_id, "role": "assistant",
        "parentID": old_user["id"], "time": {"created": 2, "completed": 3}, "finish": "stop",
    }
    user = {"id": USER_ID, "sessionID": session_id, "role": "user", "time": {"created": 4}}
    assistant = {
        "id": ASSISTANT_ID, "sessionID": session_id, "role": "assistant", "parentID": USER_ID,
        "time": {"created": 5, **({"completed": 6} if complete else {})}, "finish": finish if complete else None,
    }
    if body_parts is None:
        body_parts = ["한글 결과\n```python\nprint('full')\n```", "다음 문단\n"]
    return {
        "info": {"id": session_id, "title": "Bridge Demo", "directory": str(cwd)},
        "messages": [
            {"info": old_user, "parts": []},
            {"info": old_assistant, "parts": [{"type": "text", "text": "OLD TURN MUST NOT COPY", "messageID": old_assistant["id"], "sessionID": session_id}]},
            {"info": user, "parts": []},
            {"info": assistant, "parts": [
                *[
                    {"type": "text", "text": part, "messageID": ASSISTANT_ID, "sessionID": session_id}
                    for part in body_parts
                ],
                {"type": "tool", "state": {"status": "completed", "output": "TOOL OUTPUT MUST NOT COPY"}},
            ]},
        ],
    }


def _proc_ip(value):
    return socket.inet_aton(value)[::-1].hex().upper()


def _proc_process(proc, pid, ppid, comm, cwd, *, executable="/bin/bash", env=None, argv=None, pgrp=None, tty_nr="34816", tpgid="200"):
    folder = proc / str(pid)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "stat").write_text(f"{pid} ({comm}) S {ppid} {pgrp or pid} 1000 {tty_nr} {tpgid} " + "0 " * 20)
    (folder / "comm").write_text(comm)
    (folder / "cmdline").write_bytes(b"\0".join(arg.encode() for arg in (argv or [comm])) + b"\0")
    (folder / "environ").write_bytes(b"\0".join(f"{key}={value}".encode() for key, value in (env or {}).items()) + b"\0")
    (folder / "exe").symlink_to(executable)
    (folder / "cwd").symlink_to(cwd)


def _remote_fixture(tmp_path, *, complete=True, duplicate=False, second_opencode=False, background_opencode=False, config_content=None, process_argv=None, executable_name="opencode", comm_name="opencode", finish="stop", binding=True, plugin_marker=None, route_name="session", binding_session=SESSION_ID, generation=1, stale_binding=False, switch_during_export=False, export_session=SESSION_ID, body_parts=None, export_override=None):
    tmp_path.mkdir(parents=True, exist_ok=True)
    proc = tmp_path / "proc"
    proc.mkdir()
    (proc / "net").mkdir()
    local = f"{_proc_ip(CLIENT[2])}:{int(CLIENT[3]):04X}"
    peer = f"{_proc_ip(CLIENT[0])}:{int(CLIENT[1]):04X}"
    (proc / "net" / "tcp").write_text("header\n0: " + local + " " + peer + " 01 0:0 00:0 00000000 1000 0 1\n")
    cwd = tmp_path / "project"
    cwd.mkdir()
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_exe = fake_bin / executable_name
    fake_exe.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, pathlib, sys\n"
        "if sys.argv[1:] == ['--version']:\n print('1.19.0')\n"
        "elif sys.argv[1:3] == ['session', 'list']:\n pathlib.Path('.session-list-called').write_text('yes'); sys.exit(4)\n"
        "elif len(sys.argv) == 3 and sys.argv[1] == 'export':\n"
        " pathlib.Path('.export-called').write_text(sys.argv[2])\n"
        " pathlib.Path('.export-env.json').write_text(json.dumps({key: os.environ.get(key) for key in ('OPENCODE_DB', 'XDG_CACHE_HOME')}))\n"
        " switch = pathlib.Path('.switch-binding.json')\n"
        " if switch.exists():\n"
        "  update = json.loads(switch.read_text())\n"
        "  binding = pathlib.Path(update['path'])\n"
        "  binding.write_text(json.dumps(update['record']))\n"
        " marker = pathlib.Path('.kill-opencode-stat')\n"
        " if marker.exists(): pathlib.Path(marker.read_text()).unlink()\n"
        " print(pathlib.Path('.export.json').read_text())\n"
        "else: sys.exit(3)\n"
    )
    fake_exe.chmod(0o755)
    session = {"id": SESSION_ID, "title": "Bridge Demo", "directory": str(cwd)}
    sessions = [session, dict(session, id=OTHER_SESSION_ID)] if duplicate else [session]
    (cwd / ".sessions.json").write_text(json.dumps(sessions))
    export_json = json.dumps(_export(cwd, complete=complete, finish=finish, session_id=export_session, body_parts=body_parts))
    (cwd / ".export.json").write_text(export_override if export_override is not None else export_json)
    ssh_env = {"SSH_CONNECTION": " ".join(CLIENT)}
    _proc_process(proc, 100, 1, "sshd: test@pts/0", cwd, env=ssh_env)
    _proc_process(proc, 101, 100, "bash", cwd, env=ssh_env)
    isolated = tmp_path / "isolated-opencode"
    opencode_env = {
        **ssh_env, "HOME": str(tmp_path), "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "OPENCODE_DB": str(isolated / "opencode.db"),
        "XDG_CACHE_HOME": str(isolated / "cache"),
    }
    if config_content is not None:
        opencode_env["OPENCODE_CONFIG_CONTENT"] = config_content
    _proc_process(proc, 200, 101, comm_name, cwd, executable=str(fake_exe), env=opencode_env, argv=process_argv or ["opencode", "--session", SESSION_ID], pgrp="201" if background_opencode else "200")
    if second_opencode:
        _proc_process(proc, 201, 101, "opencode", cwd, executable=str(fake_exe), env=opencode_env, argv=["opencode"])
    binding_dir = tmp_path / "binding"
    binding_dir.mkdir()
    nonce = "0" * 32
    if binding:
        record = {
            "schema": "tower-opencode-tui-binding/v1", "pid": 200,
            "process_starttime": "0", "route_name": route_name,
            "session_id": binding_session if route_name == "session" else None,
            "generation": generation, "nonce": "0" * 32,
            "observed_at_ms": int(time.time() * 1000) - (3000 if stale_binding else 0),
        }
        nonce = record["nonce"]
        binding_path = binding_dir / "tower-opencode-session-200.json"
        binding_path.write_text(json.dumps(record))
        binding_path.chmod(0o600)
        if switch_during_export:
            update = dict(record, generation=generation + 1, session_id=OTHER_SESSION_ID)
            (cwd / ".switch-binding.json").write_text(json.dumps({"path": str(binding_path), "record": update}))
    if plugin_marker is None:
        plugin_marker = binding
    if plugin_marker:
        marker = {
            "schema": "tower-opencode-tui-plugin/v1", "pid": 200,
            "process_starttime": "0", "nonce": nonce,
        }
        marker_path = binding_dir / "tower-opencode-session-200.plugin.json"
        marker_path.write_text(json.dumps(marker))
        marker_path.chmod(0o600)
    return proc, cwd, binding_dir


def _run_remote(proc, binding_dir):
    args = [*CLIENT, str(proc), str(binding_dir)]
    completed = subprocess.run(
        [sys.executable, "-c", _REMOTE_OPENCODE_SCRIPT, *args],
        capture_output=True, text=True, check=False,
    )
    assert completed.returncode == 0, completed.stderr
    record = next(line.removeprefix("__TOWER_OPENCODE_RESULT__") for line in completed.stdout.splitlines() if line.startswith("__TOWER_OPENCODE_RESULT__"))
    return json.loads(record), completed.stdout


@pytest.mark.parametrize(
    "argv",
    [
        ["opencode"],
        ["opencode", "--session", SESSION_ID],
        ["opencode", "--session=" + SESSION_ID],
        ["opencode", "-s", SESSION_ID, "--pure"],
        ["opencode", "--session", OTHER_SESSION_ID],
    ],
)
def test_remote_provider_exports_only_the_verified_current_tui_session(tmp_path, argv):
    proc, cwd, binding_dir = _remote_fixture(tmp_path, process_argv=argv, duplicate=True)
    payload, wire = _run_remote(proc, binding_dir)

    assert payload["status"] == "ok"
    assert payload["session_id"] == SESSION_ID
    assert payload["session_binding"] == "route.current"
    assert payload["turn_complete"] is True and payload["body_complete"] is True
    assert (cwd / ".export-called").read_text() == SESSION_ID
    assert "한글 결과" not in wire
    assert not (cwd / ".session-list-called").exists()


@pytest.mark.parametrize(
    ("options", "reason"),
    [
        ({"binding": False}, "TUI_BINDING_ABSENT"),
        ({"route_name": "home"}, "NO_ACTIVE_SESSION"),
        ({"route_name": "other"}, "NO_ACTIVE_SESSION"),
        ({"stale_binding": True}, "TUI_BINDING_INVALID"),
    ],
)
def test_remote_provider_fails_closed_without_a_fresh_active_route(tmp_path, options, reason):
    proc, cwd, binding_dir = _remote_fixture(tmp_path, **options)
    payload, wire = _run_remote(proc, binding_dir)

    assert payload["status"] == "AGENT_SESSION_UNBOUND"
    assert payload["session_binding"] == reason
    assert "body_b64" not in payload and "한글 결과" not in wire
    assert not (cwd / ".export-called").exists()


def test_remote_provider_rechecks_route_generation_after_export(tmp_path):
    proc, cwd, binding_dir = _remote_fixture(tmp_path, switch_during_export=True)
    payload, wire = _run_remote(proc, binding_dir)

    assert payload["status"] == "REMOTE_RESULT_INCOMPLETE"
    assert payload["reason_code"] == "SESSION_BINDING_CHANGED"
    assert "body_b64" not in payload and "한글 결과" not in wire
    assert (cwd / ".export-called").read_text() == SESSION_ID


def test_remote_provider_rejects_insecure_binding_file(tmp_path):
    proc, cwd, binding_dir = _remote_fixture(tmp_path)
    (binding_dir / "tower-opencode-session-200.json").chmod(0o644)
    payload, _ = _run_remote(proc, binding_dir)
    assert payload["status"] == "AGENT_SESSION_UNBOUND"
    assert payload["session_binding"] == "TUI_BINDING_INVALID"
    assert not (cwd / ".export-called").exists()


@pytest.mark.parametrize("damage", ["json", "pid"])
def test_remote_provider_rejects_corrupt_or_wrong_process_binding_without_export(tmp_path, damage):
    proc, cwd, binding_dir = _remote_fixture(tmp_path)
    path = binding_dir / "tower-opencode-session-200.json"
    if damage == "json":
        path.write_text("{broken", encoding="utf-8")
    else:
        record = json.loads(path.read_text())
        record["pid"] = 201
        path.write_text(json.dumps(record), encoding="utf-8")
    path.chmod(0o600)

    payload, _ = _run_remote(proc, binding_dir)

    assert payload["status"] == "AGENT_SESSION_UNBOUND"
    assert payload["session_binding"] == "TUI_BINDING_INVALID"
    assert not (cwd / ".export-called").exists()


def test_remote_provider_does_not_treat_missing_binding_as_plugin_absence_when_plugin_is_loaded(tmp_path):
    proc, cwd, binding_dir = _remote_fixture(tmp_path, binding=False, plugin_marker=True)

    payload, _ = _run_remote(proc, binding_dir)

    assert payload["status"] == "AGENT_SESSION_UNBOUND"
    assert payload["session_binding"] == "TUI_BINDING_INVALID"
    assert not (cwd / ".export-called").exists()


@pytest.mark.parametrize("damage", ["json", "nonce", "missing"])
def test_remote_provider_rejects_binding_without_a_valid_matching_plugin_marker(tmp_path, damage):
    proc, cwd, binding_dir = _remote_fixture(tmp_path)
    path = binding_dir / "tower-opencode-session-200.plugin.json"
    if damage == "json":
        path.write_text("{broken", encoding="utf-8")
    elif damage == "nonce":
        marker = json.loads(path.read_text())
        marker["nonce"] = "1" * 32
        path.write_text(json.dumps(marker), encoding="utf-8")
    else:
        path.unlink()
    if path.exists():
        path.chmod(0o600)

    payload, _ = _run_remote(proc, binding_dir)

    assert payload["status"] == "AGENT_SESSION_UNBOUND"
    assert payload["session_binding"] == "TUI_BINDING_INVALID"
    assert not (cwd / ".export-called").exists()


def test_remote_provider_preserves_executable_and_connection_checks(tmp_path):
    proc, cwd, binding_dir = _remote_fixture(
        tmp_path / "wrong-executable", process_argv=["opencode"], executable_name="not-opencode",
    )
    payload, _ = _run_remote(proc, binding_dir)
    assert payload["status"] == "REMOTE_PROVIDER_UNAVAILABLE"
    assert "session_binding" not in payload and not (cwd / ".export-called").exists()

    proc, cwd, binding_dir = _remote_fixture(tmp_path / "wrong-socket")
    wrong_args = ["198.51.100.24", CLIENT[1], CLIENT[2], CLIENT[3], str(proc)]
    completed = subprocess.run(
        [sys.executable, "-c", _REMOTE_OPENCODE_SCRIPT, *wrong_args], capture_output=True, text=True,
    )
    record = next(
        line.removeprefix("__TOWER_OPENCODE_RESULT__") for line in completed.stdout.splitlines()
        if line.startswith("__TOWER_OPENCODE_RESULT__")
    )
    assert json.loads(record)["status"] == "REMOTE_PROVIDER_UNBOUND"

    proc, _cwd, binding_dir = _remote_fixture(tmp_path / "multiple", second_opencode=True)
    assert _run_remote(proc, binding_dir)[0]["status"] == "REMOTE_PROVIDER_UNBOUND"


def test_remote_provider_recognizes_opencode_exe_comm_alias_with_active_binding(tmp_path):
    proc, cwd, binding_dir = _remote_fixture(
        tmp_path, process_argv=["opencode"], executable_name="opencode.exe", comm_name="opencode",
    )
    payload, _ = _run_remote(proc, binding_dir)

    assert payload["status"] == "ok"
    assert (cwd / ".export-called").read_text() == SESSION_ID


def test_remote_provider_rejects_background_process_and_does_not_return_config_content(tmp_path):
    proc, _cwd, binding_dir = _remote_fixture(tmp_path / "background", background_opencode=True)
    payload, _ = _run_remote(proc, binding_dir)
    assert payload["status"] == "AGENT_SESSION_UNBOUND"

    secret = "never-copy-this-config-value"
    proc, _cwd, binding_dir = _remote_fixture(tmp_path / "config", config_content=secret)
    payload, wire = _run_remote(proc, binding_dir)
    assert payload["status"] == "REMOTE_PROVIDER_UNAVAILABLE"
    assert secret not in wire


def test_native_export_preserves_long_korean_markdown_and_exact_text_fragments(tmp_path):
    paragraphs = [
        f"{index:03d}. Tower는 원본 메시지의 순서와 UTF-8 본문을 그대로 보존하고 검증합니다. "
        f"중간의 빈 줄과 Markdown 구조도 변형하지 않도록 확인합니다."
        for index in range(1, 101)
    ]
    body = "TOWER_LONG_RESULT_START\n\n" + "\n\n".join(paragraphs)
    body += "\n\n```python\nif True:\n    print('Tower long result')\n    print('한글 UTF-8 검증')\n```\n"
    body += "TOWER_LONG_RESULT_END\n"
    split = len(body) // 2
    proc, cwd, binding_dir = _remote_fixture(tmp_path, body_parts=[body[:split], body[split:]])
    payload, _ = _run_remote(proc, binding_dir)

    assert payload["status"] == "ok"
    assert payload["native_utf8_bytes"] == len(body.encode("utf-8"))
    assert payload["native_sha256"] == hashlib.sha256(body.encode("utf-8")).hexdigest()
    decoded = base64.b64decode(payload["body_b64"]).decode("utf-8")
    assert decoded == body
    assert decoded.startswith("TOWER_LONG_RESULT_START\n")
    assert decoded.endswith("TOWER_LONG_RESULT_END\n")
    assert all(f"{index:03d}." in decoded for index in range(1, 101))
    assert "```python\nif True:\n    print('Tower long result')" in decoded


def test_native_export_preserves_the_first_completed_answer(tmp_path):
    cwd = tmp_path / "project"
    first_turn = _export(cwd)
    first_turn["messages"] = first_turn["messages"][2:]
    proc, cwd, binding_dir = _remote_fixture(
        tmp_path, export_override=json.dumps(first_turn),
    )

    payload, _ = _run_remote(proc, binding_dir)

    expected = "한글 결과\n```python\nprint('full')\n```다음 문단\n"
    assert payload["status"] == "ok"
    assert base64.b64decode(payload["body_b64"]).decode("utf-8") == expected


def test_native_export_rejects_running_or_other_session_result(tmp_path):
    proc, cwd, binding_dir = _remote_fixture(tmp_path / "running", complete=False)
    payload, _ = _run_remote(proc, binding_dir)
    assert payload["status"] == "REMOTE_RESULT_INCOMPLETE"
    assert "body_b64" not in payload


@pytest.mark.parametrize("truncated", ["{\"info\":", json.dumps(_export("/tmp"))[:-8]])
def test_native_export_rejects_truncated_json_at_either_boundary(tmp_path, truncated):
    proc, _cwd, binding_dir = _remote_fixture(tmp_path, export_override=truncated)
    payload, _ = _run_remote(proc, binding_dir)
    assert payload["status"] == "REMOTE_RESULT_INCOMPLETE"
    assert "body_b64" not in payload

    proc, cwd, binding_dir = _remote_fixture(tmp_path / "other-session", export_session=OTHER_SESSION_ID)
    payload, _ = _run_remote(proc, binding_dir)
    assert payload["status"] == "REMOTE_RESULT_INCOMPLETE"
    assert "body_b64" not in payload


def test_fetch_decodes_bounded_wire_payload_and_keeps_exact_utf8(monkeypatch):
    body = "한글 **Markdown**\n```py\nprint(1)\n```\n"
    encoded = body.encode()
    record = {
        "status": "ok", "body_b64": base64.b64encode(encoded).decode(),
        "native_length": len(body), "native_utf8_bytes": len(encoded),
        "native_sha256": hashlib.sha256(encoded).hexdigest(),
    }
    completed = SimpleNamespace(returncode=0, stdout="__TOWER_OPENCODE_RESULT__" + json.dumps(record), stderr="")
    calls = []
    monkeypatch.setattr("tmux_agent_tower.remote.opencode._run_ssh", lambda *args: calls.append(args) or completed)
    payload = fetch_remote_opencode_result("asus", {"ssh_client_pid": "123", **dict(zip(("client_ip", "client_port", "server_ip", "server_port"), CLIENT))})

    assert payload["text"] == body
    assert calls and "opencode export" not in calls[0][1]


@pytest.mark.parametrize("field", ["native_length", "native_utf8_bytes", "native_sha256"])
def test_fetch_rejects_body_that_disagrees_with_native_integrity_metadata(monkeypatch, field):
    body = "한글 **Markdown**\n"
    encoded = body.encode()
    record = {
        "status": "ok", "body_b64": base64.b64encode(encoded).decode(),
        "native_length": len(body), "native_utf8_bytes": len(encoded),
        "native_sha256": hashlib.sha256(encoded).hexdigest(),
    }
    record[field] = "0" if field == "native_sha256" else record[field] + 1
    completed = SimpleNamespace(returncode=0, stdout="__TOWER_OPENCODE_RESULT__" + json.dumps(record), stderr="")
    monkeypatch.setattr("tmux_agent_tower.remote.opencode._run_ssh", lambda *_args: completed)

    payload = fetch_remote_opencode_result(
        "asus", {"ssh_client_pid": "123", **dict(zip(("client_ip", "client_port", "server_ip", "server_port"), CLIENT))},
    )

    assert payload == {"status": "REMOTE_RESULT_INCOMPLETE"}


class _Tower:
    session = "isolated"
    own_pane_id = "%1"
    results = None

    def __init__(self):
        self.rows = [{
            "key": "%9", "pane_id": "%9", "pane_pid": "900", "session": "isolated",
            "remote": False, "transport": "ssh", "transport_target": "fixture-host",
            "execution_host": "fixture-host", "agent": "OpenCode", "auto_agent": "OpenCode",
            "command": "ssh", "pane_title": "OC | Bridge Demo", "status": "IDLE", "attention": "none",
        }]

    def load(self):
        pass


def _connection():
    return {
        "ssh_client_pid": "901", "client_ip": CLIENT[0], "client_port": CLIENT[1],
        "server_ip": CLIENT[2], "server_port": CLIENT[3],
        "local_socket": ("192.0.2.23", "57120", CLIENT[2], CLIENT[3]), "connection_mode": "wsl_nat",
    }


def _provider_payload(body):
    encoded = body.encode()
    return {
        "status": "ok", "ssh_connection": list(CLIENT), "remote_session_pid": "100",
        "remote_sshd_pid": "90", "opencode_pid": "200", "session_id": SESSION_ID,
        "user_id": USER_ID, "message_id": ASSISTANT_ID, "turn_complete": True,
        "body_complete": True, "native_length": len(body), "native_utf8_bytes": len(encoded),
        "native_sha256": hashlib.sha256(encoded).hexdigest(), "text": body,
    }


def _bind_local_ssh(monkeypatch, connection=None, *, after=None):
    first = connection or _connection()
    calls = []
    monkeypatch.setattr("tmux_agent_tower.control.actions._pane_matches_live_identity", lambda *_: True)
    monkeypatch.setattr("tmux_agent_tower.control.actions.pane_title_and_command", lambda _pane: ("ordinary terminal title", "ssh"))
    monkeypatch.setattr(
        "tmux_agent_tower.detection.sshdest.ssh_connection_for_pane",
        lambda *_: calls.append(1) or (first if len(calls) == 1 or after is None else after),
    )
    return calls


def test_tower_get_result_uses_bound_ssh_provider_and_rechecks_pane(monkeypatch):
    from tmux_agent_tower.control.actions import get_result

    tower = _Tower()
    body = "한글 최신 결과\n```python\nprint('ok')\n```"
    _bind_local_ssh(monkeypatch)
    monkeypatch.setattr(
        "tmux_agent_tower.remote.opencode.fetch_remote_opencode_result",
        lambda host, conn: _provider_payload(body) if (host, conn["ssh_client_pid"]) == ("fixture-host", "901") else None,
    )
    ok, reason, payload = get_result(tower, "%9", expected_pane_pid="900")

    assert ok and reason == "" and payload["text"] == body
    assert payload["complete"] and payload["turn_complete"] and payload["body_complete"]
    assert payload["source"] == "remote_opencode"
    assert payload["native_sha256"] == hashlib.sha256(body.encode()).hexdigest()


def test_tower_preserves_session_switch_reason_and_never_falls_back_to_capture(monkeypatch):
    from tmux_agent_tower.control.actions import get_result

    tower = _Tower()
    _bind_local_ssh(monkeypatch)
    monkeypatch.setattr(
        "tmux_agent_tower.remote.opencode.fetch_remote_opencode_result",
        lambda *_: {"status": "REMOTE_RESULT_INCOMPLETE", "reason_code": "SESSION_BINDING_CHANGED"},
    )
    monkeypatch.setattr(
        "tmux_agent_tower.control.actions._recover_result",
        lambda *_a, **_k: pytest.fail("a changed TUI session must not fall back to pane capture"),
    )

    ok, reason, payload = get_result(tower, "%9", expected_pane_pid="900")

    assert ok and reason == ""
    assert payload["complete"] is False
    assert payload["reason_code"] == "SESSION_BINDING_CHANGED"


def test_tower_uses_boundary_checked_capture_only_when_opencode_binding_is_absent(monkeypatch):
    from tmux_agent_tower.control.actions import get_result

    tower = _Tower()
    tower.results = object()
    _bind_local_ssh(monkeypatch)
    monkeypatch.setattr(
        "tmux_agent_tower.remote.opencode.fetch_remote_opencode_result",
        lambda *_: {
            "status": "AGENT_SESSION_UNBOUND",
            "session_binding": "TUI_BINDING_ABSENT",
        },
    )
    body = "A bounded screen result."
    recovered = []

    def recover(_tower, pane, _tracker, _identity, *, source, expected_pane_pid):
        recovered.append((pane, source, expected_pane_pid))
        return {
            "state": "ready", "text": body, "fingerprint": hashlib.sha256(body.encode()).hexdigest(),
            "generated_at": 1.0, "complete": True, "turn_complete": True, "body_complete": True,
            "source": source, "turn_identity": "turn-1", "timestamp": 1.0, "confidence": "high",
        }

    monkeypatch.setattr("tmux_agent_tower.control.actions._recover_result", recover)
    ok, reason, payload = get_result(tower, "%9", expected_pane_pid="900")

    assert ok and reason == ""
    assert payload["text"] == body and payload["complete"]
    assert payload["source"] == "ssh_visible_history"
    assert payload["fallback_reason_code"] == "OPENCODE_PLUGIN_NOT_INSTALLED"
    assert recovered == [("%9", "ssh_visible_history", "900")]


@pytest.mark.parametrize(
    ("captured", "history_size", "expected_text", "expected_complete", "expected_reason"),
    [
        (
            ["┃ synthetic request", "Short complete answer.", "▣ Build · model · 1.0s", "ctrl+p commands"],
            4, "Short complete answer.", True, "",
        ),
        (
            [*[f"long answer suffix line {index}" for index in range(798)], "▣ Build · model · 1.0s", "ctrl+p commands"],
            803, "", False, "OUTPUT_RANGE_EXCEEDED",
        ),
    ],
)
def test_opencode_capture_uses_real_adapter_boundaries_when_binding_plugin_is_absent(
    monkeypatch, captured, history_size, expected_text, expected_complete, expected_reason,
):
    from tmux_agent_tower.control.actions import get_result
    from tmux_agent_tower.detection.result import ResultTracker

    tower = _Tower()
    tower.results = ResultTracker()
    _bind_local_ssh(monkeypatch)
    monkeypatch.setattr(
        "tmux_agent_tower.remote.opencode.fetch_remote_opencode_result",
        lambda *_: {
            "status": "AGENT_SESSION_UNBOUND",
            "session_binding": "TUI_BINDING_ABSENT",
        },
    )
    monkeypatch.setattr(
        "tmux_agent_tower.control.actions.find_screen_target",
        lambda *_: (True, "", "%9"),
    )
    monkeypatch.setattr("tmux_agent_tower.control.actions.pane_process_id", lambda _pane: "900")
    monkeypatch.setattr(
        "tmux_agent_tower.control.actions.tmux_capture.pane_history_position",
        lambda _pane: (history_size, 2000),
    )
    monkeypatch.setattr(
        "tmux_agent_tower.control.actions.tmux_capture.capture_pane",
        lambda *_a, **_k: captured,
    )

    ok, reason, payload = get_result(tower, "%9", expected_pane_pid="900")

    assert ok and reason == ""
    assert payload["text"] == expected_text
    assert payload["complete"] is expected_complete
    if expected_complete:
        assert payload["body_complete"] is True and payload["turn_complete"] is True
        assert payload["source"] == "ssh_visible_history"
    else:
        assert payload["body_complete"] is False
        assert payload["reason_code"] == expected_reason
        assert payload["fallback_reason_code"] == "OPENCODE_PLUGIN_NOT_INSTALLED"


@pytest.mark.parametrize(
    ("provider_payload", "expected_reason"),
    [
        ({"status": "AGENT_SESSION_UNBOUND"}, "AGENT_SESSION_UNBOUND"),
        ({"status": "AGENT_SESSION_UNBOUND", "session_binding": "TUI_BINDING_INVALID"}, "OPENCODE_BINDING_INVALID"),
        ({"status": "AGENT_SESSION_UNBOUND", "session_binding": "NO_ACTIVE_SESSION"}, "OPENCODE_NO_ACTIVE_SESSION"),
        ({"status": "REMOTE_RESULT_INCOMPLETE", "reason_code": "SESSION_BINDING_CHANGED"}, "SESSION_BINDING_CHANGED"),
    ],
)
def test_tower_does_not_capture_for_invalid_or_changed_opencode_binding(monkeypatch, provider_payload, expected_reason):
    import pytest
    from tmux_agent_tower.control.actions import get_result

    tower = _Tower()
    _bind_local_ssh(monkeypatch)
    monkeypatch.setattr(
        "tmux_agent_tower.remote.opencode.fetch_remote_opencode_result",
        lambda *_: provider_payload,
    )
    monkeypatch.setattr(
        "tmux_agent_tower.control.actions._recover_result",
        lambda *_a, **_k: pytest.fail("unbound OpenCode must not reach capture"),
    )

    ok, reason, payload = get_result(tower, "%9", expected_pane_pid="900")

    assert ok and reason == ""
    assert payload["text"] == "" and not payload["complete"]
    assert payload["reason_code"] == expected_reason


def test_tower_blocks_changed_socket_and_never_falls_back_to_suffix(monkeypatch):
    import pytest
    from tmux_agent_tower.control.actions import get_result

    tower = _Tower()
    calls = _bind_local_ssh(monkeypatch, after=dict(_connection(), client_port="43124"))
    monkeypatch.setattr("tmux_agent_tower.remote.opencode.fetch_remote_opencode_result", lambda *_: _provider_payload("good"))
    monkeypatch.setattr("tmux_agent_tower.control.actions._recover_result", lambda *_a, **_k: pytest.fail("no SSH suffix fallback"))
    ok, _, payload = get_result(tower, "%9", expected_pane_pid="900")

    assert ok and not payload["complete"] and payload["text"] == ""
    assert payload["source"] == "REMOTE_PROVIDER_UNBOUND"
    assert len(calls) == 2


def test_tower_does_not_fallback_when_remote_export_is_length_limited(monkeypatch):
    import pytest
    from tmux_agent_tower.control.actions import get_result

    tower = _Tower()
    _bind_local_ssh(monkeypatch)
    monkeypatch.setattr(
        "tmux_agent_tower.remote.opencode.fetch_remote_opencode_result",
        lambda *_: {"status": "REMOTE_RESULT_INCOMPLETE"},
    )
    monkeypatch.setattr(
        "tmux_agent_tower.control.actions._recover_result",
        lambda *_a, **_k: pytest.fail("length-limited export must not use screen fallback"),
    )
    ok, _, payload = get_result(tower, "%9", expected_pane_pid="900")

    assert ok and not payload["complete"] and payload["text"] == ""
    assert payload["reason_code"] == "REMOTE_RESULT_INCOMPLETE"


def test_y_routes_only_verified_opencode_body_through_existing_clipboard_router(monkeypatch):
    from tmux_agent_tower.clipboard import CopyOutcome
    from tmux_agent_tower.ui import control_view

    tower = _Tower()
    body = "한글 **Markdown**\n\n```python\nprint('full')\n```"
    row = tower.rows[0]
    payload = {
        "state": "ready", "text": body, "complete": True, "turn_complete": True,
        "body_complete": True, "fingerprint": hashlib.sha256(body.encode()).hexdigest(),
    }
    routed = []
    monkeypatch.setattr(control_view, "find_row", lambda *_: row)
    monkeypatch.setattr(control_view, "get_result", lambda *_a, **kwargs: (True, "", payload) if kwargs.get("expected_pane_pid") == "900" else (False, "stale", {}))
    monkeypatch.setattr(control_view, "_route_clipboard", lambda _tower, text, _screen: routed.append(text) or (CopyOutcome(True, False, "clip.exe"), ""))

    notice = control_view._copy_result(tower, "%9")
    assert routed == [body]
    assert hashlib.sha256(routed[0].encode()).hexdigest() == payload["fingerprint"]
    assert "복사했습니다" in notice


def test_y_does_not_route_unbound_or_incomplete_opencode_payload(monkeypatch):
    import pytest
    from tmux_agent_tower.ui import control_view

    tower = _Tower()
    row = tower.rows[0]
    monkeypatch.setattr(control_view, "find_row", lambda *_: row)
    monkeypatch.setattr(control_view, "get_result", lambda *_a, **_k: (True, "", {
        "state": "none", "text": "", "complete": False, "turn_complete": False,
        "body_complete": False, "source": "AGENT_SESSION_UNBOUND",
    }))
    monkeypatch.setattr(control_view, "_route_clipboard", lambda *_: pytest.fail("unsafe payload reached clipboard"))

    notice = control_view._copy_result(tower, "%9")
    assert "AGENT_SESSION_UNBOUND" in notice


def test_y_explains_bounded_screen_fallback_when_binding_plugin_is_absent(monkeypatch):
    from tmux_agent_tower.clipboard import CopyOutcome
    from tmux_agent_tower.ui import control_view

    tower = _Tower()
    row = tower.rows[0]
    payload = {
        "state": "ready", "text": "complete bounded result", "complete": True,
        "turn_complete": True, "body_complete": True, "source": "ssh_visible_history",
        "fallback_reason_code": "OPENCODE_PLUGIN_NOT_INSTALLED",
    }
    monkeypatch.setattr(control_view, "find_row", lambda *_: row)
    monkeypatch.setattr(control_view, "get_result", lambda *_a, **_k: (True, "", payload))
    monkeypatch.setattr(control_view, "_route_clipboard", lambda *_: (CopyOutcome(True, False, "clip.exe"), ""))

    notice = control_view._copy_result(tower, "%9")

    assert "연결 플러그인이 없어" in notice
    assert tower.last_result_copy_diagnostic["reason_code"] == "OPENCODE_PLUGIN_NOT_INSTALLED"


def test_y_explains_incomplete_screen_fallback_and_binding_failure_in_korean(monkeypatch):
    import pytest
    from tmux_agent_tower.ui import control_view

    tower = _Tower()
    row = tower.rows[0]
    monkeypatch.setattr(control_view, "find_row", lambda *_: row)
    monkeypatch.setattr(control_view, "_route_clipboard", lambda *_: pytest.fail("incomplete result reached copy"))

    incomplete = {
        "state": "none", "text": "", "complete": False, "turn_complete": False,
        "body_complete": False, "reason_code": "OUTPUT_RANGE_EXCEEDED",
        "fallback_reason_code": "OPENCODE_PLUGIN_NOT_INSTALLED",
    }
    monkeypatch.setattr(control_view, "get_result", lambda *_a, **_k: (True, "", incomplete))
    fallback_notice = control_view._copy_result(tower, "%9")
    assert "화면 기반 대체 경로를 확인했지만 전체 결과를 검증하지 못해" in fallback_notice
    assert "OUTPUT_RANGE_EXCEEDED" in fallback_notice

    damaged = {
        "state": "none", "text": "", "complete": False, "turn_complete": False,
        "body_complete": False, "reason_code": "OPENCODE_BINDING_INVALID",
    }
    monkeypatch.setattr(control_view, "get_result", lambda *_a, **_k: (True, "", damaged))
    binding_notice = control_view._copy_result(tower, "%9")
    assert "오래됐거나 손상되어 복사를 차단" in binding_notice
