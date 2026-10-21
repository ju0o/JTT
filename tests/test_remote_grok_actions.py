import hashlib

from tmux_agent_tower.control.actions import get_result


class Tower:
    session = "tower-test"
    own_pane_id = "%1"
    results = None

    def __init__(self):
        self.rows = [{
            "key": "%9", "pane_id": "%9", "pane_pid": "900",
            "tower_pane_id": "%9", "tower_pane_pid": "900", "remote": False,
            "transport": "ssh", "transport_target": "remote-host", "execution_host": "원격 장비",
            "agent": "Grok", "command": "ssh", "pane_title": "Grok",
            "result_provider_type": "ssh_visible_history",
        }]

    def load(self):
        pass


def _connection():
    return {
        "ssh_client_pid": "901", "client_ip": "198.51.100.23", "client_port": "43123",
        "server_ip": "203.0.113.31", "server_port": "22",
        "local_socket": ("192.0.2.23", "57120", "203.0.113.31", "22"),
        "connection_mode": "wsl_nat",
    }


def _result(body):
    encoded = body.encode()
    return {
        "status": "ok", "ssh_connection": ["198.51.100.23", "43123", "203.0.113.31", "22"],
        "remote_session_pid": "100", "remote_sshd_pid": "90", "grok_pid": "200",
        "session_id": "12345678-1234-1234-1234-000000043123", "turn_number": 3,
        "turn_complete": True, "body_complete": True, "native_length": len(body),
        "native_utf8_bytes": len(encoded), "native_sha256": hashlib.sha256(encoded).hexdigest(),
        "text": body,
    }


def test_tower_resolves_the_exact_remote_grok_provider(monkeypatch):
    body = "REMOTE_GROK_BEGIN\n한글 **Markdown**\nREMOTE_GROK_FINAL"
    calls = []
    connection = _connection()
    monkeypatch.setattr(
        "tmux_agent_tower.detection.sshdest.ssh_connection_for_pane",
        lambda pid, host: connection if (pid, host) == ("900", "remote-host") else None,
    )
    monkeypatch.setattr(
        "tmux_agent_tower.remote.collector.fetch_remote_grok_result",
        lambda host, bound: calls.append((host, bound)) or _result(body),
    )

    ok, reason, payload = get_result(Tower(), "%9")

    assert ok and reason == ""
    assert payload["complete"] and payload["turn_complete"] and payload["body_complete"]
    assert payload["source"] == "remote_grok" and payload["text"] == body
    assert payload["native_sha256"] == hashlib.sha256(body.encode()).hexdigest()
    assert payload["ssh_connection"] == (
        "198.51.100.23", "43123", "203.0.113.31", "22",
    )
    assert calls[0][0] == "remote-host"
    assert calls[0][1]["ssh_client_pid"] == "901"


def test_unbound_or_unavailable_provider_never_uses_local_suffix(monkeypatch):
    import pytest

    monkeypatch.setattr(
        "tmux_agent_tower.control.actions._recover_result",
        lambda *_args, **_kwargs: pytest.fail("unbound remote Grok cannot read local suffix history"),
    )
    monkeypatch.setattr(
        "tmux_agent_tower.detection.sshdest.ssh_connection_for_pane", lambda *_args: None,
    )
    ok, _, unbound = get_result(Tower(), "%9")
    assert ok and not unbound["complete"] and unbound["text"] == ""
    assert unbound["source"] == "REMOTE_PROVIDER_UNBOUND"

    monkeypatch.setattr(
        "tmux_agent_tower.detection.sshdest.ssh_connection_for_pane",
        lambda *_args: _connection(),
    )
    monkeypatch.setattr(
        "tmux_agent_tower.remote.collector.fetch_remote_grok_result",
        lambda *_args, **_kwargs: {"status": "REMOTE_PROVIDER_UNAVAILABLE"},
    )
    ok, _, unavailable = get_result(Tower(), "%9")
    assert ok and not unavailable["complete"] and unavailable["text"] == ""
    assert unavailable["source"] == "REMOTE_PROVIDER_UNAVAILABLE"


def test_malformed_remote_provider_status_is_unknown(monkeypatch):
    from tmux_agent_tower.control.actions import _recover_ssh_grok_result

    monkeypatch.setattr(
        "tmux_agent_tower.remote.collector.fetch_remote_grok_result",
        lambda *_args, **_kwargs: {},
    )
    result = _recover_ssh_grok_result({
        "result_provider_type": "remote_grok", "result_provider_endpoint": "host",
        "ssh_client_pid": "123", "ssh_connection": ("192.0.2.1", "22", "192.0.2.2", "50000"),
    })

    assert result["source"] == "UNKNOWN"


def test_provider_rejects_incomplete_native_turn_and_mismatched_tuple(monkeypatch):
    connection = _connection()
    monkeypatch.setattr(
        "tmux_agent_tower.detection.sshdest.ssh_connection_for_pane",
        lambda *_args: connection,
    )
    body = "complete-looking suffix"
    result = _result(body)
    result["turn_complete"] = False
    monkeypatch.setattr(
        "tmux_agent_tower.remote.collector.fetch_remote_grok_result",
        lambda *_args, **_kwargs: result,
    )
    ok, _, payload = get_result(Tower(), "%9")
    assert ok and not payload["complete"] and payload["source"] == "REMOTE_RESULT_INCOMPLETE"

    result = _result(body)
    result["ssh_connection"][1] = "43124"
    monkeypatch.setattr(
        "tmux_agent_tower.remote.collector.fetch_remote_grok_result",
        lambda *_args, **_kwargs: result,
    )
    ok, _, payload = get_result(Tower(), "%9")
    assert ok and not payload["complete"] and payload["source"] == "REMOTE_PROVIDER_UNBOUND"
