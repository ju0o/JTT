from dataclasses import dataclass, field
import curses
from types import SimpleNamespace

import pytest

from tmux_agent_tower.state.jtt_workspaces import JTTWorkspaceStore
from tmux_agent_tower.terminal import TerminalIdentity
from tmux_agent_tower.ui import jtt
from tmux_agent_tower.ui import ux_preview_v2 as renderer


@dataclass
class FakeUpdate:
    output: bytes = b""
    sequence: int = 1
    state: str = "running"
    synced: bool = True
    gap: bool = False
    more: bool = False
    columns: int = 80
    lines: int = 24
    screen: object = field(default_factory=lambda: SimpleNamespace(
        rows=(), cursor_x=0, cursor_y=0, cursor_visible=False))
    renderer_available: bool = True
    renderer_state: str = "ready"


class FakeClient:
    def __init__(self, identity):
        self.identity = identity
        self.state = identity.state
        self.synced = True
        self.lease = False
        self.sent = []
        self.closed = False
        self.update = FakeUpdate()
        self.acquire_count = 0

    def poll(self):
        return self.update

    def acquire(self):
        self.acquire_count += 1
        self.lease = True
        return True

    def release(self):
        self.lease = False

    def send(self, data):
        self.sent.append(data)
        return len(data)

    def resize(self, *_size):
        pass

    def close(self):
        self.closed = True


class FakeRegistry:
    def __init__(self, identities):
        self.identities = tuple(identities)
        self.clients = {item.session_id: FakeClient(item) for item in self.identities}
        self.created = 0
        self.connected = []
        self.stop_calls = []

    def list_sessions(self):
        return self.identities

    def connect(self, session_id):
        self.connected.append(session_id)
        return self.clients[session_id]

    def create(self, name=None):
        self.created += 1
        session_id = f"session-created-{self.created}"
        identity = _identity(session_id, 900 + self.created, f"gen-created-{self.created}")
        self.identities = (*self.identities, identity)
        self.clients[session_id] = FakeClient(identity)
        return self.clients[session_id]

    def stop(self, session_id, **expected):
        self.stop_calls.append((session_id, expected))


def _identity(session_id, pid, generation):
    return TerminalIdentity(session_id, generation, 1000, "s1", pid, f"p{pid}",
                             "/bin/bash", session_id, "running", renderer_state="ready")


class FakeScreen:
    def getmaxyx(self):
        return (40, 120)


def _prefix(screen, state, command):
    assert renderer._handle_key(screen, state, "\x07") is False
    assert state["jtt_prefix_pending"]
    return renderer._handle_key(screen, state, command)


def test_build_state_is_read_only_and_maps_cards_to_exact_sessions(tmp_path):
    root = tmp_path / "jtt"
    store = JTTWorkspaceStore(root)
    registry = FakeRegistry([_identity("session-a", 101, "gen-a"),
                              _identity("session-b", 202, "gen-b")])
    default = store.workspaces()[0]
    store.set_terminal_ids(default["id"], ["session-a", "session-b"])

    state = jtt.build_state(store, registry)

    assert [row["managed_id"] for row in state["panes"]] == ["session-a", "session-b"]
    assert [row["pane_pid"] for row in state["panes"]] == ["101", "202"]
    assert state["runtime_mode"] == "jtt"
    assert registry.created == 0
    assert registry.clients["session-a"].closed is False

    renderer._set_choice(state, 0)
    state["pane_index"] = 1
    selected = renderer._selected_row(state)
    binding = renderer._managed_binding(state)
    assert selected["managed_id"] == "session-b"
    assert binding.matches(selected)
    assert binding.session_id == selected["managed_id"]


def test_input_mode_sends_keys_to_exact_selected_session_and_ctrl_bracket_releases(tmp_path):
    store = JTTWorkspaceStore(tmp_path / "jtt")
    identities = [_identity("session-a", 101, "gen-a"), _identity("session-b", 202, "gen-b")]
    registry = FakeRegistry(identities)
    store.set_terminal_ids(store.DEFAULT_ID, ["session-a", "session-b"])
    state = jtt.build_state(store, registry)
    state["pane_index"] = 1
    binding = renderer._managed_binding(state)
    binding.refresh()

    assert binding.enter(None, state) is True
    assert binding.handle_key("x") is True
    assert binding.handle_key(curses.KEY_UP) is True
    assert registry.clients["session-b"].sent == [b"x", b"\x1b[A"]
    assert registry.clients["session-a"].sent == []
    assert binding.handle_key(29) is True
    assert not binding.input_mode
    assert not registry.clients["session-b"].lease
    assert binding.enter(None, state) is True
    assert binding.handle_key("\x1d") is True
    assert not binding.input_mode


def test_expired_input_lease_exits_mode_before_forwarding_more_keys(tmp_path):
    store = JTTWorkspaceStore(tmp_path / "jtt")
    registry = FakeRegistry([_identity("session-a", 101, "gen-a")])
    store.set_terminal_ids(store.DEFAULT_ID, ["session-a"])
    state = jtt.build_state(store, registry)
    binding = renderer._managed_binding(state)
    binding.refresh()

    assert binding.enter(None, state) is True
    registry.clients["session-a"].lease = False
    binding.refresh()

    assert not binding.input_mode
    assert "권한 만료" in binding.status
    assert binding.handle_key("x") is False
    assert registry.clients["session-a"].sent == []


def test_missing_or_unsynced_session_is_stale_and_cannot_enter(tmp_path):
    store = JTTWorkspaceStore(tmp_path / "jtt")
    registry = FakeRegistry([_identity("session-a", 101, "gen-a")])
    store.set_terminal_ids(store.DEFAULT_ID, ["session-missing", "session-a"])
    state = jtt.build_state(store, registry)
    assert state["panes"][0]["stale"] is True
    state["pane_index"] = 1
    binding = renderer._managed_binding(state)
    registry.clients["session-a"].update = FakeUpdate(synced=False, gap=True)
    binding.refresh()

    assert binding.enter(None, state) is False
    assert registry.clients["session-a"].sent == []


def test_workspace_switch_attaches_only_selected_sessions_and_releases_old_client(tmp_path):
    store = JTTWorkspaceStore(tmp_path / "jtt")
    registry = FakeRegistry([_identity("session-a", 101, "gen-a"),
                              _identity("session-b", 202, "gen-b")])
    store.set_terminal_ids(store.DEFAULT_ID, ["session-a"])
    saved = store.create_workspace("Saved")
    store.set_terminal_ids(saved["id"], ["session-b"])

    state = jtt.build_state(store, registry)
    assert [row["managed_id"] for row in state["panes"]] == ["session-a"]
    assert set(state["managed_terminals"]) == {"session-a"}
    assert not registry.clients["session-a"].closed
    assert registry.connected == ["session-a"]

    saved_index = next(i for i, choice in enumerate(state["choices"])
                       if choice["id"] == saved["id"])
    renderer._set_choice(state, saved_index)
    assert [row["managed_id"] for row in state["panes"]] == ["session-b"]
    assert set(state["managed_terminals"]) == {"session-b"}
    assert registry.clients["session-a"].closed
    assert not registry.clients["session-b"].closed
    assert registry.connected == ["session-a", "session-b"]
    assert renderer._managed_binding(state).session_id == "session-b"


def test_empty_workspace_can_reach_new_terminal_with_keyboard(tmp_path):
    class Screen:
        def getmaxyx(self):
            return (40, 120)

    state = jtt.build_state(JTTWorkspaceStore(tmp_path / "jtt"), FakeRegistry([]))
    activated = []
    state["jtt_center_action_handler"] = lambda _screen, _state, index: activated.append(index)
    screen = Screen()

    assert renderer._handle_key(screen, state, "\t") is False
    assert state["active_panel"] == 1
    assert renderer._handle_key(screen, state, curses.KEY_DOWN) is False
    assert state["center_actions_focused"] is True
    assert renderer._handle_key(screen, state, curses.KEY_RIGHT) is False
    assert state["center_action_index"] == 1
    assert renderer._handle_key(screen, state, "\n") is False
    assert activated == [1]


def test_renderer_failure_keeps_running_shell_distinct_from_disconnected(tmp_path):
    store = JTTWorkspaceStore(tmp_path / "jtt")
    registry = FakeRegistry([_identity("session-a", 101, "gen-a")])
    store.set_terminal_ids(store.DEFAULT_ID, ["session-a"])
    state = jtt.build_state(store, registry)
    state["active_panel"] = 2
    client = registry.clients["session-a"]
    client.update = FakeUpdate(synced=False, renderer_available=False,
                               renderer_state="dependency_missing", screen=None)

    jtt._refresh_and_resize(state, FakeScreen())
    row = state["panes"][0]
    assert row["status"] == "WORKING"
    assert row["terminal_shell_state"] == "running"
    assert row["terminal_view_state"] == "renderer_missing"
    assert "셸 실행 중" in renderer._status(row)
    assert "연결 끊김" not in renderer._status(row)
    assert row["terminal_preview"] == "터미널 화면 미리보기 없음"
    assert renderer._managed_binding(state).input_mode is False


def test_renderer_parse_failure_and_resync_have_distinct_states(tmp_path):
    store = JTTWorkspaceStore(tmp_path / "jtt")
    registry = FakeRegistry([_identity("session-a", 101, "gen-a")])
    store.set_terminal_ids(store.DEFAULT_ID, ["session-a"])
    state = jtt.build_state(store, registry)
    binding = renderer._managed_binding(state)
    client = registry.clients["session-a"]
    client.update = FakeUpdate(synced=False, renderer_available=False,
                               renderer_state="parse_failed", screen=None)
    binding.refresh()
    assert binding.terminal_view_state == "renderer_error"
    client.update = FakeUpdate(synced=False, renderer_available=True,
                               renderer_state="ready", gap=True, screen=None)
    binding.refresh()
    assert binding.terminal_view_state == "resyncing"


def test_supervisor_failure_is_not_reported_as_shell_exit(tmp_path):
    store = JTTWorkspaceStore(tmp_path / "jtt")
    registry = FakeRegistry([_identity("session-a", 101, "gen-a")])
    store.set_terminal_ids(store.DEFAULT_ID, ["session-a"])
    state = jtt.build_state(store, registry)
    binding = renderer._managed_binding(state)
    registry.clients["session-a"].poll = lambda: (_ for _ in ()).throw(OSError("fixture"))
    jtt._refresh_and_resize(state, FakeScreen())
    row = state["panes"][0]
    assert row["terminal_shell_state"] == "running"
    assert row["terminal_view_state"] == "supervisor_connection_failed"
    assert renderer._status(row) == "! Supervisor 연결 실패"


def test_bare_navigation_n_does_not_create_and_prefix_n_creates_one_then_acquires_once(tmp_path):
    store = JTTWorkspaceStore(tmp_path / "jtt")
    registry = FakeRegistry([])
    state = jtt.build_state(store, registry)
    screen = FakeScreen()
    state["stdscr"] = screen

    assert renderer._handle_key(screen, state, "n") is False
    assert registry.created == 0
    assert _prefix(screen, state, "N") is False
    assert registry.created == 1
    assert len(state["panes"]) == 1
    assert state["active_panel"] == 2
    assert state["jtt_pending_auto_acquire"] is not None
    jtt._refresh_and_resize(state, screen)
    binding = renderer._managed_binding(state)
    assert binding is not None and binding.input_mode
    assert registry.clients["session-created-1"].acquire_count == 1
    jtt._refresh_and_resize(state, screen)
    assert registry.clients["session-created-1"].acquire_count == 1
    assert registry.created == 1


def test_new_terminal_renderer_failure_clears_auto_acquire_without_input(tmp_path):
    store = JTTWorkspaceStore(tmp_path / "jtt")
    registry = FakeRegistry([])
    state = jtt.build_state(store, registry)
    screen = FakeScreen()
    state["stdscr"] = screen
    assert _prefix(screen, state, "n") is False
    client = registry.clients["session-created-1"]
    client.update = FakeUpdate(synced=False, renderer_available=False,
                               renderer_state="initialization_failed", screen=None)

    jtt._refresh_and_resize(state, screen)
    assert state["jtt_pending_auto_acquire"] is None
    assert client.acquire_count == 0
    assert not client.lease
    assert "VT 렌더러 오류" in state["jtt_notice"]
    assert registry.created == 1


def test_input_mode_bare_n_q_tab_are_forwarded_and_ctrl_bracket_releases(tmp_path):
    store = JTTWorkspaceStore(tmp_path / "jtt")
    registry = FakeRegistry([_identity("session-a", 101, "gen-a")])
    store.set_terminal_ids(store.DEFAULT_ID, ["session-a"])
    state = jtt.build_state(store, registry)
    state["active_panel"] = 2
    screen = FakeScreen()
    binding = renderer._managed_binding(state)
    binding.refresh()
    assert binding.enter(screen, state)

    assert renderer._handle_key(screen, state, "n") is False
    assert renderer._handle_key(screen, state, "q") is False
    assert renderer._handle_key(screen, state, "\t") is False
    assert registry.created == 0
    assert registry.clients["session-a"].sent == [b"n", b"q", b"\t"]
    assert renderer._handle_key(screen, state, "\x1d") is False
    assert not binding.input_mode


def test_prefix_double_ctrl_g_sends_one_byte_only_to_valid_leased_target(tmp_path):
    store = JTTWorkspaceStore(tmp_path / "jtt")
    registry = FakeRegistry([_identity("session-a", 101, "gen-a"),
                              _identity("session-b", 202, "gen-b")])
    store.set_terminal_ids(store.DEFAULT_ID, ["session-a", "session-b"])
    state = jtt.build_state(store, registry)
    state["pane_index"] = 0
    state["selected_target_id"] = "session-a"
    state["selected_signature"] = renderer._pane_signature(state["panes"][0])
    screen = FakeScreen()
    binding = renderer._managed_binding(state)
    binding.refresh()
    assert binding.enter(screen, state)

    assert _prefix(screen, state, "\x07") is False
    assert registry.clients["session-a"].sent == [b"\x07"]
    assert registry.clients["session-b"].sent == []
    assert not state["jtt_prefix_pending"]


def test_prefix_h_l_releases_lease_before_changing_card_and_invalid_is_consumed(tmp_path):
    store = JTTWorkspaceStore(tmp_path / "jtt")
    registry = FakeRegistry([_identity("session-a", 101, "gen-a"),
                              _identity("session-b", 202, "gen-b")])
    store.set_terminal_ids(store.DEFAULT_ID, ["session-a", "session-b"])
    state = jtt.build_state(store, registry)
    screen = FakeScreen()
    binding = renderer._managed_binding(state)
    binding.refresh()
    assert binding.enter(screen, state)

    assert _prefix(screen, state, "L") is False
    assert state["pane_index"] == 1
    assert state["active_panel"] == 1
    assert not binding.input_mode and not registry.clients["session-a"].lease
    assert _prefix(screen, state, "z") is False
    assert registry.created == 0
    assert not registry.clients["session-b"].sent


def test_prefix_w_releases_input_and_help_lists_prefix_commands(tmp_path, monkeypatch):
    store = JTTWorkspaceStore(tmp_path / "jtt")
    registry = FakeRegistry([_identity("session-a", 101, "gen-a")])
    store.set_terminal_ids(store.DEFAULT_ID, ["session-a"])
    state = jtt.build_state(store, registry)
    screen = FakeScreen()
    binding = renderer._managed_binding(state)
    binding.refresh()
    assert binding.enter(screen, state)

    assert _prefix(screen, state, "W") is False
    assert state["active_panel"] == 0
    assert not binding.input_mode
    assert not registry.clients["session-a"].lease

    shown = {}
    monkeypatch.setattr(
        jtt, "show_message_screen",
        lambda _screen, title, lines: shown.update(title=title, lines=lines),
    )
    assert _prefix(screen, state, "?") is False
    assert shown["title"] == "JTT 키 안내"
    assert any("Ctrl+G n" in line for line in shown["lines"])
    assert any("Ctrl+G h/l" in line for line in shown["lines"])
    assert any("Ctrl+G x" in line for line in shown["lines"])
    assert any("Ctrl+]" in line for line in shown["lines"])


def test_prefix_cancel_timeout_and_detach_are_one_shot(tmp_path):
    store = JTTWorkspaceStore(tmp_path / "jtt")
    registry = FakeRegistry([])
    state = jtt.build_state(store, registry)
    screen = FakeScreen()
    state["stdscr"] = screen
    assert renderer._handle_key(screen, state, "\x07") is False
    assert renderer._handle_key(screen, state, "\x1b") is False
    assert not state["jtt_prefix_pending"]
    assert registry.created == 0
    assert _prefix(screen, state, "d") is True
    assert registry.created == 0


def _stop_ready_state(tmp_path, *identities):
    store = JTTWorkspaceStore(tmp_path / "jtt")
    registry = FakeRegistry(list(identities))
    store.set_terminal_ids(store.DEFAULT_ID, [item.session_id for item in identities])
    state = jtt.build_state(store, registry)
    return state, registry


def test_prefix_x_stops_only_exact_confirmed_terminal_and_cancel_is_default(tmp_path, monkeypatch):
    identities = [_identity("session-a", 101, "gen-a"),
                  _identity("session-b", 202, "gen-b")]
    state, registry = _stop_ready_state(tmp_path, *identities)
    state["pane_index"] = 1
    state["selected_target_id"] = "session-b"
    state["selected_signature"] = renderer._pane_signature(state["panes"][1])
    captured = {}

    def confirm(_screen, title, items, **kwargs):
        captured.update(title=title, items=items, kwargs=kwargs)
        return SimpleNamespace(cancelled=False, selected_key="stop")

    monkeypatch.setattr(jtt, "run_list_picker", confirm)
    screen = FakeScreen()
    assert _prefix(screen, state, "x") is False

    assert captured["title"] == "터미널 종료"
    assert captured["items"][0] == ("cancel", "취소")
    assert captured["items"][1][0] == "stop"
    assert "취소가 기본 선택" in captured["kwargs"]["footer_hint"]
    assert registry.stop_calls == [(
        "session-b", {"expected_generation": "gen-b", "expected_shell_pid": 202}
    )]
    workspace = state["jtt_store"].workspaces()[0]
    assert workspace["terminal_ids"] == ["session-a"]
    assert "session-b" not in state["managed_terminals"]


def test_prefix_x_cancel_does_not_stop_or_change_workspace(tmp_path, monkeypatch):
    state, registry = _stop_ready_state(tmp_path, _identity("session-a", 101, "gen-a"))
    monkeypatch.setattr(
        jtt, "run_list_picker",
        lambda *_args, **_kwargs: SimpleNamespace(cancelled=True, selected_key=None),
    )
    before = state["jtt_store"].workspaces()[0]["terminal_ids"]

    assert _prefix(FakeScreen(), state, "x") is False

    assert registry.stop_calls == []
    assert state["jtt_store"].workspaces()[0]["terminal_ids"] == before


def test_prefix_x_selection_change_while_confirmation_is_open_fails_closed(tmp_path, monkeypatch):
    identities = [_identity("session-a", 101, "gen-a"),
                  _identity("session-b", 202, "gen-b")]
    state, registry = _stop_ready_state(tmp_path, *identities)

    def change_selection_then_confirm(_screen, _title, _items, **_kwargs):
        state["pane_index"] = 1
        state["selected_target_id"] = "session-b"
        state["selected_signature"] = renderer._pane_signature(state["panes"][1])
        return SimpleNamespace(cancelled=False, selected_key="stop")

    monkeypatch.setattr(jtt, "run_list_picker", change_selection_then_confirm)
    assert _prefix(FakeScreen(), state, "x") is False

    assert registry.stop_calls == []
    assert "변경되어 종료를 차단" in state["jtt_notice"]


def test_prefix_x_registry_identity_change_while_modal_is_open_fails_closed(tmp_path, monkeypatch):
    original = _identity("session-a", 101, "gen-a")
    state, registry = _stop_ready_state(tmp_path, original)

    def replace_registry_identity_then_confirm(_screen, _title, _items, **_kwargs):
        registry.identities = (_identity("session-a", 303, "gen-replacement"),)
        return SimpleNamespace(cancelled=False, selected_key="stop")

    monkeypatch.setattr(jtt, "run_list_picker", replace_registry_identity_then_confirm)
    assert _prefix(FakeScreen(), state, "x") is False

    assert registry.stop_calls == []
    assert "변경되어 종료를 차단" in state["jtt_notice"]


def test_prefix_x_binding_replacement_while_modal_is_open_fails_closed(tmp_path, monkeypatch):
    identity = _identity("session-a", 101, "gen-a")
    state, registry = _stop_ready_state(tmp_path, identity)

    def replace_binding_then_confirm(_screen, _title, _items, **_kwargs):
        client = FakeClient(identity)
        state["managed_terminals"]["session-a"] = jtt.ManagedTerminalBinding(client, identity)
        return SimpleNamespace(cancelled=False, selected_key="stop")

    monkeypatch.setattr(jtt, "run_list_picker", replace_binding_then_confirm)
    assert _prefix(FakeScreen(), state, "x") is False

    assert registry.stop_calls == []
    assert "변경되어 종료를 차단" in state["jtt_notice"]


@pytest.mark.parametrize(
    ("expected_generation", "expected_shell_pid"),
    [("old-generation", 456), ("current-generation", 999)],
)
def test_registry_stop_checks_expected_identity_before_request(
    tmp_path, monkeypatch, expected_generation, expected_shell_pid,
):
    from tmux_agent_tower.terminal import manager

    registry = manager.TerminalRegistry(tmp_path / "sessions")
    metadata = {
        "session_id": "a" * 32,
        "generation": "current-generation",
        "supervisor_pid": 123,
        "supervisor_start": "sup-start",
        "shell_pid": 456,
        "shell_start": "shell-start",
        "shell_path": "/bin/bash",
        "name": "Fixture",
        "state": "running",
    }
    monkeypatch.setattr(manager, "_validate_identity", lambda _root, _session: (tmp_path, metadata))
    connected = []
    monkeypatch.setattr(registry, "connect", lambda session: connected.append(session))

    with pytest.raises(manager.TerminalError, match="identity changed"):
        registry.stop("a" * 32, expected_generation=expected_generation,
                      expected_shell_pid=expected_shell_pid)

    assert connected == []


@pytest.mark.parametrize(
    "replacement",
    [
        {"generation": "replacement-generation", "shell_pid": 456},
        {"generation": "current-generation", "shell_pid": 789},
    ],
)
def test_registry_stop_rechecks_connected_client_before_stop_request(
    tmp_path, monkeypatch, replacement,
):
    from tmux_agent_tower.terminal import manager

    registry = manager.TerminalRegistry(tmp_path / "sessions")
    metadata = {
        "session_id": "b" * 32,
        "generation": "current-generation",
        "supervisor_pid": 123,
        "supervisor_start": "sup-start",
        "shell_pid": 456,
        "shell_start": "shell-start",
        "shell_path": "/bin/bash",
        "name": "Fixture",
        "state": "running",
    }
    monkeypatch.setattr(manager, "_validate_identity", lambda _root, _session: (tmp_path, metadata))
    requested = []
    closed = []
    client = SimpleNamespace(
        identity=SimpleNamespace(**replacement),
        _request=lambda operation: requested.append(operation),
        close=lambda: closed.append(True),
    )
    monkeypatch.setattr(registry, "connect", lambda _session: client)

    with pytest.raises(manager.TerminalError, match="identity changed"):
        registry.stop("b" * 32, expected_generation="current-generation", expected_shell_pid=456)

    assert requested == []
    assert closed == [True]


def test_prefix_deadline_is_four_seconds_and_boundary_is_fail_closed(tmp_path, monkeypatch):
    clock = [50.0]
    monkeypatch.setattr(renderer.time, "monotonic", lambda: clock[0])
    store = JTTWorkspaceStore(tmp_path / "jtt")
    registry = FakeRegistry([])
    state = jtt.build_state(store, registry)
    screen = FakeScreen()

    assert renderer._handle_key(screen, state, "\x07") is False
    assert state["jtt_prefix_deadline"] == 54.0
    clock[0] = 53.999
    assert renderer._handle_key(screen, state, "n") is False
    assert registry.created == 1

    second = jtt.build_state(JTTWorkspaceStore(tmp_path / "jtt-second"), FakeRegistry([]))
    second_registry = second["jtt_registry"]
    assert renderer._handle_key(screen, second, "\x07") is False
    clock[0] = 58.0
    assert renderer._handle_key(screen, second, "n") is False
    assert second_registry.created == 0
    assert not second["jtt_prefix_pending"]
