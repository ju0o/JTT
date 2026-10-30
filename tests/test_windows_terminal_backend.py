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


def _finalize_conpty_path_result(result):
    """Snapshot path, error, and post-finally cleanup evidence for diagnostics."""
    finalized = dict(result)
    finalized["finalization"] = {
        "status": finalized.get("status"),
        "diagnostic_error_stage": finalized.get("diagnostic_error_stage"),
        "diagnostic_error_category": finalized.get("diagnostic_error_category"),
        "cleanup": finalized.get("cleanup"),
        "cleanup_confirmed": bool(finalized.get("cleanup_confirmed")),
        "cleanup_error_category": finalized.get("cleanup_error_category"),
        "cleanup_handle_close_error_category": finalized.get(
            "cleanup_handle_close_error_category"
        ),
        "client_close_error_category": finalized.get("client_close_error_category"),
        "retained_handle_signaled_after_cleanup": finalized.get(
            "retained_handle_signaled_after_cleanup"
        ),
    }
    return finalized


def _conpty_path_allows_followup(result):
    """Allow the worker comparison only when direct-path cleanup is complete."""
    return bool(result.get("cleanup_confirmed")) and not any(
        result.get(key)
        for key in (
            "cleanup_error_category",
            "cleanup_handle_close_error_category",
            "client_close_error_category",
        )
    )


class _ConptyPathAbort(Exception):
    """Stop one diagnostic path while still finalizing its cleanup evidence."""


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
        surviving = registry.list_sessions()
        assert len(surviving) == 1
        assert surviving[0] == identity
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


def _retain_test_process_handle(
        pid, expected_created, expected_image, *, allow_terminate=False):
    """Open only a PID emitted by this test and retain its verified handle."""
    import win32api
    import win32con
    import win32event
    import win32process
    from tmux_agent_tower.terminal.windows_manager import _process_image

    access = win32con.PROCESS_QUERY_LIMITED_INFORMATION | win32con.SYNCHRONIZE
    if allow_terminate:
        # Used only for the direct ConPTY PowerShell this test spawned. The
        # handle is identity-checked below before it can authorize cleanup.
        access |= win32con.PROCESS_TERMINATE
    handle = win32api.OpenProcess(
        access,
        False, int(pid),
    )
    try:
        if win32event.WaitForSingleObject(handle, 0) != win32con.WAIT_TIMEOUT:
            raise AssertionError("test-owned process was not alive before Stop")
        created = str(win32process.GetProcessTimes(handle)["CreationTime"])
        if created != str(expected_created):
            raise AssertionError("test-owned process creation identity changed")
        image = _process_image(handle)
        if image != os.path.normcase(os.path.abspath(expected_image)):
            raise AssertionError("test-owned process image identity changed")
        return handle
    except BaseException:
        handle.Close()
        raise


def _retained_process_handle_signaled(handle):
    """Confirm exit on a handle retained before Stop, without PID re-query."""
    import win32con
    import win32event

    result = win32event.WaitForSingleObject(handle, 0)
    if result == win32con.WAIT_OBJECT_0:
        return True
    if result == win32con.WAIT_TIMEOUT:
        return False
    raise AssertionError("retained test-owned process handle returned unknown state")


def test_native_stop_terminates_nested_children_in_the_owned_job(
        tmp_path, monkeypatch, record_property):
    import ju0_terminal_tower  # noqa: F401 - installs the guarded shared namespace
    import win32job
    from tmux_agent_tower.terminal import TerminalRegistry, windows_manager
    from tmux_agent_tower.terminal.windows_manager import (
        _powershell_executable,
        _process_facts,
    )

    work = tmp_path / "isolated-work"
    work.mkdir()
    monkeypatch.chdir(work)
    registry = TerminalRegistry(tmp_path / "isolated-jtt" / "terminals")
    client = registry.create("Owned Job descendant cleanup")
    identity = client.identity
    marker_file = tmp_path / "owned-job-pids.txt"
    marker_path = str(marker_file).replace("'", "''")
    launcher_entry_file = tmp_path / "owned-job-launcher-entry.txt"
    launcher_entry_path = str(launcher_entry_file).replace("'", "''")
    launcher_stage_file = tmp_path / "owned-job-launcher-stage.txt"
    launcher_stage_path = str(launcher_stage_file).replace("'", "''")
    child_stage_file = tmp_path / "owned-job-child-stage.txt"
    child_stage_path = str(child_stage_file).replace("'", "''")
    grandchild_launch_file = tmp_path / "owned-job-grandchild-launch.txt"
    grandchild_launch_path = str(grandchild_launch_file).replace("'", "''")
    grandchild_stage_file = tmp_path / "owned-job-grandchild-stage.txt"
    grandchild_stage_path = str(grandchild_stage_file).replace("'", "''")
    child_script_file = work / "owned-job-child.ps1"
    child_script_path = str(child_script_file).replace("'", "''")
    launcher_script_file = work / "owned-job-launcher.ps1"
    grandchild_script_file = work / "owned-job-grandchild.ps1"
    grandchild_script_path = str(grandchild_script_file).replace("'", "''")
    nonce = uuid.uuid4().hex[:10]
    command_started_marker = f"JTT_OWNED_START_{nonce}_END"
    tree_marker_prefix = f"JTT_OWNED_TREE_{nonce}_"
    # Keep process creation as a short staged script. The previous test put a
    # nested PowerShell script, base64 payload, and Start-Process argument
    # quoting into one interactive line, so a started outer command did not
    # tell us whether child creation or command-line parsing had failed.
    grandchild_script_file.write_text(
        "\n".join((
            "try {",
            "[System.IO.File]::WriteAllText('" + grandchild_stage_path
            + "','grandchild_started|'+[string]$PID)",
            "Start-Sleep -Seconds 60",
            "} catch {",
            "$failure=$_.Exception.GetType().Name+'|'+$_.FullyQualifiedErrorId",
            "try { [System.IO.File]::WriteAllText('" + grandchild_stage_path
            + "','grandchild_error|'+$failure) } catch {",
            "$writeFailure=$_.Exception.GetType().Name+'|'+$_.FullyQualifiedErrorId",
            "[Console]::WriteLine('JTT_OWNED_STAGE_WRITE_ERROR|grandchild_error|'+$writeFailure)",
            "}",
            "[Console]::WriteLine('JTT_OWNED_GRANDCHILD_ERROR|'+$failure)",
            "throw",
            "}",
        )) + "\n",
        encoding="utf-8-sig",
    )
    child_script_file.write_text(
        "\n".join((
            "try {",
            "$stage='child_start_marker'",
            "[System.IO.File]::WriteAllText('" + child_stage_path
            + "','child_started|'+[string]$PID)",
            "$stage='grandchild_start_process'",
            "$grand=$null",
            "try {",
            "$grand=Start-Process -ErrorAction Stop -PassThru -WindowStyle Hidden "
            "-FilePath (Join-Path $PSHOME 'powershell.exe') "
            "-ArgumentList ('-NoLogo -NoProfile -File \"" + grandchild_script_path + "\"')",
            "} catch { throw }",
            "$stage='grandchild_launch_marker'",
            "try {",
            "[System.IO.File]::WriteAllText('" + grandchild_launch_path
            + "','grandchild_process_created|'+[string]$grand.Id)",
            "} catch { throw }",
            "$stage='grandchild_start_observation'",
            "$deadline=[DateTime]::UtcNow.AddSeconds(10)",
            "while (-not [System.IO.File]::Exists('" + grandchild_stage_path + "') "
            "-and [DateTime]::UtcNow -lt $deadline) { Start-Sleep -Milliseconds 25 }",
            "$grand_stage=[System.IO.File]::ReadAllText('" + grandchild_stage_path + "').Trim()",
            "if ($grand_stage -ne ('grandchild_started|'+[string]$grand.Id)) "
            "{ throw 'grandchild stage identity mismatch' }",
            "[System.IO.File]::WriteAllText('" + marker_path + "',"
            "([string]$PID+','+[string]$grand.Id))",
            "Start-Sleep -Seconds 60",
            "} catch {",
            "$failure=$stage+'|'+$_.Exception.GetType().Name+'|'"
            "+$_.FullyQualifiedErrorId",
            "try { [System.IO.File]::WriteAllText('" + child_stage_path
            + "','child_error|'+$failure) } catch {",
            "$writeFailure=$_.Exception.GetType().Name+'|'+$_.FullyQualifiedErrorId",
            "[Console]::WriteLine('JTT_OWNED_STAGE_WRITE_ERROR|child_error|'+$writeFailure)",
            "}",
            "[Console]::WriteLine('JTT_OWNED_CHILD_ERROR|'+$failure)",
            "throw",
            "}",
        )) + "\n",
        encoding="utf-8-sig",
    )
    launcher_script = "\n".join((
        "$ErrorActionPreference = 'Stop'",
        "$__jtt_entry = '" + launcher_entry_path + "'",
        "$__jtt_stage = '" + launcher_stage_path + "'",
        "$__jtt_script = '" + child_script_path + "'",
        "try { [System.IO.File]::WriteAllText($__jtt_entry,"
        "'launcher_entered|'+[string]$PID) } catch {",
        "$failure=$_.Exception.GetType().Name+'|' +$_.FullyQualifiedErrorId",
        "[Console]::WriteLine('JTT_OWNED_STAGE_WRITE_ERROR|launcher_entry|'+$failure)",
        "throw",
        "}",
        "[Console]::WriteLine('JTT_'+'OWNED_START_'+ '" + nonce + "'+'_END')",
        "$child = $null",
        "try {",
        "$child=Start-Process -ErrorAction Stop -PassThru -WindowStyle Hidden "
        "-FilePath (Join-Path $PSHOME 'powershell.exe') "
        "-ArgumentList ('-NoLogo -NoProfile -File \"'+$__jtt_script+'\"')",
        "} catch {",
        "$failure=$_.Exception.GetType().Name+'|'+$_.FullyQualifiedErrorId",
        "try { [System.IO.File]::WriteAllText($__jtt_stage,"
        "'start_process_error|'+$failure) } catch {",
        "$writeFailure=$_.Exception.GetType().Name+'|'+$_.FullyQualifiedErrorId",
        "[Console]::WriteLine('JTT_OWNED_STAGE_WRITE_ERROR|start_process|'+$writeFailure)",
        "}",
        "[Console]::WriteLine('JTT_OWNED_START_PROCESS_ERROR|'+$failure)",
        "return",
        "}",
        "[Console]::WriteLine('JTT_OWNED_CHILD_PROCESS_CREATED|'+[string]$child.Id)",
        "try { [System.IO.File]::WriteAllText($__jtt_stage,"
        "'child_process_created|'+[string]$child.Id) } catch {",
        "$writeFailure=$_.Exception.GetType().Name+'|'+$_.FullyQualifiedErrorId",
        "try { [System.IO.File]::WriteAllText($__jtt_stage,"
        "'child_created_but_stage_write_error|'+[string]$child.Id+'|'+$writeFailure) "
        "} catch { }",
        "[Console]::WriteLine('JTT_OWNED_STAGE_WRITE_ERROR|child_created|'+$writeFailure)",
        "}",
    )) + "\n"
    launcher_script_file.write_text(launcher_script, encoding="utf-8-sig")
    outer_command = ".\\owned-job-launcher.ps1"
    assert command_started_marker not in outer_command
    assert tree_marker_prefix not in outer_command
    assert len(outer_command) < 512
    record_property(
        "owned_job_preflight",
        json.dumps({
            "working_directory_matches": (
                os.path.normcase(os.path.abspath(os.getcwd()))
                == os.path.normcase(os.path.abspath(str(work)))
            ),
            "launcher_script_exists": launcher_script_file.is_file(),
            "launcher_stage_parent_exists": launcher_stage_file.parent.is_dir(),
            "launcher_entry_parent_exists": launcher_entry_file.parent.is_dir(),
            "child_script_exists": child_script_file.is_file(),
            "grandchild_script_exists": grandchild_script_file.is_file(),
        }, sort_keys=True),
    )
    processes = []
    stop_evidence = {}
    raw_output = bytearray()
    last_update = None
    preserved_client = None
    preserved_identity = None
    preserved_shell_handle = None

    def safe_powershell_error_categories(data):
        text = data.decode("utf-8", errors="replace")
        categories = (
            "ParserError", "PSSecurityException", "ExecutionPolicy",
            "UnauthorizedAccessException", "Start-Process",
            "Cannot find path", "JTT_OWNED_STAGE_WRITE_ERROR",
            "JTT_OWNED_START_PROCESS_ERROR", "JTT_OWNED_CHILD_ERROR",
            "JTT_OWNED_GRANDCHILD_ERROR", "JTT_OWNED_CHILD_PROCESS_CREATED",
        )
        return [category for category in categories if category in text]

    try:
        expected_prompt = _expected_prompt_path(work, 100)
        initial = _wait(
            client,
            lambda update: update.screen is not None
            and expected_prompt in _compact_screen_text(update.screen),
        )
        assert (initial.columns, initial.lines) == (100, 30)
        assert expected_prompt in _compact_screen_text(initial.screen)
        assert client.acquire()
        encoded_command = (outer_command + "\r").encode("ascii")
        assert client.send(encoded_command) == len(encoded_command)

        def collect_until(predicate, description, timeout=15):
            nonlocal last_update
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                last_update = client.poll()
                raw_output.extend(last_update.output)
                if predicate(last_update, bytes(raw_output)):
                    return last_update
                time.sleep(0.02)
            raise AssertionError(
                f"owned Job stage not observed: {description}; "
                f"state={getattr(last_update, 'state', None)!r}; "
                f"sequence={getattr(last_update, 'sequence', None)!r}; "
                f"snapshot_sequence={getattr(last_update, 'snapshot_sequence', None)!r}; "
                f"raw_bytes={len(raw_output)}; marker_file={marker_file.is_file()}"
                f"; error_categories={safe_powershell_error_categories(bytes(raw_output))!r}"
            )

        start_update = collect_until(
            lambda _item, output: (
                command_started_marker.encode("ascii")
                in output.replace(b"\r", b"").replace(b"\n", b"")
                or bool(safe_powershell_error_categories(output))
            ),
            "PowerShell launcher marker or categorized startup error",
        )
        compact_start_output = bytes(raw_output).replace(b"\r", b"").replace(b"\n", b"")
        command_started = command_started_marker.encode("ascii") in compact_start_output
        record_property("owned_job_command_started", command_started)
        record_property(
            "owned_job_initial_powershell_error_categories",
            json.dumps(safe_powershell_error_categories(bytes(raw_output))),
        )
        if command_started:
            assert start_update.screen is not None
            assert command_started_marker in _screen_text(start_update.screen)
        else:
            raise AssertionError(
                "PowerShell launcher did not emit its command marker; "
                "see categorized parser/startup evidence"
            )

        def read_stage(path):
            try:
                return path.read_text(encoding="utf-8-sig").strip()
            except FileNotFoundError:
                return "ABSENT"
            except (OSError, UnicodeError) as exc:
                return "READ_ERROR|" + type(exc).__name__

        launcher_deadline = time.monotonic() + 15
        launcher_stage = "ABSENT"
        launcher_entry = "ABSENT"
        while time.monotonic() < launcher_deadline:
            launcher_entry = read_stage(launcher_entry_file)
            launcher_stage = read_stage(launcher_stage_file)
            if launcher_stage.startswith((
                "child_process_created|", "child_created_but_stage_write_error|",
                "start_process_error|",
                "READ_ERROR|",
            )) or "JTT_OWNED_STAGE_WRITE_ERROR" in bytes(raw_output).decode(
                "utf-8", errors="replace"
            ):
                break
            last_update = client.poll()
            raw_output.extend(last_update.output)
            time.sleep(0.02)
        launcher_entry_parts = launcher_entry.split("|")
        record_property(
            "owned_job_launcher_entry_stage",
            launcher_entry_parts[0] if launcher_entry_parts else "ABSENT",
        )
        record_property(
            "owned_job_launcher_entry_pid_is_decimal",
            len(launcher_entry_parts) == 2 and launcher_entry_parts[1].isdecimal(),
        )
        launcher_parts = launcher_stage.split("|")
        record_property(
            "owned_job_launcher_stage",
            launcher_parts[0] if launcher_parts else "ABSENT",
        )
        record_property(
            "owned_job_launcher_stage_write_error_categories",
            json.dumps([
                category for category in safe_powershell_error_categories(bytes(raw_output))
                if category == "JTT_OWNED_STAGE_WRITE_ERROR"
            ]),
        )
        child_process_created_match = re.search(
            rb"JTT_OWNED_CHILD_PROCESS_CREATED\|([0-9]+)", bytes(raw_output)
        )
        record_property(
            "owned_job_start_process_outcome_marker_seen",
            child_process_created_match is not None
            or launcher_parts[0] == "start_process_error",
        )
        assert launcher_entry_parts[0] == "launcher_entered", (
            "PowerShell command marker was seen but launcher entry file did not "
            f"confirm script entry ({launcher_entry_parts[0] if launcher_entry_parts else 'ABSENT'})"
        )
        if (launcher_parts[0] not in {
                "child_process_created", "child_created_but_stage_write_error",
        } and child_process_created_match is None):
            record_property(
                "owned_job_start_process_error_category",
                launcher_parts[1] if len(launcher_parts) > 1 else "UNKNOWN",
            )
            record_property(
                "owned_job_start_process_error_id",
                launcher_parts[2] if len(launcher_parts) > 2 else "UNKNOWN",
            )
            raise AssertionError(
                "Start-Process stage did not confirm child creation; "
                f"observed={launcher_parts[0] if launcher_parts else 'ABSENT'}"
            )
        launcher_child_pid = (
            launcher_parts[1]
            if launcher_parts[0] in {
                "child_process_created", "child_created_but_stage_write_error",
            } and len(launcher_parts) > 1 else
            child_process_created_match.group(1).decode("ascii")
            if child_process_created_match is not None else ""
        )
        record_property("owned_job_launcher_child_pid_is_decimal",
                        launcher_child_pid.isdecimal())
        assert launcher_child_pid.isdecimal(), (
            "Start-Process returned without a decimal test-child PID"
        )
        expected_image = os.path.normcase(
            os.path.abspath(str(_powershell_executable()))
        )
        child_created, child_image, child_alive = _process_facts(
            int(launcher_child_pid)
        )
        assert child_alive and child_created and child_image == expected_image, (
            "the test-owned child PID did not resolve to the expected live "
            "PowerShell identity"
        )
        child_handle = _retain_test_process_handle(
            int(launcher_child_pid), child_created, expected_image
        )
        processes.append((int(launcher_child_pid), child_created, child_handle))
        record_property("owned_job_child_handle_retained_before_stop", True)

        child_started_deadline = time.monotonic() + 15
        child_stage = "ABSENT"
        while time.monotonic() < child_started_deadline:
            child_stage = read_stage(child_stage_file)
            if child_stage != "ABSENT":
                break
            last_update = client.poll()
            raw_output.extend(last_update.output)
            time.sleep(0.02)
        child_parts = child_stage.split("|")
        record_property(
            "owned_job_child_stage",
            child_parts[0] if child_parts else "ABSENT",
        )
        record_property(
            "owned_job_child_output_error_categories",
            json.dumps([
                category for category in safe_powershell_error_categories(bytes(raw_output))
                if category in {
                    "JTT_OWNED_STAGE_WRITE_ERROR", "JTT_OWNED_CHILD_ERROR",
                }
            ]),
        )
        if child_parts[0] == "child_error":
            record_property(
                "owned_job_child_error_stage",
                child_parts[1] if len(child_parts) > 1 else "UNKNOWN",
            )
            record_property(
                "owned_job_child_error_category",
                child_parts[2] if len(child_parts) > 2 else "UNKNOWN",
            )
            record_property(
                "owned_job_child_error_id",
                child_parts[3] if len(child_parts) > 3 else "UNKNOWN",
            )
        assert child_parts[0] == "child_started", (
            "child process was created but its script did not publish the "
            f"start stage (observed={child_parts[0] if child_parts else 'ABSENT'})"
        )
        child_stage_pid = child_parts[-1]
        assert child_stage_pid.isdecimal()
        assert child_stage_pid == launcher_child_pid, (
            "Start-Process returned a different child PID than the child script"
        )

        grandchild_launch_deadline = time.monotonic() + 15
        grandchild_launch = "ABSENT"
        while time.monotonic() < grandchild_launch_deadline:
            grandchild_launch = read_stage(grandchild_launch_file)
            if grandchild_launch != "ABSENT":
                break
            last_update = client.poll()
            raw_output.extend(last_update.output)
            time.sleep(0.02)
        grandchild_launch_parts = grandchild_launch.split("|")
        record_property(
            "owned_job_grandchild_process_stage",
            grandchild_launch_parts[0] if grandchild_launch_parts else "ABSENT",
        )
        if grandchild_launch_parts[0] != "grandchild_process_created":
            late_child_stage = read_stage(child_stage_file).split("|")
            if late_child_stage and late_child_stage[0] == "child_error":
                record_property(
                    "owned_job_grandchild_failure_stage",
                    late_child_stage[1] if len(late_child_stage) > 1 else "UNKNOWN",
                )
                record_property(
                    "owned_job_grandchild_failure_category",
                    late_child_stage[2] if len(late_child_stage) > 2 else "UNKNOWN",
                )
                record_property(
                    "owned_job_grandchild_failure_id",
                    late_child_stage[3] if len(late_child_stage) > 3 else "UNKNOWN",
                )
        assert grandchild_launch_parts[0] == "grandchild_process_created", (
            "child started but Start-Process did not confirm grandchild "
            f"creation (observed={grandchild_launch_parts[0] if grandchild_launch_parts else 'ABSENT'})"
        )
        launched_grandchild_pid = grandchild_launch_parts[-1]
        assert launched_grandchild_pid.isdecimal()
        grandchild_created, grandchild_image, grandchild_alive = _process_facts(
            int(launched_grandchild_pid)
        )
        assert (
            grandchild_alive and grandchild_created
            and grandchild_image == expected_image
        ), "the test-owned grandchild PID did not resolve to its expected live identity"
        grandchild_handle = _retain_test_process_handle(
            int(launched_grandchild_pid), grandchild_created, expected_image
        )
        processes.append((
            int(launched_grandchild_pid), grandchild_created, grandchild_handle
        ))
        record_property("owned_job_grandchild_handle_retained_before_stop", True)

        grandchild_deadline = time.monotonic() + 15
        grandchild_stage = ""
        while time.monotonic() < grandchild_deadline:
            grandchild_stage = read_stage(grandchild_stage_file)
            if grandchild_stage != "ABSENT":
                break
            last_update = client.poll()
            raw_output.extend(last_update.output)
            time.sleep(0.02)
        grandchild_stage_parts = grandchild_stage.split("|")
        record_property(
            "owned_job_grandchild_output_error_categories",
            json.dumps([
                category for category in safe_powershell_error_categories(bytes(raw_output))
                if category in {
                    "JTT_OWNED_STAGE_WRITE_ERROR", "JTT_OWNED_GRANDCHILD_ERROR",
                }
            ]),
        )
        if grandchild_stage_parts[0] == "grandchild_error":
            record_property(
                "owned_job_grandchild_error_category",
                grandchild_stage_parts[1]
                if len(grandchild_stage_parts) > 1 else "UNKNOWN",
            )
            record_property(
                "owned_job_grandchild_error_id",
                grandchild_stage_parts[2]
                if len(grandchild_stage_parts) > 2 else "UNKNOWN",
            )
        grandchild_pid_text = (
            grandchild_stage_parts[-1]
            if grandchild_stage_parts[0] == "grandchild_started"
            else ""
        )
        record_property(
            "owned_job_grandchild_started_marker_status",
            "VALID" if grandchild_pid_text.isdecimal() else "ABSENT_OR_INVALID",
        )
        assert grandchild_pid_text.isdecimal(), (
            "test-owned child started but the grandchild script did not publish "
            "its own PID marker"
        )

        file_deadline = time.monotonic() + 15
        pid_values = []
        marker_status = "ABSENT"
        while time.monotonic() < file_deadline:
            if marker_file.is_file():
                marker_status = "MALFORMED_OR_PARTIAL"
                try:
                    candidate_values = marker_file.read_text(
                        encoding="ascii"
                    ).strip().split(",")
                except (OSError, UnicodeError):
                    candidate_values = []
                if (len(candidate_values) == 2
                        and all(value.isdecimal() for value in candidate_values)):
                    pid_values = candidate_values
                    marker_status = "VALID"
                    break
            last_update = client.poll()
            raw_output.extend(last_update.output)
            time.sleep(0.02)
        record_property("owned_job_creation_marker_present", marker_file.is_file())
        record_property("owned_job_creation_marker_status", marker_status)
        assert pid_values, (
            "test-owned child stage started, but valid child/grandchild PID "
            f"identity data was not produced ({marker_status})"
        )
        child_pid, grandchild_pid = (int(value) for value in pid_values)
        assert child_pid == int(launcher_child_pid)
        assert grandchild_pid == int(launched_grandchild_pid)
        assert grandchild_pid == int(grandchild_pid_text), (
            "parent-reported grandchild PID did not match the grandchild's "
            "independent startup marker"
        )
        assert len({child_pid, grandchild_pid, identity.shell_pid}) == 3
        assert {pid for pid, _created, _handle in processes} == {
            child_pid, grandchild_pid,
        }
        membership = []
        for pid, created, handle in processes:
            in_windows_job = bool(win32job.IsProcessInJob(handle, None))
            membership.append({
                "pid": pid,
                "creation_time": created,
                "in_a_windows_job": in_windows_job,
            })
            assert in_windows_job, (
                f"test-owned descendant {pid} was not observed in a Windows Job"
            )
        record_property(
            "owned_job_descendant_job_membership",
            json.dumps(membership, sort_keys=True),
        )
        record_property(
            "owned_job_descendant_membership_scope",
            "IsProcessInJob with NULL job handle checks any Job; exact JTT Job membership is corroborated by confirmed empty Job accounting and retained descendant handles signaled after that session's Stop",
        )
        record_property(
            "owned_job_process_identities",
            json.dumps([
                {"pid": pid, "creation_time": created, "image_matches": True}
                for pid, created, _handle in processes
            ], sort_keys=True),
        )

        expected_prompt = _expected_prompt_path(work, 100)
        prompt_update = collect_until(
            lambda item, _output: item.screen is not None
            and any(line.strip() == expected_prompt
                    for line in _screen_text(item.screen).splitlines()),
            "PowerShell prompt after test-owned child launcher returns",
        )
        assert prompt_update.screen is not None
        tree_marker = f"{tree_marker_prefix}{child_pid},{grandchild_pid}_END"
        tree_report_command = (
            "$ids=[System.IO.File]::ReadAllText('" + marker_path + "'); "
            "[Console]::WriteLine('JTT_'+'OWNED_TREE_'+ '" + nonce
            + "' +'_'+$ids+'_END')"
        )
        assert tree_marker not in tree_report_command
        tree_report_input = (tree_report_command + "\r").encode("ascii")
        tree_report_written = client.send(tree_report_input)
        record_property(
            "owned_job_tree_report_input_acknowledged",
            tree_report_written == len(tree_report_input),
        )
        assert tree_report_written == len(tree_report_input)
        tree_update = collect_until(
            lambda _item, output: tree_marker.encode("ascii")
            in output.replace(b"\r", b"").replace(b"\n", b""),
            "ConPTY output containing test-owned process IDs",
        )
        assert tree_update.screen is not None
        assert tree_marker in _screen_text(tree_update.screen)

        # Keep a second JTT-owned session alive across the explicit Stop to
        # prove that cleanup is limited to the selected session's Job.
        preserved_client = registry.create("Unrelated owned Job preservation")
        preserved_identity = preserved_client.identity
        preserved_prompt = _expected_prompt_path(work, 100)
        preserved_initial = _wait(
            preserved_client,
            lambda update: update.screen is not None
            and update.state == "running"
            and preserved_prompt in _compact_screen_text(update.screen),
        )
        assert preserved_initial.screen is not None
        preserved_shell_handle = _retain_test_process_handle(
            preserved_identity.shell_pid,
            preserved_identity.shell_start,
            preserved_identity.shell_path,
        )
        for pid, created, handle in processes:
            assert not _retained_process_handle_signaled(handle), (
                f"test-owned Job descendant {pid} exited before Stop"
            )
        record_property("owned_job_children_alive_immediately_before_stop", len(processes))
        record_property("unrelated_jtt_session_running_before_stop", True)

        original_remove = windows_manager._remove_session_files

        def capture_stop_accounting(path, **kwargs):
            metadata = windows_manager._read_metadata(
                path / windows_manager.SESSION_META
            )
            stop_evidence["job_processes_empty"] = metadata.get(
                "job_processes_empty"
            )
            return original_remove(path, **kwargs)

        monkeypatch.setattr(
            windows_manager, "_remove_session_files", capture_stop_accounting
        )
        registry.stop(
            identity.session_id, expected_generation=identity.generation,
            expected_shell_pid=identity.shell_pid,
        )
        assert stop_evidence.get("job_processes_empty") is True, (
            "Stop did not preserve the worker's confirmed empty Job accounting state"
        )
        for pid, created, handle in processes:
            assert _retained_process_handle_signaled(handle), (
                f"owned Job descendant {pid} (created {created}) remained after explicit Stop"
            )
        assert not _retained_process_handle_signaled(preserved_shell_handle), (
            "explicit Stop terminated the unrelated test-owned JTT session"
        )
        preserved_after_stop = preserved_client.poll()
        assert (
            preserved_after_stop.state == "running"
            and preserved_after_stop.screen is not None
            and preserved_prompt in _compact_screen_text(preserved_after_stop.screen)
        ), "unrelated test-owned JTT session did not remain usable after Stop"
        _preserved_dir, preserved_metadata = windows_manager._validate_identity(
            registry.root, preserved_identity.session_id
        )
        current_preserved_identity = windows_manager._identity(preserved_metadata)
        assert (
            windows_manager._terminal_identity_key(current_preserved_identity)
            == windows_manager._terminal_identity_key(preserved_identity)
        ), "unrelated test-owned JTT session identity changed during Stop"
        assert not (registry.root / identity.session_id).exists()
        record_property("owned_job_active_processes_zero", True)
        record_property("owned_job_retained_child_handles_signaled", len(processes))
        record_property("unrelated_jtt_session_alive_after_stop", True)
        record_property("unrelated_jtt_session_identity_preserved", True)
    finally:
        record_property("owned_job_marker_file_present_at_teardown", marker_file.is_file())
        record_property("owned_job_process_handle_count_at_teardown", len(processes))
        record_property(
            "owned_job_stop_accounting_at_teardown",
            str(stop_evidence.get("job_processes_empty", "unknown")),
        )
        try:
            client.close()
        finally:
            try:
                if (registry.root / identity.session_id).exists():
                    registry.stop(
                        identity.session_id,
                        expected_generation=identity.generation,
                        expected_shell_pid=identity.shell_pid,
                    )
            finally:
                try:
                    if preserved_client is not None:
                        preserved_client.close()
                finally:
                    try:
                        if (preserved_identity is not None
                                and (registry.root / preserved_identity.session_id).exists()):
                            registry.stop(
                                preserved_identity.session_id,
                                expected_generation=preserved_identity.generation,
                                expected_shell_pid=preserved_identity.shell_pid,
                            )
                    finally:
                        for _pid, _created, handle in processes:
                            handle.Close()
                        if preserved_shell_handle is not None:
                            preserved_shell_handle.Close()

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


def _stop_cleanup_metadata(session_id, generation, *, state="exited",
                           termination="requested"):
    return {
        "protocol": 1,
        "session_id": session_id,
        "generation": generation,
        "supervisor_pid": 111,
        "supervisor_start": "worker-start",
        "shell_pid": 222,
        "shell_start": "shell-start",
        "shell_path": r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",
        "name": "synthetic stop cleanup",
        "state": state,
        "termination": termination,
        "job_processes_empty": state == "exited" and termination == "requested",
        "renderer_state": "ready",
    }


def _make_stop_cleanup_fixture(tmp_path, monkeypatch, metadata):
    import json
    from contextlib import nullcontext

    from tmux_agent_tower.terminal import windows_manager as runtime

    session_dir = tmp_path / metadata["session_id"]
    session_dir.mkdir()
    metadata_path = session_dir / runtime.SESSION_META
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    monkeypatch.setattr(runtime, "_session_metadata_lock",
                        lambda *_args, **_kwargs: nullcontext())
    monkeypatch.setattr(runtime, "_private_acl", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(runtime, "_is_reparse", lambda _path: False)
    monkeypatch.setattr(
        runtime, "_read_metadata",
        lambda path: json.loads(path.read_text(encoding="utf-8")),
    )
    return runtime, session_dir, metadata_path


def test_stop_cleanup_requires_exact_final_generation_and_requested_exit(
    tmp_path, monkeypatch
):
    from tmux_agent_tower.terminal import TerminalError

    session_id, generation = uuid.uuid4().hex, uuid.uuid4().hex
    changed_generation = uuid.uuid4().hex
    metadata = _stop_cleanup_metadata(session_id, changed_generation)
    runtime, session_dir, metadata_path = _make_stop_cleanup_fixture(
        tmp_path, monkeypatch, metadata
    )
    expected = runtime._identity(_stop_cleanup_metadata(session_id, generation))
    original = metadata_path.read_bytes()

    with pytest.raises(TerminalError, match="generation changed"):
        runtime._remove_session_files(
            session_dir, expected_session_id=session_id,
            expected_generation=generation, expected_identity=expected,
        )

    assert metadata_path.read_bytes() == original
    assert session_dir.is_dir()


def test_stop_cleanup_rejects_changed_shell_image_record(tmp_path, monkeypatch):
    from tmux_agent_tower.terminal import TerminalError
    from tmux_agent_tower.terminal import windows_manager as runtime

    session_id, generation = uuid.uuid4().hex, uuid.uuid4().hex
    metadata = _stop_cleanup_metadata(session_id, generation)
    expected = runtime._identity(metadata)
    metadata["shell_path"] = r"C:\Temp\unowned-shell.exe"
    runtime, session_dir, metadata_path = _make_stop_cleanup_fixture(
        tmp_path, monkeypatch, metadata
    )
    original = metadata_path.read_bytes()

    with pytest.raises(TerminalError, match="identity changed"):
        runtime._remove_session_files(
            session_dir, expected_session_id=session_id,
            expected_generation=generation, expected_identity=expected,
        )

    assert metadata_path.read_bytes() == original
    assert session_dir.is_dir()


@pytest.mark.parametrize(
    ("state", "termination"),
    [("running", "requested"), ("exited", "shell_exit"), ("stopping", "requested")],
)
def test_stop_cleanup_requires_confirmed_requested_exit(
    tmp_path, monkeypatch, state, termination
):
    from tmux_agent_tower.terminal import TerminalError
    from tmux_agent_tower.terminal import windows_manager as runtime

    session_id, generation = uuid.uuid4().hex, uuid.uuid4().hex
    expected = runtime._identity(_stop_cleanup_metadata(session_id, generation))
    metadata = _stop_cleanup_metadata(
        session_id, generation, state=state, termination=termination
    )
    runtime, session_dir, metadata_path = _make_stop_cleanup_fixture(
        tmp_path, monkeypatch, metadata
    )
    original = metadata_path.read_bytes()

    with pytest.raises(TerminalError, match="stop was not confirmed"):
        runtime._remove_session_files(
            session_dir, expected_session_id=session_id,
            expected_generation=generation, expected_identity=expected,
        )

    assert metadata_path.read_bytes() == original
    assert session_dir.is_dir()


def test_stop_cleanup_preflights_all_artifacts_before_deletion(tmp_path, monkeypatch):
    from tmux_agent_tower.terminal import TerminalError
    from tmux_agent_tower.terminal import windows_manager as runtime

    session_id, generation = uuid.uuid4().hex, uuid.uuid4().hex
    metadata = _stop_cleanup_metadata(session_id, generation)
    runtime, session_dir, metadata_path = _make_stop_cleanup_fixture(
        tmp_path, monkeypatch, metadata
    )
    expected = runtime._identity(metadata)
    unexpected = session_dir / "unclassified-evidence.bin"
    unexpected.write_bytes(b"keep")
    original = metadata_path.read_bytes()

    with pytest.raises(TerminalError, match="unexpected terminal evidence"):
        runtime._remove_session_files(
            session_dir, expected_session_id=session_id,
            expected_generation=generation, expected_identity=expected,
        )

    assert metadata_path.read_bytes() == original
    assert unexpected.read_bytes() == b"keep"


def test_stop_cleanup_removes_only_the_confirmed_session_record(tmp_path, monkeypatch):
    from tmux_agent_tower.terminal import windows_manager as runtime

    session_id, generation = uuid.uuid4().hex, uuid.uuid4().hex
    metadata = _stop_cleanup_metadata(session_id, generation)
    runtime, session_dir, metadata_path = _make_stop_cleanup_fixture(
        tmp_path, monkeypatch, metadata
    )
    expected = runtime._identity(metadata)

    runtime._remove_session_files(
        session_dir, expected_session_id=session_id,
        expected_generation=generation, expected_identity=expected,
    )

    assert not metadata_path.exists()
    assert not session_dir.exists()
    assert not tuple(tmp_path.glob(".stop-evidence-*.json"))


def test_stop_cleanup_restores_metadata_if_directory_removal_fails(
    tmp_path, monkeypatch
):
    from tmux_agent_tower.terminal import TerminalError
    from tmux_agent_tower.terminal import windows_manager as runtime

    session_id, generation = uuid.uuid4().hex, uuid.uuid4().hex
    metadata = _stop_cleanup_metadata(session_id, generation)
    runtime, session_dir, metadata_path = _make_stop_cleanup_fixture(
        tmp_path, monkeypatch, metadata
    )
    expected = runtime._identity(metadata)
    original = metadata_path.read_bytes()
    original_rmdir = type(session_dir).rmdir

    def fail_session_rmdir(path):
        if path == session_dir:
            raise OSError("injected directory removal failure")
        return original_rmdir(path)

    monkeypatch.setattr(type(session_dir), "rmdir", fail_session_rmdir)
    with pytest.raises(TerminalError, match="registry evidence restored"):
        runtime._remove_session_files(
            session_dir, expected_session_id=session_id,
            expected_generation=generation, expected_identity=expected,
        )

    assert session_dir.is_dir()
    assert metadata_path.read_bytes() == original
    assert tuple(entry.name for entry in session_dir.iterdir()) == (runtime.SESSION_META,)
    assert not tuple(tmp_path.glob(".stop-evidence-*.json"))


def test_stop_cleanup_retains_external_evidence_if_restore_fails(
    tmp_path, monkeypatch
):
    from tmux_agent_tower.terminal import TerminalError
    from tmux_agent_tower.terminal import windows_manager as runtime

    session_id, generation = uuid.uuid4().hex, uuid.uuid4().hex
    metadata = _stop_cleanup_metadata(session_id, generation)
    runtime, session_dir, metadata_path = _make_stop_cleanup_fixture(
        tmp_path, monkeypatch, metadata
    )
    expected = runtime._identity(metadata)
    original = metadata_path.read_bytes()

    def fail_rmdir(path):
        if path == session_dir:
            raise OSError("injected directory removal failure")
        raise AssertionError("unexpected rmdir target")

    def fail_restore(*_args):
        raise OSError("injected restore failure")

    monkeypatch.setattr(type(session_dir), "rmdir", fail_rmdir)
    monkeypatch.setattr(runtime, "_restore_metadata_evidence", fail_restore)
    with pytest.raises(TerminalError, match="stop evidence backup was retained"):
        runtime._remove_session_files(
            session_dir, expected_session_id=session_id,
            expected_generation=generation, expected_identity=expected,
        )

    backups = tuple(tmp_path.glob(f".stop-evidence-{session_id}-{generation}.json"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == original
    assert not metadata_path.exists()
    assert session_dir.is_dir()


def test_atomic_session_metadata_rejects_a_replaced_generation(tmp_path, monkeypatch):
    from contextlib import nullcontext

    from tmux_agent_tower.terminal import TerminalError
    from tmux_agent_tower.terminal import windows_manager as runtime

    session_id, expected_generation = uuid.uuid4().hex, uuid.uuid4().hex
    replacement_generation = uuid.uuid4().hex
    session_dir = tmp_path / session_id
    session_dir.mkdir()
    metadata_path = session_dir / runtime.SESSION_META
    metadata_path.write_text("existing record", encoding="utf-8")
    value = _stop_cleanup_metadata(session_id, expected_generation)
    writes = []
    monkeypatch.setattr(runtime, "_session_metadata_lock",
                        lambda *_args, **_kwargs: nullcontext())
    monkeypatch.setattr(
        runtime, "_read_metadata",
        lambda _path: _stop_cleanup_metadata(session_id, replacement_generation),
    )
    monkeypatch.setattr(runtime, "_write_metadata_unlocked",
                        lambda *_args: writes.append("write"))

    with pytest.raises(TerminalError, match="generation changed"):
        runtime._atomic_metadata(metadata_path, value)

    assert writes == []


def test_identity_validation_stops_before_shell_query_when_worker_mismatches(
    tmp_path, monkeypatch
):
    import os
    from pathlib import Path

    from tmux_agent_tower.terminal import TerminalError
    from tmux_agent_tower.terminal import windows_manager as runtime

    session_id, generation = uuid.uuid4().hex, uuid.uuid4().hex
    metadata = _stop_cleanup_metadata(session_id, generation, state="running")
    calls = []
    monkeypatch.setattr(runtime, "_ensure_private_directory", lambda *_args: None)
    monkeypatch.setattr(runtime, "_private_acl", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(runtime, "_read_metadata", lambda _path: metadata)
    monkeypatch.setattr(runtime, "_worker_executable", lambda: Path(r"C:\JTT\python.exe"))

    def worker_mismatch(pid, start, image=None):
        calls.append((pid, start, image))
        return "creation_time_mismatch"

    monkeypatch.setattr(runtime, "_process_identity_mismatch", worker_mismatch)
    with pytest.raises(TerminalError, match="supervisor identity is stale"):
        runtime._validate_identity(tmp_path, session_id)

    assert calls == [(
        metadata["supervisor_pid"], metadata["supervisor_start"],
        os.path.normcase(os.path.abspath(r"C:\JTT\python.exe")),
    )]


def test_identity_validation_checks_the_live_shell_image(tmp_path, monkeypatch):
    import os
    from pathlib import Path

    from tmux_agent_tower.terminal import TerminalError
    from tmux_agent_tower.terminal import windows_manager as runtime

    session_id, generation = uuid.uuid4().hex, uuid.uuid4().hex
    metadata = _stop_cleanup_metadata(session_id, generation, state="running")
    shell_path = metadata["shell_path"]
    calls = []
    monkeypatch.setattr(runtime, "_ensure_private_directory", lambda *_args: None)
    monkeypatch.setattr(runtime, "_private_acl", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(runtime, "_read_metadata", lambda _path: metadata)
    monkeypatch.setattr(runtime, "_worker_executable", lambda: Path(r"C:\JTT\python.exe"))
    monkeypatch.setattr(runtime, "_powershell_executable", lambda: Path(shell_path))

    def image_mismatch(pid, start, image=None):
        calls.append((pid, start, image))
        return None if len(calls) == 1 else "image_path_mismatch"

    monkeypatch.setattr(runtime, "_process_identity_mismatch", image_mismatch)
    with pytest.raises(TerminalError, match="shell identity is stale"):
        runtime._validate_identity(tmp_path, session_id)

    assert len(calls) == 2
    assert calls[1] == (
        metadata["shell_pid"], metadata["shell_start"],
        os.path.normcase(os.path.abspath(shell_path)),
    )


def test_exited_identity_rejects_same_process_with_unexpected_image(tmp_path, monkeypatch):
    from pathlib import Path

    from tmux_agent_tower.terminal import TerminalError
    from tmux_agent_tower.terminal import windows_manager as runtime

    session_id, generation = uuid.uuid4().hex, uuid.uuid4().hex
    metadata = _stop_cleanup_metadata(session_id, generation, state="exited")
    calls = []
    monkeypatch.setattr(runtime, "_ensure_private_directory", lambda *_args: None)
    monkeypatch.setattr(runtime, "_private_acl", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(runtime, "_read_metadata", lambda _path: metadata)
    monkeypatch.setattr(runtime, "_worker_executable", lambda: Path(r"C:\JTT\python.exe"))
    monkeypatch.setattr(
        runtime, "_powershell_executable", lambda: Path(metadata["shell_path"])
    )

    def unexpected_image(pid, start, image=None):
        calls.append((pid, start, image))
        return None if len(calls) == 1 else "image_path_mismatch"

    monkeypatch.setattr(runtime, "_process_identity_mismatch", unexpected_image)
    with pytest.raises(TerminalError, match="shell identity is inconsistent"):
        runtime._validate_identity(tmp_path, session_id)

    assert len(calls) == 2


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


@pytest.mark.skipif(sys.platform != "win32", reason="native ConPTY required")
def test_native_conpty_input_execution_path_comparison(
        tmp_path, monkeypatch, record_property):
    """Compare steady-state commands in one direct and one JTT-owned shell."""
    import ju0_terminal_tower  # noqa: F401 - installs the guarded namespace
    import pyte
    import win32api
    import win32con
    import win32event
    from winpty import Backend, PTY
    from tmux_agent_tower.terminal import TerminalRegistry, windows_manager

    root = tmp_path / "jtt-conpty-input-rootcause"
    start = root / "work"
    destination = start / "destination"
    start.mkdir(parents=True)
    destination.mkdir()
    monkeypatch.chdir(start)
    nonce = uuid.uuid4().hex[:10]
    direct_home = root / "direct-home"
    worker_home = root / "worker-home"
    direct_home.mkdir()
    worker_home.mkdir()

    def ps_quote(value):
        return str(value).replace("'", "''")

    def case_specs(label, home):
        marker = f"JTT_EXEC_{label}_{nonce}_END"
        marker_file = start / f"{label.lower()}-{home.name}.marker"
        marker_path = ps_quote(
            marker_file if label == "PATH_CHANGE" else marker_file.name
        )
        marker_value = nonce
        if label == "SHORT_ASCII":
            commands = [(
                "[System.IO.File]::WriteAllText('" + marker_path + "','"
                + nonce + "'); [Console]::WriteLine('JTT_EXEC_'+'SHORT_ASCII_'+"
                + "'" + nonce + "'+'_END')",
                marker,
                marker_value,
            )]
        elif label == "LONG_ASCII":
            commands = [(
                "$__jtt_padding='" + ("x" * 640) + "'; "
                + "[System.IO.File]::WriteAllText('" + marker_path + "','"
                + nonce + "'); [Console]::WriteLine('JTT_EXEC_'+'LONG_ASCII_'+"
                + "'" + nonce + "'+'_END')",
                marker,
                marker_value,
            )]
        elif label == "MULTI_STEP":
            first_marker = f"JTT_MULTI_STEP1_{nonce}_END"
            final_marker = f"JTT_MULTI_STEP2_{nonce}_END"
            first_file = start / f"multi-step1-{home.name}.marker"
            final_file = start / f"multi-step2-{home.name}.marker"
            commands = [
                (
                    "$global:JTTInputDiag='" + nonce + "'; "
                    "[System.IO.File]::WriteAllText('"
                    + ps_quote(first_file.name) + "','step1|" + nonce + "'); "
                    "[Console]::WriteLine('JTT_MULTI_STEP1_'+ '" + nonce + "'+'_END')",
                    first_marker,
                    "step1|" + nonce,
                ),
                (
                    "if ($global:JTTInputDiag -ne '" + nonce
                    + "') { throw 'JTT diagnostic state mismatch' }; "
                    "[System.IO.File]::WriteAllText('"
                    + ps_quote(final_file.name) + "','step2|" + nonce + "'); "
                    "[Console]::WriteLine('JTT_MULTI_STEP2_'+ '" + nonce + "'+'_END')",
                    final_marker,
                    "step2|" + nonce,
                ),
            ]
        elif label == "UNICODE_INPUT":
            commands = [(
                "Write-Output '한글'; [System.IO.File]::WriteAllText('"
                + marker_path + "','" + nonce
                + "'); [Console]::WriteLine('JTT_EXEC_'+'UNICODE_INPUT_'+"
                + "'" + nonce + "'+'_END')",
                marker,
                marker_value,
            )]
        elif label == "PATH_CHANGE":
            expected_path = str(destination)
            commands = [(
                "Set-Location '" + ps_quote(destination) + "'; "
                "$__jtt_actual=$PWD.Path; "
                "[System.IO.File]::WriteAllText('" + marker_path
                + "',$__jtt_actual); "
                "[Console]::WriteLine('JTT_EXEC_'+'PATH_CHANGE_'+"
                + "'" + nonce + "'+'_END')",
                marker,
                expected_path,
            )]
        else:
            raise AssertionError("unknown ConPTY comparison case")
        assert all(expected not in command for command, expected, _value in commands)
        return commands

    def compact(value):
        return value.replace("\r", "").replace("\n", "")

    def prompt_seen(text):
        return any(
            line.lstrip().startswith("PS ") and ">" in line
            for line in text.splitlines()
        )

    def powershell_error_categories(text):
        return [
            token for token in (
                "ParserError", "FullyQualifiedErrorId", "Write-Error",
                "Set-Location :", "Start-Process :", "Cannot find path",
                "JTT diagnostic state mismatch",
            )
            if token in text
        ]

    def read_marker(marker_file):
        try:
            return marker_file.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError):
            return None

    def run_path(*, direct):
        path_name = "direct_pywinpty" if direct else "jtt_worker"
        result = {
            "path": path_name,
            "status": "NOT_STARTED",
            "launch": {
                "executable": str(windows_manager._powershell_executable()),
                "initial_size": [100, 30],
                "working_directory_matches": True,
                "direct_arguments": (
                    ["-NoLogo", "-NoProfile", "-NoExit"] if direct else None
                ),
                "worker_arguments": (
                    ["-NoLogo", "-NoProfile", "-NoExit",
                     "-Command", "<product bootstrap gate>"]
                    if not direct else None
                ),
                "environment_profile": (
                    "same worker shell allowlist from _worker_environment; "
                    "JTT_HOME is a distinct test-owned root per path"
                ),
                "comparison_scope": (
                    "initial prompt observed separately; compare only command "
                    "execution after each path startup"
                ),
                "environment_values_logged": False,
            },
            "cases": [],
            "cleanup": "NOT_ATTEMPTED",
            "cleanup_confirmed": False,
        }
        pty = client = registry = process_handle = None
        identity = None
        direct_raw_parts = []
        worker_raw_bytes = bytearray()
        direct_screen = pyte.Screen(100, 30) if direct else None
        direct_stream = pyte.Stream(direct_screen) if direct else None
        latest = None

        def observe():
            nonlocal latest
            if direct:
                result["last_observation_stage"] = "direct_pty_read"
                text = pty.read(blocking=False)
                if text is None:
                    text = ""
                if not isinstance(text, str):
                    raise TypeError("pywinpty.PTY.read did not return str")
                if text:
                    direct_raw_parts.append(text)
                    result["last_observation_stage"] = "direct_pyte_feed"
                    direct_stream.feed(text)
                result["last_observation_stage"] = "direct_pyte_screen_render"
                screen_text = "\n".join(
                    "".join(direct_screen.buffer[y][x].data
                            for x in range(direct_screen.columns))
                    for y in range(direct_screen.lines)
                )
                raw_text = "".join(direct_raw_parts)
                latest = None
            else:
                result["last_observation_stage"] = "worker_poll"
                latest = client.poll()
                worker_raw_bytes.extend(latest.output)
                raw_text = worker_raw_bytes.decode("utf-8", errors="replace")
                screen_text = (
                    _compact_screen_text(latest.screen)
                    if latest.screen is not None else ""
                )
            return raw_text, screen_text

        def wait_for(predicate, timeout):
            deadline = time.monotonic() + timeout
            raw_text = screen_text = ""
            while time.monotonic() < deadline:
                raw_text, screen_text = observe()
                if predicate(raw_text, screen_text):
                    return raw_text, screen_text
                time.sleep(0.02)
            return raw_text, screen_text

        try:
            result["status"] = "STARTING"
            if direct:
                session_dir = direct_home / "terminals" / ("direct-" + nonce)
                template_env = windows_manager._worker_environment(session_dir)
                shell_keys = (
                    "SystemRoot", "WINDIR", "PATH", "LOCALAPPDATA", "APPDATA",
                    "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "TEMP", "TMP",
                )
                shell_env = {
                    key: template_env[key] for key in shell_keys
                    if key in template_env
                }
                shell_env["JTT_HOME"] = str(direct_home)
                shell_env["TERM"] = "xterm"
                worker_env_keys = set(shell_env)
                result["launch"]["environment_keys_match_worker_shell_contract"] = (
                    worker_env_keys
                    == set(shell_keys).intersection(template_env)
                    | {"JTT_HOME", "TERM"}
                )
                result["launch"]["jtt_home_is_test_owned"] = (
                    shell_env["JTT_HOME"] == str(direct_home)
                )
                assert result["launch"][
                    "environment_keys_match_worker_shell_contract"
                ]
                assert result["launch"]["jtt_home_is_test_owned"]
                pty = PTY(100, 30, backend=Backend.ConPTY)
                result["last_observation_stage"] = "direct_spawn"
                spawned = pty.spawn(
                    str(windows_manager._powershell_executable()),
                    cmdline=subprocess.list2cmdline(
                        ["-NoLogo", "-NoProfile", "-NoExit"]
                    ),
                    cwd=str(start),
                    env="\0".join(
                        f"{key}={value}" for key, value in shell_env.items()
                    ) + "\0",
                )
                result["spawn_returned_true"] = bool(spawned)
                pid_value = pty.pid
                result["pid_present"] = pid_value is not None
                if pid_value is None:
                    result["status"] = (
                        "SPAWN_FAILED" if not spawned else "IDENTITY_UNKNOWN"
                    )
                    result["cleanup"] = (
                        "PROCESS_NOT_STARTED" if not spawned
                        else "OWNERSHIP_UNKNOWN"
                    )
                    result["cleanup_confirmed"] = not spawned
                    raise _ConptyPathAbort()
                pid = int(pid_value)
                created, image, alive = windows_manager._process_facts(pid)
                process_handle = _retain_test_process_handle(
                    pid, created, windows_manager._powershell_executable(),
                    allow_terminate=True,
                )
                result["process_identity_verified"] = bool(
                    created and alive
                    and image == os.path.normcase(os.path.abspath(
                        str(windows_manager._powershell_executable())
                    ))
                )
                result["test_owned_pid"] = pid
                result["creation_time_recorded"] = bool(created)
                result["alive_before_input"] = bool(alive)
                if not spawned or not result["process_identity_verified"]:
                    result["status"] = "SPAWN_OR_IDENTITY_FAILURE"
                    raise _ConptyPathAbort()
            else:
                registry = TerminalRegistry(worker_home / "terminals")
                result["last_observation_stage"] = "worker_registry_create"
                client = registry.create("isolated ConPTY execution comparison")
                identity = client.identity
                process_handle = _retain_test_process_handle(
                    identity.shell_pid, identity.shell_start, identity.shell_path
                )
                result["process_identity_verified"] = True
                result["test_owned_pid"] = identity.shell_pid
                result["session_id"] = identity.session_id
                result["generation"] = identity.generation
                result["creation_time_recorded"] = bool(identity.shell_start)
                session_dir, metadata = windows_manager._validate_identity(
                    registry.root, identity.session_id
                )
                result["jtt_home_is_test_owned"] = (
                    identity.state == "running"
                    and session_dir.parent.parent.resolve() == worker_home.resolve()
                )
                result["launch"]["working_directory_matches"] = (
                    os.path.normcase(os.path.abspath(
                        metadata.get("working_directory", "")
                    )) == os.path.normcase(os.path.abspath(str(start)))
                )
                if not result["jtt_home_is_test_owned"]:
                    raise AssertionError("worker session root is outside test home")
                if not result["launch"]["working_directory_matches"]:
                    raise AssertionError("worker working directory differs from direct path")
                result["launch"]["environment_keys_match_worker_shell_contract"] = (
                    True  # worker builds this allow-listed shell environment
                )
            result["status"] = "RUNNING"

            startup_deadline = time.monotonic() + 5
            startup_raw = ""
            while time.monotonic() < startup_deadline:
                startup_raw, _startup_screen = observe()
                if prompt_seen(startup_raw):
                    break
                if not direct and latest is not None and latest.state != "running":
                    break
                time.sleep(0.02)
            result["initial_prompt_seen"] = prompt_seen(startup_raw)
            result["worker_state_before_input"] = (
                latest.state if not direct and latest is not None else None
            )
            result["renderer_state_before_input"] = (
                getattr(latest, "renderer_state", None)
                if not direct and latest is not None else None
            )
            if not direct:
                if latest is None or latest.state != "running":
                    result["status"] = "WORKER_NOT_RUNNING"
                    raise _ConptyPathAbort()
                if not client.acquire():
                    result["status"] = "LEASE_NOT_ACQUIRED"
                    raise _ConptyPathAbort()

            baseline_proven = False
            stop_cases = False
            for label in (
                "SHORT_ASCII", "LONG_ASCII", "MULTI_STEP",
                "UNICODE_INPUT", "PATH_CHANGE",
            ):
                if stop_cases:
                    result["cases"].append({
                        "case": label,
                        "status": "NOT_RUN_PREVIOUS_EXECUTION_UNPROVEN",
                    })
                    continue
                steps = case_specs(label, direct_home if direct else worker_home)
                case_result = {"case": label, "steps": []}
                result["cases"].append(case_result)
                for step_index, (command, expected, expected_file_value) in enumerate(steps):
                    marker_file = start / (
                        f"{label.lower()}-{(direct_home if direct else worker_home).name}.marker"
                        if label != "MULTI_STEP"
                        else (
                            f"multi-step{step_index + 1}-"
                            f"{(direct_home if direct else worker_home).name}.marker"
                        )
                    )
                    try:
                        marker_file.unlink(missing_ok=True)
                    except OSError as exc:
                        case_result["status"] = "TEST_MARKER_PREPARATION_ERROR"
                        case_result["marker_prepare_error_category"] = type(exc).__name__
                        stop_cases = True
                        break
                    step = {
                        "step": step_index + 1,
                        "input_ack": None,
                        "command_echo_seen": False,
                        "side_effect_file_present": False,
                        "side_effect_file_matches": False,
                        "output_marker_in_pty_or_poll": False,
                        "output_marker_in_pyte_or_worker_screen": False,
                        "powershell_error_categories": [],
                    }
                    case_result["steps"].append(step)
                    result["last_observation_stage"] = "pre_input_drain"
                    raw_before, _screen_before = observe()
                    raw_offset = len(raw_before)
                    worker_byte_offset = len(worker_raw_bytes)
                    sequence_before = (
                        getattr(latest, "sequence", None)
                        if not direct else None
                    )
                    payload = command + "\r"
                    result["last_observation_stage"] = (
                        "direct_pty_write" if direct else "worker_client_send"
                    )
                    if direct:
                        written = pty.write(payload)
                        ack = {
                            "reported_return": written,
                            "utf8_payload_bytes": len(payload.encode("utf-8")),
                            "python_characters": len(payload),
                            "equals_byte_count": (
                                type(written) is int
                                and written == len(payload.encode("utf-8"))
                            ),
                            "equals_character_count": (
                                type(written) is int and written == len(payload)
                            ),
                            "api": "PTY.write(str)",
                        }
                    else:
                        written = client.send(payload.encode("utf-8"))
                        ack = {
                            "reported_return": written,
                            "utf8_payload_bytes": len(payload.encode("utf-8")),
                            "python_characters": len(payload),
                            "equals_byte_count": (
                                type(written) is int
                                and written == len(payload.encode("utf-8"))
                            ),
                            "api": "TerminalClient.send(utf8 bytes)",
                        }
                    step["input_ack"] = ack

                    def complete(raw_text, screen_text):
                        value = read_marker(marker_file)
                        raw_delta = raw_text[raw_offset:]
                        raw_marker = expected in compact(raw_delta)
                        screen_marker = expected in compact(screen_text)
                        step["command_echo_seen"] = (
                            command in compact(raw_delta)
                            or command in compact(screen_text)
                        )
                        step["side_effect_file_present"] = value is not None
                        step["side_effect_file_matches"] = (
                            value == expected_file_value
                            if label != "PATH_CHANGE"
                            else bool(value) and os.path.normcase(value)
                            == os.path.normcase(expected_file_value)
                        )
                        step["output_marker_in_pty_or_poll"] = raw_marker
                        step["output_marker_in_pyte_or_worker_screen"] = screen_marker
                        step["powershell_error_categories"] = (
                            powershell_error_categories(raw_delta)
                        )
                        if label == "UNICODE_INPUT":
                            raw_unicode_count = raw_delta.count("한글")
                            screen_unicode_count = screen_text.count("한글")
                            step["unicode_output_raw_occurrences"] = raw_unicode_count
                            step["unicode_output_screen_occurrences"] = screen_unicode_count
                        return (
                            step["side_effect_file_matches"]
                            and raw_marker and screen_marker
                            and (
                                label != "UNICODE_INPUT"
                                or step.get("unicode_output_raw_occurrences", 0) >= 2
                            )
                        )

                    result["last_observation_stage"] = "observe_input_echo_and_execution"
                    raw_text, screen_text = wait_for(
                        complete, 8
                    )
                    complete(raw_text, screen_text)
                    step["terminal_state"] = (
                        getattr(latest, "state", None) if not direct else "direct"
                    )
                    step["renderer_state"] = (
                        getattr(latest, "renderer_state", None)
                        if not direct else "pyte"
                    )
                    step["output_sequence"] = (
                        getattr(latest, "sequence", None) if not direct else None
                    )
                    step["snapshot_sequence"] = (
                        getattr(latest, "snapshot_sequence", None)
                        if not direct else None
                    )
                    step["output_gap"] = (
                        getattr(latest, "gap", None) if not direct else None
                    )
                    step["sequence_before"] = sequence_before
                    step["sequence_after"] = (
                        getattr(latest, "sequence", None)
                        if not direct else None
                    )
                    step["pty_read_text_characters"] = (
                        len(raw_text) - raw_offset if direct else None
                    )
                    step["worker_poll_payload_bytes"] = (
                        len(worker_raw_bytes) - worker_byte_offset
                        if not direct else None
                    )
                    step["output_measurement"] = (
                        "PTY.read returned Python str; not native ConPTY bytes"
                        if direct else "TerminalClient.poll output payload bytes"
                    )
                    if direct and process_handle is not None:
                        try:
                            process_wait = win32event.WaitForSingleObject(
                                process_handle, 0
                            )
                            step["process_state_after_input"] = (
                                "alive" if process_wait == win32con.WAIT_TIMEOUT
                                else "exited"
                                if process_wait == win32con.WAIT_OBJECT_0
                                else "unknown"
                            )
                        except Exception as exc:
                            step["process_state_after_input"] = "unknown"
                            step["process_state_error_category"] = type(exc).__name__
                    else:
                        step["process_state_after_input"] = (
                            getattr(latest, "state", None) if latest is not None
                            else "unknown"
                        )
                    step["status"] = (
                        "EXECUTION_PROVEN" if complete(raw_text, screen_text)
                        else "EXECUTION_UNPROVEN"
                    )
                    if label == "SHORT_ASCII" and step_index == 0:
                        baseline_proven = step["status"] == "EXECUTION_PROVEN"
                        result["short_ascii_execution_proven"] = baseline_proven
                    if step["status"] != "EXECUTION_PROVEN":
                        case_result["status"] = "EXECUTION_UNPROVEN"
                        stop_cases = True
                        break
                else:
                    case_result["status"] = "EXECUTION_PROVEN"
            result["status"] = (
                "DIAGNOSTIC_TOOL_ERROR" if result.get("diagnostic_error_stage")
                else "COMPLETE" if not stop_cases
                else "EXECUTION_UNPROVEN"
            )
        except _ConptyPathAbort:
            # Preserve the status set at the abort point while still running
            # the owned-resource cleanup block below.
            pass
        except Exception as exc:
            result["status"] = "DIAGNOSTIC_TOOL_ERROR" if direct else "PATH_ERROR"
            result["diagnostic_error_stage"] = result.get(
                "last_observation_stage", "setup_or_command"
            )
            result["diagnostic_error_category"] = type(exc).__name__
        finally:
            if direct and process_handle is not None:
                try:
                    wait_status = win32event.WaitForSingleObject(process_handle, 0)
                    if wait_status == win32con.WAIT_TIMEOUT:
                        result["cleanup_request"] = "retained_test_process_handle"
                        win32api.TerminateProcess(process_handle, 0)
                        wait_status = win32event.WaitForSingleObject(
                            process_handle, 5000
                        )
                    result["cleanup_confirmed"] = (
                        wait_status == win32con.WAIT_OBJECT_0
                    )
                    result["cleanup"] = (
                        "TERMINATED_OR_EXITED_ON_RETAINED_HANDLE"
                        if result["cleanup_confirmed"] else "EXIT_UNCONFIRMED"
                    )
                except Exception as exc:
                    result["cleanup"] = "CLEANUP_ERROR"
                    result["cleanup_error_category"] = type(exc).__name__
                finally:
                    try:
                        process_handle.Close()
                    except Exception as exc:
                        result["cleanup_handle_close_error_category"] = type(exc).__name__
                    process_handle = None
                    # pywinpty 3.1.1 exposes no PTY.close() method. Release the
                    # wrapper only after the retained process handle is checked.
                    pty = None
                    result["pty_wrapper_release"] = "NO_CLOSE_API_USED"
            elif direct and pty is None:
                result["cleanup_confirmed"] = (
                    result["cleanup"] == "PROCESS_NOT_STARTED"
                )
            elif not direct:
                if client is not None:
                    try:
                        client.close()
                    except Exception as exc:
                        result["client_close_error_category"] = type(exc).__name__
                if registry is not None and identity is not None:
                    try:
                        registry.stop(
                            identity.session_id,
                            expected_generation=identity.generation,
                            expected_shell_pid=identity.shell_pid,
                        )
                        result["cleanup"] = "REGISTRY_STOP_COMPLETED"
                    except Exception as exc:
                        result["cleanup"] = "STOP_FAILED_EVIDENCE_RETAINED"
                        result["cleanup_error_category"] = type(exc).__name__
                if process_handle is not None:
                    try:
                        result["retained_handle_signaled_after_cleanup"] = (
                            win32event.WaitForSingleObject(process_handle, 0)
                            == win32con.WAIT_OBJECT_0
                        )
                        result["cleanup_confirmed"] = bool(
                            result["retained_handle_signaled_after_cleanup"]
                            and result["cleanup"] == "REGISTRY_STOP_COMPLETED"
                        )
                    except Exception as exc:
                        result["cleanup_confirmed"] = False
                        result["cleanup_error_category"] = type(exc).__name__
                    finally:
                        try:
                            process_handle.Close()
                        except Exception as exc:
                            result["cleanup_handle_close_error_category"] = type(exc).__name__

        return _finalize_conpty_path_result(result)

    direct = run_path(direct=True)
    direct_cleanup_confirmed = _conpty_path_allows_followup(direct)
    if direct_cleanup_confirmed:
        worker = run_path(direct=False)
    else:
        worker = {
            "path": "jtt_worker",
            "status": "NOT_RUN_DIRECT_CLEANUP_UNCONFIRMED",
            "cleanup_confirmed": False,
            "cases": [],
        }
    evidence = {"direct": direct, "worker": worker}
    record_property(
        "conpty_input_execution_comparison",
        json.dumps(evidence, ensure_ascii=True, sort_keys=True),
    )
    print("CONPTY_INPUT_EXECUTION_COMPARISON "
          + json.dumps(evidence, ensure_ascii=True, sort_keys=True))

    worker_cases = {
        item.get("case"): item for item in worker.get("cases", [])
        if item.get("case")
    }
    worker_short = worker_cases.get("SHORT_ASCII", {})
    worker_short_steps = worker_short.get("steps", [])
    assert (
        worker_short.get("status") == "EXECUTION_PROVEN"
        and worker_short_steps
        and worker_short_steps[0].get("status") == "EXECUTION_PROVEN"
    ), "PRODUCT_WORKER_SHORT_ASCII_EXECUTION_UNPROVEN; see structured evidence"
    for label in (
        "LONG_ASCII", "MULTI_STEP", "UNICODE_INPUT", "PATH_CHANGE",
    ):
        case = worker_cases.get(label, {})
        assert case.get("status") == "EXECUTION_PROVEN", (
            f"PRODUCT_WORKER_{label}_EXECUTION_UNPROVEN; see structured evidence"
        )
    if direct.get("status") == "DIAGNOSTIC_TOOL_ERROR":
        raise AssertionError(
            "DIAGNOSTIC_TOOL_ERROR at "
            f"{direct.get('diagnostic_error_stage')}: "
            f"{direct.get('diagnostic_error_category')}"
        )
    direct_cases = {
        item.get("case"): item for item in direct.get("cases", [])
        if item.get("case")
    }
    assert direct_cases.get("SHORT_ASCII", {}).get("status") == "EXECUTION_PROVEN", (
        "DIRECT_REFERENCE_SHORT_ASCII_EXECUTION_UNPROVEN; see structured evidence"
    )
    for label in (
        "LONG_ASCII", "MULTI_STEP", "UNICODE_INPUT", "PATH_CHANGE",
    ):
        assert direct_cases.get(label, {}).get("status") == "EXECUTION_PROVEN", (
            f"DIRECT_REFERENCE_{label}_EXECUTION_UNPROVEN; see structured evidence"
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
    """Select an explicit private worker copy and pass only test-owned env values."""
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
    environment_anchor = '        shell_env["TERM"] = "xterm"\n'
    environment_forward = environment_anchor + '''        for _r02_key in (
            "JTT_R02_N", "JTT_R02_MARKER",
        ):
            if _r02_key in os.environ:
                shell_env[_r02_key] = os.environ[_r02_key]
'''
    if source.count(environment_anchor) != 1:
        raise AssertionError("R02 diagnostic environment anchor changed")
    source = source.replace(environment_anchor, environment_forward, 1)
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
        "expected_start_matches": created == identity.shell_start,
        "alive": bool(alive and created == identity.shell_start),
        "image_matches": image.casefold() == identity.shell_path.casefold(),
    }


def _r02_attach_client_trace(client, manager, roots):
    """Trace only test-owned state categories, sequence, and monotonic time."""
    trace = {
        "send_calls": [],
        "line_submissions": [],
        "polls": [],
        "command_echo_seen": False,
        "powershell_error_seen": False,
        "last_poll": None,
    }
    original_send = client.send
    original_poll = client.poll

    def traced_send(payload):
        record = {
            "monotonic_ns": time.monotonic_ns(),
            "byte_count": len(payload),
            "returned": False,
            "acknowledged_bytes": None,
            "error_category": None,
        }
        is_terminator = payload in (b"\r", b"\n", b"\r\n")
        record["phase"] = "line_terminator" if is_terminator else "input"
        try:
            acknowledged = original_send(payload)
            record.update({
                "returned": True,
                "acknowledged_bytes": int(acknowledged),
            })
            return acknowledged
        except Exception as exc:
            record["error_category"] = type(exc).__name__
            raise
        finally:
            record["monotonic_ns"] = time.monotonic_ns()
            if is_terminator:
                trace["line_submissions"].append(dict(record))
            trace["send_calls"].append(dict(record))

    def traced_poll():
        update = original_poll()
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
            trace["powershell_error_seen"] = True
        trace["last_poll"] = _r02_poll_record(update)
        if (update.output or not trace["polls"]
                or trace["polls"][-1]["sequence"] != update.sequence
                or trace["polls"][-1]["snapshot_sequence"]
                != update.snapshot_sequence):
            trace["polls"].append(_r02_poll_record(update))
        return update

    client.send = traced_send
    client.poll = traced_poll
    return trace


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


def _r02_poll_record(update):
    return {
        "monotonic_ns": time.monotonic_ns(),
        "sequence": update.sequence,
        "snapshot_sequence": update.snapshot_sequence,
        "state": update.state,
        "renderer": update.renderer_state,
        "synced": update.synced,
        "gap": update.gap,
        "more": update.more,
        "output_present": bool(update.output),
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
                assert client.send(character.encode("ascii")) == 1
            echo_deadline = time.monotonic() + 15
            while time.monotonic() < echo_deadline:
                item = client.poll()
                if item.screen is not None and marker_command in _compact_screen_text(item.screen):
                    result["marker_command_echo_before_submit"] = "YES"
                    break
            else:
                result["marker_command_echo_before_submit"] = "NO"

            submit_at = time.monotonic_ns()
            result["terminator_ack"] = client.send(terminator)
            assert result["terminator_ack"] == len(terminator)
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
                if event["monotonic_ns"] >= submit_at
            ]
            result["post_submit_output_observed"] = any(
                event["output_present"] for event in post_submit_events
            )
            result["final_sequence"] = trace["last_poll"]
            result["powershell_error_seen"] = trace["powershell_error_seen"]
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
            print("R02_LONG_PATH_REPLICA " + json.dumps({
                "nonce": nonce[:12],
                "terminator": result["terminator"],
                "initial_prompt_seen": result["initial_prompt_seen"],
                "marker_command_echo_before_submit": result.get(
                    "marker_command_echo_before_submit", "UNKNOWN"
                ),
                "execution": result["execution"],
                "marker": result["marker"],
                "screen_result_path": result["screen_result_path"],
                "new_prompt": result["new_prompt"],
                "post_submit_output_observed": result.get(
                    "post_submit_output_observed", False
                ),
                "post_submit_process": result.get("post_submit_process"),
                "sequence": (
                    (result.get("final_sequence") or {}).get("sequence")
                ),
                "snapshot_sequence": (
                    (result.get("final_sequence") or {}).get("snapshot_sequence")
                ),
                "worker_running": (
                    (result.get("final_sequence") or {}).get("state") == "running"
                ),
                "renderer_ready": (
                    (result.get("final_sequence") or {}).get("renderer") == "ready"
                ),
                "synced": (result.get("final_sequence") or {}).get("synced"),
                "gap": (result.get("final_sequence") or {}).get("gap"),
                "cleanup": result["cleanup"],
                "error_category": result.get("error"),
            }, sort_keys=True))
        results.append(result)

    assert [item["terminator"] for item in results] == ["CR", "CRLF"]
    assert all(item["cleanup"] == "STOPPED" for item in results), (
        "one or more test-owned diagnostic sessions did not stop cleanly"
    )


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
                update = original_poll()
                latest = update
                if update.output:
                    captured_output.extend(update.output)
                if (update.output or update.gap or update.more
                        or not poll_events
                        or poll_events[-1]["sequence"] != update.sequence
                        or poll_events[-1]["snapshot_sequence"]
                        != update.snapshot_sequence):
                    poll_events.append(_r02_poll_record(update))
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
        print("R02_DIAGNOSTIC " + json.dumps({
            "nonce": nonce[:12],
            "case": case_name,
            "delivery": delivery,
            "terminator": terminator_name,
            "initial_screen_observed": initial is not None,
            "initial_size": result["initial_columns_lines"],
            "command_echo_observed": result["command_echo"],
            "execution": result["command_execution"],
            "marker_file": result["marker_file"],
            "result_path_observed": result["result_path_in_screen"],
            "new_prompt_observed": result["new_prompt"],
            "input_send_count": len(result["input_records"]),
            "input_acknowledged_bytes": sum(
                int(record.get("acknowledged_bytes", 0))
                for record in result["input_records"]
            ),
            "final_sequence": result["final_sequence"],
            "snapshot_sequence": result["final_snapshot_sequence"],
            "synced": result["final_synced"],
            "gap": result["final_gap"],
            "error_category": result["final_error"],
            "cleanup": result["cleanup"],
        }, sort_keys=True))

    assert len(summaries) == 6
    assert {item["case"] for item in summaries} == {
        f"{delivery.lower()}-{terminator.lower()}"
        for delivery in ("character", "bulk")
        for terminator in ("CR", "LF", "CRLF")
    }
    assert all(item["cleanup"] == "STOPPED" for item in summaries), (
        "one or more test-owned diagnostic sessions did not stop cleanly"
    )


def _r02_startup_stage_worker_copy(tmp_path, monkeypatch, active_diagnostics):
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

    marker_helper = '''def _r02_startup_mark(stage, status="observed"):
    root = os.environ.get("JTT_STARTUP_TRACE_DIR")
    nonce = os.environ.get("JTT_STARTUP_NONCE")
    if not root or not nonce:
        return
    record = {"stage": stage, "nonce": nonce, "status": status}
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

    assign_anchor = (
        "            assign_owned_job(job, shell_handle, win32job, win32api,\n"
        "                             win32event, win32con)\n"
    )
    if source.count(assign_anchor) != 1:
        raise AssertionError("startup diagnostic Job assignment anchor changed")
    source = source.replace(
        assign_anchor,
        assign_anchor + '            _r02_startup_mark("job_assignment_confirmed")\n',
        1,
    )

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
        release_anchor + '            _r02_startup_mark("worker_release_gate_signaled")\n',
        1,
    )

    renderer_anchor = (
        '        stream.feed("\\x1b[0m")\n'
    )
    if source.count(renderer_anchor) != 1:
        raise AssertionError("startup diagnostic renderer initialization anchor changed")
    source = source.replace(
        renderer_anchor,
        renderer_anchor + '        _r02_startup_mark("renderer_initialized")\n',
        1,
    )

    running_anchor = (
        '                runtime._atomic_metadata(metadata_path, metadata)\n'
        '\n            def read_output():\n'
    )
    if source.count(running_anchor) != 1:
        raise AssertionError("startup diagnostic running-state anchor changed")
    source = source.replace(
        running_anchor,
        '                runtime._atomic_metadata(metadata_path, metadata)\n'
        '            _r02_startup_mark("worker_running_published")\n'
        '\n            def read_output():\n',
        1,
    )

    ps_helper = (
        "function global:__jttStartupMark { "
        "param([string]$stage,[string]$status='observed'); "
        "if (!$env:JTT_STARTUP_TRACE_DIR -or !$env:JTT_STARTUP_NONCE) { return }; "
        "try { $path=Join-Path $env:JTT_STARTUP_TRACE_DIR "
        "($env:JTT_STARTUP_NONCE+'.'+$stage+'.json'); "
        "if (Test-Path -LiteralPath $path) { return }; "
        "$record=@{nonce=$env:JTT_STARTUP_NONCE;stage=$stage;status=$status}; "
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
         "__jttStartupMark 'bootstrap_initialized'; "),
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
        "__jttStartupMark 'prompt_return' $(if ($jttPromptReturn -eq "
        "$env:JTT_STARTUP_EXPECTED_PROMPT) {'match'} else {'mismatch'}); "
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
            key: str(value) for key, value in active_diagnostics.items()
        })
        return env

    monkeypatch.setattr(
        windows_manager, "_worker_environment", diagnostic_environment
    )
    return windows_manager


def test_r02_startup_stage_diagnostic_separates_original_and_instrumented_copy(
        tmp_path, monkeypatch):
    # Compare product startup at short/long cwd, then trace a separate worker copy.
    import ju0_terminal_tower  # noqa: F401 - installs the guarded shared namespace
    from tmux_agent_tower.terminal import TerminalRegistry, windows_manager
    import tempfile

    case_root = tmp_path / "r02-startup02"
    long_start = case_root / "long" / ("segment_" + "a" * 24) / ("segment_" + "b" * 24)
    assert len(str(long_start)) > 64
    assert 100 < len(str(long_start)) < 230
    short_root = Path(tempfile.mkdtemp(prefix="jt", dir=tempfile.gettempdir()))
    short_start = short_root / "short"
    cases = (("short_untruncated", short_start), ("long_truncated", long_start))
    if len(str(short_start)) > 64:
        shutil.rmtree(short_root)
        raise AssertionError("runner TEMP cannot provide an untruncated short cwd")

    def identity_is_session_owned(identity, manager):
        created, image, alive = manager._process_facts(
            identity.shell_pid, expected_start=identity.shell_start,
        )
        return bool(
            identity.session_id and identity.generation
            and identity.shell_pid > 0 and identity.shell_start
            and created == identity.shell_start
            and image.casefold() == identity.shell_path.casefold()
            and alive
        )

    product_results = []
    short_root_cleanup_verified = False
    for label, start_path in cases:
        destination = start_path / "dest"
        destination.mkdir(parents=True)
        monkeypatch.chdir(start_path)
        expected_prompt = _expected_prompt_path(start_path, 100)
        registry = TerminalRegistry(
            case_root / f"product-{label}-home" / "terminals"
        )
        client = registry.create("R02 product-worker prompt comparison")
        identity = client.identity
        shell_handle = None
        shell_exit_confirmed = False
        output_tail = ""
        last = None
        identity_valid_at_startup = False
        prompt_output = False
        prompt_snapshot = False
        try:
            shell_handle = _retain_test_process_handle(
                identity.shell_pid, identity.shell_start, identity.shell_path
            )
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                last = client.poll()
                if last.output:
                    output_tail = (output_tail + last.output.decode(
                        "utf-8", errors="replace"
                    ))[-8192:]
                    prompt_output = expected_prompt in output_tail
                if (last.screen is not None and last.synced
                        and last.state == "running"
                        and expected_prompt in _compact_screen_text(last.screen)):
                    prompt_snapshot = True
                    break
                time.sleep(0.02)
            identity_valid_at_startup = identity_is_session_owned(
                identity, windows_manager
            )
        finally:
            try:
                client.close()
            finally:
                try:
                    registry.stop(
                        identity.session_id, expected_generation=identity.generation,
                        expected_shell_pid=identity.shell_pid,
                    )
                finally:
                    if shell_handle is not None:
                        try:
                            shell_exit_confirmed = _retained_process_handle_signaled(
                                shell_handle
                            )
                        finally:
                            shell_handle.Close()
        registry_empty = not registry.list_sessions()
        if label == "short_untruncated":
            short_root_cleanup_verified = (
                shell_exit_confirmed and registry_empty
            )
        product_results.append({
            "case": label,
            "nonce": uuid.uuid4().hex,
            "worker_source": "product",
            "launch_mode": "-NoExit -Command",
            "planned_initial_size": "100x30",
            "session_identity_valid": identity_valid_at_startup,
            "prompt_present_in_pty_read": prompt_output,
            "prompt_present_in_client_snapshot": prompt_snapshot,
            "worker_running": bool(last is not None and last.state == "running"),
            "renderer_ready": bool(
                last is not None and last.renderer_state == "ready"
                and last.screen is not None
            ),
            "sequence": last.sequence if last is not None else None,
            "snapshot_sequence": (
                last.snapshot_sequence if last is not None else None
            ),
            "sequence_snapshot_current": bool(
                last is not None and last.sequence == last.snapshot_sequence
            ),
            "gap": bool(last.gap) if last is not None else None,
            "initial_size_matches": bool(
                last is not None and (last.columns, last.lines) == (100, 30)
            ),
            "owned_shell_exited_after_stop": shell_exit_confirmed,
            "registry_empty_after_stop": registry_empty,
        })
        print("R02_PRODUCT_STARTUP " + json.dumps(product_results[-1], sort_keys=True))

    active_diagnostics = {}
    diagnostic_manager = _r02_startup_stage_worker_copy(
        tmp_path, monkeypatch, active_diagnostics,
    )
    _label, start_path = cases[1]
    destination = start_path / "dest"
    expected_prompt = _expected_prompt_path(start_path, 100)
    expected_destination_prompt = _expected_prompt_path(destination, 100)
    trace_dir = case_root / "private-trace-instrumented-copy"
    windows_manager._ensure_private_directory(trace_dir)
    windows_manager._private_acl(trace_dir, directory=True)
    nonce = uuid.uuid4().hex
    path_marker = trace_dir / f"{nonce}.path-executed"
    active_diagnostics.clear()
    active_diagnostics.update({
        "JTT_STARTUP_TRACE_DIR": trace_dir,
        "JTT_STARTUP_NONCE": nonce,
        "JTT_STARTUP_EXPECTED_PROMPT": expected_prompt + " ",
        "JTT_STARTUP_MARKER": path_marker,
    })
    monkeypatch.chdir(start_path)
    registry = TerminalRegistry(case_root / "instrumented-copy-home" / "terminals")
    client = registry.create("R02 explicitly instrumented diagnostic copy")
    identity = client.identity
    identity_valid_at_startup = identity_is_session_owned(
        identity, diagnostic_manager
    )
    shell_handle = None
    shell_exit_confirmed = False
    initial = None
    raw_tail = ""
    path_marker_matches = False
    path_result_observed = False
    destination_prompt_observed = False
    path_sequence_current = False
    path_output_tail = ""
    try:
        shell_handle = _retain_test_process_handle(
            identity.shell_pid, identity.shell_start, identity.shell_path
        )
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            update = client.poll()
            if update.output:
                raw_tail = (raw_tail + update.output.decode(
                    "utf-8", errors="replace"
                ))[-8192:]
            if (update.screen is not None and update.synced
                    and update.state == "running"
                    and expected_prompt in _compact_screen_text(update.screen)):
                initial = update
                client_marker = trace_dir / f"{nonce}.client_snapshot_prompt.json"
                with client_marker.open("x", encoding="ascii") as stream:
                    json.dump({
                        "nonce": nonce,
                        "stage": "client_snapshot_prompt",
                        "status": "observed",
                    }, stream, sort_keys=True)
                    stream.flush()
                break
            time.sleep(0.02)

        if initial is not None and client.acquire():
            result_marker = f"JTT_STARTUP_PATH_RESULT_{nonce}_END"
            command = (
                "Set-Location 'dest'; "
                "[System.IO.File]::WriteAllText($env:JTT_STARTUP_MARKER,$env:JTT_STARTUP_NONCE); "
                "[Console]::WriteLine('JTT_STARTUP_PATH_RESULT_' + "
                "$env:JTT_STARTUP_NONCE + '_END'); $PWD.Path"
            )
            assert result_marker not in command
            encoded = (command + "\r\n").encode("utf-8")
            sent = client.send(encoded) == len(encoded)
            output_seen = False
            last = None
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                last = client.poll()
                if last.output:
                    path_output_tail = (
                        path_output_tail + last.output.decode(
                            "utf-8", errors="replace"
                        )
                    )[-4096:]
                output_seen = result_marker in path_output_tail
                screen = (
                    _compact_screen_text(last.screen)
                    if last.screen is not None else ""
                )
                destination_prompt_observed = expected_destination_prompt in screen
                if path_marker.is_file():
                    windows_manager._private_acl(
                        path_marker, directory=False, apply=True
                    )
                    windows_manager._private_acl(path_marker, directory=False)
                    path_marker_matches = (
                        path_marker.read_text(encoding="ascii") == nonce
                    )
                if output_seen and path_marker_matches and destination_prompt_observed:
                    break
                time.sleep(0.02)
            path_result_observed = output_seen and sent
            path_sequence_current = bool(
                last is not None and last.sequence == last.snapshot_sequence
                and not last.gap
            )
        else:
            sent = False
            last = initial

        stage_names = (
            "renderer_initialized",
            "job_assignment_confirmed",
            "worker_release_gate_signaled",
            "worker_running_published",
            "child_gate_passed",
            "bootstrap_initialized",
            "prompt_registered",
            "prompt_invoked",
            "prompt_return",
            "pty_read_prompt",
            "pyte_screen_prompt",
            "client_snapshot_prompt",
        )
        stages = {}
        for stage in stage_names:
            marker = trace_dir / f"{nonce}.{stage}.json"
            if not marker.is_file():
                stages[stage] = None
                continue
            try:
                windows_manager._private_acl(marker, directory=False, apply=True)
                windows_manager._private_acl(marker, directory=False)
                record = json.loads(marker.read_text(encoding="ascii"))
                valid = (
                    record.get("nonce") == nonce
                    and record.get("stage") == stage
                    and record.get("status") in (
                        "observed", "match", "mismatch",
                    )
                )
                status = record.get("status") if valid else None
                stages[stage] = (
                    status in ("observed", "match")
                    if status in ("observed", "match", "mismatch") else None
                )
            except (OSError, ValueError, UnicodeError):
                stages[stage] = None
    finally:
        try:
            client.close()
        finally:
            try:
                registry.stop(
                    identity.session_id, expected_generation=identity.generation,
                    expected_shell_pid=identity.shell_pid,
                )
            finally:
                if shell_handle is not None:
                    try:
                        shell_exit_confirmed = _retained_process_handle_signaled(
                            shell_handle
                        )
                    finally:
                        shell_handle.Close()
        instrumented_result = {
            "case": "long",
            "nonce": nonce,
            "worker_source": "explicit_instrumented_copy_not_product_evidence",
            "launch_mode": "-NoExit -Command",
            "planned_initial_size": "100x30",
            "session_identity_valid": identity_valid_at_startup,
            "prompt_present_in_pty_read": expected_prompt in raw_tail,
            "prompt_present_in_client_snapshot": initial is not None,
            "worker_running": bool(initial is not None and initial.state == "running"),
            "renderer_ready": bool(
                initial is not None and initial.renderer_state == "ready"
                and initial.screen is not None
            ),
            "sequence": initial.sequence if initial is not None else None,
            "snapshot_sequence": (
                initial.snapshot_sequence if initial is not None else None
            ),
            "sequence_snapshot_current": bool(
                initial is not None and initial.sequence == initial.snapshot_sequence
            ),
            "gap": bool(initial.gap) if initial is not None else None,
            "initial_size_matches": bool(
                initial is not None and (initial.columns, initial.lines) == (100, 30)
            ),
            "path_command_sent_and_output_observed": path_result_observed,
            "path_marker_matches": path_marker_matches,
            "destination_prompt_observed": destination_prompt_observed,
            "path_sequence_current": path_sequence_current,
            "path_sequence": last.sequence if last is not None else None,
            "path_snapshot_sequence": (
                last.snapshot_sequence if last is not None else None
            ),
            "stages": stages,
            "owned_shell_exited_after_stop": shell_exit_confirmed,
            "registry_empty_after_stop": not registry.list_sessions(),
            "trace_directory_private": windows_manager.is_private_directory(trace_dir),
        }
        print("R02_INSTRUMENTED_COPY_STARTUP " + json.dumps(
            instrumented_result, sort_keys=True
        ), flush=True)
        assert instrumented_result["owned_shell_exited_after_stop"], (
            "test-owned instrumented PowerShell did not stop"
        )
        assert instrumented_result["registry_empty_after_stop"], (
            "test-owned instrumented registry was not empty after stop"
        )
    if not short_root_cleanup_verified:
        raise AssertionError(
            "short-cwd test-owned shell and registry cleanup was not confirmed"
        )
    shutil.rmtree(short_root)
