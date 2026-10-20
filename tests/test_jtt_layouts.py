import json
import os
import stat

import pytest

from tmux_agent_tower.state.jtt_layouts import (
    JTTLayoutStore,
    JTTLayoutStoreCommittedDurabilityError,
    JTTLayoutStoreError,
)


def test_legacy_refs_project_once_into_virtual_default_without_writing(tmp_path):
    store = JTTLayoutStore(tmp_path / "state")
    result = store.layout("workspace-a", ["session-a", "session-b", "session-a"])

    assert result["terminal_ids"] == ["session-a", "session-b"]
    assert result["folders"] == [{"id": "folder-default", "name": "기본 폴더"}]
    assert result["windows"][0]["terminal_ids"] == ["session-a", "session-b"]
    assert not store.root.exists()


def test_folder_window_round_trip_and_deletion_preserve_canonical_session_refs(tmp_path):
    store = JTTLayoutStore(tmp_path / "state")
    folder = store.create_folder("workspace-a", "프로젝트")
    window = store.create_window("workspace-a", folder["id"], "리뷰")
    store.move_terminal("workspace-a", "session-a", window["id"], ["session-a", "session-b"])
    store.move_terminal("workspace-a", "session-b", window["id"], ["session-a", "session-b"])

    reopened = JTTLayoutStore(store.root).layout("workspace-a", ["session-a", "session-b"])
    assert reopened["windows"][1]["terminal_ids"] == ["session-a", "session-b"]
    assert reopened["terminal_ids"] == ["session-a", "session-b"]
    assert stat.S_IMODE(store.root.stat().st_mode) == 0o700
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600

    store.delete_window("workspace-a", window["id"])
    after_window_delete = store.layout("workspace-a", ["session-a", "session-b"])
    assert after_window_delete["windows"][0]["terminal_ids"] == ["session-a", "session-b"]
    assert after_window_delete["terminal_ids"] == ["session-a", "session-b"]

    store.delete_folder("workspace-a", folder["id"])
    after_folder_delete = store.layout("workspace-a", ["session-a", "session-b"])
    assert [item["id"] for item in after_folder_delete["folders"]] == ["folder-default"]
    assert after_folder_delete["windows"][0]["terminal_ids"] == ["session-a", "session-b"]


def test_layout_rename_move_and_default_protection(tmp_path):
    store = JTTLayoutStore(tmp_path / "state")
    folder = store.create_folder("workspace-a", "Build")
    renamed_folder = store.rename_folder("workspace-a", folder["id"], "빌드")
    window = store.create_window("workspace-a", renamed_folder["id"], "터미널")
    renamed_window = store.rename_window("workspace-a", window["id"], "Tests")
    store.move_terminal("workspace-a", "session-a", renamed_window["id"], ["session-a"])
    store.move_terminal("workspace-a", "session-a", "window-default", ["session-a"])
    assert store.layout("workspace-a", ["session-a"])["windows"][0]["terminal_ids"] == ["session-a"]

    with pytest.raises(JTTLayoutStoreError):
        store.rename_folder("workspace-a", "folder-default", "Renamed")
    with pytest.raises(JTTLayoutStoreError):
        store.delete_folder("workspace-a", "folder-default")
    with pytest.raises(JTTLayoutStoreError):
        store.rename_window("workspace-a", "window-default", "Renamed")
    with pytest.raises(JTTLayoutStoreError):
        store.delete_window("workspace-a", "window-default")
    with pytest.raises(JTTLayoutStoreError):
        store.move_terminal("workspace-a", "session-b", renamed_window["id"], ["session-a"])


def test_invalid_layout_state_fails_closed_without_mutation(tmp_path):
    store = JTTLayoutStore(tmp_path / "state")
    folder = store.create_folder("workspace-a", "Folder")
    before = store.path.read_bytes()
    payload = json.loads(before)
    payload["workspaces"][0]["placements"] = {"session-a": "window-missing"}
    store.path.write_text(json.dumps(payload), encoding="utf-8")
    os.chmod(store.path, 0o600)
    invalid = store.path.read_bytes()

    with pytest.raises(JTTLayoutStoreError):
        store.layout("workspace-a", ["session-a"])
    assert store.path.read_bytes() == invalid
    assert folder["id"] in invalid.decode("utf-8")


def test_stale_noncanonical_placements_are_lazily_ignored(tmp_path):
    store = JTTLayoutStore(tmp_path / "state")
    folder = store.create_folder("workspace-a", "Folder")
    window = store.create_window("workspace-a", folder["id"], "Window")
    store.move_terminal("workspace-a", "removed-session", window["id"], ["removed-session"])
    before = store.path.read_bytes()

    result = store.layout("workspace-a", ["current-session"])
    assert result["windows"][1]["terminal_ids"] == []
    assert result["windows"][0]["terminal_ids"] == ["current-session"]
    assert store.path.read_bytes() == before


def test_duplicate_json_keys_fail_closed(tmp_path):
    store = JTTLayoutStore(tmp_path / "state")
    store.create_folder("workspace-a", "Folder")
    store.path.write_text(
        '{"schema_version":1,"schema_version":1,"workspaces":[]}', encoding="utf-8"
    )
    os.chmod(store.path, 0o600)

    with pytest.raises(JTTLayoutStoreError):
        store.layout("workspace-a", ["session-a"])


def test_oversized_layout_file_fails_closed(tmp_path):
    store = JTTLayoutStore(tmp_path / "state")
    store.create_folder("workspace-a", "Folder")
    store.path.write_bytes(b" " * (512 * 1024 + 1))
    os.chmod(store.path, 0o600)

    with pytest.raises(JTTLayoutStoreError, match="size"):
        store.layout("workspace-a", ["session-a"])


def test_directory_fsync_failure_reports_committed_layout_and_can_be_reloaded(
        tmp_path, monkeypatch):
    store = JTTLayoutStore(tmp_path / "state")
    original_fsync = os.fsync

    def fail_directory_fsync(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError("injected directory fsync failure")
        return original_fsync(fd)

    monkeypatch.setattr(os, "fsync", fail_directory_fsync)
    with pytest.raises(JTTLayoutStoreCommittedDurabilityError) as caught:
        store.create_folder("workspace-a", "Committed")

    assert caught.value.committed is True
    assert caught.value.workspace_id == "workspace-a"
    assert caught.value.item_id.startswith("folder-")

    monkeypatch.setattr(os, "fsync", original_fsync)
    reopened = JTTLayoutStore(store.root).layout("workspace-a", ["session-a"])
    assert any(item["id"] == caught.value.item_id and item["name"] == "Committed"
               for item in reopened["folders"])


def test_file_fsync_failure_remains_precommit_error(tmp_path, monkeypatch):
    store = JTTLayoutStore(tmp_path / "state")

    def fail_file_fsync(_fd):
        raise OSError("injected file fsync failure")

    monkeypatch.setattr(os, "fsync", fail_file_fsync)
    with pytest.raises(JTTLayoutStoreError) as caught:
        store.create_folder("workspace-a", "Not committed")

    assert not isinstance(caught.value, JTTLayoutStoreCommittedDurabilityError)
    assert not store.path.exists()
