"""Local workspace browse/create dogfood on explicit test-owned tmux sockets."""

import os
import shutil
import stat
import subprocess
import time
from pathlib import Path

import pytest

from tmux_agent_tower.launcher.browse import (
    list_local_children,
    validate_local_path,
)
from tmux_agent_tower.launcher.config import default_agent_commands
from tmux_agent_tower.launcher.discovery import ProjectEntry, find_git_projects
from tmux_agent_tower.launcher.treeview import FolderTree
from tmux_agent_tower.state.bindings import ProjectBindingStore
from tmux_agent_tower.ui.workspace_browser import WorkspacePick, launch_workspaces
from tmux_test_safety import run_isolated_tmux
from workspace_fixture import make_three_level_git_workspace


def _tmux(*args, timeout=5.0):
    return run_isolated_tmux(*args, timeout=timeout)


def _private_tmux_socket(socket_root: Path) -> Path:
    socket_path = socket_root / "server.sock"
    assert socket_path.exists(), socket_path
    assert stat.S_ISSOCK(socket_path.lstat().st_mode), socket_path
    assert socket_path.stat().st_uid == os.geteuid(), socket_path
    return socket_path


def _wait_for_pane_path(
    pane_id: str,
    target_path: str,
    *,
    timeout: float = 2.0,
    clock=time.monotonic,
) -> float:
    """Verify the requested start path, then wait for the shell's live cwd.

    tmux reports ``pane_start_path`` before the child shell has necessarily
    published its current directory. Poll the live value to a bounded deadline
    instead of treating that short startup window as a failed spawn.
    """
    started = clock()
    deadline = started + timeout
    try:
        start = _tmux(
            "display-message", "-p", "-t", pane_id, "#{pane_start_path}", timeout=timeout
        )
    except subprocess.TimeoutExpired:
        pytest.fail(
            f"timed out reading tmux pane_start_path: target={target_path!r}; "
            f"last_actual_path=<unavailable>; elapsed={clock() - started:.3f}s; "
            f"timeout={timeout:.3f}s"
        )
    if start.returncode != 0:
        pytest.fail(
            f"cannot read tmux pane_start_path: target={target_path!r}; "
            f"last_actual_path=<unavailable>; elapsed={time.monotonic() - started:.3f}s; "
            f"tmux_error={start.stderr.strip()!r}"
        )
    pane_start_path = start.stdout.strip()
    if pane_start_path != target_path:
        remaining = max(deadline - clock(), 0.001)
        try:
            current = _tmux(
                "display-message", "-p", "-t", pane_id, "#{pane_current_path}",
                timeout=remaining,
            )
            last_actual_path = current.stdout.strip() if current.returncode == 0 else "<unavailable>"
        except subprocess.TimeoutExpired:
            last_actual_path = "<unavailable>"
        pytest.fail(
            f"tmux pane start path mismatch: target={target_path!r}; "
            f"pane_start_path={pane_start_path!r}; last_actual_path={last_actual_path!r}; "
            f"elapsed={clock() - started:.3f}s"
        )

    last_actual_path = "<not-read>"
    while True:
        remaining = deadline - clock()
        if remaining <= 0:
            elapsed = clock() - started
            pytest.fail(
                f"tmux pane current path did not settle: target={target_path!r}; "
                f"pane_start_path={pane_start_path!r}; last_actual_path={last_actual_path!r}; "
                f"elapsed={elapsed:.3f}s; timeout={timeout:.3f}s"
            )
        try:
            current = _tmux(
                "display-message", "-p", "-t", pane_id, "#{pane_current_path}",
                timeout=remaining,
            )
        except subprocess.TimeoutExpired:
            elapsed = clock() - started
            pytest.fail(
                f"tmux pane current path query timed out: target={target_path!r}; "
                f"pane_start_path={pane_start_path!r}; last_actual_path={last_actual_path!r}; "
                f"elapsed={elapsed:.3f}s; timeout={timeout:.3f}s"
            )
        elapsed = clock() - started
        if current.returncode == 0:
            last_actual_path = current.stdout.strip()
            if last_actual_path == target_path:
                return elapsed
        else:
            last_actual_path = "<unavailable>"
        if elapsed >= timeout:
            pytest.fail(
                f"tmux pane current path did not settle: target={target_path!r}; "
                f"pane_start_path={pane_start_path!r}; last_actual_path={last_actual_path!r}; "
                f"elapsed={elapsed:.3f}s; timeout={timeout:.3f}s"
            )


def test_wait_for_pane_path_rechecks_live_path_until_it_matches(monkeypatch):
    replies = iter(("/target\n", "/old\n", "/target\n"))
    monkeypatch.setattr(
        __import__(__name__, fromlist=["_tmux"]),
        "_tmux",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            ["tmux"], 0, next(replies), ""
        ),
    )
    ticks = iter((1.0, 1.01, 1.02, 1.03, 1.04))

    elapsed = _wait_for_pane_path("%1", "/target", timeout=1.0, clock=lambda: next(ticks))

    assert elapsed == pytest.approx(0.04)
    assert list(replies) == []


def test_wait_for_pane_path_timeout_reports_target_last_path_and_elapsed(monkeypatch):
    replies = iter(("/target\n", "/still-old\n"))
    monkeypatch.setattr(
        __import__(__name__, fromlist=["_tmux"]),
        "_tmux",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            ["tmux"], 0, next(replies), ""
        ),
    )
    ticks = iter((3.0, 3.1, 3.6))

    try:
        _wait_for_pane_path("%2", "/target", timeout=0.5, clock=lambda: next(ticks))
    except BaseException as error:
        message = str(error)
    else:
        pytest.fail("expected the bounded pane-path poll to time out")

    assert "target='/target'" in message
    assert "pane_start_path='/target'" in message
    assert "last_actual_path='/still-old'" in message
    assert "elapsed=0.600s" in message
    assert "timeout=0.500s" in message


def _have_tmux():
    return shutil.which("tmux") is not None

@pytest.mark.skipif(not _have_tmux(), reason="tmux is not installed")
def test_local_tree_and_direct_path_create_in_an_isolated_session(tmp_path, tmux_test_safety):
    _tree_root, group, repo = make_three_level_git_workspace(tmp_path)
    parent = list_local_children(str(group))
    match = [child for child in parent.children if child.path == str(repo)]
    assert match and match[0].is_git and match[0].name == repo.name

    plain = tmp_path / "not-in-the-discovery-list"
    plain.mkdir()
    direct = validate_local_path(str(plain))
    assert direct.ok and direct.entry.is_git is False
    discovered = find_git_projects([str(group)])
    assert discovered
    assert all(entry.path != direct.entry.path for entry in discovered)

    isolated_tmux = tmux_test_safety.root
    assert "TMUX" not in os.environ and "TMUX_PANE" not in os.environ
    assert os.environ["TMUX_TMPDIR"] == str(isolated_tmux)
    assert not (isolated_tmux / "server.sock").exists()
    session = f"tower-browser-{os.getpid()}"
    created = _tmux("new-session", "-d", "-s", session, "-n", "tower")
    assert created.returncode == 0, created.stderr
    try:
        socket_path = _private_tmux_socket(isolated_tmux)
        tower_window = _tmux("display-message", "-p", "-t", session, "#{window_id}").stdout.strip()
        results = launch_workspaces(
            session=session,
            state_dir=tmp_path / "state",
            picks=[WorkspacePick("workstation-a", "workstation-a", False, match[0])],
            agent="Shell",
            placement="new",
            layout_choice="auto",
            agents_cfg=default_agent_commands(),
        )
        assert results[0].ok
        assert _private_tmux_socket(isolated_tmux) == socket_path
        windows = _tmux(
            "list-panes", "-s", "-t", session,
            "-F", "#{window_id}\t#{pane_id}",
        ).stdout
        new_panes = [
            line.split("\t")[1] for line in windows.splitlines()
            if line.split("\t")[0] != tower_window
        ]
        assert new_panes
        pane_id = new_panes[0]
        _wait_for_pane_path(pane_id, str(repo))
        pid = _tmux("display-message", "-p", "-t", pane_id, "#{pane_pid}").stdout.strip()
        bound = ProjectBindingStore(tmp_path / "state" / "project-bindings.json").usable(pane_id, session, pid)
        assert bound["project_path"] == str(repo)
        assert bound["agent"] == "Shell"
        assert bound["project_name"] == repo.name

        panes_before_direct = set(
            _tmux("list-panes", "-s", "-t", session, "-F", "#{pane_id}").stdout.splitlines()
        )
        direct_result = launch_workspaces(
            session=session,
            state_dir=tmp_path / "state",
            picks=[WorkspacePick("workstation-a", "workstation-a", False, direct.entry)],
            agent="Shell",
            placement="new",
            layout_choice="horizontal",
            agents_cfg=default_agent_commands(),
        )
        assert direct_result[0].ok
        panes_after_direct = set(
            _tmux("list-panes", "-s", "-t", session, "-F", "#{pane_id}").stdout.splitlines()
        )
        new_direct_panes = panes_after_direct - panes_before_direct
        assert len(new_direct_panes) == 1
        _wait_for_pane_path(new_direct_panes.pop(), direct.entry.path)
    finally:
        killed = _tmux("kill-session", "-t", session)
        assert killed.returncode == 0, killed.stderr


def _expand(tree: FolderTree, path: str):
    page = list_local_children(path, include_heavy=True)
    assert page.ok, page.error
    tree.apply_children(path, page.children)
    return page


def _select(tree: FolderTree, name: str):
    rows = tree.visible_rows()
    for index, row in enumerate(rows):
        if row.node.name == name:
            tree.selected = index
            return row.node
    raise AssertionError(name)


@pytest.mark.skipif(not _have_tmux(), reason="tmux is not installed")
def test_local_tree_three_levels_then_new_window(tmp_path, tmux_test_safety):
    tree_root, _group, _repo = make_three_level_git_workspace(tmp_path)
    tree = FolderTree("workstation-a", ProjectEntry(tree_root.name, str(tree_root), False))
    _expand(tree, tree.root.path)
    _select(tree, "group")
    _expand(tree, tree.selected_node().path)
    _select(tree, "sample-repo")
    _expand(tree, tree.selected_node().path)
    assert len(tree.loaded_paths()) >= 3
    chosen = tree.as_entry()
    assert chosen.is_git and chosen.name == "sample-repo"
    assert chosen.path == str(_repo)
    assert "group" in [row.node.name for row in tree.visible_rows()]

    assert "TMUX" not in os.environ and "TMUX_PANE" not in os.environ
    isolated_tmux = tmux_test_safety.root
    assert os.environ["TMUX_TMPDIR"] == str(isolated_tmux)
    assert not (isolated_tmux / "server.sock").exists()
    session = f"tower-tree-{os.getpid()}"
    created = _tmux("new-session", "-d", "-s", session, "-n", "tower")
    assert created.returncode == 0, created.stderr
    try:
        socket_path = _private_tmux_socket(isolated_tmux)
        starter = set(_tmux("list-windows", "-t", session, "-F", "#{window_id}").stdout.split())
        results = launch_workspaces(
            session=session,
            state_dir=tmp_path / "state",
            picks=[WorkspacePick("workstation-a", "workstation-a", False, chosen)],
            agent="Shell",
            placement="new",
            layout_choice="auto",
            agents_cfg=default_agent_commands(),
        )
        assert results[0].ok
        assert _private_tmux_socket(isolated_tmux) == socket_path
        listed = _tmux("list-panes", "-s", "-t", session, "-F", "#{window_id}\t#{pane_id}").stdout
        new_panes = [
            line.split("\t")[1] for line in listed.splitlines()
            if line.split("\t")[0] not in starter
        ]
        assert new_panes
        pane_id = new_panes[0]
        _wait_for_pane_path(pane_id, chosen.path)
        pid = _tmux("display-message", "-p", "-t", pane_id, "#{pane_pid}").stdout.strip()
        bound = ProjectBindingStore(tmp_path / "state" / "project-bindings.json").usable(pane_id, session, pid)
        assert bound["project_path"] == chosen.path
        assert bound["agent"] == "Shell"
    finally:
        killed = _tmux("kill-session", "-t", session)
        assert killed.returncode == 0, killed.stderr
