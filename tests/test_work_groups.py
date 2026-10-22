import shutil
from types import SimpleNamespace

import pytest

from tmux_agent_tower.control.actions import enter_intent
from tmux_agent_tower.server.httpapi import build_status_payload
from tmux_agent_tower.server.webui import PAGE_HTML
from tmux_agent_tower.state.work_groups import WorkGroupStore, aggregate
from tmux_agent_tower.state.worksets import WorksetStore
from tmux_agent_tower.ui import render, work_groups
from tmux_agent_tower.state.folders import window_ref
from tmux_agent_tower.ui import folder_tree
from tmux_agent_tower.ui import control_view
from tmux_agent_tower.ui.tower import Tower
from tmux_agent_tower.ui import structure_menu
from tmux_test_safety import run_isolated_tmux


def _row(target, *, name=None, role=None, status="IDLE", attention="none", result="none", pane=None):
    pane = pane or f"%{target[-2:]}"
    return {
        "target_id": target, "key": pane, "kind": "pane", "pane_id": pane,
        "display_name": name or target, "task_name": name or target,
        "project": "ExampleProject", "agent": "Codex", "role": role,
        "status": status, "attention": attention, "result_state": result,
        "host": "fixture", "tmux_host": "fixture", "remote": False,
        "session": "fixture", "session_id": "$1", "window_id": "@1",
        "window_index": "0", "window_name": "Main", "window_created": "100",
    }


def _apply_tower_tree(tower):
    assets = {}
    for row in tower.rows:
        if not row.get("pane_id") or row.get("tower_runtime"):
            continue
        identity = {key: row.get(key) for key in (
            "tmux_host", "host", "session", "session_id", "window_id",
            "window_index", "window_name", "window_created", "remote",
        )}
        ref = window_ref(identity)
        row["window_ref"] = ref
        assets.setdefault(ref, {**identity, "window_ref": ref})
    membership = tower.work_groups.membership()
    groups = {group["group_id"]: group for group in tower.work_groups.all()}
    for row in tower.rows:
        group = groups.get(membership.get(str(row.get("target_id") or "")))
        if group:
            row["work_group_id"] = group["group_id"]
            row["work_group_name"] = group["display_name"]
        else:
            row.pop("work_group_id", None)
            row.pop("work_group_name", None)
    tower.window_assets = folder_tree.infer_window_assets(assets.values(), tower.rows, tower.local_host)
    tower._apply_filter()


def test_store_persists_group_name_membership_order_and_stale_labels(tmp_path):
    path = tmp_path / "work-groups.json"
    store = WorkGroupStore(path)
    group = store.create("ExampleProject · V2", ["a", "b"], project_binding={"name": "ExampleProject"}, labels={"a": "PM", "b": "Builder"})
    store.move_member(group["group_id"], "b", -1)
    reopened = WorkGroupStore(path)
    saved = reopened.all()[0]
    assert saved["display_name"] == "ExampleProject · V2"
    assert saved["member_target_ids"] == ["a", "b"]
    assert saved["member_order"] == ["b", "a"]
    assert saved["member_labels"] == {"a": "PM", "b": "Builder"}
    assert saved["project_binding"] == {"name": "ExampleProject"}
    assert saved["layout"] == "split-2"
    assert saved["layout_slots"] == {"a": "main", "b": "side-1"}


def test_logical_move_reorder_and_slot_swap_preserve_managed_pane_identity(tmp_path):
    path = tmp_path / "groups.json"
    store = WorkGroupStore(path)
    first = store.create("A", ["pm", "builder"], layout="main-plus-side",
                         layout_slots={"pm": "side-1", "builder": "main"})
    second = store.create("B", ["qa"])
    resource = {"session": "s", "pane_id": "%9", "pane_pid": "1234", "window_id": "@2",
                "tmux_host": "host", "launch_id": "a" * 32}
    store.register_managed_resources({"builder": resource})

    store.move_target("builder", second["group_id"], label="Builder")
    store.reorder_member(second["group_id"], "builder", "top")

    reopened = WorkGroupStore(path)
    groups = {group["group_id"]: group for group in reopened.all()}
    assert groups[first["group_id"]]["member_order"] == ["pm"]
    assert groups[first["group_id"]]["layout"] == "main-plus-side"
    assert groups[second["group_id"]]["member_order"] == ["builder", "qa"]
    assert reopened.managed_resources(["builder"]) == {"builder": resource}

    reopened.move_target("builder", None)
    assert reopened.membership() == {"pm": first["group_id"], "qa": second["group_id"]}
    assert reopened.managed_resources(["builder"])["builder"]["pane_id"] == "%9"


def test_reorder_supports_top_and_bottom_without_changing_layout_slots(tmp_path):
    store = WorkGroupStore(tmp_path / "groups.json")
    group = store.create("G", ["a", "b", "c"], layout_slots={"a": "main", "b": "side-1", "c": "side-2"})
    store.swap_layout_slots(group["group_id"], "b", "c")
    store.reorder_member(group["group_id"], "c", "top")
    store.reorder_member(group["group_id"], "c", "bottom")
    saved = store.all()[0]
    assert saved["member_order"] == ["a", "b", "c"]
    assert saved["layout_slots"] == {"a": "main", "b": "side-2", "c": "side-1"}


def test_store_allows_duplicate_group_names_but_one_group_per_target(tmp_path):
    store = WorkGroupStore(tmp_path / "groups.json")
    first = store.create("Release", ["target-a"])
    second = store.create("Release", ["target-b"])
    assert first["group_id"] != second["group_id"]
    with pytest.raises(ValueError):
        store.add_members(second["group_id"], ["target-a"])
    store.remove_members(first["group_id"], ["target-a"])
    store.add_members(second["group_id"], ["target-a"])
    assert store.membership()["target-a"] == second["group_id"]


def test_rename_keeps_stable_group_id_and_names_may_repeat(tmp_path):
    store = WorkGroupStore(tmp_path / "groups.json")
    group = store.create("First", ["a"])
    store.rename(group["group_id"], "Same name")
    other = store.create("Same name", ["b"])
    assert [item["display_name"] for item in store.all()] == ["Same name", "Same name"]
    assert store.all()[0]["group_id"] == group["group_id"] != other["group_id"]


def test_remove_and_dissolve_only_change_membership(tmp_path):
    store = WorkGroupStore(tmp_path / "groups.json")
    group = store.create("QA", ["pane-target"])
    store.remove_members(group["group_id"], ["pane-target"])
    assert store.all()[0]["member_target_ids"] == []
    store.dissolve(group["group_id"])
    assert store.all() == []


def test_group_rows_aggregate_attention_results_and_stale_members():
    rows = [
        _row("pm", role="orchestrator", status="WORKING"),
        _row("builder", role="builder", status="WAITING", attention="input_required"),
        _row("qa", role="qa", status="IDLE", result="ready"),
    ]
    group = {"group_id": "g", "display_name": "ExampleProject · V2", "member_target_ids": ["pm", "builder", "qa", "gone"],
             "member_order": ["pm", "builder", "qa", "gone"], "member_labels": {"gone": "Dogfood"}}
    output = work_groups.build_rows([group], rows, set())
    header, *members = output
    assert header["kind"] == "work_group"
    assert header["summary_counts"] == {"error": 0, "attention": 1, "working": 1, "result": 1, "idle": 0, "unavailable": 1}
    assert [row.get("role") for row in members[:3]] == ["orchestrator", "builder", "qa"]
    assert members[-1]["kind"] == "work_group_stale"
    assert "확인 필요" in header["summary"]
    assert enter_intent(header) == "group"
    assert enter_intent(members[-1]) == "ignore"


def test_ungrouped_legacy_tasks_cluster_visually_by_project_without_saving_groups():
    rows = [
        _row("a", name="SampleProject · 조율", role="orchestrator", pane="%1"),
        _row("b", name="SampleProject 구현", role="builder", pane="%2"),
        _row("c", name="독립 작업", pane="%3"),
    ]
    rows[0]["project"] = "SampleProject"
    rows[1]["project"] = "SampleProject"
    rows[2]["project"] = "(이름 없음)"
    output = work_groups.build_rows([], rows, set())
    assert [row["key"] for row in output] == ["%1", "%2", "%3"]
    assert [row.get("_project_cluster") for row in output] == ["SampleProject", "SampleProject", None]
    assert all(row.get("kind") != "work_group" for row in output)


def test_group_result_count_uses_only_complete_tracker_state():
    from tmux_agent_tower.adapters.base import ResultCandidate
    from tmux_agent_tower.detection.result import ResultTracker

    tracker = ResultTracker()
    group = {"group_id": "g", "display_name": "V2", "member_target_ids": ["qa"], "member_order": ["qa"]}
    qa = _row("qa", role="qa")
    qa["result_state"] = tracker.observe(
        qa["key"], "IDLE", ResultCandidate("fragment", "partial", confidence="partial")
    ).state
    partial_rows = work_groups.build_rows([group], [qa], set())
    assert partial_rows[0]["summary_counts"]["result"] == 0
    assert partial_rows[1]["result_state"] == "none"

    qa["result_state"] = tracker.observe(
        qa["key"], "IDLE", ResultCandidate("complete answer", "complete")
    ).state
    complete_rows = work_groups.build_rows([group], [qa], set())
    assert complete_rows[0]["summary_counts"]["result"] == 1

    assert tracker.mark_read(qa["key"], "complete")
    qa["result_state"] = tracker.snapshot(qa["key"]).state
    read_rows = work_groups.build_rows([group], [qa], set())
    assert read_rows[0]["summary_counts"]["result"] == 0
    assert qa["result_state"] == "read"


def test_aggregate_keeps_error_and_unavailable_counts_distinct():
    counts = aggregate([
        {"status": "UNKNOWN", "attention": "error"},
        {"status": "IDLE", "attention": "none"},
        {"status": "DEAD", "attention": "none"},
    ])
    assert counts == {"error": 1, "attention": 0, "working": 0, "result": 0, "idle": 1, "unavailable": 1}


def test_group_collapse_search_and_ungrouped_legacy_rows():
    rows = [_row("a1", name="Builder"), _row("b1", name="Loose task")]
    group = {"group_id": "g", "display_name": "V2 Release", "member_target_ids": ["a1"], "member_order": ["a1"], "member_labels": {}}
    collapsed = work_groups.build_rows([group], rows, {"g"})
    assert [row["kind"] for row in collapsed] == ["work_group", "pane"]
    assert collapsed[0]["collapsed"] is True
    search = work_groups.build_rows([group], rows, set(), "release")
    assert [row.get("display_name") for row in search] == ["V2 Release", "Builder"]
    assert render.list_row_parts(search[0], 24, lambda key: key, "")["project"] == "V2 Release"


def test_search_skips_nonmatching_group_before_matching_group():
    rows = [_row("tower", name="Tower task"), _row("ju", name="ExampleProject task")]
    rows[0]["project"] = "Tower"
    groups = [
        {"group_id": "tower", "display_name": "Tower work", "member_target_ids": ["tower"], "member_order": ["tower"]},
        {"group_id": "ju", "display_name": "ExampleProject work", "member_target_ids": ["ju"], "member_order": ["ju"]},
    ]

    search = work_groups.build_rows(groups, rows, set(), "ExampleProject")

    assert [row.get("group_id") for row in search if row.get("kind") == "work_group"] == ["ju"]
    assert [row.get("target_id") for row in search if row.get("kind") == "pane"] == ["ju"]


@pytest.mark.parametrize("query", ["ExampleProject", "Planning", "Cursor", "not-present"])
def test_group_search_fields_and_empty_results_do_not_crash(query):
    rows = [_row("qa", name="ExampleProject release review", role="qa")]
    rows[0]["agent"] = "Cursor"
    group = {
        "group_id": "g", "display_name": "ExampleProject release", "member_target_ids": ["qa"],
        "member_order": ["qa"], "member_labels": {}, "project_binding": {"name": "Planning"},
    }

    filtered = work_groups.build_rows([group], rows, set(), query)

    if query == "not-present":
        assert filtered == []
    else:
        assert filtered


def test_search_matches_localized_role_name():
    from tmux_agent_tower.i18n import t

    assert render.row_matches_filter(_row("qa", role="qa"), t("role.qa"))


def test_tower_group_navigation_changes_only_logical_rows(tmp_path, monkeypatch):
    from tmux_agent_tower.ui import tower as tower_module

    monkeypatch.setattr(tower_module, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(tower_module, "CONFIG_DIR", tmp_path / "config")
    monkeypatch.setattr(tower_module, "HOST_FILE", tmp_path / "config" / "host")
    monkeypatch.setattr(tower_module, "REMOTE_HOSTS_FILE", tmp_path / "config" / "remote-hosts.txt")
    tower = Tower("fixture")
    one, two = _row("target-one", role="builder"), _row("target-two", role="qa")
    tower.rows = [one, two]
    group = tower.work_groups.create("ExampleProject · V2", [one["target_id"], two["target_id"]])
    _apply_tower_tree(tower)
    assert [row["kind"] for row in tower.visible_rows] == [
        "other_section", "window_asset", "pane", "pane",
    ]
    assert all(row["work_group_name"] == "ExampleProject · V2"
               for row in tower.visible_rows if row["kind"] == "pane")
    tower.selected = 2
    assert enter_intent(tower.visible_rows[2]) == "control"
    window = tower.visible_rows[1]
    tower.toggle_window_asset(window["window_ref"])
    assert [row["kind"] for row in tower.visible_rows] == ["other_section", "window_asset"]
    assert tower.work_groups.membership() == {one["target_id"]: group["group_id"],
                                              two["target_id"]: group["group_id"]}


def test_phone_status_payload_keeps_group_order_and_never_exposes_project_path(tmp_path, monkeypatch):
    from tmux_agent_tower.server import httpapi

    rows = [_row("target-a", pane="%1", role="builder", status="WORKING"), _row("target-b", pane="%2", role="qa", status="WAITING", attention="approval_required")]
    group_store = WorkGroupStore(tmp_path / "groups.json")
    group = group_store.create("ExampleProject · V2", ["target-b", "target-a"], project_binding={"name": "ExampleProject", "path": "/private/project"})
    for row in rows:
        row["work_group_id"] = group["group_id"]
        row["work_group_name"] = group["display_name"]

    fake = SimpleNamespace(session="fixture", rows=rows, work_groups=group_store, load=lambda: None)
    monkeypatch.setattr(httpapi, "WorksetStore", lambda: WorksetStore(tmp_path / "worksets.json"))
    payload = build_status_payload(fake)
    assert payload["groups"][0]["member_target_ids"] == ["target-b", "target-a"]
    assert payload["groups"][0]["summary_counts"]["attention"] == 1
    assert payload["groups"][0]["layout"] == "split-2"
    assert payload["groups"][0]["layout_slots"] == {"target-b": "main", "target-a": "side-1"}
    assert payload["panes"][0]["work_group_name"] == "ExampleProject · V2"
    assert "/private/project" not in str(payload)


def test_phone_group_cards_open_existing_member_detail():
    assert 'payload.groups || []' in PAGE_HTML
    assert 'g.member_target_ids || []' in PAGE_HTML
    assert 'openDetail(p.key)' in PAGE_HTML
    assert 'className = "work-folder"' in PAGE_HTML
    assert 'className = "work-window"' in PAGE_HTML
    assert 'appendPaneCard(member, content)' in PAGE_HTML
    assert '"/api/groups/action"' in PAGE_HTML
    assert 'action: "reorder"' in PAGE_HTML
    assert 'action: "layout"' in PAGE_HTML
    assert 'action: "move"' in PAGE_HTML


def test_group_display_clips_cjk_by_terminal_columns():
    group = {"group_id": "g", "display_name": "ExampleProject · V2 고도화", "member_target_ids": [], "member_order": [], "summary_compact": "●2  !1  ✓1"}
    row = work_groups.build_rows([group], [], set())[0]
    row["summary_compact"] = group["summary_compact"]
    label = render.list_row_parts(row, 18, lambda key: key)["project"]
    clipped = render.truncate_to_width(label, 14)
    assert render.display_width(clipped) <= 14
    assert render.list_row_parts(row, 100, lambda key: key)["badge"] == "●2  !1  ✓1"
    assert render.list_row_parts(row, 50, lambda key: key)["badge"] == ""


def test_conversation_header_includes_group_without_changing_member_identity(monkeypatch):
    seen = []

    class Screen:
        def erase(self): pass
        def getmaxyx(self): return 40, 100
        def noutrefresh(self): pass

    monkeypatch.setattr(control_view, "safe_add", lambda _s, y, _x, text, *_a: seen.append((y, text)))
    monkeypatch.setattr(control_view, "_live_lines", lambda *_a: [])
    monkeypatch.setattr(control_view, "_draw_inline_composer", lambda *_a, **_k: None)
    monkeypatch.setattr(control_view, "_draw_actions", lambda *_a, **_k: None)
    monkeypatch.setattr(control_view.curses, "doupdate", lambda: None)
    control_view._draw(Screen(), SimpleNamespace(config={}), {
        "display_name": "ExampleProject · 구현", "work_group_name": "ExampleProject · V2 고도화",
        "agent": "Codex", "role": "builder", "status": "WORKING", "execution_host": "workstation-b",
        "host": "workstation-b", "attention": "none", "activity_text": "", "remote": False, "interaction": {},
    }, "", False)
    assert "ExampleProject · 구현" in dict(seen)[0]
    assert dict(seen)[1] == "ExampleProject · V2 고도화 › Codex · 구현 · 실행 위치 workstation-b"


def test_group_menu_creation_role_sorts_and_removal_keeps_task(tmp_path, monkeypatch):
    from tmux_agent_tower.ui import tower as tower_module

    monkeypatch.setattr(tower_module, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(tower_module, "CONFIG_DIR", tmp_path / "config")
    monkeypatch.setattr(tower_module, "HOST_FILE", tmp_path / "config" / "host")
    monkeypatch.setattr(tower_module, "REMOTE_HOSTS_FILE", tmp_path / "config" / "remote-hosts.txt")
    tower = Tower("fixture")
    pm, qa, builder = _row("pm", role="orchestrator"), _row("qa", role="qa"), _row("builder", role="builder")
    tower.rows = [qa, builder, pm]
    tower.load = lambda: None
    monkeypatch.setattr(structure_menu, "run_list_picker", lambda *_a, **_k: SimpleNamespace(cancelled=False, selected_keys={"qa", "builder", "pm"}))
    monkeypatch.setattr(structure_menu, "prompt_text", lambda *_a, **_k: "ExampleProject · V2")

    structure_menu.create_work_group(None, tower)
    group = tower.work_groups.all()[0]
    assert group["member_target_ids"] == ["pm", "builder", "qa"]

    decisions = iter([
        SimpleNamespace(cancelled=False, selected_key="remove"),
        SimpleNamespace(cancelled=False, selected_keys={"builder"}),
    ])
    monkeypatch.setattr(structure_menu, "run_list_picker", lambda *_a, **_k: next(decisions))
    structure_menu.open_work_group_menu(None, tower, {"group_id": group["group_id"]})
    assert "builder" not in tower.work_groups.all()[0]["member_target_ids"]
    assert any(row["target_id"] == "builder" for row in tower.rows)


def test_empty_group_add_uses_canonical_new_task_flow_and_adds_created_target(tmp_path, monkeypatch):
    store = WorkGroupStore(tmp_path / "groups.json")
    group = store.create("ExampleProject", ["existing"])
    existing = _row("existing", pane="%1")
    tower = SimpleNamespace(
        work_groups=store, rows=[existing], load=lambda: None,
    )
    choices = iter([
        SimpleNamespace(cancelled=False, selected_key="add"),
        SimpleNamespace(cancelled=False, selected_key="create"),
    ])
    monkeypatch.setattr(structure_menu, "run_list_picker", lambda *_a, **_k: next(choices))
    created = _row("new-target", name="Tower · 구현", role="builder", pane="%55")

    def create_through_existing_flow(_stdscr, _tower):
        tower.rows.append(created)

    monkeypatch.setattr(structure_menu, "start_task", create_through_existing_flow)

    structure_menu.open_work_group_menu(None, tower, {"group_id": group["group_id"]})

    saved = store.all()[0]
    assert saved["member_target_ids"] == ["existing", "new-target"]
    assert tower.rows == [existing, created]


def test_group_layout_saves_to_saved_work_only_not_template(tmp_path, monkeypatch):
    from tmux_agent_tower.state.worksets import WorkMember, new_workset

    saved = new_workset(kind="saved_work", name="Saved", layout="split-2", members=[
        WorkMember("", "builder", "Codex", project_name="P", project_path=str(tmp_path)),
        WorkMember("", "qa", "Claude", project_name="P", project_path=str(tmp_path)),
    ])
    template = new_workset(kind="template", name="Template", layout="split-2", members=[
        WorkMember("", "builder", "Codex"), WorkMember("", "qa", "Claude"),
    ])
    store = WorksetStore(tmp_path / "worksets.json")
    store.save(saved)
    store.save(template)
    monkeypatch.setattr("tmux_agent_tower.state.worksets.WorksetStore", lambda: store)
    monkeypatch.setattr(structure_menu, "run_list_picker", lambda *_a, **_k: SimpleNamespace(
        cancelled=False, selected_key=saved.workset_id,
    ))
    monkeypatch.setattr(structure_menu, "show_message_screen", lambda *_a, **_k: None)
    tower = SimpleNamespace(load=lambda: None, rows=[
        _row("target-a", role="builder"), _row("target-b", role="qa"),
    ])
    group = {
        "group_id": "group-1", "member_order": ["target-a", "target-b"],
        "layout": "main-plus-side",
        "layout_slots": {"target-a": "side-1", "target-b": "main"},
    }

    structure_menu.save_group_layout_to_workset(None, tower, group)

    updated_saved = store.list("saved_work")[0]
    unchanged_template = store.list("template")[0]
    assert updated_saved.layout == "main-plus-side"
    assert [member.layout_slot for member in updated_saved.members] == ["side-1", "main"]
    assert unchanged_template.layout == template.layout == "split-2"
    assert [member.layout_slot for member in unchanged_template.members] == ["main", "side-1"]


@pytest.mark.skipif(not shutil.which("tmux"), reason="tmux is not installed")
def test_work_group_lifecycle_against_isolated_tmux_server(tmp_path, monkeypatch, tmux_test_safety):
    from tmux_agent_tower.ui import tower as tower_module

    def tmux(*args):
        return run_isolated_tmux(*args, check=True).stdout.strip()

    try:
        tmux("new-session", "-d", "-x", "160", "-y", "60", "-s", "wbs03", "-n", "isolated", "sleep 600")
        for _ in range(4):
            tmux("split-window", "-t", "wbs03", "-d", "sleep 600")
        tmux("select-layout", "-t", "wbs03", "tiled")
        before = tmux("list-panes", "-s", "-t", "wbs03", "-F", "#{pane_id}:#{pane_pid}:#{window_id}:#{pane_left}:#{pane_top}:#{pane_width}:#{pane_height}").splitlines()
        windows_before = tmux("list-windows", "-a", "-F", "#{session_name}:#{window_id}:#{window_name}:#{window_layout}").splitlines()
        assert len(before) == 5
        pane_rows = [_row(f"stable-{i}", role=role, status=status, pane=entry.split(":")[0]) for i, (entry, role, status) in enumerate(zip(
            before, ("orchestrator", "planner", "builder", "qa", "dogfood"), ("IDLE", "WORKING", "IDLE", "WAITING", "IDLE")
        ))]
        for row, entry in zip(pane_rows, before):
            pane_id, pane_pid = entry.split(":", 2)[:2]
            row["target_id"] = tower_module._target_id("fixture", "wbs03", pane_id, pane_pid)
        pane_rows[3]["attention"] = "approval_required"
        monkeypatch.setattr(tower_module, "STATE_DIR", tmp_path / "state")
        monkeypatch.setattr(tower_module, "CONFIG_DIR", tmp_path / "config")
        monkeypatch.setattr(tower_module, "HOST_FILE", tmp_path / "config" / "host")
        monkeypatch.setattr(tower_module, "REMOTE_HOSTS_FILE", tmp_path / "config" / "remote-hosts.txt")
        tower = Tower("wbs03")
        tower.rows = pane_rows
        group = tower.work_groups.create("ExampleProject · V2", [row["target_id"] for row in pane_rows], labels={row["target_id"]: row["display_name"] for row in pane_rows})

        # A/B/C: group members remain tasks within the default window tree.
        _apply_tower_tree(tower)
        assert [row["kind"] for row in tower.visible_rows] == [
            "other_section", "window_asset", "pane", "pane", "pane", "pane", "pane",
        ]
        assert tower.visible_rows[0]["summary_counts"]["attention"] == 1
        tower.toggle_window_asset(tower.visible_rows[1]["window_ref"])
        assert len(tower.visible_rows) == 2 and tower.visible_rows[1]["kind"] == "window_asset"
        tower.toggle_window_asset(tower.visible_rows[1]["window_ref"])
        expanded = tower.visible_rows
        assert [row.get("role") for row in expanded[2:]] == [row["role"] for row in pane_rows]
        # D/E: member Enter resolves to the existing control surface; live state rolls into summary.
        assert enter_intent(expanded[3]) == "control"
        tower.rows[2]["result_state"] = "ready"
        tower._apply_filter()
        window = next(row for row in tower.visible_rows if row["kind"] == "window_asset")
        assert window["summary_counts"]["result"] == 1
        # F/G/H: membership removal leaves its tmux pane alive; restart preserves data; stale member remains visible.
        removed = pane_rows[0]["target_id"]
        tower.work_groups.remove_members(group["group_id"], [removed])
        _apply_tower_tree(tower)
        assert any(row.get("target_id") == removed and not row.get("work_group_id") for row in tower.visible_rows)
        after_remove = tmux("list-panes", "-s", "-t", "wbs03", "-F", "#{pane_id}:#{pane_pid}:#{window_id}:#{pane_left}:#{pane_top}:#{pane_width}:#{pane_height}").splitlines()
        assert after_remove == before
        reopened = WorkGroupStore(tmp_path / "state" / "work-groups.json")
        assert removed not in reopened.all()[0]["member_target_ids"]
        reopened.add_members(group["group_id"], [removed], {removed: pane_rows[0]["display_name"]})
        stale_rows = [row for row in pane_rows if row["target_id"] != removed]
        stale_view = work_groups.build_rows(reopened.all(), stale_rows, set())
        assert stale_view[-1]["kind"] == "work_group_stale"
        assert tmux("list-panes", "-s", "-t", "wbs03", "-F", "#{pane_id}:#{pane_pid}:#{window_id}:#{pane_left}:#{pane_top}:#{pane_width}:#{pane_height}").splitlines() == before
        assert tmux("list-windows", "-a", "-F", "#{session_name}:#{window_id}:#{window_name}:#{window_layout}").splitlines() == windows_before
    finally:
        run_isolated_tmux("kill-server", check=False)
