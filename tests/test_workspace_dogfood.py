"""Live browse and create. Remote checks require an explicit opt-in host.

Browse is read-only. A remote create adds one window and then removes only
that window.
"""

import os
import re
import shutil
import stat
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

from tmux_agent_tower.launcher.browse import (
    collect_remote_catalog,
    list_local_children,
    list_remote_children,
    remote_list_script,
    remote_start_roots,
    script_is_read_only,
    validate_local_path,
    validate_remote_path,
)
from tmux_agent_tower.launcher.config import default_agent_commands
from tmux_agent_tower.launcher.discovery import ProjectEntry, find_git_projects
from tmux_agent_tower.launcher.spawn import SpawnTarget
from tmux_agent_tower.launcher.treeview import FolderTree
from tmux_agent_tower.state.bindings import ProjectBindingStore
from tmux_agent_tower.ui.workspace_browser import WorkspacePick, launch_workspaces
from workspace_fixture import make_three_level_git_workspace


def _tmux(*args, timeout=5.0):
    return subprocess.run(
        ["tmux", *args], capture_output=True, text=True, check=False,
        env=os.environ.copy(), timeout=timeout,
    )


def _private_tmux_socket(socket_root: Path) -> Path:
    sockets = list(socket_root.glob("tmux-*/default"))
    assert len(sockets) == 1, sockets
    assert stat.S_ISSOCK(sockets[0].stat().st_mode), sockets[0]
    return sockets[0]


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


@pytest.fixture
def isolated_tmux(monkeypatch, request):
    socket_root = Path(tempfile.mkdtemp(prefix="tower-tmux-", dir="/tmp"))

    def cleanup():
        sockets = list(socket_root.glob("tmux-*/default"))
        assert len(sockets) <= 1, sockets
        if sockets:
            assert stat.S_ISSOCK(sockets[0].stat().st_mode), sockets[0]
            stopped = subprocess.run(
                ["tmux", "-S", str(sockets[0]), "kill-server"],
                capture_output=True, text=True, check=False, env=os.environ.copy(),
            )
            if stopped.returncode != 0:
                assert stopped.stderr == f"no server running on {sockets[0]}\n", stopped.stderr
                sockets[0].unlink()
        assert not list(socket_root.glob("tmux-*/default")), "isolated tmux socket still exists"
        shutil.rmtree(socket_root)

    request.addfinalizer(cleanup)
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.delenv("TMUX_PANE", raising=False)
    monkeypatch.setenv("TMUX_TMPDIR", str(socket_root))
    assert "TMUX" not in os.environ
    assert "TMUX_PANE" not in os.environ
    assert os.environ["TMUX_TMPDIR"] == str(socket_root)
    assert not list(socket_root.glob("tmux-*/default"))
    return socket_root


def _have_tmux():
    return _tmux("-V").returncode == 0

@pytest.mark.skipif(not _have_tmux(), reason="tmux is not installed")
def test_local_tree_and_direct_path_create_in_an_isolated_session(tmp_path, isolated_tmux):
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

    assert "TMUX" not in os.environ and "TMUX_PANE" not in os.environ
    assert os.environ["TMUX_TMPDIR"] == str(isolated_tmux)
    assert not list(isolated_tmux.glob("tmux-*/default"))
    session = f"tower-browser-{os.getpid()}"
    created = _tmux("-f", "/dev/null", "new-session", "-d", "-s", session, "-n", "tower")
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


def _remote_target() -> str:
    return os.environ.get("TOWER_REMOTE_HOST", "").strip()


def _remote_workstation_up() -> bool:
    target = _remote_target()
    if not target:
        return False
    try:
        result = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=4", target, "true"],
            capture_output=True,
            timeout=8,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _remote_workstation(script: str) -> subprocess.CompletedProcess:
    target = _remote_target()
    return subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", target, script],
        capture_output=True,
        text=True,
        timeout=12,
        check=False,
    )


@pytest.mark.skipif(not _remote_workstation_up(), reason="TOWER_REMOTE_HOST is unset or SSH is not reachable")
def test_remote_workstation_browse_then_one_new_window_only():
    before = _remote_workstation('tmux list-windows -a -F "#{window_id}" 2>/dev/null || true').stdout
    roots, error = remote_start_roots(_remote_target(), [], timeout=8)
    assert error is None
    assert roots
    home = next(path for path in roots if path)
    page = list_remote_children(_remote_target(), home, timeout=8)
    assert page.ok, page.error
    during = _remote_workstation('tmux list-windows -a -F "#{window_id}" 2>/dev/null || true').stdout
    assert during == before

    catalog, catalog_error = collect_remote_catalog(_remote_target(), timeout=12)
    assert catalog_error is None
    chosen = next((entry for entry in catalog if entry.is_git), None)
    if chosen is None and page.children:
        chosen = page.children[0]
    if chosen is None:
        checked = validate_remote_path(_remote_target(), home)
        assert checked.ok
        chosen = checked.entry
    checked = validate_remote_path(_remote_target(), chosen.path)
    assert checked.ok
    chosen = checked.entry
    assert script_is_read_only(remote_list_script(chosen.path))

    from tmux_agent_tower.launcher.spawn import spawn_remote

    results = spawn_remote(
        _remote_target(),
        "tower-browser-dogfood",
        [SpawnTarget(chosen.path, chosen.name, "Shell")],
        {"Shell": "bash"},
        timeout=15,
    )
    assert results and results[0].ok, results[0].detail if results else "no result"
    after = set(_remote_workstation('tmux list-windows -a -F "#{window_id}"').stdout.split())
    before_ids = set(before.split())
    created_ids = after - before_ids
    window_id = next(iter(created_ids)) if len(created_ids) == 1 else ""
    try:
        assert len(created_ids) == 1
        assert re.fullmatch(r"@\d+", window_id)
        info = _remote_workstation(
            f'tmux list-panes -t {window_id} -F "#{{pane_current_path}}\t#{{pane_current_command}}"'
        ).stdout.strip()
        path, _, command = info.partition("\t")
        assert path == chosen.path
        assert command in ("bash", "sh", "zsh")
    finally:
        if re.fullmatch(r"@\d+", window_id or ""):
            killed = _remote_workstation(f"tmux kill-window -t {window_id}")
            assert killed.returncode == 0, killed.stderr
    restored = set(_remote_workstation('tmux list-windows -a -F "#{window_id}"').stdout.split())
    assert restored == before_ids


def _expand(tree: FolderTree, path: str, *, remote: bool = False):
    if remote:
        page = list_remote_children(tree.host, path, include_heavy=True, timeout=8)
    else:
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
def test_local_tree_three_levels_then_new_window(tmp_path, isolated_tmux):
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
    assert os.environ["TMUX_TMPDIR"] == str(isolated_tmux)
    assert not list(isolated_tmux.glob("tmux-*/default"))
    session = f"tower-tree-{os.getpid()}"
    created = _tmux("-f", "/dev/null", "new-session", "-d", "-s", session, "-n", "tower")
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


def _remote_workstation_tree_path() -> str:
    """Remote project leaf for the live SSH tree dogfood.

    ``TOWER_REMOTE_TREE`` is an absolute directory on the host in
    ``TOWER_REMOTE_HOST``. The test
    opens its great-grandparent, then the next two folders, then selects
    this leaf. Example shape: ``/<root>/<middle>/<folder>/<project>``.
    Unset means this dogfood is not configured. Public tests do not use it.
    """

    return os.environ.get("TOWER_REMOTE_TREE", "").strip()


def _remote_workstation_tree_ready() -> bool:
    path = _remote_workstation_tree_path()
    # ``/``, root, middle, folder, and the project leaf.
    if len(Path(path).parts) < 5:
        return False
    return _remote_workstation_up()


@pytest.mark.skipif(
    not _remote_workstation_tree_ready(),
    reason="TOWER_REMOTE_HOST/TOWER_REMOTE_TREE is unset or SSH is not reachable",
)
def test_remote_workstation_tree_reaches_code_and_adds_one_window():
    leaf = Path(_remote_workstation_tree_path())
    folder = leaf.parent
    middle = folder.parent
    root = middle.parent
    before = _remote_workstation('tmux list-windows -a -F "#{window_id}" 2>/dev/null || true').stdout
    home = validate_remote_path(_remote_target(), str(root), timeout=8)
    assert home.ok
    tree = FolderTree(_remote_target(), home.entry)
    _expand(tree, tree.root.path, remote=True)
    projects = _select(tree, middle.name)
    _expand(tree, projects.path, remote=True)
    opened = _select(tree, folder.name)
    _expand(tree, opened.path, remote=True)
    code = _select(tree, leaf.name)
    assert len(tree.loaded_paths()) >= 3
    visible = [row.node.name for row in tree.visible_rows()]
    assert middle.name in visible
    assert folder.name in visible
    assert leaf.name in visible
    during = _remote_workstation('tmux list-windows -a -F "#{window_id}" 2>/dev/null || true').stdout
    assert during == before
    assert script_is_read_only(remote_list_script(code.path, include_heavy=True))

    from tmux_agent_tower.launcher.spawn import spawn_remote

    results = spawn_remote(
        _remote_target(),
        "tower-tree-dogfood",
        [SpawnTarget(code.path, code.name, "Shell")],
        {"Shell": "bash"},
        timeout=15,
    )
    assert results and results[0].ok, results[0].detail if results else "no result"
    after = set(_remote_workstation('tmux list-windows -a -F "#{window_id}"').stdout.split())
    before_ids = set(before.split())
    created_ids = after - before_ids
    window_id = next(iter(created_ids)) if len(created_ids) == 1 else ""
    try:
        assert len(created_ids) == 1
        assert re.fullmatch(r"@\d+", window_id)
        info = _remote_workstation(f'tmux list-panes -t {window_id} -F "#{{pane_current_path}}\t#{{pane_current_command}}"').stdout.strip()
        path, _, command = info.partition("\t")
        assert path == code.path
        assert command in ("bash", "sh", "zsh")
    finally:
        if re.fullmatch(r"@\d+", window_id or ""):
            killed = _remote_workstation(f"tmux kill-window -t {window_id}")
            assert killed.returncode == 0, killed.stderr
    restored = set(_remote_workstation('tmux list-windows -a -F "#{window_id}"').stdout.split())
    assert restored == before_ids
