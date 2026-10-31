"""Bounded, single-flight overlapped transport for the Windows named pipe.

The transport deliberately poisons a connection after any ambiguous I/O result.
It never attempts to match a late response to a later request on that pipe.
"""
from __future__ import annotations

import ctypes
import math
import threading
import time
from typing import Any


ERROR_IO_PENDING = 997
ERROR_IO_INCOMPLETE = 996
ERROR_OPERATION_ABORTED = 995
ERROR_BROKEN_PIPE = 109
ERROR_NO_DATA = 232
ERROR_PIPE_NOT_CONNECTED = 233
ERROR_MORE_DATA = 234
ERROR_HANDLE_EOF = 38
WAIT_OBJECT_0 = 0
WAIT_TIMEOUT = 258
WAIT_FAILED = 0xFFFFFFFF
INFINITE = 0xFFFFFFFF
FILE_FLAG_OVERLAPPED = 0x40000000
MAX_PENDING_PIPE_IO = 32
CANCEL_DRAIN_TIMEOUT_MS = 200


class PipeIoError(RuntimeError):
    """The request could not be completed with a trustworthy response."""


class PipeIoTimeout(PipeIoError):
    def __init__(self, stage: str, cancel_state: str):
        super().__init__("named pipe {} timed out".format(stage))
        self.stage = stage
        self.cancel_state = cancel_state


class PipeIoCapacityError(PipeIoError):
    """Too many earlier operations are still awaiting OS completion."""

    def __init__(self, message: str):
        super().__init__(message)
        self.request_may_have_been_sent = False


class _IoUnresolved(PipeIoError):
    """The operation's terminal state is not yet safe to release."""


class _IoIncomplete(_IoUnresolved):
    """GetOverlappedResult reported that the operation is still pending."""


class _IoCompletionUnknown(_IoUnresolved):
    """A Win32 result error did not establish operation completion."""

    def __init__(self, error_code: int):
        super().__init__("named pipe I/O completion is unknown")
        self.error_code = error_code


def _error_code_from_exception(exc: BaseException) -> int | None:
    code = getattr(exc, "winerror", None)
    return int(code) if isinstance(code, int) else None


class _OverlappedUnion(ctypes.Union):
    _fields_ = [
        ("Offset", ctypes.c_uint32),
        ("OffsetHigh", ctypes.c_uint32),
        ("Pointer", ctypes.c_void_p),
    ]


class _Overlapped(ctypes.Structure):
    _anonymous_ = ("position",)
    _fields_ = [
        ("Internal", ctypes.c_size_t),
        ("InternalHigh", ctypes.c_size_t),
        ("position", _OverlappedUnion),
        ("hEvent", ctypes.c_void_p),
    ]


class _NativeOperation:
    __slots__ = ("direction", "handle", "event", "overlapped", "buffer",
                 "pending", "transferred", "closed", "cancel_state",
                 "reaper_owned", "reaper_completed", "dispose_attempted")

    def __init__(self, direction: str, handle: int, event: int,
                 overlapped: _Overlapped, buffer: Any):
        self.direction = direction
        self.handle = handle
        self.event = event
        self.overlapped = overlapped
        self.buffer = buffer
        self.pending = False
        self.transferred = 0
        self.closed = False
        self.cancel_state = "not_requested"
        self.reaper_owned = False
        self.reaper_completed = False
        self.dispose_attempted = False


class _NativeApi:
    """Small ctypes binding to the native APIs needed by pywin32 handles."""

    def __init__(self):
        if not hasattr(ctypes, "WinDLL"):
            raise OSError("native overlapped pipe I/O requires Windows")
        from ctypes import wintypes

        self._wintypes = wintypes
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._kernel32.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL,
                                                wintypes.BOOL, wintypes.LPCWSTR]
        self._kernel32.CreateEventW.restype = wintypes.HANDLE
        self._kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        self._kernel32.WaitForSingleObject.restype = wintypes.DWORD
        self._kernel32.WriteFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p,
                                             wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
                                             ctypes.POINTER(_Overlapped)]
        self._kernel32.WriteFile.restype = wintypes.BOOL
        self._kernel32.ReadFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p,
                                            wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
                                            ctypes.POINTER(_Overlapped)]
        self._kernel32.ReadFile.restype = wintypes.BOOL
        self._kernel32.GetOverlappedResult.argtypes = [
            wintypes.HANDLE, ctypes.POINTER(_Overlapped),
            ctypes.POINTER(wintypes.DWORD), wintypes.BOOL,
        ]
        self._kernel32.GetOverlappedResult.restype = wintypes.BOOL
        self._kernel32.CancelIoEx.argtypes = [wintypes.HANDLE, ctypes.POINTER(_Overlapped)]
        self._kernel32.CancelIoEx.restype = wintypes.BOOL
        self._kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        self._kernel32.CloseHandle.restype = wintypes.BOOL

    @staticmethod
    def _handle_value(handle: Any) -> int:
        # pywintypes.HANDLE documents both .handle and int(handle). Prefer the
        # documented property; integer handles are accepted for native tests.
        value = getattr(handle, "handle", handle)
        return int(value)

    def begin(self, handle: Any, direction: str, value: Any) -> _NativeOperation:
        raw_handle = self._handle_value(handle)
        if direction == "write":
            buffer = ctypes.create_string_buffer(value, len(value))
            length = len(value)
        elif direction == "read":
            buffer = ctypes.create_string_buffer(int(value))
            length = int(value)
        else:
            raise ValueError("unknown pipe I/O direction")
        event = self._kernel32.CreateEventW(None, True, False, None)
        if not event:
            raise ctypes.WinError(ctypes.get_last_error())
        event_value = event if isinstance(event, int) else event.value
        overlapped = _Overlapped()
        overlapped.hEvent = event_value
        operation = _NativeOperation(direction, raw_handle, event_value,
                                     overlapped, buffer)
        function = self._kernel32.WriteFile if direction == "write" else self._kernel32.ReadFile
        try:
            # For FILE_FLAG_OVERLAPPED handles the byte-count pointer is NULL.
            # The OVERLAPPED result field supplies the count if this call
            # completes synchronously; GetOverlappedResult is for pending I/O.
            ok = function(raw_handle, ctypes.byref(buffer), length,
                          None, ctypes.byref(overlapped))
        except BaseException:
            self.dispose(operation)
            raise
        if ok:
            # For overlapped ReadFile/WriteFile, InternalHigh is the byte count
            # when the operation completes before the initiating call returns.
            # GetOverlappedResult is only for operations that returned
            # ERROR_IO_PENDING.
            operation.transferred = int(overlapped.InternalHigh)
            return operation
        error = ctypes.get_last_error()
        if error == ERROR_IO_PENDING:
            operation.pending = True
            return operation
        self.dispose(operation)
        raise ctypes.WinError(error)

    def wait(self, operation: _NativeOperation, timeout_ms: int) -> bool:
        result = self._kernel32.WaitForSingleObject(operation.event, timeout_ms)
        if result == WAIT_OBJECT_0:
            return True
        if result == WAIT_TIMEOUT:
            return False
        if result == WAIT_FAILED:
            error = ctypes.get_last_error() or 6  # ERROR_INVALID_HANDLE
        else:
            # WaitForSingleObject has no last-error contract for other status
            # values; do not report a stale thread-local error from an earlier
            # Win32 call as the cause.
            error = 31  # ERROR_GEN_FAILURE
        raise ctypes.WinError(error)

    def result(self, operation: _NativeOperation, *, wait: bool = False) -> Any:
        if not operation.pending:
            transferred = operation.transferred
            if operation.direction == "read":
                return operation.buffer.raw[:transferred]
            return transferred
        transferred = self._wintypes.DWORD(0)
        ok = self._kernel32.GetOverlappedResult(
            operation.handle, ctypes.byref(operation.overlapped),
            ctypes.byref(transferred), bool(wait),
        )
        if not ok:
            error = ctypes.get_last_error()
            if error == ERROR_IO_INCOMPLETE:
                raise _IoIncomplete("named pipe I/O is still incomplete")
            if error not in {
                ERROR_OPERATION_ABORTED,
                ERROR_BROKEN_PIPE,
                ERROR_NO_DATA,
                ERROR_PIPE_NOT_CONNECTED,
                ERROR_MORE_DATA,
                ERROR_HANDLE_EOF,
            }:
                raise _IoCompletionUnknown(error)
            operation.pending = False
            raise ctypes.WinError(error)
        operation.transferred = int(transferred.value)
        operation.pending = False
        if operation.direction == "read":
            return operation.buffer.raw[:operation.transferred]
        return operation.transferred

    def cancel(self, operation: _NativeOperation) -> str:
        ok = self._kernel32.CancelIoEx(
            operation.handle, ctypes.byref(operation.overlapped)
        )
        if ok:
            return "requested"
        error = ctypes.get_last_error()
        return "not_found" if error == 1168 else "failed:{}".format(error)

    def dispose(self, operation: _NativeOperation) -> None:
        if operation.closed:
            return
        if operation.event:
            if not self._kernel32.CloseHandle(operation.event):
                raise ctypes.WinError(ctypes.get_last_error() or 6)
            operation.event = 0
        operation.closed = True

    def reap(self, operation: _NativeOperation) -> None:
        # The reaper owns all referenced objects until the kernel signals the
        # exact OVERLAPPED operation. No buffer, event, or pipe handle is freed
        # merely because CancelIoEx returned.
        if not self.wait(operation, INFINITE):
            raise OSError("pending named-pipe I/O did not signal completion")
        try:
            self.result(operation)
        except _IoUnresolved:
            # A signaled event is not enough if the completion query still
            # reports pending. Keep all operation storage alive while the
            # blocking completion query establishes the terminal result.
            try:
                self.result(operation, wait=True)
            except _IoUnresolved:
                # Fail closed: _PendingIoReaper retains the operation and its
                # capacity reservation when completion cannot be established.
                raise
            except OSError:
                # With bWait=True, a terminal I/O error (including
                # ERROR_OPERATION_ABORTED) still proves the operation ended.
                pass
        except OSError:
            # A signaled operation is complete even when its final status is
            # ERROR_OPERATION_ABORTED or another I/O error.
            pass
        self.dispose(operation)


class _Reservation:
    def __init__(self, reaper: "_PendingIoReaper"):
        self._reaper = reaper
        self._released = False
        self._release_state = "reserved"
        self._operation = None
        self._lock = threading.Lock()

    def _claim(self, operation: Any) -> bool:
        """Bind this capacity token to one operation exactly once."""
        with self._lock:
            if self._release_state != "reserved":
                return False
            if self._operation is None:
                self._operation = operation
                return True
            return self._operation is operation

    def _release_if_unbound(self, operation: Any = None) -> bool:
        """Return an unused duplicate token, without touching I/O resources."""
        with self._lock:
            if self._released:
                return False
            if self._release_state != "reserved":
                return False
            if self._operation is operation and operation is not None:
                return False
            if self._operation is not None:
                raise RuntimeError("pending I/O reservation belongs to another operation")
        return True

    def _release_owned(self, operation: Any) -> None:
        self._release(operation)

    def release(self) -> None:
        self._release(None)

    def _release(self, operation: Any) -> None:
        with self._lock:
            if self._released:
                return
            if self._release_state != "reserved":
                raise RuntimeError("pending I/O reservation release state is unknown")
            if self._operation is not operation:
                if operation is None and self._operation is not None:
                    raise RuntimeError("pending I/O reservation is owned by its reaper")
                raise RuntimeError("pending I/O reservation ownership changed")
        try:
            self._reaper._release(self, operation)
        except BaseException:
            with self._lock:
                if self._release_state != "released":
                    self._release_state = "unknown"
            raise


class _ReaperRecord:
    __slots__ = ("operation", "pipe_handle", "api", "reservation", "reaper",
                 "thread", "state")

    def __init__(self, operation: Any, pipe_handle: Any, api: Any,
                 reservation: _Reservation, reaper: "_PendingIoReaper"):
        self.operation = operation
        self.pipe_handle = pipe_handle
        self.api = api
        self.reservation = reservation
        self.reaper = reaper
        self.thread = None
        self.state = "claimed"


# A single operation can be presented to more than one reaper instance. Keep a
# strong reference in each record so id(operation) cannot be recycled while
# ownership is outstanding. All cross-instance claims and removals use this
# lock; it is always acquired before an instance lock.
_REAPER_OWNERSHIP_LOCK = threading.Lock()
_REAPER_OWNERS: dict[int, _ReaperRecord] = {}


class _PendingIoReaper:
    """Retain timed-out native I/O until the kernel confirms completion."""

    def __init__(self, limit: int = MAX_PENDING_PIPE_IO):
        self._limit = limit
        self._lock = threading.Lock()
        self._reserved = 0
        self._unreaped: dict[int, _ReaperRecord] = {}

    def reserve(self) -> _Reservation:
        with self._lock:
            if self._reserved >= self._limit:
                raise PipeIoCapacityError("too many pending named pipe operations")
            self._reserved += 1
        return _Reservation(self)

    @property
    def pending_count(self) -> int:
        with self._lock:
            return len(self._unreaped)

    def _release(self, reservation: _Reservation, operation: Any = None) -> bool:
        with self._lock:
            with reservation._lock:
                if reservation._reaper is not self:
                    raise RuntimeError("pending I/O reservation belongs to another reaper")
                if reservation._release_state == "released":
                    return False
                if reservation._release_state != "reserved":
                    raise RuntimeError("pending I/O reservation release state is unknown")
                if reservation._operation is not operation:
                    if operation is None and reservation._operation is not None:
                        raise RuntimeError("pending I/O reservation is owned by its reaper")
                    raise RuntimeError("pending I/O reservation ownership changed")
                if self._reserved <= 0:
                    raise RuntimeError("pending I/O reservation count underflow")
                reservation._release_state = "releasing"
                self._reserved -= 1
                reservation._released = True
                reservation._operation = None
                reservation._release_state = "released"
        return True

    def _start_record_locked(self, key: int, record: _ReaperRecord) -> bool:
        """Start the sole worker while ownership is still globally locked."""
        try:
            thread = threading.Thread(
                target=self._reap_record, args=(key, record),
                name="jtt-pipe-io-reaper", daemon=True,
            )
        except BaseException:
            record.state = "start_failed"
            return False
        record.thread = thread
        record.state = "starting"
        try:
            thread.start()
        except BaseException:
            # Some Thread implementations can raise after the OS thread has
            # started. Such a result is ambiguous and must never be retried.
            try:
                started = thread.ident is not None or thread.is_alive()
            except BaseException:
                started = True
            record.state = "running_unknown" if started else "start_failed"
            return False
        record.state = "running"
        return True

    def _reap_record(self, key: int, record: _ReaperRecord) -> None:
        with _REAPER_OWNERSHIP_LOCK:
            if _REAPER_OWNERS.get(key) is not record:
                return
            record.state = "reaping"

        try:
            record.api.reap(record.operation)
        except BaseException:
            # Completion, disposal, and handle state are not fully known.
            # Keep the owner record and capacity reservation for evidence and
            # to prevent any second reaper from taking these same resources.
            with _REAPER_OWNERSHIP_LOCK:
                if _REAPER_OWNERS.get(key) is record:
                    record.state = "retained_after_reap_error"
            return

        with _REAPER_OWNERSHIP_LOCK:
            if _REAPER_OWNERS.get(key) is not record:
                return
            record.state = "io_completed"
            # A completed operation must remain ineligible for adoption even
            # after its owner record is removed.
            try:
                record.operation.reaper_completed = True
            except BaseException:
                record.state = "retained_without_completion_marker"
                return

        try:
            close = (getattr(record.pipe_handle, "Close", None)
                     or getattr(record.pipe_handle, "close", None))
            if close is not None:
                close()
        except BaseException:
            # Do not retry an ambiguous native close. Retain the record and
            # reservation, and never let another owner close the same handle.
            with _REAPER_OWNERSHIP_LOCK:
                if _REAPER_OWNERS.get(key) is record:
                    record.state = "retained_after_handle_close_error"
            return

        with _REAPER_OWNERSHIP_LOCK:
            if _REAPER_OWNERS.get(key) is not record:
                return
            with self._lock:
                self._unreaped.pop(key, None)
            record.state = "completed"
            del _REAPER_OWNERS[key]
        record.reservation._release_owned(record.operation)

    def adopt(self, operation: Any, pipe_handle: Any, api: Any,
              reservation: _Reservation) -> bool:
        """Transfer a pending operation to exactly one reaper.

        Returns True only when this call starts the owning worker. Duplicate
        calls return False and never dispose or close the already-owned I/O.
        """
        key = id(operation)
        release_duplicate = False
        with _REAPER_OWNERSHIP_LOCK:
            existing = _REAPER_OWNERS.get(key)
            if existing is not None:
                if existing.operation is not operation:
                    raise RuntimeError("pending I/O identity collision")
                if reservation is existing.reservation:
                    if not reservation._claim(operation):
                        raise RuntimeError("owned pending I/O reservation is invalid")
                else:
                    release_duplicate = reservation._release_if_unbound(operation)
                # Crucially, do not replace the record, close resources, or
                # start another thread on this path.
                record = None
            elif getattr(operation, "reaper_completed", False):
                release_duplicate = reservation._release_if_unbound(operation)
                record = None
            else:
                if reservation._reaper is not self:
                    raise RuntimeError("pending I/O reservation belongs to another reaper")
                if not reservation._claim(operation):
                    raise RuntimeError("pending I/O reservation is already bound or released")
                record = _ReaperRecord(operation, pipe_handle, api, reservation, self)
                with self._lock:
                    self._unreaped[key] = record
                _REAPER_OWNERS[key] = record
                try:
                    operation.reaper_owned = True
                except BaseException:
                    # The global and local owner records already retain every
                    # resource. Do not start a thread if ownership cannot be
                    # represented on the operation object.
                    record.state = "retained_without_owner_marker"
                    return False
                started = self._start_record_locked(key, record)
                return started

        if release_duplicate:
            # Release duplicate capacity after leaving the global ownership lock.
            reservation.release()
        return False

    def retry_start_failed(self, operation: Any) -> bool:
        """Explicitly retry a worker that is proven not to have started."""
        key = id(operation)
        with _REAPER_OWNERSHIP_LOCK:
            record = _REAPER_OWNERS.get(key)
            if (record is None or record.operation is not operation
                    or record.reaper is not self or record.state != "start_failed"):
                return False
            return self._start_record_locked(key, record)


_DEFAULT_REAPER = _PendingIoReaper()


class OverlappedPipeTransport:
    """Single-flight, deadline-bound request/reply transport.

    The API and reaper are injectable so the state transitions can be tested
    on non-Windows hosts without pretending to validate native pipe behavior.
    """

    def __init__(self, pipe_handle: Any, *, timeout: float,
                 api: Any = None, reaper: Any = None,
                 cancel_drain_timeout_ms: int = CANCEL_DRAIN_TIMEOUT_MS,
                 max_read_bytes: int = 896 * 1024):
        timeout = float(timeout)
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("pipe timeout must be positive")
        self._pipe_handle = pipe_handle
        self._timeout = min(timeout, 30.0)
        self._api = api if api is not None else _NativeApi()
        self._reaper = reaper if reaper is not None else _DEFAULT_REAPER
        self._cancel_drain_timeout_ms = int(cancel_drain_timeout_ms)
        self._max_read_bytes = int(max_read_bytes)
        self._lock = threading.Lock()
        self._poisoned = False
        self.close_error = None

    @property
    def poisoned(self) -> bool:
        return self._poisoned

    def exchange(self, payload: bytes) -> bytes:
        if not isinstance(payload, bytes) or not payload:
            raise ValueError("pipe request must be non-empty bytes")
        with self._lock:
            if self._poisoned or self._pipe_handle is None:
                raise PipeIoError("named pipe connection is poisoned")
            deadline = time.monotonic() + self._timeout
            write_completed = False
            try:
                written = self._perform("write", payload, deadline)
                if type(written) is not int or written != len(payload):
                    raise PipeIoError("named pipe request write was incomplete")
                write_completed = True
                response = self._perform("read", self._max_read_bytes, deadline)
                if not isinstance(response, bytes):
                    raise PipeIoError("named pipe response is invalid")
                return response
            except PipeIoCapacityError as exc:
                exc.request_may_have_been_sent = write_completed
                self._poison_and_close()
                raise
            except BaseException:
                self._poison_and_close()
                raise

    def _perform(self, direction: str, value: Any, deadline: float) -> Any:
        reservation = self._reaper.reserve()
        operation = None
        try:
            operation = self._api.begin(self._pipe_handle, direction, value)
            if not operation.pending:
                try:
                    return self._api.result(operation)
                finally:
                    self._dispose_and_release(operation, reservation)
            remaining_ms = max(0, int((deadline - time.monotonic()) * 1000))
            try:
                completed = remaining_ms > 0 and self._api.wait(operation, remaining_ms)
            except BaseException:
                self._cancel_or_reap(operation, reservation)
                raise
            if completed:
                try:
                    result = self._api.result(operation)
                except _IoUnresolved as unresolved:
                    self._cancel_or_reap(operation, reservation)
                    if isinstance(unresolved, _IoIncomplete):
                        raise PipeIoError(
                            "named pipe I/O remained pending after completion signal"
                        )
                    raise PipeIoError(
                        "named pipe I/O completion state is unknown"
                    )
                except BaseException:
                    # Other errors after a signaled event are terminal I/O
                    # statuses; unresolved results take the reaper path above.
                    self._dispose_and_release(operation, reservation)
                    raise
                else:
                    self._dispose_and_release(operation, reservation)
                    return result
            self._cancel_or_reap(operation, reservation)
            raise PipeIoTimeout(direction, getattr(operation, "cancel_state", "unknown"))
        except PipeIoTimeout:
            raise
        except BaseException:
            if operation is None:
                reservation.release()
            elif (not operation.pending
                  and not getattr(operation, "closed", False)
                  and not getattr(operation, "dispose_attempted", False)):
                self._dispose_and_release(operation, reservation)
            raise

    def _dispose_and_release(self, operation: Any,
                             reservation: _Reservation) -> None:
        """Attempt disposal once, return its reservation, and report uncertainty."""
        if getattr(operation, "dispose_attempted", False):
            raise PipeIoError("named pipe operation disposal was already attempted")
        operation.dispose_attempted = True

        try:
            self._api.dispose(operation)
        except BaseException as dispose_error:
            release_error = None
            try:
                reservation.release()
            except BaseException as exc:
                release_error = exc
            self._attach_cleanup_state(
                dispose_error, operation, reservation,
                dispose_error=dispose_error, release_error=release_error,
            )
            if release_error is not None and hasattr(dispose_error, "add_note"):
                dispose_error.add_note(
                    "reservation release also failed ({})".format(
                        type(release_error).__name__
                    )
                )
            raise

        try:
            reservation.release()
        except BaseException as release_error:
            self._attach_cleanup_state(
                release_error, operation, reservation,
                release_error=release_error,
            )
            raise

    @staticmethod
    def _attach_cleanup_state(error: BaseException, operation: Any,
                              reservation: _Reservation, *,
                              dispose_error: BaseException | None = None,
                              release_error: BaseException | None = None) -> None:
        reaper = reservation._reaper
        lock = getattr(reaper, "_lock", None)
        if lock is None:
            reserved_slots = getattr(reaper, "_reserved", None)
        else:
            with lock:
                reserved_slots = getattr(reaper, "_reserved", None)
        state = {
            "direction": getattr(operation, "direction", "unknown"),
            "io_completion": "unknown" if getattr(operation, "pending", True) else "confirmed",
            "dispose_attempted": bool(getattr(operation, "dispose_attempted", False)),
            "operation_closed": bool(getattr(operation, "closed", False)),
            "event_handle_state": "closed" if getattr(operation, "closed", False) else "unknown",
            "reservation_release_attempted": True,
            "reservation_release_state": getattr(reservation, "_release_state", "unknown"),
            "reservation_token_consumed": getattr(reservation, "_release_state", "unknown") != "reserved",
            "reserved_slots": reserved_slots,
            "dispose_error_category": (type(dispose_error).__name__
                                       if dispose_error is not None else None),
            "reservation_error_category": (type(release_error).__name__
                                           if release_error is not None else None),
        }
        try:
            error.cleanup_state = state
        except BaseException:
            # Preserve the original cleanup exception if it cannot carry metadata.
            pass

    def _cancel_or_reap(self, operation: Any, reservation: _Reservation) -> None:
        try:
            operation.cancel_state = self._api.cancel(operation)
        except BaseException:
            operation.cancel_state = "failed"
        try:
            drained = self._api.wait(operation, self._cancel_drain_timeout_ms)
        except BaseException:
            drained = False
        if drained:
            try:
                self._api.result(operation)
            except _IoUnresolved:
                operation.cancel_state = operation.cancel_state + ":completion_pending"
                self._adopt_pending(operation, reservation)
                return
            except BaseException as exc:
                # Signaled means the request has completed. Preserve its final
                # status as diagnostic metadata; never reuse the connection.
                if _error_code_from_exception(exc) == ERROR_OPERATION_ABORTED:
                    operation.cancel_state = operation.cancel_state + ":completed_aborted"
                else:
                    operation.cancel_state = operation.cancel_state + ":completed_with_error"
            else:
                operation.cancel_state = operation.cancel_state + ":completed"
            self._dispose_and_release(operation, reservation)
            return
        operation.cancel_state = operation.cancel_state + ":completion_pending"
        self._adopt_pending(operation, reservation)

    def _adopt_pending(self, operation: Any, reservation: _Reservation) -> None:
        handle = self._pipe_handle
        self._pipe_handle = None
        operation.reaper_owned = True
        self._reaper.adopt(operation, handle, self._api, reservation)

    def _poison_and_close(self) -> None:
        self._poisoned = True
        handle, self._pipe_handle = self._pipe_handle, None
        if handle is not None:
            close = getattr(handle, "Close", None) or getattr(handle, "close", None)
            if close is not None:
                try:
                    close()
                except Exception as exc:
                    self.close_error = type(exc).__name__

    def poison(self) -> None:
        with self._lock:
            self._poison_and_close()

    def close(self) -> None:
        with self._lock:
            self._poison_and_close()
