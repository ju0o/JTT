import json

from tmux_agent_tower.state.overrides import ROLE_IDS, OverrideStore

SESSION = "main"
PID = "100"


def test_missing_override_returns_none(tmp_path):
    store = OverrideStore(tmp_path / "overrides.json")
    assert store.get_project("%1", SESSION, PID) is None
    assert store.get_agent("%1", SESSION, PID) is None
    assert store.get_title("%1", SESSION, PID) is None


def test_set_and_get_project(tmp_path):
    store = OverrideStore(tmp_path / "overrides.json")
    store.set_project("%1", "My Project", SESSION, PID)
    assert store.get_project("%1", SESSION, PID) == "My Project"


def test_task_name_is_separate_from_project_identity_and_process_bound(tmp_path):
    store = OverrideStore(tmp_path / "overrides.json")
    store.set_project("%1", "SamplePortal", SESSION, PID)
    store.set_task_name("%1", "로그인 버그 수정", SESSION, PID)

    assert store.get_project("%1", SESSION, PID) == "SamplePortal"
    assert store.get_task_name("%1", SESSION, PID) == "로그인 버그 수정"
    assert store.get_task_name("%1", SESSION, "101") is None


def test_task_title_sync_is_persisted_only_for_bound_pane_identity(tmp_path):
    path = tmp_path / "overrides.json"
    store = OverrideStore(path)
    store.set_task_name("%1", "User task", SESSION, PID)
    assert store.set_task_title_sync(
        "%1", SESSION, PID, enabled=True, conflicts=2,
        original_title="Agent title", last_title="User task",
    )

    reopened = OverrideStore(path)
    assert reopened.get_task_title_sync("%1", SESSION, PID) == {
        "enabled": True, "blocked": False, "conflicts": 2,
        "original_title": "Agent title", "last_title": "User task",
    }
    assert reopened.get_task_title_sync("%1", SESSION, "101")["enabled"] is False
    assert reopened.set_task_title_sync("%1", SESSION, "101", enabled=True) is False

    reopened.clear_field("%1", "task_name", SESSION, PID)
    assert reopened.get_task_name("%1", SESSION, PID) is None
    assert reopened.get_task_title_sync("%1", SESSION, PID)["last_title"] == ""


def test_set_refuses_a_label_with_no_process_id(tmp_path):
    store = OverrideStore(tmp_path / "overrides.json")
    store.set_agent("%1", "Claude", SESSION, "")
    assert store.get_agent("%1", SESSION, PID) is None


def test_set_and_get_agent(tmp_path):
    store = OverrideStore(tmp_path / "overrides.json")
    store.set_agent("%1", "Claude", SESSION, PID)
    assert store.get_agent("%1", SESSION, PID) == "Claude"


def test_custom_agent_free_text(tmp_path):
    store = OverrideStore(tmp_path / "overrides.json")
    store.set_agent("%1", "Gemini CLI", SESSION, PID)
    assert store.get_agent("%1", SESSION, PID) == "Gemini CLI"


def test_set_and_get_title(tmp_path):
    store = OverrideStore(tmp_path / "overrides.json")
    store.set_title("%1", "JuHome dev", SESSION, PID)
    assert store.get_title("%1", SESSION, PID) == "JuHome dev"


def test_fields_are_independent(tmp_path):
    store = OverrideStore(tmp_path / "overrides.json")
    store.set_project("%1", "JuHome", SESSION, PID)
    store.set_agent("%1", "Claude", SESSION, PID)
    store.set_title("%1", "JuHome dev", SESSION, PID)

    assert store.get_project("%1", SESSION, PID) == "JuHome"
    assert store.get_agent("%1", SESSION, PID) == "Claude"
    assert store.get_title("%1", SESSION, PID) == "JuHome dev"


def test_persists_across_instances(tmp_path):
    path = tmp_path / "overrides.json"
    store = OverrideStore(path)
    store.set_project("%1", "My Project", SESSION, PID)
    store.set_agent("%1", "Claude", SESSION, PID)

    reopened = OverrideStore(path)
    assert reopened.get_project("%1", SESSION, PID) == "My Project"
    assert reopened.get_agent("%1", SESSION, PID) == "Claude"


def test_reset_clears_only_that_pane(tmp_path):
    path = tmp_path / "overrides.json"
    store = OverrideStore(path)
    store.set_project("%1", "JuHome", SESSION, PID)
    store.set_project("%2", "OtherProject", SESSION, "200")

    store.reset("%1")

    assert store.get_project("%1", SESSION, PID) is None
    assert store.get_project("%2", SESSION, "200") == "OtherProject"


def test_reset_clears_all_fields_for_that_pane(tmp_path):
    store = OverrideStore(tmp_path / "overrides.json")
    store.set_project("%1", "JuHome", SESSION, PID)
    store.set_agent("%1", "Claude", SESSION, PID)
    store.set_title("%1", "JuHome dev", SESSION, PID)

    store.reset("%1")

    assert store.get_project("%1", SESSION, PID) is None
    assert store.get_agent("%1", SESSION, PID) is None
    assert store.get_title("%1", SESSION, PID) is None


def test_reset_on_nonexistent_key_is_a_safe_noop(tmp_path):
    store = OverrideStore(tmp_path / "overrides.json")
    store.reset("%does-not-exist")


def test_old_flat_string_is_kept_but_not_applied(tmp_path):
    # No session or pane pid: applying it would be the stale-label bug.
    path = tmp_path / "overrides.json"
    path.write_text(json.dumps({"%1": "JuHome", "%2": "OtherProject"}), encoding="utf-8")

    store = OverrideStore(path)
    assert store.get_project("%1", SESSION, PID) is None
    assert store.get_project("%2", SESSION, PID) is None
    assert store.get_agent("%1", SESSION, PID) is None


def test_migrated_project_returns_once_the_user_stamps_this_pane(tmp_path):
    path = tmp_path / "overrides.json"
    path.write_text(json.dumps({"%1": "JuHome"}), encoding="utf-8")

    store = OverrideStore(path)
    store.set_agent("%1", "Claude", SESSION, PID)

    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["%1"]["project"] == "JuHome"
    assert on_disk["%1"]["agent"] == "Claude"
    assert on_disk["%1"]["session"] == SESSION
    assert on_disk["%1"]["pane_pid"] == PID
    assert store.get_project("%1", SESSION, PID) == "JuHome"
    assert store.get_project("%1", SESSION, "999") is None


def test_malformed_override_file_fails_safe(tmp_path):
    path = tmp_path / "overrides.json"
    path.write_text("{ not valid json", encoding="utf-8")
    store = OverrideStore(path)
    assert store.get_project("%1", SESSION, PID) is None
    store.set_project("%1", "Recovered", SESSION, PID)
    assert store.get_project("%1", SESSION, PID) == "Recovered"


def test_malformed_entry_shape_is_skipped_not_fatal(tmp_path):
    path = tmp_path / "overrides.json"
    path.write_text(json.dumps({"%1": ["not", "a", "string", "or", "dict"]}), encoding="utf-8")
    store = OverrideStore(path)
    assert store.get_project("%1", SESSION, PID) is None
    store.set_project("%2", "Fine", SESSION, PID)
    assert store.get_project("%2", SESSION, PID) == "Fine"


def test_stale_pid_is_rejected_and_a_fresh_override_still_wins(tmp_path):
    """The screenshot: SamplePortal / CommandCode stuck on a pane now running cursor-agent."""

    store = OverrideStore(tmp_path / "overrides.json")
    store.set_project("%12", "SamplePortal", "main", "111")
    store.set_agent("%12", "CommandCode", "main", "111")

    assert store.get_project("%12", "main", "222") is None
    assert store.get_agent("%12", "main", "222") is None
    assert store.get_agent("%12", "main", "111") == "CommandCode"

    store.set_agent("%12", "CommandCode", "main", "222")
    assert store.get_agent("%12", "main", "222") == "CommandCode"
    assert store.get_agent("%12", "main", "111") is None


def test_drop_if_stale_removes_a_reused_pane_id(tmp_path):
    store = OverrideStore(tmp_path / "overrides.json")
    store.set_agent("%5", "CommandCode", "0", "111")
    assert store.drop_if_stale("%5", "0", "700") is True
    assert store.get_agent("%5", "0", "111") is None
    assert store.get_agent("%5", "0", "700") is None


def test_drop_if_stale_keeps_a_matching_pane(tmp_path):
    store = OverrideStore(tmp_path / "overrides.json")
    store.set_agent("%5", "CommandCode", "0", "700")
    assert store.drop_if_stale("%5", "0", "700") is False
    assert store.get_agent("%5", "0", "700") == "CommandCode"


def test_all_default_roles_are_stored_as_separate_metadata(tmp_path):
    store = OverrideStore(tmp_path / "overrides.json")
    assert ROLE_IDS == ("orchestrator", "planner", "builder", "reviewer", "qa", "dogfood", "e2e")
    for index, role in enumerate(ROLE_IDS):
        key = f"%{index + 1}"
        store.set_role(key, role, SESSION, str(index + 1))
        assert store.get_role(key, SESSION, str(index + 1)) == role
        assert store.get_agent(key, SESSION, str(index + 1)) is None


def test_role_is_bound_to_local_or_host_namespaced_remote_pane_identity(tmp_path):
    store = OverrideStore(tmp_path / "overrides.json")
    store.set_role("%8", "builder", "main", "800")
    store.set_role("workstation-b:%8", "reviewer", "peer", "800")

    assert store.get_role("%8", "main", "800") == "builder"
    assert store.get_role("workstation-b:%8", "peer", "800") == "reviewer"
    assert store.get_role("%8", "peer", "800") is None
    assert store.get_role("workstation-b:%8", "peer", "801") is None
    assert store.get_role("other:%8", "peer", "800") is None


def test_role_for_reused_pane_is_ignored_and_discarded(tmp_path):
    store = OverrideStore(tmp_path / "overrides.json")
    store.set_role("%9", "builder", "main", "901")

    assert store.get_role("%9", "main", "902") is None
    assert store.get_role("%9", "other", "901") is None
    assert store.drop_if_stale("%9", "main", "902") is True
    assert store.get_role("%9", "main", "901") is None


def test_unknown_role_is_not_written_or_displayed(tmp_path):
    store = OverrideStore(tmp_path / "overrides.json")
    store.set_role("%10", "unexplained-id", SESSION, PID)
    assert store.get_role("%10", SESSION, PID) is None


def test_clear_field_can_require_matching_identity_and_fails_closed(tmp_path):
    store = OverrideStore(tmp_path / "overrides.json")
    store.set_role("%11", "builder", SESSION, PID)

    store.clear_field("%11", "role", "other-session", PID)
    assert store.get_role("%11", SESSION, PID) == "builder"
    store.clear_field("%11", "role", SESSION, "")
    assert store.get_role("%11", SESSION, PID) == "builder"

    store.clear_field("%11", "role", SESSION, PID)
    assert store.get_role("%11", SESSION, PID) is None


def test_clear_field_keeps_legacy_two_argument_behavior(tmp_path):
    store = OverrideStore(tmp_path / "overrides.json")
    store.set_role("%12", "reviewer", SESSION, PID)

    store.clear_field("%12", "role")

    assert store.get_role("%12", SESSION, PID) is None
