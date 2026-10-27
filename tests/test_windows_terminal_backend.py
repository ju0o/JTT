"""Native Windows integration tests using only isolated JTT-owned sessions."""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
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
        f"snapshot_sequence={getattr(last, 'snapshot_sequence', None)!r}; "
        f"synced={getattr(last, 'synced', None)!r}; "
        f"gap={getattr(last, 'gap', None)!r}; "
        f"more={getattr(last, 'more', None)!r}; "
        f"collected_bytes={len(output)}; "
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

        # A screen snapshot can lead the client's output cursor. Use bytes that
        # are absent from the command as the readiness barrier, then drain
        # through the prompt before sending another line.
        ready_nonce = uuid.uuid4().hex[:10]
        ready_marker = f"JTT_PATH_INPUT_READY_{ready_nonce}_END"
        ready_command = (
            "[Console]::WriteLine('JTT_PATH_INPUT_READY_' + '"
            f"{ready_nonce}" + "' + '_END')"
        )
        assert ready_marker not in ready_command
        client.send((ready_command + "\r").encode("ascii"))
        _ready_output_update, ready_output = _wait_for_raw_output(
            client, ready_marker.encode("ascii"),
            checkpoints=(("ready_command_echo", ready_command.encode("ascii")),),
        )
        start_prompt = _expected_prompt_path(start, 100)
        start_prompt_bytes = start_prompt.encode("utf-8")
        ready_compact = ready_output.replace(b"\r", b"").replace(b"\n", b"")
        if start_prompt_bytes not in ready_compact:
            _prompt_update, _prompt_output = _wait_for_raw_output(
                client, start_prompt_bytes,
                checkpoints=(("ready_marker_output", ready_marker.encode("ascii")),),
            )
        _wait(
            client,
            lambda item: item.screen is not None
            and item.synced and not item.more
            and item.sequence == item.snapshot_sequence
            and _screen_contains_after(item.screen, ready_marker, start_prompt),
        )

        # Keep the submitted line short enough not to wrap from the prompt into
        # a second input row. The resolved path is not in the command, proving
        # PowerShell executed the location change rather than echoing input.
        location_command = f"cd '{destination.name}'; $PWD.Path"
        assert str(destination) not in location_command
        assert len(start_prompt) + 2 + len(location_command) < 100
        # Match the native UI's per-key RPC path, including a separate Enter
        # acknowledgement, instead of bulk-pasting a whole command line. An
        # input RPC only acknowledges delivery to ConPTY; wait for the full
        # echoed line before Enter so the test does not race PowerShell's line
        # editor on fast hosted runners.
        for character in location_command:
            assert client.send(character.encode("utf-8")) == 1
        typed_line = _wait(
            client,
            lambda item: item.screen is not None
            and _screen_contains_after(item.screen, ready_marker, location_command),
        )
        assert _screen_contains_after(typed_line.screen, ready_marker, location_command)
        # Use the Windows console line terminator after the per-key writes.
        assert client.send(b"\r\n") == 2

        initial_width_prompt = _expected_prompt_path(destination, 100)
        prompt_bytes = initial_width_prompt.encode("utf-8")
        path_bytes = str(destination).encode("utf-8")
        _path_update, path_output = _wait_for_raw_output(
            client,
            path_bytes,
            checkpoints=(
                ("location_command_echo", location_command.encode("utf-8")),
                ("location_error", b"Set-Location :"),
            ),
            screen_checkpoints=(
                ("location_command_echo", location_command),
                ("resolved_location", str(destination)),
                ("resolved_prompt", initial_width_prompt),
            ),
        )
        compact_path_output = path_output.replace(b"\r", b"").replace(b"\n", b"")
        assert path_bytes in compact_path_output

        # The resolved path and prompt may be delivered in one ConPTY read.
        # Reuse bytes already observed before polling for a later chunk.
        prompt_output = bytearray(path_output)
        if prompt_bytes not in compact_path_output:
            _prompt_update, prompt_more = _wait_for_raw_output(
                client,
                prompt_bytes,
                checkpoints=(("location_error", b"Set-Location :"),),
            )
            prompt_output.extend(prompt_more)
        compact_prompt_output = bytes(prompt_output).replace(b"\r", b"").replace(b"\n", b"")
        assert prompt_bytes in compact_prompt_output

        # The ConPTY stream proves the new prompt was emitted. Check its
        # screen-cell rendering separately so output and pyte state cannot be
        # confused when diagnosing a failure.
        resolved_path = str(destination)

        def resolved_location_rendered(item):
            if item.screen is None:
                return False
            compact = _compact_screen_text(item.screen)
            command_index = compact.rfind(location_command)
            path_index = compact.find(resolved_path, command_index + len(location_command))
            prompt_index = compact.find(initial_width_prompt, path_index + len(resolved_path))
            return command_index >= 0 and path_index >= 0 and prompt_index >= 0

        update = _wait(client, resolved_location_rendered)
        assert (update.columns, update.lines) == (100, 30)
        rendered_location = _compact_screen_text(update.screen)
        assert resolved_location_rendered(update)
        # This verifies the full line-wrapped result made it through pyte cells,
        # separately from the raw ConPTY assertion above.
        assert rendered_location.find(resolved_path) >= 0

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


def _r02_worker_copy(tmp_path, monkeypatch, active_diagnostics):
    """Instrument a temporary copy of the worker without changing runtime code."""
    from tmux_agent_tower.terminal import windows_manager

    source_root = Path(windows_manager.__file__).resolve().parents[2]
    diagnostic_root = tmp_path / "r02-worker-source" / "src"
    diagnostic_root.mkdir(parents=True)
    source_package = next(
        (source_root / package for package in (
            "tmux_agent_tower", "ju0_terminal_tower",
        ) if (source_root / package).is_dir()),
        None,
    )
    if source_package is None:
        raise AssertionError("JTT terminal package source is unavailable for instrumentation")
    shutil.copytree(
        source_package,
        diagnostic_root / "tmux_agent_tower",
        ignore=shutil.ignore_patterns("__pycache__"),
    )

    worker = diagnostic_root / "tmux_agent_tower" / "terminal" / "_windows_worker.py"
    source = worker.read_text(encoding="utf-8")

    write_anchor = "                        terminal.write(text)\n"
    write_instrumentation = '''                        _r02_write_result = None
                        _r02_write_error = None
                        try:
                            _r02_write_result = terminal.write(text)
                        except Exception as _r02_exc:
                            _r02_write_error = type(_r02_exc).__name__
                            raise
                        finally:
                            try:
                                _r02_log = os.environ.get("JTT_R02_WRITE_LOG")
                                if _r02_log:
                                    _r02_record = {
                                        "at_monotonic": time.monotonic(),
                                        "worker_pid": os.getpid(),
                                        "input_bytes": len(data),
                                        "input_hex": data.hex(),
                                        "write_result_type": type(
                                            _r02_write_result
                                        ).__name__,
                                        "write_result": repr(_r02_write_result),
                                        "write_error": _r02_write_error,
                                    }
                                    with open(_r02_log, "a", encoding="utf-8") as _r02_stream:
                                        _r02_stream.write(
                                            json.dumps(_r02_record, sort_keys=True) + "\\n"
                                        )
                            except Exception:
                                pass
'''
    if source.count(write_anchor) != 1:
        raise AssertionError("worker PTY.write instrumentation anchor changed")
    source = source.replace(write_anchor, write_instrumentation, 1)

    read_state_anchor = "        output = deque()\n"
    read_state = (
        read_state_anchor
        + '        _r02_read_trace = {"chunks": [], "bytes": 0, "truncated": False}\n'
    )
    if source.count(read_state_anchor) != 1:
        raise AssertionError("worker output trace instrumentation anchor changed")
    source = source.replace(read_state_anchor, read_state, 1)

    read_anchor = "                        if data:\n                            encoded = data.encode(\"utf-8\", errors=\"strict\")\n"
    read_instrumentation = '''                        if data:
                            _r02_encoded = data.encode("utf-8", errors="strict")
                            if (len(_r02_read_trace["chunks"]) < 512
                                    and _r02_read_trace["bytes"] + len(_r02_encoded)
                                    <= 131072):
                                _r02_read_trace["chunks"].append({
                                    "time": time.monotonic(), "text": data,
                                })
                                _r02_read_trace["bytes"] += len(_r02_encoded)
                            else:
                                _r02_read_trace["truncated"] = True
                            encoded = _r02_encoded
'''
    if source.count(read_anchor) != 1:
        raise AssertionError("worker PTY.read instrumentation anchor changed")
    source = source.replace(read_anchor, read_instrumentation, 1)

    environment_anchor = '        shell_env["TERM"] = "xterm"\n'
    environment_instrumentation = environment_anchor + '''        for _r02_key in (
            "JTT_R02_META_FILE", "JTT_R02_N", "JTT_R02_MARKER",
        ):
            if _r02_key in os.environ:
                shell_env[_r02_key] = os.environ[_r02_key]
'''
    if source.count(environment_anchor) != 1:
        raise AssertionError("worker environment instrumentation anchor changed")
    source = source.replace(environment_anchor, environment_instrumentation, 1)

    bootstrap_anchor = (
        '        "[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new($false); "\n'
        '        "$OutputEncoding=[Console]::OutputEncoding; "\n'
    )
    bootstrap_instrumentation = bootstrap_anchor + '''        "if ($env:JTT_R02_META_FILE) { try { "
        "$r02=@{ pid=$PID; version=$PSVersionTable.PSVersion.ToString(); "
        "edition=$PSVersionTable.PSEdition; "
        "cwd=$PWD.Path; "
        "input_encoding=[Console]::InputEncoding.WebName; "
        "input_codepage=[Console]::InputEncoding.CodePage; "
        "output_encoding=[Console]::OutputEncoding.WebName; "
        "output_codepage=[Console]::OutputEncoding.CodePage; "
        "columns=$Host.UI.RawUI.WindowSize.Width; "
        "lines=$Host.UI.RawUI.WindowSize.Height }; "
        "[System.IO.File]::WriteAllText($env:JTT_R02_META_FILE, "
        "($r02 | ConvertTo-Json -Compress)) } catch {} }; "
'''
    if source.count(bootstrap_anchor) != 1:
        raise AssertionError("PowerShell bootstrap instrumentation anchor changed")
    source = source.replace(bootstrap_anchor, bootstrap_instrumentation, 1)

    flush_anchor = "            reader.join(timeout=1)\n"
    flush_instrumentation = flush_anchor + '''            try:
                _r02_read_log = os.environ.get("JTT_R02_READ_LOG")
                if _r02_read_log:
                    Path(_r02_read_log).write_text(
                        json.dumps(_r02_read_trace, sort_keys=True), encoding="utf-8"
                    )
            except Exception:
                pass
'''
    if source.count(flush_anchor) != 1:
        raise AssertionError("worker read trace flush anchor changed")
    source = source.replace(flush_anchor, flush_instrumentation, 1)
    worker.write_text(source, encoding="utf-8")

    original_environment = windows_manager._worker_environment

    def diagnostic_environment(session_dir):
        env = original_environment(session_dir)
        env.update({key: str(value) for key, value in active_diagnostics.items()})
        return env

    monkeypatch.setattr(windows_manager, "WORKER", worker)
    monkeypatch.setattr(windows_manager, "_worker_environment", diagnostic_environment)
    return windows_manager


def _r02_safe_diagnostic(value, roots):
    """Redact isolated temp roots recursively before a diagnostic is printed."""
    if isinstance(value, dict):
        return {
            str(key): _r02_safe_diagnostic(item, roots)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_r02_safe_diagnostic(item, roots) for item in value]
    if isinstance(value, str):
        return _r02_redact(value, roots)
    return value


def _r02_process_state(manager, identity):
    created, image, alive = manager._process_facts(
        identity.shell_pid, expected_start=identity.shell_start,
    )
    return {
        "pid": identity.shell_pid,
        "expected_start_matches": created == identity.shell_start,
        "alive": bool(alive and created == identity.shell_start),
        "image_matches": image.casefold() == identity.shell_path.casefold(),
    }


def _r02_attach_client_trace(client, manager, roots):
    """Trace one test-owned client's input acknowledgements and poll results."""
    trace = {
        "send_calls": [],
        "line_submissions": [],
        "polls": [],
        "command_echo_seen": False,
        "powershell_error_observations": [],
        "last_screen": "",
        "last_poll": None,
    }
    original_send = client.send
    original_poll = client.poll

    def traced_send(payload):
        record = {
            "at": time.monotonic(),
            "bytes": len(payload),
            "payload_hex": payload.hex(),
        }
        is_terminator = payload in (b"\r", b"\n", b"\r\n")
        is_line_submission = is_terminator or (
            len(payload) > 1 and payload.endswith((b"\r", b"\n"))
        )
        if is_line_submission:
            record["phase"] = (
                "preparation_command"
                if b"JTT_PATH_INPUT_READY_" in payload
                else "followup_command"
            )
        elif len(payload) == 1:
            record["phase"] = "character_input"
        else:
            record["phase"] = "bulk_input"
        identity = client.identity
        if is_line_submission:
            record["process_before"] = _r02_process_state(manager, identity)
        try:
            acknowledged = original_send(payload)
            record["acknowledged_bytes"] = acknowledged
            return acknowledged
        except Exception as exc:
            record["send_error"] = type(exc).__name__
            raise
        finally:
            record["elapsed_seconds"] = round(time.monotonic() - record["at"], 6)
            if is_line_submission:
                record["process_after"] = _r02_process_state(manager, identity)
                trace["line_submissions"].append(dict(record))
            trace["send_calls"].append(dict(record))

    def traced_poll():
        update = original_poll()
        received_at = time.monotonic()
        screen_text = (
            _compact_screen_text(update.screen)
            if update.screen is not None else ""
        )
        if "cd 'dest'; $PWD.Path" in screen_text:
            trace["command_echo_seen"] = True
        output_text = update.output.decode("utf-8", errors="backslashreplace")
        error_tokens = (
            "ParserError", "Set-Location :", "Set-Content :",
            "Cannot find path", "Exception",
        )
        if any(token in screen_text or token in output_text for token in error_tokens):
            trace["powershell_error_observations"].append({
                "sequence": update.sequence,
                "output": _r02_redact(output_text, roots),
                "screen": _r02_redact(screen_text[-1200:], roots),
            })
        trace["last_screen"] = screen_text
        trace["last_poll"] = _r02_poll_record(update, received_at)
        if (update.output or not trace["polls"]
                or trace["polls"][-1]["sequence"] != update.sequence
                or trace["polls"][-1]["snapshot_sequence"]
                != update.snapshot_sequence):
            record = _r02_poll_record(update, received_at)
            record.update({
                "output_repr_redacted": _r02_redact(output_text, roots),
                "screen_repr_redacted": _r02_redact(screen_text[-1600:], roots),
                "command_echo_seen": trace["command_echo_seen"],
            })
            trace["polls"].append(record)
        return update

    client.send = traced_send
    client.poll = traced_poll
    return trace


@pytest.fixture(autouse=True)
def r02_trace_original_long_location_failure(request, tmp_path, monkeypatch):
    """Add observation only; keep the original failing test and assertions intact."""
    if request.node.originalname != "test_native_powershell_prompt_tracks_long_current_location":
        yield
        return

    import ju0_terminal_tower  # noqa: F401 - installs the guarded shared namespace
    from tmux_agent_tower.terminal import windows_manager

    diagnostic_dir = tmp_path / "r02-original-e2e"
    diagnostic_dir.mkdir()
    paths = {
        "JTT_R02_META_FILE": diagnostic_dir / "powershell-meta.json",
        "JTT_R02_N": uuid.uuid4().hex,
        "JTT_R02_WRITE_LOG": diagnostic_dir / "pty-write.jsonl",
        "JTT_R02_READ_LOG": diagnostic_dir / "pty-read.json",
    }
    active = {key: str(value) for key, value in paths.items()}
    manager = _r02_worker_copy(tmp_path, monkeypatch, active)
    originals = manager.TerminalRegistry.create
    captured = []
    roots = (tmp_path, os.environ.get("USERPROFILE", ""))

    def traced_create(registry, *args, **kwargs):
        client = originals(registry, *args, **kwargs)
        captured.append({
            "client": client,
            "registry": registry,
            "identity": client.identity,
            "trace": _r02_attach_client_trace(client, manager, roots),
        })
        return client

    monkeypatch.setattr(manager.TerminalRegistry, "create", traced_create)
    try:
        yield
    finally:
        for item in captured:
            client = item["client"]
            identity = item["identity"]
            try:
                item["trace"]["process_at_fixture_teardown"] = (
                    _r02_process_state(manager, identity)
                )
            except Exception as exc:
                item["trace"]["process_at_fixture_teardown"] = (
                    f"unavailable:{type(exc).__name__}"
                )
            item["trace"]["identity"] = {
                "session_id_present": bool(identity.session_id),
                "generation_present": bool(identity.generation),
                "shell_pid": identity.shell_pid,
                "shell_start_present": bool(identity.shell_start),
                "shell_path": identity.shell_path,
            }
            item["trace"]["runtime_metadata"] = "not_recorded"
            metadata_path = Path(paths["JTT_R02_META_FILE"])
            if metadata_path.is_file():
                try:
                    item["trace"]["runtime_metadata"] = json.loads(
                        metadata_path.read_text(encoding="utf-8")
                    )
                except (OSError, ValueError, UnicodeError):
                    item["trace"]["runtime_metadata"] = "invalid_or_unavailable"
            write_path = Path(paths["JTT_R02_WRITE_LOG"])
            if write_path.is_file():
                try:
                    write_records = [
                        json.loads(line) for line in
                        write_path.read_text(encoding="utf-8").splitlines() if line
                    ]
                    item["trace"]["worker_writes"] = write_records
                except (OSError, ValueError, UnicodeError):
                    item["trace"]["worker_writes"] = "invalid_or_unavailable"
            read_path = Path(paths["JTT_R02_READ_LOG"])
            if read_path.is_file():
                try:
                    read_trace = json.loads(read_path.read_text(encoding="utf-8"))
                    item["trace"]["worker_read_api"] = (
                        "pywinpty.PTY.read returned str; utf8 re-encoding only"
                    )
                    item["trace"]["worker_read_trace"] = read_trace
                except (OSError, ValueError, UnicodeError):
                    item["trace"]["worker_read_trace"] = "invalid_or_unavailable"
            safe = _r02_safe_diagnostic(item["trace"], roots)
            print("R02_ORIGINAL_E2E_TRACE " + json.dumps(safe, sort_keys=True))


def _r02_redact(value, roots):
    text = str(value)
    for root in sorted((str(item) for item in roots), key=len, reverse=True):
        if root:
            text = text.replace(root, "<TEMP>")
            text = text.replace(root.replace("\\", "/"), "<TEMP>")
            text = text.replace(root.replace("/", "\\"), "<TEMP>")
    # Long-path prompt shortening intentionally removes the middle of the
    # temporary path, so exact-root replacement cannot match that rendering.
    text = re.sub(r"(?i)(PS\s+)[A-Z]:\\\.{3}\\[^>\r\n]*>", r"\1<TEMP>>", text)
    return text


def _r02_poll_record(update, received_at):
    return {
        "time": received_at,
        "sequence": update.sequence,
        "snapshot_sequence": update.snapshot_sequence,
        "state": update.state,
        "renderer": update.renderer_state,
        "columns": update.columns,
        "lines": update.lines,
        "synced": update.synced,
        "gap": update.gap,
        "more": update.more,
        "output_bytes": len(update.output),
        "output_sha256": hashlib.sha256(update.output).hexdigest(),
    }


def _r02_wait_for(client, predicate, timeout=15):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = client.poll()
        if predicate(last):
            return last
        time.sleep(0.01)
    return last


def test_r02_long_location_command_terminator_replicas(tmp_path, monkeypatch):
    """Replay the prepared long-path command with CR and CRLF; report, do not mask."""
    import ju0_terminal_tower  # noqa: F401 - installs the guarded shared namespace
    from tmux_agent_tower.terminal import TerminalRegistry

    active_diagnostics = {}
    manager = _r02_worker_copy(tmp_path, monkeypatch, active_diagnostics)
    results = []

    for terminator_name, terminator in (("CR", b"\r"), ("CRLF", b"\r\n")):
        case_root = tmp_path / f"r02-replica-{terminator_name.lower()}"
        start = case_root / "isolated-start"
        destination = start / "dest"
        diagnostic_dir = case_root / "diagnostics"
        diagnostic_dir.mkdir(parents=True)
        nonce = uuid.uuid4().hex
        metadata_path = diagnostic_dir / "powershell-meta.json"
        write_log = diagnostic_dir / "pty-write.jsonl"
        read_log = diagnostic_dir / "pty-read.json"
        marker_path = diagnostic_dir / "command-ran.txt"
        active_diagnostics.clear()
        active_diagnostics.update({
            "JTT_R02_META_FILE": str(metadata_path),
            "JTT_R02_N": nonce,
            "JTT_R02_MARKER": str(marker_path),
            "JTT_R02_WRITE_LOG": str(write_log),
            "JTT_R02_READ_LOG": str(read_log),
        })
        result = {
            "terminator": terminator_name,
            "execution": "UNKNOWN",
            "marker": "ABSENT",
            "screen_result_path": "NO",
            "new_prompt": "NO",
            "runtime_metadata": "not_recorded",
            "cleanup": "NOT_ATTEMPTED",
        }
        client = None
        registry = None
        identity = None
        trace = None
        try:
            destination.mkdir(parents=True)
            result["destination_length"] = len(str(destination))
            if len(str(destination)) <= 100:
                raise RuntimeError("replica_destination_is_not_long_enough")
            monkeypatch.chdir(start)
            registry = TerminalRegistry(
                case_root / "isolated-jtt-home" / "terminals"
            )
            client = registry.create("R02 long-path terminator replica")
            identity = client.identity
            trace = _r02_attach_client_trace(
                client, manager,
                (tmp_path, os.environ.get("USERPROFILE", "")),
            )
            result["identity"] = {
                "session_id_present": bool(identity.session_id),
                "generation_present": bool(identity.generation),
                "shell_pid": identity.shell_pid,
                "shell_start_present": bool(identity.shell_start),
                "process_before_input": _r02_process_state(manager, identity),
            }
            result["initial_columns_lines"] = "not_observed"
            initial_prompt = _expected_prompt_path(start, 100)
            initial = _r02_wait_for(
                client,
                lambda item: item is not None and item.screen is not None
                and item.synced and item.state == "running"
                and initial_prompt in _compact_screen_text(item.screen),
            )
            result["initial_prompt_seen"] = bool(
                initial is not None and initial.screen is not None
                and initial_prompt in _compact_screen_text(initial.screen)
            )
            if initial is not None:
                result["initial_columns_lines"] = [initial.columns, initial.lines]
            if not client.acquire():
                raise RuntimeError("input_lease_not_acquired")

            ready_nonce = uuid.uuid4().hex[:10]
            ready_marker = f"JTT_PATH_INPUT_READY_{ready_nonce}_END"
            ready_command = (
                "[Console]::WriteLine('JTT_PATH_INPUT_READY_' + '"
                f"{ready_nonce}" + "' + '_END')"
            )
            assert ready_marker not in ready_command
            client.send((ready_command + "\r").encode("ascii"))
            _ready_update, ready_output = _wait_for_raw_output(
                client, ready_marker.encode("ascii"),
                checkpoints=(("ready_command_echo", ready_command.encode("ascii")),),
            )
            start_prompt_bytes = initial_prompt.encode("utf-8")
            compact_ready = ready_output.replace(b"\r", b"").replace(b"\n", b"")
            if start_prompt_bytes not in compact_ready:
                _wait_for_raw_output(
                    client, start_prompt_bytes,
                    checkpoints=(("ready_marker_output", ready_marker.encode("ascii")),),
                )
            _r02_wait_for(
                client,
                lambda item: item is not None and item.screen is not None
                and item.synced and not item.more
                and item.sequence == item.snapshot_sequence
                and _screen_contains_after(item.screen, ready_marker, initial_prompt),
            )

            location_command = f"cd '{destination.name}'; $PWD.Path"
            assert str(destination) not in location_command
            marker_command = (
                location_command
                + ";[System.IO.File]::WriteAllText($env:JTT_R02_MARKER,$env:JTT_R02_N)"
            )
            assert str(destination) not in marker_command
            assert nonce not in marker_command
            result["command_under_test"] = location_command
            result["marker_command_is_separate_replica"] = True
            for character in marker_command:
                client.send(character.encode("ascii"))
            echo_deadline = time.monotonic() + 15
            while time.monotonic() < echo_deadline:
                item = client.poll()
                if item.screen is not None and marker_command in _compact_screen_text(item.screen):
                    result["marker_command_echo_before_submit"] = "YES"
                    break
            else:
                result["marker_command_echo_before_submit"] = "NO"

            submit_at = time.monotonic()
            result["submitted_at_monotonic"] = submit_at
            result["terminator_hex"] = terminator.hex()
            result["terminator_ack"] = client.send(terminator)
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                item = client.poll()
                screen = _compact_screen_text(item.screen) if item.screen else ""
                command_index = screen.rfind(marker_command)
                path_index = (
                    screen.find(str(destination), command_index + len(marker_command))
                    if command_index >= 0 else -1
                )
                prompt = _expected_prompt_path(destination, 100)
                prompt_index = screen.find(prompt, path_index + len(str(destination))) if path_index >= 0 else -1
                if path_index >= 0:
                    result["screen_result_path"] = "YES"
                if path_index >= 0 and prompt_index > path_index:
                    result["new_prompt"] = "YES"
                if marker_path.is_file():
                    try:
                        result["marker"] = (
                            "PRESENT_MATCH"
                            if marker_path.read_text(encoding="utf-8").strip() == nonce
                            else "PRESENT_MISMATCH"
                        )
                    except (OSError, UnicodeError):
                        result["marker"] = "PRESENT_UNREADABLE"
                    result["execution"] = (
                        "YES" if result["marker"] == "PRESENT_MATCH" else "NO"
                    )
                if result["marker"] != "ABSENT" and result["new_prompt"] == "YES":
                    break
                time.sleep(0.01)
            result["post_submit_process"] = _r02_process_state(manager, identity)
            post_submit_events = [
                event for event in trace["polls"]
                if event["time"] >= submit_at
            ]
            result["post_submit_output_bytes"] = sum(
                event["output_bytes"] for event in post_submit_events
            )
            result["post_submit_output_repr"] = "".join(
                event["output_repr_redacted"] for event in post_submit_events
            )
            result["final_sequence"] = trace["last_poll"]
            result["powershell_error_observations"] = (
                trace["powershell_error_observations"]
            )
            result["screen_repr"] = _r02_redact(
                trace["last_screen"],
                (tmp_path, os.environ.get("USERPROFILE", "")),
            )
            result["client_trace"] = trace
        except Exception as exc:
            result["error"] = type(exc).__name__
            if trace is not None:
                result["final_sequence"] = trace.get("last_poll")
                result["client_trace"] = trace
            if marker_path.is_file():
                try:
                    result["marker"] = (
                        "PRESENT_MATCH"
                        if marker_path.read_text(encoding="utf-8").strip() == nonce
                        else "PRESENT_MISMATCH"
                    )
                    result["execution"] = (
                        "YES" if result["marker"] == "PRESENT_MATCH" else "NO"
                    )
                except (OSError, UnicodeError):
                    result["marker"] = "PRESENT_UNREADABLE"
        finally:
            if client is not None:
                try:
                    client.close()
                except Exception as exc:
                    result["client_close_error"] = type(exc).__name__
            if registry is not None and identity is not None:
                try:
                    registry.stop(
                        identity.session_id,
                        expected_generation=identity.generation,
                        expected_shell_pid=identity.shell_pid,
                    )
                    result["cleanup"] = (
                        "STOPPED" if not _process_same(
                            identity.shell_pid, identity.shell_start
                        ) else "PROCESS_STILL_MATCHES_IDENTITY"
                    )
                except Exception as exc:
                    result["cleanup"] = f"STOP_ERROR_{type(exc).__name__}"
            if metadata_path.is_file():
                try:
                    result["runtime_metadata"] = json.loads(
                        metadata_path.read_text(encoding="utf-8")
                    )
                except (OSError, ValueError, UnicodeError):
                    result["runtime_metadata"] = "invalid_or_unavailable"
            if write_log.is_file():
                try:
                    result["worker_write_records"] = [
                        json.loads(line)
                        for line in write_log.read_text(encoding="utf-8").splitlines()
                        if line
                    ]
                except (OSError, ValueError, UnicodeError):
                    result["worker_write_records"] = "invalid_or_unavailable"
            if read_log.is_file():
                try:
                    result["worker_read_api"] = (
                        "pywinpty.PTY.read str; UTF-8 re-encoding, not native bytes"
                    )
                    result["worker_read_trace"] = json.loads(
                        read_log.read_text(encoding="utf-8")
                    )
                except (OSError, ValueError, UnicodeError):
                    result["worker_read_trace"] = "invalid_or_unavailable"
            safe = _r02_safe_diagnostic(
                result, (tmp_path, os.environ.get("USERPROFILE", ""))
            )
            print("R02_LONG_PATH_REPLICA " + json.dumps(safe, sort_keys=True))
        results.append(result)

    assert [item["terminator"] for item in results] == ["CR", "CRLF"]
    assert all(item["cleanup"] == "STOPPED" for item in results), results


def test_r02_powershell_input_and_terminator_diagnostic_matrix(tmp_path, monkeypatch):
    """Collect six bounded input/terminator observations; do not relax regressions."""
    import ju0_terminal_tower  # noqa: F401 - installs the guarded shared namespace
    from tmux_agent_tower.terminal import TerminalRegistry

    cases = tuple(
        (delivery, terminator_name, terminator)
        for delivery in ("character", "bulk")
        for terminator_name, terminator in (
            ("CR", b"\r"), ("LF", b"\n"), ("CRLF", b"\r\n"),
        )
    )
    active_diagnostics = {}
    _r02_worker_copy(tmp_path, monkeypatch, active_diagnostics)
    summaries = []

    for delivery, terminator_name, terminator in cases:
        case_name = f"{delivery.lower()}-{terminator_name.lower()}"
        case_root = tmp_path / case_name
        start_name = "s" * max(1, 108 - len(str(case_root)))
        start = case_root / start_name
        destination = start / "d"
        nonce = uuid.uuid4().hex.upper()
        marker_path = destination / "m"
        metadata_path = case_root / "child-meta.json"
        write_log = case_root / "pty-writes.jsonl"
        read_log = case_root / "pty-reads.json"
        active_diagnostics.clear()
        active_diagnostics.update({
            "JTT_R02_META_FILE": metadata_path,
            "JTT_R02_N": nonce,
            "JTT_R02_WRITE_LOG": write_log,
            "JTT_R02_READ_LOG": read_log,
        })

        registry = None
        client = None
        identity = None
        captured_output = bytearray()
        poll_events = []
        latest = None
        input_records = []
        submit_time = None
        post_enter_output_start = None
        command = (
            "Set-Location d;Set-Content -LiteralPath m -Value "
            "$env:JTT_R02_N -Encoding Ascii;$PWD.Path"
        )
        expected_prompt = _expected_prompt_path(destination, 100)
        command_echo = False
        result = {
            "case": case_name,
            "delivery": delivery,
            "terminator": terminator_name,
            "terminator_hex": terminator.hex(),
            "command": command,
            "command_bytes": len(command.encode("ascii")),
            "command_sha256": hashlib.sha256(command.encode("ascii")).hexdigest(),
            "initial_columns_lines": None,
            "child": None,
            "input_records": input_records,
            "command_echo": False,
            "command_execution": "UNKNOWN",
            "marker_file": "UNKNOWN",
            "marker_file_content": "UNKNOWN",
            "poll_events": poll_events,
            "poll_output_bytes": 0,
            "poll_output_sha256": None,
            "poll_output_repr": None,
            "poll_output_hex_redacted": None,
            "post_enter_output_bytes": None,
            "post_enter_output_repr": None,
            "post_enter_output_hex_redacted": None,
            "result_path_in_poll_output": "UNKNOWN",
            "result_path_in_screen": "UNKNOWN",
            "pty_read_text_repr": None,
            "pty_read_utf8_hex_redacted": None,
            "post_enter_pty_read_text_repr": None,
            "post_enter_pty_read_utf8_hex_redacted": None,
            "post_enter_elapsed_seconds": None,
            "pty_read_api_note": (
                "pywinpty.PTY.read returns Python text; UTF-8 hex is the encoded "
                "text returned by that API, not native ConPTY bytes"
            ),
            "poll_output_api_note": (
                "worker encodes pywinpty.PTY.read text as UTF-8 before poll"
            ),
            "pty_write_records": None,
            "new_prompt": "UNKNOWN",
            "final_error": None,
            "powershell_error": None,
            "final_sequence": None,
            "final_snapshot_sequence": None,
            "final_synced": None,
            "final_gap": None,
            "final_more": None,
            "screen_repr": None,
            "cleanup": "NOT_ATTEMPTED",
        }

        try:
            destination.mkdir(parents=True)
            if len(str(destination)) <= 100:
                raise RuntimeError("diagnostic_destination_is_not_longer_than_100")
            monkeypatch.chdir(start)
            registry = TerminalRegistry(
                case_root / "isolated-jtt-home" / "terminals"
            )
            client = registry.create("WBS-R02 input diagnostic")
            identity = client.identity
            original_poll = client.poll

            def capture_poll():
                nonlocal latest
                received_at = time.monotonic()
                update = original_poll()
                latest = update
                if update.output:
                    captured_output.extend(update.output)
                if (update.output or update.gap or update.more
                        or not poll_events
                        or poll_events[-1]["sequence"] != update.sequence
                        or poll_events[-1]["snapshot_sequence"]
                        != update.snapshot_sequence):
                    poll_events.append(_r02_poll_record(update, received_at))
                return update

            client.poll = capture_poll
            initial = _r02_wait_for(
                client,
                lambda item: item is not None and item.screen is not None
                and item.synced and item.state == "running",
                timeout=15,
            )
            if initial is None or initial.screen is None:
                result["final_error"] = "initial_screen_not_observed"
                raise RuntimeError("initial_screen_not_observed")
            result["initial_columns_lines"] = [initial.columns, initial.lines]
            result["child"] = {
                "executable": _r02_redact(
                    identity.shell_path,
                    (tmp_path, start, destination, os.environ.get("USERPROFILE", "")),
                ),
                "pid": identity.shell_pid,
                "generation_present": bool(identity.generation),
            }

            metadata_deadline = time.monotonic() + 12
            while time.monotonic() < metadata_deadline and not metadata_path.is_file():
                client.poll()
                time.sleep(0.01)
            if metadata_path.is_file():
                try:
                    result["child"]["runtime"] = json.loads(
                        metadata_path.read_text(encoding="utf-8")
                    )
                except (OSError, ValueError, UnicodeError):
                    result["child"]["runtime"] = "invalid_or_unavailable"
            else:
                result["child"]["runtime"] = "not_recorded"

            if not client.acquire():
                result["final_error"] = "input_lease_not_acquired"
                raise RuntimeError("input_lease_not_acquired")

            command_bytes = command.encode("ascii")
            if delivery == "character":
                for index, byte in enumerate(command_bytes):
                    payload = bytes((byte,))
                    try:
                        written = client.send(payload)
                        input_records.append({
                            "phase": "command_char", "index": index,
                            "bytes": len(payload), "payload_hex": payload.hex(),
                            "acknowledged_bytes": written,
                        })
                    except Exception as exc:
                        input_records.append({
                            "phase": "command_char", "index": index,
                            "bytes": len(payload), "payload_hex": payload.hex(),
                            "send_error": type(exc).__name__,
                        })
                        raise
                echo_update = _r02_wait_for(
                    client,
                    lambda item: item is not None and item.screen is not None
                    and command in _compact_screen_text(item.screen),
                    timeout=15,
                )
                command_echo = bool(
                    echo_update is not None and echo_update.screen is not None
                    and command in _compact_screen_text(echo_update.screen)
                )
                send_time = time.monotonic()
                submit_time = send_time
                post_enter_output_start = len(captured_output)
                try:
                    acknowledged = client.send(terminator)
                    input_records.append({
                        "phase": "terminator", "bytes": len(terminator),
                        "payload_hex": terminator.hex(),
                        "acknowledged_bytes": acknowledged,
                        "sent_at": send_time,
                    })
                except Exception as exc:
                    input_records.append({
                        "phase": "terminator", "bytes": len(terminator),
                        "payload_hex": terminator.hex(),
                        "send_error": type(exc).__name__, "sent_at": send_time,
                    })
                    raise
            else:
                payload = command_bytes + terminator
                send_time = time.monotonic()
                submit_time = send_time
                post_enter_output_start = len(captured_output)
                try:
                    acknowledged = client.send(payload)
                    input_records.append({
                        "phase": "command_and_terminator",
                        "bytes": len(payload), "payload_hex": payload.hex(),
                        "command_sha256": hashlib.sha256(command_bytes).hexdigest(),
                        "terminator_hex": terminator.hex(),
                        "acknowledged_bytes": acknowledged,
                        "sent_at": send_time,
                    })
                except Exception as exc:
                    input_records.append({
                        "phase": "command_and_terminator",
                        "bytes": len(payload), "payload_hex": payload.hex(),
                        "send_error": type(exc).__name__, "sent_at": send_time,
                    })
                    raise

            result["command_echo"] = command_echo
            deadline = time.monotonic() + 15
            last_error_text = ""
            prompt_seen = False
            while time.monotonic() < deadline:
                update = client.poll()
                if update.screen is not None:
                    screen_text = _compact_screen_text(update.screen)
                    command_echo = command_echo or command in screen_text
                    if any(token in screen_text for token in (
                        "ParserError", "Set-Location :", "Set-Content :",
                        "Cannot find path", "Exception",
                    )):
                        last_error_text = screen_text
                    command_index = screen_text.rfind(command)
                    result_path_index = screen_text.find(
                        str(destination), command_index + len(command)
                    ) if command_index >= 0 else -1
                    prompt_index = screen_text.rfind(expected_prompt.rstrip())
                    result["result_path_in_screen"] = (
                        "YES" if result_path_index >= 0 else "NO"
                    )
                    if result_path_index >= 0 and prompt_index > result_path_index:
                        prompt_seen = True
                        result["new_prompt"] = "YES"
                if marker_path.is_file():
                    try:
                        if marker_path.read_text(encoding="utf-8").strip() == nonce:
                            result["command_execution"] = "YES"
                            result["marker_file"] = "PRESENT_MATCH"
                            result["marker_file_content"] = "MATCH"
                        else:
                            result["command_execution"] = "NO"
                            result["marker_file"] = "PRESENT_MISMATCH"
                            result["marker_file_content"] = "MISMATCH"
                    except (OSError, UnicodeError):
                        result["marker_file"] = "PRESENT_UNREADABLE"
                        result["marker_file_content"] = "UNREADABLE"
                if result["command_execution"] == "YES" and prompt_seen:
                    break
                time.sleep(0.01)

            if not marker_path.exists():
                result["marker_file"] = "ABSENT"
                if last_error_text:
                    result["command_execution"] = "NO"
                result["marker_file_content"] = "ABSENT"
            if last_error_text:
                result["powershell_error"] = _r02_redact(
                    last_error_text,
                    (tmp_path, start, destination, os.environ.get("USERPROFILE", "")),
                )
                if result["command_execution"] != "YES":
                    result["command_execution"] = "NO"
            elif result["command_execution"] != "YES":
                result["command_execution"] = "UNKNOWN"

            latest_screen = latest.screen if latest is not None else None
            if latest_screen is not None:
                final_screen_text = _compact_screen_text(latest_screen)
                command_index = final_screen_text.rfind(command)
                result_path_index = final_screen_text.find(
                    str(destination), command_index + len(command)
                ) if command_index >= 0 else -1
                prompt_index = final_screen_text.rfind(expected_prompt.rstrip())
                result["result_path_in_screen"] = (
                    "YES" if result_path_index >= 0 else "NO"
                )
                if result_path_index >= 0 and prompt_index > result_path_index:
                    result["new_prompt"] = "YES"
                elif result["command_execution"] == "YES":
                    result["new_prompt"] = "NO"
                result["screen_repr"] = _r02_redact(
                    final_screen_text,
                    (tmp_path, start, destination, os.environ.get("USERPROFILE", "")),
                )
            post_enter_output = (
                bytes(captured_output[post_enter_output_start:])
                if post_enter_output_start is not None else b""
            )
            compact_post_enter = post_enter_output.replace(b"\r", b"").replace(b"\n", b"")
            result.update({
                "command_echo": command_echo,
                "post_enter_output_bytes": len(post_enter_output),
                "post_enter_output_repr": _r02_redact(
                    post_enter_output.decode("utf-8", errors="backslashreplace"),
                    (tmp_path, start, destination, os.environ.get("USERPROFILE", "")),
                ),
                "result_path_in_poll_output": (
                    "YES" if str(destination).encode("utf-8") in compact_post_enter
                    else "NO"
                ),
            })
            result["post_enter_output_hex_redacted"] = result[
                "post_enter_output_repr"
            ].encode("utf-8").hex()
            if submit_time is not None:
                result["post_enter_elapsed_seconds"] = round(
                    time.monotonic() - submit_time, 3
                )
            result.update({
                "poll_output_bytes": len(captured_output),
                "poll_output_sha256": hashlib.sha256(captured_output).hexdigest(),
                "poll_output_repr": _r02_redact(
                    bytes(captured_output).decode("utf-8", errors="backslashreplace"),
                    (tmp_path, start, destination, os.environ.get("USERPROFILE", "")),
                ),
                "poll_output_hex_redacted": _r02_redact(
                    bytes(captured_output).decode("utf-8", errors="backslashreplace"),
                    (tmp_path, start, destination, os.environ.get("USERPROFILE", "")),
                ).encode("utf-8").hex(),
                "final_sequence": getattr(latest, "sequence", None),
                "final_snapshot_sequence": getattr(latest, "snapshot_sequence", None),
                "final_synced": getattr(latest, "synced", None),
                "final_gap": getattr(latest, "gap", None),
                "final_more": getattr(latest, "more", None),
            })
        except Exception as exc:
            result["final_error"] = type(exc).__name__
            if latest is not None:
                result.update({
                    "final_sequence": getattr(latest, "sequence", None),
                    "final_snapshot_sequence": getattr(latest, "snapshot_sequence", None),
                    "final_synced": getattr(latest, "synced", None),
                    "final_gap": getattr(latest, "gap", None),
                    "final_more": getattr(latest, "more", None),
                })
        finally:
            if submit_time is not None:
                result["post_enter_elapsed_seconds"] = round(
                    time.monotonic() - submit_time, 3
                )
            if client is not None:
                try:
                    client.close()
                except Exception as exc:
                    result["cleanup"] = f"client_close_{type(exc).__name__}"
            if identity is not None:
                try:
                    if registry is None:
                        raise RuntimeError("registry_unavailable_for_identity_cleanup")
                    registry.stop(
                        identity.session_id,
                        expected_generation=identity.generation,
                        expected_shell_pid=identity.shell_pid,
                    )
                    result["cleanup"] = (
                        "STOPPED" if not _process_same(
                            identity.shell_pid, identity.shell_start
                        ) else "PROCESS_STILL_MATCHES_IDENTITY"
                    )
                except Exception as exc:
                    result["cleanup"] = f"STOP_ERROR_{type(exc).__name__}"

        if write_log.is_file():
            try:
                write_records = [
                    json.loads(line)
                    for line in write_log.read_text(encoding="utf-8").splitlines()
                    if line
                ]
                write_payload = b"".join(
                    bytes.fromhex(record["input_hex"]) for record in write_records
                )
                write_result_counts = {}
                write_errors = []
                for record in write_records:
                    label = (
                        f"{record['write_result_type']}:{record['write_result']}"
                    )
                    write_result_counts[label] = write_result_counts.get(label, 0) + 1
                    if record["write_error"]:
                        write_errors.append(record["write_error"])
                result["pty_write_records"] = {
                    "calls": len(write_records),
                    "input_bytes": len(write_payload),
                    "input_hex": write_payload.hex(),
                    "write_result_counts": write_result_counts,
                    "write_errors": write_errors,
                }
            except (OSError, ValueError, UnicodeError):
                result["pty_write_records"] = "unreadable"
        else:
            result["pty_write_records"] = "not_recorded"

        if read_log.is_file():
            try:
                trace = json.loads(read_log.read_text(encoding="utf-8"))
                read_chunks = [event["text"] for event in trace.get("chunks", [])]
                read_text = "".join(read_chunks)
                redaction_roots = (
                    tmp_path, start, destination, os.environ.get("USERPROFILE", "")
                )
                safe_read_text = _r02_redact(read_text, redaction_roots)
                read_bytes = read_text.encode("utf-8", errors="strict")
                read_after_enter = "".join(
                    event["text"] for event in trace.get("chunks", [])
                    if submit_time is not None and event["time"] >= submit_time
                )
                post_enter_events = [
                    event for event in trace.get("chunks", [])
                    if submit_time is not None and event["time"] >= submit_time
                ]
                safe_read_after_enter = _r02_redact(
                    read_after_enter, redaction_roots
                )
                result.update({
                    "pty_read_text_utf8_bytes": len(read_bytes),
                    "pty_read_text_sha256": hashlib.sha256(read_bytes).hexdigest(),
                    "pty_read_text_repr": safe_read_text,
                    "pty_read_utf8_hex_redacted": safe_read_text.encode("utf-8").hex(),
                    "post_enter_pty_read_text_repr": safe_read_after_enter,
                    "post_enter_pty_read_utf8_hex_redacted": (
                        safe_read_after_enter.encode("utf-8").hex()
                    ),
                    "post_enter_pty_read_first_seconds": (
                        round(post_enter_events[0]["time"] - submit_time, 4)
                        if post_enter_events else None
                    ),
                    "post_enter_pty_read_last_seconds": (
                        round(post_enter_events[-1]["time"] - submit_time, 4)
                        if post_enter_events else None
                    ),
                    "pty_read_chunks": len(read_chunks),
                    "pty_read_truncated": bool(trace.get("truncated")),
                })
            except (OSError, ValueError, UnicodeError, KeyError, TypeError):
                result["pty_read_text_repr"] = "unreadable"

        # Never print unredacted temporary paths. The command is constant and
        # contains no nonce; the child environment and file marker carry it.
        safe_input_records = []
        character_records = [
            record for record in input_records if record["phase"] == "command_char"
        ]
        other_records = [
            record for record in input_records if record["phase"] != "command_char"
        ]
        if character_records:
            command_payload = b"".join(
                bytes.fromhex(record["payload_hex"]) for record in character_records
            )
            safe_input_records.append({
                "phase": "command_character_delivery",
                "send_calls": len(character_records),
                "input_bytes": len(command_payload),
                "payload_hex": command_payload.hex(),
                "acknowledged_bytes": sum(
                    int(record.get("acknowledged_bytes", 0))
                    for record in character_records
                ),
                "send_errors": [
                    record["send_error"] for record in character_records
                    if "send_error" in record
                ],
            })
        safe_input_records.extend(dict(record) for record in other_records)
        result["input_records"] = safe_input_records
        summaries.append(result)
        print("R02_DIAGNOSTIC " + json.dumps(result, sort_keys=True))

    assert len(summaries) == 6
    assert {item["case"] for item in summaries} == {
        f"{delivery.lower()}-{terminator.lower()}"
        for delivery in ("character", "bulk")
        for terminator in ("CR", "LF", "CRLF")
    }
    assert all(item["cleanup"] == "STOPPED" for item in summaries), summaries


def _r02_startup_stage_worker_copy(
        tmp_path, monkeypatch, trace_dir, nonce, expected_prompt, path_marker):
    """Instrument only a private worker copy; the original worker remains untouched."""
    from tmux_agent_tower.terminal import windows_manager

    source_root = Path(windows_manager.__file__).resolve().parents[2]
    source_package = next(
        (source_root / package for package in (
            "tmux_agent_tower", "ju0_terminal_tower",
        ) if (source_root / package).is_dir()),
        None,
    )
    if source_package is None:
        raise AssertionError("JTT terminal source is unavailable for startup diagnostics")
    package_copy = tmp_path / "r02-startup-worker-copy" / "src" / "tmux_agent_tower"
    package_copy.parent.mkdir(parents=True)
    shutil.copytree(
        source_package, package_copy,
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    worker = package_copy / "terminal" / "_windows_worker.py"
    source = worker.read_text(encoding="utf-8")

    marker_helper = '''def _r02_startup_mark(stage, **details):
    root = os.environ.get("JTT_STARTUP_TRACE_DIR")
    nonce = os.environ.get("JTT_STARTUP_NONCE")
    if not root or not nonce:
        return
    record = {
        "stage": stage, "nonce": nonce, "worker_pid": os.getpid(),
        "utc_ms": int(time.time() * 1000),
    }
    record.update(details)
    path = Path(root) / (nonce + "." + stage + ".json")
    try:
        with path.open("x", encoding="utf-8") as stream:
            json.dump(record, stream, sort_keys=True)
            stream.flush()
    except (FileExistsError, OSError, TypeError, ValueError):
        pass


'''
    import_anchor = "import weakref\n"
    if source.count(import_anchor) != 1:
        raise AssertionError("startup diagnostic import anchor changed")
    source = source.replace(import_anchor, import_anchor + "\n" + marker_helper, 1)

    environment_anchor = '        shell_env["TERM"] = "xterm"\n'
    environment_forward = environment_anchor + '''        for _r02_key in (
            "JTT_STARTUP_TRACE_DIR", "JTT_STARTUP_NONCE",
            "JTT_STARTUP_EXPECTED_PROMPT", "JTT_STARTUP_MARKER",
        ):
            if _r02_key in os.environ:
                shell_env[_r02_key] = os.environ[_r02_key]
'''
    if source.count(environment_anchor) != 1:
        raise AssertionError("startup diagnostic environment anchor changed")
    source = source.replace(environment_anchor, environment_forward, 1)

    release_anchor = "            win32event.SetEvent(gate)\n"
    if source.count(release_anchor) != 1:
        raise AssertionError("startup diagnostic release gate anchor changed")
    source = source.replace(
        release_anchor,
        release_anchor + '            _r02_startup_mark("worker_release_gate")\n',
        1,
    )

    ps_helper = (
        "function global:__jttStartupMark { "
        "param([string]$stage,[string]$detail=''); "
        "if (!$env:JTT_STARTUP_TRACE_DIR -or !$env:JTT_STARTUP_NONCE) { return }; "
        "try { $path=Join-Path $env:JTT_STARTUP_TRACE_DIR "
        "($env:JTT_STARTUP_NONCE+'.'+$stage+'.json'); "
        "if (Test-Path -LiteralPath $path) { return }; "
        "$record=@{nonce=$env:JTT_STARTUP_NONCE;stage=$stage;pid=$PID;"
        "utc_ms=[DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()}; "
        "if ($stage -eq 'bootstrap_initialized') { "
        "$record.powershell_version=$PSVersionTable.PSVersion.ToString(); "
        "$record.powershell_edition=$PSVersionTable.PSEdition; "
        "$record.columns=$Host.UI.RawUI.WindowSize.Width; "
        "$record.lines=$Host.UI.RawUI.WindowSize.Height; "
        "$record.input_codepage=[Console]::InputEncoding.CodePage }; "
        "if ($stage -eq 'prompt_return') { "
        "$record.prompt_matches_expected=($detail -eq "
        "$env:JTT_STARTUP_EXPECTED_PROMPT); "
        "$record.prompt_length=$detail.Length }; "
        "[System.IO.File]::WriteAllText($path,($record|ConvertTo-Json -Compress)) "
        "} catch {} }; "
    )
    try_anchor = '        "try { "\n'
    if source.count(try_anchor) != 1:
        raise AssertionError("startup diagnostic PowerShell bootstrap anchor changed")
    source = source.replace(
        try_anchor,
        "        " + json.dumps(ps_helper + "try { ") + "\n",
        1,
    )

    for anchor, injected in (
        ('        "$r.Dispose(); $a.Dispose(); "\n',
         "__jttStartupMark 'child_gate_passed'; "),
        ('        "Remove-Module PSReadLine -ErrorAction SilentlyContinue; "\n',
         "__jttStartupMark 'bootstrap_initialized' "
         "($PSVersionTable.PSVersion.ToString()+'|'+"
         "$Host.UI.RawUI.WindowSize.Width+'|'+"
         "$Host.UI.RawUI.WindowSize.Height+'|'+"
         "[Console]::InputEncoding.CodePage); "),
        ('        "function global:prompt { "\n',
         "__jttStartupMark 'prompt_invoked'; "),
    ):
        if source.count(anchor) != 1:
            raise AssertionError("startup diagnostic PowerShell stage anchor changed")
        source = source.replace(
            anchor, anchor + "        " + json.dumps(injected) + "\n", 1
        )

    prompt_end = '        "}; \'PS \'+$path+\'> \' }"\n'
    prompt_replacement = (
        "}; $jttPromptReturn='PS '+$path+'> '; "
        "__jttStartupMark 'prompt_return' $jttPromptReturn; "
        "$jttPromptReturn }; __jttStartupMark 'prompt_registered'"
    )
    if source.count(prompt_end) != 1:
        raise AssertionError("startup diagnostic prompt return anchor changed")
    source = source.replace(
        prompt_end, "        " + json.dumps(prompt_replacement) + "\n", 1
    )

    read_state_anchor = "        output = deque()\n"
    if source.count(read_state_anchor) != 1:
        raise AssertionError("startup diagnostic reader state anchor changed")
    source = source.replace(
        read_state_anchor,
        read_state_anchor + '        _r02_startup_read_tail = ""\n',
        1,
    )
    reader_anchor = (
        "                nonlocal sequence, output_bytes, screen_error, state, exit_code\n"
    )
    if source.count(reader_anchor) != 1:
        raise AssertionError("startup diagnostic reader closure anchor changed")
    source = source.replace(
        reader_anchor,
        "                nonlocal sequence, output_bytes, screen_error, state, exit_code, _r02_startup_read_tail\n",
        1,
    )
    read_anchor = (
        "                        if data:\n"
        "                            encoded = data.encode(\"utf-8\", errors=\"strict\")\n"
    )
    read_trace = '''                        if data:
                            _r02_expected_prompt = os.environ.get(
                                "JTT_STARTUP_EXPECTED_PROMPT", ""
                            )
                            _r02_startup_read_tail = (
                                _r02_startup_read_tail + data
                            )[-8192:]
                            _r02_read_prompt_seen = bool(
                                _r02_expected_prompt
                                and _r02_expected_prompt in _r02_startup_read_tail
                            )
                            encoded = data.encode("utf-8", errors="strict")
'''
    if source.count(read_anchor) != 1:
        raise AssertionError("startup diagnostic PTY read anchor changed")
    source = source.replace(read_anchor, read_trace, 1)
    feed_anchor = (
        "                            with screen_lock:\n"
        "                                stream.feed(data)\n"
    )
    feed_trace = '''                            with screen_lock:
                                stream.feed(data)
                                _r02_cell_text = "\\n".join(
                                    "".join(
                                        pyte_screen.buffer[y][x].data
                                        for x in range(pyte_screen.columns)
                                    )
                                    for y in range(pyte_screen.lines)
                                )
                                _r02_screen_prompt_seen = bool(
                                    _r02_expected_prompt
                                    and _r02_expected_prompt in _r02_cell_text
                                )
                            if _r02_read_prompt_seen:
                                _r02_startup_mark("pty_read_prompt")
                            if _r02_screen_prompt_seen:
                                _r02_startup_mark("pyte_screen_prompt")
'''
    if source.count(feed_anchor) != 1:
        raise AssertionError("startup diagnostic pyte feed anchor changed")
    source = source.replace(feed_anchor, feed_trace, 1)
    worker.write_text(source, encoding="utf-8")

    original_environment = windows_manager._worker_environment
    monkeypatch.setattr(windows_manager, "WORKER", worker)

    def diagnostic_environment(session_dir):
        env = original_environment(session_dir)
        env.update({
            "JTT_STARTUP_TRACE_DIR": str(trace_dir),
            "JTT_STARTUP_NONCE": nonce,
            "JTT_STARTUP_EXPECTED_PROMPT": expected_prompt + " ",
            "JTT_STARTUP_MARKER": str(path_marker),
        })
        return env

    monkeypatch.setattr(
        windows_manager, "_worker_environment", diagnostic_environment
    )
    return windows_manager


def test_r02_startup_stage_diagnostic_separates_original_and_instrumented_copy(
        tmp_path, monkeypatch):
    """Compare a plain session with a separately instrumented worker copy."""
    import ju0_terminal_tower  # noqa: F401 - installs the guarded shared namespace
    from tmux_agent_tower.terminal import TerminalRegistry, windows_manager

    case_root = tmp_path / "r02-startup02"
    start = case_root / "work" / ("segment_" + "a" * 24) / ("segment_" + "b" * 24)
    destination = start / "dest"
    destination.mkdir(parents=True)
    assert 100 < len(str(start)) < 230
    monkeypatch.chdir(start)
    expected_start_prompt = _expected_prompt_path(start, 100)
    expected_destination_prompt = _expected_prompt_path(destination, 100)
    nonce = uuid.uuid4().hex

    def identity_record(identity, manager):
        created, image, alive = manager._process_facts(
            identity.shell_pid, expected_start=identity.shell_start,
        )
        return {
            "session_id": identity.session_id,
            "generation": identity.generation,
            "supervisor_pid": identity.supervisor_pid,
            "supervisor_start": identity.supervisor_start,
            "shell_pid": identity.shell_pid,
            "shell_start": identity.shell_start,
            "shell_start_matches": created == identity.shell_start,
            "shell_image_matches": image.casefold() == identity.shell_path.casefold(),
            "shell_alive": bool(alive and created == identity.shell_start),
        }

    def run_original():
        registry = TerminalRegistry(case_root / "original-jtt-home" / "terminals")
        client = registry.create("R02 original uninstrumented startup")
        identity = client.identity
        result = {
            "run": "original_uninstrumented",
            "identity": identity_record(identity, windows_manager),
            "initial_size": [100, 30],
            "first_prompt": "UNKNOWN",
            "prompt_in_poll_output": False,
            "prompt_in_client_snapshot": False,
            "sequence": None,
            "snapshot_sequence": None,
            "synced": None,
            "gap": None,
            "powershell": "UNKNOWN",
            "cleanup": "NOT_ATTEMPTED",
        }
        try:
            deadline = time.monotonic() + 15
            collected_output = ""
            while time.monotonic() < deadline:
                update = client.poll()
                collected_output += update.output.decode("utf-8", errors="replace")
                visible = bool(
                    update.screen is not None
                    and expected_start_prompt in _compact_screen_text(update.screen)
                )
                if visible:
                    result.update({
                        "first_prompt": "YES",
                        "prompt_in_poll_output": expected_start_prompt in collected_output,
                        "prompt_in_client_snapshot": True,
                        "sequence": update.sequence,
                        "snapshot_sequence": update.snapshot_sequence,
                        "synced": update.synced,
                        "gap": update.gap,
                    })
                    break
                time.sleep(0.02)
            if result["first_prompt"] == "YES" and client.acquire():
                version_nonce = uuid.uuid4().hex[:10]
                marker = f"JTT_STARTUP_VERSION_{version_nonce}_END"
                command = (
                    "[Console]::WriteLine('JTT_STARTUP_VERSION_' + '"
                    + version_nonce
                    + "' + '_END|' + $PSVersionTable.PSVersion.ToString() + '|' + "
                    "$Host.UI.RawUI.WindowSize.Width + 'x' + "
                    "$Host.UI.RawUI.WindowSize.Height + '|' + "
                    "[Console]::InputEncoding.CodePage)"
                )
                assert marker not in command
                client.send((command + "\r").encode("ascii"))
                deadline = time.monotonic() + 10
                output_text = ""
                while time.monotonic() < deadline:
                    update = client.poll()
                    output_text += update.output.decode("utf-8", errors="replace")
                    match = re.search(
                        rf"{re.escape(marker)}\|([0-9.]+)\|(\d+x\d+)\|(\d+)",
                        output_text,
                    )
                    if match:
                        result["powershell"] = {
                            "version": match.group(1),
                            "columns_lines": match.group(2),
                            "input_codepage": int(match.group(3)),
                        }
                        break
                    time.sleep(0.02)
            return result
        finally:
            client.close()
            registry.stop(
                identity.session_id, expected_generation=identity.generation,
                expected_shell_pid=identity.shell_pid,
            )
            created, _image, alive = windows_manager._process_facts(
                identity.shell_pid, expected_start=identity.shell_start,
            )
            result["cleanup"] = "STOPPED" if not alive else "PROCESS_STILL_ALIVE"
            result["shell_identity_matched_before_stop"] = (
                result["identity"]["shell_start_matches"]
            )
            result["registry_entries_after_cleanup"] = len(registry.list_sessions())

    original_result = run_original()
    print(
        "STARTUP02_ORIGINAL_UNMODIFIED "
        + json.dumps(original_result, sort_keys=True),
        flush=True,
    )

    trace_dir = case_root / "private-trace"
    windows_manager._ensure_private_directory(trace_dir)
    windows_manager._private_acl(trace_dir, directory=True)

    def secure_private_marker(path):
        windows_manager._private_acl(path, directory=False, apply=True)
        windows_manager._private_acl(path, directory=False)

    permission_probe = trace_dir / (nonce + ".acl-probe")
    permission_probe.write_text(nonce, encoding="ascii")
    secure_private_marker(permission_probe)
    assert permission_probe.read_text(encoding="ascii") == nonce
    permission_probe.unlink()

    path_marker = trace_dir / (nonce + ".path-executed")
    diagnostic_manager = _r02_startup_stage_worker_copy(
        tmp_path, monkeypatch, trace_dir, nonce,
        expected_start_prompt, path_marker,
    )
    registry = TerminalRegistry(
        case_root / "instrumented-jtt-home" / "terminals"
    )
    client = registry.create("R02 instrumented startup replica")
    identity = client.identity
    replica = {
        "run": "instrumented_replica",
        "identity": identity_record(identity, diagnostic_manager),
        "initial_size": [100, 30],
        "first_prompt": "UNKNOWN",
        "prompt_in_poll_output": False,
        "prompt_in_client_snapshot": False,
        "sequence": None,
        "snapshot_sequence": None,
        "synced": None,
        "gap": None,
        "path_change": "NOT_ATTEMPTED",
        "cleanup": "NOT_ATTEMPTED",
        "marker_directory_private": True,
        "marker_write_preflight": True,
    }
    try:
        deadline = time.monotonic() + 15
        collected_output = ""
        while time.monotonic() < deadline:
            update = client.poll()
            collected_output += update.output.decode("utf-8", errors="replace")
            visible = bool(
                update.screen is not None
                and expected_start_prompt in _compact_screen_text(update.screen)
            )
            if visible:
                replica.update({
                    "first_prompt": "YES",
                    "prompt_in_poll_output": expected_start_prompt in collected_output,
                    "prompt_in_client_snapshot": True,
                    "sequence": update.sequence,
                    "snapshot_sequence": update.snapshot_sequence,
                    "synced": update.synced,
                    "gap": update.gap,
                })
                break
            time.sleep(0.02)

        if replica["first_prompt"] == "YES" and client.acquire():
            result_marker = f"JTT_STARTUP_PATH_RESULT_{nonce}_END"
            command = (
                "Set-Location 'dest'; "
                "[System.IO.File]::WriteAllText($env:JTT_STARTUP_MARKER,$env:JTT_STARTUP_NONCE); "
                "[Console]::WriteLine('JTT_STARTUP_PATH_RESULT_' + "
                "$env:JTT_STARTUP_NONCE + '_END'); $PWD.Path"
            )
            assert result_marker not in command
            client.send((command + "\r\n").encode("utf-8"))
            deadline = time.monotonic() + 15
            output_text = ""
            next_prompt_seen = False
            path_marker_acl_valid = False
            last_update = None
            while time.monotonic() < deadline:
                last_update = client.poll()
                output_text += last_update.output.decode("utf-8", errors="replace")
                screen = (
                    _compact_screen_text(last_update.screen)
                    if last_update.screen is not None else ""
                )
                if path_marker.is_file() and not path_marker_acl_valid:
                    secure_private_marker(path_marker)
                    path_marker_acl_valid = True
                next_prompt_seen = expected_destination_prompt in screen
                if (result_marker in output_text and path_marker_acl_valid
                        and next_prompt_seen):
                    break
                time.sleep(0.02)
            path_marker_status = "UNKNOWN"
            if path_marker_acl_valid and path_marker.is_file():
                path_marker_status = (
                    "YES"
                    if path_marker.read_text(encoding="ascii") == nonce
                    else "NO"
                )
            replica["path_change"] = {
                "unique_output_marker": result_marker in output_text,
                "nonce_file_status": path_marker_status,
                "nonce_file_acl_valid": path_marker_acl_valid,
                "destination_prompt": next_prompt_seen,
                "sequence": getattr(last_update, "sequence", None),
                "snapshot_sequence": getattr(last_update, "snapshot_sequence", None),
            }

        stages = (
            "worker_release_gate", "child_gate_passed", "bootstrap_initialized",
            "prompt_registered", "prompt_invoked", "prompt_return",
            "pty_read_prompt", "pyte_screen_prompt",
        )
        stage_results = {}
        for stage in stages:
            marker_file = trace_dir / f"{nonce}.{stage}.json"
            if not marker_file.is_file():
                stage_results[stage] = {
                    "status": "UNKNOWN",
                    "evidence": "marker_absent_is_not_stage_failure",
                }
                continue
            try:
                secure_private_marker(marker_file)
                record = json.loads(marker_file.read_text(encoding="utf-8"))
                status = "YES" if record.get("nonce") == nonce else "UNKNOWN"
                stage_results[stage] = {
                    "status": status,
                    "marker_acl_valid": True,
                    "pid": record.get("pid", record.get("worker_pid")),
                    "utc_ms": record.get("utc_ms"),
                }
                if stage == "bootstrap_initialized":
                    stage_results[stage].update({
                        key: record.get(key) for key in (
                            "powershell_version", "powershell_edition",
                            "columns", "lines", "input_codepage",
                        )
                    })
                if stage == "prompt_return":
                    stage_results[stage]["prompt_matches_expected"] = record.get(
                        "prompt_matches_expected", False
                    )
            except (OSError, ValueError, UnicodeError):
                stage_results[stage] = {
                    "status": "UNKNOWN", "evidence": "marker_unreadable",
                }
        replica["stages"] = stage_results
        replica["marker_directory_private"] = windows_manager.is_private_directory(
            trace_dir
        )
    finally:
        client.close()
        registry.stop(
            identity.session_id, expected_generation=identity.generation,
            expected_shell_pid=identity.shell_pid,
        )
        _created, _image, alive = diagnostic_manager._process_facts(
            identity.shell_pid, expected_start=identity.shell_start,
        )
        replica["cleanup"] = "STOPPED" if not alive else "PROCESS_STILL_ALIVE"
        replica["shell_identity_matched_before_stop"] = (
            replica["identity"]["shell_start_matches"]
        )
        replica["registry_entries_after_cleanup"] = len(registry.list_sessions())
        print(
            "STARTUP02_INSTRUMENTED_REPLICA "
            + json.dumps(replica, sort_keys=True),
            flush=True,
        )
        assert not alive, "isolated diagnostic PowerShell remained alive after stop"
        assert len(registry.list_sessions()) == 0
