"""Tower as a tmux window/pane navigator.

Enter moves by pane id (select-window + select-pane). A renamed window,
a moved pane, and a stale id never become a window-name lookup or a
keystroke into the pane.
"""

from tmux_agent_tower.tmux import keybind, navigation
from tmux_agent_tower.ui.tower import navigator_rows


def _pane(
    pane_id,
    window_index,
    window_name,
    pane_index="0",
    active=False,
    session="0",
    window_id="",
    project="",
    agent="Claude",
    status="IDLE",
    host="workstation-a",
    remote=False,
):
    return {
        "kind": "pane",
        "key": pane_id,
        "session": session,
        "window_id": window_id,
        "window_index": window_index,
        "window_name": window_name,
        "pane_index": pane_index,
        "pane_id": pane_id,
        "pane_active": active,
        "host": host,
        "project": project or pane_id,
        "agent": agent,
        "status": status,
        "remote": remote,
    }


def _apply_user_work_view(tower):
    from tmux_agent_tower.state.folders import window_ref
    from tmux_agent_tower.ui import folder_tree

    windows = {}
    for row in tower.rows:
        if not row.get("pane_id") or row.get("tower_runtime"):
            continue
        row.setdefault("tmux_host", row.get("host") or tower.local_host)
        identity = {
            "tmux_host": row.get("tmux_host"), "host": row.get("host"),
            "session": row.get("session"), "session_id": row.get("session_id"),
            "window_id": row.get("window_id") or "@test-" + str(row.get("pane_id")),
            "window_index": row.get("window_index"), "window_name": row.get("window_name"),
            "window_created": row.get("window_created"), "remote": row.get("remote"),
        }
        ref = window_ref(identity)
        row["window_ref"] = ref
        windows.setdefault(ref, {**identity, "window_ref": ref})
        row.setdefault("target_id", row.get("key") or row.get("pane_id"))
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
    tower.window_assets = folder_tree.infer_window_assets(windows.values(), tower.rows, tower.local_host)
    tower._apply_filter()


def test_navigator_windows_are_selectable_rows():
    rows = navigator_rows(
        [
            _pane("%6", "0", "main", active=True, window_id="@1", project="main-task"),
            _pane("%44", "0", "main", pane_index="1", window_id="@1"),
            _pane("%51", "1", "agents", active=True, window_id="@2"),
            _pane("%52", "1", "agents", pane_index="1", window_id="@2"),
            {
                "key": "workstation-b:%43",
                "remote": True,
                "pane_id": "%43",
                "host": "workstation-b",
                "session": "0",
                "window_index": "0",
                "window_name": "work",
                "project": "JuAgentEconomy",
                "agent": "Codex",
                "status": "IDLE",
            },
        ]
    )
    assert [row["kind"] for row in rows] == [
        "window", "pane", "pane", "window", "pane", "pane", "window", "pane",
    ]
    assert [row["key"] for row in rows if row["kind"] == "window"] == ["@1", "@2", "workstation-b:0:0"]
    assert [row["pane_id"] for row in rows if row["kind"] == "pane"] == ["%6", "%44", "%51", "%52", "%43"]
    assert rows[1]["nav_group"] == rows[2]["nav_group"] == "win:workstation-a:0:@1"
    assert "main-task" in rows[0]["project"]
    assert rows[-1]["remote"] is True


def test_duplicate_window_names_do_not_merge():
    rows = navigator_rows(
        [
            _pane("%1", "0", "main", window_id="@1"),
            _pane("%2", "1", "main", window_id="@2"),
        ]
    )
    windows = [row for row in rows if row["kind"] == "window"]
    assert [row["key"] for row in windows] == ["@1", "@2"]
    assert windows[0]["nav_group"] != windows[1]["nav_group"]


def test_navigator_visual_draws_a_window_divider_once_per_group(tmp_path, monkeypatch):
    from tmux_agent_tower.ui import tower as tower_module

    monkeypatch.setattr(tower_module, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(tower_module, "CONFIG_DIR", tmp_path / "config")
    monkeypatch.setattr(tower_module, "HOST_FILE", tmp_path / "config" / "host")
    monkeypatch.setattr(tower_module, "REMOTE_HOSTS_FILE", tmp_path / "config" / "remote-hosts.txt")
    tower = tower_module.Tower("0", own_pane_id="%6")
    tower.navigator_mode = True
    tower.rows = [
        _pane("%6", "0", "main", window_id="@1"),
        _pane("%44", "0", "main", pane_index="1", window_id="@1"),
        _pane("%51", "1", "agents", window_id="@2"),
    ]
    tower._apply_filter()

    assert [row["kind"] for row in tower.visible_rows] == ["window", "pane", "pane", "window", "pane"]
    assert [row.get("pane_id") for row in tower.visible_rows if row["kind"] == "pane"] == ["%6", "%44", "%51"]
    headers = [(item["host"], item.get("level")) for item in tower.visual if item["type"] == "header"]
    assert headers == [("workstation-a", None)]
    tower.selected = len(tower.visible_rows) - 1
    tower.move_down()
    assert tower.selected == 0
    assert tower.visible_rows[0]["kind"] == "window"
    assert tower.control_key() == "@1"


def test_renamed_window_keeps_its_id():
    before = navigator_rows([_pane("%44", "0", "main", window_id="@4", project="task")])
    after = navigator_rows([_pane("%44", "0", "renamed", window_id="@4", project="task")])
    assert before[0]["kind"] == "window"
    assert before[0]["key"] == after[0]["key"] == "@4"
    assert before[1]["pane_id"] == after[1]["pane_id"] == "%44"
    assert before[0]["nav_group"] == after[0]["nav_group"] == "win:workstation-a:0:@4"
    assert after[0]["project"] == before[0]["project"]
    assert after[0]["window_name"] == "renamed"


def test_enter_prefers_pane_id_over_window_index(monkeypatch):
    from types import SimpleNamespace

    calls = []

    def fake(args, capture=True, timeout=3.0):
        calls.append(list(args))
        if args[:2] == ["list-panes", "-s"]:
            return "%44"
        return ""

    monkeypatch.setattr(navigation.capture, "run_tmux", fake)
    monkeypatch.setattr(navigation.capture, "pane_exists", lambda pane_id: pane_id == "%44")
    monkeypatch.setattr(
        navigation.subprocess, "run",
        lambda args, **_kwargs: calls.append(args[1:]) or SimpleNamespace(returncode=0),
    )

    # The row still says window 9. The move must not target 9.
    assert navigation.open_pane("0", "9", "%44") is True
    assert ["select-window", "-t", "%44"] in calls
    assert ["select-pane", "-t", "%44"] in calls
    assert not any("9" in arg for call in calls for arg in call)
    assert not any(call and call[0] == "send-keys" for call in calls)


def test_moved_pane_still_uses_its_id(monkeypatch):
    from types import SimpleNamespace

    calls = []

    def fake(args, capture=True, timeout=3.0):
        calls.append(list(args))
        if args[0] == "list-panes" and "-s" in args:
            return "%33"
        return ""

    monkeypatch.setattr(navigation.capture, "run_tmux", fake)
    monkeypatch.setattr(navigation.capture, "pane_exists", lambda pane_id: True)
    monkeypatch.setattr(
        navigation.subprocess, "run",
        lambda args, **_kwargs: calls.append(args[1:]) or SimpleNamespace(returncode=0),
    )
    assert navigation.focus_local_pane("0", "%33") is True
    assert calls[1] == ["select-window", "-t", "%33"]
    assert calls[2] == ["select-pane", "-t", "%33"]


def test_stale_pane_does_not_move(monkeypatch):
    calls = []
    monkeypatch.setattr(navigation.capture, "run_tmux", lambda args, capture=True, timeout=3.0: calls.append(list(args)) or "")
    monkeypatch.setattr(navigation.capture, "pane_exists", lambda pane_id: False)
    assert navigation.focus_local_pane("0", "%999") is False
    assert not any(call[0] in ("select-window", "select-pane", "send-keys") for call in calls)


def test_focus_reports_tmux_selection_failure(monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr(navigation, "_panes_in_session", lambda _session: {"%33"})
    monkeypatch.setattr(navigation.capture, "pane_exists", lambda _pane_id: True)
    for failed_command in ("select-window", "select-pane"):
        calls = []

        def run(args, *, stdin, stdout, stderr, timeout, check):
            calls.append(args[1])
            return SimpleNamespace(returncode=1 if args[1] == failed_command else 0)

        monkeypatch.setattr(navigation.subprocess, "run", run)
        assert navigation.focus_local_pane("0", "%33") is False
        assert calls == (["select-window"] if failed_command == "select-window" else ["select-window", "select-pane"])


def test_focus_handles_tmux_selection_error(monkeypatch):
    monkeypatch.setattr(navigation, "_panes_in_session", lambda _session: {"%33"})
    monkeypatch.setattr(navigation.capture, "pane_exists", lambda _pane_id: True)

    def fail(*_args, **_kwargs):
        raise OSError("tmux unavailable")

    monkeypatch.setattr(navigation.subprocess, "run", fail)
    assert navigation.focus_local_pane("0", "%33") is False


def test_window_enter_goes_to_that_windows_active_pane(monkeypatch):
    from types import SimpleNamespace

    calls = []

    def fake(args, capture=True, timeout=3.0):
        calls.append(list(args))
        if args[0] == "list-panes" and "-s" not in args:
            return "%51\t0\n%52\t1"
        if args[0] == "list-panes":
            return "%51\n%52"
        return ""

    monkeypatch.setattr(navigation.capture, "run_tmux", fake)
    monkeypatch.setattr(navigation.capture, "pane_exists", lambda pane_id: True)
    monkeypatch.setattr(
        navigation.subprocess, "run",
        lambda args, **_kwargs: calls.append(args[1:]) or SimpleNamespace(returncode=0),
    )
    assert navigation.focus_window("0", "1") is True
    assert ["select-window", "-t", "%52"] in calls
    assert ["select-pane", "-t", "%52"] in calls


def test_gone_window_does_not_move(monkeypatch):
    calls = []
    monkeypatch.setattr(navigation.capture, "run_tmux", lambda args, capture=True, timeout=3.0: calls.append(list(args)) or "")
    assert navigation.focus_window("0", "9") is False
    assert not any(call[0] in ("select-window", "select-pane") for call in calls)


def test_smart_w_binding_does_not_look_up_a_window_name():
    args = keybind.binding_args("choose-tree -Zw", tower_cmd="/opt/tower")
    text = " ".join(args)
    assert "if-shell" in args
    assert "@tmux_agent_tower_pane" in text
    assert "/opt/tower" not in text
    assert "tower --focus" not in text
    assert args[-1] == "choose-tree -Zw"
    assert "window_name" not in text
    assert "choose-tree" in text


def test_project_list_is_the_default_and_attention_does_not_reorder_it(tmp_path, monkeypatch):
    from tmux_agent_tower.ui import tower as tower_module

    monkeypatch.setattr(tower_module, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(tower_module, "CONFIG_DIR", tmp_path / "config")
    monkeypatch.setattr(tower_module, "HOST_FILE", tmp_path / "config" / "host")
    monkeypatch.setattr(tower_module, "REMOTE_HOSTS_FILE", tmp_path / "config" / "remote-hosts.txt")
    tower = tower_module.Tower("0", own_pane_id="%6")
    assert tower.navigator_mode is False
    waiting = _pane("%6", "0", "main", window_id="@1", project="SamplePortal", status="IDLE")
    waiting["attention"] = "approval_required"
    idle = _pane("%7", "1", "agents", window_id="@2", project="Other", status="IDLE")
    tower.rows = [waiting, idle]
    _apply_user_work_view(tower)
    assert [row["pane_id"] for row in tower.visible_rows if row.get("kind") == "pane"] == ["%6", "%7"]
    assert [row["kind"] for row in tower.visible_rows] == ["other_section", "pane", "pane"]
    assert tower.visible_rows[0]["display_name"] == "자동으로 찾은 작업"

    tower.toggle_attention()
    assert [row["kind"] for row in tower.visible_rows] == ["pane", "pane"]
    assert [row["key"] for row in tower.visible_rows] == ["%6", "%7"]
    tower.toggle_attention()
    assert [row["pane_id"] for row in tower.visible_rows if row.get("kind") == "pane"] == ["%6", "%7"]


def test_home_prioritizes_agent_work_and_collapses_discovered_shells_by_default(tmp_path, monkeypatch):
    from tmux_agent_tower.ui import tower as tower_module

    monkeypatch.setattr(tower_module, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(tower_module, "CONFIG_DIR", tmp_path / "config")
    monkeypatch.setattr(tower_module, "HOST_FILE", tmp_path / "config" / "host")
    monkeypatch.setattr(tower_module, "REMOTE_HOSTS_FILE", tmp_path / "config" / "remote-hosts.txt")
    tower = tower_module.Tower("0")
    agent = _pane("%agent", "0", "Agent", window_id="@agent", project="Agent task", agent="OpenCode")
    shell = _pane("%shell", "1", "Shell", window_id="@shell", project="Terminal", agent="Shell")
    tower.rows = [agent, shell]
    _apply_user_work_view(tower)

    sections = [row for row in tower.visible_rows if row.get("kind") == "other_section"]
    assert [row["display_name"] for row in sections] == [
        "자동으로 찾은 작업", "기타 터미널 · 자동 발견",
    ]
    assert sections[0]["collapsed"] is False
    assert sections[1]["collapsed"] is True
    assert "%agent" in [row.get("pane_id") for row in tower.visible_rows]
    assert "%shell" not in [row.get("pane_id") for row in tower.visible_rows]

    tower.selected = tower.visible_rows.index(sections[1])
    tower.toggle_folder("__unfiled__")
    assert "%shell" in [row.get("pane_id") for row in tower.visible_rows]


def test_enter_reports_why_a_selected_remote_window_cannot_open(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from tmux_agent_tower.i18n import t
    from tmux_agent_tower.ui import tower as tower_module

    row = {"kind": "window", "remote": True, "window_id": "", "key": "remote"}
    tower = SimpleNamespace(visible_rows=[row], selected=0, notice="")
    tower_module._open_selected_row(None, tower)
    assert tower.notice == t("enter.remote_readonly")


def test_selected_item_enter_hint_explains_expand_open_and_disabled_actions():
    from tmux_agent_tower.i18n import ko
    from tmux_agent_tower.ui.tower import _selected_enter_hint

    assert _selected_enter_hint({"kind": "folder"}) == ko.STRINGS["hint.enter_expand"]
    assert _selected_enter_hint({"kind": "window_asset"}) == ko.STRINGS["hint.enter_expand"]
    assert _selected_enter_hint({"kind": "pane", "pane_id": "%1"}) == ko.STRINGS["hint.enter_conversation"]
    assert _selected_enter_hint({"kind": "window", "window_id": "@1"}) == ko.STRINGS["hint.enter_window"]
    assert _selected_enter_hint({"kind": "window", "remote": True}) == ko.STRINGS["hint.enter_unavailable"]


def test_home_visually_clusters_legacy_ungrouped_tasks_by_project(tmp_path, monkeypatch):
    from tmux_agent_tower.ui import tower as tower_module

    monkeypatch.setattr(tower_module, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(tower_module, "CONFIG_DIR", tmp_path / "config")
    monkeypatch.setattr(tower_module, "HOST_FILE", tmp_path / "config" / "host")
    monkeypatch.setattr(tower_module, "REMOTE_HOSTS_FILE", tmp_path / "config" / "remote-hosts.txt")
    tower = tower_module.Tower("0")
    first = _pane("%20", "0", "old-window-a", window_id="@20", project="SampleProject")
    second = _pane("%21", "1", "old-window-b", window_id="@21", project="SampleProject")
    first.update(target_id="a", display_name="PM", role="orchestrator")
    second.update(target_id="b", display_name="구현", role="builder")
    tower.rows = [first, second]
    _apply_user_work_view(tower)

    assert [row["kind"] for row in tower.visible_rows] == ["other_section", "pane", "pane"]
    panes = [row for row in tower.visible_rows if row.get("kind") == "pane"]
    assert [(row["display_name"], row["window_name"]) for row in panes] == [
        ("PM", "old-window-a"), ("구현", "old-window-b"),
    ]
    assert all(row.get("kind") != "work_group" for row in tower.visible_rows)


def test_other_tower_runtime_is_not_a_user_task_but_remains_in_terminal_structure(tmp_path, monkeypatch):
    from tmux_agent_tower.ui import tower as tower_module

    monkeypatch.setattr(tower_module, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(tower_module, "CONFIG_DIR", tmp_path / "config")
    monkeypatch.setattr(tower_module, "HOST_FILE", tmp_path / "config" / "host")
    monkeypatch.setattr(tower_module, "REMOTE_HOSTS_FILE", tmp_path / "config" / "remote-hosts.txt")
    tower = tower_module.Tower("0")
    runtime = _pane("%30", "0", "tower-window", window_id="@30", project="Tower")
    runtime.update(target_id="tower", tower_runtime=True, agent="Shell")
    task = _pane("%31", "0", "task-window", window_id="@30", project="SampleProject")
    task.update(target_id="task", tower_runtime=False)
    tower.rows = [runtime, task]

    _apply_user_work_view(tower)
    assert [row["pane_id"] for row in tower.visible_rows if row.get("pane_id")] == ["%31"]

    tower.toggle_navigator()
    assert [row.get("pane_id") for row in tower.visible_rows if row.get("pane_id")] == ["%30", "%31"]


def test_remote_only_default_uses_user_work_rows_and_physical_tree_is_explicit(tmp_path, monkeypatch):
    from tmux_agent_tower.ui import tower as tower_module

    monkeypatch.setattr(tower_module, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(tower_module, "CONFIG_DIR", tmp_path / "config")
    monkeypatch.setattr(tower_module, "HOST_FILE", tmp_path / "config" / "host")
    monkeypatch.setattr(tower_module, "REMOTE_HOSTS_FILE", tmp_path / "config" / "remote-hosts.txt")
    tower = tower_module.Tower("0")
    remote = _pane(
        "%42", "0", "work", window_id="@8", project="SamplePortal",
        host="remote-host", remote=True,
    )
    remote["target_id"] = "target-remote"
    tower.rows = [remote]

    assert tower.view_mode == tower_module.USER_WORK_VIEW
    _apply_user_work_view(tower)
    assert [row["kind"] for row in tower.visible_rows] == ["other_section", "pane"]

    tower.toggle_navigator()
    assert tower.view_mode == tower_module.TERMINAL_STRUCTURE_VIEW
    assert [row["kind"] for row in tower.visible_rows] == ["window", "pane"]

    tower.toggle_navigator()
    assert tower.view_mode == tower_module.USER_WORK_VIEW
    assert [row["kind"] for row in tower.visible_rows] == ["other_section", "pane"]


def test_collapse_hides_children_and_search_keeps_the_window(tmp_path, monkeypatch):
    from tmux_agent_tower.control.actions import enter_intent
    from tmux_agent_tower.ui import render, tower as tower_module

    monkeypatch.setattr(tower_module, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(tower_module, "CONFIG_DIR", tmp_path / "config")
    monkeypatch.setattr(tower_module, "HOST_FILE", tmp_path / "config" / "host")
    monkeypatch.setattr(tower_module, "REMOTE_HOSTS_FILE", tmp_path / "config" / "remote-hosts.txt")
    tower = tower_module.Tower("0")
    tower.navigator_mode = True
    tower.rows = [
        _pane("%1", "0", "main", window_id="@1", project="Other"),
        _pane("%2", "1", "agents", window_id="@2", project="SamplePortal", agent="Cursor"),
        _pane("%3", "1", "agents", pane_index="1", window_id="@2", project="Marketplace", agent="Codex"),
    ]
    tower._apply_filter()
    assert enter_intent(tower.visible_rows[0]) == "window"
    assert tower.control_key() == "@1"
    tower.collapse_selected()
    assert [row["key"] for row in tower.visible_rows if row["kind"] == "pane"] == ["%2", "%3"]
    assert tower.visible_rows[0]["collapsed"] is True
    tower.expand_selected()
    assert "%1" in [row.get("pane_id") for row in tower.visible_rows]

    tower.set_filter("SamplePortal")
    shown = tower.visible_rows
    assert [row["key"] for row in shown] == ["@2", "%2"]
    assert shown[0]["host"] == "workstation-a"
    assert "%3" not in [row.get("pane_id") for row in shown]
    tower.set_filter("agents")
    assert tower.visible_rows == []  # Window labels are advanced terminal structure, not task search fields.

    summary = render.window_summary(
        [
            {"pane_id": "%1", "attention": "approval_required", "status": "IDLE"},
            {"pane_id": "%2", "result_state": "ready", "status": "IDLE"},
            {"pane_id": "%3", "status": "WORKING"},
            {"pane_id": "%4", "status": "IDLE"},
        ]
    )
    assert summary == "!1 ✓1 ●1"
    narrow = render.list_row_parts(
        {"kind": "pane", "project": "쥬포탈", "task_name": "로그인 수정", "agent": "Cursor", "status": "WORKING", "pane_id": "%2", "path": "/tmp/secret"},
        58,
        lambda key: "작업 중",
        "3m",
    )
    wide = render.list_row_parts(
        {"kind": "pane", "project": "쥬포탈", "task_name": "로그인 수정", "agent": "Cursor", "status": "WORKING", "pane_id": "%2", "guide": "│  ├─ "},
        100,
        lambda key: "작업 중",
        "3m",
    )
    assert narrow["agent"] == ""
    assert narrow["project"] == "로그인 수정"
    assert narrow["task"] == ""
    assert "3m" not in narrow["badge"]
    assert render.agent_status_line("Cursor", "● 작업 중") == "Cursor  ● 작업 중"
    assert "%2" not in "".join(narrow.values())
    assert "/tmp/secret" not in "".join(narrow.values())
    assert "%2" not in "".join(wide.values())
    assert wide["agent"] == "Cursor"
    assert wide["project"] == "로그인 수정"
    assert wide["task"] == ""
    assert "3m" in wide["badge"]
    assert render.display_width("쥬포탈") == 6
    assert render.display_width("├─ ") == render.display_width("└─ ")


def test_live_session_tree_is_readable_and_does_not_focus(tmux_test_safety):
    from tmux_test_safety import run_isolated_tmux
    from tmux_agent_tower.control.actions import enter_intent

    session = "navigator-live-fixture"
    created = run_isolated_tmux("new-session", "-d", "-s", session, "sleep", "60")
    assert created.returncode == 0, created.stderr
    before = run_isolated_tmux("list-windows", "-t", session, "-F", "#{window_id}")
    listed = run_isolated_tmux(
        "list-panes",
        "-s",
        "-t", session,
        "-F",
        "#{window_id}\t#{window_index}\t#{window_name}\t#{pane_id}\t#{pane_index}\t#{pane_current_command}",
    )
    panes = []
    for line in listed.stdout.splitlines():
        window_id, index, name, pane_id, pane_index, command = line.split("\t")
        panes.append(
            {
                "kind": "pane",
                "key": pane_id,
                "pane_id": pane_id,
                "session": session,
                "window_id": window_id,
                "window_index": index,
                "window_name": name,
                "pane_index": pane_index,
                "host": "workstation-a",
                "project": command or pane_id,
                "agent": "Shell",
                "status": "IDLE",
                "remote": False,
            }
        )
    rows = navigator_rows(panes)
    windows = [row for row in rows if row["kind"] == "window"]
    assert windows
    assert all(row["window_id"].startswith("@") for row in windows)
    assert enter_intent(windows[0]) == "window"
    pane = next(row for row in rows if row["kind"] == "pane")
    assert enter_intent(pane) == "control"
    assert pane["guide"]
    after = run_isolated_tmux("list-windows", "-t", session, "-F", "#{window_id}")
    assert after.stdout == before.stdout


def _fold_tower(tmp_path, monkeypatch):
    from tmux_agent_tower.ui import tower as tower_module

    monkeypatch.setattr(tower_module, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(tower_module, "CONFIG_DIR", tmp_path / "config")
    monkeypatch.setattr(tower_module, "HOST_FILE", tmp_path / "config" / "host")
    monkeypatch.setattr(tower_module, "REMOTE_HOSTS_FILE", tmp_path / "config" / "remote-hosts.txt")
    tower = tower_module.Tower("0")
    tower.navigator_mode = True
    return tower


def _select_window(tower, window_id):
    for index, row in enumerate(tower.visible_rows):
        if row.get("kind") == "window" and row.get("window_id") == window_id:
            tower.selected = index
            return row["key"]
    raise AssertionError(window_id)


def _select_window_asset(tower, window_id):
    for index, row in enumerate(tower.visible_rows):
        if row.get("kind") == "window_asset" and row.get("window_id") == window_id:
            tower.selected = index
            return row["window_ref"]
    raise AssertionError(window_id)


def test_expand_uses_the_same_window_key_after_refresh(tmp_path, monkeypatch):
    tower = _fold_tower(tmp_path, monkeypatch)
    panes = [
        _pane("%14", "1", "bash", window_id="@9", host="workstation-b", session="0"),
        _pane("%17", "1", "bash", pane_index="1", window_id="@9", host="workstation-a", session="0"),
        _pane("%19", "2", "bash", window_id="@10", host="workstation-a", session="0"),
    ]
    for pane in panes:
        pane["tmux_host"] = "workstation-a"
        pane["execution_host"] = pane["host"]
    panes[0]["transport"] = "ssh"
    tower.rows = panes
    tower._apply_filter()
    windows = [row for row in tower.visible_rows if row["kind"] == "window"]
    assert [row["key"] for row in windows] == ["workstation-a:0:@9", "workstation-a:0:@10"]
    assert [row["window_id"] for row in windows] == ["@9", "@10"]

    stored = _select_window(tower, "@9")
    tower.collapse_selected()
    assert tower.collapsed == {stored}
    assert "%14" not in [row.get("pane_id") for row in tower.visible_rows]
    assert "%17" not in [row.get("pane_id") for row in tower.visible_rows]
    assert "%19" in [row.get("pane_id") for row in tower.visible_rows]

    refreshed = []
    for pane in reversed(panes):
        pane = dict(pane)
        pane["window_name"] = "renamed"
        pane["host"] = "peer-box"
        pane["execution_host"] = "peer-box"
        refreshed.append(pane)
    tower.rows = refreshed
    tower._apply_filter(stored)
    assert tower.collapsed == {stored}
    assert _select_window(tower, "@9") == stored
    assert [row.get("pane_id") for row in tower.visible_rows if row["kind"] == "pane"] == ["%19"]
    tower.expand_selected()
    assert stored not in tower.collapsed
    assert {row.get("pane_id") for row in tower.visible_rows if row["kind"] == "pane"} == {"%14", "%17", "%19"}


def test_search_clear_keeps_fold_and_attention_uses_work_list(tmp_path, monkeypatch):
    tower = _fold_tower(tmp_path, monkeypatch)
    tower.navigator_mode = False
    panes = [
        _pane("%14", "1", "bash", window_id="@9", project="SamplePortal", host="workstation-b"),
        _pane("%17", "1", "bash", pane_index="1", window_id="@9", project="Other", host="workstation-a"),
        _pane("%19", "2", "agents", window_id="@10", project="Elsewhere", host="workstation-a"),
    ]
    for pane in panes:
        pane["tmux_host"] = "workstation-a"
    tower.rows = panes
    _apply_user_work_view(tower)
    stored = _select_window_asset(tower, "@9")
    tower.collapse_selected()

    tower.set_filter("SamplePortal")
    assert "%14" in [row.get("pane_id") for row in tower.visible_rows]
    tower.clear_filter()
    assert tower.folders.window(stored)["collapsed"] is True
    assert "%14" not in [row.get("pane_id") for row in tower.visible_rows]

    tower.toggle_attention()
    assert all(row.get("kind") != "window" for row in tower.visible_rows)
    tower.toggle_attention()
    assert tower.folders.window(stored)["collapsed"] is True
    assert [row.get("pane_id") for row in tower.visible_rows if row.get("pane_id")] == ["%19"]


def test_rename_action_on_window_renames_active_tower_work_item(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from tmux_agent_tower.ui import tower as tower_module

    tower = _fold_tower(tmp_path, monkeypatch)
    panes = [
        _pane("%41", "0", "shell", active=False, window_id="@4", project="old-task"),
        _pane("%42", "0", "shell", active=True, window_id="@4", project="active-task"),
    ]
    panes[1]["pane_pid"] = "4242"
    tower.rows = panes
    tower.navigator_mode = True
    tower._apply_filter()
    tower.selected = next(i for i, row in enumerate(tower.visible_rows) if row["kind"] == "window")
    monkeypatch.setattr(tower_module, "prompt_text", lambda *args, **kwargs: "new-task")
    monkeypatch.setattr(
        tower_module, "run_list_picker",
        lambda *args, **kwargs: SimpleNamespace(cancelled=False, selected_keys=set()),
    )
    monkeypatch.setattr(tower, "load", lambda: None)

    tower.rename_selected(None)

    assert tower.overrides.get_task_name("%42", "0", "4242") == "new-task"
    assert tower.overrides.get_project("%42", "0", "4242") is None
    panes[1]["task_name"] = "new-task"
    window = next(row for row in navigator_rows(panes) if row["kind"] == "window")
    assert window["project"] == "active-task"


def test_rename_action_on_pane_changes_only_tower_work_name(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from tmux_agent_tower.ui import tower as tower_module

    tower = _fold_tower(tmp_path, monkeypatch)
    pane = _pane("%52", "0", "terminal", window_id="@5", project="old-name")
    pane["pane_pid"] = "5252"
    tower.rows = [pane]
    tower.navigator_mode = False
    _apply_user_work_view(tower)
    tower.selected = next(i for i, row in enumerate(tower.visible_rows) if row.get("kind") == "pane")
    monkeypatch.setattr(tower_module, "prompt_text", lambda *args, **kwargs: "new-name")
    monkeypatch.setattr(
        tower_module, "run_list_picker",
        lambda *args, **kwargs: SimpleNamespace(cancelled=False, selected_keys=set()),
    )
    monkeypatch.setattr(tower, "load", lambda: None)

    tower.rename_selected(None)

    assert tower.overrides.get_task_name("%52", "0", "5252") == "new-name"
    assert tower.overrides.get_project("%52", "0", "5252") is None


def test_rename_opt_in_syncs_only_the_selected_local_pane(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from tmux_agent_tower.ui import tower as tower_module
    from tmux_agent_tower.tmux import structure

    tower = _fold_tower(tmp_path, monkeypatch)
    pane = _pane("%53", "0", "terminal", window_id="@5", project="old-name")
    pane.update(pane_pid="5353", tmux_host=tower.local_host, transport="local")
    tower.rows = [pane]
    tower.visible_rows = [pane]
    monkeypatch.setattr(tower_module, "prompt_text", lambda *args, **kwargs: "new-name")
    seen = {}
    monkeypatch.setattr(
        tower_module, "run_list_picker",
        lambda *args, **kwargs: seen.update(checked=kwargs["checked"], preamble=kwargs["preamble"])
        or SimpleNamespace(cancelled=False, selected_keys={"sync"}),
    )
    monkeypatch.setattr(
        structure, "pane_title_identity",
        lambda session, pane_id: {"session": session, "pane_id": pane_id, "pane_pid": "5353", "pane_title": "Agent title"},
    )
    title_writes = []
    monkeypatch.setattr(
        structure, "rename_pane",
        lambda session, pane_id, title, **kwargs: title_writes.append((session, pane_id, title, kwargs))
        or structure.StructureResult(True, pane_id=pane_id),
    )
    monkeypatch.setattr(tower, "load", lambda: None)

    tower.rename_selected(None)

    assert seen["checked"] == set()
    assert title_writes == [("0", "%53", "new-name", {"expected_pane_pid": "5353"})]
    assert tower.overrides.get_task_name("%53", "0", "5353") == "new-name"
    assert tower.overrides.get_task_title_sync("%53", "0", "5353")["enabled"] is True
    assert tower.notice == "Tower 이름과 tmux 화면 제목을 변경했습니다."


def test_remote_ssh_task_never_offers_or_applies_tmux_title_sync(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from tmux_agent_tower.ui import tower as tower_module
    from tmux_agent_tower.tmux import structure

    tower = _fold_tower(tmp_path, monkeypatch)
    pane = _pane("%54", "0", "ssh", window_id="@5", project="remote-task")
    pane.update(pane_pid="5454", transport="ssh", execution_host="ASUS", tmux_host=tower.local_host)
    tower.rows = [pane]
    tower.visible_rows = [pane]
    seen = {}
    monkeypatch.setattr(tower_module, "prompt_text", lambda *args, **kwargs: "ASUS 작업")
    monkeypatch.setattr(
        tower_module, "run_list_picker",
        lambda *args, **kwargs: seen.update(items=kwargs.get("items"))
        or SimpleNamespace(cancelled=False, selected_keys={"sync"}),
    )
    monkeypatch.setattr(
        structure, "pane_title_identity",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("remote title control attempted")),
    )
    monkeypatch.setattr(tower, "load", lambda: None)

    tower.rename_selected(None)

    assert seen == {}
    assert tower.overrides.get_task_name("%54", "0", "5454") == "ASUS 작업"
    assert tower.notice == "Tower 작업 이름을 변경했습니다."


def test_reset_task_name_returns_to_auto_detection(tmp_path, monkeypatch):
    from tmux_agent_tower.ui import tower as tower_module

    tower = _fold_tower(tmp_path, monkeypatch)
    pane = _pane("%55", "0", "terminal", window_id="@5", project="detected-name")
    pane.update(pane_pid="5555", task_name="custom-name", display_name="custom-name", suggested_name="detected-name")
    tower.rows = [pane]
    tower.visible_rows = [pane]
    tower.overrides.set_task_name("%55", "custom-name", "0", "5555")
    monkeypatch.setattr(tower_module, "prompt_text", lambda *args, **kwargs: "")
    monkeypatch.setattr(tower, "load", lambda: None)

    tower.rename_selected(None)

    assert tower.overrides.get_task_name("%55", "0", "5555") is None
    assert tower.notice == "자동 감지한 작업 이름으로 복원했습니다."


def test_reset_does_not_overwrite_an_agent_changed_pane_title(tmp_path, monkeypatch):
    from tmux_agent_tower.ui import tower as tower_module
    from tmux_agent_tower.tmux import structure

    tower = _fold_tower(tmp_path, monkeypatch)
    pane = _pane("%56", "0", "terminal", window_id="@5", project="detected-name")
    pane.update(pane_pid="5656", task_name="custom-name", display_name="custom-name", suggested_name="detected-name")
    tower.rows = [pane]
    tower.visible_rows = [pane]
    tower.overrides.set_task_name("%56", "custom-name", "0", "5656")
    tower.overrides.set_task_title_sync(
        "%56", "0", "5656", enabled=True, original_title="Original", last_title="custom-name",
    )
    monkeypatch.setattr(tower_module, "prompt_text", lambda *args, **kwargs: "")
    monkeypatch.setattr(
        structure, "pane_title_identity",
        lambda *args: {"session": "0", "pane_id": "%56", "pane_pid": "5656", "pane_title": "Agent title"},
    )
    monkeypatch.setattr(
        structure, "rename_pane",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("Agent title must be preserved")),
    )
    monkeypatch.setattr(tower, "load", lambda: None)

    tower.rename_selected(None)

    assert tower.overrides.get_task_name("%56", "0", "5656") is None
    assert tower.notice == "자동 감지한 작업 이름으로 복원했습니다."


def test_edit_menu_uses_task_facing_actions_and_hides_terminal_names(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from tmux_agent_tower.ui import tower as tower_module

    tower = _fold_tower(tmp_path, monkeypatch)
    pane = _pane("%62", "0", "main", window_id="@6", project="detected-project")
    pane.update(pane_pid="6262", auto_project="detected-project")
    tower.rows = [pane]
    tower.visible_rows = [pane]
    seen = {}

    def choose_project(_screen, title, items, **_kwargs):
        seen["title"] = title
        seen["items"] = items
        return SimpleNamespace(cancelled=True, selected_key=None)

    monkeypatch.setattr(tower_module, "run_list_picker", choose_project)
    monkeypatch.setattr(tower, "load", lambda: None)

    tower.edit_selected(None)

    assert seen["title"] == "작업 편집"
    assert [item[0] for item in seen["items"]] == [
        "rename", "agent", "role", "project", "more", "cancel",
    ]
    assert not any("pane" in label.lower() or "window" in label.lower() for _key, label in seen["items"])
    assert tower.overrides.get_task_name("%62", "0", "6262") is None


def test_selected_detail_keeps_tmux_identity_under_advanced_heading(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from tmux_agent_tower.ui import tower as tower_module

    _fold_tower(tmp_path, monkeypatch)
    fields = tower_module._build_detail_fields(
        SimpleNamespace(config={"show_activity": False}),
        {
            "project": "작업",
            "agent": "Codex",
            "status": "WORKING",
            "attention": "none",
            "result_state": "none",
            "pane_title": "Codex task",
            "path": "/work/task",
            "host": "workstation-a",
            "execution_host": "workstation-a",
            "tmux_host": "LOCAL",
            "transport": "local",
            "session": "0",
            "window_index": 2,
            "window_name": "main",
            "window_id": "@4",
            "pane_id": "%42",
            "pane_index": 1,
            "pane_active": True,
        },
        advanced=True,
    )
    detail = [value for label, value in fields if label != "고급 정보"]
    values = dict(fields)
    labels = [label for label, _value in fields]
    assert values["고급 정보"] == ""
    assert labels.index("고급 정보") < labels.index("Session")
    assert labels.index("고급 정보") < labels.index("Window ID")
    assert labels.index("고급 정보") < labels.index("Pane ID")
    assert values["실행 위치"] == "workstation-a"
    assert values["연결 방식"] == "로컬"
    assert values["Session"] == "0"
    assert values["작업 묶음"] == "2: main"
    assert values["Pane ID"] == "%42"
    assert values["Window ID"] == "@4"
    assert values["Pane 순서"] == "1"
    assert values["활성 상태"] == "예"
    assert values["경로"] == "/work/task"
    assert "터미널 위치" not in values
    assert sum(value == "0" for value in detail) == 1
    assert sum(value == "2: main" for value in detail) == 1
    assert sum(value == "%42" for value in detail) == 1
