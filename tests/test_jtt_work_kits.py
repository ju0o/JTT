import curses
import fcntl
import json
import os
import stat
import threading
from types import SimpleNamespace

import pytest

from tmux_agent_tower.state.jtt_work_kits import (
    JTTWorkKitStore,
    WorkKitStoreCommittedDurabilityError,
    WorkKitStoreError,
    layout_for_terminal_count,
)
from tmux_agent_tower.state.jtt_keys import DEFAULT_JTT_KEYBINDINGS, JTTKeybindings
from tmux_agent_tower.state.jtt_workspaces import JTTWorkspaceStore
from tmux_agent_tower.terminal import TerminalIdentity
from tmux_agent_tower.ui import jtt
from tmux_agent_tower.ui import ux_preview_v2 as renderer


class FakeClient:
    def __init__(self, identity, registry):
        self.identity = identity
        self.registry = registry
        self.lease = False

    def release(self):
        self.lease = False

    def close(self):
        self.registry.closed += 1

    def send(self, data):
        self.registry.sent.append(data)
        return len(data)


class FakeRegistry:
    def __init__(self, identities=()):
        self.identities = tuple(identities)
        self.created = 0
        self.stopped = []
        self.connected = []
        self.sent = []
        self.closed = 0
        self.clients = {}

    def list_sessions(self):
        return self.identities

    def connect(self, session_id):
        self.connected.append(session_id)
        identity = next(item for item in self.identities if item.session_id == session_id)
        client = self.clients.setdefault(session_id, FakeClient(identity, self))
        return client

    def create(self, *_args, **_kwargs):
        self.created += 1
        raise AssertionError("Work Kit preview must not create a PTY")

    def stop(self, session_id, **_kwargs):
        self.stopped.append(session_id)


class FakeScreen:
    def __init__(self, height=44, width=160):
        self.height = height
        self.width = width
        self.writes = []

    def getmaxyx(self):
        return self.height, self.width

    def addnstr(self, y, x, text, _length, _attr=0):
        self.writes.append((y, x, str(text)))

    def text(self):
        return "\n".join(value for _y, _x, value in self.writes)


def _specs(count):
    return [{"name": f"터미널 {index + 1}", "role": f"역할 {index + 1}",
             "working_directory": "~/not-probed" if index == 0 else None}
            for index in range(count)]


def _kit_choice(state, kit_id):
    return next(index for index, item in enumerate(state["choices"])
                if item.get("kind") == "work_kit" and item.get("id") == kit_id)


def test_work_kit_store_is_read_only_until_explicit_create_and_round_trips(tmp_path):
    store = JTTWorkKitStore(tmp_path / "state")

    assert store.kits() == []
    assert not store.root.exists()

    kit = store.create_kit("  API 작업  ", "로컬 개발 템플릿", _specs(2))
    reopened = JTTWorkKitStore(store.root).kits()

    assert reopened == [kit]
    assert kit["name"] == "API 작업"
    assert kit["layout"] == "split_horizontal"
    assert kit["terminals"][0]["working_directory"] == "~/not-probed"
    assert stat.S_IMODE(store.root.stat().st_mode) == 0o700
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    assert store.path.stat().st_nlink == 1


@pytest.mark.parametrize("count,layout", [
    (1, "single"), (2, "split_horizontal"),
    (3, "two_top_one_wide"), (4, "grid_2x2"),
])
def test_work_kit_layout_is_bound_to_terminal_count(tmp_path, count, layout):
    store = JTTWorkKitStore(tmp_path / "state")
    kit = store.create_kit("Kit", "", _specs(count))

    assert kit["layout"] == layout
    assert layout_for_terminal_count(count) == layout
    with pytest.raises(WorkKitStoreError):
        layout_for_terminal_count(True)


@pytest.mark.parametrize("mutate", [
    lambda data: data["kits"][0].update(command="echo unsafe"),
    lambda data: data["kits"][0].update(layout="grid_2x2"),
    lambda data: data["kits"][0]["terminals"][0].update(environment={"X": "Y"}),
    lambda data: data["kits"][0]["terminals"][0].update(name="\nbad"),
])
def test_invalid_or_unsafe_work_kit_json_is_rejected(tmp_path, mutate):
    store = JTTWorkKitStore(tmp_path / "state")
    store.create_kit("Kit", "", _specs(1))
    payload = json.loads(store.path.read_text(encoding="utf-8"))
    mutate(payload)
    store.path.write_text(json.dumps(payload), encoding="utf-8")
    os.chmod(store.path, 0o600)

    with pytest.raises(WorkKitStoreError):
        store.kits()


def test_work_kit_file_rejects_non_private_permissions_and_links(tmp_path):
    store = JTTWorkKitStore(tmp_path / "state")
    store.create_kit("Kit", "", _specs(1))
    os.chmod(store.path, 0o644)
    with pytest.raises(WorkKitStoreError):
        store.kits()
    os.chmod(store.path, 0o600)
    linked = tmp_path / "linked.json"
    try:
        os.link(store.path, linked)
    except OSError as exc:
        pytest.skip(f"hard links unavailable: {exc}")
    with pytest.raises(WorkKitStoreError):
        store.kits()


def test_work_kit_atomic_replace_failure_is_reported_without_overwrite(tmp_path, monkeypatch):
    store = JTTWorkKitStore(tmp_path / "state")
    store.create_kit("Existing", "Keep me", _specs(1))
    original_bytes = store.path.read_bytes()

    def fail_replace(_source, _destination):
        raise OSError("fixture replace failure")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(WorkKitStoreError):
        store.create_kit("New", "Must not replace", _specs(2))

    assert store.path.read_bytes() == original_bytes
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    assert not list(store.root.glob(".work_kits.*.tmp"))


def test_delete_work_kit_revalidates_full_snapshot_and_removes_only_selected(tmp_path):
    store = JTTWorkKitStore(tmp_path / "state")
    first = store.create_kit("First", "Keep first", _specs(1))
    second = store.create_kit("Second", "Keep second", _specs(2))
    before = store.path.read_bytes()

    with pytest.raises(WorkKitStoreError, match="changed"):
        store.delete_kit(first["id"], expected_snapshot={**first, "description": "changed"})
    assert store.path.read_bytes() == before

    removed = store.delete_kit(first["id"], expected_snapshot=first)
    assert removed == first
    assert JTTWorkKitStore(store.root).kits() == [second]


def test_delete_work_kit_atomic_replace_failure_preserves_original(tmp_path, monkeypatch):
    store = JTTWorkKitStore(tmp_path / "state")
    first = store.create_kit("First", "Keep me", _specs(1))
    before = store.path.read_bytes()

    def fail_replace(_source, _destination):
        raise OSError("fixture replace failure")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(WorkKitStoreError):
        store.delete_kit(first["id"], expected_snapshot=first)

    assert store.path.read_bytes() == before


def test_concurrent_work_kit_create_serializes_after_delete(tmp_path, monkeypatch):
    root = tmp_path / "state"
    deleting = JTTWorkKitStore(root)
    concurrent = JTTWorkKitStore(root)
    target = deleting.create_kit("Delete me", "Target", _specs(1))
    original_write = deleting._write
    entered_write = threading.Event()
    release_write = threading.Event()
    create_read = threading.Event()
    create_lock_attempt = threading.Event()
    errors = []

    def pause_delete_write(kits):
        entered_write.set()
        if not release_write.wait(5):
            raise AssertionError("test did not release the delete writer")
        original_write(kits)

    deleting._write = pause_delete_write
    original_read = concurrent.kits

    def observe_create_read():
        create_read.set()
        return original_read()

    concurrent.kits = observe_create_read
    real_flock = fcntl.flock

    def observe_lock(fd, operation):
        if (threading.current_thread().name == "kit-concurrent-create"
                and operation == fcntl.LOCK_EX):
            create_lock_attempt.set()
        return real_flock(fd, operation)

    monkeypatch.setattr(fcntl, "flock", observe_lock)

    def delete_target():
        try:
            deleting.delete_kit(target["id"], expected_snapshot=target)
        except Exception as exc:
            errors.append(exc)

    def create_unrelated():
        try:
            concurrent.create_kit("Concurrent create", "Keep me", _specs(2))
        except Exception as exc:
            errors.append(exc)

    delete_thread = threading.Thread(target=delete_target, name="kit-delete")
    create_thread = threading.Thread(target=create_unrelated, name="kit-concurrent-create")
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
    names = {kit["name"] for kit in deleting.kits()}
    assert "Delete me" not in names
    assert "Concurrent create" in names


def test_workspace_and_work_kit_mutations_share_one_root_lock(tmp_path, monkeypatch):
    root = tmp_path / "shared-state"
    workspace_store = JTTWorkspaceStore(root)
    kit_store = JTTWorkKitStore(root)
    target = workspace_store.create_workspace("Delete me")
    target = next(item for item in workspace_store.workspaces() if item["id"] == target["id"])
    workspace_write = workspace_store._write
    entered_write = threading.Event()
    release_write = threading.Event()
    kit_read = threading.Event()
    kit_lock_attempt = threading.Event()
    errors = []

    def pause_workspace_write(workspaces):
        entered_write.set()
        if not release_write.wait(5):
            raise AssertionError("test did not release the workspace writer")
        workspace_write(workspaces)

    workspace_store._write = pause_workspace_write
    original_kits = kit_store.kits

    def observe_kits_read():
        kit_read.set()
        return original_kits()

    kit_store.kits = observe_kits_read
    real_flock = fcntl.flock

    def observe_lock(fd, operation):
        if (threading.current_thread().name == "cross-store-kit-create"
                and operation == fcntl.LOCK_EX):
            kit_lock_attempt.set()
        return real_flock(fd, operation)

    monkeypatch.setattr(fcntl, "flock", observe_lock)

    def delete_workspace():
        try:
            workspace_store.delete_workspace(target["id"], expected_snapshot=target)
        except Exception as exc:
            errors.append(exc)

    def create_kit():
        try:
            kit_store.create_kit("Concurrent Kit", "Survives workspace delete", _specs(1))
        except Exception as exc:
            errors.append(exc)

    delete_thread = threading.Thread(target=delete_workspace, name="workspace-delete")
    kit_thread = threading.Thread(target=create_kit, name="cross-store-kit-create")
    delete_thread.start()
    assert entered_write.wait(5)
    kit_thread.start()
    lock_waited = kit_lock_attempt.wait(2)
    read_while_workspace_held = kit_read.is_set()
    release_write.set()
    delete_thread.join(5)
    kit_thread.join(5)

    assert not delete_thread.is_alive()
    assert not kit_thread.is_alive()
    assert lock_waited
    assert not read_while_workspace_held
    assert errors == []
    assert all(item["id"] != target["id"] for item in workspace_store.workspaces())
    assert [kit["name"] for kit in kit_store.kits()] == ["Concurrent Kit"]


def test_directory_sync_failure_reports_post_replace_state(tmp_path, monkeypatch):
    store = JTTWorkKitStore(tmp_path / "state")
    store.create_kit("Existing", "Keep me", _specs(1))
    real_fsync = os.fsync
    calls = 0

    def fail_directory_sync(fd):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("fixture directory sync failure")
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", fail_directory_sync)
    with pytest.raises(WorkKitStoreCommittedDurabilityError,
                       match="replacement completed") as caught:
        store.create_kit("New", "May already be present", _specs(2))

    assert caught.value.committed is True
    assert caught.value.record_id is not None
    assert [kit["name"] for kit in JTTWorkKitStore(store.root).kits()] == ["Existing", "New"]


def _fail_directory_fsync(monkeypatch):
    real_fsync = os.fsync
    calls = 0

    def fail_directory_sync(fd):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("fixture directory sync failure")
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", fail_directory_sync)


def test_duplicate_json_keys_are_rejected(tmp_path):
    store = JTTWorkKitStore(tmp_path / "state")
    store._ensure_private_root()
    store.path.write_text('{"schema_version":1,"schema_version":1,"kits":[]}',
                          encoding="utf-8")
    os.chmod(store.path, 0o600)

    with pytest.raises(WorkKitStoreError):
        store.kits()


@pytest.mark.parametrize("failure", ["malformed", "deeply_nested", "insecure_permissions"])
def test_invalid_optional_work_kit_data_does_not_block_workspaces_or_overwrite(
        tmp_path, monkeypatch, failure):
    workspace_store = JTTWorkspaceStore(tmp_path / "jtt")
    saved = workspace_store.create_workspace("Saved")
    workspace_store.set_terminal_ids(saved["id"], ["session-valid"])
    workspace_bytes = workspace_store.path.read_bytes()
    kit_store = JTTWorkKitStore(workspace_store.root)
    if failure == "malformed":
        kit_store._ensure_private_root()
        kit_store.path.write_text('{"schema_version":1,"kits":[', encoding="utf-8")
        os.chmod(kit_store.path, 0o600)
    elif failure == "deeply_nested":
        kit_store._ensure_private_root()
        kit_store.path.write_bytes(("[" * 1500 + "0" + "]" * 1500).encode("ascii"))
        os.chmod(kit_store.path, 0o600)
    else:
        kit_store.create_kit("Existing", "Preserve", _specs(1))
        os.chmod(kit_store.path, 0o644)
    original_kit_bytes = kit_store.path.read_bytes()
    original_mode = stat.S_IMODE(kit_store.path.stat().st_mode)
    registry = FakeRegistry([
        TerminalIdentity("session-valid", "gen", 1000, "s1", 202, "p202", "/bin/bash",
                         "saved", "running", renderer_state="ready"),
    ])

    state = jtt.build_state(workspace_store, registry, attach_default=False)

    assert state["jtt_work_kit_error"] is True
    assert any(item.get("id") == saved["id"] for item in state["choices"])
    assert not any(item.get("id") == "action:work-kit-create" for item in state["choices"])
    unavailable = next(item for item in state["choices"]
                       if item.get("id") == "work-kit-unavailable")
    assert unavailable["kind"] == "info"
    assert "사용 불가" in unavailable["label"]

    screen = FakeScreen(height=24, width=80)
    rect = renderer._regions(80, 24, 0)[0][0]
    renderer._draw_left(screen, rect, state, True)
    assert "Work Kit 사용 불가" in screen.text()

    monkeypatch.setattr(jtt, "prompt_text", lambda *_args, **_kwargs: pytest.fail("must not prompt"))
    jtt._create_work_kit(None, state)
    with pytest.raises(WorkKitStoreError):
        kit_store.create_kit("Should not overwrite", "", _specs(1))
    assert kit_store.path.read_bytes() == original_kit_bytes
    assert stat.S_IMODE(kit_store.path.stat().st_mode) == original_mode
    assert workspace_store.path.read_bytes() == workspace_bytes

    saved_index = next(index for index, item in enumerate(state["choices"])
                       if item.get("id") == saved["id"])
    renderer._set_choice(state, saved_index)
    assert [row["managed_id"] for row in state["panes"]] == ["session-valid"]
    assert registry.connected == ["session-valid"]


def test_ui_create_flow_persists_only_work_kit_file(tmp_path, monkeypatch):
    workspace_store = JTTWorkspaceStore(tmp_path / "jtt")
    workspace_store.set_terminal_ids(workspace_store.DEFAULT_ID, [])
    workspaces_before = workspace_store.path.read_bytes()
    state = jtt.build_state(workspace_store, FakeRegistry())
    responses = iter(["Dev Kit", "개발용", "2", "API", "백엔드", "  ~/not-probed  ",
                      "Tests", "검증", ""])
    monkeypatch.setattr(jtt, "prompt_text", lambda *_args, **_kwargs: next(responses))
    monkeypatch.setattr(jtt, "_prompt_working_directory", lambda *_args, **_kwargs: next(responses))

    jtt._create_work_kit(None, state)

    kits = state["jtt_work_kit_store"].kits()
    assert len(kits) == 1
    assert kits[0]["name"] == "Dev Kit"
    assert kits[0]["terminals"][0]["working_directory"] == "  ~/not-probed  "
    assert workspace_store.path.read_bytes() == workspaces_before
    assert state["work_kit_preview"]["id"] == kits[0]["id"]
    assert state["jtt_notice"] == "Dev Kit Work Kit 저장 완료 · 터미널 2개 구성 · 아직 실행되지 않음"


def test_work_kit_create_reports_committed_when_directory_sync_fails(tmp_path, monkeypatch):
    workspace_store = JTTWorkspaceStore(tmp_path / "jtt")
    workspace_store.set_terminal_ids(workspace_store.DEFAULT_ID, [])
    workspace_before = workspace_store.path.read_bytes()
    state = jtt.build_state(workspace_store, FakeRegistry())
    responses = iter(["Committed Kit", "Description", "1", "Shell", "Terminal role"])
    monkeypatch.setattr(jtt, "prompt_text", lambda *_args, **_kwargs: next(responses))
    monkeypatch.setattr(jtt, "_prompt_working_directory", lambda *_args, **_kwargs: "")
    _fail_directory_fsync(monkeypatch)

    jtt._create_work_kit(None, state)

    kits = state["jtt_work_kit_store"].kits()
    assert [kit["name"] for kit in kits] == ["Committed Kit"]
    assert state["work_kit_preview"]["id"] == kits[0]["id"]
    assert workspace_store.path.read_bytes() == workspace_before
    assert "Work Kit 저장 완료" in state["jtt_notice"]
    assert "디렉터리 동기화 확인 필요" in state["jtt_notice"]
    assert "아직 실행되지 않음" in state["jtt_notice"]


def test_work_kit_delete_reports_committed_and_refreshes_after_directory_sync_failure(
        tmp_path, monkeypatch):
    workspace_store = JTTWorkspaceStore(tmp_path / "jtt")
    kit_store = JTTWorkKitStore(workspace_store.root)
    selected = kit_store.create_kit("Delete", "Selected", _specs(1))
    keep = kit_store.create_kit("Keep", "Other", _specs(1))
    state = jtt.build_state(workspace_store, FakeRegistry())
    renderer._set_choice(state, _kit_choice(state, selected["id"]))
    monkeypatch.setattr(jtt, "run_list_picker", lambda *_args, **_kwargs:
                        SimpleNamespace(cancelled=False, selected_key="delete"))
    _fail_directory_fsync(monkeypatch)

    jtt._confirm_and_delete_work_kit(None, state)

    assert kit_store.kits() == [keep]
    assert state["work_kit_preview"] is None
    assert not any(item.get("id") == selected["id"] for item in state["choices"])
    assert "Work Kit 삭제 반영됨" in state["jtt_notice"]
    assert "디렉터리 동기화 확인 필요" in state["jtt_notice"]


def test_workspace_save_reports_committed_and_refreshes_after_directory_sync_failure(
        tmp_path, monkeypatch):
    store = JTTWorkspaceStore(tmp_path / "jtt")
    state = jtt.build_state(store, FakeRegistry())
    monkeypatch.setattr(jtt, "prompt_text", lambda *_args, **_kwargs: "Committed workspace")
    _fail_directory_fsync(monkeypatch)

    jtt._save_configuration(None, state)

    saved = next(item for item in store.workspaces() if item["name"] == "Committed workspace")
    assert any(item.get("id") == saved["id"] for item in state["choices"])
    assert state["workspace_id"] == saved["id"]
    assert "구성 저장 반영됨" in state["jtt_notice"]
    assert "디렉터리 동기화 확인 필요" in state["jtt_notice"]


def test_workspace_delete_reports_committed_and_refreshes_after_directory_sync_failure(
        tmp_path, monkeypatch):
    store = JTTWorkspaceStore(tmp_path / "jtt")
    saved = store.create_workspace("Delete workspace")
    store.set_terminal_ids(saved["id"], ["session-to-preserve"])
    state = jtt.build_state(store, FakeRegistry())
    renderer._set_choice(state, next(index for index, item in enumerate(state["choices"])
                                     if item.get("id") == saved["id"]))
    monkeypatch.setattr(jtt, "run_list_picker", lambda *_args, **_kwargs:
                        SimpleNamespace(cancelled=False, selected_key="delete"))
    _fail_directory_fsync(monkeypatch)

    jtt._confirm_and_delete_workspace(None, state)

    remaining = {item["id"]: item for item in store.workspaces()}
    assert saved["id"] not in remaining
    assert remaining[store.DEFAULT_ID]["terminal_ids"] == ["session-to-preserve"]
    assert state["workspace_id"] == store.DEFAULT_ID
    assert "작업공간 삭제 반영됨" in state["jtt_notice"]
    assert "디렉터리 동기화 확인 필요" in state["jtt_notice"]


def test_new_terminal_keeps_created_shell_when_reference_commit_sync_is_uncertain(
        tmp_path, monkeypatch):
    store = JTTWorkspaceStore(tmp_path / "jtt")
    store.set_terminal_ids(store.DEFAULT_ID, [])

    class CreateRegistry(FakeRegistry):
        def create(self):
            self.created += 1
            identity = TerminalIdentity(
                "created-session", "generation-new", 1000, "s1", 505, "p505",
                "/bin/bash", "New shell", "running", renderer_state="ready",
            )
            self.identities = (identity,)
            return FakeClient(identity, self)

    registry = CreateRegistry()
    state = jtt.build_state(store, registry)
    _fail_directory_fsync(monkeypatch)

    jtt._create_terminal(None, state)

    assert registry.created == 1
    assert registry.stopped == []
    assert registry.closed == 0
    assert "created-session" in state["managed_terminals"]
    assert store.workspaces()[0]["terminal_ids"] == ["created-session"]
    assert state["panes"][state["pane_index"]]["managed_id"] == "created-session"
    assert state["selected_target_id"] == "created-session"
    assert state["jtt_pending_auto_acquire"]["session_id"] == "created-session"
    assert "카드 반영됨" in state["jtt_notice"]
    assert "디렉터리 동기화 확인 필요" in state["jtt_notice"]


def test_explicit_stop_reports_committed_card_removal_after_directory_sync_failure(
        tmp_path, monkeypatch):
    store, registry, state, identity = _uncertain_terminal_state(tmp_path)
    monkeypatch.setattr(jtt, "run_list_picker", lambda *_args, **_kwargs:
                        SimpleNamespace(cancelled=False, selected_key="stop"))
    _fail_directory_fsync(monkeypatch)

    jtt._confirm_and_stop_selected_terminal(None, state)

    assert registry.stopped == [identity.session_id]
    assert registry.closed == 1
    assert store.workspaces()[0]["terminal_ids"] == []
    assert identity.session_id not in state["managed_terminals"]
    assert "종료 및 카드 제거 반영됨" in state["jtt_notice"]
    assert "디렉터리 동기화 확인 필요" in state["jtt_notice"]


def test_work_kit_creation_cancel_and_store_failure_report_without_pty(monkeypatch, tmp_path):
    workspace_store = JTTWorkspaceStore(tmp_path / "jtt")
    registry = FakeRegistry()
    state = jtt.build_state(workspace_store, registry)
    monkeypatch.setattr(jtt, "prompt_text", lambda *_args, **_kwargs: None)

    jtt._create_work_kit(None, state)

    assert "생성 취소" in state["jtt_notice"]
    assert "터미널은 실행되지 않았습니다" in state["jtt_notice"]
    assert not state["jtt_work_kit_store"].path.exists()
    assert registry.created == 0

    responses = iter(["Kit", "Description", "1", "Shell", "Terminal role"])
    monkeypatch.setattr(jtt, "prompt_text", lambda *_args, **_kwargs: next(responses))
    monkeypatch.setattr(jtt, "_prompt_working_directory", lambda *_args, **_kwargs: "")

    def fail_create(*_args, **_kwargs):
        raise WorkKitStoreError("fixture store failure")

    monkeypatch.setattr(state["jtt_work_kit_store"], "create_kit", fail_create)
    jtt._create_work_kit(None, state)
    assert "저장 상태를 확인할 수 없습니다" in state["jtt_notice"]
    assert registry.created == 0


def test_work_kit_selection_and_prefix_new_never_touch_pty_or_workspace_data(tmp_path):
    workspace_store = JTTWorkspaceStore(tmp_path / "jtt")
    workspace = workspace_store.workspaces()[0]
    workspace_store.set_terminal_ids(workspace["id"], [])
    workspace_before = workspace_store.path.read_bytes()
    kit_store = JTTWorkKitStore(workspace_store.root)
    kit = kit_store.create_kit("Preview", "Not running", _specs(1))
    registry = FakeRegistry()
    state = jtt.build_state(workspace_store, registry)
    before_calls = (registry.created, tuple(registry.connected), tuple(registry.stopped),
                    tuple(registry.sent))

    state["active_panel"] = 0
    assert renderer._handle_key(FakeScreen(), state, curses.KEY_DOWN) is False
    assert state["choices"][state["nav_index"]]["id"] == kit["id"]
    assert state["panes"][0]["terminal_kind"] == "work_kit_template"
    assert state["panes"][0]["terminal_view_state"] == "not_running"
    assert "target_id" not in state["panes"][0]
    assert "managed_id" not in state["panes"][0]
    screen = FakeScreen()
    state["jtt_notice"] = ""
    renderer._draw_jtt_footer(screen, state, 80, 24)
    assert "Work Kit 미리보기" in screen.text()
    assert "Enter 입력" not in screen.text()
    assert renderer._handle_key(screen, state, "\x07") is False
    screen.writes.clear()
    renderer._draw_jtt_footer(screen, state, 80, 24)
    assert "n 차단" in screen.text()
    assert renderer._handle_key(screen, state, "n") is False

    after_calls = (registry.created, tuple(registry.connected), tuple(registry.stopped),
                   tuple(registry.sent))
    assert after_calls == before_calls
    assert workspace_store.path.read_bytes() == workspace_before


@pytest.mark.parametrize("keybindings", [
    DEFAULT_JTT_KEYBINDINGS,
    JTTKeybindings(prefix="Ctrl+X", input_escape="Ctrl+Y",
                   prefix_byte=24, input_escape_byte=25),
], ids=["built-in", "custom"])
def test_saved_workspace_reconnects_exact_sessions_and_keybindings_after_work_kit_preview(
        tmp_path, keybindings):
    workspace_store = JTTWorkspaceStore(tmp_path / "jtt")
    default = workspace_store.workspaces()[0]
    workspace_store.set_terminal_ids(default["id"], ["session-a"])
    saved = workspace_store.create_workspace("Saved")
    workspace_store.set_terminal_ids(saved["id"], ["session-b", "session-c"])
    identities = [
        TerminalIdentity("session-a", "gen-a", 1000, "s1", 101, "p101", "/bin/bash",
                         "A", "running", renderer_state="ready"),
        TerminalIdentity("session-b", "gen-b", 1000, "s1", 202, "p202", "/bin/bash",
                         "B", "running", renderer_state="ready"),
        TerminalIdentity("session-c", "gen-c", 1000, "s1", 303, "p303", "/bin/bash",
                         "C", "running", renderer_state="ready"),
    ]
    registry = FakeRegistry(identities)
    kit_store = JTTWorkKitStore(workspace_store.root)
    kit = kit_store.create_kit("Preview", "", _specs(1))
    state = jtt.build_state(workspace_store, registry, keybindings=keybindings)
    kit_index = _kit_choice(state, kit["id"])

    renderer._set_choice(state, kit_index)
    assert registry.connected == ["session-a"]
    saved_index = next(index for index, item in enumerate(state["choices"])
                       if item.get("kind") == "workspace" and item.get("id") == saved["id"])
    renderer._set_choice(state, saved_index)

    assert state["work_kit_preview"] is None
    identities_before_switch = [
        (row["managed_id"], row["managed_identity"]["shell_pid"]) for row in state["panes"]
    ]
    assert identities_before_switch == [("session-b", 202), ("session-c", 303)]
    assert registry.connected == ["session-a", "session-b", "session-c"]

    for session_id in ("session-b", "session-c"):
        binding = state["managed_terminals"][session_id]
        assert binding.input_escape_byte == keybindings.input_escape_byte
        assert binding.input_escape_name == keybindings.input_escape

    renderer._move_pane(state, 1, 44, 160)
    assert renderer._selected_row(state)["managed_id"] == "session-c"
    assert renderer._selected_row(state)["managed_identity"]["shell_pid"] == 303
    renderer._move_pane(state, -1, 44, 160)
    assert renderer._selected_row(state)["managed_id"] == "session-b"
    assert [(row["managed_id"], row["managed_identity"]["shell_pid"])
            for row in state["panes"]] == identities_before_switch

    screen = FakeScreen()
    assert renderer._handle_key(screen, state, chr(keybindings.prefix_byte)) is False
    assert state["jtt_prefix_pending"] is True
    assert renderer._handle_key(screen, state, "\x1b") is False
    assert state["jtt_prefix_pending"] is False

    escape_binding = state["managed_terminals"]["session-b"]
    escape_binding.input_mode = True
    escape_binding.client.lease = True
    escape_binding.synced = True
    escape_binding.screen = object()
    assert escape_binding.handle_key(chr(keybindings.input_escape_byte)) is True
    assert escape_binding.input_mode is False
    assert registry.sent == []


@pytest.mark.parametrize("count", [1, 2, 3, 4])
def test_work_kit_preview_cards_and_right_are_unmistakably_not_live(tmp_path, count):
    workspace_store = JTTWorkspaceStore(tmp_path / "jtt")
    kit_store = JTTWorkKitStore(workspace_store.root)
    kit = kit_store.create_kit(f"Kit {count}", "Preview only", _specs(count))
    state = jtt.build_state(workspace_store, FakeRegistry())
    renderer._set_choice(state, _kit_choice(state, kit["id"]))
    screen = FakeScreen()

    rects = renderer.card_rectangles(count, (5, 5, 80, 26))
    assert len(rects) == count
    renderer._draw_center(screen, (0, 0, 60, 35), state, True)
    renderer._draw_right(screen, (61, 0, 48, 35), state, True)

    text = screen.text()
    assert text.count("템플릿") >= count
    assert "Preview only" in text
    assert "실행 중인 터미널이 아닙니다." in text
    assert "템플릿 · 셸/명령 미실행" in text
    assert "관찰 중" not in text
    assert "새 터미널" not in text
    assert not any(hit[4] == "center_action" for hit in state["hit_boxes"])


def test_narrow_left_layout_keeps_work_kits_and_actions_inside_panel(tmp_path):
    workspace_store = JTTWorkspaceStore(tmp_path / "jtt")
    kit_store = JTTWorkKitStore(workspace_store.root)
    for index in range(5):
        kit_store.create_kit(f"Kit {index}", "", _specs(1))
    state = jtt.build_state(workspace_store, FakeRegistry())
    state["active_panel"] = 0
    screen = FakeScreen(height=24, width=80)
    rect = renderer._regions(80, 24, 0)[0][0]

    renderer._draw_left(screen, rect, state, True)

    rows = [y for y, _x, _text in screen.writes]
    assert rows and min(rows) >= 0 and max(rows) < 24
    written = screen.text()
    assert "Work Kit" in written
    assert "저장한 작업" in written
    assert all(y + height <= 23 for _x, y, _width, height, kind, _index in state["hit_boxes"]
               if kind == "left")
    screen.writes.clear()
    state["nav_index"] = next(index for index, item in enumerate(state["choices"])
                               if item.get("id") == "action:work-kit-create")
    state["hit_boxes"] = []
    renderer._draw_left(screen, rect, state, True)
    assert "새 Work Kit" in screen.text()


def _uncertain_terminal_state(tmp_path):
    store = JTTWorkspaceStore(tmp_path / "jtt")
    default = store.workspaces()[0]
    store.set_terminal_ids(default["id"], ["session-live"])
    identity = TerminalIdentity(
        "session-live", "generation-a", 1000, "s1", 202, "p202", "/bin/bash",
        "Live shell", "running", renderer_state="dependency_missing",
    )
    registry = FakeRegistry([identity])
    state = jtt.build_state(store, registry)
    return store, registry, state, identity


def test_uncertain_card_cleanup_cancel_changes_nothing(tmp_path, monkeypatch):
    store, registry, state, _identity = _uncertain_terminal_state(tmp_path)
    before = store.path.read_bytes()
    results = iter([
        SimpleNamespace(cancelled=False, selected_key="cleanup_card"),
        SimpleNamespace(cancelled=True, selected_key=None),
    ])
    monkeypatch.setattr(jtt, "run_list_picker", lambda *_args, **_kwargs: next(results))

    jtt._settings_menu(None, state)

    assert store.path.read_bytes() == before
    assert registry.stopped == []
    assert registry.closed == 0
    assert [item.session_id for item in registry.list_sessions()] == ["session-live"]


def test_uncertain_card_cleanup_blocks_changed_selection_without_write(tmp_path, monkeypatch):
    store, registry, state, _identity = _uncertain_terminal_state(tmp_path)
    before = store.path.read_bytes()
    calls = 0

    def change_selection(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return SimpleNamespace(cancelled=False, selected_key="cleanup_card")
        state["selected_target_id"] = "different-session"
        return SimpleNamespace(cancelled=False, selected_key="remove")

    monkeypatch.setattr(jtt, "run_list_picker", change_selection)
    jtt._settings_menu(None, state)

    assert store.path.read_bytes() == before
    assert registry.stopped == []
    assert registry.closed == 0
    assert "차단" in state["jtt_notice"]


def test_uncertain_card_cleanup_preserves_running_session_and_recovers_final_ref(
        tmp_path, monkeypatch):
    store, registry, state, original = _uncertain_terminal_state(tmp_path)
    before_identity = (original.session_id, original.generation, original.shell_pid)
    captured = {}

    def confirm(_screen, _title, _items, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(cancelled=False, selected_key="remove")

    monkeypatch.setattr(jtt, "run_list_picker", confirm)

    jtt._remove_card_reference(None, state)

    assert registry.stopped == []
    assert registry.closed == 1
    current = registry.list_sessions()
    assert [(item.session_id, item.generation, item.shell_pid) for item in current] == [
        before_identity,
    ]
    recovery = next(item for item in store.workspaces()
                    if item["name"] == "복구 보관함")
    assert recovery["terminal_ids"] == [original.session_id]
    assert "종료하지 않았습니다" in state["jtt_notice"]
    assert any("생존 여부는 확인되지 않았습니다" in line
               for line in captured["preamble"])


def test_saved_workspace_delete_is_confirmed_and_does_not_stop_terminal(tmp_path, monkeypatch):
    store = JTTWorkspaceStore(tmp_path / "jtt")
    saved = store.create_workspace("Saved")
    store.set_terminal_ids(saved["id"], ["session-live"])
    identity = TerminalIdentity(
        "session-live", "generation-a", 1000, "s1", 202, "p202", "/bin/bash",
        "Live shell", "running", renderer_state="dependency_missing",
    )
    registry = FakeRegistry([identity])
    state = jtt.build_state(store, registry)
    choice_index = next(index for index, item in enumerate(state["choices"])
                        if item.get("kind") == "workspace" and item.get("id") == saved["id"])
    renderer._set_choice(state, choice_index)
    saved_snapshot = next(item for item in store.workspaces() if item["id"] == saved["id"])
    captured = {}

    def confirm(_screen, title, items, **kwargs):
        captured.update(title=title, items=items, kwargs=kwargs)
        return SimpleNamespace(cancelled=False, selected_key="delete")

    monkeypatch.setattr(jtt, "run_list_picker", confirm)

    jtt._confirm_and_delete_workspace(None, state)

    remaining = {item["id"]: item for item in store.workspaces()}
    assert saved["id"] not in remaining
    assert remaining[store.DEFAULT_ID]["terminal_ids"] == ["session-live"]
    assert registry.stopped == []
    assert captured["title"] == "저장 작업공간 삭제"
    assert captured["items"][0] == ("cancel", "취소")
    assert captured["items"][1] == ("delete", "Saved 삭제 · 포함 터미널 1개")
    assert [(item.session_id, item.shell_pid) for item in registry.list_sessions()] == [
        (identity.session_id, identity.shell_pid),
    ]
    assert saved_snapshot["terminal_ids"] == [identity.session_id]


def test_saved_workspace_delete_cancel_and_selection_revalidation_do_not_write(tmp_path, monkeypatch):
    store = JTTWorkspaceStore(tmp_path / "jtt")
    first = store.create_workspace("First")
    second = store.create_workspace("Second")
    state = jtt.build_state(store, FakeRegistry())
    renderer._set_choice(state, next(index for index, item in enumerate(state["choices"])
                                     if item.get("id") == first["id"]))
    before = store.path.read_bytes()
    calls = []

    def cancel(*args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(cancelled=True, selected_key=None)

    monkeypatch.setattr(jtt, "run_list_picker", cancel)
    jtt._confirm_and_delete_workspace(None, state)
    assert store.path.read_bytes() == before
    assert calls[0][0][2][0][0] == "cancel"

    calls = 0

    def change_selection(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        renderer._set_choice(
            state, next(index for index, item in enumerate(state["choices"])
                        if item.get("id") == second["id"]),
        )
        return SimpleNamespace(cancelled=False, selected_key="delete")

    renderer._set_choice(state, next(index for index, item in enumerate(state["choices"])
                                     if item.get("id") == first["id"]))
    monkeypatch.setattr(jtt, "run_list_picker", change_selection)
    jtt._confirm_and_delete_workspace(None, state)

    assert calls == 1
    assert store.path.read_bytes() == before
    assert {item["id"] for item in store.workspaces()} >= {first["id"], second["id"]}
    assert "차단" in state["jtt_notice"]


def test_work_kit_delete_cancel_and_selection_revalidation_do_not_write(tmp_path, monkeypatch):
    store = JTTWorkspaceStore(tmp_path / "jtt")
    kits = JTTWorkKitStore(store.root)
    first = kits.create_kit("First", "One", _specs(1))
    second = kits.create_kit("Second", "Two", _specs(2))
    state = jtt.build_state(store, FakeRegistry())
    renderer._set_choice(state, _kit_choice(state, first["id"]))
    before = kits.path.read_bytes()
    calls = []

    def cancel(*args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(cancelled=True, selected_key=None)

    monkeypatch.setattr(jtt, "run_list_picker", cancel)
    jtt._confirm_and_delete_work_kit(None, state)
    assert kits.path.read_bytes() == before
    assert calls[0][0][2][0][0] == "cancel"

    def change_selection(*_args, **_kwargs):
        renderer._set_choice(state, _kit_choice(state, second["id"]))
        return SimpleNamespace(cancelled=False, selected_key="delete")

    renderer._set_choice(state, _kit_choice(state, first["id"]))
    monkeypatch.setattr(jtt, "run_list_picker", change_selection)
    jtt._confirm_and_delete_work_kit(None, state)

    assert kits.path.read_bytes() == before
    assert [kit["id"] for kit in kits.kits()] == [first["id"], second["id"]]
    assert "차단" in state["jtt_notice"]


def test_work_kit_delete_changes_only_selected_template(tmp_path, monkeypatch):
    store = JTTWorkspaceStore(tmp_path / "jtt")
    kit_store = JTTWorkKitStore(store.root)
    first = kit_store.create_kit("First", "One", _specs(1))
    second = kit_store.create_kit("Second", "Two", _specs(2))
    workspace_path = store.path
    state = jtt.build_state(store, FakeRegistry())
    renderer._set_choice(state, _kit_choice(state, first["id"]))
    monkeypatch.setattr(jtt, "run_list_picker", lambda *_args, **_kwargs:
                        SimpleNamespace(cancelled=False, selected_key="delete"))

    jtt._confirm_and_delete_work_kit(None, state)

    assert kit_store.kits() == [second]
    assert not workspace_path.exists()
    assert state["work_kit_preview"] is None
    assert "Work Kit 템플릿만 삭제" in state["jtt_notice"]


def test_uncertain_card_cleanup_preserves_shared_reference_without_stopping_session(
        tmp_path, monkeypatch):
    store, registry, state, original = _uncertain_terminal_state(tmp_path)
    shared = store.create_workspace("Shared workspace")
    store.set_terminal_ids(shared["id"], [original.session_id])
    before_identity = (original.session_id, original.generation, original.shell_pid)
    monkeypatch.setattr(jtt, "run_list_picker", lambda *_args, **_kwargs:
                        SimpleNamespace(cancelled=False, selected_key="remove"))

    jtt._remove_card_reference(None, state)

    workspaces = {item["id"]: item for item in store.workspaces()}
    assert original.session_id not in workspaces[store.DEFAULT_ID]["terminal_ids"]
    assert workspaces[shared["id"]]["terminal_ids"] == [original.session_id]
    assert registry.stopped == []
    assert registry.closed == 1
    assert [(item.session_id, item.generation, item.shell_pid)
            for item in registry.list_sessions()] == [before_identity]


def test_sparse_orphan_cleanup_is_ref_only_and_never_uses_stop_path(tmp_path, monkeypatch):
    store = JTTWorkspaceStore(tmp_path / "jtt")
    default = store.workspaces()[0]
    store.set_terminal_ids(default["id"], ["orphan-session"])
    registry = FakeRegistry()
    state = jtt.build_state(store, registry)
    row = renderer._selected_row(state)

    assert row["terminal_view_state"] == "session_unavailable"
    assert row["managed_identity"] == {"session_id": "orphan-session"}
    assert jtt._capture_stop_target(state) is None
    target = jtt._capture_card_reference_target(state)
    assert target["classification"] == "uncertain"
    assert target["sparse_identity"] is True
    assert jtt._handle_prefix_command(None, state, "x") is False
    assert registry.stopped == []

    monkeypatch.setattr(jtt, "run_list_picker", lambda *_args, **_kwargs:
                        SimpleNamespace(cancelled=False, selected_key="remove"))
    jtt._remove_card_reference(None, state)

    recovery = next(item for item in store.workspaces()
                    if item["name"] == "복구 보관함")
    assert recovery["terminal_ids"] == ["orphan-session"]
    assert registry.stopped == []
    assert registry.closed == 0
    assert "종료하지 않았습니다" in state["jtt_notice"]


def test_uncertain_card_cleanup_reports_committed_after_directory_sync_failure(
        tmp_path, monkeypatch):
    store, registry, state, original = _uncertain_terminal_state(tmp_path)
    monkeypatch.setattr(jtt, "run_list_picker", lambda *_args, **_kwargs:
                        SimpleNamespace(cancelled=False, selected_key="remove"))
    _fail_directory_fsync(monkeypatch)

    jtt._remove_card_reference(None, state)

    recovery = next(item for item in store.workspaces()
                    if item["name"] == "복구 보관함")
    assert recovery["terminal_ids"] == [original.session_id]
    assert registry.stopped == []
    assert registry.closed == 1
    assert original.session_id not in state["managed_terminals"]
    assert "카드 참조 정리 반영됨" in state["jtt_notice"]
    assert "디렉터리 동기화 확인 필요" in state["jtt_notice"]


def test_sparse_orphan_revalidation_blocks_if_registry_identity_appears(tmp_path, monkeypatch):
    store = JTTWorkspaceStore(tmp_path / "jtt")
    default = store.workspaces()[0]
    store.set_terminal_ids(default["id"], ["orphan-session"])
    registry = FakeRegistry()
    state = jtt.build_state(store, registry)
    before = store.path.read_bytes()

    def identity_appears(*_args, **_kwargs):
        registry.identities = (TerminalIdentity(
            "orphan-session", "generation-a", 1000, "s1", 302, "p302", "/bin/bash",
            "Recovered", "running", renderer_state="ready",
        ),)
        return SimpleNamespace(cancelled=False, selected_key="remove")

    monkeypatch.setattr(jtt, "run_list_picker", identity_appears)
    jtt._remove_card_reference(None, state)

    assert store.path.read_bytes() == before
    assert registry.stopped == []
    assert "차단" in state["jtt_notice"]
