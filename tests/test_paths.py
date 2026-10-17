import os
import subprocess
import sys
from pathlib import Path

import pytest

from tmux_agent_tower.paths import configured_path, resolve_app_home


def test_default_app_home_remains_user_home():
    path, isolated = resolve_app_home(environ={}, home=Path("/home/person"))

    assert path == Path("/home/person")
    assert isolated is False


def test_app_home_override_must_be_absolute_and_does_not_change_home():
    path, isolated = resolve_app_home(
        environ={"TMUX_AGENT_TOWER_HOME": "/tmp/tower-sidecar"},
        home=Path("/home/person"),
    )

    assert path == Path("/tmp/tower-sidecar")
    assert isolated is True
    with pytest.raises(ValueError, match="absolute path"):
        resolve_app_home(environ={"TMUX_AGENT_TOWER_HOME": "relative"}, home=Path("/home/person"))


def test_external_read_only_profile_paths_require_absolute_paths():
    default = Path("/tmp/tower-sidecar/remote-hosts.txt")

    assert configured_path("TOWER_PROFILE", default, {}) == default
    assert configured_path("TOWER_PROFILE", default, {"TOWER_PROFILE": "/home/person/remote-hosts.txt"}) == Path(
        "/home/person/remote-hosts.txt"
    )
    with pytest.raises(ValueError, match="absolute path"):
        configured_path("TOWER_PROFILE", default, {"TOWER_PROFILE": "remote-hosts.txt"})


def test_imported_tower_storage_moves_to_sidecar_while_home_and_ssh_profile_stay_put(tmp_path):
    sidecar = tmp_path / "tower-isolated-home"
    remote_hosts = tmp_path / "existing-remote-hosts.txt"
    host_label = tmp_path / "existing-host-label"
    code = r'''
import os
from pathlib import Path
from tmux_agent_tower import paths
from tmux_agent_tower.ui import tower
from tmux_agent_tower.launcher import config, presets
from tmux_agent_tower import i18n, clipboard_bridge, role_harness
from tmux_agent_tower.detection import result
from tmux_agent_tower.server import httpapi, service
from tmux_agent_tower.state import worksets
from tmux_agent_tower.tmux import keybind
from tmux_agent_tower import update_manager
from tmux_agent_tower.ui.tower import Tower
root = Path(os.environ["TMUX_AGENT_TOWER_HOME"])
assert Path.home() == Path(os.environ["HOME"])
assert paths.APP_HOME_ISOLATED
assert tower.STATE_DIR == root / ".cache/tmux-agent-tower"
assert tower.CONFIG_DIR == root / ".config/tmux-agent-tower"
assert tower.REMOTE_HOSTS_FILE == Path(os.environ["TMUX_AGENT_TOWER_REMOTE_HOSTS_FILE"])
assert tower.HOST_FILE == Path(os.environ["TMUX_AGENT_TOWER_HOST_FILE"])
assert config.CONFIG_FILE == root / ".config/tmux-agent-tower/config.toml"
assert i18n.CONFIG_FILE == config.CONFIG_FILE
assert presets.PRESET_FILE.parent == root / ".config/tmux-agent-tower"
assert worksets.WORKSET_FILE.parent == root / ".config/tmux-agent-tower"
assert role_harness.DEFAULT_HARNESS_DIR.parent == root / ".config/tmux-agent-tower"
assert service.CONFIG_DIR == root / ".config/tmux-agent-tower"
assert httpapi.default_token_path().parent == root / ".config/tmux-agent-tower"
assert result.shared_result_state_path().parent == root / ".cache/tmux-agent-tower"
assert clipboard_bridge.bridge_state_dir().parent == root / ".local/state/tmux-agent-tower"
assert keybind.state_path().parent == root / ".config/tmux-agent-tower"
assert update_manager.data_root() == root / ".local/share/tmux-agent-tower"
tower = Tower(session="sidecar")
assert tower.discover_all_sessions
assert tower._supports_local_pane_title_sync({
    "kind": "pane", "remote": False, "transport": "local", "tmux_host": "host"
}) is False
print("SIDE_CAR_PATHS_OK")
'''
    env = os.environ.copy()
    env["TMUX_AGENT_TOWER_HOME"] = str(sidecar)
    env["TMUX_AGENT_TOWER_REMOTE_HOSTS_FILE"] = str(remote_hosts)
    env["TMUX_AGENT_TOWER_HOST_FILE"] = str(host_label)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    completed = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, env=env,
        timeout=15, check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "SIDE_CAR_PATHS_OK"


def test_isolated_keybinding_commands_refuse_user_tmux_config_writes(monkeypatch):
    from tmux_agent_tower.tmux import keybind

    monkeypatch.setattr(keybind, "APP_HOME_ISOLATED", True)
    monkeypatch.setattr(keybind, "query_tmux_default_w", lambda: pytest.fail("must not query or mutate tmux"))
    monkeypatch.setattr(keybind, "restore_smart_w", lambda *_a, **_kw: pytest.fail("must not restore config"))

    with pytest.raises(RuntimeError, match="isolated Tower mode"):
        keybind.install_for_user()
    with pytest.raises(RuntimeError, match="isolated Tower mode"):
        keybind.restore_for_user()
