from dataclasses import dataclass
import curses

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
    screen: object = None
    renderer_available: bool = True


class FakeClient:
    def __init__(self, identity):
        self.identity = identity
        self.state = identity.state
        self.synced = True
        self.lease = False
        self.sent = []
        self.closed = False
        self.update = FakeUpdate()

    def poll(self):
        return self.update

    def acquire(self):
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

    def list_sessions(self):
        return self.identities

    def connect(self, session_id):
        self.connected.append(session_id)
        return self.clients[session_id]

    def create(self, name=None):
        self.created += 1
        raise AssertionError("the state builder must not start a shell")


def _identity(session_id, pid, generation):
    return TerminalIdentity(session_id, generation, 1000, "s1", pid, f"p{pid}",
                             "/bin/bash", session_id, "running")


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
