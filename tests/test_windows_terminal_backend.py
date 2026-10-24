"""Native Windows integration tests using only isolated JTT-owned sessions."""
from __future__ import annotations

import base64
import subprocess
import sys
import threading
import time
import uuid

import pytest


pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="native Windows required")


def test_worker_interpreter_preserves_process_identity(tmp_path):
    import ju0_terminal_tower  # noqa: F401 - installs the guarded shared namespace
    from tmux_agent_tower.terminal.windows_manager import (
        _worker_environment,
        _worker_executable,
    )

    executable = _worker_executable()
    process = subprocess.Popen(
        [str(executable), "-S", "-c", "import os; print(os.getpid(), flush=True)"],
        cwd=tmp_path,
        env=_worker_environment(tmp_path),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        shell=False,
    )
    try:
        reported_pid = int(process.stdout.readline().strip())
        assert process.wait(timeout=5) == 0
        assert reported_pid == process.pid
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=5)


def _screen_text(screen):
    return "\n".join("".join(cell.text for cell in row) for row in screen.rows)


def _compact_screen_text(screen):
    return "".join(line.rstrip() for line in _screen_text(screen).splitlines())


def _screen_contains_after(screen, marker, text):
    compact = _compact_screen_text(screen)
    marker_index = compact.rfind(marker)
    return (
        marker_index >= 0
        and compact.find(text, marker_index + len(marker)) >= 0
    )


def _expected_prompt_path(path, columns):
    from pathlib import PureWindowsPath

    text = str(path)
    limit = max(12, columns - 36)
    if len(text) > limit:
        root = PureWindowsPath(text).anchor
        if len(root) > limit - 5:
            root = root[:limit - 5]
        tail = max(1, limit - len(root) - 4)
        text = root + "...\\" + text[-tail:]
    return "PS " + text + ">"


def _wait(client, predicate, timeout=15):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = client.poll()
        if predicate(last):
            return last
        time.sleep(0.02)
    raise AssertionError(
        "Windows terminal condition not observed: "
        f"state={getattr(last, 'state', None)!r}; "
        f"renderer={getattr(last, 'renderer_state', None)!r}; "
        f"sequence={getattr(last, 'sequence', None)!r}"
    )


def _wait_for_raw_output(
        client, expected: bytes, timeout=15, *, checkpoints=(),
        screen_checkpoints=()):
    """Collect actual ConPTY bytes; console soft-wraps may split output markers."""
    deadline = time.monotonic() + timeout
    output = bytearray()
    last = None
    while time.monotonic() < deadline:
        last = client.poll()
        output.extend(last.output)
        compact_output = bytes(output).replace(b"\r", b"").replace(b"\n", b"")
        if expected in compact_output:
            return last, bytes(output)
        time.sleep(0.02)
    compact_output = bytes(output).replace(b"\r", b"").replace(b"\n", b"")
    seen_checkpoints = tuple(
        label for label, marker in checkpoints if marker in compact_output
    )
    screen_text = _compact_screen_text(last.screen) if last and last.screen else ""
    seen_screen_checkpoints = tuple(
        label for label, marker in screen_checkpoints if marker in screen_text
    )
    raise AssertionError(
        "PowerShell output marker missing from ConPTY stream: "
        f"state={getattr(last, 'state', None)!r}; "
        f"renderer={getattr(last, 'renderer_state', None)!r}; "
        f"sequence={getattr(last, 'sequence', None)!r}; "
        f"observed_checkpoints={seen_checkpoints!r}; "
        f"screen_checkpoints={seen_screen_checkpoints!r}; "
        f"continuation_prompt={'>>' in screen_text!r}; "
        f"marker={expected!r}"
    )


def _generated_marker(client, label):
    nonce = uuid.uuid4().hex[:10]
    expected = f"JTT_{label}_{nonce}_END"
    command = f"[Console]::WriteLine('JTT_{label}_' + '{nonce}' + '_END')"
    assert expected not in command
    client.send((command + "\r").encode("ascii"))
    _wait(client, lambda update: update.screen is not None
          and expected in _screen_text(update.screen))
    return expected


def test_native_powershell_create_io_resize_detach_reconnect_stop(tmp_path, monkeypatch):
    import ju0_terminal_tower  # noqa: F401 - installs the guarded shared namespace
    from tmux_agent_tower.terminal import TerminalError, TerminalRegistry
    from tmux_agent_tower.terminal.windows_manager import (
        _terminate_owned_powershell,
    )

    work = tmp_path / "isolated-work"
    work.mkdir()
    monkeypatch.chdir(work)
    registry = TerminalRegistry(tmp_path / "isolated-jtt" / "terminals")
    client = registry.create("Isolated PowerShell")
    identity = client.identity
    second = None
    try:
        assert identity.state == "running"
        assert identity.shell_pid > 0
        assert identity.generation
        assert not _terminate_owned_powershell(
            identity.shell_pid, identity.shell_start + "-stale", identity.shell_path
        )
        assert _process_same(identity.shell_pid, identity.shell_start)
        _wait(client, lambda update: update.screen is not None
              and _expected_prompt_path(work, 100)
              in _compact_screen_text(update.screen))
        assert client.acquire()
        marker = _generated_marker(client, "CREATE")
        assert marker

        # A second client may observe the same live screen but cannot steal the
        # controller's exclusive input lease.
        second = registry.connect(identity.session_id)
        observed = second.observe()
        assert observed.synced
        assert marker in _screen_text(observed.screen)
        assert not second.acquire()
        with pytest.raises(Exception, match="input_lease_required"):
            second.send(b"observer-must-not-write")
        second.close()
        second = None

        client.resize(80, 24)
        _generated_marker(client, "RESIZE")
        width_nonce = uuid.uuid4().hex[:8]
        width = f"JTT_WIDTH_{width_nonce}_80_END"
        command = (
            f"[Console]::WriteLine('JTT_WIDTH_' + '{width_nonce}' + '_' + "
            "[Console]::WindowWidth + '_END')"
        )
        assert width not in command
        client.send((command + "\r").encode("ascii"))
        resized = _wait(client, lambda update: update.screen is not None
                        and width in _screen_text(update.screen))
        assert (resized.columns, resized.lines) == (80, 24)

        korean_nonce = uuid.uuid4().hex[:8]
        korean_marker = f"JTT_KO_{korean_nonce}_한글_END"
        korean_command = (
            f"[Console]::WriteLine('JTT_KO_{korean_nonce}_' + "
            "[char]0xD55C + [char]0xAE00 + '_END')"
        )
        assert korean_marker not in korean_command
        client.send((korean_command + "\r").encode("ascii"))
        unicode_update = _wait(client, lambda update: update.screen is not None
                               and korean_marker in _screen_text(update.screen))
        assert (unicode_update.columns, unicode_update.lines) == (80, 24)

        stale_identity = {
            "session_id": identity.session_id,
            "generation": "0" * 32,
            "supervisor_pid": identity.supervisor_pid,
            "supervisor_start": identity.supervisor_start,
            "shell_pid": identity.shell_pid,
            "shell_start": identity.shell_start,
        }
        with pytest.raises(Exception, match="stale_identity"):
            client._request("input", identity=stale_identity,
                            after=client._output_cursor,
                            data=base64.b64encode(b"forbidden").decode("ascii"))

        client.close()
        client = None
        second = registry.connect(identity.session_id)
        assert second.identity.session_id == identity.session_id
        assert second.identity.generation == identity.generation
        assert second.identity.shell_pid == identity.shell_pid
        observation = second.observe()
        assert observation.synced
        assert korean_marker in _screen_text(observation.screen)
        with pytest.raises(Exception, match="input_lease_required"):
            second.send(b"must-not-be-delivered")
        assert _process_same(identity.shell_pid, identity.shell_start)

        second.close()
        second = None
        with pytest.raises(TerminalError, match="identity changed before stop"):
            registry.stop(identity.session_id,
                          expected_generation="0" * 32,
                          expected_shell_pid=identity.shell_pid)
        with pytest.raises(TerminalError, match="identity changed before stop"):
            registry.stop(identity.session_id,
                          expected_generation=identity.generation,
                          expected_shell_pid=identity.shell_pid + 1)
        assert _process_same(identity.shell_pid, identity.shell_start)
        registry.stop(identity.session_id,
                      expected_generation=identity.generation,
                      expected_shell_pid=identity.shell_pid)
        assert not (registry.root / identity.session_id).exists()
        assert registry.list_sessions() == ()
    finally:
        if client is not None:
            client.close()
        if second is not None:
            second.close()
        # Only the session created by this test is named here; identity-checked
        # stop is attempted to prevent leaving its worker behind on assertion.
        try:
            registry.stop(identity.session_id,
                          expected_generation=identity.generation,
                          expected_shell_pid=identity.shell_pid)
        except Exception:
            pass


def test_windows_multi_observer_lease_is_atomic_and_disconnect_releases(tmp_path, monkeypatch):
    import ju0_terminal_tower  # noqa: F401 - installs the guarded shared namespace
    from tmux_agent_tower.terminal import TerminalError, TerminalRegistry

    work = tmp_path / "isolated-work"
    work.mkdir()
    monkeypatch.chdir(work)
    registry = TerminalRegistry(tmp_path / "isolated-jtt" / "terminals")
    controller = registry.create("Multi-client observers")
    identity = controller.identity
    first = second = None
    try:
        assert _process_same(identity.shell_pid, identity.shell_start)
        assert controller.acquire()
        initial_marker = _generated_marker(controller, "MULTI_INITIAL")
        first = registry.connect(identity.session_id)
        second = registry.connect(identity.session_id)
        for observer in (first, second):
            observation = observer.observe()
            assert observation.synced
            assert observation.session_id == identity.session_id
            assert observation.generation == identity.generation
            assert initial_marker in _screen_text(observation.screen)

        # Concurrent acquisition requests must serialize to one lease owner.
        controller.release()
        gate = threading.Barrier(3)
        acquired = []
        failures = []

        def contend(client):
            try:
                gate.wait(timeout=5)
                acquired.append((client, client.acquire()))
            except Exception as exc:
                failures.append(exc)

        workers = [threading.Thread(target=contend, args=(client,))
                   for client in (first, second)]
        for thread in workers:
            thread.start()
        gate.wait(timeout=5)
        for thread in workers:
            thread.join(timeout=8)
            assert not thread.is_alive(), "concurrent lease request did not finish"
        assert failures == []
        owners = [client for client, result in acquired if result]
        non_owners = [client for client, result in acquired if not result]
        assert len(owners) == 1
        assert len(non_owners) == 1
        owner, observer = owners[0], non_owners[0]
        with pytest.raises(Exception, match="input_lease_required"):
            observer.send(b"concurrent-observer-must-not-write")

        owner_marker = _generated_marker(owner, "MULTI_OWNER")
        observed = _wait(observer, lambda update: update.screen is not None
                         and owner_marker in _screen_text(update.screen))
        assert observed.synced
        assert owner_marker in _screen_text(observed.screen)
        assert _process_same(identity.shell_pid, identity.shell_start)

        # The lease is connection-scoped and is released when its owner leaves.
        owner.close()
        if owner is first:
            first = None
        else:
            second = None
        assert observer.acquire()
        transferred_marker = _generated_marker(observer, "MULTI_TRANSFER")
        visible = _wait(controller, lambda update: update.screen is not None
                        and transferred_marker in _screen_text(update.screen))
        assert visible.synced
        assert _process_same(identity.shell_pid, identity.shell_start)

        # The original observing connection still cannot acquire while the
        # transferred lease is held.
        assert not controller.acquire()
        with pytest.raises(Exception, match="input_lease_required"):
            controller.send(b"stale-observer-must-not-write")

        # Keep the former controller connected as a read-only observer while
        # stopping the session. The stop request uses its own short-lived
        # authenticated connection and must close idle pipe reads safely.
        observer.release()
        assert not observer.lease

        registry.stop(identity.session_id,
                      expected_generation=identity.generation,
                      expected_shell_pid=identity.shell_pid)
        assert not _process_same(identity.shell_pid, identity.shell_start)
        with pytest.raises(TerminalError, match="terminal supervisor connection failed"):
            observer.observe()
        assert registry.list_sessions() == ()
    finally:
        for client in (controller, first, second):
            if client is not None:
                try:
                    client.close()
                except Exception:
                    pass
        try:
            registry.stop(identity.session_id,
                          expected_generation=identity.generation,
                          expected_shell_pid=identity.shell_pid)
        except Exception:
            pass


def test_windows_observer_capacity_is_bounded_and_disconnect_frees_slot(
    tmp_path, monkeypatch
):
    import ju0_terminal_tower  # noqa: F401 - installs the guarded shared namespace
    from tmux_agent_tower.terminal import TerminalError, TerminalRegistry
    from tmux_agent_tower.terminal import _windows_worker

    work = tmp_path / "isolated-work"
    work.mkdir()
    monkeypatch.chdir(work)
    registry = TerminalRegistry(tmp_path / "isolated-jtt" / "terminals")
    controller = registry.create("Bounded observers")
    identity = controller.identity
    clients = [controller]
    try:
        for _ in range(_windows_worker.MAX_PIPE_CLIENTS - 1):
            clients.append(registry.connect(identity.session_id))
        for client in clients:
            assert client.observe().synced

        with pytest.raises(TerminalError) as saturated:
            registry.connect(identity.session_id, timeout=0.5)
        assert str(saturated.value) in {
            "terminal supervisor connection failed",
            "named pipe server process could not be verified",
        }
        assert registry.list_sessions()
        for client in clients:
            assert client.observe().synced

        released = clients.pop()
        released.close()
        replacement = registry.connect(identity.session_id, timeout=3)
        clients.append(replacement)
        assert replacement.observe().synced

        for client in clients:
            client.close()
        clients.clear()
        registry.stop(identity.session_id,
                      expected_generation=identity.generation,
                      expected_shell_pid=identity.shell_pid)
        assert registry.list_sessions() == ()
    finally:
        for client in clients:
            try:
                client.close()
            except Exception:
                pass
        try:
            registry.stop(identity.session_id,
                          expected_generation=identity.generation,
                          expected_shell_pid=identity.shell_pid)
        except Exception:
            pass


def test_windows_repeated_observer_connect_disconnect_preserves_session(
    tmp_path, monkeypatch
):
    import ju0_terminal_tower  # noqa: F401 - installs the guarded shared namespace
    from tmux_agent_tower.terminal import TerminalRegistry

    work = tmp_path / "isolated-work"
    work.mkdir()
    monkeypatch.chdir(work)
    registry = TerminalRegistry(tmp_path / "isolated-jtt" / "terminals")
    controller = registry.create("Repeated observer churn")
    identity = controller.identity
    try:
        assert _process_same(identity.shell_pid, identity.shell_start)
        assert controller.observe().synced

        # Exercise the real named-pipe accept/worker/finally path repeatedly.
        # Every connection is an observer; no reconnect acquires the lease.
        for _ in range(32):
            observer = registry.connect(identity.session_id, timeout=5)
            try:
                observation = observer.observe()
                assert observation.synced
                assert observation.session_id == identity.session_id
                assert observation.generation == identity.generation
                assert observation.shell_pid == identity.shell_pid
                assert not observer.lease
            finally:
                observer.close()
            assert _process_same(identity.shell_pid, identity.shell_start)

        assert controller.observe().synced
        assert _process_same(identity.shell_pid, identity.shell_start)
        registry.stop(identity.session_id,
                      expected_generation=identity.generation,
                      expected_shell_pid=identity.shell_pid)
        assert not _process_same(identity.shell_pid, identity.shell_start)
        assert registry.list_sessions() == ()
    finally:
        try:
            controller.close()
        except Exception:
            pass
        try:
            registry.stop(identity.session_id,
                          expected_generation=identity.generation,
                          expected_shell_pid=identity.shell_pid)
        except Exception:
            pass


def test_process_access_denied_preserves_session_registry(tmp_path, monkeypatch):
    import pywintypes
    import win32api

    import ju0_terminal_tower  # noqa: F401
    from tmux_agent_tower.terminal import TerminalRegistry
    from tmux_agent_tower.terminal.manager import TerminalError

    work = tmp_path / "isolated-work"
    work.mkdir()
    monkeypatch.chdir(work)
    registry = TerminalRegistry(tmp_path / "isolated-jtt" / "terminals")
    client = registry.create("Access denied remains unknown")
    identity = client.identity
    try:
        client.close()
        def denied(*_args, **_kwargs):
            raise pywintypes.error(5, "OpenProcess", "Access is denied")

        with monkeypatch.context() as patch:
            patch.setattr(win32api, "OpenProcess", denied)
            with pytest.raises(TerminalError, match="process identity could not be verified"):
                registry.stop(identity.session_id,
                              expected_generation=identity.generation,
                              expected_shell_pid=identity.shell_pid)

        assert (registry.root / identity.session_id / "session.json").is_file()
        assert _process_same(identity.shell_pid, identity.shell_start)
    finally:
        try:
            client.close()
        except Exception:
            pass
        try:
            registry.stop(identity.session_id,
                          expected_generation=identity.generation,
                          expected_shell_pid=identity.shell_pid)
        except Exception:
            pass


def test_windows_metadata_reader_allows_atomic_replace_while_open(tmp_path, monkeypatch):
    import ju0_terminal_tower  # noqa: F401
    import win32file

    from tmux_agent_tower.terminal.windows_manager import (
        _atomic_metadata,
        _ensure_private_directory,
        _read_metadata,
    )
    from tmux_agent_tower.terminal import windows_manager

    root = tmp_path / "isolated-jtt" / "terminals"
    _ensure_private_directory(root)
    path = root / "session.json"
    _atomic_metadata(path, {"generation": "old"})
    original_read = win32file.ReadFile
    opened = threading.Event()
    resume = threading.Event()
    replace_denied = threading.Event()
    results = []
    failures = []
    original_replace = windows_manager.os.replace

    def blocking_read(handle, size):
        opened.set()
        if not resume.wait(5):
            raise AssertionError("metadata reader release timed out")
        return original_read(handle, size)

    def reader():
        try:
            results.append(_read_metadata(path))
        except Exception as exc:  # captured for assertion on the test thread
            failures.append(type(exc).__name__)

    def observed_replace(source, destination):
        try:
            return original_replace(source, destination)
        except OSError as exc:
            if getattr(exc, "winerror", None) == 5:
                replace_denied.set()
            raise

    def writer():
        try:
            _atomic_metadata(path, {"generation": "new"})
        except Exception as exc:  # captured for assertion on the test thread
            failures.append(type(exc).__name__)

    monkeypatch.setattr(win32file, "ReadFile", blocking_read)
    monkeypatch.setattr(windows_manager.os, "replace", observed_replace)
    thread = threading.Thread(target=reader, daemon=True)
    writer_thread = threading.Thread(target=writer, daemon=True)
    thread.start()
    try:
        assert opened.wait(5), "metadata reader did not open the file"
        writer_thread.start()
        assert replace_denied.wait(5), "held metadata reader did not block replacement"
    finally:
        resume.set()
        thread.join(5)
        if writer_thread.ident is not None:
            writer_thread.join(5)
    assert not writer_thread.is_alive(), "metadata writer did not finish after reader closed"
    assert not thread.is_alive(), "metadata reader thread did not finish"
    assert failures == []
    assert results == [{"generation": "old"}]
    assert _read_metadata(path) == {"generation": "new"}


@pytest.mark.parametrize("process_signaled_after_query", [True, False])
def test_process_image_query_race_fails_closed_unless_handle_is_signaled(
    monkeypatch, process_signaled_after_query
):
    import ju0_terminal_tower  # noqa: F401
    import win32api
    import win32con
    import win32event
    import win32process

    from tmux_agent_tower.terminal import TerminalError
    from tmux_agent_tower.terminal import windows_manager

    class FakeHandle:
        closed = False

        def __int__(self):
            return 1234

        def Close(self):
            self.closed = True

    handle = FakeHandle()
    second_wait = (win32con.WAIT_OBJECT_0 if process_signaled_after_query
                   else win32con.WAIT_TIMEOUT)
    waits = iter((win32con.WAIT_TIMEOUT, second_wait))
    monkeypatch.setattr(win32api, "OpenProcess", lambda *_args, **_kwargs: handle)
    monkeypatch.setattr(
        win32event, "WaitForSingleObject", lambda *_args: next(waits)
    )
    monkeypatch.setattr(win32process, "GetProcessTimes", lambda _handle: {
        "CreationTime": "fixture-start",
    })

    class Kernel32:
        @staticmethod
        def QueryFullProcessImageNameW(*_args):
            return 0

    monkeypatch.setattr(windows_manager.ctypes, "WinDLL", lambda *_args, **_kwargs: Kernel32())
    if not process_signaled_after_query:
        with pytest.raises(TerminalError, match="process image could not be verified"):
            windows_manager._process_facts(1234)
    else:
        assert windows_manager._process_facts(1234) == ("", "", False)
    assert handle.closed


def test_process_facts_skips_image_query_for_reused_pid(monkeypatch):
    import ju0_terminal_tower  # noqa: F401
    import win32api
    import win32con
    import win32event
    import win32process

    from tmux_agent_tower.terminal import windows_manager

    class FakeHandle:
        def Close(self):
            self.closed = True

    handle = FakeHandle()
    handle.closed = False
    monkeypatch.setattr(win32api, "OpenProcess", lambda *_args, **_kwargs: handle)
    monkeypatch.setattr(
        win32event, "WaitForSingleObject",
        lambda *_args: win32con.WAIT_TIMEOUT,
    )
    monkeypatch.setattr(win32process, "GetProcessTimes", lambda _handle: {
        "CreationTime": "new-process-start",
    })

    def unexpected_image_query(*_args, **_kwargs):
        raise AssertionError("a reused PID must not be image-queried as the old process")

    monkeypatch.setattr(windows_manager.ctypes, "WinDLL", unexpected_image_query)
    assert windows_manager._process_facts(
        1234, expected_start="old-process-start"
    ) == ("new-process-start", "", True)
    assert handle.closed


def test_native_shell_exit_remains_listed_until_explicit_owned_cleanup(tmp_path, monkeypatch):
    import ju0_terminal_tower  # noqa: F401
    from tmux_agent_tower.terminal import TerminalRegistry

    work = tmp_path / "isolated-work"
    work.mkdir()
    monkeypatch.chdir(work)
    registry = TerminalRegistry(tmp_path / "isolated-jtt" / "terminals")
    client = registry.create("Shell exits first")
    identity = client.identity
    try:
        assert client.acquire()
        client.send(b"exit\r")
        _wait(client, lambda update: update.state == "exited", timeout=15)
        rows = {item.session_id: item for item in registry.list_sessions()}
        assert rows[identity.session_id].state == "exited"
        client.close()
        registry.stop(identity.session_id,
                      expected_generation=identity.generation,
                      expected_shell_pid=identity.shell_pid)
        assert registry.list_sessions() == ()
    finally:
        try:
            client.close()
        except Exception:
            pass
        try:
            registry.stop(identity.session_id,
                          expected_generation=identity.generation,
                          expected_shell_pid=identity.shell_pid)
        except Exception:
            pass


def _process_same(pid, created):
    from tmux_agent_tower.terminal.windows_manager import _process_facts

    actual_created, _image, alive = _process_facts(pid)
    return alive and actual_created == created


def test_job_assignment_failure_terminates_only_retained_child_handle():
    from ju0_terminal_tower.terminal._windows_worker import assign_owned_job

    class JobApi:
        @staticmethod
        def AssignProcessToJobObject(_job, _handle):
            raise RuntimeError("injected assignment rejection")

    class ProcessApi:
        calls = []

        @classmethod
        def TerminateProcess(cls, handle, code):
            cls.calls.append((handle, code))

    class EventApi:
        @staticmethod
        def WaitForSingleObject(_handle, _timeout):
            return 0

    class Constants:
        WAIT_OBJECT_0 = 0

    with pytest.raises(RuntimeError, match="injected assignment rejection"):
        assign_owned_job("owned-job", "retained-child-handle", JobApi,
                         ProcessApi, EventApi, Constants)
    assert ProcessApi.calls == [("retained-child-handle", 1)]


def test_powershell_exits_when_named_startup_gate_cannot_be_opened():
    import ju0_terminal_tower  # noqa: F401
    from tmux_agent_tower.terminal._windows_worker import _bootstrap_script
    from tmux_agent_tower.terminal.windows_manager import _powershell_executable

    missing = "Local\\JTT-missing-" + uuid.uuid4().hex
    process = subprocess.run(
        [str(_powershell_executable()), "-NoLogo", "-NoProfile", "-NoExit",
         "-Command", _bootstrap_script(missing, missing + "-abort")],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        timeout=10, check=False,
    )
    assert process.returncode == 71
    assert b"PS " not in process.stdout + process.stderr


def test_spawned_powershell_handle_failure_aborts_child_and_keeps_registry(
    tmp_path, monkeypatch
):
    import ju0_terminal_tower  # noqa: F401
    import pywintypes
    import win32api
    import win32con
    import win32process

    from tmux_agent_tower.terminal import windows_manager as runtime
    from tmux_agent_tower.terminal._windows_worker import main as worker_main

    root = tmp_path / "isolated-jtt" / "terminals"
    runtime._ensure_private_directory(root)
    session_id, generation = uuid.uuid4().hex, uuid.uuid4().hex
    session_dir = root / session_id
    session_dir.mkdir()
    runtime._private_acl(session_dir, directory=True, apply=True)
    working = tmp_path / "isolated-work"
    working.mkdir()
    metadata_path = session_dir / runtime.SESSION_META
    runtime._atomic_metadata(metadata_path, {
        "protocol": runtime.PROTOCOL,
        "session_id": session_id,
        "generation": generation,
        "supervisor_pid": 0,
        "supervisor_start": "",
        "shell_pid": 0,
        "shell_start": "",
        "shell_path": str(runtime._powershell_executable()),
        "name": "Injected handle acquisition failure",
        "state": "starting",
        "renderer_state": "starting",
        "pipe_name": runtime._planned_socket_path(session_dir, session_id),
        "working_directory": str(working),
        "spawn_attempted": False,
    })

    original_open = win32api.OpenProcess
    captured = {}
    handle_attempt = threading.Event()
    test_errors = []

    def deny_worker_handle(access, inherit, pid):
        if not captured:
            # Independent read-only test observer captures the actual child
            # identity before injecting the worker's retained-handle failure.
            observer = original_open(
                win32con.PROCESS_QUERY_LIMITED_INFORMATION | win32con.SYNCHRONIZE,
                False, pid,
            )
            try:
                captured.update({
                    "pid": pid,
                    "start": str(win32process.GetProcessTimes(observer)["CreationTime"]),
                    "image": runtime._process_image(observer),
                })
            finally:
                observer.Close()
            handle_attempt.set()
            raise pywintypes.error(5, "OpenProcess", "injected access denial")
        return original_open(access, inherit, pid)

    monkeypatch.setattr(win32api, "OpenProcess", deny_worker_handle)

    def run_worker():
        try:
            test_errors.append(worker_main([
                "--worker", str(session_dir), session_id, generation,
                "Injected handle acquisition failure",
            ]))
        except Exception as exc:
            test_errors.append(exc)

    thread = threading.Thread(target=run_worker, daemon=True)
    thread.start()
    assert handle_attempt.wait(15), "ConPTY PowerShell did not reach handle acquisition"
    thread.join(10)
    assert not thread.is_alive(), "worker did not exit after startup handle failure"
    assert test_errors == [1]
    assert captured["image"].casefold() == str(
        runtime._powershell_executable()
    ).casefold()

    metadata = runtime._read_metadata(metadata_path)
    assert metadata["spawn_attempted"] is True
    assert metadata["shell_pid"] == captured["pid"]
    assert metadata["shell_start"] == ""
    assert metadata["startup_failure"]["stage"] == "shell_handle_open"

    deadline = time.monotonic() + 10
    exact_child_alive = True
    while time.monotonic() < deadline:
        created, _image, alive = runtime._process_facts(
            captured["pid"], expected_start=captured["start"]
        )
        exact_child_alive = alive and created == captured["start"]
        if not exact_child_alive:
            break
        time.sleep(0.02)
    try:
        assert not exact_child_alive, (
            "the exact gated PowerShell process remained alive after worker failure"
        )
    finally:
        if exact_child_alive:
            # Cleanup is permitted only through the identity-checked helper.
            runtime._terminate_owned_powershell(
                captured["pid"], captured["start"], captured["image"]
            )

    # The worker lacked a retained process handle and therefore cannot certify
    # cleanup itself. Its registry entry must remain for explicit recovery.
    assert metadata_path.is_file()
    rows = {item.session_id: item for item in runtime.TerminalRegistry(root).list_sessions()}
    assert session_id in rows


@pytest.mark.parametrize(("shell_pid", "shell_start"), [(0, ""), (43210, "")])
def test_failed_startup_without_full_shell_identity_preserves_record(
    tmp_path, monkeypatch, shell_pid, shell_start
):
    import ju0_terminal_tower  # noqa: F401
    from tmux_agent_tower.terminal import TerminalError
    from tmux_agent_tower.terminal import windows_manager as runtime

    root = tmp_path / "isolated-jtt" / "terminals"
    runtime._ensure_private_directory(root)
    session_id, generation = uuid.uuid4().hex, uuid.uuid4().hex
    session_dir = root / session_id
    session_dir.mkdir()
    runtime._private_acl(session_dir, directory=True, apply=True)
    metadata_path = session_dir / runtime.SESSION_META
    runtime._atomic_metadata(metadata_path, {
        "protocol": runtime.PROTOCOL,
        "session_id": session_id,
        "generation": generation,
        "supervisor_pid": 0,
        "supervisor_start": "",
        "shell_pid": shell_pid,
        "shell_start": shell_start,
        "shell_path": str(runtime._powershell_executable()),
        "name": "Unknown startup ownership",
        "state": "starting",
        "renderer_state": "starting",
        "spawn_attempted": True,
        "startup_failure": {"stage": "shell_handle_open", "exception": "error"},
    })

    def forbidden(*_args, **_kwargs):
        raise AssertionError("unknown PID must not be signaled or registry removed")

    monkeypatch.setattr(runtime, "_terminate_owned_powershell", forbidden)
    monkeypatch.setattr(runtime, "_remove_session_files", forbidden)
    with pytest.raises(TerminalError, match="registry retained"):
        runtime._cleanup_failed_startup(
            session_dir, session_id, generation, "fixture"
        )
    assert metadata_path.is_file()


def test_native_input_rejects_output_gap_without_preflight_poll(tmp_path):
    import ju0_terminal_tower  # noqa: F401 - installs the guarded shared namespace
    from tmux_agent_tower.terminal import TerminalError, TerminalRegistry

    registry = TerminalRegistry(tmp_path / "isolated-jtt" / "terminals")
    client = registry.create("Bounded stream input guard")
    identity = client.identity
    try:
        assert client.acquire()
        # Generate more than the bounded stream capacity without polling the
        # controller. observe() sees the current screen but does not advance
        # the client's output cursor.
        nonce = identity.session_id[:10]
        marker = f"JTT_STREAM_GAP_{nonce}_DONE"
        command = (
            "1..25000 | ForEach-Object { [Console]::Write('X' * 64) }; "
            "[Console]::WriteLine('JTT_STREAM_GAP_' + "
            f"'{nonce}' + '_DONE')"
        )
        assert marker not in command
        client.send((command + "\r").encode("ascii"))
        deadline = time.monotonic() + 30
        observed = None
        while time.monotonic() < deadline:
            observed = client.observe()
            if (observed.screen is not None
                    and marker in _screen_text(observed.screen)):
                break
            time.sleep(0.02)
        assert observed is not None and observed.screen is not None
        assert marker in _screen_text(observed.screen)
        with pytest.raises(TerminalError, match="stream_gap"):
            client.send(b"JTT_MUST_NOT_BE_DELIVERED\r")
        assert not client.lease
        current = client.observe()
        assert current.screen is not None
        assert "JTT_MUST_NOT_BE_DELIVERED" not in _screen_text(current.screen)
    finally:
        client.close()
        current = next((item for item in registry.list_sessions()
                        if item.session_id == identity.session_id), None)
        if current is not None:
            registry.stop(
                identity.session_id, expected_generation=identity.generation,
                expected_shell_pid=identity.shell_pid,
            )


def test_native_input_reply_sequence_does_not_ack_unread_output(tmp_path):
    import ju0_terminal_tower  # noqa: F401 - installs the guarded shared namespace
    from tmux_agent_tower.terminal import TerminalError, TerminalRegistry
    from tmux_agent_tower.terminal._windows_worker import OUTPUT_BUFFER_BYTES

    registry = TerminalRegistry(tmp_path / "isolated-jtt" / "terminals")
    client = registry.create("Input response is not output acknowledgement")
    identity = client.identity

    def wait_for_marker(marker, timeout=30):
        deadline = time.monotonic() + timeout
        previous_sequence = None
        stable_reads = 0
        last = None
        while time.monotonic() < deadline:
            last = client.observe()
            visible = last.screen is not None and marker in _screen_text(last.screen)
            if visible and last.sequence == previous_sequence:
                stable_reads += 1
                if stable_reads >= 2:
                    return last
            else:
                stable_reads = 0
            previous_sequence = last.sequence
            time.sleep(0.01)
        raise AssertionError(
            f"PowerShell marker not stable in observe snapshot: {marker}; "
            f"sequence={getattr(last, 'sequence', None)}"
        )

    try:
        assert client.acquire()
        output_cursor = client._output_cursor

        nonce_a = uuid.uuid4().hex[:8]
        marker_a = f"JTT_SEQ_A_{nonce_a}_END"
        count_a = (OUTPUT_BUFFER_BYTES - 32 * 1024) // 64
        command_a = (
            f"1..{count_a} | ForEach-Object {{ [Console]::Write('A' * 64) }}; "
            f"[Console]::WriteLine('JTT_SEQ_' + 'A_{nonce_a}_END')"
        )
        assert marker_a not in command_a
        client.send((command_a + "\r").encode("ascii"))
        observed_a = wait_for_marker(marker_a)
        assert observed_a.sequence > output_cursor
        assert client._output_cursor == output_cursor

        # This input response reports the latest output sequence, but returns
        # no output bytes. It must not claim that the preceding output was
        # consumed by this client.
        client.send(b"\r")
        assert client._output_cursor == output_cursor

        nonce_b = uuid.uuid4().hex[:8]
        marker_b = f"JTT_SEQ_B_{nonce_b}_END"
        count_b = (96 * 1024) // 64
        command_b = (
            f"1..{count_b} | ForEach-Object {{ [Console]::Write('B' * 64) }}; "
            f"[Console]::WriteLine('JTT_SEQ_' + 'B_{nonce_b}_END')"
        )
        assert marker_b not in command_b
        client.send((command_b + "\r").encode("ascii"))
        observed_b = wait_for_marker(marker_b)
        assert observed_b.sequence > observed_a.sequence

        # A + B exceeds the worker's bounded output buffer. Because no poll
        # delivered those bytes, the next input must see the old output cursor,
        # detect the gap, and revoke the lease instead of silently proceeding.
        with pytest.raises(TerminalError, match="stream_gap"):
            client.send(b"JTT_MUST_NOT_BE_DELIVERED\r")
        assert not client.lease
        current = client.observe()
        assert current.screen is not None
        assert "JTT_MUST_NOT_BE_DELIVERED" not in _screen_text(current.screen)
    finally:
        client.close()
        current = next((item for item in registry.list_sessions()
                        if item.session_id == identity.session_id), None)
        if current is not None:
            registry.stop(
                identity.session_id, expected_generation=identity.generation,
                expected_shell_pid=identity.shell_pid,
            )


@pytest.mark.parametrize("columns,lines", [(80, 24), (160, 44)])
def test_native_powershell_prompt_tracks_long_current_location(
        tmp_path, monkeypatch, columns, lines):
    import ju0_terminal_tower  # noqa: F401 - installs the guarded shared namespace
    from tmux_agent_tower.terminal import TerminalRegistry

    start = tmp_path / "isolated-start"
    destination = start / "dest"
    start.mkdir()
    destination.mkdir(parents=True)
    assert len(str(destination)) > 100
    monkeypatch.chdir(start)
    registry = TerminalRegistry(tmp_path / "isolated-jtt" / "terminals")
    client = registry.create("PowerShell current path prompt")
    identity = client.identity

    try:
        initial = _wait(
            client,
            lambda update: update.screen is not None
            and _expected_prompt_path(start, 100)
            in _compact_screen_text(update.screen),
        )
        assert _expected_prompt_path(start, 100) in _compact_screen_text(initial.screen)
        assert client.acquire()
        nonce = identity.session_id[:10]
        width_marker = f"JTT_WIDTH_{nonce}_{columns}_END"
        rawui_width_marker = f"JTT_RAWUI_{nonce}_{columns}_END"

        # A prompt is rendered immediately before PowerShell begins reading
        # the next line. Require one real command result as the input-ready
        # barrier instead of assuming the visible prompt is already readable.
        ready_marker = _generated_marker(client, "PATH_INPUT_READY")
        start_prompt = _expected_prompt_path(start, 100)
        _wait(
            client,
            lambda item: item.screen is not None
            and _screen_contains_after(item.screen, ready_marker, start_prompt),
        )

        path_marker = f"JTT_PATH_{nonce}_END"
        location_command = (
            f"Set-Location -LiteralPath '{destination.name}'; "
            "[Console]::WriteLine((Get-Location).Path); "
            f"[Console]::WriteLine('{path_marker}')"
        )
        assert str(destination) not in location_command
        client.send((location_command + "\r").encode("utf-8"))

        initial_width_prompt = _expected_prompt_path(destination, 100)
        path_bytes = str(destination).encode("utf-8")
        _path_update, path_output = _wait_for_raw_output(
            client,
            path_bytes,
            checkpoints=(
                ("location_command_echo", location_command.encode("utf-8")),
                ("location_marker", path_marker.encode("ascii")),
                ("location_error", b"Set-Location :"),
            ),
        )
        compact_path_output = path_output.replace(b"\r", b"").replace(b"\n", b"")
        assert path_bytes in compact_path_output
        assert path_marker.encode("ascii") in compact_path_output

        # Raw ConPTY output proves PowerShell changed directory and reported
        # the resolved path. Independently require pyte to render that result
        # followed by the prompt for the new location.
        update = _wait(
            client,
            lambda item: item.screen is not None
            and _screen_contains_after(item.screen, path_marker, initial_width_prompt),
        )
        assert initial_width_prompt in _compact_screen_text(update.screen)

        client.resize(columns, lines)
        resized = _wait(
            client,
            lambda item: item.screen is not None
            and (item.columns, item.lines) == (columns, lines),
        )
        assert (resized.columns, resized.lines) == (columns, lines)

        width_command = (
            "[Console]::WriteLine('JTT_'+'WIDTH_'+"
            f"'{nonce}'+'_'+"
            "[Console]::WindowWidth+'_END');"
            "[Console]::WriteLine('JTT_'+'RAWUI_'+"
            f"'{nonce}'+'_'+"
            "$Host.UI.RawUI.WindowSize.Width+'_END')"
        )
        assert width_marker not in width_command
        assert rawui_width_marker not in width_command
        client.send((width_command + "\r").encode("ascii"))
        _width_update, width_output = _wait_for_raw_output(
            client, rawui_width_marker.encode("ascii"),
        )
        compact_width_output = width_output.replace(b"\r", b"").replace(b"\n", b"")
        assert width_marker.encode("ascii") in compact_width_output
        assert rawui_width_marker.encode("ascii") in compact_width_output

        expected_prompt = _expected_prompt_path(destination, columns)
        def path_prompt_observed(item):
            if item.screen is None:
                return False
            compact = _compact_screen_text(item.screen)
            width_index = compact.rfind(width_marker)
            prompt_index = compact.find(expected_prompt, width_index + len(width_marker))
            # The full destination was already asserted from actual command
            # output before resizing. A resize may reflow/evict those older
            # cells, so only require the post-resize width and prompt here.
            return width_index >= 0 and prompt_index >= 0

        update = _wait(client, path_prompt_observed)
        display = _compact_screen_text(update.screen)
        assert (update.columns, update.lines) == (columns, lines)

        assert expected_prompt in display
    finally:
        client.close()
        current = next((item for item in registry.list_sessions()
                        if item.session_id == identity.session_id), None)
        if current is not None:
            registry.stop(
                identity.session_id, expected_generation=identity.generation,
                expected_shell_pid=identity.shell_pid,
            )
