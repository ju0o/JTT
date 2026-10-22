import io

from tmux_agent_tower import main
from tmux_agent_tower.server import service
from tmux_agent_tower.server.auth import PairingSession, TokenStore


def test_serve_subcommand_routes_with_defaults(monkeypatch):
    calls = []
    monkeypatch.setattr(main, "run_serve", lambda lan, port, use_tailscale=False: calls.append((lan, port, use_tailscale)))
    main.cli(["serve"])
    assert calls == [(False, main.DEFAULT_SERVE_PORT, False)]


def test_serve_subcommand_with_lan_and_port(monkeypatch):
    calls = []
    monkeypatch.setattr(main, "run_serve", lambda lan, port, use_tailscale=False: calls.append((lan, port, use_tailscale)))
    main.cli(["serve", "--lan", "--port", "9999"])
    assert calls == [(True, 9999, False)]


def test_serve_subcommand_with_tailscale(monkeypatch):
    calls = []
    monkeypatch.setattr(main, "run_serve", lambda lan, port, use_tailscale=False: calls.append((lan, port, use_tailscale)))
    main.cli(["serve", "--tailscale"])
    assert calls == [(False, main.DEFAULT_SERVE_PORT, True)]


def test_lan_and_tailscale_are_mutually_exclusive():
    import pytest

    with pytest.raises(SystemExit):
        main.cli(["serve", "--lan", "--tailscale"])


def test_no_subcommand_still_runs_normal_ui(monkeypatch):
    calls = []
    monkeypatch.setattr(main, "run_ui", lambda: calls.append("ui"))
    main.cli([])
    assert calls == ["ui"]


def test_version_flag_still_works(monkeypatch, capsys):
    main.cli(["--version"])
    out = capsys.readouterr().out
    assert out.strip() == "tower 0.3.0rc3"


class _FakeServer:
    def __init__(self, pairing):
        self.pairing = pairing


def test_stdin_watcher_regenerates_on_r(tmp_path, capsys):
    pairing = PairingSession(TokenStore(tmp_path / "tokens.json"))
    old_code = pairing.current_code()
    server = _FakeServer(pairing)

    stdin = io.StringIO("r\n")
    real_stdin = service.sys.stdin
    service.sys.stdin = stdin
    try:
        service._watch_stdin_for_regenerate(server)
    finally:
        service.sys.stdin = real_stdin

    out = capsys.readouterr().out
    assert "New pairing code:" in out
    assert pairing.try_pair(old_code) is None  # old code was invalidated


def test_stdin_watcher_ignores_other_input(tmp_path, capsys):
    pairing = PairingSession(TokenStore(tmp_path / "tokens.json"))
    code = pairing.current_code()
    server = _FakeServer(pairing)

    stdin = io.StringIO("hello\nwhatever\n")
    real_stdin = service.sys.stdin
    service.sys.stdin = stdin
    try:
        service._watch_stdin_for_regenerate(server)
    finally:
        service.sys.stdin = real_stdin

    out = capsys.readouterr().out
    assert "New pairing code:" not in out
    assert pairing.try_pair(code) is not None  # original code still valid


def test_stdin_watcher_returns_when_stdin_closes(tmp_path):
    pairing = PairingSession(TokenStore(tmp_path / "tokens.json"))
    server = _FakeServer(pairing)

    stdin = io.StringIO("")  # immediately EOF
    real_stdin = service.sys.stdin
    service.sys.stdin = stdin
    try:
        service._watch_stdin_for_regenerate(server)  # must return, not hang
    finally:
        service.sys.stdin = real_stdin
