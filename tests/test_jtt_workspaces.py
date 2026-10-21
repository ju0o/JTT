import json
import os
import stat

import pytest

from tmux_agent_tower.state.jtt_workspaces import JTTWorkspaceStore, WorkspaceStoreError


def test_opening_store_does_not_create_directory_or_workspace_file(tmp_path):
    root = tmp_path / "jtt-state"
    store = JTTWorkspaceStore(root)

    assert store.workspaces() == [{
        "id": store.DEFAULT_ID,
        "name": "내 작업공간",
        "terminal_ids": [],
        "saved": False,
    }]
    assert not root.exists()


def test_workspace_configuration_is_private_and_round_trips(tmp_path):
    root = tmp_path / "private" / "jtt"
    store = JTTWorkspaceStore(root)
    default = store.workspaces()[0]

    store.set_terminal_ids(default["id"], ["session-a", "session-b", "session-a"])
    saved = store.save_configuration("", "  작업   세션  ", ["session-a", "session-b"])

    assert saved["name"] == "작업 세션"
    assert saved["terminal_ids"] == ["session-a", "session-b"]
    assert {item["id"]: item for item in store.workspaces()}[default["id"]]["terminal_ids"] == [
        "session-a", "session-b",
    ]
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    payload = json.loads(store.path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 1


def test_symlinked_workspace_file_fails_closed(tmp_path):
    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    target = tmp_path / "elsewhere.json"
    target.write_text('{"schema_version":1,"workspaces":[]}', encoding="utf-8")
    try:
        store = JTTWorkspaceStore(root)
        store.path.symlink_to(target)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")

    with pytest.raises(WorkspaceStoreError):
        store.workspaces()


def test_insecure_existing_workspace_file_is_rejected(tmp_path):
    store = JTTWorkspaceStore(tmp_path / "private")
    store._ensure_private_root()
    store.path.write_text('{"schema_version":1,"workspaces":[]}', encoding="utf-8")
    os.chmod(store.path, 0o644)

    with pytest.raises(WorkspaceStoreError):
        store.workspaces()
