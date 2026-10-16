from types import SimpleNamespace

from tmux_agent_tower.i18n import t
from tmux_agent_tower.server import httpapi, webui
from tmux_agent_tower.control.actions import apply_identity_edit
from tmux_agent_tower.state.bindings import ProjectBindingStore
from tmux_agent_tower.state.overrides import OverrideStore
from tmux_agent_tower.ui import control_view, render
from tmux_agent_tower.ui.tower import Tower


def _identity(tower, *, project="ExampleProject", command="codex", title="", session="0", pane_pid="123"):
    return tower._identity(
        "%1", command, title, command, (), project, project, "workstation-b", "(이름 없음)",
        session=session, pane_pid=pane_pid,
    )


def test_auto_name_uses_project_role_agent_and_terminal_fallbacks():
    label = lambda role: {"builder": "구현", "qa": "확인"}[role]
    suggest = render.suggest_task_name
    assert suggest("ExampleProject", "builder", "Codex", "", "workstation-b", "(이름 없음)", label, "터미널", "새 작업") == "ExampleProject · 구현"
    assert suggest("ExampleProject", None, "Codex", "", "workstation-b", "(이름 없음)", label, "터미널", "새 작업") == "ExampleProject · Codex"
    assert suggest("ExampleProject", None, "Shell", "", "workstation-b", "(이름 없음)", label, "터미널", "새 작업") == "ExampleProject · 터미널"
    assert suggest("(이름 없음)", None, "Shell", "%15", "workstation-b", "(이름 없음)", label, "터미널", "새 작업") == "터미널"


def test_legacy_titles_infer_project_task_and_role_without_persisting():
    assert render.title_identity("Fix checkout flow | SampleProject", "workstation-a") == (
        "SampleProject", "Fix checkout flow", None,
    )
    assert render.title_identity("SampleProject | PM", "workstation-a") == ("SampleProject", None, "orchestrator")
    assert render.title_identity("Review checkout flow | devuser42", "workstation-a") == (
        None, "Review checkout flow", None,
    )
    assert render.title_identity("workstation-a", "workstation-a") == (None, None, None)


def test_legacy_ssh_title_becomes_task_name_and_role(tmp_path):
    tower = Tower.__new__(Tower)
    tower.overrides = OverrideStore(tmp_path / "overrides.json")
    identity = tower._identity(
        "%8", "ssh", "Fix checkout flow | SampleProject", "ssh example-host", (),
        None, None, "workstation-a", "(이름 없음)", session="0", pane_pid="12",
    )
    assert identity["project"] == "SampleProject"
    assert identity["display_name"] == "Fix checkout flow"
    assert identity["role"] is None

    role = tower._identity(
        "%9", "ssh", "SampleProject | PM", "ssh example-host", (), None, None, "workstation-a",
        "(이름 없음)", session="0", pane_pid="13",
    )
    assert role["project"] == "SampleProject"
    assert role["display_name"] == "SampleProject · 조율"
    assert role["role"] == "orchestrator"


def test_auto_role_name_tracks_role_and_legacy_name_is_a_user_name(tmp_path):
    tower = Tower.__new__(Tower)
    tower.overrides = OverrideStore(tmp_path / "overrides.json")

    assert _identity(tower)["display_name"] == "ExampleProject · Codex"
    tower.overrides.set_role("%1", "builder", "0", "123")
    assert _identity(tower)["display_name"] == "ExampleProject · 구현"

    tower.overrides.set_task_name("%1", "기능 구현", "0", "123")
    tower.overrides.set_role("%1", "qa", "0", "123")
    identity = _identity(
        tower, project="Tower", command="claude", title="Agent changed title | AnotherProject",
    )
    assert identity["display_name"] == "기능 구현"
    assert identity["name_origin"] == "user"

    reopened = OverrideStore(tmp_path / "overrides.json")
    assert reopened.get_task_name("%1", "0", "123") == "기능 구현"
    assert reopened.get_task_name_origin("%1", "0", "123") == "user"
    restarted = Tower.__new__(Tower)
    restarted.overrides = reopened
    assert _identity(
        restarted, project="AnotherProject", command="opencode", title="Agent changed it | Project",
    )["display_name"] == "기능 구현"


def test_tower_runtime_identity_is_detected_without_window_name_matching(tmp_path):
    tower = Tower.__new__(Tower)
    tower.overrides = OverrideStore(tmp_path / "overrides.json")
    identity = tower._identity(
        "%1", "python3", "python3", "python3 -m tmux_agent_tower.main", (),
        None, None, "workstation-a", "(이름 없음)", session="0", pane_pid="123",
    )
    assert identity["tower_runtime"] is True

    regular = tower._identity(
        "%2", "bash", "Tower setup shell", "bash", (), None, None,
        "workstation-a", "(이름 없음)", session="0", pane_pid="124",
    )
    assert regular["tower_runtime"] is False


def test_name_fallback_never_uses_physical_ids():
    label = lambda _role: ""
    for bad_title in ("%15", "@2", "Session 0"):
        name = render.suggest_task_name(
            "(이름 없음)", None, None, bad_title, "workstation-b", "(이름 없음)", label, "터미널", "새 작업"
        )
        assert name == "새 작업"
        assert "%" not in name and "@" not in name and "Session" not in name


def test_duplicate_names_remain_distinct_by_stable_pane_key(tmp_path):
    store = OverrideStore(tmp_path / "overrides.json")
    store.set_task_name("%1", "ExampleProject · 구현", "0", "123")
    store.set_task_name("%2", "ExampleProject · 구현", "0", "456")
    assert store.get_task_name("%1", "0", "123") == store.get_task_name("%2", "0", "456")
    assert store.get_task_name("%1", "0", "456") is None


def test_default_task_details_hide_tmux_names_and_ids():
    row = {
        "project": "ExampleProject", "display_name": "ExampleProject · 구현", "agent": "Codex",
        "role": "builder", "status": "WORKING", "attention": "none",
        "result_state": "none", "session": "0", "window_index": 2,
        "window_name": "main", "window_id": "@2", "pane_id": "%15",
        "pane_index": 0,
    }
    from tmux_agent_tower.ui import tower as tower_module

    labels = [label for label, _value in tower_module._build_detail_fields(
        SimpleNamespace(config={"show_activity": False}), row
    )]
    assert labels == ["작업 이름", "프로젝트", "에이전트", "역할", "상태", "확인할 일", "결과", "실행 위치"]


def test_default_selected_summary_omits_empty_project_and_role():
    from tmux_agent_tower.ui import tower as tower_module

    lines = tower_module._build_selected_summary(
        SimpleNamespace(config={"show_status_duration": False}),
        {
            "display_name": "터미널", "project": "(이름 없음)", "agent": "Shell",
            "role": None, "status": "IDLE", "attention": "none", "result_state": "none",
            "execution_host": "UNKNOWN",
        },
    )
    assert lines == ["터미널", "Shell · ○ 대기 중"]
    assert not any("이름 없음" in line or "역할 없음" in line for line in lines)


def test_project_rebind_changes_path_and_preserves_result_provider(tmp_path):
    store = ProjectBindingStore(tmp_path / "bindings.json")
    provider = {
        "result_provider_type": "remote_tmux",
        "result_provider_endpoint": "ssh:server",
        "result_provider_session_id": "$9",
        "result_provider_pane_id": "%8",
        "result_provider_pane_pid": "98",
        "result_provider_liveness": "live",
    }
    store.record("%1", "0", "123", "/old/project", "Old", "Codex", result_provider=provider)
    assert store.rebind_project("%1", "0", "123", "/new/project", "New", {"agent": "Codex"})
    binding = store._load()["%1"]
    assert binding["project_path"] == "/new/project"
    assert binding["project_name"] == "New"
    assert binding["result_provider_endpoint"] == "ssh:server"
    assert binding["result_provider_pane_id"] == "%8"
    assert not store.rebind_project("%1", "0", "999", "/wrong", "Wrong")


def test_project_edit_preserves_auto_task_name_and_rebinds_only_project(tmp_path, monkeypatch):
    from tmux_agent_tower.ui import tower as tower_module

    overrides = OverrideStore(tmp_path / "overrides.json")
    bindings = ProjectBindingStore(tmp_path / "bindings.json")
    bindings.record("%1", "0", "123", "/old", "Old", "Codex")
    row = {
        "key": "%1", "pane_id": "%1", "session": "0", "pane_pid": "123",
        "display_name": "기능 구현", "task_name": "기능 구현", "name_origin": "auto",
        "suggested_name": "기능 구현", "remote": False, "offline": False,
        "project": "Old", "agent": "Codex", "transport": "local",
    }
    tower = Tower.__new__(Tower)
    tower.overrides = overrides
    tower.bindings = bindings
    tower.visible_rows = [row]
    tower.selected = 0
    tower.load = lambda: None
    monkeypatch.setattr(tower_module, "STATE_DIR", tmp_path)
    monkeypatch.setattr(
        tower_module, "run_list_picker",
        lambda *_args, **_kwargs: SimpleNamespace(cancelled=False, selected_key="project"),
    )
    monkeypatch.setattr(
        "tmux_agent_tower.ui.workspace_browser.pick_project_for_target",
        lambda *_args, **_kwargs: SimpleNamespace(entry=SimpleNamespace(path="/new", name="New")),
    )

    tower.edit_selected(None)

    binding = bindings._load()["%1"]
    assert binding["project_path"] == "/new"
    assert binding["project_name"] == "New"
    assert overrides.get_task_name("%1", "0", "123") == "기능 구현"
    assert overrides.get_task_name_origin("%1", "0", "123") == "auto"


def test_agent_edit_preserves_auto_name_and_role_edit_recomputes_it(tmp_path):
    overrides = OverrideStore(tmp_path / "overrides.json")
    overrides.set_task_name("%1", "ExampleProject · Codex", "0", "123", origin="auto")

    class FakeTower:
        def __init__(self):
            self.overrides = overrides
            self.rows = []

        def load(self):
            agent = overrides.get_agent("%1", "0", "123") or "Codex"
            role = overrides.get_role("%1", "0", "123")
            suggested = render.suggest_task_name(
                "ExampleProject", role, agent, "", "workstation-b", "(이름 없음)",
                lambda value: {"builder": "구현", "qa": "확인"}[value], "터미널", "새 작업",
            )
            saved = overrides.get_task_name("%1", "0", "123")
            self.rows = [{
                "key": "%1", "session": "0", "pane_pid": "123", "remote": False,
                "project": "ExampleProject", "agent": agent, "role": role,
                "task_name": saved or suggested, "display_name": saved or suggested,
                "name_origin": overrides.get_task_name_origin("%1", "0", "123") or "auto",
                "suggested_name": suggested,
            }]

    tower = FakeTower()
    assert apply_identity_edit(tower, "%1", {"agent": "Cursor"}) == (True, "")
    assert overrides.get_task_name("%1", "0", "123") == "ExampleProject · Codex"
    assert overrides.get_task_name_origin("%1", "0", "123") == "auto"

    assert apply_identity_edit(tower, "%1", {"role": "builder"}) == (True, "")
    assert overrides.get_task_name("%1", "0", "123") == "ExampleProject · 구현"
    assert overrides.get_task_name_origin("%1", "0", "123") == "auto"


def test_status_and_phone_use_the_same_task_identity():
    tower = SimpleNamespace(
        load=lambda: None,
        rows=[{
            "key": "%1", "project": "ExampleProject", "task_name": "ExampleProject · 구현",
            "display_name": "ExampleProject · 구현", "name_origin": "auto",
            "suggested_name": "ExampleProject · 구현", "agent": "Codex", "role": "builder",
            "status": "WORKING", "host": "workstation-b", "execution_host": "workstation-b",
        }],
        session="0",
    )
    pane = httpapi.build_status_payload(tower)["panes"][0]
    assert pane["display_name"] == "ExampleProject · 구현"
    assert pane["role_label"] == "구현"
    assert 'proj.textContent = p.display_name || p.task_name || p.project' in webui.PAGE_HTML
    assert '$("d-project").textContent = p.display_name || p.task_name || p.project' in webui.PAGE_HTML
    assert 'id="edit-task-name"' in webui.PAGE_HTML


def test_conversation_header_uses_identity_and_keeps_location_friendly(monkeypatch):
    seen = []

    class Screen:
        def erase(self):
            pass

        def getmaxyx(self):
            return 40, 100

        def noutrefresh(self):
            pass

    monkeypatch.setattr(control_view, "safe_add", lambda _screen, y, _x, text, *_args: seen.append((y, text)))
    monkeypatch.setattr(control_view, "_live_lines", lambda *_args: [])
    monkeypatch.setattr(control_view, "_draw_inline_composer", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(control_view, "_draw_actions", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(control_view.curses, "doupdate", lambda: None)
    tower = SimpleNamespace(config={})
    control_view._draw(
        Screen(), tower,
        {
            "key": "%1", "display_name": "ExampleProject · 구현", "project": "ExampleProject",
            "agent": "Codex", "role": "builder", "status": "WORKING",
            "execution_host": "workstation-b", "host": "workstation-b", "attention": "none",
            "activity_text": "", "remote": False, "interaction": {},
        },
        "", False,
    )
    by_line = dict(seen)
    assert "ExampleProject · 구현" in by_line[0]
    assert by_line[1] == "Codex · 구현 · 실행 위치 workstation-b"
    assert by_line[2] == "상태: ● 작업 중"
    assert not any("Pane" in text or "Window" in text for _line, text in seen)
