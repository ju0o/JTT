"""Custom project/agent/pane-title overrides (the "E" edit menu).

Stored separately from auto-detection so that:

* auto-discovery never clobbers a value the user explicitly chose, and
* a manual agent-name override is display-only metadata -- it never
  changes what process is running or sends it anything.
* a task role describes work only; it never changes the Agent identity.

A pane id (``%12``) is reused after that pane dies. An override is
applied only when the session and the pane's process id still match the
ones recorded when the user set it. A record without that identity, or
one whose pid belongs to an older pane, is ignored. Phone and PC share
this file and this check.

v0.1.2 changed the on-disk shape from ``{key: "project name"}`` to
``{key: {"project": ..., "agent": ..., "title": ...}}``. ``_load()``
still accepts that older shape. Those entries have no session or
pane pid, so they are not applied.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, Optional

ROLE_IDS = ("orchestrator", "planner", "builder", "reviewer", "qa", "dogfood", "e2e")
FIELDS = ("project", "task_name", "agent", "title", "execution_host", "role")
_TITLE_SYNC_FIELDS = (
    "task_title_sync_enabled", "task_title_sync_conflicts", "task_title_sync_blocked",
    "task_title_sync_original_title", "task_title_sync_last_title",
)
_KEPT = FIELDS + ("task_name_origin", "session", "pane_pid") + _TITLE_SYNC_FIELDS


class OverrideStore:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._cache: Optional[Dict[str, Dict[str, str]]] = None
        self._loaded_stamp: Optional[tuple] = None

    def _stamp(self) -> Optional[tuple]:
        try:
            st = self.path.stat()
        except FileNotFoundError:
            return None
        except Exception:
            return None
        return (st.st_mtime_ns, st.st_size)

    def _load(self) -> Dict[str, Dict[str, str]]:
        # Another writer (Tower Remote's identity edits from the phone)
        # shares this file. Re-read when it changed on disk so the TUI
        # picks the edit up on its next refresh instead of at restart.
        stamp = self._stamp()
        if self._cache is not None and stamp == self._loaded_stamp:
            return self._cache

        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            data = {}
        except Exception:
            data = {}

        migrated: Dict[str, Dict[str, str]] = {}

        if isinstance(data, dict):
            for key, value in data.items():
                if isinstance(value, str):
                    migrated[str(key)] = {"project": value}
                elif isinstance(value, dict):
                    entry = {
                        field: str(value[field])
                        for field in _KEPT
                        if field in value and isinstance(value[field], (str, int, float))
                    }
                    if entry:
                        migrated[str(key)] = entry

        self._cache = migrated
        self._loaded_stamp = stamp
        return self._cache

    def _save(self) -> None:
        try:
            self.path.write_text(json.dumps(self._cache, indent=2, sort_keys=True), encoding="utf-8")
        except Exception:
            pass
        self._loaded_stamp = self._stamp()

    @staticmethod
    def _matches(entry: Dict[str, str], session: str, pane_pid: str) -> bool:
        """True only for this session and this pane process."""

        if str(entry.get("session") or "") != str(session or ""):
            return False
        stored = str(entry.get("pane_pid") or "").strip()
        current = str(pane_pid or "").strip()
        if not stored or stored != current:
            return False
        return True

    def _get_field(self, key: str, field: str, session: str, pane_pid: str) -> Optional[str]:
        entry = self._load().get(str(key))
        if not entry or not self._matches(entry, session, pane_pid):
            return None
        return entry.get(field)

    def _set_field(self, key: str, field: str, value: str, session: str, pane_pid: str) -> None:
        """Stamp the override with the pane the user is looking at now.

        A blank session or pid is refused: it could not be checked later,
        and an unchecked label is how a dead pane's name landed on a new one.
        """

        if not str(key) or not str(session or "") or not str(pane_pid or "").strip():
            return
        data = self._load()
        entry = dict(data.get(str(key), {}))
        entry[field] = value
        entry["session"] = str(session)
        entry["pane_pid"] = str(pane_pid).strip()
        data[str(key)] = entry
        self._cache = data
        self._save()

    def get_project(self, key: str, session: str = "", pane_pid: str = "") -> Optional[str]:
        return self._get_field(key, "project", session, pane_pid)

    def set_project(self, key: str, value: str, session: str, pane_pid: str) -> None:
        self._set_field(key, "project", value, session, pane_pid)

    def get_task_name(self, key: str, session: str = "", pane_pid: str = "") -> Optional[str]:
        return self._get_field(key, "task_name", session, pane_pid)

    def get_task_name_origin(self, key: str, session: str = "", pane_pid: str = "") -> Optional[str]:
        if not self.get_task_name(key, session, pane_pid):
            return None
        return "auto" if self._get_field(key, "task_name_origin", session, pane_pid) == "auto" else "user"

    def set_task_name(self, key: str, value: str, session: str, pane_pid: str, *, origin: str = "user") -> None:
        if not str(key) or not str(session or "") or not str(pane_pid or "").strip():
            return
        data = self._load()
        entry = dict(data.get(str(key), {}))
        entry["task_name"] = value
        entry["task_name_origin"] = "auto" if origin == "auto" else "user"
        entry["session"] = str(session)
        entry["pane_pid"] = str(pane_pid).strip()
        data[str(key)] = entry
        self._cache = data
        self._save()

    def get_task_title_sync(self, key: str, session: str, pane_pid: str) -> Dict[str, object]:
        entry = self._load().get(str(key))
        if not entry or not self._matches(entry, session, pane_pid):
            return {"enabled": False, "blocked": False, "conflicts": 0, "original_title": "", "last_title": ""}
        try:
            conflicts = max(0, int(entry.get("task_title_sync_conflicts") or 0))
        except (TypeError, ValueError):
            conflicts = 0
        blocked = entry.get("task_title_sync_blocked") == "1"
        return {
            "enabled": entry.get("task_title_sync_enabled") == "1" and not blocked,
            "blocked": blocked,
            "conflicts": conflicts,
            "original_title": entry.get("task_title_sync_original_title") or "",
            "last_title": entry.get("task_title_sync_last_title") or "",
        }

    def set_task_title_sync(
        self,
        key: str,
        session: str,
        pane_pid: str,
        *,
        enabled: Optional[bool] = None,
        blocked: Optional[bool] = None,
        conflicts: Optional[int] = None,
        original_title: Optional[str] = None,
        last_title: Optional[str] = None,
    ) -> bool:
        """Update opt-in title syncing only on the exact bound pane identity."""

        data = self._load()
        entry = data.get(str(key))
        if not entry or not self._matches(entry, session, pane_pid) or not entry.get("task_name"):
            return False
        entry = dict(entry)
        if enabled is not None:
            entry["task_title_sync_enabled"] = "1" if enabled else "0"
        if blocked is not None:
            entry["task_title_sync_blocked"] = "1" if blocked else "0"
        if conflicts is not None:
            entry["task_title_sync_conflicts"] = str(max(0, conflicts))
        if original_title is not None:
            entry["task_title_sync_original_title"] = original_title
        if last_title is not None:
            entry["task_title_sync_last_title"] = last_title
        data[str(key)] = entry
        self._cache = data
        self._save()
        return True

    def get_agent(self, key: str, session: str = "", pane_pid: str = "") -> Optional[str]:
        return self._get_field(key, "agent", session, pane_pid)

    def set_agent(self, key: str, value: str, session: str, pane_pid: str) -> None:
        self._set_field(key, "agent", value, session, pane_pid)

    def get_title(self, key: str, session: str = "", pane_pid: str = "") -> Optional[str]:
        return self._get_field(key, "title", session, pane_pid)

    def set_title(self, key: str, value: str, session: str, pane_pid: str) -> None:
        self._set_field(key, "title", value, session, pane_pid)

    def get_execution_host(self, key: str, session: str = "", pane_pid: str = "") -> Optional[str]:
        return self._get_field(key, "execution_host", session, pane_pid)

    def set_execution_host(self, key: str, value: str, session: str, pane_pid: str) -> None:
        self._set_field(key, "execution_host", value, session, pane_pid)

    def get_role(self, key: str, session: str = "", pane_pid: str = "") -> Optional[str]:
        role = self._get_field(key, "role", session, pane_pid)
        return role if role in ROLE_IDS else None

    def set_role(self, key: str, value: str, session: str, pane_pid: str) -> None:
        if value in ROLE_IDS:
            self._set_field(key, "role", value, session, pane_pid)

    def drop_if_stale(self, key: str, session: str, pane_pid: str) -> bool:
        """Drop a record that does not belong to this pane. True if removed.

        The launcher calls this for a pane it just created. A reused pane
        id must not keep the previous pane's label.
        """

        data = self._load()
        entry = data.get(str(key))
        if not entry:
            return False
        if self._matches(entry, session, pane_pid):
            return False
        del data[str(key)]
        self._cache = data
        self._save()
        return True

    def clear_field(
        self,
        key: str,
        field: str,
        session: Optional[str] = None,
        pane_pid: Optional[str] = None,
    ) -> None:
        """Clears just one field without touching the other labels.

        When an identity is supplied, it must be complete and match the
        record. The two-argument form remains available to legacy callers.
        """

        data = self._load()
        entry = data.get(str(key))
        if not entry or (field not in entry and not (field == "task_name" and "task_name_origin" in entry)):
            return
        if session is not None or pane_pid is not None:
            if not str(session or "").strip() or not str(pane_pid or "").strip():
                return
            if not self._matches(entry, str(session), str(pane_pid)):
                return

        entry = dict(entry)
        entry.pop(field, None)
        if field == "task_name":
            entry.pop("task_name_origin", None)
            for sync_field in _TITLE_SYNC_FIELDS:
                entry.pop(sync_field, None)
        leftover = {k: v for k, v in entry.items() if k in FIELDS}
        if leftover:
            data[str(key)] = entry
        else:
            del data[str(key)]

        self._cache = data
        self._save()

    def reset(self, key: str) -> None:
        """Clears every override for ``key`` -- only this one pane."""

        data = self._load()
        if str(key) in data:
            del data[str(key)]
            self._cache = data
            self._save()
