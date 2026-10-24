"""Native Windows checks for the shared, private JTT state schemas."""

from __future__ import annotations

import json
import sys

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="native Windows required")


def test_windows_workspace_and_layout_stores_share_private_jtt_home(tmp_path, monkeypatch):
    from ju0_terminal_tower.state.jtt_layouts import JTTLayoutStore
    from ju0_terminal_tower.state.jtt_workspaces import JTTWorkspaceStore, default_jtt_root
    from ju0_terminal_tower.terminal.windows_manager import (
        _default_root as terminal_default_root,
        _private_acl,
    )

    home = tmp_path / "isolated-jtt-home"
    monkeypatch.setenv("JTT_HOME", str(home))
    root = default_jtt_root()
    assert root == home
    assert terminal_default_root() == home / "terminals"

    workspaces = JTTWorkspaceStore(root)
    layouts = JTTLayoutStore(root)
    default = workspaces.workspaces()[0]
    saved = workspaces.create_workspace("Saved configuration")
    workspaces.add_terminal_reference(saved["id"], "session-fixture")
    window = layouts.create_window(saved["id"], "folder-default", "Build")
    layouts.move_terminal(saved["id"], "session-fixture", window["id"],
                          ["session-fixture"])

    assert next(item for item in workspaces.workspaces()
                if item["id"] == saved["id"])["terminal_ids"] == ["session-fixture"]
    projected = layouts.layout(saved["id"], ["session-fixture"])
    assert next(item for item in projected["windows"]
                if item["id"] == window["id"])["terminal_ids"] == ["session-fixture"]
    assert next(item for item in projected["windows"]
                if item["id"] == "window-default")["terminal_ids"] == []
    assert default["terminal_ids"] == []

    assert json.loads(workspaces.path.read_text(encoding="utf-8"))["schema_version"] == 1
    assert json.loads(layouts.path.read_text(encoding="utf-8"))["schema_version"] == 1
    for path, directory in ((root, True), (root / ".jtt-state.lock", False),
                            (workspaces.path, False), (layouts.path, False)):
        _private_acl(path, directory=directory)
    assert (root / ".jtt-state.lock").stat().st_size == 1


def test_windows_new_state_lock_is_current_user_private_across_writes(tmp_path):
    from ju0_terminal_tower.state.jtt_workspaces import JTTWorkspaceStore
    from ju0_terminal_tower.terminal.windows_manager import _private_acl

    root = tmp_path / "isolated-jtt-home"
    workspaces = JTTWorkspaceStore(root)

    first = workspaces.create_workspace("First")
    lock_path = root / ".jtt-state.lock"
    _private_acl(lock_path, directory=False)

    second = workspaces.create_workspace("Second")
    _private_acl(lock_path, directory=False)

    assert first["id"] != second["id"]
    assert len(workspaces.workspaces()) == 3  # default plus two saved workspaces


def test_windows_default_state_root_uses_local_app_data_without_jtt_home(tmp_path):
    from ju0_terminal_tower.state.jtt_workspaces import (
        WorkspaceStoreError,
        default_jtt_root,
    )

    assert default_jtt_root({"LOCALAPPDATA": str(tmp_path)}) == tmp_path / "JTT"
    with pytest.raises(WorkspaceStoreError, match="LOCALAPPDATA"):
        default_jtt_root({})
