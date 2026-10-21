from tmux_agent_tower.state.overrides import OverrideStore
from tmux_agent_tower.tmux import structure
from tmux_agent_tower.ui.tower import Tower


def _sync_tower(tmp_path):
    tower = Tower.__new__(Tower)
    tower.overrides = OverrideStore(tmp_path / "overrides.json")
    tower.local_host = "mainpc"
    tower.rows = []
    tower.notice = ""
    tower._last_title_sync_scan = 0.0
    tower._title_sync_last_applied = {}
    return tower


def _row(*, transport="local", remote=False):
    return {
        "kind": "pane", "key": "%17", "pane_id": "%17", "session": "tower-session",
        "pane_pid": "1700", "task_name": "User task", "name_origin": "user",
        "remote": remote, "transport": transport, "tmux_host": "mainpc",
    }


def _opt_in(tower):
    tower.overrides.set_task_name("%17", "User task", "tower-session", "1700")
    assert tower.overrides.set_task_title_sync(
        "%17", "tower-session", "1700", enabled=True, original_title="Original", last_title="User task",
    )


def test_periodic_title_sync_stops_after_repeated_agent_overwrites(tmp_path, monkeypatch):
    tower = _sync_tower(tmp_path)
    row = _row()
    tower.rows = [row]
    _opt_in(tower)
    live = {"title": "Original"}
    writes = []
    monkeypatch.setattr(
        structure, "pane_title_identity",
        lambda session, pane_id: {
            "session": session, "pane_id": pane_id, "pane_pid": "1700", "pane_title": live["title"],
        },
    )

    def rename(session, pane_id, title, **kwargs):
        writes.append((session, pane_id, title, kwargs["expected_pane_pid"]))
        live["title"] = title
        return structure.StructureResult(True, pane_id=pane_id)

    monkeypatch.setattr(structure, "rename_pane", rename)
    assert tower._sync_one_pane_title(row, "User task", 0.0, initial=True)[0]

    for now in (20.0, 40.0):
        live["title"] = f"Agent title {int(now)}"
        tower.sync_opted_in_pane_titles(now)

    live["title"] = "Agent title 60"
    assert tower.sync_opted_in_pane_titles(60.0) is True
    assert len(writes) == 3  # Initial explicit apply plus two bounded retries.
    state = tower.overrides.get_task_title_sync("%17", "tower-session", "1700")
    assert state["enabled"] is False
    assert state["blocked"] is True
    assert tower.notice == "Agent가 터미널 제목을 계속 변경하고 있습니다. Tower의 작업 이름은 유지됩니다."

    assert tower.sync_opted_in_pane_titles(80.0) is False
    assert len(writes) == 3


def test_stale_pane_identity_disables_sync_without_writing(tmp_path, monkeypatch):
    tower = _sync_tower(tmp_path)
    row = _row()
    tower.rows = [row]
    _opt_in(tower)
    writes = []
    monkeypatch.setattr(
        structure, "pane_title_identity",
        lambda session, pane_id: {
            "session": session, "pane_id": pane_id, "pane_pid": "different-process", "pane_title": "Agent title",
        },
    )
    monkeypatch.setattr(structure, "rename_pane", lambda *args, **kwargs: writes.append(args))

    assert tower.sync_opted_in_pane_titles(20.0) is True
    assert writes == []
    assert tower.overrides.get_task_title_sync("%17", "tower-session", "1700")["enabled"] is False
    assert tower.notice == "이 작업은 Tower의 표시 이름만 변경할 수 있습니다."


def test_remote_or_ssh_work_cannot_use_local_title_sync(tmp_path):
    tower = _sync_tower(tmp_path)
    assert tower._supports_local_pane_title_sync(_row(transport="ssh")) is False
    assert tower._supports_local_pane_title_sync(_row(remote=True)) is False
    misplaced = _row()
    misplaced["tmux_host"] = "different-host"
    assert tower._supports_local_pane_title_sync(misplaced) is False


def test_title_sync_observes_the_fifteen_second_interval(tmp_path, monkeypatch):
    tower = _sync_tower(tmp_path)
    tower._last_title_sync_scan = 20.0
    _opt_in(tower)
    tower.rows = [_row()]
    reads = []
    monkeypatch.setattr(structure, "pane_title_identity", lambda *args: reads.append(args))

    assert tower.sync_opted_in_pane_titles(34.9) is False
    assert reads == []


def test_result_copy_entry_does_not_run_title_reconciliation(monkeypatch):
    from types import SimpleNamespace

    from tmux_agent_tower.ui import control_view, tower as tower_module

    calls = []
    tower = SimpleNamespace(
        visible_rows=[{"kind": "pane", "key": "%17"}], selected=0,
        sync_opted_in_pane_titles=lambda *_args: calls.append("title-sync"),
    )
    monkeypatch.setattr(control_view, "_copy_result", lambda *_args: calls.append("copy") or "copied")
    # _show_selected_result_copy imports the message helper lazily from widgets.
    from tmux_agent_tower.ui import widgets
    monkeypatch.setattr(widgets, "show_message_screen", lambda *_args: None)
    tower_module._show_selected_result_copy(None, tower)

    assert calls == ["copy"]
