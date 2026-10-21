import json
import fcntl
import os
import stat
import threading

import pytest

from tmux_agent_tower.state.jtt_workspaces import (
    JTTWorkspaceStore,
    WorkspaceStoreCommittedDurabilityError,
    WorkspaceStoreError,
)


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


def test_rename_saved_workspace_revalidates_snapshot_and_preserves_exact_terminal_refs(tmp_path):
    store = JTTWorkspaceStore(tmp_path / "private" / "jtt")
    saved = store.create_workspace("Original")
    store.set_terminal_ids(saved["id"], ["session-a", "session-b"])
    snapshot = next(item for item in store.workspaces() if item["id"] == saved["id"])
    before = store.path.read_bytes()

    renamed = store.rename_workspace(saved["id"], "Renamed", expected_snapshot=snapshot)

    assert renamed["id"] == saved["id"]
    assert renamed["name"] == "Renamed"
    assert renamed["terminal_ids"] == ["session-a", "session-b"]
    assert next(item for item in store.workspaces() if item["id"] == saved["id"])["terminal_ids"] == [
        "session-a", "session-b",
    ]
    with pytest.raises(WorkspaceStoreError, match="changed"):
        store.rename_workspace(saved["id"], "Stale", expected_snapshot=snapshot)
    with pytest.raises(WorkspaceStoreError, match="default"):
        store.rename_workspace(store.DEFAULT_ID, "No", expected_snapshot=store.workspaces()[0])
    assert store.path.read_bytes() != before


def test_workspace_directory_sync_failure_reports_committed_replacement(tmp_path, monkeypatch):
    store = JTTWorkspaceStore(tmp_path / "private")
    existing = store.create_workspace("Existing")
    before = store.path.read_bytes()
    real_fsync = os.fsync
    calls = 0

    def fail_directory_sync(fd):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("fixture directory sync failure")
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", fail_directory_sync)
    with pytest.raises(WorkspaceStoreCommittedDurabilityError) as caught:
        store.delete_workspace(existing["id"], expected_snapshot=existing)

    assert caught.value.committed is True
    assert caught.value.record_id is None
    assert "replacement completed" in str(caught.value)
    remaining = {item["id"]: item for item in JTTWorkspaceStore(store.root).workspaces()}
    assert existing["id"] not in remaining
    assert remaining[store.DEFAULT_ID]["terminal_ids"] == []
    assert store.path.read_bytes() != before


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


def test_delete_workspace_rejects_default_and_changed_snapshot(tmp_path):
    store = JTTWorkspaceStore(tmp_path / "private")
    default = store.workspaces()[0]
    saved = store.create_workspace("Saved")
    store.set_terminal_ids(saved["id"], ["session-a"])
    before = store.path.read_bytes()

    with pytest.raises(WorkspaceStoreError, match="default"):
        store.delete_workspace(default["id"], expected_snapshot=default)
    stale = {**saved, "terminal_ids": ["different"]}
    with pytest.raises(WorkspaceStoreError, match="changed"):
        store.delete_workspace(saved["id"], expected_snapshot=stale)

    assert store.path.read_bytes() == before


def test_delete_workspace_transfers_only_final_terminal_references(tmp_path):
    store = JTTWorkspaceStore(tmp_path / "private")
    first = store.create_workspace("First")
    shared = store.create_workspace("Shared")
    store.set_terminal_ids(first["id"], ["unique", "shared"])
    store.set_terminal_ids(shared["id"], ["shared"])
    first = next(item for item in store.workspaces() if item["id"] == first["id"])
    default_before = next(item for item in store.workspaces()
                         if item["id"] == store.DEFAULT_ID)

    removed = store.delete_workspace(first["id"], expected_snapshot=first)
    remaining = {item["id"]: item for item in store.workspaces()}

    assert removed["id"] == first["id"]
    assert first["id"] not in remaining
    assert remaining[store.DEFAULT_ID]["terminal_ids"] == ["unique"]
    assert remaining[shared["id"]]["terminal_ids"] == ["shared"]
    assert remaining[store.DEFAULT_ID]["terminal_ids"] != default_before["terminal_ids"]


def test_remove_terminal_reference_preserves_shared_refs_and_recovers_final_default_ref(tmp_path):
    store = JTTWorkspaceStore(tmp_path / "private")
    default = store.workspaces()[0]
    store.set_terminal_ids(default["id"], ["shared", "last-default"])
    saved = store.create_workspace("Saved")
    store.set_terminal_ids(saved["id"], ["shared"])

    current_default = next(item for item in store.workspaces()
                           if item["id"] == store.DEFAULT_ID)
    result = store.remove_terminal_reference(
        current_default["id"], "shared", expected_snapshot=current_default,
    )
    assert result["preserved_elsewhere"] is True
    assert {item["id"]: item for item in store.workspaces()}[saved["id"]]["terminal_ids"] == ["shared"]

    current_default = next(item for item in store.workspaces()
                           if item["id"] == store.DEFAULT_ID)
    result = store.remove_terminal_reference(
        current_default["id"], "last-default", expected_snapshot=current_default,
    )
    reopened = store.workspaces()
    recovery = next(item for item in reopened if item["id"] == result["recovery_workspace_id"])
    assert recovery["name"] == "복구 보관함"
    assert recovery["terminal_ids"] == ["last-default"]
    assert not next(item for item in reopened if item["id"] == store.DEFAULT_ID)["terminal_ids"]


def test_uncertain_cleanup_reuses_recovery_workspace_and_allows_reference_pruning(tmp_path):
    store = JTTWorkspaceStore(tmp_path / "private")
    store.set_terminal_ids(store.DEFAULT_ID, ["uncertain-1", "uncertain-2", "uncertain-3"])

    for terminal_id in ("uncertain-1", "uncertain-2", "uncertain-3"):
        default = next(item for item in store.workspaces() if item["id"] == store.DEFAULT_ID)
        store.remove_terminal_reference(store.DEFAULT_ID, terminal_id, expected_snapshot=default)

    workspaces = store.workspaces()
    recovery = next(item for item in workspaces
                    if item["id"] == store.RECOVERY_ID)
    assert recovery["terminal_ids"] == ["uncertain-1", "uncertain-2", "uncertain-3"]
    assert len([item for item in workspaces if item["id"] == store.RECOVERY_ID]) == 1

    result = store.remove_terminal_reference(store.RECOVERY_ID, "uncertain-1",
                                             expected_snapshot=recovery)
    recovery = next(item for item in store.workspaces() if item["id"] == store.RECOVERY_ID)
    assert recovery["terminal_ids"] == ["uncertain-2", "uncertain-3"]
    assert result["recovery_workspace_id"] is None


def test_forget_confirmed_exited_reference_only_removes_selected_workspace_ref(tmp_path):
    store = JTTWorkspaceStore(tmp_path / "private")
    store.set_terminal_ids(store.DEFAULT_ID, ["exited-shared", "exited-final"])
    for terminal_id in ("exited-shared", "exited-final"):
        default = next(item for item in store.workspaces() if item["id"] == store.DEFAULT_ID)
        store.remove_terminal_reference(store.DEFAULT_ID, terminal_id, expected_snapshot=default)
    store.set_terminal_ids(store.DEFAULT_ID, ["exited-shared"])
    recovery = next(item for item in store.workspaces() if item["id"] == store.RECOVERY_ID)

    store.forget_terminal_reference(store.RECOVERY_ID, "exited-shared",
                                    expected_snapshot=recovery)

    workspaces = {item["id"]: item for item in store.workspaces()}
    assert workspaces[store.DEFAULT_ID]["terminal_ids"] == ["exited-shared"]
    assert workspaces[store.RECOVERY_ID]["terminal_ids"] == ["exited-final"]

    recovery = workspaces[store.RECOVERY_ID]
    store.forget_terminal_reference(store.RECOVERY_ID, "exited-final",
                                    expected_snapshot=recovery)
    workspaces = {item["id"]: item for item in store.workspaces()}
    assert workspaces[store.RECOVERY_ID]["terminal_ids"] == []
    assert workspaces[store.DEFAULT_ID]["terminal_ids"] == ["exited-shared"]


def test_remove_terminal_reference_is_atomic_on_replace_failure(tmp_path, monkeypatch):
    store = JTTWorkspaceStore(tmp_path / "private")
    saved = store.create_workspace("Saved")
    store.set_terminal_ids(saved["id"], ["session-a"])
    saved = next(item for item in store.workspaces() if item["id"] == saved["id"])
    before = store.path.read_bytes()

    def fail_replace(_source, _destination):
        raise OSError("fixture replace failure")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(OSError, match="fixture replace failure"):
        store.remove_terminal_reference(
            saved["id"], "session-a", expected_snapshot=saved,
        )

    assert store.path.read_bytes() == before
    assert not list(store.root.glob(".workspaces.*.tmp"))


def test_concurrent_workspace_create_serializes_after_workspace_delete(tmp_path, monkeypatch):
    root = tmp_path / "private"
    deleting = JTTWorkspaceStore(root)
    concurrent = JTTWorkspaceStore(root)
    target = deleting.create_workspace("Delete me")
    target = next(item for item in deleting.workspaces() if item["id"] == target["id"])
    original_write = deleting._write
    entered_write = threading.Event()
    release_write = threading.Event()
    create_read = threading.Event()
    create_lock_attempt = threading.Event()
    errors = []

    def pause_delete_write(values):
        entered_write.set()
        if not release_write.wait(5):
            raise AssertionError("test did not release the delete writer")
        original_write(values)

    deleting._write = pause_delete_write
    original_read = concurrent.workspaces

    def observe_create_read():
        create_read.set()
        return original_read()

    concurrent.workspaces = observe_create_read
    real_flock = fcntl.flock

    def observe_lock(fd, operation):
        if (threading.current_thread().name == "workspace-concurrent-create"
                and operation == fcntl.LOCK_EX):
            create_lock_attempt.set()
        return real_flock(fd, operation)

    monkeypatch.setattr(fcntl, "flock", observe_lock)

    def delete_target():
        try:
            deleting.delete_workspace(target["id"], expected_snapshot=target)
        except Exception as exc:
            errors.append(exc)

    def create_unrelated():
        try:
            concurrent.create_workspace("Concurrent create")
        except Exception as exc:
            errors.append(exc)

    delete_thread = threading.Thread(target=delete_target, name="workspace-delete")
    create_thread = threading.Thread(target=create_unrelated, name="workspace-concurrent-create")
    delete_thread.start()
    assert entered_write.wait(5)
    create_thread.start()
    lock_waited = create_lock_attempt.wait(2)
    read_while_delete_held = create_read.is_set()
    release_write.set()
    delete_thread.join(5)
    create_thread.join(5)

    assert not delete_thread.is_alive()
    assert not create_thread.is_alive()
    assert lock_waited
    assert not read_while_delete_held
    assert errors == []
    names = {item["name"] for item in deleting.workspaces()}
    assert "Delete me" not in names
    assert "Concurrent create" in names
