"""Local-only PTY lifecycle tests; these never invoke tmux or an agent CLI."""

import json
import base64
import os
from pathlib import Path
import pwd
import random
import shutil
import signal
import stat
import time
import zlib

import pytest

from tmux_agent_tower.terminal import TerminalError, TerminalRegistry, renderer_available
from tmux_agent_tower.terminal.manager import MAX_FRAME, _ensure_private_root, _worker_is_same_process
from tmux_agent_tower.terminal import _worker


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
    # Keep the full session socket under sockaddr_un's platform path limit.
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
    assert stat.S_IMODE((session_dir / "s").stat().st_mode) == 0o600
    metadata = json.loads((session_dir / "session.json").read_text())
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


def test_optional_renderer_absence_blocks_input_and_does_not_autostart(tmp_path, monkeypatch):
    if not renderer_available():
        pytest.skip("system Python has no pyte; fail-closed branch is already the default")
    account_shell = Path(pwd.getpwuid(os.getuid()).pw_shell).resolve()
    if account_shell != Path("/bin/bash").resolve():
        pytest.skip("test contract deliberately uses the account's /bin/bash login shell")
    _safe_test_home(monkeypatch, tmp_path)
    # Do not inherit the test-only pyte path into the worker. No dependency is installed.
    monkeypatch.delenv("PYTHONPATH", raising=False)
    registry = TerminalRegistry(root=tmp_path / "r")
    client = registry.create("renderer unavailable")
    session_id = client.identity.session_id
    try:
        update = client.poll()
        assert not update.renderer_available
        assert not update.synced
        assert update.screen is None
        with pytest.raises(TerminalError, match="stream_gap|renderer_unavailable"):
            client.acquire()
        assert not client.lease
    finally:
        client.close()
        registry.stop(session_id)


def _proc_start(pid):
    try:
        raw = Path("/proc/{}/stat".format(pid)).read_text(encoding="ascii")
        return raw.rsplit(")", 1)[1].split()[19]
    except (OSError, IndexError, ValueError):
        return None
