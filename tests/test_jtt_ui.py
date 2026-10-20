from dataclasses import dataclass, field
import curses
import os
import stat
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


def _identity(session_id, pid, generation, state="running", renderer_state="ready"):
    return TerminalIdentity(session_id, generation, 1000, "s1", pid, f"p{pid}",
                             "/bin/bash", session_id, state, renderer_state=renderer_state)


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


def test_logical_window_selection_filters_cards_without_changing_canonical_refs(tmp_path):
    store = JTTWorkspaceStore(tmp_path / "jtt")
    registry = FakeRegistry([_identity("session-a", 101, "gen-a"),
                             _identity("session-b", 202, "gen-b"),
                             _identity("session-c", 303, "gen-c")])
    store.set_terminal_ids(store.DEFAULT_ID, ["session-a", "session-b", "session-c"])
    layouts = jtt.JTTLayoutStore(store.root)
    folder = layouts.create_folder(store.DEFAULT_ID, "Review")
    window = layouts.create_window(store.DEFAULT_ID, folder["id"], "Code")
    layouts.move_terminal(store.DEFAULT_ID, "session-b", window["id"],
                          ["session-a", "session-b", "session-c"])

    state = jtt.build_state(store, registry)
    choice_index = next(index for index, item in enumerate(state["choices"])
                        if item.get("kind") == "logical_window"
                        and item.get("window_id") == window["id"])
    renderer._set_choice(state, choice_index)

    assert [row["managed_id"] for row in state["panes"]] == ["session-b"]
    assert store.workspaces()[0]["terminal_ids"] == ["session-a", "session-b", "session-c"]
    assert registry.stop_calls == []


def test_deleting_logical_window_recovers_sessions_without_stopping_them(tmp_path, monkeypatch):
    store = JTTWorkspaceStore(tmp_path / "jtt")
    registry = FakeRegistry([_identity("session-a", 101, "gen-a"),
                             _identity("session-b", 202, "gen-b")])
    canonical = ["session-a", "session-b"]
    store.set_terminal_ids(store.DEFAULT_ID, canonical)
    layouts = jtt.JTTLayoutStore(store.root)
    folder = layouts.create_folder(store.DEFAULT_ID, "Review")
    window = layouts.create_window(store.DEFAULT_ID, folder["id"], "Code")
    for session_id in canonical:
        layouts.move_terminal(store.DEFAULT_ID, session_id, window["id"], canonical)
    state = jtt.build_state(store, registry)
    choice = next(item for item in state["choices"]
                  if item.get("kind") == "logical_window" and item.get("window_id") == window["id"])
    jtt._attach_layout_choice(state, choice)
    workspace, layout, _ = jtt._workspace_layout(state)
    state["stdscr"] = FakeScreen()
    monkeypatch.setattr(jtt, "run_list_picker", lambda *_a, **_k:
                        SimpleNamespace(cancelled=False, selected_key="delete"))

    jtt._delete_layout_item(FakeScreen(), state, workspace, layout, "delete_window")

    recovered = state["jtt_layout_store"].layout(store.DEFAULT_ID, canonical)
    assert recovered["windows"][0]["terminal_ids"] == canonical
    assert [row["managed_id"] for row in state["panes"]] == canonical
    assert registry.stop_calls == []
    assert [(item.session_id, item.shell_pid) for item in registry.list_sessions()] == [
        ("session-a", 101), ("session-b", 202),
    ]


def test_mini_sampler_requests_visible_center_cards_only_and_clears_when_hidden(tmp_path):
    identities = [_identity(f"session-{index}", 100 + index, f"gen-{index}")
                  for index in range(24)]
    store = JTTWorkspaceStore(tmp_path / "jtt")
    store.set_terminal_ids(store.DEFAULT_ID, [item.session_id for item in identities])
    state = jtt.build_state(store, FakeRegistry(identities))

    class SpySampler:
        def __init__(self):
            self.submissions = []
            self.result = None

        def submit(self, registry, token, targets):
            self.submissions.append((registry, token, tuple(targets)))
            return True

        def take(self):
            result, self.result = self.result, None
            return result

    sampler = SpySampler()
    state["mini_live_sampler"] = sampler
    state["active_panel"] = 1
    screen = FakeScreen()
    token, visible = jtt._visible_mini_targets(state, 120, 40)
    jtt._refresh_mini_live(state, screen)

    assert sampler.submissions[-1][1] == token
    assert sampler.submissions[-1][2] == tuple(visible)
    visible_ids = {target[0] for target in visible}
    assert len(visible_ids) < len(identities)
    for row in state["panes"]:
        if row["managed_id"] not in visible_ids:
            assert "mini_screen" not in row

    visible_row = next(row for row in state["panes"] if row["managed_id"] in visible_ids)
    visible_row["mini_screen"] = object()
    before = len(sampler.submissions)
    state["active_panel"] = 0
    jtt._refresh_mini_live(state, screen)
    assert len(sampler.submissions) == before
    assert "mini_screen" not in visible_row


def test_mini_live_cache_reports_only_output_change_and_clears_unsynced_sample(tmp_path, monkeypatch):
    from tmux_agent_tower.terminal import TerminalCell, TerminalScreen

    identity = _identity("session-a", 101, "gen-a")
    store = JTTWorkspaceStore(tmp_path / "jtt")
    store.set_terminal_ids(store.DEFAULT_ID, [identity.session_id])
    state = jtt.build_state(store, FakeRegistry([identity]))

    class SpySampler:
        def __init__(self):
            self.result = None
        def submit(self, _registry, _token, _targets):
            return True
        def take(self):
            result, self.result = self.result, None
            return result

    sampler = SpySampler()
    state["mini_live_sampler"] = sampler
    screen = FakeScreen()
    token, targets = jtt._visible_mini_targets(state, 120, 40)
    cells = ((TerminalCell("before", "default", "default", False, False, False,
                           False, False, False),),)
    sampler.result = (token, [{
        "session_id": identity.session_id, "generation": identity.generation,
        "shell_pid": identity.shell_pid, "sequence": 10,
        "screen": TerminalScreen(cells, 0, 0, True), "synced": True,
    }])
    ticks = iter((10.0, 11.0, 12.0, 13.0, 17.0))
    monkeypatch.setattr(jtt.time, "monotonic", lambda: next(ticks))

    jtt._refresh_mini_live(state, screen)
    row = state["panes"][0]
    row["terminal_view_state"] = "resyncing"
    original_view_state = row["terminal_view_state"]
    assert row["mini_screen"].rows[0][0].text == "before"
    assert row["mini_live_state"] == "synced"
    assert "카드 화면 관찰됨" in renderer._status(row)
    assert "재동기화" not in renderer._status(row)
    assert renderer._status_attr(row) == renderer._color(renderer._COLOR_WORKING, curses.A_DIM)
    assert row["terminal_view_state"] == original_view_state
    sampler.result = (token, [{
        "session_id": identity.session_id, "generation": identity.generation,
        "shell_pid": identity.shell_pid, "sequence": 11,
        "screen": TerminalScreen(((TerminalCell(
            "after", "default", "default", False, False, False, False, False, False
        ),),), 0, 0, True), "synced": True,
    }])
    jtt._refresh_mini_live(state, screen)
    assert row["mini_screen"].rows[0][0].text == "after"
    assert row["mini_live_state"] == "synced"
    assert row["terminal_activity"] == "출력 변화"
    assert "카드 화면 관찰됨" in renderer._status(row)
    assert row["terminal_view_state"] == original_view_state

    sampler.result = (token, [{
        "session_id": identity.session_id, "generation": identity.generation,
        "shell_pid": identity.shell_pid, "sequence": 12,
        "screen": None, "synced": False,
    }])
    jtt._refresh_mini_live(state, screen)
    assert "mini_screen" not in row
    assert "mini_live_state" not in row
    assert "terminal_activity" not in row
    assert renderer._status(row) == "↻ 셸 실행 중 · 화면 재동기화"

    sampler.result = (token, [{
        "session_id": identity.session_id, "generation": identity.generation,
        "shell_pid": identity.shell_pid, "sequence": 13,
        "screen": TerminalScreen(cells, 0, 0, True), "synced": True,
    }])
    jtt._refresh_mini_live(state, screen)
    assert row["mini_live_state"] == "synced"
    assert "mini_screen" in row
    jtt._refresh_mini_live(state, screen)
    assert "mini_screen" not in row
    assert "mini_live_state" not in row
    assert "terminal_activity" not in row
    assert renderer._status(row) == "↻ 셸 실행 중 · 화면 재동기화"


def test_mini_sampler_close_replaces_full_pending_queue_with_stop_sentinel():
    import queue
    import threading

    sampler = jtt._MiniLiveSampler.__new__(jtt._MiniLiveSampler)
    sampler._requests = queue.Queue(maxsize=1)
    sampler._requests.put_nowait((None, "pending", ()))
    sampler._results = queue.Queue(maxsize=1)
    sampler._lock = threading.Lock()
    sampler._busy = True
    sampler._stopping = False

    class JoinSpy:
        def __init__(self):
            self.timeout = None
        def join(self, timeout=None):
            self.timeout = timeout

    sampler._thread = JoinSpy()
    sampler.close()

    assert sampler._stopping is True
    assert sampler._requests.get_nowait() is None
    assert sampler._requests.empty()
    assert sampler._thread.timeout == 0.5


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
    assert renderer._handle_key(screen, state, "Q") is False
    assert renderer._handle_key(screen, state, "\x1b") is False
    assert renderer._handle_key(screen, state, "\t") is False
    assert registry.created == 0
    assert registry.clients["session-a"].sent == [b"n", b"q", b"Q", b"\x1b", b"\t"]
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


def _exited_ready_state(tmp_path, *identities):
    store = JTTWorkspaceStore(tmp_path / "jtt")
    registry = FakeRegistry(list(identities))
    store.set_terminal_ids(store.DEFAULT_ID, [item.session_id for item in identities])
    return jtt.build_state(store, registry), registry


def _select_pane(state, index):
    state["pane_index"] = index
    row = state["panes"][index]
    state["selected_target_id"] = str(row.get("target_id") or "")
    state["selected_signature"] = renderer._pane_signature(row)


def test_prefix_x_removes_only_selected_exited_card_reference_and_preserves_other_workspace(
    tmp_path, monkeypatch,
):
    state, registry = _exited_ready_state(
        tmp_path, _identity("session-a", 101, "gen-a", "exited"),
        _identity("session-b", 202, "gen-b", "exited"),
    )
    _select_pane(state, 1)
    other = state["jtt_store"].create_workspace("Other")
    state["jtt_store"].set_terminal_ids(other["id"], ["session-b"])
    captured = {}

    def confirm(_screen, title, items, **kwargs):
        captured.update(title=title, items=items, kwargs=kwargs)
        return SimpleNamespace(cancelled=False, selected_key="remove")

    monkeypatch.setattr(jtt, "run_list_picker", confirm)
    assert _prefix(FakeScreen(), state, "x") is False

    assert captured["title"] == "종료된 터미널 카드 제거"
    assert captured["items"][0] == ("cancel", "취소")
    assert captured["items"][1][0] == "remove"
    assert "카드 참조만 제거" in captured["kwargs"]["footer_hint"]
    workspaces = {item["id"]: item for item in state["jtt_store"].workspaces()}
    assert workspaces[state["jtt_store"].DEFAULT_ID]["terminal_ids"] == ["session-a"]
    assert workspaces[other["id"]]["terminal_ids"] == ["session-b"]
    assert {item.session_id for item in registry.list_sessions()} == {"session-a", "session-b"}
    assert registry.stop_calls == []
    assert registry.clients["session-b"].closed is True


def test_prefix_x_cancel_on_exited_card_keeps_workspace_and_session_unchanged(tmp_path, monkeypatch):
    state, registry = _exited_ready_state(
        tmp_path, _identity("session-a", 101, "gen-a", "exited"),
    )
    before = state["jtt_store"].workspaces()[0]["terminal_ids"]
    monkeypatch.setattr(
        jtt, "run_list_picker",
        lambda *_args, **_kwargs: SimpleNamespace(cancelled=False, selected_key="cancel"),
    )

    assert _prefix(FakeScreen(), state, "x") is False

    assert state["jtt_store"].workspaces()[0]["terminal_ids"] == before
    assert [item.session_id for item in registry.list_sessions()] == ["session-a"]
    assert registry.stop_calls == []
    assert not registry.clients["session-a"].closed


@pytest.mark.parametrize(
    "mutation", ["selection", "registry_generation", "registry_pid", "binding", "membership"],
)
def test_prefix_x_exited_card_revalidates_after_modal(tmp_path, monkeypatch, mutation):
    identities = [_identity("session-a", 101, "gen-a", "exited"),
                  _identity("session-b", 202, "gen-b", "exited")]
    state, registry = _exited_ready_state(tmp_path, *identities)
    original_set_ids = state["jtt_store"].set_terminal_ids
    store_writes_after_modal = []

    def mutate_then_confirm(_screen, _title, _items, **_kwargs):
        if mutation == "selection":
            _select_pane(state, 1)
        elif mutation == "registry_generation":
            registry.identities = (_identity("session-a", 303, "replacement", "exited"),
                                   identities[1])
        elif mutation == "registry_pid":
            registry.identities = (_identity("session-a", 303, "gen-a", "exited"),
                                   identities[1])
        elif mutation == "binding":
            replacement = _identity("session-a", 101, "gen-a", "exited")
            state["managed_terminals"]["session-a"] = jtt.ManagedTerminalBinding(
                FakeClient(replacement), replacement,
            )
        elif mutation == "membership":
            original_set_ids(state["workspace_id"], ["session-b"])
            monkeypatch.setattr(
                state["jtt_store"], "set_terminal_ids",
                lambda *_args, **_kwargs: store_writes_after_modal.append(True),
            )
        return SimpleNamespace(cancelled=False, selected_key="remove")

    monkeypatch.setattr(jtt, "run_list_picker", mutate_then_confirm)
    assert _prefix(FakeScreen(), state, "x") is False

    if mutation == "membership":
        assert store_writes_after_modal == []
        assert state["jtt_store"].workspaces()[0]["terminal_ids"] == ["session-b"]
    else:
        assert "변경되어 카드 제거를 차단" in state["jtt_notice"]
        assert state["jtt_store"].workspaces()[0]["terminal_ids"] == ["session-a", "session-b"]


def test_layout_menu_creates_and_renames_folder_and_window_without_starting_ptys(
        tmp_path, monkeypatch):
    identity = _identity("session-a", 101, "gen-a")
    store = JTTWorkspaceStore(tmp_path / "jtt")
    store.set_terminal_ids(store.DEFAULT_ID, [identity.session_id])
    registry = FakeRegistry([identity])
    state = jtt.build_state(store, registry)
    actions = iter(("create_folder", "rename_folder", "create_window", "rename_window"))
    names = iter(("Feature", "Product", "Checks", "Verify"))
    monkeypatch.setattr(
        jtt, "run_list_picker",
        lambda *_args, **_kwargs: SimpleNamespace(cancelled=False, selected_key=next(actions)),
    )
    monkeypatch.setattr(jtt, "prompt_text", lambda *_args, **_kwargs: next(names))

    for _ in range(4):
        jtt._layout_menu(FakeScreen(), state)

    workspace, layout, _ = jtt._workspace_layout(state)
    product = next(item for item in layout["folders"] if item["name"] == "Product")
    verify = next(item for item in layout["windows"] if item["name"] == "Verify")
    assert verify["folder_id"] == product["id"]
    assert state["selected_folder_id"] == product["id"]
    assert state["selected_window_id"] == verify["id"]
    assert workspace["terminal_ids"] == [identity.session_id]
    assert registry.created == 0 and registry.stop_calls == []
    assert registry.stop_calls == []


def test_layout_create_reconciles_committed_directory_fsync_failure(tmp_path, monkeypatch):
    identity = _identity("session-a", 101, "gen-a")
    store = JTTWorkspaceStore(tmp_path / "jtt")
    store.set_terminal_ids(store.DEFAULT_ID, [identity.session_id])
    registry = FakeRegistry([identity])
    state = jtt.build_state(store, registry)
    monkeypatch.setattr(
        jtt, "run_list_picker",
        lambda *_args, **_kwargs: SimpleNamespace(cancelled=False, selected_key="create_folder"),
    )
    monkeypatch.setattr(jtt, "prompt_text", lambda *_args, **_kwargs: "Committed folder")
    original_fsync = os.fsync

    def fail_directory_fsync(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError("injected directory fsync failure")
        return original_fsync(fd)

    monkeypatch.setattr(os, "fsync", fail_directory_fsync)
    jtt._layout_menu(FakeScreen(), state)

    workspace, layout, _ = jtt._workspace_layout(state)
    committed = next(item for item in layout["folders"]
                     if item["name"] == "Committed folder")
    assert state["selected_folder_id"] == committed["id"]
    assert "폴더 생성 반영됨" in state["jtt_notice"]
    assert "디렉터리 동기화 확인 필요" in state["jtt_notice"]
    assert workspace["terminal_ids"] == [identity.session_id]
    assert registry.created == 0 and registry.stop_calls == []


def test_layout_delete_reconciles_committed_directory_fsync_failure(tmp_path, monkeypatch):
    identity = _identity("session-a", 101, "gen-a")
    store = JTTWorkspaceStore(tmp_path / "jtt")
    store.set_terminal_ids(store.DEFAULT_ID, [identity.session_id])
    registry = FakeRegistry([identity])
    layouts = jtt.JTTLayoutStore(store.root)
    folder = layouts.create_folder(store.DEFAULT_ID, "Review")
    window = layouts.create_window(store.DEFAULT_ID, folder["id"], "Code")
    layouts.move_terminal(store.DEFAULT_ID, identity.session_id, window["id"],
                          [identity.session_id])
    state = jtt.build_state(store, registry)
    choice = next(item for item in state["choices"]
                  if item.get("kind") == "logical_window"
                  and item.get("window_id") == window["id"])
    jtt._attach_layout_choice(state, choice)
    workspace, layout, _ = jtt._workspace_layout(state)
    monkeypatch.setattr(
        jtt, "run_list_picker",
        lambda *_args, **_kwargs: SimpleNamespace(cancelled=False, selected_key="delete"),
    )
    original_fsync = os.fsync

    def fail_directory_fsync(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError("injected directory fsync failure")
        return original_fsync(fd)

    monkeypatch.setattr(os, "fsync", fail_directory_fsync)
    jtt._delete_layout_item(FakeScreen(), state, workspace, layout, "delete_window")

    recovered = state["jtt_layout_store"].layout(store.DEFAULT_ID, [identity.session_id])
    assert recovered["windows"][0]["terminal_ids"] == [identity.session_id]
    assert state["selected_window_id"] == "window-default"
    assert "Window 삭제 반영됨" in state["jtt_notice"]
    assert "디렉터리 동기화 확인 필요" in state["jtt_notice"]
    assert [row["managed_id"] for row in state["panes"]] == [identity.session_id]
    assert registry.stop_calls == []


@pytest.mark.parametrize(
    ("identity", "registry_override"),
    [
        (_identity("session-a", 101, "gen-a", "orphaned"), None),
        (_identity("session-a", 101, "gen-a", "stopping"), None),
        (_identity("session-a", 101, "gen-a", "exited", "parse_failed"), None),
        (_identity("session-a", 101, "gen-a", "running", "parse_failed"), None),
        (_identity("session-a", 101, "gen-a", "running"), "lookup_failure"),
        (_identity("session-a", 101, "gen-a", "running"), "missing"),
    ],
    ids=["orphaned", "stopping", "exited-renderer-error", "running-renderer-error",
         "lookup-failure", "missing-or-dead-supervisor"],
)
def test_prefix_x_never_offers_exited_card_cleanup_for_uncertain_states(
    tmp_path, monkeypatch, identity, registry_override,
):
    state, registry = _exited_ready_state(tmp_path, identity)
    if registry_override == "lookup_failure":
        registry.list_sessions = lambda: (_ for _ in ()).throw(RuntimeError("fixture"))
    elif registry_override == "missing":
        registry.identities = ()
    dialogs = []

    def confirm(_screen, title, items, **_kwargs):
        dialogs.append((title, items))
        return SimpleNamespace(cancelled=False, selected_key="cancel")

    monkeypatch.setattr(jtt, "run_list_picker", confirm)
    assert _prefix(FakeScreen(), state, "x") is False

    assert all(key != "remove" for _title, items in dialogs for key, _label in items)
    assert registry.stop_calls == []
    if identity.state in {"orphaned", "stopping", "exited"} or registry_override:
        assert dialogs == []
    else:
        # The existing running-session stop confirmation may remain, but it is
        # never presented as exited-card cleanup.
        assert len(dialogs) == 1
        assert dialogs[0][0] == "터미널 종료"


def test_jtt_navigation_q_uppercase_q_and_escape_do_not_disconnect(tmp_path):
    state, _registry = _stop_ready_state(tmp_path, _identity("session-a", 101, "gen-a"))
    screen = FakeScreen()

    for key in ("q", "Q", "\x1b"):
        assert renderer._handle_key(screen, state, key) is False
    assert state["jtt_prefix_pending"] is False


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
