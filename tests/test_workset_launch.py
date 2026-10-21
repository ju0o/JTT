import json
import os
import shutil
import stat
import subprocess
import uuid
from dataclasses import replace
from pathlib import Path

import pytest

from tmux_agent_tower.launcher.spawn import SpawnResult, SpawnTarget, spawn_local
from tmux_agent_tower.launcher.workset_launch import (
    WorksetLaunchError, _rollback_message, launch_workset,
)
from tmux_agent_tower.state.worksets import WorkMember, WorksetStore, new_workset
from tmux_agent_tower.ui.tower import Tower
from tmux_test_safety import run_isolated_tmux


pytestmark = pytest.mark.skipif(not shutil.which("tmux"), reason="tmux is not installed")


def _tmux(*args):
    return run_isolated_tmux(*args, check=True).stdout.strip()


def _private_tmux_socket(socket_dir):
    socket_path = socket_dir / "server.sock"
    assert stat.S_ISSOCK(socket_path.lstat().st_mode), socket_path
    assert socket_path.stat().st_uid == os.geteuid(), socket_path
    return socket_path


def _window_geometry(window_id):
    rows = _tmux("list-panes", "-t", window_id, "-F",
                 "#{pane_index}:#{pane_left}:#{pane_top}:#{pane_width}:#{pane_height}")
    return [tuple(row.split(":")) for row in rows.splitlines()]


@pytest.fixture
def isolated_tower(tmp_path, monkeypatch, tmux_test_safety):
    socket_dir = tmux_test_safety.root
    assert "TMUX" not in os.environ and "TMUX_PANE" not in os.environ
    assert os.environ.get("JTT_TEST_TMUX_SOCKET") == str(socket_dir / "server.sock")
    assert not (socket_dir / "server.sock").exists()
    from tmux_agent_tower.ui import tower as tower_module

    state_dir = tmp_path / "state"
    config_dir = tmp_path / "config"
    monkeypatch.setattr(tower_module, "STATE_DIR", state_dir)
    monkeypatch.setattr(tower_module, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(tower_module, "HOST_FILE", config_dir / "host")
    monkeypatch.setattr(tower_module, "REMOTE_HOSTS_FILE", config_dir / "remote-hosts.txt")
    session = "workset-" + uuid.uuid4().hex[:8]
    _tmux("new-session", "-d", "-x", "160", "-y", "60", "-s", session, "sleep 600")
    socket_path = _private_tmux_socket(socket_dir)
    assert socket_path.parent == socket_dir
    base = _tmux("list-panes", "-t", session, "-F", "#{pane_id} #{pane_pid}").split()
    tower = Tower(session, own_pane_id=base[0])
    yield tower, state_dir, base


def _agents():
    return {name: "sleep 600" for name in ("Codex", "Claude", "Cursor", "OpenCode", "Shell")}


@pytest.mark.parametrize(("returncode", "cleaned"), [(0, True), (1, False)])
def test_remote_rollback_report_matches_cleanup_result(monkeypatch, returncode, cleaned):
    from types import SimpleNamespace

    from tmux_agent_tower.launcher import workset_launch

    target = SpawnTarget("/project", "project", "Shell")
    result = SpawnResult(target, True, "", window_id="@123")
    calls = []
    monkeypatch.setattr(workset_launch.subprocess, "run", lambda *args, **_kwargs:
                        calls.append(args[0]) or SimpleNamespace(returncode=returncode))

    message = _rollback_message(object(), [([result], "test-asus")], "시작 실패")

    if cleaned:
        assert "이번 실행을 정리했습니다." in message
        assert "새 작업이 남아 있을 수 있습니다." not in message
    else:
        assert "이번 실행을 정리했습니다." not in message
        assert "새 작업이 남아 있을 수 있습니다." in message
    assert calls == [["ssh", "-o", "BatchMode=yes", "test-asus", "tmux kill-window -t @123"]]


def test_launch_resolves_new_pane_after_its_process_id_changes(tmp_path, isolated_tower, monkeypatch):
    from tmux_agent_tower.launcher import workset_launch

    tower, state_dir, _base = isolated_tower
    project = tmp_path / "project"
    project.mkdir()
    workset = new_workset(kind="saved_work", name="PID refresh", members=[
        WorkMember("", "builder", "Shell", display_name="Builder",
                   project_name="project", project_path=str(project)),
    ])
    real_spawn = workset_launch.spawn_local

    def report_startup_pid(*args, **kwargs):
        return [replace(row, pane_pid="999999999") for row in real_spawn(*args, **kwargs)]

    monkeypatch.setattr(workset_launch, "spawn_local", report_startup_pid)
    launched = launch_workset(tower, workset, state_dir, agents_cfg=_agents())

    assert len(launched.members) == 1
    assert launched.members[0]["pane_pid"] != "999999999"
    assert launched.members[0]["role"] == "builder"


def test_template_override_launches_role_group_and_duplicate_without_touching_existing_pane(tmp_path, isolated_tower):
    tower, state_dir, base = isolated_tower
    project_b = tmp_path / "Project B"
    project_b.mkdir()
    roles = ("orchestrator", "planner", "builder", "reviewer", "qa")
    template = new_workset(
        kind="template", name="기능 고도화 팀",
        members=[WorkMember("", role, "Codex") for role in roles],
        layout="main-plus-side",
    )
    assert all(not member.project_path and member.execution_target == "auto" for member in template.members)
    store = WorksetStore(tmp_path / "worksets.json")
    store.save(template)
    encoded = store.path.read_text(encoding="utf-8")
    assert "Project A" not in encoded and "pane_id" not in encoded and "%15" not in encoded

    with pytest.raises(WorksetLaunchError, match="사용할 수 없습니다"):
        launch_workset(
            tower, template, state_dir, agents_cfg={"Codex": "sleep 600"},
            project_override=("Project B", str(project_b)),
            agent_overrides={template.members[2].member_id: "Cursor"},
        )
    assert len(_tmux("list-windows", "-t", tower.session, "-F", "#{window_id}").splitlines()) == 1

    first = launch_workset(
        tower, template, state_dir, agents_cfg=_agents(),
        project_override=("Project B", str(project_b)),
        agent_overrides={template.members[2].member_id: "Cursor"},
        execution_target="auto", group_name="Project B · 기능 고도화 팀",
    )
    second = launch_workset(
        tower, template, state_dir, agents_cfg=_agents(),
        project_override=("Project B", str(project_b)),
        agent_overrides={template.members[2].member_id: "Cursor"},
        execution_target="auto", group_name="Project B · 기능 고도화 팀",
    )
    groups = tower.work_groups.all()
    assert len(groups) == 2
    assert first.group_id != second.group_id
    assert groups[0]["member_order"] != groups[1]["member_order"]
    tower.load()
    by_target = {row["target_id"]: row for row in tower.rows}
    members = [by_target[target] for target in groups[0]["member_order"]]
    assert [row["role"] for row in members] == list(roles)
    assert [row["agent"] for row in members] == ["Codex", "Codex", "Cursor", "Codex", "Codex"]
    assert all(row["project"] == "Project B" for row in members)
    assert len(_tmux("list-windows", "-t", tower.session, "-F", "#{window_id}").splitlines()) == 3
    assert base[0] in _tmux("list-panes", "-t", tower.session, "-F", "#{pane_id}").splitlines()
    assert template.layout == "main-plus-side"


def test_group_save_reload_and_template_project_override_keep_roles_layout_names_and_existing_pane(
    tmp_path, isolated_tower, monkeypatch,
):
    from types import SimpleNamespace

    from tmux_agent_tower.ui import worksets as workset_ui

    tower, state_dir, base = isolated_tower
    project_a, project_b = tmp_path / "Project A", tmp_path / "Project B"
    project_a.mkdir()
    project_b.mkdir()
    roles = ("orchestrator", "planner", "builder", "reviewer", "qa")
    template_seed = new_workset(
        kind="template", name="기능 고도화 팀",
        members=[WorkMember("", role, "Codex") for role in roles],
        layout="main-plus-side",
    )
    launch_workset(
        tower, template_seed, state_dir, agents_cfg=_agents(),
        project_override=("Project A", str(project_a)), group_name="Project A · 기능 고도화",
    )
    first_group = tower.work_groups.all()[0]
    tower.load()
    first_target = first_group["member_order"][0]
    first_row = next(row for row in tower.rows if row.get("target_id") == first_target)
    first_layout = _window_geometry(first_row["window_id"])
    before = _tmux("list-panes", "-a", "-F",
                   "#{pane_id}:#{pane_pid}:#{window_id}:#{pane_left}:#{pane_top}:#{pane_width}:#{pane_height}").splitlines()
    original = next(line for line in before if line.startswith(base[0] + ":"))
    store = WorksetStore(tmp_path / "worksets.json")
    monkeypatch.setattr(workset_ui, "WorksetStore", lambda: store)
    choices = iter(("saved_work", "template"))
    monkeypatch.setattr(workset_ui, "run_list_picker", lambda *_a, **_k: SimpleNamespace(selected_key=next(choices)))
    monkeypatch.setattr(workset_ui, "prompt_text", lambda _s, _p, initial="": initial)
    monkeypatch.setattr(workset_ui, "show_message_screen", lambda *_a, **_k: None)
    workset_ui.save_work_group(None, tower, first_group)
    workset_ui.save_work_group(None, tower, first_group)
    saved = WorksetStore(store.path).list("saved_work")[0]
    reusable = WorksetStore(store.path).list("template")[0]

    # Simulate Tower process restart while keeping the isolated tmux/state store.
    from tmux_agent_tower.ui.tower import Tower as TowerRuntime
    restarted = TowerRuntime(tower.session, own_pane_id=tower.own_pane_id)
    reopened = launch_workset(restarted, saved, state_dir, agents_cfg=_agents())
    reopened_rows = list(reopened.members)
    assert [row["role"] for row in reopened_rows] == list(roles)
    assert [row["agent"] for row in reopened_rows] == ["Codex"] * 5
    assert [row["display_name"] for row in reopened_rows] == [row.display_name for row in saved.members]
    assert reopened.group_id != first_group["group_id"]
    reopened_layout = _window_geometry(reopened_rows[0]["window_id"])
    assert reopened_layout == first_layout

    builder = next(row for row in reusable.members if row.role == "builder")
    started = launch_workset(
        restarted, reusable, state_dir, agents_cfg=_agents(),
        project_override=("Project B", str(project_b)),
        agent_overrides={builder.member_id: "Cursor"},
        group_name="Project B · 기능 고도화",
    )
    assert started.group_name == "Project B · 기능 고도화"
    assert [row["role"] for row in started.members] == list(roles)
    assert [row["agent"] for row in started.members] == ["Codex", "Codex", "Cursor", "Codex", "Codex"]
    assert all(row["project"] == "Project B" for row in started.members)
    assert _window_geometry(started.members[0]["window_id"]) == first_layout
    assert reusable.layout == "main-plus-side"
    assert [row.layout_slot for row in reusable.members] == ["main", "side-1", "side-2", "bottom", "side-4"]
    after = _tmux("list-panes", "-a", "-F",
                  "#{pane_id}:#{pane_pid}:#{window_id}:#{pane_left}:#{pane_top}:#{pane_width}:#{pane_height}").splitlines()
    assert original in after
    assert len(_tmux("list-windows", "-t", tower.session, "-F", "#{window_id}").splitlines()) == 4

    encoded = store.path.read_text(encoding="utf-8")
    template_json = next(row for row in json.loads(encoded)["worksets"] if row["kind"] == "template")
    assert str(project_a) not in str(template_json) and str(project_b) not in str(template_json)
    assert "pane_id" not in str(template_json) and "%15" not in str(template_json)


def test_transactional_spawn_rolls_back_only_its_window_after_split_failure(tmp_path, isolated_tower, monkeypatch):
    tower, _state_dir, _base = isolated_tower
    project = tmp_path / "project"
    project.mkdir()
    from tmux_agent_tower.launcher import spawn

    real_run_tmux = spawn.tmux_capture.run_tmux
    split_count = 0

    def fail_second_split(args, *a, **kw):
        nonlocal split_count
        if args[0] == "split-window":
            split_count += 1
            if split_count == 2:
                return ""
        return real_run_tmux(args, *a, **kw)

    monkeypatch.setattr(spawn.tmux_capture, "run_tmux", fail_second_split)
    targets = [SpawnTarget(str(project), "project", "Shell") for _ in range(3)]
    results = spawn_local(tower.session, targets, {"Shell": "sleep 600"}, transactional=True)

    assert len(results) == 3 and all(not row.ok for row in results)
    assert len(_tmux("list-windows", "-t", tower.session, "-F", "#{window_id}").splitlines()) == 1


def test_mixed_execution_locations_roll_back_earlier_host_if_later_host_fails(
    tmp_path, isolated_tower, monkeypatch,
):
    from types import SimpleNamespace

    from tmux_agent_tower.launcher import workset_launch
    from tmux_agent_tower.launcher.browse import ProjectEntry
    from tmux_agent_tower.launcher.spawn import SpawnResult

    tower, state_dir, base = isolated_tower
    local_project = tmp_path / "local"
    remote_project = "/remote/project"
    local_project.mkdir()
    tower.remote_hosts = [{"alias": "valid-remote", "name": "remote host"}]
    template = new_workset(kind="saved_work", name="mixed", members=[
        WorkMember("", "builder", "Shell", project_name="local", project_path=str(local_project)),
        WorkMember("", "qa", "Shell", project_name="remote", project_path=remote_project,
                   execution_target="valid-remote"),
    ])
    monkeypatch.setattr(workset_launch, "validate_remote_path", lambda host, path:
                        SimpleNamespace(ok=True, entry=ProjectEntry("remote", path, False)))
    called = []

    def fail_remote(host, name, targets, config, **kwargs):
        called.append((host, kwargs.get("transactional")))
        return [SpawnResult(target, False, "remote launch failed") for target in targets]

    monkeypatch.setattr(workset_launch, "spawn_remote", fail_remote)
    with pytest.raises(WorksetLaunchError, match="remote launch failed"):
        launch_workset(tower, template, state_dir, agents_cfg=_agents())

    assert called == [("valid-remote", True)]
    assert len(_tmux("list-windows", "-t", tower.session, "-F", "#{window_id}").splitlines()) == 1
    assert base[0] in _tmux("list-panes", "-t", tower.session, "-F", "#{pane_id}").splitlines()


def test_missing_project_path_fails_before_creating_a_window(tmp_path, isolated_tower):
    tower, state_dir, base = isolated_tower
    workset = new_workset(kind="saved_work", name="missing path", members=[
        WorkMember("", "builder", "Shell", project_name="Missing", project_path=str(tmp_path / "gone")),
    ])
    with pytest.raises(WorksetLaunchError, match="경로를 확인"):
        launch_workset(tower, workset, state_dir, agents_cfg=_agents())
    assert len(_tmux("list-windows", "-t", tower.session, "-F", "#{window_id}").splitlines()) == 1
    assert base[0] in _tmux("list-panes", "-t", tower.session, "-F", "#{pane_id}").splitlines()


def test_work_group_moves_keep_process_layout_ownership_and_unrelated_panes_safe(
    tmp_path, isolated_tower, monkeypatch,
):
    from types import SimpleNamespace
    import hashlib

    from tmux_agent_tower.adapters.base import ResultCandidate
    from tmux_agent_tower.clipboard import CopyOutcome
    from tmux_agent_tower.detection.result import ResultTracker
    from tmux_agent_tower.tmux import structure
    from tmux_agent_tower.ui import control_view
    from tmux_agent_tower.ui import structure_menu
    from tmux_agent_tower.control import actions
    from tmux_agent_tower.detection import result as result_module

    tower, state_dir, base = isolated_tower
    project = tmp_path / "Move Project"
    project.mkdir()
    work_a = new_workset(kind="saved_work", name="Build", layout="main-plus-side", members=[
        WorkMember("", role, "Codex", project_name="Move Project", project_path=str(project))
        for role in ("orchestrator", "builder", "qa")
    ])
    work_b = new_workset(kind="saved_work", name="Review", layout="split-2", members=[
        WorkMember("", role, "Codex", project_name="Move Project", project_path=str(project))
        for role in ("reviewer", "dogfood")
    ])
    launched_a = launch_workset(tower, work_a, state_dir, agents_cfg=_agents())
    launched_b = launch_workset(tower, work_b, state_dir, agents_cfg=_agents())
    group_a = next(group for group in tower.work_groups.all() if group["group_id"] == launched_a.group_id)
    group_b = next(group for group in tower.work_groups.all() if group["group_id"] == launched_b.group_id)

    tower.load()
    by_role = {row.get("role"): row for row in tower.rows if row.get("target_id")}
    builder = by_role["builder"]
    before = (builder["target_id"], builder["pane_id"], builder["pane_pid"], builder["window_id"])
    tower.composer_drafts = {builder["pane_id"]: ("draft stays here", 9)}

    # Supply a deterministic, identity-bound complete Result to exercise Y
    # after the same pane has physically moved to another tmux window.
    def identity(row):
        if not row:
            return None
        return {
            "tmux_host": str(row.get("tmux_host") or "local"),
            "server_scope": {"host": "isolated", "socket": "/tmp/isolated-tmux",
                             "server_pid": "12345", "socket_device": 1, "socket_inode": 1},
            "session": str(row.get("session") or ""),
            "window_id": str(row.get("window_id") or ""),
            "pane_id": str(row.get("pane_id") or ""),
            "pane_pid": str(row.get("pane_pid") or ""),
        }

    monkeypatch.setattr(result_module, "pane_result_identity", identity)
    monkeypatch.setattr(actions, "pane_result_identity", identity)
    monkeypatch.setattr(control_view, "pane_result_identity", identity)
    tower.results = ResultTracker(tmp_path / "move-results.sqlite3")
    tower._result_state = lambda *_args, **_kwargs: "ready"
    expected_result = "\n".join(f"완료 결과 {index:03d}" for index in range(303))
    expected_hash = hashlib.sha256(expected_result.encode("utf-8")).hexdigest()
    tower.results.observe(
        builder["key"], "IDLE",
        ResultCandidate(expected_result, expected_hash, "high", True),
        identity=identity(builder),
    )

    # A-C: membership, order, and slot change without any tmux mutation.
    store = tower.work_groups
    store.move_target(builder["target_id"], group_b["group_id"], label="Builder")
    store.reorder_member(group_b["group_id"], builder["target_id"], "top")
    store.swap_layout_slots(group_b["group_id"], builder["target_id"], by_role["reviewer"]["target_id"])
    store.set_layout(group_b["group_id"], "main-plus-side")
    tower.load()
    moved_logically = next(row for row in tower.rows if row["target_id"] == builder["target_id"])
    assert (moved_logically["target_id"], moved_logically["pane_id"], moved_logically["pane_pid"],
            moved_logically["window_id"]) == before
    assert store.all()[1]["layout_slots"][builder["target_id"]] == "main"
    assert store.managed_resources([builder["target_id"]])[builder["target_id"]]["window_id"] == before[3]

    # A stale ownership window must fail closed even when pane ID/PID still match.
    store.update_managed_window(builder["target_id"], "@99999")
    from tmux_agent_tower.i18n import t

    current_group_b = next(group for group in store.all() if group["group_id"] == group_b["group_id"])
    stale_group, stale_error = structure_menu._managed_group_window(tower, current_group_b)
    assert stale_group is None and stale_error == t("layout.stale")
    monkeypatch.setattr(structure_menu, "run_list_picker", lambda *_a, **_k: pytest.fail(
        "stale ownership must stop before offering a physical destination"
    ))
    monkeypatch.setattr(structure_menu, "show_message_screen", lambda *_a, **_k: None)
    structure_menu.move_pane_physical(None, tower, moved_logically)
    assert structure.pane_window_id(builder["pane_id"]) == before[3]
    store.update_managed_window(builder["target_id"], before[3])

    # G/I: explicit physical move preserves pane id, process, and composer draft.
    destination = next(row["window_id"] for row in tower.rows if row.get("role") == "reviewer")
    before_cancel = _tmux("list-panes", "-a", "-F",
                          "#{pane_id}:#{pane_pid}:#{window_id}:#{pane_left}:#{pane_top}:#{pane_width}:#{pane_height}")
    monkeypatch.setattr(structure_menu, "run_list_picker", lambda *_a, **_k: SimpleNamespace(
        cancelled=True, selected_key=None,
    ))
    structure_menu.move_pane_physical(None, tower, moved_logically)
    after_cancel = _tmux("list-panes", "-a", "-F",
                         "#{pane_id}:#{pane_pid}:#{window_id}:#{pane_left}:#{pane_top}:#{pane_width}:#{pane_height}")
    assert after_cancel == before_cancel
    monkeypatch.setattr(structure_menu, "run_list_picker", lambda *_a, **_k: SimpleNamespace(
        cancelled=False, selected_key=destination,
    ))
    monkeypatch.setattr(structure_menu, "_confirm", lambda *_a, **_k: True)
    monkeypatch.setattr(structure_menu, "show_message_screen", lambda *_a, **_k: None)
    structure_menu.move_pane_physical(None, tower, moved_logically)
    tower.load()
    moved_physically = next(row for row in tower.rows if row["target_id"] == builder["target_id"])
    assert (moved_physically["pane_id"], moved_physically["pane_pid"], moved_physically["window_id"]) == (
        builder["pane_id"], builder["pane_pid"], destination,
    )
    assert tower.composer_drafts[builder["pane_id"]] == ("draft stays here", 9)
    assert store.managed_resources([builder["target_id"]])[builder["target_id"]]["window_id"] == destination

    # J: Y resolves and routes the whole complete Result under the new identity.
    moved_physically["status"] = "IDLE"
    real_tower_load = tower.load
    monkeypatch.setattr(tower, "load", lambda: None)
    moved_snapshot = tower.results.snapshot(builder["key"], identity=identity(moved_physically))
    assert moved_snapshot.fingerprint == expected_hash and moved_snapshot.text == expected_result
    routed = {}
    monkeypatch.setattr(actions, "_recover_result", lambda *_args, **_kwargs: None)

    def route_result(_tower, text, _stdscr=None):
        routed["text"] = text
        return CopyOutcome(True, False, "test-clipboard"), ""

    monkeypatch.setattr(control_view, "_route_clipboard", route_result)
    ok, reason, payload = actions.get_result(tower, builder["key"])
    assert ok and payload.get("complete"), (reason, payload, moved_physically.get("status"))
    notice = control_view._copy_result(tower, builder["key"])
    assert routed["text"] == expected_result
    assert hashlib.sha256(routed["text"].encode("utf-8")).hexdigest() == expected_hash
    assert notice.startswith("✓") and "전체" in notice
    new_result = tower.results.snapshot(builder["key"], identity=identity(moved_physically))
    assert new_result.fingerprint == expected_hash and new_result.text == expected_result
    monkeypatch.setattr(tower, "load", real_tower_load)

    # F: detach another work item while retaining the same pane and PID.
    qa = next(row for row in tower.rows if row.get("role") == "qa")
    monkeypatch.setattr(structure_menu, "run_list_picker", lambda *_a, **_k: SimpleNamespace(
        cancelled=False, selected_key="detach",
    ))
    structure_menu.move_pane_physical(None, tower, qa)
    tower.load()
    moved_qa = next(row for row in tower.rows if row["target_id"] == qa["target_id"])
    assert (moved_qa["pane_id"], moved_qa["pane_pid"]) == (qa["pane_id"], qa["pane_pid"])
    assert moved_qa["window_id"] != qa["window_id"]
    assert store.managed_resources([qa["target_id"]])[qa["target_id"]]["window_id"] == moved_qa["window_id"]

    # D/H: apply is explicit; a command failure restores the captured layout.
    live_group_b = next(group for group in store.all() if group["group_id"] == group_b["group_id"])
    live_rows = [next(row for row in tower.rows if row.get("target_id") == target)
                 for target in live_group_b["member_order"]]
    window_id = live_rows[0]["window_id"]
    layout_before = structure.window_layout(tower.session, window_id)
    real_apply, real_restore = structure.apply_layout, structure.restore_layout
    restored = []
    monkeypatch.setattr(structure, "apply_layout", lambda *_a, **_k: structure.StructureResult(False, detail="injected"))
    monkeypatch.setattr(structure, "restore_layout", lambda *args: restored.append(args) or real_restore(*args))
    assert not structure_menu.apply_group_terminal_layout(None, tower, live_group_b, "grid-4")
    assert restored and structure.window_layout(tower.session, window_id) == layout_before
    monkeypatch.setattr(structure, "apply_layout", real_apply)
    monkeypatch.setattr(structure, "restore_layout", real_restore)
    assert structure_menu.apply_group_terminal_layout(None, tower, live_group_b, "grid-4")
    panes_before = _tmux("list-panes", "-t", window_id, "-F", "#{pane_id}:#{pane_pid}").splitlines()
    assert set(panes_before) == {f"{row['pane_id']}:{row['pane_pid']}" for row in live_rows}

    # E: a user-owned extra pane makes layout application fail without retile.
    unrelated = structure.create_pane(window_id, direction="horizontal", cwd=str(project))
    assert unrelated.ok
    before_geometry = _tmux("list-panes", "-t", window_id, "-F",
                            "#{pane_id}:#{pane_pid}:#{pane_left}:#{pane_top}:#{pane_width}:#{pane_height}")
    assert not structure_menu.apply_group_terminal_layout(None, tower, live_group_b, "main-plus-side")
    after_geometry = _tmux("list-panes", "-t", window_id, "-F",
                           "#{pane_id}:#{pane_pid}:#{pane_left}:#{pane_top}:#{pane_width}:#{pane_height}")
    assert before_geometry == after_geometry
    assert unrelated.pane_id in structure.pane_ids(tower.session)
    assert base[0] in structure.pane_ids(tower.session)
