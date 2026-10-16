from types import SimpleNamespace

from tmux_agent_tower.i18n import ko
from tmux_agent_tower.launcher.spawn import SpawnResult, SpawnTarget
from tmux_agent_tower.state.overrides import OverrideStore
from tmux_agent_tower.ui import control_view, settings_menu, tower as tower_ui, worksets, workspace_browser
from tmux_agent_tower.ui.widgets import PickResult


def test_empty_home_has_three_plain_choices_and_no_tmux_identity():
    from tmux_agent_tower.ui.tower import zero_state_rows

    rows = zero_state_rows()
    assert [row["action"] for row in rows] == ["task", "saved", "template"]
    assert [row["project"] for row in rows] == [
        "첫 작업 시작", "저장한 구성 열기", "템플릿으로 시작"
    ]
    visible = " ".join((ko.STRINGS[key] for key in (
        "zero.title", "zero.description", "zero.footer", "zero.task", "zero.saved", "zero.template"
    )))
    for raw in ("pane", "window", "session", "PTY", "binding", "endpoint", "workset", "preset"):
        assert raw not in visible.lower()


def test_first_launch_goes_to_home_without_setup_picker(monkeypatch):
    class Screen:
        def keypad(self, _enabled): pass
        def timeout(self, _value): pass

    monkeypatch.setattr(tower_ui, "setup_colors", lambda: None)
    monkeypatch.setattr(tower_ui, "_disable_xon_flow_control", lambda: None)
    monkeypatch.setattr(tower_ui.curses, "noecho", lambda: None)
    monkeypatch.setattr(tower_ui.curses, "cbreak", lambda: None)
    monkeypatch.setattr(tower_ui.curses, "curs_set", lambda *_: None)
    monkeypatch.setattr(tower_ui.tmux_capture, "current_session", lambda: "isolated")
    monkeypatch.setattr(tower_ui.tmux_capture, "current_pane_id", lambda: "")
    monkeypatch.setattr(tower_ui, "_run_loop", lambda *_: None)
    monkeypatch.setattr("tmux_agent_tower.server.service.on_tui_exit", lambda: None)

    # A clean or returning user sees Home directly; setup remains in Settings.
    tower_ui.main(Screen())


def test_current_folder_is_recommended_only_for_project_directories(tmp_path):
    assert workspace_browser.current_project_entry(tmp_path) is None
    (tmp_path / ".git").mkdir()
    entry = workspace_browser.current_project_entry(tmp_path)
    assert entry is not None
    assert entry.name == tmp_path.name
    assert entry.path == str(tmp_path.resolve())


def test_task_quick_start_is_three_steps_with_automatic_defaults(monkeypatch, tmp_path):
    from tmux_agent_tower.launcher.browse import ProjectEntry

    selected = iter(("local", "current", "Codex"))
    menus = []
    launches = []
    overrides = OverrideStore(tmp_path / "overrides.json")
    tower = SimpleNamespace(
        session="isolated",
        local_host="LOCAL",
        remote_hosts=[],
        selected=0,
        visible_rows=[],
        rows=[],
        overrides=overrides,
        load=lambda: None,
    )
    current = ProjectEntry("Sample", str(tmp_path), True)

    monkeypatch.setattr(workspace_browser, "current_project_entry", lambda: current)
    monkeypatch.setattr(workspace_browser, "load_config", lambda: {"agents": {"Codex": "codex"}})
    monkeypatch.setattr(workspace_browser, "resolve_agent_command", lambda *_: "codex")

    def pick(_screen, title, items, **_kwargs):
        menus.append((title, items, _kwargs.get("preamble") or []))
        return PickResult(selected_key=next(selected))

    monkeypatch.setattr(workspace_browser, "run_list_picker", pick)

    def launch(plan, **_kwargs):
        launches.append(plan)
        target = SpawnTarget(str(tmp_path), "Sample", "Codex")
        return [SpawnResult(target, True, "started", "%42", "isolated", "4242")]

    monkeypatch.setattr(workspace_browser, "execute_launch_plan", launch)
    monkeypatch.setattr(workspace_browser, "show_message_screen", lambda *_a, **_k: None)

    workspace_browser.run_workspace_create(None, tower, tmp_path, multi=False, open_tree=True, task_flow=True)

    assert [menu[0] for menu in menus] == [
        ko.STRINGS["environment.step1"],
        ko.STRINGS["environment.step2"],
        ko.STRINGS["environment.step3"],
    ]
    assert menus[1][2] == [ko.STRINGS["environment.local"]]
    assert str(tmp_path) not in menus[1][2]
    assert launches[0].host_key == "LOCAL"
    assert launches[0].workspaces[0].path == str(tmp_path)
    assert launches[0].agents == ("Codex",)
    assert launches[0].placement == "new"
    assert launches[0].layout == "tiled"
    assert overrides.get_task_name("%42", "isolated", "4242") == "Sample · Codex"
    assert overrides.get_role("%42", "isolated", "4242") is None


def test_agent_picker_shows_install_state_and_never_falls_back(monkeypatch):
    picks = iter(("Codex", "Claude"))
    menus = []
    errors = []
    monkeypatch.setattr(workspace_browser, "load_config", lambda: {"agents": {"Codex": "missing-codex", "Claude": "claude"}})
    monkeypatch.setattr(workspace_browser, "resolve_agent_command", lambda name, _cfg: "claude" if name == "Claude" else None)

    def pick(_screen, _title, items, **_kwargs):
        menus.append(items)
        return PickResult(selected_key=next(picks))

    monkeypatch.setattr(workspace_browser, "run_list_picker", pick)
    monkeypatch.setattr(workspace_browser, "show_message_screen", lambda _s, title, lines: errors.append((title, lines)))

    assert workspace_browser._pick_agent(None, []) == "Claude"
    labels = dict(menus[0])
    assert labels["Claude"].endswith("설치됨")
    assert labels["Codex"].endswith("설치되지 않음")
    assert errors and "대신 사용하지 않습니다" in errors[0][1][0]


def test_help_and_saved_work_explain_actions_without_internal_terms(monkeypatch):
    from tmux_agent_tower.ui.render import display_width

    assert "목록 Y" in ko.STRINGS["help.results"]
    assert "상세 Ctrl+Y" in ko.STRINGS["help.results"]
    assert "Ctrl+S" in ko.STRINGS["help.results"]
    assert "복사하지 않습니다" in ko.STRINGS["help.copy_safety_block"]
    assert "상태 확인 가능" in ko.STRINGS["help.remote"]
    assert "문제 해결" in ko.STRINGS["help.troubleshoot"]
    help_keys = (
        "help.navigation", "help.actions", "help.saved", "help.results", "help.copy_safety",
        "help.copy_safety_block", "help.remote", "help.remote_limits", "help.manage",
        "help.detail", "help.detail_advanced", "help.detail_reject", "help.troubleshoot",
        "help.advanced", "help.start", "help.existing", "help.destinations", "help.exit",
    )
    assert all(display_width(ko.STRINGS[key]) <= 56 for key in help_keys)
    assert "새 작업" in ko.STRINGS["help.start"]
    assert "기존 작업" in ko.STRINGS["help.existing"]
    assert "기존 작업" in ko.STRINGS["hint.home_help"]
    assert "새 작업" in ko.STRINGS["hint.home_help"]
    assert "복사" in ko.STRINGS["help.destinations"]

    captured = {}
    monkeypatch.setattr(
        worksets,
        "run_list_picker",
        lambda _s, _title, items, **kwargs: captured.update(items=items, preamble=kwargs.get("preamble"))
        or PickResult(selected_key="back"),
    )
    worksets.open_saved_collection(None, object(), None)
    assert captured["preamble"] == [ko.STRINGS["workset.saved_help"], ko.STRINGS["workset.template_help"]]


def test_settings_keep_language_choice_after_first_run_setup_is_removed(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        settings_menu,
        "run_list_picker",
        lambda _s, _title, items, **_kwargs: captured.update(items=items) or PickResult(selected_key="back"),
    )
    settings_menu.open_settings(None)
    keys = {key for key, _label in captured["items"]}
    assert {"language", "agents", "copy"}.issubset(keys)


def test_phone_empty_home_explains_where_to_start():
    from tmux_agent_tower.server.webui import PAGE_HTML

    assert "아직 실행 중인 작업이 없습니다. PC Tower에서 작업을 시작하거나 저장한 구성을 열어주세요." in PAGE_HTML


def test_unknown_access_does_not_block_first_use_or_fake_destination():
    assert "현재 접속 위치를 확인할 수 없습니다" in ko.STRINGS["access.unknown_context"]
    assert "복사할 때 위치를 선택합니다" in ko.STRINGS["access.unknown_context"]


def test_destructive_actions_explain_what_stays_or_stops():
    assert "계속 실행" in ko.STRINGS["group.pick_remove"]
    assert "삭제되지 않습니다" in ko.STRINGS["group.dissolve_confirm"]
    assert "Agent와 터미널이 끝날 수 있습니다" in ko.STRINGS["control.close_idle"]


def test_first_task_wording_and_availability_are_localized():
    assert ko.STRINGS["wizard.agent_missing_title"] == "이 Agent를 사용할 수 없습니다."
    assert "로그인 안내가 나오면" in ko.STRINGS["wizard.agent_login_hint"]
    assert "Ctrl+G" in ko.STRINGS["wizard.agent_login_hint"]
    assert "프로젝트 폴더를 찾을 수 없습니다" in ko.STRINGS["wizard.project_missing"]
    assert ko.STRINGS["workset.saved"] == "저장했습니다."


def test_login_prompt_is_explained_and_composer_stays_gated(monkeypatch):
    class Screen:
        def __init__(self): self.writes = []
        def erase(self): self.writes.clear()
        def getmaxyx(self): return 24, 100
        def addnstr(self, y, x, text, _n, _attr=0): self.writes.append((y, x, text))
        def noutrefresh(self): pass

    row = {
        "display_name": "첫 작업", "status": "WAITING", "attention": "input_required",
        "interaction": {},
        "agent": "Codex", "role": "", "pane_id": "%1", "key": "%1",
        "remote": False, "execution_host": "LOCAL", "attention_prompt": "Please sign in to continue.",
    }
    tower = SimpleNamespace(config={}, results=None, rows=[row], session="isolated", own_pane_id="")
    monkeypatch.setattr(control_view, "get_pane_screen", lambda *_: (True, "", {"lines": ["login screen"]}))
    monkeypatch.setattr(control_view.curses, "doupdate", lambda: None)
    screen = Screen()

    control_view._draw(screen, tower, row, "", False)

    text = " ".join(item[2] for item in screen.writes)
    assert "로그인이 필요합니다." in text
    assert "Ctrl+G 실제 터미널" in text
    assert "Please sign in" not in text
