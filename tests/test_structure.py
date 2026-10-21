"""tmux structure writes target ids. Names are labels. Focus stays put."""

import inspect

from tmux_agent_tower.control import actions
from tmux_agent_tower.server import httpapi
from tmux_agent_tower.tmux import structure
from tmux_agent_tower.ui.structure_menu import window_close_summary
from tmux_agent_tower.ui import tower as tower_module


class World:
    """A tiny tmux. ``current`` is the focused window and must not move."""

    def __init__(self):
        self.windows = {}
        self.names = {}
        self.layouts = {}
        self.titles = {}
        self.pids = {}
        self.current = ""
        self.calls = []
        self._w = 1
        self._p = 1
        self.fail_split = False

    def add(self, name="keep"):
        window_id = f"@{self._w}"
        pane_id = f"%{self._p}"
        self._w += 1
        self._p += 1
        self.windows[window_id] = [pane_id]
        self.names[window_id] = name
        self.layouts[window_id] = "single"
        self.pids[pane_id] = str(1000 + self._p - 1)
        if not self.current:
            self.current = window_id
        return window_id, pane_id

    def _result(self, stdout="", code=0):
        class Result:
            returncode = code
            stderr = ""

        result = Result()
        result.stdout = stdout
        return result

    def run(self, args):
        self.calls.append(list(args))
        cmd = args[0]
        if cmd == "list-windows" and "#{window_id}\t" in args[-1]:
            lines = [
                f"{wid}\t{index}\t{self.names[wid]}\t{len(panes)}"
                for index, (wid, panes) in enumerate(self.windows.items())
            ]
            return self._result("\n".join(lines))
        if cmd == "list-windows":
            return self._result("\n".join(self.windows))
        if cmd == "list-panes" and "-s" in args:
            if "-t" in args and args[args.index("-t") + 1] != "0":
                return self._result("", 1)
            panes = [pane for panes in self.windows.values() for pane in panes]
            if "\t" in args[-1]:
                return self._result("\n".join(
                    "\t".join((pane, self.pids[pane], "0", self.titles.get(pane, "")))
                    for pane in panes
                ))
            return self._result("\n".join(panes))
        if cmd == "list-panes":
            window_id = args[args.index("-t") + 1]
            return self._result("\n".join(self.windows.get(window_id, [])))
        if cmd == "display-message":
            target = args[args.index("-t") + 1]
            if target.startswith("%"):
                for wid, panes in self.windows.items():
                    if target in panes:
                        if "\t" in args[-1]:
                            return self._result("\t".join((target, self.pids[target], "0", self.titles.get(target, ""))))
                        return self._result(wid)
                return self._result("", 1)
            if target.startswith("@"):
                return self._result(target if target in self.windows else "", 0 if target in self.windows else 1)
            return self._result(self.current)
        if cmd == "new-window":
            assert "-d" in args
            window_id = f"@{self._w}"
            pane_id = f"%{self._p}"
            self._w += 1
            self._p += 1
            name = args[args.index("-n") + 1] if "-n" in args else "tmux"
            self.windows[window_id] = [pane_id]
            self.names[window_id] = name
            self.layouts[window_id] = "single"
            self.pids[pane_id] = str(1000 + self._p - 1)
            return self._result(f"{window_id}\t{pane_id}")
        if cmd == "split-window":
            assert "-d" in args
            window_id = args[args.index("-t") + 1]
            if self.fail_split or window_id not in self.windows:
                return self._result("", 1)
            pane_id = f"%{self._p}"
            self._p += 1
            self.windows[window_id].append(pane_id)
            self.pids[pane_id] = str(1000 + self._p - 1)
            return self._result(f"{window_id}\t{pane_id}")
        if cmd == "move-pane":
            assert "-d" in args
            pane_id = args[args.index("-s") + 1]
            window_id = args[args.index("-t") + 1]
            for wid, panes in list(self.windows.items()):
                if pane_id in panes:
                    panes.remove(pane_id)
                    if not panes:
                        del self.windows[wid]
                    break
            self.windows[window_id].append(pane_id)
            return self._result()
        if cmd == "break-pane":
            assert "-d" in args
            pane_id = args[args.index("-s") + 1]
            source = ""
            for wid, panes in list(self.windows.items()):
                if pane_id in panes:
                    source = wid
                    panes.remove(pane_id)
                    if not panes:
                        del self.windows[wid]
                    break
            window_id = f"@{self._w}"
            self._w += 1
            self.windows[window_id] = [pane_id]
            self.names[window_id] = "broken"
            self.layouts[window_id] = "single"
            assert window_id != source
            return self._result(f"{window_id}\t{pane_id}")
        if cmd == "select-layout":
            window_id = args[args.index("-t") + 1]
            self.layouts[window_id] = args[-1]
            return self._result()
        if cmd == "rename-window":
            window_id = args[args.index("-t") + 1]
            self.names[window_id] = args[-1]
            return self._result()
        if cmd == "select-pane":
            pane_id = args[args.index("-t") + 1]
            self.titles[pane_id] = args[args.index("-T") + 1]
            return self._result()
        if cmd == "kill-pane":
            assert "-a" not in args
            pane_id = args[args.index("-t") + 1]
            for wid, panes in list(self.windows.items()):
                if pane_id in panes:
                    panes.remove(pane_id)
                    if not panes:
                        del self.windows[wid]
            return self._result()
        if cmd == "kill-window":
            assert "-a" not in args
            window_id = args[args.index("-t") + 1]
            del self.windows[window_id]
            return self._result()
        raise AssertionError(args)


def _bind(monkeypatch):
    world = World()
    monkeypatch.setattr(structure, "_run", world.run)
    return world


def test_create_window_returns_the_exact_ids_and_keeps_focus(monkeypatch):
    world = _bind(monkeypatch)
    keep, _pane = world.add("keep")
    created = structure.create_window("0", name="alpha")
    assert created.ok
    assert created.window_id != keep
    assert created.window_id in world.windows
    assert world.windows[created.window_id] == [created.pane_id]
    assert world.current == keep
    assert world.calls[0][0] == "new-window"
    assert "-d" in world.calls[0]
    assert "-t" in world.calls[0]


def test_duplicate_window_names_stay_distinct(monkeypatch):
    world = _bind(monkeypatch)
    world.add("keep")
    first = structure.create_window("0", name="same")
    second = structure.create_window("0", name="same")
    assert first.window_id != second.window_id
    assert world.names[first.window_id] == world.names[second.window_id] == "same"
    structure.apply_layout("0", first.window_id, "tiled")
    assert world.layouts[first.window_id] == "tiled"
    assert world.layouts[second.window_id] == "single"


def test_create_pane_stays_in_that_window(monkeypatch):
    world = _bind(monkeypatch)
    keep, _pane = world.add("keep")
    other, _other_pane = world.add("other")
    horizontal = structure.create_pane(other, "horizontal")
    vertical = structure.create_pane(other, "vertical")
    assert horizontal.ok and horizontal.window_id == other
    assert vertical.ok and vertical.window_id == other
    assert keep not in [call[call.index("-t") + 1] for call in world.calls if call[0] == "split-window"]
    split = [call for call in world.calls if call[0] == "split-window"]
    assert "-h" in split[0] and "-v" in split[1]
    assert all("-d" in call for call in split)
    assert world.windows[keep] == [_pane]
    assert world.current == keep


def test_split_rejects_a_window_name(monkeypatch):
    _bind(monkeypatch)
    assert structure.create_pane("agents", "horizontal").detail == "bad_window"


def test_move_pane_uses_source_pane_and_target_window(monkeypatch):
    world = _bind(monkeypatch)
    keep, tower_pane = world.add("keep")
    left, left_pane = world.add("left")
    structure.create_pane(left, "auto")
    right, _right_pane = world.add("right")
    extra = [pane for pane in world.windows[left] if pane != left_pane][0]
    moved = structure.move_pane("0", extra, right)
    assert moved.ok and moved.pane_id == extra and moved.window_id == right
    assert extra in world.windows[right]
    assert extra not in world.windows[left]
    call = next(call for call in world.calls if call[0] == "move-pane")
    assert call[call.index("-s") + 1] == extra
    assert call[call.index("-t") + 1] == right
    assert "-d" in call
    assert world.windows[keep] == [tower_pane]
    assert world.current == keep


def test_break_pane_returns_a_new_window_and_the_same_pane(monkeypatch):
    world = _bind(monkeypatch)
    world.add("keep")
    window_id, pane_id = world.add("work")
    sibling = structure.create_pane(window_id, "auto")
    broken = structure.break_pane("0", sibling.pane_id, name="off")
    call = next(call for call in world.calls if call[0] == "break-pane")
    assert call[call.index("-t") + 1] == "0:"
    assert call[call.index("-s") + 1] == sibling.pane_id
    assert broken.ok
    assert broken.pane_id == sibling.pane_id
    assert broken.window_id != window_id
    assert broken.pane_id in world.windows[broken.window_id]
    assert world.current == "@1"


def test_layout_touches_only_the_named_window(monkeypatch):
    world = _bind(monkeypatch)
    keep, _pane = world.add("keep")
    other, _other = world.add("other")
    before = world.layouts[keep]
    assert structure.apply_layout("0", other, "even-vertical").ok
    assert world.layouts[other] == "even-vertical"
    assert world.layouts[keep] == before
    call = next(call for call in world.calls if call[0] == "select-layout")
    assert call[call.index("-t") + 1] == other
    assert "keep" not in call


def test_rename_window_and_pane_keep_ids(monkeypatch):
    world = _bind(monkeypatch)
    window_id, pane_id = world.add("old")
    assert structure.rename_window("0", window_id, "new name").ok
    assert world.names[window_id] == "new name"
    assert window_id in world.windows
    assert structure.rename_pane("0", pane_id, "title").ok
    assert world.titles[pane_id] == "title"
    title_call = next(call for call in world.calls if call[0] == "select-pane")
    assert "-d" not in title_call
    assert title_call[title_call.index("-t") + 1] == pane_id


def test_pane_title_rename_checks_exact_session_and_process_before_writing(monkeypatch):
    world = _bind(monkeypatch)
    _window_id, target = world.add("target")
    _other_window, other = world.add("other")

    assert structure.rename_pane("0", target, "Task name", expected_pane_pid=world.pids[target]).ok
    assert world.titles[target] == "Task name"
    assert world.titles.get(other, "") == ""

    before = len([call for call in world.calls if call[0] == "select-pane"])
    stale = structure.rename_pane("0", target, "Wrong process", expected_pane_pid="999999")
    wrong_session = structure.rename_pane("other-session", target, "Wrong session", expected_pane_pid=world.pids[target])

    assert stale.detail == "stale"
    assert wrong_session.detail == "not_found"
    assert len([call for call in world.calls if call[0] == "select-pane"]) == before
    assert world.titles[target] == "Task name"
    assert world.titles.get(other, "") == ""


def test_close_exact_pane_and_protect_tower(monkeypatch):
    world = _bind(monkeypatch)
    window_id, tower_pane = world.add("tower")
    other = structure.create_pane(window_id, "auto")
    assert structure.kill_pane("0", tower_pane, own_pane_id=tower_pane).detail == "tower_pane"
    assert tower_pane in world.windows[window_id]
    assert structure.kill_pane("0", other.pane_id, own_pane_id=tower_pane).ok
    assert other.pane_id not in world.windows[window_id]
    assert tower_pane in world.windows[window_id]
    killed = [call for call in world.calls if call[0] == "kill-pane"]
    assert killed == [["kill-pane", "-t", other.pane_id]]


def test_close_window_confirmation_blocks_tower_and_lists_panes():
    assert structure.window_close_block(["%1", "%2"], "%1", 3) == "tower_pane"
    assert structure.window_close_block(["%2"], "", 1) == "last_window"
    assert structure.window_close_block(["%2"], "%1", 3) is None
    lines = window_close_summary(
        "agents",
        [
            {"pane_id": "%8", "project": "SamplePortal", "agent": "Codex", "status": "WORKING", "attention": "none"},
            {"pane_id": "%9", "project": "JuRadar", "agent": "Claude", "status": "IDLE", "attention": "none"},
            {"pane_id": "%10", "project": "Agent-Relay", "agent": "OpenCode", "status": "IDLE", "attention": "approval_required"},
        ],
    )
    text = "\n".join(lines)
    assert "agents" in text and "@4" not in text
    assert "● SamplePortal / Codex" in text and "%8" not in text
    assert "○ JuRadar / Claude" in text
    assert "! Agent-Relay / OpenCode" in text
    assert "승인 필요" in text or "approval" in text.lower()


def test_close_window_leaves_other_windows(monkeypatch):
    world = _bind(monkeypatch)
    keep, _pane = world.add("keep")
    other, _other = world.add("other")
    assert structure.kill_window("0", other, own_pane_id="%nope").ok
    assert other not in world.windows
    assert keep in world.windows
    assert world.current == keep


def test_close_window_refuses_tower_and_the_last_window(monkeypatch):
    world = _bind(monkeypatch)
    only, pane = world.add("only")
    assert structure.kill_window("0", only, own_pane_id=pane).detail == "tower_pane"
    world2 = _bind(monkeypatch)
    alone, _pane = world2.add("alone")
    assert structure.kill_window("0", alone, own_pane_id="").detail == "last_window"
    assert alone in world2.windows


def test_commands_never_delete_a_session_or_pass_a_shell():
    source = inspect.getsource(structure)
    assert "kill-session" not in source
    assert "shell=True" not in source
    assert '"-a"' not in source


def test_phone_does_not_grow_structure_writes():
    post = inspect.getsource(httpapi.TowerRemoteHandler.do_POST)
    payload = inspect.getsource(httpapi.build_status_payload)
    assert "window_id" in payload
    for forbidden in ("new-window", "split-window", "kill-window", "kill-pane", "move-pane", "break-pane"):
        assert forbidden not in post


def test_mutating_commands_do_not_select_a_client(monkeypatch):
    world = _bind(monkeypatch)
    world.add("keep")
    created = structure.create_window("0", name="a")
    structure.create_pane(created.window_id, "horizontal")
    structure.apply_layout("0", created.window_id, "main-horizontal")
    assert not any(call[0] in ("select-window", "switch-client") for call in world.calls)
    assert world.current == "@1"


def test_zero_state_offers_create_actions(tmp_path, monkeypatch):
    monkeypatch.setattr(tower_module, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(tower_module, "CONFIG_DIR", tmp_path / "config")
    monkeypatch.setattr(tower_module, "HOST_FILE", tmp_path / "config" / "host")
    monkeypatch.setattr(tower_module, "REMOTE_HOSTS_FILE", tmp_path / "config" / "remote-hosts.txt")
    tower = tower_module.Tower("0")
    tower.rows = []
    tower._apply_filter()
    assert [row["action"] for row in tower.visible_rows] == ["task", "saved", "template"]
    assert actions.enter_intent(tower.visible_rows[0]) == "zero"
    tower.rows = [
        {
            "kind": "pane",
            "key": "%2",
            "pane_id": "%2",
            "remote": False,
            "host": "pc",
            "project": "demo",
            "agent": "Shell",
            "status": "IDLE",
        }
    ]
    tower._apply_filter()
    assert all(row.get("kind") != "zero" for row in tower.visible_rows)


def test_enter_on_a_window_does_not_focus(monkeypatch):
    calls = []
    monkeypatch.setattr(actions, "focus_local_pane", lambda *args, **kwargs: calls.append(args) or True)
    row = {"kind": "window", "key": "@3", "window_id": "@3", "remote": False}
    assert actions.enter_intent(row) == "window"
    assert calls == []
