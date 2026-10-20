"""Local-only PTY lifecycle tests; these never invoke tmux or an agent CLI."""

import json
import base64
import os
from pathlib import Path
import pwd
import random
import re
import shutil
import signal
import stat
import subprocess
import sys
import time
import zlib

import pytest

from tmux_agent_tower.terminal import TerminalError, TerminalRegistry, renderer_available
from tmux_agent_tower.terminal.manager import MAX_FRAME, _ensure_private_root, _worker_is_same_process
from tmux_agent_tower.terminal import _worker


@pytest.mark.skipif(_worker.pyte is None, reason="pyte renderer is unavailable")
def test_private_sgr_extension_does_not_disable_terminal_renderer():
    screen = _worker._JttScreen(20, 3)
    stream = _worker.pyte.Stream(screen)

    # xterm-like applications may emit a private SGR extension. pyte dispatches
    # it with private=True although its base Screen handler lacks that keyword.
    stream.feed("\x1b[?1m\x1b[31mJTT_RENDER_OK")

    assert "JTT_RENDER_OK" in "".join(screen.display)
    assert screen.cursor.attrs.fg == "red"


@pytest.mark.skipif(_worker.pyte is None, reason="pyte renderer is unavailable")
def test_tmux_private_device_status_query_does_not_disable_terminal_renderer():
    screen = _worker._JttScreen(40, 3)
    stream = _worker.pyte.Stream(screen)

    # tmux issues this private DSR during terminal initialization. There is no
    # terminal-response channel in the worker's VT model, so the extension is
    # ignored while subsequent ordinary rendering continues.
    stream.feed("\x1b[?996n\x1b[32mJTT_DSR_OK")

    assert "JTT_DSR_OK" in "".join(screen.display)
    assert screen.cursor.attrs.fg == "green"


def _safe_test_home(monkeypatch, tmp_path):
    home = tmp_path / "shell-home"
    home.mkdir(mode=0o700)
    home.chmod(0o700)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.delenv("TMUX_PANE", raising=False)
    # Opt-in worker diagnostics contain only a static stage and exception type;
    # no terminal bytes, environment values, or exception messages are logged.
    monkeypatch.setenv("JTT_TERMINAL_FIXTURE_DIAGNOSTICS", "1")
    return home


def test_nested_registry_creation_keeps_every_new_jtt_component_private(tmp_path):
    trusted_parent = tmp_path / "anchor"
    trusted_parent.mkdir(mode=0o700)
    trusted_parent.chmod(0o700)
    parent_mode_before = stat.S_IMODE(trusted_parent.stat().st_mode)
    root = trusted_parent / "jtt-home" / "nested" / "terminals"

    assert _ensure_private_root(root) == root
    created = (
        trusted_parent / "jtt-home",
        trusted_parent / "jtt-home" / "nested",
        root,
    )
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o700 for path in created)
    assert stat.S_IMODE(trusted_parent.stat().st_mode) == parent_mode_before


def test_existing_nonprivate_jtt_home_is_rejected_without_chmod(tmp_path):
    jtt_home = tmp_path / "jtt-home"
    jtt_home.mkdir(mode=0o755)
    jtt_home.chmod(0o755)
    root = jtt_home / "terminals"

    with pytest.raises(TerminalError, match="must be private"):
        _ensure_private_root(root)
    assert stat.S_IMODE(jtt_home.stat().st_mode) == 0o755
    assert not root.exists()


@pytest.mark.parametrize("renderer_state", ["dependency_missing", "initialization_failed"])
def test_renderer_failure_prevents_registry_and_supervisor_creation(
        tmp_path, monkeypatch, renderer_state):
    import tmux_agent_tower.terminal.manager as manager

    root = tmp_path / "jtt" / "terminals"
    monkeypatch.setattr(manager, "_probe_renderer", lambda: renderer_state)
    monkeypatch.setattr(manager.subprocess, "Popen",
                        lambda *_args, **_kwargs: pytest.fail("Popen must not run"))

    with pytest.raises(TerminalError, match="VT 렌더러"):
        TerminalRegistry(root=root).create("must not start")
    assert not root.exists()
    assert not root.parent.exists()


def test_supervisor_early_exit_is_not_reported_as_stale_identity(tmp_path, monkeypatch):
    import tmux_agent_tower.terminal.manager as manager

    _safe_test_home(monkeypatch, tmp_path)
    registry = TerminalRegistry(root=tmp_path / "jtt" / "terminals")
    original_popen = manager.subprocess.Popen

    def exit_worker(args, **kwargs):
        if len(args) > 1 and str(args[1]) == str(manager.WORKER):
            return original_popen(
                [sys.executable, "-c", "raise SystemExit(23)"], **kwargs
            )
        return original_popen(args, **kwargs)

    monkeypatch.setattr(manager.subprocess, "Popen", exit_worker)
    with pytest.raises(TerminalError, match="exited during startup.*23"):
        registry.create("early exit fixture")


@pytest.mark.skipif(not renderer_available(), reason="worker identity test requires pyte")
@pytest.mark.skipif(not Path("/bin/bash").exists(), reason="bash is not available")
def test_live_supervisor_argv_mismatch_is_reported_separately(tmp_path, monkeypatch):
    import tmux_agent_tower.terminal.manager as manager

    _safe_test_home(monkeypatch, tmp_path)
    monkeypatch.setattr(manager, "SUPERVISOR_STARTUP_TIMEOUT", 0.3)
    monkeypatch.setattr(manager, "_worker_identity_status",
                        lambda *_args, **_kwargs: "argv_mismatch")
    registry = TerminalRegistry(root=tmp_path / "jtt" / "terminals")
    original_popen = manager.subprocess.Popen
    worker_processes = []

    def record_worker(args, **kwargs):
        process = original_popen(args, **kwargs)
        if len(args) > 1 and str(args[1]) == str(manager.WORKER):
            worker_processes.append(process)
        return process

    monkeypatch.setattr(manager.subprocess, "Popen", record_worker)
    with pytest.raises(TerminalError, match="argv identity could not be verified"):
        registry.create("identity mismatch fixture")
    assert len(worker_processes) == 1
    assert worker_processes[0].poll() is not None
    assert registry.list_sessions() == ()


@pytest.mark.parametrize(
    ("worker_status", "shell_live", "metadata_state", "expected"),
    [
        ("argv_mismatch", True, "running", "identity_mismatch"),
        ("start_mismatch", True, "running", "identity_mismatch"),
        ("not_running", True, "running", "orphaned"),
        ("unavailable", False, "running", "orphaned"),
        ("not_running", False, "stopped", "orphaned"),
        ("not_running", False, "exited", "exited"),
    ],
    ids=["argv-mismatch", "pid-reuse", "wsl-owner-gone-shell-live",
         "wsl-owner-and-shell-gone-unconfirmed", "stopped-not-normal-exit",
         "confirmed-normal-exit"],
)
def test_registry_distinguishes_owner_loss_identity_mismatch_and_confirmed_exit(
    tmp_path, monkeypatch, worker_status, shell_live, metadata_state, expected,
):
    import tmux_agent_tower.terminal.manager as manager

    root = tmp_path / "private-jtt" / "terminals"
    manager._ensure_private_root(root)
    session_id = "a" * 32
    session_dir = root / session_id
    session_dir.mkdir(mode=0o700)
    manager._atomic_metadata(session_dir / manager.SESSION_META, {
        "protocol": manager.PROTOCOL, "session_id": session_id,
        "generation": "generation-a", "supervisor_pid": 123,
        "supervisor_start": "supervisor-start", "shell_pid": 456,
        "shell_start": "shell-start", "shell_path": "/bin/bash",
        "name": "fixture", "state": metadata_state,
        "exit_code": 0 if metadata_state == "exited" else None,
        "exit_signal": None,
    })
    monkeypatch.setattr(manager, "_worker_identity_status",
                        lambda *_args, **_kwargs: worker_status)
    monkeypatch.setattr(
        manager, "_proc_start",
        lambda pid: ("shell-start" if pid == 456 and shell_live else
                     "supervisor-start" if pid == 123 and worker_status == "verified" else None),
    )

    identity, = manager.TerminalRegistry(root).list_sessions()
    assert identity.state == expected


def test_registry_does_not_treat_exit_without_wait_status_as_normal(tmp_path, monkeypatch):
    import tmux_agent_tower.terminal.manager as manager

    root = tmp_path / "private-jtt" / "terminals"
    manager._ensure_private_root(root)
    session_id = "b" * 32
    session_dir = root / session_id
    session_dir.mkdir(mode=0o700)
    manager._atomic_metadata(session_dir / manager.SESSION_META, {
        "protocol": manager.PROTOCOL, "session_id": session_id,
        "generation": "generation-b", "supervisor_pid": 123,
        "supervisor_start": "supervisor-start", "shell_pid": 456,
        "shell_start": "shell-start", "shell_path": "/bin/bash",
        "name": "fixture", "state": "exited",
        "exit_code": None, "exit_signal": None,
    })
    monkeypatch.setattr(manager, "_worker_identity_status", lambda *_args: "verified")
    monkeypatch.setattr(manager, "_proc_start", lambda pid: {
        123: "supervisor-start", 456: None,
    }.get(pid))

    identity, = manager.TerminalRegistry(root).list_sessions()
    assert identity.state == "identity_mismatch"


def test_registry_inspect_reference_does_not_conflate_missing_and_malformed(tmp_path):
    import tmux_agent_tower.terminal.manager as manager

    root = tmp_path / "private-jtt" / "terminals"
    manager._ensure_private_root(root)
    registry = manager.TerminalRegistry(root)
    missing = "c" * 32
    malformed = "d" * 32
    bad_dir = root / malformed
    bad_dir.mkdir(mode=0o700)
    manager._atomic_metadata(bad_dir / manager.SESSION_META, {
        "protocol": manager.PROTOCOL + 1, "session_id": malformed,
    })

    assert registry.inspect_reference(missing) == "not_found"
    assert registry.inspect_reference(malformed) == "identity_mismatch"


@pytest.mark.skipif(not renderer_available(), reason="live-shell preservation requires pyte")
@pytest.mark.skipif(not Path("/bin/bash").exists(), reason="bash is not available")
def test_renderer_preflight_does_not_hide_or_restart_existing_session(tmp_path, monkeypatch):
    import tmux_agent_tower.terminal.manager as manager

    home = _safe_test_home(monkeypatch, tmp_path)
    root = tmp_path / "r"
    registry = TerminalRegistry(root=root)
    original = registry.create("preserved shell")
    session_id = original.identity.session_id
    shell_pid = original.identity.shell_pid
    supervisor_pid = original.identity.supervisor_pid
    original.close()

    monkeypatch.setattr(manager, "_probe_renderer", lambda: "dependency_missing")
    listed = registry.list_sessions()
    assert len(listed) == 1
    assert listed[0].session_id == session_id
    assert listed[0].shell_pid == shell_pid
    reconnected = registry.connect(session_id)
    try:
        assert reconnected.identity.shell_pid == shell_pid
        assert reconnected.identity.supervisor_pid == supervisor_pid
        assert os.kill(shell_pid, 0) is None
    finally:
        reconnected.close()
        registry.stop(session_id)
    assert home.exists()


def _screen_text(update):
    if update.screen is None:
        return ""
    return "\n".join("".join(cell.text for cell in row) for row in update.screen.rows)


def _wait_for_text(client, expected, timeout=5.0):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = client.poll()
        if expected in _screen_text(last):
            return last
        time.sleep(0.03)
    raise AssertionError("expected terminal marker not observed")


@pytest.mark.skipif(not renderer_available(), reason="interactive input requires optional pyte renderer")
@pytest.mark.skipif(not Path("/bin/bash").exists(), reason="bash is not available")
def test_create_interact_disconnect_reconnect_and_explicit_stop(tmp_path, monkeypatch):
    account_shell = Path(pwd.getpwuid(os.getuid()).pw_shell).resolve()
    if account_shell != Path("/bin/bash").resolve():
        pytest.skip("test contract deliberately uses the account's /bin/bash login shell")
    home = _safe_test_home(monkeypatch, tmp_path)
    root = tmp_path / "r"
    registry = TerminalRegistry(root=root)

    # Constructing/listing a registry is not a request to launch a shell.
    assert not root.exists()
    assert registry.list_sessions() == ()
    assert not root.exists()

    client = registry.create("test shell")
    session_id = client.identity.session_id
    shell_pid = client.identity.pid
    supervisor_pid = client.identity.supervisor_pid
    assert client.state == "running"
    assert client.identity.shell_path == str(account_shell)
    assert Path("/proc/{}/cwd".format(shell_pid)).resolve() == home.resolve()
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    session_dir = root / session_id
    assert stat.S_IMODE(session_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE((session_dir / "session.json").stat().st_mode) == 0o600
    metadata = json.loads((session_dir / "session.json").read_text())
    socket_path = Path(metadata.get("socket_path", session_dir / "s"))
    assert stat.S_IMODE(socket_path.stat().st_mode) == 0o600
    assert not {"environment", "env", "output", "transcript"}.intersection(metadata)

    initial = client.poll()
    assert initial.renderer_available
    assert initial.synced
    assert initial.screen is not None
    assert len(initial.screen.rows) == 24
    assert len(initial.screen.rows[0]) == 80
    assert client.acquire()

    marker = "JTT_PTY_" + os.urandom(6).hex()
    command = "printf '%s\\n'\n" % marker
    assert client.send(command.encode("utf-8")) == len(command.encode("utf-8"))
    update = _wait_for_text(client, marker)
    assert update.synced
    assert update.screen is not None
    # Cell attributes and cursor coordinates are part of the snapshot contract.
    assert isinstance(update.screen.rows[0][0].foreground, str)
    assert 0 <= update.screen.cursor_x < update.columns
    assert 0 <= update.screen.cursor_y < update.lines

    term_command = "printf 'JTT_TERM=%s\\n' \"$TERM\"\n"
    client.send(term_command.encode("utf-8"))
    assert "JTT_TERM=xterm" in _screen_text(_wait_for_text(client, "JTT_TERM=xterm"))

    client.resize(100, 30)
    resized = client.poll()
    assert resized.columns == 100
    assert resized.lines == 30
    resize_marker = "JTT_SIZE_" + os.urandom(4).hex()
    size_command = "printf '%s:'; stty size; printf '%s\\n'\n" % (resize_marker, resize_marker)
    client.send(size_command.encode("utf-8"))
    size_update = _wait_for_text(client, "30 100")
    assert resize_marker in _screen_text(size_update)

    # Closing a UI connection releases input ownership, but not the shell.
    client.close()
    time.sleep(0.05)
    assert os.kill(shell_pid, 0) is None
    assert registry.list_sessions()[0].shell_pid == shell_pid

    reconnected = registry.connect(session_id)
    assert reconnected.identity.session_id == session_id
    assert reconnected.identity.pid == shell_pid
    assert reconnected.identity.supervisor_pid == supervisor_pid
    reconnect_update = reconnected.poll()
    assert reconnect_update.synced
    assert reconnect_update.screen is not None
    assert "30 100" in _screen_text(reconnect_update)
    reconnected.close()

    registry.stop(session_id)
    # PID reuse after exit is valid; the original process identity must be gone.
    assert _proc_start(shell_pid) != client.identity.shell_start
    assert not _worker_is_same_process(
        supervisor_pid, client.identity.supervisor_start, session_dir,
        session_id, client.identity.generation,
    )
    assert not session_dir.exists()
    assert registry.list_sessions() == ()


@pytest.mark.skipif(not renderer_available(), reason="long-home socket test requires pyte")
@pytest.mark.skipif(not Path("/bin/bash").exists(), reason="bash is not available")
def test_long_jtt_home_keeps_socket_path_connectable_through_reconnect(tmp_path, monkeypatch):
    """A long but valid JTT_HOME must not fail after spawning its supervisor."""

    account_shell = Path(pwd.getpwuid(os.getuid()).pw_shell).resolve()
    if account_shell != Path("/bin/bash").resolve():
        pytest.skip("test contract deliberately uses the account's /bin/bash login shell")
    _safe_test_home(monkeypatch, tmp_path)
    root = tmp_path / ("j" * 70) / "terminals"
    registry = TerminalRegistry(root=root)

    client = registry.create("long JTT home")
    session_id = client.identity.session_id
    identity = client.identity
    session_dir = root / session_id
    assert len(os.fsencode(session_dir / "s")) > 107
    metadata = json.loads((session_dir / "session.json").read_text(encoding="utf-8"))
    socket_path = Path(metadata["socket_path"])
    assert len(os.fsencode(socket_path)) <= 107
    assert stat.S_IMODE(socket_path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(socket_path.parent.parent.stat().st_mode) == 0o700
    assert socket_path.exists()
    assert stat.S_IMODE(socket_path.stat().st_mode) == 0o600
    assert client.poll().synced
    assert client.acquire()
    marker = "JTT_LONG_HOME_INPUT_OK"
    command = "printf '%s\\n'\n" % marker
    client.send(command.encode("utf-8"))
    assert marker in _screen_text(_wait_for_text(client, marker))

    client.close()
    reconnected = registry.connect(session_id)
    try:
        assert reconnected.identity.session_id == identity.session_id
        assert reconnected.identity.generation == identity.generation
        assert reconnected.identity.shell_pid == identity.shell_pid
        assert reconnected.identity.supervisor_pid == identity.supervisor_pid
        assert reconnected.poll().synced
    finally:
        reconnected.close()
        registry.stop(session_id, expected_generation=identity.generation,
                      expected_shell_pid=identity.shell_pid)
    assert not socket_path.exists()
    assert not session_dir.exists()
    assert registry.list_sessions() == ()


@pytest.mark.skipif(not renderer_available(), reason="input lease test requires optional pyte renderer")
@pytest.mark.skipif(not Path("/bin/bash").exists(), reason="bash is not available")
def test_single_input_lease_and_disconnect_release(tmp_path, monkeypatch):
    account_shell = Path(pwd.getpwuid(os.getuid()).pw_shell).resolve()
    if account_shell != Path("/bin/bash").resolve():
        pytest.skip("test contract deliberately uses the account's /bin/bash login shell")
    _safe_test_home(monkeypatch, tmp_path)
    registry = TerminalRegistry(root=tmp_path / "r")
    first = registry.create("lease test")
    session_id = first.identity.session_id
    second = registry.connect(session_id)
    try:
        assert first.poll().synced
        assert first.acquire()
        assert second.poll().synced
        assert second.acquire() is False
        with pytest.raises(TerminalError, match="input_lease_required"):
            second.send(b"printf unexpected\\n\n")
        # Keep the active terminal view polling beyond the nominal lease
        # duration. Its heartbeat should retain exclusive input ownership
        # instead of dropping the first key after an idle pause.
        heartbeat_deadline = time.monotonic() + _worker.LEASE_SECONDS + 0.2
        while time.monotonic() < heartbeat_deadline:
            assert first.poll().synced
            time.sleep(0.2)
        assert first.lease
        assert second.acquire() is False
        marker = "JTT_LEASE_HEARTBEAT_" + os.urandom(5).hex()
        command = "printf '%s\\n'\n" % marker
        first.send(command.encode("utf-8"))
        assert marker in _screen_text(_wait_for_text(first, marker))
    finally:
        first.close()
    try:
        assert second.acquire()
        marker = "JTT_LEASE_" + os.urandom(5).hex()
        command = "printf '%s\\n'\n" % marker
        second.send(command.encode("utf-8"))
        assert marker in _screen_text(_wait_for_text(second, marker))
    finally:
        second.close()
        registry.stop(session_id)


@pytest.mark.skipif(not renderer_available(), reason="snapshot test requires optional pyte renderer")
def test_large_valid_snapshot_fits_transport_and_oversize_recovers(monkeypatch):
    import pyte

    screen = pyte.Screen(300, 120)
    rng = random.Random(20261019)
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
    for y in range(120):
        row = screen.buffer[y]
        for x in range(300):
            row[x] = pyte.screens.Char(
                data=rng.choice(alphabet),
                fg="#{:06x}".format(rng.randrange(1 << 24)),
                bg="#{:06x}".format(rng.randrange(1 << 24)),
            )

    owner = object.__new__(_worker.PtyOwner)
    owner.screen = screen
    owner.screen_error = False
    owner.lines = 120
    owner.columns = 300
    owner.sequence = 0
    owner.output = []
    owner.lease_owner = None
    owner.lease_deadline = 0.0
    owner.snapshot_synced = False
    owner.child = type("Child", (), {"poll": lambda self: None})()
    owner.session_id = "a" * 32
    owner.generation = "b" * 32
    owner.state = "running"
    owner.renderer_state = "ready"

    encoded = owner._screen_payload()
    assert encoded is not None
    assert len(encoded) > 20 * 1024  # Reproduces the former permanent-failure threshold.
    assert len(encoded) <= _worker.MAX_SCREEN_B64
    decoded = json.loads(zlib.decompress(base64.b64decode(encoded)))
    assert len(decoded["rows"]) == 120
    assert len(decoded["rows"][0]) == 300
    owner.sequence = 1
    owner.output = [(1, b"x" * (48 * 1024))]
    max_poll = owner._poll({"after": 0, "client_id": "c" * 32})
    wire_frame = json.dumps(max_poll, separators=(",", ":")).encode("utf-8") + b"\n"
    assert len(wire_frame) <= MAX_FRAME

    # Simulate a configured cap that is temporarily too small. The renderer
    # must not enter a permanent error state, and input stays fail-closed until
    # a later complete snapshot can be delivered.
    monkeypatch.setattr(_worker, "MAX_SCREEN_B64", 32)
    too_large = owner._poll({"after": 0, "client_id": "c" * 32})
    assert too_large["synced"] is False
    assert too_large["gap"] is True
    assert owner.screen_error is False
    assert owner.snapshot_synced is False
    with pytest.raises(TerminalError, match="renderer_unavailable"):
        owner._input("c" * 32, {"after": 0, "data": base64.b64encode(b"x").decode("ascii")})
    monkeypatch.setattr(_worker, "MAX_SCREEN_B64", 768 * 1024)
    recovered = owner._poll({"after": 0, "client_id": "c" * 32})
    assert recovered["synced"] is True
    assert recovered["gap"] is False
    assert owner.snapshot_synced is True


@pytest.mark.skipif(not renderer_available(), reason="observe snapshot requires optional pyte renderer")
def test_observe_snapshot_is_atomic_and_does_not_mutate_worker_cursor_or_lease():
    import pyte

    screen = pyte.Screen(40, 8)
    pyte.Stream(screen).feed("\x1b[31mOBSERVE_ONLY\x1b[0m")
    owner = object.__new__(_worker.PtyOwner)
    owner.screen = screen
    owner.screen_error = False
    owner.lines = 8
    owner.columns = 40
    owner.sequence = 27
    owner.output = [(26, b"before"), (27, b"after")]
    owner.snapshot_synced = True
    owner.lease_owner = "active-input-client"
    owner.lease_deadline = 1234.5
    owner.child = type("Child", (), {"poll": lambda self: None})()
    owner.session_id = "a" * 32
    owner.generation = "b" * 32
    owner.metadata = {"shell_pid": 4321}
    owner.renderer_state = "ready"

    before = (owner.sequence, list(owner.output), owner.snapshot_synced,
              owner.lease_owner, owner.lease_deadline, owner.columns, owner.lines)
    observation = owner._observe()
    after = (owner.sequence, list(owner.output), owner.snapshot_synced,
             owner.lease_owner, owner.lease_deadline, owner.columns, owner.lines)

    assert observation["synced"] is True
    assert observation["output_sequence"] == 27
    assert observation["snapshot_revision"] == 27
    assert observation["columns"] == 40 and observation["lines"] == 8
    assert before == after


def test_new_client_hello_does_not_claim_an_existing_input_lease():
    owner = object.__new__(_worker.PtyOwner)
    owner.session_id = "a" * 32
    owner.generation = "b" * 32
    owner.supervisor_start = "fixture-start"
    owner.child = None
    owner.lease_owner = "c" * 32
    owner.columns = 80
    owner.lines = 24
    owner.screen = object()
    owner.screen_error = False
    owner.renderer_state = "ready"
    owner.sequence = 0
    client_id = "d" * 32
    client = {"client_id": None}

    response, stop_after = owner._dispatch(client, {
        "op": "hello",
        "client_id": client_id,
        "session_id": owner.session_id,
        "generation": owner.generation,
        "supervisor_pid": os.getpid(),
        "supervisor_start": owner.supervisor_start,
    })

    assert not stop_after
    assert client["client_id"] == client_id
    assert owner.lease_owner == "c" * 32
    assert response["lease"] is False


def test_optional_renderer_absence_blocks_shell_creation_before_popen(tmp_path, monkeypatch):
    import tmux_agent_tower.terminal.manager as manager

    root = tmp_path / "r"
    monkeypatch.setattr(manager, "_probe_renderer", lambda: "dependency_missing")
    monkeypatch.setattr(manager.subprocess, "Popen",
                        lambda *_args, **_kwargs: pytest.fail("shell supervisor must not start"))
    with pytest.raises(TerminalError, match="pyte"):
        TerminalRegistry(root=root).create("renderer unavailable")
    assert not root.exists()


def _proc_start(pid):
    try:
        raw = Path("/proc/{}/stat".format(pid)).read_text(encoding="ascii")
        fields = raw.rsplit(")", 1)[1].split()
        if fields[0] in ("Z", "X"):
            return None
        return fields[19]
    except (OSError, IndexError, ValueError):
        return None


def _proc_session_and_group(pid):
    try:
        fields = Path("/proc/{}/stat".format(pid)).read_text(
            encoding="ascii"
        ).rsplit(")", 1)[1].split()
        if fields[0] in ("Z", "X"):
            return None
        # Fields after comm begin with state, ppid, pgrp, session.
        return int(fields[3]), int(fields[2])
    except (OSError, IndexError, ValueError):
        return None


@pytest.mark.skipif(not renderer_available(), reason="supervisor crash test requires pyte")
@pytest.mark.skipif(not Path("/proc").is_dir(), reason="Linux process identities are required")
def test_supervisor_crash_makes_session_stale_and_shell_exits(tmp_path, monkeypatch):
    import tmux_agent_tower.terminal.manager as manager

    _safe_test_home(monkeypatch, tmp_path)
    registry = TerminalRegistry(root=tmp_path / "r")
    worker_processes = []
    popen = manager.subprocess.Popen

    def capture_worker(*args, **kwargs):
        process = popen(*args, **kwargs)
        worker_processes.append(process)
        return process

    monkeypatch.setattr(manager.subprocess, "Popen", capture_worker)
    client = registry.create("crashed supervisor fixture")
    session_id = client.identity.session_id
    shell_pid = client.identity.shell_pid
    supervisor_pid = client.identity.supervisor_pid
    assert len(worker_processes) == 1
    assert worker_processes[0].pid == supervisor_pid
    client.close()

    try:
        if _proc_start(shell_pid) is None:
            registry.stop(session_id)
            worker_processes[0].wait(timeout=5)
            pytest.skip("fixture shell exited before the supervisor-crash check")
        os.kill(supervisor_pid, signal.SIGKILL)
        worker_processes[0].wait(timeout=5)
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if (_proc_start(supervisor_pid) is None
                    and _proc_start(shell_pid) is None):
                break
            time.sleep(0.02)

        assert _proc_start(supervisor_pid) is None
        assert _proc_start(shell_pid) is None
        listed = registry.list_sessions()
        assert len(listed) == 1
        assert listed[0].session_id == session_id
        # The worker was killed before it could persist an exit boundary, so
        # process disappearance alone is not proof of a normal shell exit.
        assert listed[0].state == "orphaned"
        with pytest.raises(TerminalError, match="supervisor identity is stale"):
            registry.connect(session_id)
    finally:
        if _proc_start(shell_pid) is not None:
            _worker._signal_session_processes(shell_pid, signal.SIGKILL)


@pytest.mark.skipif(not Path("/proc").is_dir(), reason="Linux process identities are required")
@pytest.mark.parametrize("unsupported_call", ["pidfd_open", "pidfd_send_signal"])
def test_pidfd_syscall_unavailable_falls_back_to_checked_pid_signal(
        monkeypatch, unsupported_call):
    if unsupported_call == "pidfd_send_signal" and not hasattr(signal, "pidfd_send_signal"):
        pytest.skip("Python does not expose pidfd_send_signal")
    child = subprocess.Popen(["/bin/sleep", "30"], start_new_session=True)
    try:
        if unsupported_call == "pidfd_open":
            def unavailable(*_args, **_kwargs):
                raise OSError(_worker.errno.ENOSYS, "pidfd_open unavailable")

            monkeypatch.setattr(_worker.os, "pidfd_open", unavailable)
        else:
            def unavailable(*_args, **_kwargs):
                raise OSError(_worker.errno.ENOSYS, "pidfd_send_signal unavailable")

            monkeypatch.setattr(signal, "pidfd_send_signal", unavailable)

        _worker._signal_session_processes(child.pid, signal.SIGTERM)
        assert child.wait(timeout=3) == -signal.SIGTERM
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=3)


@pytest.mark.skipif(not renderer_available(), reason="process cleanup requires optional pyte renderer")
@pytest.mark.skipif(not Path("/bin/bash").exists(), reason="bash is not available")
@pytest.mark.parametrize("exit_shell_first", [False, True])
def test_explicit_stop_terminates_child_in_separate_process_group(
        tmp_path, monkeypatch, exit_shell_first):
    account_shell = Path(pwd.getpwuid(os.getuid()).pw_shell).resolve()
    if account_shell != Path("/bin/bash").resolve():
        pytest.skip("test contract deliberately uses the account's /bin/bash login shell")
    _safe_test_home(monkeypatch, tmp_path)
    registry = TerminalRegistry(root=tmp_path / "r")
    client = registry.create("process group cleanup")
    session_id = client.identity.session_id
    shell_pid = client.identity.shell_pid
    other = None
    other_session_id = None
    other_shell_pid = None
    child_pid = None
    try:
        other = registry.create("unrelated terminal")
        other_session_id = other.identity.session_id
        other_shell_pid = other.identity.shell_pid
        assert other_session_id != session_id
        assert _proc_start(other_shell_pid) is not None
        assert client.acquire()
        marker = "JTT_CHILD_PG_" + os.urandom(6).hex()
        command = (
            "python3 -c 'import os,signal,time; os.setpgid(0,0); "
            "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            "print(\"{}\" + str(os.getpid()), flush=True); time.sleep(60)' &\n"
        ).format(marker)
        client.send(command.encode("utf-8"))
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            text = _screen_text(client.poll())
            match = re.search(re.escape(marker) + r"(\d+)", text)
            if match:
                child_pid = int(match.group(1))
                break
            time.sleep(0.03)
        assert child_pid is not None, "fixture child PID marker was not rendered"
        assert _proc_start(child_pid) is not None
        assert _proc_session_and_group(child_pid) == (shell_pid, child_pid)
        assert child_pid != shell_pid

        if exit_shell_first:
            # Disown the fixture job so Bash does not send it SIGHUP on exit;
            # the terminal stop must still clean its retained PTY session.
            client.send(b"disown -a\nexit\n")
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline:
                client.poll()
                if client.state == "exited":
                    break
                time.sleep(0.03)
            assert client.state == "exited"
            assert _proc_start(child_pid) is not None, (
                "Bash exit unexpectedly ended the disowned fixture child"
            )

        registry.stop(session_id)
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and _proc_start(child_pid) is not None:
            time.sleep(0.02)
        assert _proc_start(child_pid) is None, (
            "explicit stop left a live child in another process group "
            "within the shell's session"
        )
        assert _proc_start(other_shell_pid) is not None, (
            "stopping one terminal affected a different terminal session"
        )
    finally:
        client.close()
        # Contain the intentionally failing pre-fix case: never leave its
        # private fixture child behind if the assertion above fails.
        if child_pid is not None and _proc_start(child_pid) is not None:
            identity = _proc_session_and_group(child_pid)
            if identity == (shell_pid, child_pid):
                try:
                    os.killpg(child_pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        try:
            if (tmp_path / "r" / session_id).exists():
                registry.stop(session_id)
        except (OSError, TerminalError):
            pass
        if other is not None:
            other.close()
            try:
                if other_session_id and (tmp_path / "r" / other_session_id).exists():
                    registry.stop(other_session_id)
            except (OSError, TerminalError):
                pass
