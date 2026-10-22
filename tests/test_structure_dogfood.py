"""Empty session to a multi-window workspace, using Tower's structure API only.

The harness creates the isolated session. Every later step is a function
the TUI calls. No window name is used as a write target.
"""

import os
import shutil

import pytest

from tmux_agent_tower.state.bindings import ProjectBindingStore
from tmux_agent_tower.state.overrides import OverrideStore
from tmux_agent_tower.tmux import structure
from tmux_test_safety import run_isolated_tmux
from workspace_fixture import make_three_level_git_workspace


def _tmux(*args):
    return run_isolated_tmux(*args)


def _have_tmux():
    return shutil.which("tmux") is not None


@pytest.mark.skipif(not _have_tmux(), reason="tmux is not installed")
def test_empty_to_working_without_manual_tmux_commands(tmp_path, tmux_test_safety):
    session = f"tower-dogfood-{os.getpid()}"
    created = _tmux("new-session", "-d", "-s", session, "-n", "tower")
    assert created.returncode == 0, created.stderr
    try:
        tower_window = structure.current_window_id(session)
        tower_panes = structure.panes_of_window(tower_window)
        assert tower_window and len(tower_panes) == 1
        tower_pane = tower_panes[0]
        keep_layout = _tmux("display-message", "-p", "-t", tower_window, "#{window_layout}").stdout.strip()

        # 1. New window. The client (session current window) stays on Tower.
        first = structure.create_window(session, name="work", cwd="/tmp")
        assert first.ok and first.window_id != tower_window
        assert structure.current_window_id(session) == tower_window

        # 2. Add a pane in that window only.
        added = structure.create_pane(first.window_id, "horizontal", cwd="/tmp")
        assert added.ok and added.window_id == first.window_id
        assert structure.current_window_id(session) == tower_window

        # 3. Second project pane, recorded like the launcher.
        second = structure.create_window(session, name="proj2", cwd="/tmp")
        assert second.ok and second.window_id not in (tower_window, first.window_id)
        structure.rename_pane(session, second.pane_id, "Cursor - proj2")
        pid = _tmux("display-message", "-p", "-t", added.pane_id, "#{pane_pid}").stdout.strip()
        bindings = ProjectBindingStore(tmp_path / "bindings.json")
        overrides = OverrideStore(tmp_path / "overrides.json")
        bindings.record(added.pane_id, session, pid, "/tmp", "proj2", "Cursor")
        overrides.set_agent(added.pane_id, "Cursor", session, pid)

        # 4. Layout of the work window only.
        assert structure.apply_layout(session, first.window_id, "even-horizontal").ok
        assert _tmux("display-message", "-p", "-t", tower_window, "#{window_layout}").stdout.strip() == keep_layout
        assert structure.current_window_id(session) == tower_window

        # 5. Move the added pane onto the second window.
        moved = structure.move_pane(session, added.pane_id, second.window_id)
        assert moved.ok and moved.pane_id == added.pane_id
        assert structure.pane_window_id(added.pane_id) == second.window_id
        assert bindings.usable(added.pane_id, session, pid)["project_name"] == "proj2"
        assert overrides.get_agent(added.pane_id, session, pid) == "Cursor"

        # 6. Break that moved pane into a new window. The pane id stays.
        broken = structure.break_pane(session, added.pane_id, name="split-off")
        assert broken.ok
        assert broken.pane_id == added.pane_id
        assert broken.window_id not in (tower_window, first.window_id, second.window_id)
        assert bindings.usable(added.pane_id, session, pid)["agent"] == "Cursor"
        assert overrides.get_agent(added.pane_id, session, pid) == "Cursor"
        assert structure.current_window_id(session) == tower_window

        # 7. Rename. Identity stays the window id.
        assert structure.rename_window(session, broken.window_id, "renamed").ok
        assert structure.rename_pane(session, broken.pane_id, "renamed-pane").ok
        listed = {item["window_id"]: item["window_name"] for item in structure.list_windows(session)}
        assert listed[broken.window_id] == "renamed"
        title = _tmux("display-message", "-p", "-t", broken.pane_id, "#{pane_title}").stdout.strip()
        assert title == "renamed-pane"

        # 8. Live read of that pane, the same capture the control view uses.
        screen = _tmux("capture-pane", "-p", "-t", broken.pane_id, "-S", "-5")
        assert screen.returncode == 0

        # Duplicate names do not retile each other.
        same_a = structure.create_window(session, name="same")
        same_b = structure.create_window(session, name="same")
        assert same_a.window_id != same_b.window_id
        layout_b = _tmux("display-message", "-p", "-t", same_b.window_id, "#{window_layout}").stdout.strip()
        assert structure.apply_layout(session, same_a.window_id, "tiled").ok
        assert _tmux("display-message", "-p", "-t", same_b.window_id, "#{window_layout}").stdout.strip() == layout_b

        # 9. Close the test pane. Tower's pane is refused.
        assert structure.kill_pane(session, tower_pane, own_pane_id=tower_pane).detail == "tower_pane"
        assert structure.kill_pane(session, broken.pane_id, own_pane_id=tower_pane).ok
        assert broken.pane_id not in structure.pane_ids(session)
        assert tower_pane in structure.pane_ids(session)

        # 10. Close a test window. The window that holds Tower is refused.
        assert structure.kill_window(session, tower_window, own_pane_id=tower_pane).detail == "tower_pane"
        assert structure.kill_window(session, first.window_id, own_pane_id=tower_pane).ok
        assert first.window_id not in structure.window_ids(session)
        assert tower_window in structure.window_ids(session)
        assert structure.current_window_id(session) == tower_window
        assert _tmux("display-message", "-p", "-t", tower_window, "#{window_layout}").stdout.strip() == keep_layout
    finally:
        _tmux("kill-session", "-t", session)


def _origin(pane_id: str):
    text = _tmux("display-message", "-p", "-t", pane_id, "#{pane_left} #{pane_top}").stdout.split()
    return int(text[0]), int(text[1])


def _select_named(tree, name: str) -> None:
    for index, row in enumerate(tree.visible_rows()):
        if row.node.name == name:
            tree.selected = index
            return
    raise AssertionError(name)


@pytest.mark.skipif(not _have_tmux(), reason="tmux is not installed")
def test_tree_choice_builds_and_rearranges_a_workspace(tmp_path, tmux_test_safety):
    """The empty-session path: tree, shell, split, move, break, rename, close.

    The harness only creates the isolated session. Later writes go through
    the launcher and structure helpers. Session 0 is not a target.
    """

    from tmux_agent_tower.launcher.browse import ProjectEntry, list_local_children
    from tmux_agent_tower.launcher.spawn import SpawnTarget, _finish_new_pane, spawn_into_window
    from tmux_agent_tower.launcher.treeview import FolderTree

    root_path, group, repo_path = make_three_level_git_workspace(tmp_path)
    repo = str(repo_path)
    root = str(root_path)
    session = f"tower-struct-{os.getpid()}"
    created = _tmux("new-session", "-d", "-s", session, "-n", "tower")
    assert created.returncode == 0, created.stderr
    try:
        tower_window = structure.current_window_id(session)
        tower_pane = structure.panes_of_window(tower_window)[0]
        tower_layout = _tmux("display-message", "-p", "-t", tower_window, "#{window_layout}").stdout.strip()

        # 1. Empty window. The client stays on Tower.
        work = structure.create_window(session, name="work")
        assert work.ok and work.window_id != tower_window
        assert structure.current_window_id(session) == tower_window

        # 2. Tree, three opens, then the repo itself.
        tree = FolderTree("workstation-a", ProjectEntry(root_path.name, root, False))

        def _open(path: str) -> None:
            page = list_local_children(path, include_heavy=True)
            assert page.ok
            tree.apply_children(path, page.children)

        _open(root)
        _select_named(tree, "group")
        assert tree.expand_action() == "fetch"
        _open(tree.selected_node().path)
        _select_named(tree, "sample-repo")
        assert tree.expand_action() == "fetch"
        _open(tree.selected_node().path)
        chosen = tree.as_entry()
        assert chosen.path == repo and chosen.is_git
        assert len(tree.loaded_paths()) >= 3

        # 3. Start a shell in the window created above. Not a second window.
        bindings = ProjectBindingStore(tmp_path / "bindings.json")
        overrides = OverrideStore(tmp_path / "overrides.json")
        agents = {"Shell": "bash"}
        first = SpawnTarget(chosen.path, chosen.name, "Shell")
        started = _finish_new_pane(session, first, "bash", work.pane_id, agents, bindings, overrides)
        assert started.ok
        pid = _tmux("display-message", "-p", "-t", work.pane_id, "#{pane_pid}").stdout.strip()
        overrides.set_agent(work.pane_id, "Shell", session, pid)
        assert bindings.usable(work.pane_id, session, pid)["project_path"] == repo

        # 4-6. Second project in the same window, left/right.
        second = SpawnTarget(str(group), group.name, "Shell")
        added = spawn_into_window(
            session, work.window_id, second, agents, bindings, overrides, direction="horizontal",
        )
        assert added.ok
        siblings = [pane for pane in structure.panes_of_window(work.window_id) if pane != work.pane_id]
        assert len(siblings) == 1
        side = siblings[0]
        left_a, top_a = _origin(work.pane_id)
        left_b, top_b = _origin(side)
        assert top_a == top_b and left_a != left_b

        # Up/down is the other axis, and it stays in this window.
        stacked = structure.create_pane(work.window_id, "vertical")
        assert stacked.ok and stacked.window_id == work.window_id
        left_c, top_c = _origin(stacked.pane_id)
        assert left_c == left_a and top_c != top_a

        # 7. Layout of this window only.
        assert structure.apply_layout(session, work.window_id, "even-horizontal").ok
        assert structure.apply_layout(session, work.window_id, "main-vertical").ok
        assert _tmux("display-message", "-p", "-t", tower_window, "#{window_layout}").stdout.strip() == tower_layout
        assert structure.current_window_id(session) == tower_window

        # 8-9. New window, then move one pane onto its id.
        dest = structure.create_window(session, name="dest")
        assert dest.ok
        moved = structure.move_pane(session, side, dest.window_id)
        assert moved.ok and moved.pane_id == side
        assert structure.pane_window_id(side) == dest.window_id
        assert bindings.usable(side, session, _tmux("display-message", "-p", "-t", side, "#{pane_pid}").stdout.strip())["project_name"] == group.name

        # 10. Break a pane that still has a sibling. The pane id stays.
        broken = structure.break_pane(session, work.pane_id, name="split-off")
        assert broken.ok and broken.pane_id == work.pane_id
        assert broken.window_id not in (tower_window, work.window_id, dest.window_id)
        assert bindings.usable(work.pane_id, session, pid)["agent"] == "Shell"
        assert overrides.get_agent(work.pane_id, session, pid) == "Shell"

        # 11-13. Rename by id, then read the screen.
        assert structure.rename_window(session, broken.window_id, "renamed").ok
        assert structure.rename_pane(session, broken.pane_id, "renamed-pane").ok
        names = {item["window_id"]: item["window_name"] for item in structure.list_windows(session)}
        assert names[broken.window_id] == "renamed"
        assert _tmux("display-message", "-p", "-t", broken.pane_id, "#{pane_title}").stdout.strip() == "renamed-pane"
        assert _tmux("capture-pane", "-p", "-t", broken.pane_id).returncode == 0

        # 14-15. Close that pane, then a window that is not Tower's.
        assert structure.kill_pane(session, tower_pane, own_pane_id=tower_pane).detail == "tower_pane"
        assert structure.kill_pane(session, broken.pane_id, own_pane_id=tower_pane).ok
        assert broken.pane_id not in structure.pane_ids(session)
        assert structure.kill_window(session, tower_window, own_pane_id=tower_pane).detail == "tower_pane"
        assert structure.kill_window(session, dest.window_id, own_pane_id=tower_pane).ok
        assert dest.window_id not in structure.window_ids(session)
        assert tower_window in structure.window_ids(session)
        assert tower_pane in structure.pane_ids(session)
        assert structure.current_window_id(session) == tower_window
    finally:
        _tmux("kill-session", "-t", session)
