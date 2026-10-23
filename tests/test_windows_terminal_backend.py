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
              and "JTT>" in _screen_text(update.screen))
        assert client.acquire()
        marker = _generated_marker(client, "CREATE")
        assert marker

        # The server permits one connected input channel. A second connection
        # is rejected by the single-instance pipe rather than sharing a lease.
        with pytest.raises(Exception):
            registry.connect(identity.session_id, timeout=0.1)

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
                            after=client._sequence,
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
