from types import SimpleNamespace

from tmux_agent_tower.access_context import (
    ACCESS_CONTEXT_ENV,
    AccessContext,
    at_tower,
    context_from_dict,
    context_from_environment,
    decode_access_context,
    encode_access_context,
)
from tmux_agent_tower.clipboard_dest import (
    AUTO,
    CURRENT_TERMINAL,
    LOCAL_HOST,
    CopyClient,
    observe,
    resolve_plan,
    route_for_access_client,
)


def _context(**changes):
    values = {
        "client_id": "a" * 32,
        "display_name": "workstation-b",
        "platform": "linux-wayland",
        "transport": "ssh",
        "access_host": "workstation-b",
        "tower_host": "workstation-a-WSL",
        "clipboard_capability": "bridge",
        "registration_timestamp": 1234.0,
        "liveness": "live",
    }
    values.update(changes)
    return AccessContext(**values)


def test_context_roundtrip_and_input_validation():
    context = _context()
    encoded = encode_access_context(context)
    assert decode_access_context(encoded) == context
    assert context_from_environment(f"OTHER=x\0{ACCESS_CONTEXT_ENV}={encoded}\0".encode()) == context
    assert decode_access_context("not-base64") is None
    assert context_from_dict({**context.__dict__, "platform": "untrusted"}) is None


def test_platform_uses_the_only_wayland_socket_without_requiring_tmux_env(monkeypatch, tmp_path):
    import socket

    from tmux_agent_tower.access_context import platform_name

    server = socket.socket(socket.AF_UNIX)
    try:
        server.bind(str(tmp_path / "wayland-0"))
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
        monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
        assert platform_name({"XDG_RUNTIME_DIR": str(tmp_path), "TOWER_UNAME": "Linux testhost"}) == "linux-wayland"
    finally:
        server.close()


def test_platform_prioritizes_wsl_over_wayland_socket(tmp_path):
    import socket

    from tmux_agent_tower.access_context import platform_name

    server = socket.socket(socket.AF_UNIX)
    try:
        server.bind(str(tmp_path / "wayland-0"))
        assert platform_name({
            "XDG_RUNTIME_DIR": str(tmp_path),
            "TOWER_UNAME": "microsoft-standard-WSL2",
        }) == "windows-wsl"
    finally:
        server.close()


def test_nested_ssh_keeps_originating_access_client():
    context = _context()
    nested = at_tower(context, "SERVER-B")
    assert nested.access_host == "workstation-b"
    assert nested.client_id == context.client_id
    assert nested.tower_host == "SERVER-B"


def test_access_and_tower_hosts_stay_independent_in_auto_routing():
    context = _context()
    client = CopyClient("/dev/pts/8", 44, True, False, "a" * 32, context)
    plan = resolve_plan(AUTO, client, (("/dev/pts/8", 44),))
    assert (context.access_host, context.tower_host) == ("workstation-b", "workstation-a-WSL")
    assert plan.destination == CURRENT_TERMINAL
    assert not plan.ask


def test_same_result_source_routes_to_each_access_client():
    clients = (("/dev/pts/8", 44),)
    remote_access = CopyClient("/dev/pts/8", 44, True, False, "a" * 32, _context())
    local_context = _context(
        client_id="b" * 32, display_name="workstation-a", transport="local",
        access_host="workstation-a", tower_host="workstation-a", clipboard_capability="host",
    )
    local_access = CopyClient("/dev/pts/8", 44, False, False, access_context=local_context)
    assert resolve_plan(AUTO, remote_access, clients).destination == CURRENT_TERMINAL
    assert resolve_plan(AUTO, local_access, clients).destination == LOCAL_HOST


def test_registered_bridge_context_does_not_depend_on_ssh_ancestry(monkeypatch):
    context = _context()
    monkeypatch.setattr("tmux_agent_tower.clipboard_dest.client_rows", lambda _lines: [("/dev/pts/8", 44, "")])
    monkeypatch.setattr(
        "tmux_agent_tower.clipboard_dest.resolve_terminal_target",
        lambda *_args: SimpleNamespace(tty="/dev/pts/8", ambiguous=False, ssh=False),
    )
    monkeypatch.setattr("tmux_agent_tower.clipboard_dest.context_for_process", lambda pid: context if pid == 44 else None)
    monkeypatch.setattr("tmux_agent_tower.clipboard_dest.bridge_id_for_process", lambda _pid: "a" * 32)
    monkeypatch.setattr(
        "tmux_agent_tower.clipboard_dest.load_bridge_client",
        lambda _client_id: SimpleNamespace(access_context=context),
    )

    client, _clients = observe(["/dev/pts/8\t44\t"])

    assert client.access_context.access_host == "workstation-b"
    assert client.access_context.client_id == "a" * 32
    assert client.ssh


def test_local_access_detects_clipboard_capability_without_runtime_error(monkeypatch):
    monkeypatch.setattr("tmux_agent_tower.clipboard_dest.client_rows", lambda _lines: [("/dev/pts/1", 4, "")])
    monkeypatch.setattr(
        "tmux_agent_tower.clipboard_dest.resolve_terminal_target",
        lambda *_args: SimpleNamespace(tty="/dev/pts/1", ambiguous=False, ssh=False),
    )
    monkeypatch.setattr("tmux_agent_tower.clipboard_dest.context_for_process", lambda _pid: None)
    monkeypatch.setattr("tmux_agent_tower.clipboard_dest.bridge_id_for_process", lambda _pid: None)
    monkeypatch.setattr("tmux_agent_tower.clipboard_dest.native_clipboard_provider", lambda: "wl-copy")

    client, clients = observe(["/dev/pts/1\t4\t"])

    assert clients == (("/dev/pts/1", 4),)
    assert client.access_context is not None
    assert client.access_context.transport == "local"
    assert client.access_context.clipboard_capability == "host"


def test_unknown_or_stale_remote_access_fails_closed():
    clients = (("/dev/pts/8", 44),)
    unknown = CopyClient("/dev/pts/8", 44, True, False)
    assert resolve_plan(AUTO, unknown, clients).ask
    stale = CopyClient("/dev/pts/8", 44, True, False, "a" * 32, _context(liveness="stale"))
    plan = resolve_plan(AUTO, stale, clients)
    assert plan.ask and plan.destination == AUTO


def test_local_access_client_routes_to_local_clipboard():
    context = _context(transport="local", clipboard_capability="host")
    client = CopyClient("/dev/pts/1", 4, False, False, access_context=context)
    plan = resolve_plan(AUTO, client, (("/dev/pts/1", 4),))
    assert plan.destination == LOCAL_HOST and not plan.ask
    assert route_for_access_client(LOCAL_HOST, client) == LOCAL_HOST
