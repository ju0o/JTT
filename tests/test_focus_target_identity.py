from types import SimpleNamespace

import pytest

from tmux_agent_tower.control import actions
from tmux_agent_tower.tmux import navigation


def _row(**updates):
    row = {
        "kind": "pane",
        "key": "%44",
        "pane_id": "%44",
        "pane_pid": "4400",
        "session": "main",
        "session_id": "$7",
        "window_id": "@13",
        "tmux_host": "mainpc",
        "transport": "local",
        "transport_target": "",
        "remote": False,
        "target_id": actions._focus_target_id("mainpc", "main", "%44", "4400"),
    }
    row.update(updates)
    return row


def _physical(row=None, **updates):
    row = row or _row()
    values = {
        "session": row["session"],
        "session_id": row["session_id"],
        "window_id": row["window_id"],
        "pane_id": row["pane_id"],
        "pane_pid": row["pane_pid"],
        "pane_dead": "0",
    }
    values.update(updates)
    return navigation.LocalPaneIdentity(**values)


def _processes(pane_pid="4400", ssh_destination=""):
    result = {pane_pid: {"ppid": "1", "args": "bash"}}
    if ssh_destination:
        result["4401"] = {
            "ppid": pane_pid,
            "args": f"ssh -tt user@{ssh_destination}",
        }
    return result


def _tower(row=None, rows=None, *, own_pane_id="%6", local_host="mainpc"):
    row = row or _row()
    return SimpleNamespace(
        rows=list(rows if rows is not None else [row]),
        own_pane_id=own_pane_id,
        local_host=local_host,
    )


def _install_snapshot(monkeypatch, row=None, *, physical=None, processes=None):
    row = row or _row()
    monkeypatch.setattr(
        actions.tmux_navigation,
        "read_local_pane_identities",
        lambda: tuple(physical if physical is not None else [_physical(row)]),
    )
    monkeypatch.setattr(
        actions.process_detection,
        "process_snapshot",
        lambda: processes if processes is not None else _processes(row["pane_pid"]),
    )


def test_validates_exact_local_pane_without_focusing_or_sending(monkeypatch):
    row = _row()
    _install_snapshot(monkeypatch, row)
    monkeypatch.setattr(actions, "focus_local_pane", lambda *_a, **_k: pytest.fail("must not focus"))
    monkeypatch.setattr(actions, "send_prompt", lambda *_a, **_k: pytest.fail("must not send"))

    result = actions.validate_focus_target(_tower(row), row)

    assert result.status is actions.FocusTargetStatus.ALLOWED
    assert result.allowed is True
    assert result.pane_id == "%44"


def test_text_submission_validation_requires_matching_live_agent_process(monkeypatch):
    row = _row(transport="local")
    processes = _processes()
    processes["4401"] = {"ppid": "4400", "args": "codex"}
    _install_snapshot(monkeypatch, row, processes=processes)

    allowed = actions.validate_focus_target(_tower(row), row, "Codex")
    assert allowed.status is actions.FocusTargetStatus.ALLOWED

    processes["4401"] = {"ppid": "4400", "args": "claude"}
    stale = actions.validate_focus_target(_tower(row), row, "Codex")
    assert stale.status is actions.FocusTargetStatus.STALE


def test_text_submission_validation_does_not_treat_ssh_client_as_local_agent(monkeypatch):
    row = _row(transport="ssh", transport_target="asus")
    _install_snapshot(monkeypatch, row, processes=_processes(ssh_destination="asus"))

    result = actions.validate_focus_target(_tower(row), row, "Claude")

    assert result.status is actions.FocusTargetStatus.REMOTE_UNSUPPORTED


@pytest.mark.parametrize(
    ("physical_changes", "expected"),
    [
        ({"pane_pid": "4499"}, actions.FocusTargetStatus.STALE),
        ({"session": "other"}, actions.FocusTargetStatus.STALE),
        ({"session_id": "$9"}, actions.FocusTargetStatus.STALE),
        ({"window_id": "@99"}, actions.FocusTargetStatus.STALE),
        ({"pane_dead": "1"}, actions.FocusTargetStatus.STALE),
    ],
)
def test_rejects_reused_or_changed_physical_pane(monkeypatch, physical_changes, expected):
    row = _row()
    _install_snapshot(monkeypatch, row, physical=[_physical(row, **physical_changes)])

    assert actions.validate_focus_target(_tower(row), row).status is expected


def test_missing_pane_is_not_found(monkeypatch):
    row = _row()
    _install_snapshot(monkeypatch, row, physical=[])

    assert actions.validate_focus_target(_tower(row), row).status is actions.FocusTargetStatus.NOT_FOUND


def test_self_target_is_blocked_before_snapshot_queries(monkeypatch):
    row = _row()
    monkeypatch.setattr(
        actions.tmux_navigation,
        "read_local_pane_identities",
        lambda: pytest.fail("self target must be rejected before querying"),
    )

    result = actions.validate_focus_target(_tower(row, own_pane_id="%44"), row)

    assert result.status is actions.FocusTargetStatus.SELF_TARGET


def test_remote_peer_is_blocked_even_if_pane_key_matches_local(monkeypatch):
    row = _row(remote=True, tmux_host="asus", transport="local")
    monkeypatch.setattr(
        actions.tmux_navigation,
        "read_local_pane_identities",
        lambda: pytest.fail("remote peer must be rejected before local query"),
    )

    assert actions.validate_focus_target(_tower(row), row).status is actions.FocusTargetStatus.REMOTE_UNSUPPORTED


def test_foreign_host_identity_is_not_treated_as_a_local_pane(monkeypatch):
    row = _row(tmux_host="asus", remote=False)
    assert actions.validate_focus_target(_tower(row), row).status is actions.FocusTargetStatus.REMOTE_UNSUPPORTED


def test_same_pane_key_on_another_host_does_not_confuse_local_target(monkeypatch):
    local = _row()
    peer = _row(
        target_id="remote-target",
        tmux_host="asus",
        remote=True,
    )
    _install_snapshot(monkeypatch, local)

    assert actions.validate_focus_target(_tower(local, rows=[peer, local]), local).status is actions.FocusTargetStatus.ALLOWED
    assert actions.validate_focus_target(_tower(peer, rows=[peer, local]), peer).status is actions.FocusTargetStatus.REMOTE_UNSUPPORTED


def test_ssh_client_pane_is_allowed_only_for_matching_live_destination(monkeypatch):
    row = _row(transport="ssh", transport_target="asus")
    _install_snapshot(monkeypatch, row, processes=_processes(ssh_destination="asus"))

    assert actions.validate_focus_target(_tower(row), row).status is actions.FocusTargetStatus.ALLOWED


def test_ssh_destination_mismatch_is_stale(monkeypatch):
    row = _row(transport="ssh", transport_target="asus")
    _install_snapshot(monkeypatch, row, processes=_processes(ssh_destination="other-host"))

    assert actions.validate_focus_target(_tower(row), row).status is actions.FocusTargetStatus.STALE


def test_local_transport_is_rejected_if_an_ssh_client_is_now_present(monkeypatch):
    row = _row()
    _install_snapshot(monkeypatch, row, processes=_processes(ssh_destination="asus"))

    assert actions.validate_focus_target(_tower(row), row).status is actions.FocusTargetStatus.STALE


def test_query_and_process_failures_fail_closed(monkeypatch):
    row = _row()
    monkeypatch.setattr(actions.tmux_navigation, "read_local_pane_identities", lambda: None)
    assert actions.validate_focus_target(_tower(row), row).status is actions.FocusTargetStatus.UNVERIFIED

    _install_snapshot(monkeypatch, row, processes={})
    assert actions.validate_focus_target(_tower(row), row).status is actions.FocusTargetStatus.UNVERIFIED


def test_duplicate_snapshot_keys_are_ambiguous(monkeypatch):
    row = _row()
    _install_snapshot(monkeypatch, row, physical=[_physical(row), _physical(row)])

    assert actions.validate_focus_target(_tower(row), row).status is actions.FocusTargetStatus.AMBIGUOUS


def test_duplicate_tower_identity_is_ambiguous(monkeypatch):
    row = _row()
    _install_snapshot(monkeypatch, row)

    assert actions.validate_focus_target(_tower(row, rows=[row, dict(row)]), row).status is actions.FocusTargetStatus.AMBIGUOUS


def test_local_duplicate_pane_key_is_ambiguous_even_when_one_pid_matches(monkeypatch):
    row = _row()
    competing = _row(
        pane_pid="4499",
        target_id=actions._focus_target_id("mainpc", "main", "%44", "4499"),
    )
    _install_snapshot(monkeypatch, row)

    assert actions.validate_focus_target(_tower(row, rows=[row, competing]), row).status is actions.FocusTargetStatus.AMBIGUOUS


def test_stale_or_incomplete_rows_are_never_authorized(monkeypatch):
    row = _row()
    changed = dict(row, pane_pid="4499")
    _install_snapshot(monkeypatch, row)
    assert actions.validate_focus_target(_tower(row, rows=[changed]), row).status is actions.FocusTargetStatus.STALE

    missing = dict(row)
    del missing["session_id"]
    assert actions.validate_focus_target(_tower(missing), missing).status is actions.FocusTargetStatus.UNVERIFIED

    mismatched_key = dict(row, key="%45")
    assert actions.validate_focus_target(_tower(mismatched_key), mismatched_key).status is actions.FocusTargetStatus.UNVERIFIED

    mismatched_target = dict(row, target_id="not-the-tower-target-id")
    assert actions.validate_focus_target(_tower(mismatched_target), mismatched_target).status is actions.FocusTargetStatus.STALE


def test_failed_identity_validation_never_focuses_selects_or_sends(monkeypatch):
    row = _row()
    _install_snapshot(monkeypatch, row, physical=[_physical(row, pane_pid="4499")])
    monkeypatch.setattr(actions, "focus_local_pane", lambda *_a, **_k: pytest.fail("must not focus"))
    monkeypatch.setattr(actions, "send_prompt", lambda *_a, **_k: pytest.fail("must not send"))
    monkeypatch.setattr(
        navigation.subprocess,
        "run",
        lambda args, **_kwargs: pytest.fail(f"must not execute tmux/process command: {args}"),
    )

    assert actions.validate_focus_target(_tower(row), row).status is actions.FocusTargetStatus.STALE


def test_transport_unknown_and_unreadable_ssh_process_fail_closed(monkeypatch):
    unknown = _row(transport="unknown")
    assert actions.validate_focus_target(_tower(unknown), unknown).status is actions.FocusTargetStatus.UNVERIFIED

    row = _row(transport="ssh", transport_target="asus")
    _install_snapshot(monkeypatch, row, processes=_processes(ssh_destination=""))
    monkeypatch.setattr(
        actions.process_detection,
        "process_snapshot",
        lambda: {
            "4400": {"ppid": "1", "args": "bash"},
            "4401": {"ppid": "4400", "args": "ssh -o"},
        },
    )
    assert actions.validate_focus_target(_tower(row), row).status is actions.FocusTargetStatus.UNVERIFIED


def test_identity_reader_distinguishes_failure_from_empty_and_parses_fields(monkeypatch):
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=0, stdout="main\x1f$7\x1f@13\x1f%44\x1f4400\x1f0\n")

    monkeypatch.setattr(navigation.subprocess, "run", run)
    identities = navigation.read_local_pane_identities()

    assert identities == (navigation.LocalPaneIdentity("main", "$7", "@13", "%44", "4400", "0"),)
    assert calls[0][0][:4] == ["tmux", "list-panes", "-a", "-F"]
    assert calls[0][1]["stdin"] is navigation.subprocess.DEVNULL

    monkeypatch.setattr(navigation.subprocess, "run", lambda *_a, **_k: SimpleNamespace(returncode=0, stdout=""))
    assert navigation.read_local_pane_identities() == ()
    monkeypatch.setattr(navigation.subprocess, "run", lambda *_a, **_k: SimpleNamespace(returncode=1, stdout=""))
    assert navigation.read_local_pane_identities() is None
    monkeypatch.setattr(navigation.subprocess, "run", lambda *_a, **_k: SimpleNamespace(returncode=0, stdout="bad output\n"))
    assert navigation.read_local_pane_identities() is None
