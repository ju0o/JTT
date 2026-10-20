import curses
import json
import os
import stat
from types import SimpleNamespace

import pytest

from tmux_agent_tower.state.jtt_work_kits import (
    JTTWorkKitStore,
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
    with pytest.raises(WorkKitStoreError, match="replacement completed"):
        store.create_kit("New", "May already be present", _specs(2))

    assert [kit["name"] for kit in JTTWorkKitStore(store.root).kits()] == ["Existing", "New"]


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
