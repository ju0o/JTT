"""Synthetic transport tests; native named-pipe cases run only on Windows."""
from __future__ import annotations

import ctypes
import threading
import time
import uuid
from ctypes import wintypes

import pytest

from tmux_agent_tower.terminal import windows_ipc
from tmux_agent_tower.terminal import windows_manager
from tmux_agent_tower.terminal.manager import TerminalError, TerminalIdentity


class _Operation:
    def __init__(self, direction, *, pending=False, value=None, error=None,
                 waits=(), cancel_state="requested"):
        self.direction = direction
        self.pending = pending
        self.value = value
        self.error = error
        self.waits = list(waits)
        self.cancel_state = "not_requested"
        self.next_cancel_state = cancel_state
        self.closed = False
        self.result_calls = 0


class _Handle:
    def __init__(self):
        self.close_count = 0

    def Close(self):
        self.close_count += 1


class _Reservation:
    def __init__(self, owner):
        self.owner = owner
        self.released = False

    def release(self):
        if not self.released:
            self.released = True
            self.owner.released += 1


class _FakeReaper:
    def __init__(self):
        self.released = 0
        self.orphans = []

    def reserve(self):
        return _Reservation(self)

    def adopt(self, operation, pipe_handle, api, reservation):
        self.orphans.append((operation, pipe_handle, api, reservation))

    def complete_orphan(self, index=0):
        operation, pipe_handle, api, reservation = self.orphans.pop(index)
        operation.waits = [True]
        try:
            api.result(operation)
        except OSError:
            pass
        api.dispose(operation)
        pipe_handle.Close()
        reservation.release()


class _FakeApi:
    def __init__(self, *operations):
        self.operations = list(operations)
        self.started = []

    def begin(self, handle, direction, value):
        operation = self.operations.pop(0)
        assert operation.direction == direction
        self.started.append((direction, value, handle))
        return operation

    def wait(self, operation, timeout_ms):
        assert timeout_ms >= 0
        if operation.waits:
            return operation.waits.pop(0)
        return False

    def result(self, operation):
        operation.result_calls += 1
        if operation.error:
            raise operation.error
        return operation.value

    def cancel(self, operation):
        operation.cancel_state = operation.next_cancel_state
        return operation.cancel_state

    def dispose(self, operation):
        operation.closed = True


class _WinError(OSError):
    def __init__(self, code):
        super().__init__("synthetic Win32 error {}".format(code))
        self.winerror = code


class _LifecycleOperation:
    """Synthetic pending operation retained by the real reaper."""

    def __init__(self, *, direction="read", waits=(), incomplete_results=0,
                 final_error=None, value=b"late", cancel_result="requested"):
        self.direction = direction
        self.pending = True
        self.waits = list(waits)
        self.incomplete_results = incomplete_results
        self.final_error = final_error
        self.value = value
        self.initial_event = threading.Event()
        self.initial_event.set()
        self.final_event = threading.Event()
        self.incomplete_seen = threading.Event()
        self.reaper_waiting = threading.Event()
        self.cancel_state = "not_requested"
        self.cancel_result = cancel_result
        self.reaper_owned = False
        self.closed = False
        self.result_calls = 0


class _LifecycleApi:
    """Synthetic Win32 result contract used with the real _PendingIoReaper."""

    def __init__(self, *operations):
        self.operations = list(operations)
        self.started = []

    def begin(self, handle, direction, value):
        operation = self.operations.pop(0)
        assert operation.direction == direction
        self.started.append((direction, value, handle))
        return operation

    def wait(self, operation, timeout_ms):
        if operation.waits:
            return operation.waits.pop(0)
        timeout = None if timeout_ms == windows_ipc.INFINITE else timeout_ms / 1000
        return operation.initial_event.wait(timeout)

    def result(self, operation):
        operation.result_calls += 1
        if isinstance(operation, _Operation):
            if operation.error:
                raise operation.error
            return operation.value
        if operation.incomplete_results:
            operation.incomplete_results -= 1
            operation.incomplete_seen.set()
            raise windows_ipc._IoIncomplete("synthetic ERROR_IO_INCOMPLETE")
        if not operation.final_event.is_set():
            operation.incomplete_seen.set()
            raise windows_ipc._IoIncomplete("synthetic ERROR_IO_INCOMPLETE")
        operation.pending = False
        if operation.final_error is not None:
            raise operation.final_error
        return operation.value

    def cancel(self, operation):
        operation.cancel_state = operation.cancel_result
        return operation.cancel_state

    def dispose(self, operation):
        operation.closed = True

    def reap(self, operation):
        if not self.wait(operation, windows_ipc.INFINITE):
            raise AssertionError("synthetic operation did not signal")
        try:
            self.result(operation)
        except windows_ipc._IoIncomplete:
            # Mirrors the native reaper's blocking GetOverlappedResult(bWait=True).
            operation.reaper_waiting.set()
            if not operation.final_event.wait(3):
                raise AssertionError("synthetic completion was not released")
            try:
                self.result(operation)
            except windows_ipc._IoIncomplete:
                raise
            except OSError:
                # A terminal I/O status, including ERROR_OPERATION_ABORTED,
                # proves completion even though no response was delivered.
                pass
        except OSError:
            pass
        self.dispose(operation)


class _SuccessfulPendingOperation:
    """A pending operation whose completion is reported by the API result."""

    def __init__(self, direction, value):
        self.direction = direction
        self.value = value
        self.pending = True
        self.closed = False
        self.dispose_count = 0
        self.dispose_attempted = False
        self.result_calls = 0


class _SuccessfulPendingApi:
    """Synthetic pending API for normal-completion transport regressions."""

    def __init__(self, response=b"reply", *, dispose_failures=0):
        self.response = response
        self.dispose_failures = dispose_failures
        self.operations = []

    def begin(self, _handle, direction, value):
        result = len(value) if direction == "write" else self.response
        operation = _SuccessfulPendingOperation(direction, result)
        self.operations.append(operation)
        return operation

    def wait(self, operation, timeout_ms):
        assert operation.pending
        assert timeout_ms > 0
        return True

    def result(self, operation):
        operation.result_calls += 1
        operation.pending = False
        return operation.value

    def dispose(self, operation):
        operation.dispose_count += 1
        if operation.dispose_count <= self.dispose_failures:
            raise OSError("synthetic dispose failure")
        operation.closed = True

    def cancel(self, _operation):
        raise AssertionError("normal completion must not request cancellation")


def _wait_for_reaper(reaper, *, pending_count, reserved_count=None, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with reaper._lock:
            pending = len(reaper._unreaped)
            reserved = reaper._reserved
        if pending == pending_count and (reserved_count is None or reserved == reserved_count):
            return
        time.sleep(0.005)
    raise AssertionError("synthetic reaper state did not reach the expected value")


def _assert_reaper_reservations(reaper, expected):
    with reaper._lock:
        assert reaper._reserved == expected
        assert len(reaper._unreaped) == 0


def _count_reservation_releases(reaper):
    calls = []
    original_release = reaper._release

    def counted_release(reservation, operation=None):
        calls.append(1)
        return original_release(reservation, operation)

    reaper._release = counted_release
    return calls


def test_pending_write_success_disposes_and_releases_reservation():
    reaper = windows_ipc._PendingIoReaper(limit=windows_ipc.MAX_PENDING_PIPE_IO)
    release_calls = _count_reservation_releases(reaper)
    api = _SuccessfulPendingApi()
    handle = _Handle()
    transport = windows_ipc.OverlappedPipeTransport(
        handle, timeout=1.0, api=api, reaper=reaper
    )

    assert transport._perform("write", b"request", time.monotonic() + 1.0) == 7
    assert len(api.operations) == 1
    operation = api.operations[0]
    assert operation.result_calls == 1
    assert operation.dispose_count == 1
    assert operation.closed
    _assert_reaper_reservations(reaper, 0)
    assert len(release_calls) == 1
    assert not transport.poisoned
    assert handle.close_count == 0


def test_pending_read_success_disposes_and_releases_reservation():
    reaper = windows_ipc._PendingIoReaper(limit=windows_ipc.MAX_PENDING_PIPE_IO)
    release_calls = _count_reservation_releases(reaper)
    api = _SuccessfulPendingApi(response=b"response")
    handle = _Handle()
    transport = windows_ipc.OverlappedPipeTransport(
        handle, timeout=1.0, api=api, reaper=reaper
    )

    assert transport._perform("read", 896 * 1024,
                               time.monotonic() + 1.0) == b"response"
    assert len(api.operations) == 1
    operation = api.operations[0]
    assert operation.result_calls == 1
    assert operation.dispose_count == 1
    assert operation.closed
    _assert_reaper_reservations(reaper, 0)
    assert len(release_calls) == 1
    assert not transport.poisoned
    assert handle.close_count == 0


def test_repeated_pending_requests_release_slots_below_reaper_capacity():
    reaper = windows_ipc._PendingIoReaper(limit=windows_ipc.MAX_PENDING_PIPE_IO)
    release_calls = _count_reservation_releases(reaper)
    api = _SuccessfulPendingApi(response=b"response")
    handle = _Handle()
    transport = windows_ipc.OverlappedPipeTransport(
        handle, timeout=1.0, api=api, reaper=reaper
    )

    responses = [
        transport.exchange("request-{}".format(index).encode("ascii"))
        for index in range(40)
    ]

    assert responses == [b"response"] * 40
    _assert_reaper_reservations(reaper, 0)
    assert not transport.poisoned

    assert len(api.operations) == 80
    assert len(release_calls) == 80
    assert all(operation.result_calls == 1 for operation in api.operations)
    assert all(operation.dispose_count == 1 for operation in api.operations)
    assert all(operation.closed and not operation.pending for operation in api.operations)
    assert handle.close_count == 0


def test_pending_dispose_failure_is_not_retried_and_poisons_transport():
    reaper = windows_ipc._PendingIoReaper(limit=windows_ipc.MAX_PENDING_PIPE_IO)
    release_calls = _count_reservation_releases(reaper)
    api = _SuccessfulPendingApi(dispose_failures=1)
    handle = _Handle()
    transport = windows_ipc.OverlappedPipeTransport(
        handle, timeout=1.0, api=api, reaper=reaper
    )

    with pytest.raises(OSError, match="synthetic dispose failure") as caught:
        transport.exchange(b"request")

    assert len(api.operations) == 1
    operation = api.operations[0]
    assert operation.result_calls == 1
    assert operation.dispose_count == 1
    assert operation.dispose_attempted
    assert not operation.pending
    assert not operation.closed
    assert transport.poisoned
    assert handle.close_count == 1
    assert len(release_calls) == 1
    _assert_reaper_reservations(reaper, 0)
    cleanup_state = getattr(caught.value, "cleanup_state", None)
    assert cleanup_state == {
        "direction": "write",
        "io_completion": "confirmed",
        "dispose_attempted": True,
        "operation_closed": False,
        "event_handle_state": "unknown",
        "reservation_release_attempted": True,
        "reservation_release_state": "released",
        "reservation_token_consumed": True,
        "reserved_slots": 0,
        "dispose_error_category": "OSError",
        "reservation_error_category": None,
    }

    with pytest.raises(windows_ipc.PipeIoError, match="poisoned"):
        transport.exchange(b"must-not-be-sent")
    assert len(api.operations) == 1
    assert operation.dispose_count == 1


def test_reservation_release_failure_is_reported_and_fails_closed(monkeypatch):
    reaper = windows_ipc._PendingIoReaper(limit=windows_ipc.MAX_PENDING_PIPE_IO)
    api = _SuccessfulPendingApi()
    handle = _Handle()
    transport = windows_ipc.OverlappedPipeTransport(
        handle, timeout=1.0, api=api, reaper=reaper
    )
    reservations = []
    original_reserve = reaper.reserve

    def capture_reservation():
        reservation = original_reserve()
        reservations.append(reservation)
        return reservation

    release_attempts = []

    def fail_before_reaper_decrement(_reservation, _operation=None):
        release_attempts.append(1)
        raise OSError("synthetic reservation release failure")

    monkeypatch.setattr(reaper, "reserve", capture_reservation)
    monkeypatch.setattr(reaper, "_release", fail_before_reaper_decrement)
    with pytest.raises(OSError, match="synthetic reservation release failure") as caught:
        transport.exchange(b"request")

    operation = api.operations[0]
    assert operation.dispose_count == 1 and operation.closed
    assert operation.dispose_attempted
    assert len(release_attempts) == 1
    assert reservations[0]._release_state == "unknown"
    assert not reservations[0]._released
    assert reaper._reserved == 1
    assert transport.poisoned
    cleanup_state = getattr(caught.value, "cleanup_state", None)
    assert cleanup_state == {
        "direction": "write",
        "io_completion": "confirmed",
        "dispose_attempted": True,
        "operation_closed": True,
        "event_handle_state": "closed",
        "reservation_release_attempted": True,
        "reservation_release_state": "unknown",
        "reservation_token_consumed": True,
        "reserved_slots": 1,
        "dispose_error_category": None,
        "reservation_error_category": "OSError",
    }

    with pytest.raises(windows_ipc.PipeIoError, match="poisoned"):
        transport.exchange(b"must-not-be-sent")
    assert len(api.operations) == 1


def _count_reaper_worker_starts(monkeypatch, *, fail_before_start=0,
                                raise_after_start=0):
    real_thread = threading.Thread
    attempts = []
    started = []

    class CountingThread(real_thread):
        def start(self):
            is_reaper = self.name == "jtt-pipe-io-reaper"
            if is_reaper:
                attempts.append(self)
                ordinal = len(attempts)
                if ordinal == fail_before_start:
                    raise RuntimeError("synthetic thread start failure")
            result = super().start()
            if is_reaper:
                started.append(self)
                if len(attempts) == raise_after_start:
                    raise RuntimeError("synthetic post-start error")
            return result

    monkeypatch.setattr(windows_ipc.threading, "Thread", CountingThread)
    return attempts, started


def _owner_record(operation):
    with windows_ipc._REAPER_OWNERSHIP_LOCK:
        return windows_ipc._REAPER_OWNERS.get(id(operation))


class _NativeCallable:
    def __init__(self, callback):
        self.callback = callback
        self.calls = 0

    def __call__(self, *args):
        self.calls += 1
        return self.callback(*args)


def _overlapped_from_pointer(pointer):
    return ctypes.cast(
        pointer, ctypes.POINTER(windows_ipc._Overlapped)
    ).contents


def _native_api_for_result(monkeypatch, outcomes):
    last_error = {"code": 0}
    wait_values = []
    close_calls = []

    def get_result(_handle, _overlapped, transferred_pointer, wait):
        wait_values.append(bool(wait))
        ok, error, count = outcomes.pop(0)
        last_error["code"] = error
        if ok:
            ctypes.cast(
                transferred_pointer, ctypes.POINTER(wintypes.DWORD)
            ).contents.value = count
        return int(ok)

    kernel32 = type("Kernel32", (), {})()
    kernel32.GetOverlappedResult = _NativeCallable(get_result)
    kernel32.WaitForSingleObject = _NativeCallable(
        lambda *_args: windows_ipc.WAIT_OBJECT_0
    )
    kernel32.CloseHandle = _NativeCallable(
        lambda handle: close_calls.append(handle) or 1
    )
    api = object.__new__(windows_ipc._NativeApi)
    api._wintypes = wintypes
    api._kernel32 = kernel32
    monkeypatch.setattr(
        windows_ipc.ctypes, "get_last_error", lambda: last_error["code"], raising=False
    )
    monkeypatch.setattr(
        windows_ipc.ctypes, "WinError", lambda code: _WinError(code), raising=False
    )
    overlapped = windows_ipc._Overlapped()
    operation = windows_ipc._NativeOperation(
        "read", 77, 88, overlapped, ctypes.create_string_buffer(16)
    )
    operation.pending = True
    return api, operation, wait_values, close_calls


def test_native_api_immediate_write_uses_first_call_byte_count():
    def write_file(_handle, _buffer, length, byte_count, overlapped_pointer):
        assert byte_count is None
        _overlapped_from_pointer(overlapped_pointer).InternalHigh = length - 1
        return 1

    kernel32 = type("Kernel32", (), {})()
    kernel32.CreateEventW = _NativeCallable(lambda *_args: 123)
    kernel32.WriteFile = _NativeCallable(write_file)
    kernel32.ReadFile = _NativeCallable(lambda *_args: 0)
    kernel32.GetOverlappedResult = _NativeCallable(
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("immediate completion must not query pending I/O")
        )
    )
    kernel32.CloseHandle = _NativeCallable(lambda *_args: 1)
    api = object.__new__(windows_ipc._NativeApi)
    api._wintypes = wintypes
    api._kernel32 = kernel32

    operation = api.begin(99, "write", b"request")
    assert operation.pending is False
    assert operation.transferred == len(b"request") - 1
    assert api.result(operation) == len(b"request") - 1
    assert kernel32.GetOverlappedResult.calls == 0
    api.dispose(operation)
    assert operation.closed


def test_native_api_immediate_read_uses_first_call_byte_count_and_payload():
    payload = b"reply"

    def read_file(_handle, buffer_pointer, _length, byte_count, overlapped_pointer):
        assert byte_count is None
        ctypes.memmove(buffer_pointer, payload, len(payload))
        _overlapped_from_pointer(overlapped_pointer).InternalHigh = len(payload)
        return 1

    kernel32 = type("Kernel32", (), {})()
    kernel32.CreateEventW = _NativeCallable(lambda *_args: 456)
    kernel32.WriteFile = _NativeCallable(lambda *_args: 0)
    kernel32.ReadFile = _NativeCallable(read_file)
    kernel32.GetOverlappedResult = _NativeCallable(
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("immediate completion must not query pending I/O")
        )
    )
    kernel32.CloseHandle = _NativeCallable(lambda *_args: 1)
    api = object.__new__(windows_ipc._NativeApi)
    api._wintypes = wintypes
    api._kernel32 = kernel32

    operation = api.begin(100, "read", 32)
    assert operation.pending is False
    assert operation.transferred == len(payload)
    assert api.result(operation) == payload
    assert kernel32.GetOverlappedResult.calls == 0
    api.dispose(operation)


def test_native_reaper_waits_for_definitive_completion_after_io_incomplete(monkeypatch):
    api, operation, wait_values, close_calls = _native_api_for_result(
        monkeypatch,
        [(False, windows_ipc.ERROR_IO_INCOMPLETE, 0), (True, 0, 5)],
    )

    api.reap(operation)

    assert wait_values == [False, True]
    assert not operation.pending
    assert operation.closed
    assert close_calls == [88]


def test_native_result_keeps_operation_pending_on_error_io_incomplete(monkeypatch):
    api, operation, wait_values, close_calls = _native_api_for_result(
        monkeypatch, [(False, windows_ipc.ERROR_IO_INCOMPLETE, 0)]
    )

    with pytest.raises(windows_ipc._IoIncomplete):
        api.result(operation)

    assert wait_values == [False]
    assert operation.pending
    assert not operation.closed
    assert close_calls == []


def test_native_reaper_retains_resources_when_completion_state_is_unknown(monkeypatch):
    api, operation, wait_values, close_calls = _native_api_for_result(
        monkeypatch, [(False, 6, 0), (False, 6, 0)]
    )

    with pytest.raises(windows_ipc._IoCompletionUnknown) as caught:
        api.reap(operation)

    assert caught.value.error_code == 6
    assert wait_values == [False, True]
    assert operation.pending
    assert not operation.closed
    assert close_calls == []


def test_operation_aborted_is_a_terminal_completion_status(monkeypatch):
    api, operation, wait_values, close_calls = _native_api_for_result(
        monkeypatch, [(False, windows_ipc.ERROR_OPERATION_ABORTED, 0)]
    )

    with pytest.raises(_WinError) as caught:
        api.result(operation)

    assert caught.value.winerror == windows_ipc.ERROR_OPERATION_ABORTED
    assert wait_values == [False]
    assert not operation.pending
    assert not operation.closed
    assert close_calls == []


def test_transport_returns_one_normal_request_response_pair():
    api = _FakeApi(
        _Operation("write", value=3),
        _Operation("read", value=b"reply"),
    )
    reaper = _FakeReaper()
    handle = _Handle()
    transport = windows_ipc.OverlappedPipeTransport(
        handle, timeout=0.5, api=api, reaper=reaper
    )

    assert transport.exchange(b"req") == b"reply"
    assert [item[0] for item in api.started] == ["write", "read"]
    assert not transport.poisoned
    assert reaper.released == 2
    assert handle.close_count == 0
    transport.close()
    assert handle.close_count == 1


def test_write_timeout_cancels_exact_operation_before_any_read():
    pending_write = _Operation("write", pending=True, value=3, waits=[False, True])
    api = _FakeApi(pending_write)
    handle = _Handle()
    transport = windows_ipc.OverlappedPipeTransport(
        handle, timeout=0.01, api=api, reaper=_FakeReaper(),
        cancel_drain_timeout_ms=5,
    )

    with pytest.raises(windows_ipc.PipeIoTimeout) as caught:
        transport.exchange(b"req")

    assert caught.value.stage == "write"
    assert caught.value.cancel_state == "requested:completed"
    assert [item[0] for item in api.started] == ["write"]
    assert pending_write.closed
    assert transport.poisoned
    assert handle.close_count == 1


def test_partial_write_is_not_followed_by_a_read_or_retransmission():
    api = _FakeApi(_Operation("write", value=1))
    handle = _Handle()
    transport = windows_ipc.OverlappedPipeTransport(
        handle, timeout=0.5, api=api, reaper=_FakeReaper()
    )

    with pytest.raises(windows_ipc.PipeIoError, match="write was incomplete"):
        transport.exchange(b"request")
    assert [item[0] for item in api.started] == ["write"]
    assert transport.poisoned
    assert handle.close_count == 1


def test_timeout_cancellation_completion_is_verified_but_connection_is_poisoned():
    late_response = _Operation("read", pending=True, value=b"late", waits=[False, True])
    api = _FakeApi(_Operation("write", value=3), late_response)
    reaper = _FakeReaper()
    handle = _Handle()
    transport = windows_ipc.OverlappedPipeTransport(
        handle, timeout=0.01, api=api, reaper=reaper, cancel_drain_timeout_ms=5
    )

    with pytest.raises(windows_ipc.PipeIoTimeout) as caught:
        transport.exchange(b"req")

    assert caught.value.stage == "read"
    assert caught.value.cancel_state == "requested:completed"
    assert late_response.closed
    assert transport.poisoned
    assert handle.close_count == 1
    with pytest.raises(windows_ipc.PipeIoError):
        transport.exchange(b"another request")


def test_completion_racing_cancel_is_not_accepted_as_a_later_response():
    completed_before_cancel = _Operation(
        "read", pending=True, value=b"late", waits=[False, True],
        cancel_state="not_found",
    )
    api = _FakeApi(_Operation("write", value=len(b"request")), completed_before_cancel)
    reaper = _FakeReaper()
    handle = _Handle()
    transport = windows_ipc.OverlappedPipeTransport(
        handle, timeout=0.01, api=api, reaper=reaper,
        cancel_drain_timeout_ms=5,
    )

    # The request deadline expires, then the server response completes in the
    # race before cancellation is observed. The result still belongs to an
    # expired request, so it is not returned to a later caller.
    with pytest.raises(windows_ipc.PipeIoTimeout) as caught:
        transport.exchange(b"request")

    assert caught.value.stage == "read"
    assert caught.value.cancel_state == "not_found:completed"
    assert completed_before_cancel.closed
    assert transport.poisoned
    assert handle.close_count == 1
    assert reaper.released == 2
    with pytest.raises(windows_ipc.PipeIoError, match="poisoned"):
        transport.exchange(b"next request")


def test_failed_cancel_with_confirmed_completion_remains_a_timeout():
    operation = _Operation(
        "read", pending=True, error=OSError("synthetic completion error"),
        waits=[False, True], cancel_state="failed:5",
    )
    api = _FakeApi(_Operation("write", value=len(b"request")), operation)
    handle = _Handle()
    transport = windows_ipc.OverlappedPipeTransport(
        handle, timeout=0.01, api=api, reaper=_FakeReaper(),
        cancel_drain_timeout_ms=5,
    )

    with pytest.raises(windows_ipc.PipeIoTimeout) as caught:
        transport.exchange(b"request")

    assert caught.value.stage == "read"
    assert caught.value.cancel_state == "failed:5:completed_with_error"
    assert operation.closed
    assert transport.poisoned
    assert handle.close_count == 1


def test_cancel_completion_with_operation_aborted_is_terminal_not_success():
    operation = _Operation(
        "read", pending=True, error=_WinError(windows_ipc.ERROR_OPERATION_ABORTED),
        waits=[False, True],
    )
    api = _FakeApi(_Operation("write", value=3), operation)
    handle = _Handle()
    reaper = _FakeReaper()
    transport = windows_ipc.OverlappedPipeTransport(
        handle, timeout=0.01, api=api, reaper=reaper, cancel_drain_timeout_ms=5
    )

    with pytest.raises(windows_ipc.PipeIoTimeout) as caught:
        transport.exchange(b"req")

    assert caught.value.cancel_state == "requested:completed_aborted"
    assert operation.closed and transport.poisoned
    assert handle.close_count == 1
    assert reaper.released == 2


def test_delayed_or_failed_cancel_retains_buffers_and_handle_until_reaped():
    pending_read = _Operation(
        "read", pending=True, value=b"late", waits=[False, False],
        cancel_state="failed:unknown",
    )
    api = _FakeApi(_Operation("write", value=3), pending_read)
    reaper = _FakeReaper()
    handle = _Handle()
    transport = windows_ipc.OverlappedPipeTransport(
        handle, timeout=0.01, api=api, reaper=reaper, cancel_drain_timeout_ms=1
    )

    with pytest.raises(windows_ipc.PipeIoTimeout) as caught:
        transport.exchange(b"req")

    assert caught.value.cancel_state == "failed:unknown:completion_pending"
    assert len(reaper.orphans) == 1
    assert not pending_read.closed
    assert handle.close_count == 0
    # A late completion is reaped on the old connection and cannot be reused.
    reaper.complete_orphan()
    assert pending_read.closed
    assert handle.close_count == 1
    with pytest.raises(windows_ipc.PipeIoError):
        transport.exchange(b"another request")


def test_real_reaper_retains_resources_after_io_incomplete_until_final_completion():
    reaper = windows_ipc._PendingIoReaper(limit=1)
    operation = _LifecycleOperation(incomplete_results=1)
    handle = _Handle()
    reservation = reaper.reserve()

    reaper.adopt(operation, handle, _LifecycleApi(), reservation)
    try:
        assert operation.reaper_waiting.wait(1)
        assert not operation.closed
        assert handle.close_count == 0
        assert reaper.pending_count == 1
        with reaper._lock:
            assert reaper._reserved == 1
    finally:
        operation.final_event.set()
    _wait_for_reaper(reaper, pending_count=0, reserved_count=0)
    assert operation.closed
    assert handle.close_count == 1


def test_real_reaper_sequential_duplicate_adopt_has_one_owner_and_one_close(monkeypatch):
    attempts, _started = _count_reaper_worker_starts(monkeypatch)
    reaper = windows_ipc._PendingIoReaper(limit=2)
    operation = _LifecycleOperation()
    api = _LifecycleApi()
    handle = _Handle()
    duplicate_handle = _Handle()
    reservation = reaper.reserve()
    release_calls = 0
    release = reaper._release

    def counted_release(reservation, operation=None):
        nonlocal release_calls
        release_calls += 1
        return release(reservation, operation)

    monkeypatch.setattr(reaper, "_release", counted_release)

    assert reaper.adopt(operation, handle, api, reservation)
    assert operation.reaper_waiting.wait(1)
    record = _owner_record(operation)
    assert record is not None and record.reaper is reaper
    assert reaper.adopt(operation, duplicate_handle, api, reservation) is False
    assert _owner_record(operation) is record
    assert len(attempts) == 1
    assert reaper.pending_count == 1
    assert reaper._reserved == 1
    assert release_calls == 0
    assert not operation.closed
    assert handle.close_count == duplicate_handle.close_count == 0

    operation.final_event.set()
    _wait_for_reaper(reaper, pending_count=0, reserved_count=0)
    assert operation.reaper_completed
    assert handle.close_count == 1
    assert duplicate_handle.close_count == 0
    assert release_calls == 1

    unused = reaper.reserve()
    assert reaper.adopt(operation, duplicate_handle, api, unused) is False
    assert unused._released
    assert reaper._reserved == 0
    assert len(attempts) == 1
    assert duplicate_handle.close_count == 0


def test_real_reaper_concurrent_duplicate_adopt_starts_one_worker(monkeypatch):
    attempts, _started = _count_reaper_worker_starts(monkeypatch)
    reaper = windows_ipc._PendingIoReaper(limit=2)
    operation = _LifecycleOperation()
    api = _LifecycleApi()
    handles = [_Handle(), _Handle()]
    reservation = reaper.reserve()
    barrier = threading.Barrier(3)
    results = []
    errors = []

    def adopt(index):
        barrier.wait()
        try:
            results.append(reaper.adopt(
                operation, handles[index], api, reservation
            ))
        except BaseException as exc:
            errors.append(exc)

    callers = [threading.Thread(target=adopt, args=(index,)) for index in range(2)]
    for caller in callers:
        caller.start()
    barrier.wait()
    for caller in callers:
        caller.join(2)
        assert not caller.is_alive()

    assert errors == []
    assert sorted(results) == [False, True]
    assert operation.reaper_waiting.wait(1)
    assert len(attempts) == 1
    assert reaper.pending_count == 1
    assert reaper._reserved == 1
    with windows_ipc._REAPER_OWNERSHIP_LOCK:
        assert sum(
            record.operation is operation
            for record in windows_ipc._REAPER_OWNERS.values()
        ) == 1

    operation.final_event.set()
    _wait_for_reaper(reaper, pending_count=0, reserved_count=0)
    assert sum(handle.close_count for handle in handles) == 1
    assert sorted(handle.close_count for handle in handles) == [0, 1]


def test_real_reaper_duplicate_across_instances_returns_unused_slot(monkeypatch):
    attempts, _started = _count_reaper_worker_starts(monkeypatch)
    owner = windows_ipc._PendingIoReaper(limit=1)
    duplicate = windows_ipc._PendingIoReaper(limit=1)
    operation = _LifecycleOperation()
    api = _LifecycleApi()
    owned_handle = _Handle()
    duplicate_handle = _Handle()
    owned_reservation = owner.reserve()
    duplicate_reservation = duplicate.reserve()

    assert owner.adopt(operation, owned_handle, api, owned_reservation)
    assert operation.reaper_waiting.wait(1)
    assert duplicate.adopt(
        operation, duplicate_handle, api, duplicate_reservation
    ) is False
    assert len(attempts) == 1
    assert owner.pending_count == 1 and owner._reserved == 1
    assert duplicate.pending_count == 0 and duplicate._reserved == 0
    assert duplicate_reservation._released
    assert not operation.closed
    assert owned_handle.close_count == duplicate_handle.close_count == 0

    operation.final_event.set()
    _wait_for_reaper(owner, pending_count=0, reserved_count=0)
    _wait_for_reaper(duplicate, pending_count=0, reserved_count=0)
    assert owned_handle.close_count == 1
    assert duplicate_handle.close_count == 0


def test_real_reaper_concurrent_cross_instance_adopt_has_global_single_owner(monkeypatch):
    attempts, _started = _count_reaper_worker_starts(monkeypatch)
    reapers = [
        windows_ipc._PendingIoReaper(limit=1),
        windows_ipc._PendingIoReaper(limit=1),
    ]
    operation = _LifecycleOperation()
    api = _LifecycleApi()
    handles = [_Handle(), _Handle()]
    reservations = [reaper.reserve() for reaper in reapers]
    barrier = threading.Barrier(3)
    results = []
    errors = []

    def adopt(index):
        barrier.wait()
        try:
            results.append(reapers[index].adopt(
                operation, handles[index], api, reservations[index]
            ))
        except BaseException as exc:
            errors.append(exc)

    callers = [threading.Thread(target=adopt, args=(index,)) for index in range(2)]
    for caller in callers:
        caller.start()
    barrier.wait()
    for caller in callers:
        caller.join(2)
        assert not caller.is_alive()

    assert errors == []
    assert sorted(results) == [False, True]
    owner_record = _owner_record(operation)
    assert owner_record is not None
    assert owner_record.reaper in reapers
    assert owner_record.reservation in reservations
    assert operation.reaper_waiting.wait(1)
    assert len(attempts) == 1
    assert sum(reaper.pending_count for reaper in reapers) == 1
    assert sum(reaper._reserved for reaper in reapers) == 1
    assert sum(reservation._released for reservation in reservations) == 1

    operation.final_event.set()
    for reaper in reapers:
        _wait_for_reaper(reaper, pending_count=0, reserved_count=0)
    assert sum(handle.close_count for handle in handles) == 1


def test_real_reaper_duplicate_is_safe_during_owner_slot_release_race(monkeypatch):
    attempts, _started = _count_reaper_worker_starts(monkeypatch)
    reaper = windows_ipc._PendingIoReaper(limit=2)
    operation = _LifecycleOperation()
    api = _LifecycleApi()
    handle = _Handle()
    reservation = reaper.reserve()
    release_entered = threading.Event()
    allow_release = threading.Event()
    real_release_owned = reservation._release_owned

    def delayed_release_owned(owned_operation):
        release_entered.set()
        if not allow_release.wait(2):
            raise AssertionError("test did not release reservation gate")
        real_release_owned(owned_operation)

    monkeypatch.setattr(reservation, "_release_owned", delayed_release_owned)
    assert reaper.adopt(operation, handle, api, reservation)
    assert operation.reaper_waiting.wait(1)
    operation.final_event.set()
    assert release_entered.wait(1)

    # The completed marker and owner removal precede slot return. Re-adoption
    # in that narrow window must remain a no-op, not steal or double-release.
    assert reaper.adopt(operation, _Handle(), api, reservation) is False
    assert len(attempts) == 1
    assert reaper.pending_count == 0
    assert reaper._reserved == 1
    allow_release.set()
    _wait_for_reaper(reaper, pending_count=0, reserved_count=0)
    assert handle.close_count == 1
    assert len(attempts) == 1


def test_real_reaper_rejects_reusing_one_reservation_for_another_operation():
    reaper = windows_ipc._PendingIoReaper(limit=2)
    first = _LifecycleOperation()
    second = _LifecycleOperation()
    api = _LifecycleApi()
    first_handle = _Handle()
    second_handle = _Handle()
    reservation = reaper.reserve()

    assert reaper.adopt(first, first_handle, api, reservation)
    assert first.reaper_waiting.wait(1)
    with pytest.raises(RuntimeError, match="already bound or released"):
        reaper.adopt(second, second_handle, api, reservation)
    assert reaper.pending_count == 1
    assert reaper._reserved == 1
    assert _owner_record(second) is None
    assert not second.reaper_owned
    assert first_handle.close_count == second_handle.close_count == 0

    first.final_event.set()
    _wait_for_reaper(reaper, pending_count=0, reserved_count=0)
    assert first_handle.close_count == 1
    assert second_handle.close_count == 0


def test_real_reaper_rejects_already_completed_operation_without_owning_resources(monkeypatch):
    attempts, _started = _count_reaper_worker_starts(monkeypatch)
    reaper = windows_ipc._PendingIoReaper(limit=1)
    operation = _LifecycleOperation()
    operation.reaper_completed = True
    handle = _Handle()
    reservation = reaper.reserve()

    assert reaper.adopt(operation, handle, _LifecycleApi(), reservation) is False
    assert reservation._released
    assert reaper.pending_count == 0 and reaper._reserved == 0
    assert _owner_record(operation) is None
    assert not attempts
    assert handle.close_count == 0


def test_real_reaper_handles_two_distinct_operations_independently(monkeypatch):
    attempts, _started = _count_reaper_worker_starts(monkeypatch)
    reaper = windows_ipc._PendingIoReaper(limit=2)
    pairs = [
        (_LifecycleOperation(), _LifecycleApi(), _Handle()),
        (_LifecycleOperation(), _LifecycleApi(), _Handle()),
    ]
    for operation, api, handle in pairs:
        assert reaper.adopt(operation, handle, api, reaper.reserve())
    assert all(operation.reaper_waiting.wait(1) for operation, _, _ in pairs)
    assert len(attempts) == 2
    assert reaper.pending_count == 2 and reaper._reserved == 2
    for operation, _, _ in pairs:
        operation.final_event.set()
    _wait_for_reaper(reaper, pending_count=0, reserved_count=0)
    assert all(operation.reaper_completed for operation, _, _ in pairs)
    assert all(handle.close_count == 1 for _, _, handle in pairs)


def test_real_reaper_duplicate_does_not_consume_capacity_at_limit(monkeypatch):
    attempts, _started = _count_reaper_worker_starts(monkeypatch)
    limit = windows_ipc.MAX_PENDING_PIPE_IO
    reaper = windows_ipc._PendingIoReaper(limit=limit)
    other_reaper = windows_ipc._PendingIoReaper(limit=1)
    records = []
    for _ in range(limit):
        operation = _LifecycleOperation()
        api = _LifecycleApi()
        handle = _Handle()
        reservation = reaper.reserve()
        assert reaper.adopt(operation, handle, api, reservation)
        records.append((operation, handle, reservation))

    try:
        assert all(operation.reaper_waiting.wait(1) for operation, _, _ in records)
        first_operation, first_handle, first_reservation = records[0]
        assert reaper.adopt(
            first_operation, _Handle(), _LifecycleApi(), first_reservation
        ) is False
        cross_handle = _Handle()
        cross_reservation = other_reaper.reserve()
        assert other_reaper.adopt(
            first_operation, cross_handle, _LifecycleApi(), cross_reservation
        ) is False
        assert reaper.pending_count == limit and reaper._reserved == limit
        assert other_reaper.pending_count == 0 and other_reaper._reserved == 0
        assert cross_reservation._released
        assert cross_handle.close_count == 0
        assert len(attempts) == limit
        with pytest.raises(windows_ipc.PipeIoCapacityError):
            reaper.reserve()

        records[0][0].final_event.set()
        _wait_for_reaper(reaper, pending_count=limit - 1,
                         reserved_count=limit - 1)
        recovered = reaper.reserve()
        assert reaper._reserved == limit
        recovered.release()
        assert reaper._reserved == limit - 1
    finally:
        for operation, _handle, _reservation in records:
            operation.final_event.set()
    _wait_for_reaper(reaper, pending_count=0, reserved_count=0)
    assert reaper.pending_count == 0
    assert all(handle.close_count == 1 for _, handle, _ in records)
    assert first_handle.close_count == 1


def test_real_reaper_duplicate_adopt_while_io_incomplete_keeps_single_owner(monkeypatch):
    attempts, _started = _count_reaper_worker_starts(monkeypatch)
    reaper = windows_ipc._PendingIoReaper(limit=1)
    operation = _LifecycleOperation(incomplete_results=1)
    api = _LifecycleApi()
    handle = _Handle()
    reservation = reaper.reserve()

    assert reaper.adopt(operation, handle, api, reservation)
    assert operation.reaper_waiting.wait(1)
    assert operation.incomplete_seen.is_set()
    assert reaper.adopt(operation, _Handle(), api, reservation) is False
    assert len(attempts) == 1
    assert reaper.pending_count == 1 and reaper._reserved == 1
    assert not operation.closed and handle.close_count == 0

    operation.final_event.set()
    _wait_for_reaper(reaper, pending_count=0, reserved_count=0)
    assert handle.close_count == 1


def test_real_reaper_recovers_proven_thread_start_failure_without_duplicate_worker(monkeypatch):
    attempts, started = _count_reaper_worker_starts(monkeypatch, fail_before_start=1)
    reaper = windows_ipc._PendingIoReaper(limit=1)
    operation = _LifecycleOperation()
    api = _LifecycleApi()
    handle = _Handle()
    reservation = reaper.reserve()

    assert reaper.adopt(operation, handle, api, reservation) is False
    assert operation.reaper_owned
    assert reaper.pending_count == 1 and reaper._reserved == 1
    assert _owner_record(operation).state == "start_failed"
    assert not operation.closed and handle.close_count == 0
    assert reaper.adopt(operation, _Handle(), api, reservation) is False
    assert len(attempts) == 1 and started == []
    assert reaper.pending_count == 1 and reaper._reserved == 1

    assert reaper.retry_start_failed(operation)
    assert len(attempts) == 2 and len(started) == 1
    assert operation.reaper_waiting.wait(1)
    operation.final_event.set()
    _wait_for_reaper(reaper, pending_count=0, reserved_count=0)
    assert handle.close_count == 1


def test_real_reaper_does_not_retry_ambiguous_thread_start_that_started(monkeypatch):
    attempts, started = _count_reaper_worker_starts(monkeypatch, raise_after_start=1)
    reaper = windows_ipc._PendingIoReaper(limit=1)
    operation = _LifecycleOperation()
    api = _LifecycleApi()
    handle = _Handle()
    reservation = reaper.reserve()

    assert reaper.adopt(operation, handle, api, reservation) is False
    assert operation.reaper_waiting.wait(1)
    assert len(attempts) == len(started) == 1
    assert _owner_record(operation).state in {"running_unknown", "reaping"}
    assert reaper.retry_start_failed(operation) is False
    assert len(attempts) == 1
    operation.final_event.set()
    _wait_for_reaper(reaper, pending_count=0, reserved_count=0)
    assert handle.close_count == 1


def test_real_reaper_handles_cancel_normal_completion_race_and_never_reuses_late_reply():
    reaper = windows_ipc._PendingIoReaper(limit=2)
    operation = _LifecycleOperation(
        waits=[False, True], incomplete_results=1, cancel_result="not_found",
        value=b"late reply",
    )
    api = _LifecycleApi(_Operation("write", value=3), operation)
    handle = _Handle()
    transport = windows_ipc.OverlappedPipeTransport(
        handle, timeout=0.01, api=api, reaper=reaper,
        cancel_drain_timeout_ms=1,
    )

    with pytest.raises(windows_ipc.PipeIoTimeout) as caught:
        transport.exchange(b"req")
    assert caught.value.cancel_state == "not_found:completion_pending"
    assert operation.reaper_waiting.wait(1)
    assert not operation.closed and handle.close_count == 0
    with pytest.raises(windows_ipc.PipeIoError, match="poisoned"):
        transport.exchange(b"next request")

    # Closing after ownership transfer cannot close the pipe handle out from
    # under the reaper. The eventual old response remains attached to no new
    # request and the old pipe is closed only after I/O completion.
    transport.close()
    assert handle.close_count == 0
    operation.final_event.set()
    _wait_for_reaper(reaper, pending_count=0, reserved_count=0)
    assert operation.closed
    assert handle.close_count == 1


def test_real_reaper_treats_operation_aborted_as_completed_and_releases_slot():
    reaper = windows_ipc._PendingIoReaper(limit=1)
    operation = _LifecycleOperation(final_error=_WinError(windows_ipc.ERROR_OPERATION_ABORTED))
    operation.final_event.set()
    handle = _Handle()
    reservation = reaper.reserve()

    reaper.adopt(operation, handle, _LifecycleApi(), reservation)

    _wait_for_reaper(reaper, pending_count=0, reserved_count=0)
    assert operation.closed
    assert handle.close_count == 1


def test_io_incomplete_after_signaled_wait_is_transferred_to_real_reaper():
    reaper = windows_ipc._PendingIoReaper(limit=1)
    operation = _LifecycleOperation(waits=[True, False], incomplete_results=1)
    api = _LifecycleApi(_Operation("write", value=3), operation)
    handle = _Handle()
    transport = windows_ipc.OverlappedPipeTransport(
        handle, timeout=0.5, api=api, reaper=reaper,
        cancel_drain_timeout_ms=1,
    )

    with pytest.raises(windows_ipc.PipeIoError, match="remained pending"):
        transport.exchange(b"req")
    assert operation.reaper_waiting.wait(1)
    assert not operation.closed and handle.close_count == 0
    assert reaper.pending_count == 1

    operation.final_event.set()
    _wait_for_reaper(reaper, pending_count=0, reserved_count=0)
    assert operation.closed and handle.close_count == 1


def test_real_reaper_enforces_32_pending_cap_and_releases_capacity_after_reap():
    reaper = windows_ipc._PendingIoReaper(limit=windows_ipc.MAX_PENDING_PIPE_IO)
    records = []
    for _ in range(windows_ipc.MAX_PENDING_PIPE_IO):
        operation = _LifecycleOperation()
        handle = _Handle()
        reservation = reaper.reserve()
        reaper.adopt(operation, handle, _LifecycleApi(), reservation)
        records.append((operation, handle))

    try:
        deadline = time.monotonic() + 2
        while reaper.pending_count < windows_ipc.MAX_PENDING_PIPE_IO and time.monotonic() < deadline:
            time.sleep(0.005)
        assert reaper.pending_count == windows_ipc.MAX_PENDING_PIPE_IO
        with pytest.raises(windows_ipc.PipeIoCapacityError):
            reaper.reserve()
        transport_handle = _Handle()
        transport = windows_ipc.OverlappedPipeTransport(
            transport_handle, timeout=0.5, api=_FakeApi(), reaper=reaper
        )
        with pytest.raises(windows_ipc.PipeIoCapacityError) as caught:
            transport.exchange(b"not sent")
        assert caught.value.request_may_have_been_sent is False
        assert transport.poisoned and transport_handle.close_count == 1

        # Reaping one completed operation returns exactly one slot while other
        # incomplete operations remain owned by the same reaper.
        records[0][0].final_event.set()
        deadline = time.monotonic() + 2
        while reaper.pending_count != windows_ipc.MAX_PENDING_PIPE_IO - 1 and time.monotonic() < deadline:
            time.sleep(0.005)
        assert reaper.pending_count == windows_ipc.MAX_PENDING_PIPE_IO - 1
        recovered = reaper.reserve()
        recovered.release()
    finally:
        for operation, _handle in records:
            operation.final_event.set()
    _wait_for_reaper(reaper, pending_count=0, reserved_count=0)
    assert all(operation.closed for operation, _handle in records)
    assert all(handle.close_count == 1 for _operation, handle in records)


def test_server_abort_after_request_poisons_connection():
    api = _FakeApi(
        _Operation("write", value=3),
        _Operation("read", pending=True, waits=[True],
                   error=OSError("synthetic server closed pipe")),
    )
    handle = _Handle()
    transport = windows_ipc.OverlappedPipeTransport(
        handle, timeout=0.5, api=api, reaper=_FakeReaper()
    )

    with pytest.raises(OSError, match="server closed"):
        transport.exchange(b"req")
    assert transport.poisoned
    assert handle.close_count == 1


def test_stale_generation_is_rejected_before_ipc_request(monkeypatch, tmp_path):
    current = TerminalIdentity(
        session_id="a" * 32, generation="b" * 32, supervisor_pid=41,
        supervisor_start="worker-start", shell_pid=42, shell_start="shell-start",
        shell_path="powershell.exe", name="synthetic", state="running",
    )
    stale = TerminalIdentity(
        session_id=current.session_id, generation="c" * 32, supervisor_pid=41,
        supervisor_start="worker-start", shell_pid=42, shell_start="shell-start",
        shell_path="powershell.exe", name="synthetic", state="running",
    )

    class _Transport:
        calls = 0

        def exchange(self, _payload):
            self.calls += 1
            raise AssertionError("stale identity must not reach IPC")

        def poison(self):
            pass

    client = object.__new__(windows_manager.TerminalClient)
    client._closed = False
    client._request_lock = threading.Lock()
    client._root = tmp_path
    client.identity = stale
    client._transport = _Transport()
    client.state, client.synced, client.lease = "running", True, True
    client.stop_outcome_unknown = False
    client.stop_failure_diagnostic = None
    monkeypatch.setattr(
        windows_manager, "_validate_identity",
        lambda *_args: (tmp_path, {
            "session_id": current.session_id, "generation": current.generation,
            "supervisor_pid": current.supervisor_pid,
            "supervisor_start": current.supervisor_start,
            "shell_pid": current.shell_pid, "shell_start": current.shell_start,
            "shell_path": current.shell_path, "state": "running",
        }),
    )

    with pytest.raises(TerminalError, match="stale_identity"):
        client._request("input")
    assert client._transport.calls == 0
    assert client.lease is False


def test_stop_unknown_preserves_registry_and_does_not_retry(monkeypatch, tmp_path):
    session_id = "a" * 32
    generation = "b" * 32
    session_dir = tmp_path / session_id
    session_dir.mkdir()
    metadata_path = session_dir / "session.json"
    original_metadata = b'{"fixture":"preserve-on-unknown"}\n'
    metadata_path.write_bytes(original_metadata)
    metadata = {
        "session_id": session_id, "generation": generation,
        "supervisor_pid": 41, "supervisor_start": "worker-start",
        "shell_pid": 42, "shell_start": "shell-start",
        "shell_path": "powershell.exe", "state": "running",
    }
    calls = {"request": 0, "cleanup": 0}

    class _FakeClient:
        identity = windows_manager._identity(metadata)
        stop_outcome_unknown = False
        stop_failure_diagnostic = {"stage": "read", "exception": "io_timeout"}

        def __init__(self, *_args, **_kwargs):
            pass

        def _request(self, operation):
            calls["request"] += 1
            assert operation == "stop"
            self.stop_outcome_unknown = True
            raise TerminalError("transport timeout")

        def close(self):
            pass

    registry = windows_manager.TerminalRegistry(tmp_path)
    monkeypatch.setattr(windows_manager, "_require_windows", lambda: None)
    monkeypatch.setattr(windows_manager, "_retry_confirmed_stop_cleanup", lambda *_a, **_k: False)
    monkeypatch.setattr(windows_manager, "_validate_identity", lambda *_a: (session_dir, metadata))
    monkeypatch.setattr(registry, "_connect_for_stop", lambda *_a, **_k: _FakeClient())
    monkeypatch.setattr(
        windows_manager, "_remove_session_files",
        lambda *_a, **_k: calls.__setitem__("cleanup", calls["cleanup"] + 1),
    )

    with pytest.raises(TerminalError, match="outcome is unknown; registry retained"):
        registry.stop(session_id)
    assert calls == {"request": 1, "cleanup": 0}
    assert metadata_path.read_bytes() == original_metadata


def test_stop_in_progress_is_unknown_and_does_not_resend(monkeypatch, tmp_path):
    session_id = "c" * 32
    generation = "d" * 32
    session_dir = tmp_path / session_id
    session_dir.mkdir()
    metadata_path = session_dir / "session.json"
    original_metadata = b'{"fixture":"stop-already-in-progress"}\n'
    metadata_path.write_bytes(original_metadata)
    metadata = {
        "session_id": session_id, "generation": generation,
        "supervisor_pid": 51, "supervisor_start": "worker-start",
        "shell_pid": 52, "shell_start": "shell-start",
        "shell_path": "powershell.exe", "state": "stopping",
    }

    registry = windows_manager.TerminalRegistry(tmp_path)
    monkeypatch.setattr(windows_manager, "_require_windows", lambda: None)
    monkeypatch.setattr(
        windows_manager, "_retry_confirmed_stop_cleanup", lambda *_a, **_k: False
    )
    monkeypatch.setattr(windows_manager, "_validate_identity", lambda *_a: (session_dir, metadata))
    monkeypatch.setattr(
        registry, "_connect_for_stop",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("an uncertain Stop must not be resent")
        ),
    )

    with pytest.raises(TerminalError, match="outcome is unknown; registry retained"):
        registry.stop(session_id)
    assert metadata_path.read_bytes() == original_metadata


def test_stop_read_timeout_is_unknown_and_never_resends(monkeypatch, tmp_path):
    identity = TerminalIdentity(
        session_id="e" * 32, generation="f" * 32, supervisor_pid=61,
        supervisor_start="worker-start", shell_pid=62, shell_start="shell-start",
        shell_path="powershell.exe", name="synthetic", state="running",
    )
    metadata = {
        "session_id": identity.session_id, "generation": identity.generation,
        "supervisor_pid": identity.supervisor_pid,
        "supervisor_start": identity.supervisor_start,
        "shell_pid": identity.shell_pid, "shell_start": identity.shell_start,
        "shell_path": identity.shell_path, "state": "running",
    }

    class _TimeoutTransport:
        close_error = None

        def __init__(self):
            self.calls = 0
            self.poison_calls = 0

        def exchange(self, _payload):
            self.calls += 1
            raise windows_ipc.PipeIoTimeout("read", "requested:completion_pending")

        def poison(self):
            self.poison_calls += 1

    client = object.__new__(windows_manager.TerminalClient)
    client._closed = False
    client._request_lock = threading.Lock()
    client._root = tmp_path
    client.identity = identity
    client._client_id = "test-client"
    client._transport = _TimeoutTransport()
    client.state, client.synced, client.lease = "running", True, False
    client.stop_outcome_unknown = False
    client.stop_failure_diagnostic = None
    monkeypatch.setattr(windows_manager, "_validate_identity", lambda *_a: (tmp_path, metadata))

    with pytest.raises(TerminalError, match="outcome is unknown; registry retained"):
        client._request("stop")
    assert client.stop_outcome_unknown
    assert client.state == "unavailable"
    assert client.synced is False and client.lease is False
    assert client.stop_failure_diagnostic == {
        "stage": "read", "exception": "io_timeout_requested_completion_pending",
    }
    assert client._transport.calls == 1 and client._transport.poison_calls == 1
    with pytest.raises(TerminalError, match="outcome is unknown; registry retained"):
        client._request("stop")
    assert client._transport.calls == 1


def _native_pipe_pair(server_action):
    """Create a current-user-only synthetic named pipe with a server thread."""
    import pywintypes
    import win32con
    import win32file
    import win32pipe
    import win32security

    pipe_name = r"\\.\pipe\jtt-ipc-test-" + uuid.uuid4().hex
    token = win32security.OpenProcessToken(
        __import__("win32api").GetCurrentProcess(), win32con.TOKEN_QUERY
    )
    try:
        sid = win32security.GetTokenInformation(token, win32security.TokenUser)[0]
    finally:
        token.Close()
    acl = win32security.ACL()
    acl.AddAccessAllowedAce(win32security.ACL_REVISION, win32con.GENERIC_ALL, sid)
    descriptor = win32security.SECURITY_DESCRIPTOR()
    descriptor.SetSecurityDescriptorOwner(sid, False)
    descriptor.SetSecurityDescriptorDacl(True, acl, False)
    security = pywintypes.SECURITY_ATTRIBUTES()
    security.SECURITY_DESCRIPTOR = descriptor
    security.bInheritHandle = False
    server = win32pipe.CreateNamedPipe(
        pipe_name, win32pipe.PIPE_ACCESS_DUPLEX,
        win32pipe.PIPE_TYPE_MESSAGE | win32pipe.PIPE_READMODE_MESSAGE | win32pipe.PIPE_WAIT
        | getattr(win32pipe, "PIPE_REJECT_REMOTE_CLIENTS", 0x00000008),
        1, 65536, 65536, 1000, security,
    )
    accepted = threading.Event()
    server_errors = []

    def run_server():
        try:
            try:
                win32pipe.ConnectNamedPipe(server, None)
            except pywintypes.error as exc:
                if exc.winerror != 535:  # ERROR_PIPE_CONNECTED
                    raise
            accepted.set()
            server_action(server, win32file)
        except BaseException as exc:
            server_errors.append(type(exc).__name__)
        finally:
            try:
                server.Close()
            except Exception:
                pass

    thread = threading.Thread(target=run_server, name="jtt-test-owned-pipe-server")
    thread.start()
    client = None
    setup_complete = False
    try:
        win32pipe.WaitNamedPipe(pipe_name, 3000)
        client = win32file.CreateFile(
            pipe_name, win32con.GENERIC_READ | win32con.GENERIC_WRITE, 0,
            None, win32con.OPEN_EXISTING,
            getattr(win32con, "FILE_FLAG_OVERLAPPED", 0x40000000), None,
        )
        win32pipe.SetNamedPipeHandleState(client, win32pipe.PIPE_READMODE_MESSAGE, None, None)
        if not accepted.wait(3):
            raise AssertionError("synthetic pipe server did not accept test client")
        setup_complete = True
    finally:
        if not setup_complete:
            if client is not None:
                try:
                    client.Close()
                except Exception:
                    pass
            try:
                server.Close()
            except Exception:
                pass
            thread.join(2)
    return client, thread, server_errors


def _wait_for_native_reaper(initial_count, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if windows_ipc._DEFAULT_REAPER.pending_count <= initial_count:
            return
        time.sleep(0.01)
    raise AssertionError("synthetic pipe I/O completion was not reaped")


def _server_echo(server, win32file):
    _status, request = win32file.ReadFile(server, 4096)
    win32file.WriteFile(server, b"reply:" + request)


@pytest.mark.skipif(__import__("sys").platform != "win32", reason="native Windows named pipe required")
def test_native_synthetic_pipe_normal_request_response():
    client, thread, errors = _native_pipe_pair(_server_echo)
    transport = windows_ipc.OverlappedPipeTransport(client, timeout=1.0)
    try:
        assert transport.exchange(b"request") == b"reply:request"
    finally:
        transport.close()
        thread.join(2)
    assert not thread.is_alive()
    assert errors == []


@pytest.mark.skipif(__import__("sys").platform != "win32", reason="native Windows named pipe required")
def test_native_synthetic_pipe_server_silence_times_out_and_cancels():
    pending_before = windows_ipc._DEFAULT_REAPER.pending_count
    release_server = threading.Event()

    def silent_server(server, win32file):
        win32file.ReadFile(server, 4096)
        release_server.wait(2)

    client, thread, errors = _native_pipe_pair(silent_server)
    transport = windows_ipc.OverlappedPipeTransport(
        client, timeout=0.1, cancel_drain_timeout_ms=25
    )
    try:
        with pytest.raises(windows_ipc.PipeIoTimeout, match="read") as caught:
            transport.exchange(b"request")
        cancel_request_state, io_completion_state = caught.value.cancel_state.rsplit(":", 1)
        assert cancel_request_state in {"requested", "not_found"}, caught.value.cancel_state
        assert io_completion_state in {
            "completed", "completed_aborted", "completion_pending",
        }, caught.value.cancel_state
        assert transport.poisoned
        with pytest.raises(windows_ipc.PipeIoError, match="poisoned"):
            transport.exchange(b"next request")
    finally:
        release_server.set()
        transport.close()
        thread.join(3)
    assert not thread.is_alive()
    assert errors == []
    _wait_for_native_reaper(pending_before)


@pytest.mark.skipif(__import__("sys").platform != "win32", reason="native Windows named pipe required")
def test_native_synthetic_pipe_server_abort_after_request_poisons_client():
    def abort_server(server, win32file):
        win32file.ReadFile(server, 4096)

    client, thread, errors = _native_pipe_pair(abort_server)
    transport = windows_ipc.OverlappedPipeTransport(client, timeout=1.0)
    try:
        with pytest.raises(OSError):
            transport.exchange(b"request")
        assert transport.poisoned
    finally:
        transport.close()
        thread.join(2)
    assert not thread.is_alive()
    assert errors == []


@pytest.mark.skipif(__import__("sys").platform != "win32", reason="native Windows named pipe required")
def test_native_synthetic_pipe_response_before_deadline_is_delivered():
    def near_deadline_server(server, win32file):
        _status, request = win32file.ReadFile(server, 4096)
        time.sleep(0.35)
        win32file.WriteFile(server, b"reply:" + request)

    client, thread, errors = _native_pipe_pair(near_deadline_server)
    transport = windows_ipc.OverlappedPipeTransport(client, timeout=0.5)
    try:
        assert transport.exchange(b"request") == b"reply:request"
    finally:
        transport.close()
        thread.join(2)
    assert not thread.is_alive()
    assert errors == []


@pytest.mark.skipif(__import__("sys").platform != "win32", reason="native Windows named pipe required")
def test_native_synthetic_pipe_late_response_after_timeout_is_never_reused():
    pending_before = windows_ipc._DEFAULT_REAPER.pending_count
    release_response = threading.Event()

    def late_server(server, win32file):
        _status, request = win32file.ReadFile(server, 4096)
        release_response.wait(2)
        try:
            win32file.WriteFile(server, b"late:" + request)
        except Exception:
            # A client-side cancellation/disconnect is an expected test outcome.
            pass

    client, thread, errors = _native_pipe_pair(late_server)
    transport = windows_ipc.OverlappedPipeTransport(
        client, timeout=0.1, cancel_drain_timeout_ms=10
    )
    try:
        with pytest.raises(windows_ipc.PipeIoTimeout):
            transport.exchange(b"request")
        release_response.set()
        with pytest.raises(windows_ipc.PipeIoError, match="poisoned"):
            transport.exchange(b"next request")
    finally:
        release_response.set()
        transport.close()
        thread.join(3)
    assert not thread.is_alive()
    assert errors == []
    _wait_for_native_reaper(pending_before)
