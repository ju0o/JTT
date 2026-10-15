import json

import pytest

from tmux_agent_tower.state.folders import FolderStore, window_ref
from tmux_agent_tower.ui import folder_tree, render
from tmux_agent_tower.tmux import discovery


def _window(name, *, session="s1", session_id="$1", window_id="@1", created="100"):
    value = {
        "tmux_host": "host-a", "session": session, "session_id": session_id,
        "window_id": window_id, "window_index": "0", "window_name": name,
        "window_created": created,
    }
    value["window_ref"] = window_ref(value)
    return value


def _task(window, target, *, name, role=None, agent="Codex", status="IDLE",
          attention="none", result_state="none", **extra):
    return {
        "target_id": target, "key": target, "pane_id": "%1", "window_ref": window["window_ref"],
        "display_name": name, "task_name": name, "project": "Project A", "role": role,
        "agent": agent, "status": status, "attention": attention,
        "result_state": result_state, **extra,
    }


def test_window_identity_survives_rename_but_not_window_reuse():
    first = _window("Main")
    renamed = dict(first, window_name="Release")
    reused = dict(first, window_created="200")
    assert window_ref(first) == window_ref(renamed)
    assert window_ref(first) != window_ref(reused)


def test_folder_state_persists_and_folder_operations_are_logical_only(tmp_path):
    path = tmp_path / "folders.json"
    store = FolderStore(path)
    a = _window("Main")
    b = _window("QA", window_id="@2", created="200")
    physical_snapshot = json.dumps([a, b], sort_keys=True)
    first = store.create("개인 프로젝트")
    duplicate = store.create("개인 프로젝트")
    store.move_window(a["window_ref"], first["folder_id"])
    store.move_window(b["window_ref"], first["folder_id"])
    store.reorder_window(b["window_ref"], "top")
    store.rename(first["folder_id"], "실험")
    store.toggle_folder(first["folder_id"])
    store.reorder_folder(duplicate["folder_id"], "top")
    store.update_window(a["window_ref"], display_name="Main renamed", collapsed=True)

    reopened = FolderStore(path)
    folders = reopened.all()
    assert [item["display_name"] for item in folders] == ["개인 프로젝트", "실험"]
    assert folders[1]["window_refs"] == [b["window_ref"], a["window_ref"]]
    assert folders[1]["collapsed"] is True
    assert folders[0]["window_refs"] == []
    assert folders[0]["folder_id"] != folders[1]["folder_id"]
    assert reopened.window(a["window_ref"])["display_name"] == "Main renamed"
    assert reopened.window(a["window_ref"])["collapsed"] is True
    assert json.dumps([a, b], sort_keys=True) == physical_snapshot

    assert reopened.delete(first["folder_id"]) == [b["window_ref"], a["window_ref"]]
    assert reopened.all()[0]["folder_id"] == duplicate["folder_id"]
    assert reopened.workspace([a, b], []) ["unfiled_windows"]


def test_move_keeps_exactly_one_folder_membership_and_only_changes_logical_state(tmp_path):
    store = FolderStore(tmp_path / "folders.json")
    window = _window("Main")
    first = store.create("A")
    second = store.create("B")
    before = json.dumps(window, sort_keys=True)
    store.move_window(window["window_ref"], first["folder_id"])
    store.move_window(window["window_ref"], second["folder_id"])
    tree = store.workspace([window], [])
    assert tree["folders"][0]["windows"] == []
    assert tree["folders"][1]["windows"][0]["window_ref"] == window["window_ref"]
    assert json.dumps(window, sort_keys=True) == before


def test_moving_window_to_unknown_folder_does_not_change_membership(tmp_path):
    store = FolderStore(tmp_path / "folders.json")
    window = _window("Main")
    folder = store.create("A")
    store.move_window(window["window_ref"], folder["folder_id"])
    with pytest.raises(KeyError):
        store.move_window(window["window_ref"], "missing")
    assert store.all()[0]["window_refs"] == [window["window_ref"]]


def test_corrupt_or_future_folder_state_is_never_overwritten(tmp_path):
    path = tmp_path / "folders.json"
    path.write_text('{"schema_version":99,"folders":[]}', encoding="utf-8")
    original = path.read_bytes()
    store = FolderStore(path)
    assert store.all() == []
    with pytest.raises(OSError):
        store.create("New")
    assert path.read_bytes() == original


def test_workspace_projection_keeps_stale_windows_and_aggregates_states(tmp_path):
    store = FolderStore(tmp_path / "folders.json")
    window = _window("JuBrain")
    folder = store.create("개인 프로젝트")
    store.move_window(window["window_ref"], folder["folder_id"])
    stale_ref = "a" * 64
    store.move_window(stale_ref, folder["folder_id"])
    tasks = [
        _task(window, "builder", name="API 구현", status="WORKING"),
        _task(window, "qa", name="확인", role="qa", status="WAITING", attention="input_required"),
        _task(window, "reviewer", name="검수", role="reviewer", result_state="ready"),
    ]

    workspace = store.workspace([window], tasks)
    saved_folder = workspace["folders"][0]
    assert saved_folder["summary_counts"] == {
        "error": 0, "attention": 1, "working": 1, "result": 1,
        "idle": 0, "unavailable": 1,
    }
    assert saved_folder["windows"][1]["stale"] is True
    assert saved_folder["windows"][1]["summary_counts"]["unavailable"] == 1
    assert saved_folder["windows"][0]["summary_counts"]["attention"] == 1
    assert saved_folder["windows"][0]["summary_counts"]["working"] == 1
    assert saved_folder["windows"][0]["summary_counts"]["result"] == 1
    assert "path" not in json.dumps(workspace)
    assert "pane_id" not in json.dumps(workspace)
    assert "window_id" not in json.dumps(workspace)


def test_folder_tree_renders_empty_folder_grouped_and_unfiled_windows(tmp_path):
    store = FolderStore(tmp_path / "folders.json")
    main = _window("JuBrain")
    loose = _window("Research", window_id="@2", created="200")
    folder = store.create("개인 프로젝트")
    empty = store.create("외주")
    store.move_window(main["window_ref"], folder["folder_id"])
    tasks = [
        _task(main, "pm", name="PM", role="orchestrator", agent="Claude", status="IDLE"),
        _task(main, "builder", name="구현", role="builder", status="WORKING"),
        _task(loose, "research", name="조사", role=None, agent="OpenCode"),
    ]
    assets = folder_tree.infer_window_assets([main, loose], tasks, "host-a")
    rows = folder_tree.build_rows(store, assets, tasks)

    assert [(row["kind"], row["display_name"]) for row in rows] == [
        ("other_section", "Agent 작업 · 자동 발견"),
        ("window_asset", "Research"), ("pane", "조사"),
        ("folder", "개인 프로젝트"), ("window_asset", "JuBrain"),
        ("pane", "PM"), ("pane", "구현"), ("folder", "외주"),
    ]
    assert rows[3]["summary_counts"]["working"] == 1
    assert rows[3]["summary_counts"]["idle"] == 1
    assert "pane_id" not in json.dumps([rows[0], rows[1], rows[3], rows[4]])
    assert empty["window_refs"] == []
    by_task = folder_tree.build_rows(store, assets, tasks, "구현")
    assert any(row.get("display_name") == "구현" and row["kind"] == "pane" for row in by_task)


def test_unfiled_agent_work_is_prioritized_and_misc_terminals_can_start_collapsed(tmp_path):
    store = FolderStore(tmp_path / "folders.json")
    agent_window = _window("OpenCode", window_id="@agent", created="100")
    shell_window = _window("Shell", window_id="@shell", created="200")
    tasks = [
        _task(agent_window, "agent", name="API work", agent="Display label", result_adapter="OpenCode"),
        _task(shell_window, "shell", name="Command prompt", agent="Custom terminal", result_adapter="Shell"),
    ]
    assets = folder_tree.infer_window_assets([agent_window, shell_window], tasks, "host-a")

    rows = folder_tree.build_rows(store, assets, tasks, collapsed_other=True)
    assert [(row["kind"], row["display_name"]) for row in rows] == [
        ("other_section", "Agent 작업 · 자동 발견"),
        ("window_asset", "OpenCode"), ("pane", "API work"),
        ("other_section", "기타 터미널 · 자동 발견"),
    ]
    assert rows[0]["collapsed"] is False
    assert rows[3]["collapsed"] is True

    expanded = folder_tree.build_rows(store, assets, tasks, collapsed_other=False)
    assert any(row.get("display_name") == "Command prompt" for row in expanded)


def test_folder_tree_search_matches_folder_window_and_tasks_without_other_stale_leaks(tmp_path):
    store = FolderStore(tmp_path / "folders.json")
    main = _window("JuBrain Main")
    other = _window("Tower QA", window_id="@2", created="200")
    folder = store.create("개인 프로젝트")
    stale_ref = "b" * 64
    store.move_window(main["window_ref"], folder["folder_id"])
    store.move_window(stale_ref, folder["folder_id"])

    assets = folder_tree.infer_window_assets([main, other], [], "host-a")
    by_folder = folder_tree.build_rows(store, assets, [], "개인 프로젝트")
    by_window = folder_tree.build_rows(store, assets, [], "Tower QA")
    assert any(row["kind"] == "window_asset" and row["display_name"] == "JuBrain Main" for row in by_folder)
    assert not any(row.get("window_ref") == stale_ref for row in by_window)
    assert any(row["kind"] == "window_asset" and row["display_name"] == "Tower QA" for row in by_window)
    by_other = folder_tree.build_rows(store, assets, [], "기타")
    assert any(row["kind"] == "other_section" for row in by_other)


def test_inference_uses_meaningful_window_title_then_project_then_terminal():
    main = _window("JuBrain | Main")
    shell = _window("bash", window_id="@2", created="200")
    unknown = _window("0", window_id="@3", created="300")
    tasks = [_task(shell, "one", name="Shell work", project="TowerProject")]
    inferred = folder_tree.infer_window_assets([main, shell, unknown], tasks, "host-a")
    assert [row["inferred_name"] for row in inferred] == ["JuBrain | Main", "TowerProject", "터미널"]


def test_window_and_task_rows_fit_cjk_width():
    text = render.truncate_to_width("개인 프로젝트 · 긴 작업 화면 이름", 15)
    assert render.display_width(text) <= 15


def test_tui_window_asset_keeps_internal_target_for_existing_window_actions(tmp_path):
    store = FolderStore(tmp_path / "folders.json")
    source = _window("JuBrain")
    asset = folder_tree.infer_window_assets([source], [], "host-a")
    row = folder_tree.build_rows(store, asset, [])[1]
    assert row["window_id"] == "@1"
    assert row["session"] == "s1"


def test_tower_runtime_only_window_is_hidden_but_mixed_window_remains():
    tower_window = _window("Tower", window_id="@tower")
    user_window = _window("Main", window_id="@main", created="200")
    runtime = {"pane_id": "%tower", "window_ref": tower_window["window_ref"], "tower_runtime": True}
    user = {"pane_id": "%user", "window_ref": user_window["window_ref"], "tower_runtime": False}
    mixed = {"pane_id": "%tower2", "window_ref": user_window["window_ref"], "tower_runtime": True}
    assert folder_tree.visible_windows([tower_window, user_window], [runtime, user], "@tower") == [user_window]
    assert folder_tree.visible_windows([user_window], [user, mixed]) == [user_window]


def test_window_discovery_reads_ids_and_creation_marker(monkeypatch):
    fields = ["s1", "$1", "@17", "3", "Main", "12345"]
    monkeypatch.setattr(discovery.capture, "run_tmux", lambda args: (
        "@17" if args[0] == "display-message" else "\x1f".join(fields)
    ))
    rows = discovery.list_windows("s1")
    assert rows == [{"session": "s1", "session_id": "$1", "window_id": "@17",
                     "window_index": "3", "window_name": "Main", "window_created": "12345"}]
    assert discovery.window_for_pane("%5") == "@17"
