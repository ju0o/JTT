import json
import os

import pytest

from tmux_agent_tower.state.folders import FolderStore, window_ref
from tmux_agent_tower.state.projects import ProjectStore
from tmux_agent_tower.ui import folder_tree


def _window(host, name, *, session_id="$1", window_id="@1", created="100"):
    value = {
        "tmux_host": host, "session": "work", "session_id": session_id,
        "window_id": window_id, "window_index": "0", "window_name": name,
        "window_created": created,
    }
    value["window_ref"] = window_ref(value)
    return value


def test_projects_group_windows_across_hosts_and_preserve_same_pane_id_identity(tmp_path):
    main = _window("mainpc", "Main")
    asus = _window("asus", "Main")
    store = ProjectStore(tmp_path / "projects.json")
    project = store.create("통합 작업")
    store.add_windows(project["project_id"], [main["window_ref"], asus["window_ref"]])

    reopened = ProjectStore(tmp_path / "projects.json")
    saved = reopened.all()[0]

    assert saved["window_refs"] == [main["window_ref"], asus["window_ref"]]
    assert main["window_ref"] != asus["window_ref"]
    assert reopened.memberships() == {
        main["window_ref"]: project["project_id"],
        asus["window_ref"]: project["project_id"],
    }


def test_project_window_reassignment_requires_explicit_move_and_keeps_order(tmp_path):
    first_window = _window("mainpc", "One")
    second_window = _window("asus", "Two")
    store = ProjectStore(tmp_path / "projects.json")
    first = store.create("First")
    second = store.create("Second")
    store.add_windows(first["project_id"], [first_window["window_ref"]])

    with pytest.raises(ValueError, match="another project"):
        store.add_windows(second["project_id"], [first_window["window_ref"], second_window["window_ref"]])
    assert store.all()[0]["window_refs"] == [first_window["window_ref"]]
    assert store.all()[1]["window_refs"] == []

    store.add_windows(second["project_id"], [first_window["window_ref"], second_window["window_ref"]], move_existing=True)
    assert store.all()[0]["window_refs"] == []
    assert store.all()[1]["window_refs"] == [first_window["window_ref"], second_window["window_ref"]]


def test_project_crud_order_visibility_and_stale_refs_survive_reopen(tmp_path):
    path = tmp_path / "projects.json"
    first = _window("mainpc", "One")
    stale = _window("asus", "Gone", window_id="@9")
    store = ProjectStore(path)
    alpha = store.create("Alpha")
    beta = store.create("Beta")
    store.add_windows(alpha["project_id"], [first["window_ref"], stale["window_ref"]])
    store.add_windows(beta["project_id"], [_window("mainpc", "Two", window_id="@2")["window_ref"]])
    store.rename(alpha["project_id"], "개발")
    store.reorder_project(beta["project_id"], "top")
    store.reorder_window(alpha["project_id"], stale["window_ref"], "top")
    store.toggle_project(alpha["project_id"])
    store.set_hidden(first["window_ref"], True)

    reopened = ProjectStore(path)
    assert [item["display_name"] for item in reopened.all()] == ["Beta", "개발"]
    saved_alpha = next(item for item in reopened.all() if item["project_id"] == alpha["project_id"])
    assert saved_alpha["window_refs"] == [stale["window_ref"], first["window_ref"]]
    assert saved_alpha["collapsed"] is True
    assert reopened.hidden_refs() == {first["window_ref"]}

    reopened.set_hidden(first["window_ref"], False)
    assert reopened.hidden_refs() == set()
    assert reopened.delete(alpha["project_id"]) == [stale["window_ref"], first["window_ref"]]
    assert reopened.hidden_refs() == set()


def test_project_operations_never_modify_legacy_folder_data(tmp_path):
    folder_path = tmp_path / "folders.json"
    folder_store = FolderStore(folder_path)
    window = _window("mainpc", "Main")
    folder = folder_store.create("개인 프로젝트")
    folder_store.move_window(window["window_ref"], folder["folder_id"])
    before = folder_path.read_bytes()

    projects = ProjectStore(tmp_path / "projects.json")
    project = projects.create("새 논리 프로젝트")
    projects.add_windows(project["project_id"], [window["window_ref"]])
    projects.rename(project["project_id"], "통합 프로젝트")
    projects.set_hidden(window["window_ref"], True)

    assert folder_path.read_bytes() == before
    assert FolderStore(folder_path).all()[0]["display_name"] == "개인 프로젝트"


def test_corrupt_or_future_project_file_is_never_overwritten(tmp_path):
    path = tmp_path / "projects.json"
    path.write_text('{"schema_version": 77, "projects": []}', encoding="utf-8")
    original = path.read_bytes()
    store = ProjectStore(path)

    assert store.all() == []
    assert store.corrupt is True
    with pytest.raises(OSError):
        store.create("Do not overwrite")
    assert path.read_bytes() == original


def test_project_state_symlink_is_not_followed_or_replaced(tmp_path):
    target = tmp_path / "outside.json"
    target.write_text('{"keep": true}', encoding="utf-8")
    path = tmp_path / "projects.json"
    path.symlink_to(target)
    store = ProjectStore(path)
    original = target.read_bytes()

    assert store.corrupt is True
    with pytest.raises(OSError):
        store.create("Do not follow link")
    assert path.is_symlink()
    assert target.read_bytes() == original


def test_project_save_detects_external_change_and_uses_private_file_mode(tmp_path):
    path = tmp_path / "projects.json"
    store = ProjectStore(path)
    project = store.create("First")
    assert os.stat(path).st_mode & 0o777 == 0o600

    external = json.loads(path.read_text(encoding="utf-8"))
    external["projects"][0]["display_name"] = "Changed elsewhere"
    path.write_text(json.dumps(external), encoding="utf-8")
    before = path.read_bytes()
    with pytest.raises(OSError, match="changed outside"):
        store.rename(project["project_id"], "Second")
    assert path.read_bytes() == before


def test_project_remove_and_move_keep_only_logical_references(tmp_path):
    window = _window("mainpc", "Main")
    physical_before = json.dumps(window, sort_keys=True)
    store = ProjectStore(tmp_path / "projects.json")
    project = store.create("Project")
    store.add_windows(project["project_id"], [window["window_ref"]])
    store.remove_windows(project["project_id"], [window["window_ref"]])

    assert store.all()[0]["window_refs"] == []
    assert json.dumps(window, sort_keys=True) == physical_before


def test_hidden_project_window_is_not_in_default_folder_projection_and_can_be_restored(tmp_path):
    folder_store = FolderStore(tmp_path / "folders.json")
    window = _window("mainpc", "Main")
    folder = folder_store.create("기존 폴더")
    folder_store.move_window(window["window_ref"], folder["folder_id"])
    task = {
        "target_id": "target-main", "key": "%1", "pane_id": "%1",
        "window_ref": window["window_ref"], "display_name": "Main task",
        "agent": "Codex", "status": "IDLE",
    }

    hidden_rows = folder_tree.build_rows(
        folder_store, [window], [task], hidden_window_refs={window["window_ref"]},
    )
    restored_rows = folder_tree.build_rows(folder_store, [window], [task])

    assert [row["kind"] for row in hidden_rows] == ["folder"]
    assert [row["kind"] for row in restored_rows] == ["folder", "window_asset", "pane"]


def test_tower_hides_project_window_from_default_work_list_and_restores_it(tmp_path):
    from types import SimpleNamespace

    from tmux_agent_tower.ui.tower import Tower, USER_WORK_VIEW

    window = _window("mainpc", "Main")
    task = {
        "target_id": "target-main", "key": "%1", "pane_id": "%1",
        "window_ref": window["window_ref"], "display_name": "Main task",
        "agent": "Codex", "status": "IDLE", "attention": "none",
        "result_state": "none",
    }
    project_store = ProjectStore(tmp_path / "projects.json")
    project_store.create("Release", [window["window_ref"]])
    project_store.set_hidden(window["window_ref"], True)
    tower = Tower.__new__(Tower)
    tower.rows = [task]
    tower.window_assets = [window]
    tower.projects = project_store
    tower.work_groups = SimpleNamespace(all=lambda: [])
    tower.folders = FolderStore(tmp_path / "folders.json")
    tower.filter_text = ""
    tower.view_mode = USER_WORK_VIEW
    tower.attention_mode = False
    tower.collapsed_other = False
    tower.collapsed_agents = False
    tower.selected = 0
    tower.visible_rows = []
    tower.visual = []
    tower._build_visual = lambda: None

    tower._apply_filter()
    assert [row.get("action") for row in tower.visible_rows if row.get("kind") == "zero"] == [
        "task", "saved", "template", "projects",
    ]

    project_store.set_hidden(window["window_ref"], False)
    tower._apply_filter()
    assert any(row.get("kind") == "pane" and row.get("key") == "%1" for row in tower.visible_rows)
