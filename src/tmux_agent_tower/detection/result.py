"""Per-pane final-answer state.

Only state metadata is shared between Tower processes. Result text stays in
each process's memory and is re-extracted from the live pane when requested.
"""

from __future__ import annotations

import json
import os
import re
import socket
import sqlite3
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Mapping, Optional

from ..adapters.base import ResultCandidate
from ..paths import STATE_DIR

RESULT_NONE = "none"
RESULT_READY = "ready"
RESULT_READ = "read"
SCHEMA_VERSION = 1

_SUPPRESSING = frozenset({"WORKING", "WAITING", "DEAD", "UNKNOWN"})
_PANE_ID_RE = re.compile(r"^%\d+$")
_WINDOW_ID_RE = re.compile(r"^@\d+$")
_IDENTITY_FIELDS = (
    "tmux_host",
    "server_scope",
    "session",
    "window_id",
    "pane_id",
    "pane_pid",
)


def shared_result_state_path() -> Path:
    """The same local metadata file for the TUI and Phone Remote."""

    return STATE_DIR / "result-state.sqlite3"


def pane_result_identity(row: Mapping[str, object]) -> Optional[dict]:
    """Stable physical pane identity, or ``None`` when evidence is incomplete.

    ``$TMUX`` identifies the server socket and its generation. The socket
    inode protects against a restarted server reusing the same socket path
    and pane ID. Client index is deliberately omitted because the TUI and
    detached Phone Remote can have different tmux clients.
    """

    tmux = os.environ.get("TMUX", "")
    try:
        socket_path, server_pid, _client_index = tmux.rsplit(",", 2)
        if not socket_path or not server_pid.isdigit():
            return None
        info = os.stat(socket_path)
        if not stat.S_ISSOCK(info.st_mode):
            return None
    except (OSError, ValueError):
        return None

    values = {
        "tmux_host": row.get("tmux_host"),
        "server_scope": {
            "host": socket.gethostname(),
            "socket": os.path.realpath(socket_path),
            "server_pid": server_pid,
            "socket_device": info.st_dev,
            "socket_inode": info.st_ino,
        },
        "session": row.get("session"),
        "window_id": row.get("window_id"),
        "pane_id": row.get("pane_id") or row.get("key"),
        "pane_pid": row.get("pane_pid"),
    }
    if any(not isinstance(values[field], str) or not values[field].strip() for field in ("tmux_host", "session", "window_id", "pane_id", "pane_pid")):
        return None
    if (
        not _WINDOW_ID_RE.fullmatch(str(values["window_id"]))
        or not _PANE_ID_RE.fullmatch(str(values["pane_id"]))
        or not str(values["pane_pid"]).isdigit()
    ):
        return None
    return values


def _identity_key(identity: Optional[Mapping[str, object]]) -> str:
    if not isinstance(identity, Mapping) or any(field not in identity for field in _IDENTITY_FIELDS):
        return ""
    if any(identity.get(field) in (None, "") for field in _IDENTITY_FIELDS):
        return ""
    try:
        return json.dumps(identity, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    except (TypeError, ValueError):
        return ""


@dataclass
class ResultSnapshot:
    state: str
    text: str
    fingerprint: str
    generated_at: float = 0.0
    complete: bool = False
    turn_complete: bool = False
    body_complete: bool = False


@dataclass
class _Rec:
    fingerprint: str = ""
    text: str = ""
    state: str = RESULT_NONE
    suppressed: bool = False
    generated_at: float = 0.0
    confidence: str = "high"
    complete: bool = False
    turn_complete: bool = False
    body_complete: bool = False
    full_recovery: bool = False
    identity: str = ""


class ResultTracker:
    def __init__(self, state_path: Optional[Path] = None) -> None:
        # No path means the small, isolated in-memory tracker used by adapter
        # callers/tests. Product entry points pass the shared local path.
        self.state_path = Path(state_path) if state_path is not None else None
        self._recs: Dict[str, _Rec] = {}
        # Only numeric tmux history positions are kept here; never prompt or
        # terminal text. This is intentionally process-local metadata.
        self._turn_watermarks: Dict[str, tuple] = {}

    def record_turn_start(self, pane_key: str, position, identity=None) -> None:
        key = _identity_key(identity)
        if key and isinstance(position, tuple) and len(position) == 2:
            self._turn_watermarks[pane_key] = (int(position[0]), int(position[1]), key)

    def turn_start_watermark(self, pane_key: str, identity=None):
        stored = self._turn_watermarks.get(pane_key)
        if stored is None or stored[2] != _identity_key(identity):
            return None
        return stored[:2]

    def _connect(self) -> sqlite3.Connection:
        assert self.state_path is not None
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(str(self.state_path), timeout=30.0, isolation_level=None)
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute(
            "CREATE TABLE IF NOT EXISTS result_state ("
            "schema_version INTEGER NOT NULL, "
            "pane_identity TEXT PRIMARY KEY NOT NULL, "
            "state TEXT NOT NULL CHECK (state IN ('none', 'ready', 'read')), "
            "fingerprint TEXT NOT NULL, "
            "generated_at REAL NOT NULL)"
        )
        return connection

    @staticmethod
    def _stored(connection: sqlite3.Connection, identity: str):
        row = connection.execute(
            "SELECT schema_version, state, fingerprint, generated_at "
            "FROM result_state WHERE pane_identity = ?",
            (identity,),
        ).fetchone()
        if not row or row[0] != SCHEMA_VERSION or row[1] not in (RESULT_NONE, RESULT_READY, RESULT_READ):
            return None
        return row[1], row[2], float(row[3])

    @staticmethod
    def _store(connection: sqlite3.Connection, identity: str, rec: _Rec) -> None:
        # This is intentionally the complete durable field set. Never pass
        # candidate.text or any prompt/screen content to SQLite.
        connection.execute(
            "INSERT INTO result_state(schema_version, pane_identity, state, fingerprint, generated_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(pane_identity) DO UPDATE SET "
            "schema_version=excluded.schema_version, state=excluded.state, "
            "fingerprint=excluded.fingerprint, generated_at=excluded.generated_at",
            (SCHEMA_VERSION, identity, rec.state, rec.fingerprint, rec.generated_at),
        )

    def _cached_rec(self, pane_key: str, identity: str, stored) -> _Rec:
        cached = self._recs.get(pane_key)
        if stored is None:
            return _Rec(identity=identity)
        state, fingerprint, generated_at = stored
        same = cached is not None and cached.identity == identity and cached.fingerprint == fingerprint
        return _Rec(
            fingerprint=fingerprint,
            text=cached.text if same else "",
            state=state,
            suppressed=state == RESULT_NONE and bool(fingerprint),
            generated_at=generated_at,
            confidence=cached.confidence if same else "high",
            complete=cached.complete if same else state in (RESULT_READY, RESULT_READ),
            turn_complete=cached.turn_complete if same else state in (RESULT_READY, RESULT_READ),
            body_complete=cached.body_complete if same else state in (RESULT_READY, RESULT_READ),
            full_recovery=cached.full_recovery if same else False,
            identity=identity,
        )

    @staticmethod
    def _snapshot(rec: _Rec, state=None, text=None) -> ResultSnapshot:
        resolved_state = rec.state if state is None else state
        complete = rec.complete and resolved_state in (RESULT_READY, RESULT_READ)
        return ResultSnapshot(
            resolved_state,
            rec.text if text is None else text,
            rec.fingerprint,
            rec.generated_at,
            complete,
            rec.turn_complete if complete else False,
            rec.body_complete if complete else False,
        )

    @staticmethod
    def _suppress_partial(rec: _Rec, candidate: ResultCandidate) -> None:
        rec.text = ""
        rec.state = RESULT_NONE
        rec.suppressed = bool(rec.fingerprint)
        rec.generated_at = 0.0
        rec.confidence = candidate.confidence or "partial"
        rec.complete = False
        rec.turn_complete = candidate.turn_complete
        rec.body_complete = candidate.body_complete
        rec.full_recovery = False

    @staticmethod
    def _is_cached_body_suffix(rec: _Rec, candidate: ResultCandidate) -> bool:
        return bool(
            rec.full_recovery and rec.complete and rec.body_complete and rec.text and candidate.text
            and rec.text.endswith(candidate.text)
        )

    def _observe_shared(
        self,
        pane_key: str,
        status: str,
        candidate: Optional[ResultCandidate],
        identity: Optional[Mapping[str, object]],
        *,
        full_recovery: bool = False,
    ) -> ResultSnapshot:
        identity_key = _identity_key(identity)
        if not identity_key:
            return ResultSnapshot(RESULT_NONE, "", "")
        connection = None
        try:
            connection = self._connect()
            connection.execute("BEGIN IMMEDIATE")
            rec = self._cached_rec(pane_key, identity_key, self._stored(connection, identity_key))
            should_store = bool(rec.fingerprint)

            if status in _SUPPRESSING:
                rec.suppressed = True
                rec.full_recovery = False
                # Keep a completed read durable across a running turn, just
                # as the in-memory tracker did. Other pollers independently
                # suppress it when their execution evidence says WORKING.
                if rec.state != RESULT_READ:
                    rec.state = RESULT_NONE
                else:
                    should_store = False
                snapshot = self._snapshot(rec, RESULT_NONE, "")
            elif candidate is None:
                if rec.suppressed:
                    snapshot = self._snapshot(rec, RESULT_NONE, "")
                else:
                    snapshot = self._snapshot(rec)
            elif not candidate.complete:
                if self._is_cached_body_suffix(rec, candidate):
                    snapshot = self._snapshot(rec)
                    connection.commit()
                    self._recs[pane_key] = rec
                    return snapshot
                # An incomplete newer candidate invalidates any older ready
                # result. It cannot be copied and must not remain advertised.
                self._suppress_partial(rec, candidate)
                snapshot = self._snapshot(rec, RESULT_NONE, "")
                should_store = True
            elif candidate.fingerprint != rec.fingerprint:
                rec.fingerprint = candidate.fingerprint
                rec.text = candidate.text
                rec.confidence = candidate.confidence or "high"
                rec.complete = candidate.complete
                rec.turn_complete = candidate.turn_complete
                rec.body_complete = candidate.body_complete
                rec.full_recovery = full_recovery
                rec.state = RESULT_READY
                rec.suppressed = False
                rec.generated_at = time.time()
                snapshot = self._snapshot(rec, RESULT_READY)
                should_store = True
            elif rec.suppressed:
                rec.suppressed = False
                if candidate.text:
                    rec.text = candidate.text
                rec.complete = candidate.complete
                rec.turn_complete = candidate.turn_complete
                rec.body_complete = candidate.body_complete
                if full_recovery and candidate.turn_complete and candidate.body_complete:
                    rec.full_recovery = True
                    if rec.state != RESULT_READ:
                        rec.state = RESULT_READY
                        rec.generated_at = rec.generated_at or time.time()
                else:
                    rec.full_recovery = False
                    # Polling alone must not restore a completion that was
                    # suppressed while a new turn was running.
                    if rec.state != RESULT_READ:
                        rec.state = RESULT_NONE
                snapshot = self._snapshot(rec)
                should_store = True
            else:
                if candidate.text:
                    rec.text = candidate.text
                rec.confidence = candidate.confidence or rec.confidence
                rec.complete = candidate.complete
                rec.turn_complete = candidate.turn_complete
                rec.body_complete = candidate.body_complete
                if full_recovery and candidate.turn_complete and candidate.body_complete:
                    rec.full_recovery = True
                snapshot = self._snapshot(rec)

            rec.identity = identity_key
            if should_store:
                self._store(connection, identity_key, rec)
            connection.commit()
            self._recs[pane_key] = rec
            return snapshot
        except (OSError, sqlite3.Error, TypeError, ValueError):
            if connection is not None:
                connection.rollback()
            return ResultSnapshot(RESULT_NONE, "", "")
        finally:
            if connection is not None:
                connection.close()

    def observe(
        self,
        pane_key: str,
        status: str,
        candidate: Optional[ResultCandidate],
        identity: Optional[Mapping[str, object]] = None,
        *,
        full_recovery: bool = False,
    ) -> ResultSnapshot:
        if self.state_path is not None:
            return self._observe_shared(pane_key, status, candidate, identity, full_recovery=full_recovery)

        rec = self._recs.setdefault(pane_key, _Rec())

        if status in _SUPPRESSING:
            rec.suppressed = True
            rec.full_recovery = False
            return self._snapshot(rec, RESULT_NONE, "")

        if candidate is None:
            if rec.suppressed:
                return self._snapshot(rec, RESULT_NONE, "")
            if rec.state in (RESULT_READY, RESULT_READ) and rec.text:
                return self._snapshot(rec)
            return self._snapshot(rec, RESULT_NONE, "")

        if not candidate.complete:
            if self._is_cached_body_suffix(rec, candidate):
                return self._snapshot(rec)
            # Match shared-state polling: partial output is never a Result and
            # suppresses an older completion until a complete candidate arrives.
            self._suppress_partial(rec, candidate)
            return self._snapshot(rec, RESULT_NONE, "")

        if candidate.fingerprint != rec.fingerprint:
            rec.fingerprint = candidate.fingerprint
            rec.text = candidate.text
            rec.confidence = candidate.confidence or "high"
            rec.complete = candidate.complete
            rec.turn_complete = candidate.turn_complete
            rec.body_complete = candidate.body_complete
            rec.full_recovery = full_recovery
            rec.state = RESULT_READY
            rec.suppressed = False
            rec.generated_at = time.time()
            return self._snapshot(rec, RESULT_READY)

        if rec.suppressed:
            rec.suppressed = False
            rec.complete = candidate.complete
            rec.turn_complete = candidate.turn_complete
            rec.body_complete = candidate.body_complete
            if full_recovery and candidate.turn_complete and candidate.body_complete:
                rec.full_recovery = True
                if rec.state != RESULT_READ:
                    rec.state = RESULT_READY
                    rec.generated_at = rec.generated_at or time.time()
            else:
                rec.full_recovery = False
                if rec.state != RESULT_READ:
                    rec.state = RESULT_NONE
            if candidate.text:
                rec.text = candidate.text
            return self._snapshot(rec)

        if full_recovery and candidate.turn_complete and candidate.body_complete:
            rec.full_recovery = True
        return self._snapshot(rec)

    def rebind_identity(self, pane_key: str, old_identity, new_identity) -> bool:
        """Carry one pane's Result metadata across a verified same-server move.

        The pane id and process stay stable while ``window_id`` changes. Result
        text remains in memory; only its identity key and metadata are moved.
        """
        if self.state_path is None:
            return True
        old_key, new_key = _identity_key(old_identity), _identity_key(new_identity)
        if not old_key or not new_key:
            return False
        if old_key == new_key:
            return True
        cached = self._recs.get(pane_key)
        source_cache = cached if cached is not None and cached.identity == old_key else None
        connection = None
        try:
            connection = self._connect()
            connection.execute("BEGIN IMMEDIATE")
            old_stored = self._stored(connection, old_key)
            new_stored = self._stored(connection, new_key)
            source = old_stored
            if source is None and source_cache is not None:
                source = (source_cache.state, source_cache.fingerprint, source_cache.generated_at)
            if source and new_stored and new_stored[0] != RESULT_NONE and new_stored[1] != source[1]:
                connection.rollback()
                return False
            if source:
                state, fingerprint, generated_at = source
                if new_stored and new_stored[1] == fingerprint and new_stored[0] == RESULT_READ:
                    state = RESULT_READ
                connection.execute(
                    "INSERT INTO result_state(schema_version, pane_identity, state, fingerprint, generated_at) "
                    "VALUES (?, ?, ?, ?, ?) ON CONFLICT(pane_identity) DO UPDATE SET "
                    "schema_version=excluded.schema_version, state=excluded.state, "
                    "fingerprint=excluded.fingerprint, generated_at=excluded.generated_at",
                    (SCHEMA_VERSION, new_key, state, fingerprint, generated_at),
                )
                connection.execute("DELETE FROM result_state WHERE pane_identity = ?", (old_key,))
            connection.commit()
        except sqlite3.Error:
            if connection is not None:
                connection.rollback()
            return False
        finally:
            if connection is not None:
                connection.close()
        if source_cache is not None:
            self._recs[pane_key] = _Rec(
                fingerprint=source_cache.fingerprint,
                text=source_cache.text,
                state=state,
                suppressed=source_cache.suppressed,
                generated_at=generated_at,
                confidence=source_cache.confidence,
                complete=source_cache.complete,
                turn_complete=source_cache.turn_complete,
                body_complete=source_cache.body_complete,
                identity=new_key,
            )
        return True

    def snapshot(
        self, pane_key: str, identity: Optional[Mapping[str, object]] = None
    ) -> ResultSnapshot:
        if self.state_path is not None:
            identity_key = _identity_key(identity)
            if not identity_key:
                return ResultSnapshot(RESULT_NONE, "", "")
            connection = None
            try:
                connection = self._connect()
                connection.execute("BEGIN")
                stored = self._stored(connection, identity_key)
                connection.commit()
                if stored is None:
                    self._recs.pop(pane_key, None)
                    return ResultSnapshot(RESULT_NONE, "", "")
                rec = self._cached_rec(pane_key, identity_key, stored)
                self._recs[pane_key] = rec
                if rec.state == RESULT_NONE or rec.suppressed:
                    return self._snapshot(rec, RESULT_NONE, "")
                return self._snapshot(rec)
            except (OSError, sqlite3.Error, TypeError, ValueError):
                if connection is not None:
                    connection.rollback()
                return ResultSnapshot(RESULT_NONE, "", "")
            finally:
                if connection is not None:
                    connection.close()

        rec = self._recs.get(pane_key)
        if rec is None or rec.suppressed or not rec.text:
            fingerprint = rec.fingerprint if rec else ""
            generated = rec.generated_at if rec else 0.0
            return ResultSnapshot(RESULT_NONE, "", fingerprint, generated)
        return self._snapshot(rec)

    def mark_read(
        self,
        pane_key: str,
        fingerprint: str,
        identity: Optional[Mapping[str, object]] = None,
    ) -> bool:
        if self.state_path is not None:
            identity_key = _identity_key(identity)
            if not identity_key or not fingerprint:
                return False
            connection = None
            try:
                connection = self._connect()
                connection.execute("BEGIN IMMEDIATE")
                stored = self._stored(connection, identity_key)
                if stored is None or stored[1] != fingerprint or stored[0] not in (RESULT_READY, RESULT_READ):
                    connection.commit()
                    return False
                if stored[0] == RESULT_READY:
                    connection.execute(
                        "UPDATE result_state SET state = ? WHERE pane_identity = ? AND fingerprint = ? AND state = ?",
                        (RESULT_READ, identity_key, fingerprint, RESULT_READY),
                    )
                connection.commit()
                rec = self._cached_rec(pane_key, identity_key, (RESULT_READ, fingerprint, stored[2]))
                rec.state = RESULT_READ
                self._recs[pane_key] = rec
                return True
            except (OSError, sqlite3.Error, TypeError, ValueError):
                if connection is not None:
                    connection.rollback()
                return False
            finally:
                if connection is not None:
                    connection.close()

        rec = self._recs.get(pane_key)
        if rec is None or not fingerprint or rec.fingerprint != fingerprint:
            return False
        if rec.state != RESULT_READY:
            return rec.state == RESULT_READ
        rec.state = RESULT_READ
        return True
