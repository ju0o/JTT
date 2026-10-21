import json

from tmux_agent_tower import main
from tmux_agent_tower.i18n import en, ko
from tmux_agent_tower.tmux import keybind, registration
from tmux_agent_tower.ui.settings_menu import keys_menu_items, keys_preamble


USER_CONF = """# keep me
set -g mouse on

# >>> tmux-agent-tower v0.1.1 focus binding >>>
unbind-key w
bind-key w run-shell -b 'tower --focus'
# <<< tmux-agent-tower v0.1.1 focus binding <<<
"""


def test_parse_default_w_command():
    line = "bind-key -T prefix w choose-tree -Zw"
    assert keybind.parse_w_command(line) == "choose-tree -Zw"


def test_absent_tower_uses_the_chooser_and_keeps_a_custom_command_for_restore():
    custom = "bind-key -T prefix w run-shell -b '$HOME/bin/mine'"
    owned = 'bind-key -T prefix w run-shell -b "tower --focus"'
    assert keybind.runtime_fallback("choose-tree -Zw") == "choose-tree -Zw"
    assert keybind.runtime_fallback("") == ""
    assert keybind.preserved_command(custom) == "run-shell -b '$HOME/bin/mine'"
    assert keybind.preserved_command(owned) == ""


def test_reinstall_does_not_treat_the_tower_binding_as_the_users_command():
    line = "bind-key -T prefix w if-shell 'tower --has-active' 'run-shell -b \"tower --focus\"' 'choose-tree -Zw'"
    assert keybind.runtime_fallback("choose-tree -Zw") == "choose-tree -Zw"
    assert keybind.preserved_command(line, saved_restore="display-menu mine") == "display-menu mine"


def test_install_is_idempotent_and_preserves_the_rest_of_the_file(tmp_path):
    conf = tmp_path / ".tmux.conf"
    state = tmp_path / "smart-w.json"
    conf.write_text(USER_CONF, encoding="utf-8")
    applied = []

    first = keybind.install_smart_w(
        conf, state,
        list_keys_line='bind-key -T prefix w run-shell -b "tower --focus"',
        tmux_default="choose-tree -Zw",
        apply=applied.append,
    )
    second = keybind.install_smart_w(
        conf, state,
        list_keys_line=applied[0][-1] and "bind-key -T prefix w if-shell 'tower --has-active'",
        tmux_default="choose-tree -Zw",
        apply=applied.append,
    )

    text = conf.read_text(encoding="utf-8")
    assert first == second == "choose-tree -Zw"
    assert text.count(keybind.BEGIN) == 1
    assert "v0.1.1 focus binding" not in text
    assert "set -g mouse on" in text
    assert "# keep me" in text
    assert "@tmux_agent_tower_pane" in text
    assert "tower --has-active" not in text
    assert "tower --focus" not in text
    assert "choose-tree -Zw" in text
    assert applied[0][:4] == ["bind-key", "-T", "prefix", "w"]
    assert applied[0][-1] == "choose-tree -Zw"
    saved = json.loads(state.read_text(encoding="utf-8"))
    assert saved["fallback"] == "choose-tree -Zw"
    assert saved["source"] == "tmux-default"
    assert saved["restore"] == "choose-tree -Zw"


def test_install_refuses_to_guess_when_no_default_is_known(tmp_path):
    conf = tmp_path / ".tmux.conf"
    conf.write_text("set -g mouse on\n", encoding="utf-8")
    try:
        keybind.install_smart_w(
            conf, tmp_path / "smart-w.json",
            list_keys_line='bind-key -T prefix w run-shell -b "tower --focus"',
            tmux_default="",
        )
        raised = False
    except RuntimeError:
        raised = True
    assert raised
    assert conf.read_text(encoding="utf-8") == "set -g mouse on\n"
    assert not (tmp_path / "smart-w.json").exists()


def test_restore_removes_only_the_tower_block(tmp_path):
    conf = tmp_path / ".tmux.conf"
    state = tmp_path / "smart-w.json"
    conf.write_text("set -g mouse on\n\n" + keybind.render_block("choose-tree -Zw"), encoding="utf-8")
    state.write_text(json.dumps({"fallback": "choose-tree -Zw", "source": "tmux-default"}), encoding="utf-8")
    applied = []

    keybind.restore_smart_w(conf, state, apply=applied.append)

    text = conf.read_text(encoding="utf-8")
    assert keybind.BEGIN not in text
    assert "set -g mouse on" in text
    assert applied == [["bind-key", "-T", "prefix", "w", "choose-tree", "-Zw"]]


def test_custom_command_is_restore_only(tmp_path):
    conf = tmp_path / ".tmux.conf"
    state = tmp_path / "smart-w.json"
    custom = "bind-key -T prefix w display-message kept"
    keybind.install_smart_w(
        conf, state, list_keys_line=custom, tmux_default="choose-tree -Zw",
    )
    text = conf.read_text(encoding="utf-8")
    assert "choose-tree -Zw" in text
    assert "display-message kept" not in text
    saved = json.loads(state.read_text(encoding="utf-8"))
    assert saved["restore"] == "display-message kept"
    applied = []
    keybind.restore_smart_w(conf, state, apply=applied.append)
    assert applied == [["bind-key", "-T", "prefix", "w", "display-message", "kept"]]
    assert "display-message kept" not in conf.read_text(encoding="utf-8")


def test_first_backup_is_the_pre_change_file_and_is_not_overwritten(tmp_path):
    conf = tmp_path / ".tmux.conf"
    conf.write_text(USER_CONF, encoding="utf-8")
    keybind.install_smart_w(
        conf, tmp_path / "smart-w.json",
        list_keys_line="bind-key -T prefix w display-menu mine",
        tmux_default="choose-tree -Zw",
    )
    backup = tmp_path / ".tmux.conf.tower-backup"
    assert backup.read_text(encoding="utf-8") == USER_CONF
    keybind.restore_smart_w(conf, tmp_path / "smart-w.json")
    assert backup.read_text(encoding="utf-8") == USER_CONF


def test_has_active_exit_codes(monkeypatch):
    class Fake:
        def __init__(self):
            self.options = {}
            self.alive = {"%7"}

        def current_session(self):
            return "sess"

        def get_session_option(self, session, name):
            return self.options.get((session, name), "")

        def set_session_option(self, session, name, value):
            self.options[(session, name)] = value

        def unset_session_option(self, session, name):
            self.options.pop((session, name), None)

        def pane_exists(self, pane_id):
            return pane_id in self.alive

        def pane_is_live_in_session(self, pane_id, session):
            return pane_id in self.alive and session == "sess"

    fake = Fake()
    monkeypatch.setattr(main.tmux_capture, "current_session", fake.current_session)
    monkeypatch.setattr(registration, "capture", fake)

    assert main.has_active_tower() == 1
    registration.register("sess", "%7")
    assert main.has_active_tower() == 0
    fake.alive.clear()
    assert main.has_active_tower() == 1
    assert fake.get_session_option("sess", registration.PANE_OPTION) == ""


def test_has_active_cli_does_not_start_the_ui(monkeypatch, capsys):
    monkeypatch.setattr(main, "has_active_tower", lambda: 1)
    monkeypatch.setattr(main, "run_ui", lambda: (_ for _ in ()).throw(AssertionError("ui")))
    try:
        main.cli(["--has-active"])
        exited = None
    except SystemExit as exc:
        exited = exc.code
    assert exited == 1
    assert capsys.readouterr().out == ""


def test_keys_install_cli_routes_without_starting_the_ui(monkeypatch, capsys):
    monkeypatch.setattr(main.keybind, "install_for_user", lambda: "choose-tree -Zw")
    monkeypatch.setattr(main, "run_ui", lambda: (_ for _ in ()).throw(AssertionError("ui")))
    try:
        main.cli(["keys", "install"])
        exited = None
    except SystemExit as exc:
        exited = exc.code
    assert exited == 0
    assert "Ctrl+b w" in capsys.readouterr().out


def test_settings_menu_offers_install_or_restore():
    assert keys_menu_items(False)[0][0] == "install"
    assert keys_menu_items(True)[0][0] == "restore"
    on = "\n".join(keys_preamble(True))
    off = "\n".join(keys_preamble(False))
    assert "스마트" in on or "smart" in on.lower()
    assert "작업 묶음 목록" in off
    assert ko.STRINGS["hint.settings"].startswith("C ")
    assert en.STRINGS["hint.settings"].startswith("C ")


def test_on_branch_focuses_and_off_branch_is_the_recorded_command():
    custom = "run-shell -b '$HOME/bin/mine'"
    args = keybind.binding_args(custom, tower_cmd="/opt/tower")
    assert args[:4] == ["bind-key", "-T", "prefix", "w"]
    assert args[4] == "if-shell"
    assert "@tmux_agent_tower_pane" in args[5]
    assert "tower --has-active" not in args[5]
    assert "select-pane" in args[6]
    assert "\\$pane" in args[6]
    assert "tower --focus" not in args[6]
    assert "/opt/tower" not in " ".join(args)
    assert args[7] == custom
    assert "window_name" not in " ".join(args)
    assert "choose-tree" not in " ".join(args)


def test_control_window_command_is_not_the_runtime_fallback():
    live = 'bind-key -T prefix w if-shell "tower --has-active" "run-shell -b \\"tower --focus\\"" "choose-tree -Zw"'
    configured = "bind-key -T prefix w run-shell -b '$HOME/bin/tmux-control-open'"
    assert keybind.runtime_fallback("choose-tree -Zw") == "choose-tree -Zw"
    assert keybind.preserved_command(live, configured) == "run-shell -b '$HOME/bin/tmux-control-open'"


def test_bare_command_is_not_treated_as_a_list_keys_line():
    assert keybind.parse_w_command("display-menu mine") == ""
    assert keybind.parse_w_command("bind-key -T prefix w display-menu mine") == "display-menu mine"


def test_install_for_user_keeps_the_live_custom_binding(monkeypatch, tmp_path):
    conf = tmp_path / ".tmux.conf"
    state = tmp_path / "smart-w.json"
    conf.write_text("set -g mouse on\n", encoding="utf-8")
    monkeypatch.setattr(keybind, "config_path", lambda: conf)
    monkeypatch.setattr(keybind, "state_path", lambda: state)
    monkeypatch.setattr(keybind, "query_tmux_default_w", lambda: "choose-tree -Zw")
    monkeypatch.setattr(keybind, "query_w_line", lambda _argv: "bind-key -T prefix w display-menu mine")
    monkeypatch.setattr(keybind, "query_configured_w", lambda _path: "")
    applied = []
    monkeypatch.setattr(keybind.capture, "run_tmux", lambda args, capture=True, timeout=3.0: applied.append(list(args)) or "")

    assert keybind.install_for_user() == "choose-tree -Zw"
    text = conf.read_text(encoding="utf-8")
    assert "choose-tree -Zw" in text
    assert "display-menu mine" not in text
    saved = json.loads(state.read_text(encoding="utf-8"))
    assert saved["fallback"] == "choose-tree -Zw"
    assert saved["restore"] == "display-menu mine"
    assert applied[-1][-1] == "choose-tree -Zw"


def test_configured_server_keeps_a_sourced_binding_and_ignores_the_tower_block(
        tmp_path, monkeypatch, tmux_test_safety):
    original_named_temporary_file = keybind.tempfile.NamedTemporaryFile

    def private_temporary_file(*args, **kwargs):
        kwargs.setdefault("dir", tmux_test_safety.root)
        return original_named_temporary_file(*args, **kwargs)

    monkeypatch.setattr(keybind.tempfile, "NamedTemporaryFile", private_temporary_file)
    conf = tmp_path / "tmux.conf"
    conf.write_text(
        "bind-key -T prefix w display-message kept\n" + keybind.render_block("choose-tree -Zw"),
        encoding="utf-8",
    )
    assert keybind.parse_w_command(keybind.query_configured_w(conf)) == "display-message kept"


def test_tower_exit_makes_has_active_fail_and_restart_points_at_the_new_pane(monkeypatch):
    class Fake:
        def __init__(self):
            self.options = {}
            self.alive = {"%6", "%40"}

        def current_session(self):
            return "0"

        def get_session_option(self, session, name):
            return self.options.get((session, name), "")

        def set_session_option(self, session, name, value):
            self.options[(session, name)] = value

        def unset_session_option(self, session, name):
            self.options.pop((session, name), None)

        def pane_is_live_in_session(self, pane_id, session):
            return pane_id in self.alive and session == "0"

    fake = Fake()
    monkeypatch.setattr(main.tmux_capture, "current_session", fake.current_session)
    monkeypatch.setattr(registration, "capture", fake)

    registration.register("0", "%6")
    assert main.has_active_tower() == 0
    registration.unregister_if_self("0", "%6")
    assert main.has_active_tower() == 1

    registration.register("0", "%40")
    assert registration.resolve_active_pane("0") == "%40"
    assert main.has_active_tower() == 0


def test_old_state_keeps_the_control_command_for_restore_only(tmp_path):
    conf = tmp_path / ".tmux.conf"
    state = tmp_path / "smart-w.json"
    custom = """run-shell -b "/home/user/bin/tmux-control-open '#{session_name}'" """
    state.write_text(
        json.dumps({"fallback": custom.strip(), "source": "config"}) + "\n",
        encoding="utf-8",
    )
    conf.write_text(keybind.render_block(custom.strip()), encoding="utf-8")
    applied = []
    result = keybind.install_smart_w(
        conf,
        state,
        list_keys_line=(
            "bind-key -T prefix w if-shell 'tower --has-active' "
            "'run-shell -b \"tower --focus\"' "
            "'run-shell -b \"/home/user/bin/tmux-control-open\"'"
        ),
        tmux_default="choose-tree -Zw",
        apply=applied.append,
    )
    text = conf.read_text(encoding="utf-8")
    assert result == "choose-tree -Zw"
    assert "choose-tree -Zw" in text
    assert "tmux-control-open" not in text
    saved = json.loads(state.read_text(encoding="utf-8"))
    assert saved["fallback"] == "choose-tree -Zw"
    assert "tmux-control-open" in saved["restore"]
    keybind.restore_smart_w(conf, state, apply=applied.append)
    restored = " ".join(applied[-1])
    assert restored.startswith("bind-key -T prefix w run-shell")
    assert "tmux-control-open" in restored


def test_render_block_quotes_a_fallback_that_contains_quotes():
    custom = """run-shell -b "$HOME/bin/tmux-control-open '#{session_name}'" """
    text = keybind.render_block(custom.strip(), "/opt/tower")
    assert text.count(keybind.BEGIN) == 1
    assert "tmux-control-open" in text
    assert "choose-tree" not in text
    assert "@tmux_agent_tower_pane" in text
    assert "tower --has-active" not in text
    assert "/opt/tower" not in text
