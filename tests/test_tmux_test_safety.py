import os
import shutil
import socket
import stat
import subprocess
from pathlib import Path

import pytest

from tmux_test_safety import TmuxSafetyError, _safe_stop_socket, run_isolated_tmux


pytestmark = pytest.mark.skipif(not shutil.which("tmux"), reason="tmux unavailable")


def test_unqualified_tmux_commands_are_pinned_and_can_only_stop_the_private_server(tmux_test_safety):
    created = subprocess.run(
        ["tmux", "new-session", "-d", "-s", "guard-fixture", "sleep", "30"],
        capture_output=True, text=True, timeout=10, check=False,
    )
    assert created.returncode == 0, created.stderr
    path = tmux_test_safety.socket_path
    assert path.exists() and stat.S_ISSOCK(path.lstat().st_mode)
    assert path.stat().st_uid == os.geteuid()

    stopped = subprocess.run(
        ["tmux", "kill-server"], capture_output=True, text=True, timeout=10, check=False,
    )
    assert stopped.returncode == 0, stopped.stderr
    assert not path.exists()


def test_external_socket_target_is_rejected_before_tmux_runs(tmp_path):
    external = tmp_path / "not-the-private-test-socket"
    server_socket = socket.socket(socket.AF_UNIX)
    try:
        server_socket.bind(str(external))
        result = subprocess.run(
            ["tmux", "-S", str(external), "kill-server"],
            capture_output=True, text=True, timeout=5, check=False,
        )
        assert result.returncode == 125
        assert "outside private test root" in result.stderr
        assert external.exists()
    finally:
        server_socket.close()


def test_external_tmux_config_is_rejected_before_server_creation(tmp_path):
    config = tmp_path / "outside-test-root.conf"
    config.write_text("bind-key -T prefix x display-message unexpected\n", encoding="utf-8")
    result = subprocess.run(
        ["tmux", "-f", str(config), "new-session", "-d", "-s", "must-not-exist"],
        capture_output=True, text=True, timeout=5, check=False,
    )
    assert result.returncode == 125
    assert "config is outside private test root" in result.stderr
    assert not (Path(os.environ["JTT_TEST_TMUX_ROOT"]) / "server.sock").exists()


def test_direct_cleanup_without_verified_test_server_fails_closed():
    with pytest.raises(TmuxSafetyError, match="socket is missing|expected test socket"):
        run_isolated_tmux("kill-server")


def test_stale_private_socket_is_unlinked_only_after_kernel_confirms_no_listener(tmux_test_safety):
    stale = socket.socket(socket.AF_UNIX)
    stale.bind(str(tmux_test_safety.socket_path))
    stale.close()
    assert tmux_test_safety.socket_path.exists()

    assert _safe_stop_socket(
        tmux_test_safety.binary, tmux_test_safety.socket_path, tmux_test_safety.root,
    ) is None
    assert not tmux_test_safety.socket_path.exists()


def test_teardown_tolerates_tmux_removing_private_socket_dir_during_enumeration(
        monkeypatch, tmux_test_safety):
    socket_dir = tmux_test_safety.root / ("tmux-" + str(os.geteuid()))
    socket_dir.mkdir(mode=0o700)
    original_lstat = Path.lstat

    def disappearing_lstat(path):
        if path == socket_dir:
            raise FileNotFoundError
        return original_lstat(path)

    monkeypatch.setattr(Path, "lstat", disappearing_lstat)
    assert tmux_test_safety._socket_paths() == []
    socket_dir.rmdir()


def test_explicit_helper_drops_inherited_tmux_routing(monkeypatch, tmux_test_safety):
    monkeypatch.setenv("TMUX", "/tmp/not-a-test-socket,999,0")
    monkeypatch.setenv("TMUX_PANE", "%999")
    created = run_isolated_tmux("new-session", "-d", "-s", "cleared-env", "sleep", "30")
    assert created.returncode == 0, created.stderr
    assert tmux_test_safety.socket_path.exists()
    assert run_isolated_tmux("kill-server").returncode == 0
    assert not tmux_test_safety.socket_path.exists()
