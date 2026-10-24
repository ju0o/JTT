"""Bounded read-only observers for native Windows terminal cards."""

from __future__ import annotations

import queue
import threading
from typing import Any

from ..terminal.manager import TerminalIdentity, TerminalScreen


MAX_CARD_SAMPLES = 16
MAX_SCREEN_ROWS = 3
MAX_SCREEN_COLUMNS = 64


def identity_key(identity: TerminalIdentity) -> tuple[str, str, int, str, int, str]:
    return (
        identity.session_id, identity.generation,
        identity.supervisor_pid, identity.supervisor_start,
        identity.shell_pid, identity.shell_start,
    )


def _clip_screen(screen: TerminalScreen | None) -> TerminalScreen | None:
    if screen is None:
        return None
    rows = tuple(tuple(row[:MAX_SCREEN_COLUMNS]) for row in screen.rows[:MAX_SCREEN_ROWS])
    return TerminalScreen(rows, int(screen.cursor_x), int(screen.cursor_y),
                          bool(screen.cursor_visible))


class WindowsMiniLiveSampler:
    """Run one bounded observer thread; it never acquires leases or resizes."""

    def __init__(self) -> None:
        self._requests: queue.Queue[Any] = queue.Queue(maxsize=1)
        self._results: queue.Queue[Any] = queue.Queue(maxsize=1)
        self._lock = threading.Lock()
        self._busy = False
        self._stopping = False
        self._thread = threading.Thread(
            target=self._run, name="jtt-windows-mini-live", daemon=True,
        )
        self._thread.start()

    @property
    def busy(self) -> bool:
        with self._lock:
            return self._busy

    def submit(self, registry, token: tuple, targets: tuple[TerminalIdentity, ...]) -> bool:
        if not targets:
            return False
        with self._lock:
            if self._busy or self._stopping:
                return False
            self._busy = True
            try:
                self._requests.put_nowait(
                    (registry, token, tuple(targets[:MAX_CARD_SAMPLES]))
                )
            except queue.Full:
                self._busy = False
                return False
            return True

    def take(self):
        try:
            return self._results.get_nowait()
        except queue.Empty:
            return None

    def close(self) -> None:
        with self._lock:
            self._stopping = True
            try:
                self._requests.get_nowait()
            except queue.Empty:
                pass
            try:
                self._requests.put_nowait(None)
            except queue.Full:
                try:
                    self._requests.get_nowait()
                except queue.Empty:
                    pass
                try:
                    self._requests.put_nowait(None)
                except queue.Full:
                    pass
        self._thread.join(timeout=0.5)

    def _run(self) -> None:
        while True:
            request = self._requests.get()
            if request is None:
                return
            registry, token, targets = request
            samples = []
            try:
                for expected in targets:
                    if self._stopping:
                        break
                    sample = {
                        "session_id": expected.session_id,
                        "identity": identity_key(expected),
                        "sequence": None,
                        "screen": None,
                        "synced": False,
                    }
                    client = None
                    try:
                        client = registry.connect(expected.session_id, timeout=0.25)
                        if identity_key(client.identity) == sample["identity"]:
                            observation = client.observe()
                            valid = (
                                observation.synced
                                and observation.session_id == expected.session_id
                                and observation.generation == expected.generation
                                and observation.shell_pid == expected.shell_pid
                                and observation.state == "running"
                                and observation.screen is not None
                            )
                            if valid:
                                sample.update(
                                    sequence=observation.sequence,
                                    screen=_clip_screen(observation.screen),
                                    synced=True,
                                )
                    except Exception:
                        # An unavailable or stale observer produces no LIVE
                        # content; the UI must not retain a previous snapshot.
                        pass
                    finally:
                        if client is not None:
                            try:
                                client.close()
                            except Exception:
                                pass
                    samples.append(sample)
                if not self._stopping:
                    try:
                        self._results.put_nowait((token, samples))
                    except queue.Full:
                        try:
                            self._results.get_nowait()
                        except queue.Empty:
                            pass
                        self._results.put_nowait((token, samples))
            finally:
                with self._lock:
                    self._busy = False
