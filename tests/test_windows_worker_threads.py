"""Platform-neutral lifecycle tests for the Windows worker's client threads."""
from __future__ import annotations

import threading
import gc
import weakref

from tmux_agent_tower.terminal._windows_worker import _ClientThreadRegistry


def test_completed_client_threads_are_not_retained():
    registry = _ClientThreadRegistry()
    completed = 0
    completed_lock = threading.Lock()
    thread_refs = []

    def finish_one():
        nonlocal completed
        with completed_lock:
            completed += 1

    for _ in range(128):
        thread, rejection = registry.start(finish_one)
        assert rejection is None
        assert thread is not None
        thread.join(timeout=2)
        assert not thread.is_alive()
        assert registry.snapshot() == ()
        thread_refs.append(weakref.ref(thread))
        del thread

    assert completed == 128
    gc.collect()
    assert all(thread_ref() is None for thread_ref in thread_refs)


def test_shutdown_serializes_with_concurrent_client_admission():
    registry = _ClientThreadRegistry()
    stop_event = threading.Event()
    release_workers = threading.Event()
    start_gate = threading.Barrier(9)
    outcomes = []
    outcomes_lock = threading.Lock()

    def hold_worker():
        assert release_workers.wait(timeout=5)

    def attempt_start():
        start_gate.wait(timeout=5)
        thread, rejection = registry.start(hold_worker)
        with outcomes_lock:
            outcomes.append((thread, rejection))

    starters = [threading.Thread(target=attempt_start) for _ in range(8)]
    for starter in starters:
        starter.start()
    start_gate.wait(timeout=5)

    shutdown_snapshot = registry.begin_shutdown(stop_event)
    assert stop_event.is_set()
    for starter in starters:
        starter.join(timeout=5)
        assert not starter.is_alive()

    accepted = {thread for thread, rejection in outcomes if rejection is None}
    assert all(rejection in (None, "closing") for _, rejection in outcomes)
    assert accepted == set(shutdown_snapshot)
    assert len(accepted) <= 8

    thread, rejection = registry.start(lambda: None)
    assert thread is None
    assert rejection == "closing"

    release_workers.set()
    for thread in shutdown_snapshot:
        thread.join(timeout=5)
        assert not thread.is_alive()
    assert registry.snapshot() == ()


