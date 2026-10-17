"""Versioned, logical Tower projects that group stable tmux Window refs.

Projects are intentionally separate from legacy folders (window classification),
Work Groups (Agent Pane collections), and Worksets (launch recipes). This store
never invokes tmux and never rewrites any of those existing stores.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
import uuid
from pathlib import Path
from typing import Iterable, Optional

_SCHEMA_VERSION = 1
_WINDOW_REF = re.compile(r"^[a-f0-9]{64}$")
_UNLOADED = object()


class ProjectStore:
    """Atomic metadata for cross-host logical projects; never changes tmux."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._digest = _UNLOADED
        self._projects: list[dict] = []
        self._hidden_refs: set[str] = set()
        self._corrupt = False
        self._external_change = False

    @staticmethod
    def _clean(data) -> tuple[list[dict], set[str]]:
        if not isinstance(data, dict) or data.get("schema_version") != _SCHEMA_VERSION:
            raise ValueError("unsupported project state")
        if set(data) != {"schema_version", "projects", "hidden_window_refs"}:
            raise ValueError("unexpected project state fields")
        raw_projects = data.get("projects")
        raw_hidden = data.get("hidden_window_refs")
        if not isinstance(raw_projects, list) or not isinstance(raw_hidden, list):
            raise ValueError("invalid project state")

        projects = []
        project_ids = set()
        memberships = set()
        for raw in raw_projects:
            if not isinstance(raw, dict):
                raise ValueError("invalid project entry")
            project_id = str(raw.get("project_id") or "").strip()
            name = str(raw.get("display_name") or "").strip()
            refs = raw.get("window_refs")
            if not project_id or not name or len(name) > 120 or not isinstance(refs, list):
                raise ValueError("invalid project entry")
            if project_id in project_ids:
                raise ValueError("duplicate project ID")
            project_ids.add(project_id)
            window_refs = []
            for item in refs:
                ref = str(item)
                if not _WINDOW_REF.fullmatch(ref) or ref in window_refs or ref in memberships:
                    raise ValueError("invalid or duplicate project Window reference")
                window_refs.append(ref)
                memberships.add(ref)
            order = raw.get("order", len(projects))
            if isinstance(order, bool) or not isinstance(order, int) or order < 0:
                raise ValueError("invalid project order")
            projects.append({
                "project_id": project_id,
                "display_name": name,
                "window_refs": window_refs,
                "order": order,
                "collapsed": bool(raw.get("collapsed", False)),
                "created_at": raw.get("created_at") or time.time(),
                "updated_at": raw.get("updated_at") or time.time(),
            })

        hidden_refs = set()
        for item in raw_hidden:
            ref = str(item)
            if not _WINDOW_REF.fullmatch(ref):
                raise ValueError("invalid hidden Window reference")
            hidden_refs.add(ref)
        if not hidden_refs.issubset(memberships):
            raise ValueError("hidden Window is not assigned to a project")
        return projects, hidden_refs

    def _load(self) -> None:
        if self.path.is_symlink():
            self._corrupt = True
            self._projects, self._hidden_refs = [], set()
            self._digest = _UNLOADED
            return
        try:
            raw = self.path.read_bytes()
            digest = hashlib.sha256(raw).hexdigest()
        except FileNotFoundError:
            raw = None
            digest = None
        except OSError:
            self._corrupt = True
            self._projects, self._hidden_refs = [], set()
            self._digest = _UNLOADED
            return

        if digest == self._digest:
            return
        if self._digest is not _UNLOADED:
            self._external_change = True
        try:
            data = json.loads(raw.decode("utf-8")) if raw is not None else {
                "schema_version": _SCHEMA_VERSION,
                "projects": [],
                "hidden_window_refs": [],
            }
            self._projects, self._hidden_refs = self._clean(data)
            self._corrupt = False
        except (UnicodeError, ValueError, TypeError, json.JSONDecodeError):
            self._projects, self._hidden_refs = [], set()
            self._corrupt = True
        self._digest = digest

    @property
    def corrupt(self) -> bool:
        self._load()
        return self._corrupt

    def all(self) -> list[dict]:
        self._load()
        return [dict(item, window_refs=list(item["window_refs"])) for item in
                sorted(self._projects, key=lambda item: (item["order"], self._projects.index(item)))]

    def hidden_refs(self) -> set[str]:
        self._load()
        return set(self._hidden_refs)

    def memberships(self) -> dict[str, str]:
        self._load()
        return {ref: item["project_id"] for item in self._projects for ref in item["window_refs"]}

    def _project(self, project_id: str, projects: Optional[list[dict]] = None) -> dict:
        values = self._projects if projects is None else projects
        item = next((value for value in values if value["project_id"] == project_id), None)
        if item is None:
            raise KeyError(project_id)
        return item

    def _commit(self, projects: list[dict], hidden_refs: set[str]) -> None:
        self._load()
        if self._corrupt:
            raise OSError(f"project state is unreadable: {self.path}")
        if self._external_change:
            raise OSError(f"project state changed outside Tower: {self.path}")
        try:
            current = self.path.read_bytes()
            current_digest = hashlib.sha256(current).hexdigest()
        except FileNotFoundError:
            current_digest = None
        except OSError as exc:
            raise OSError(f"project state cannot be checked: {self.path}") from exc
        if current_digest != self._digest:
            self._digest = _UNLOADED
            self._load()
            raise OSError(f"project state changed outside Tower: {self.path}")

        payload = json.dumps({
            "schema_version": _SCHEMA_VERSION,
            "projects": projects,
            "hidden_window_refs": sorted(hidden_refs),
        }, ensure_ascii=False, indent=2).encode("utf-8") + b"\n"
        fd, tmp_name = tempfile.mkstemp(prefix=f".{self.path.name}.", suffix=".tmp", dir=str(self.path.parent))
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "wb") as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())

            # Detect an external edit made while the new file was prepared.
            try:
                current = self.path.read_bytes()
                current_digest = hashlib.sha256(current).hexdigest()
            except FileNotFoundError:
                current_digest = None
            if current_digest != self._digest:
                self._digest = _UNLOADED
                self._load()
                raise OSError(f"project state changed outside Tower: {self.path}")

            os.replace(tmp, self.path)
            try:
                directory_fd = os.open(self.path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except OSError:
                # The data file replacement itself is already durable enough
                # for normal use; unsupported directory fsync is non-fatal.
                pass
        finally:
            if tmp.exists():
                tmp.unlink()
        self._projects = projects
        self._hidden_refs = hidden_refs
        self._digest = hashlib.sha256(payload).hexdigest()
        self._corrupt = False
        self._external_change = False

    @staticmethod
    def _valid_refs(window_refs: Iterable[str]) -> list[str]:
        refs = list(dict.fromkeys(str(item) for item in window_refs))
        if any(not _WINDOW_REF.fullmatch(ref) for ref in refs):
            raise ValueError("invalid Window reference")
        return refs

    def create(self, display_name: str, window_refs: Iterable[str] = (), *,
               move_existing: bool = False) -> dict:
        self._load()
        name = str(display_name or "").strip()
        if not name or len(name) > 120:
            raise ValueError("project name must contain 1 to 120 characters")
        refs = self._valid_refs(window_refs)
        membership = {ref: item["project_id"] for item in self._projects for ref in item["window_refs"]}
        conflicts = {ref: membership[ref] for ref in refs if ref in membership}
        if conflicts and not move_existing:
            raise ValueError("Window already belongs to another project")
        now = time.time()
        project = {
            "project_id": uuid.uuid4().hex,
            "display_name": name,
            "window_refs": refs,
            "order": len(self._projects),
            "collapsed": False,
            "created_at": now,
            "updated_at": now,
        }
        projects = [dict(item, window_refs=list(item["window_refs"])) for item in self._projects]
        for ref, source_id in conflicts.items():
            source = self._project(source_id, projects)
            source["window_refs"].remove(ref)
            source["updated_at"] = now
        projects.append(project)
        self._commit(projects, set(self._hidden_refs))
        return dict(project, window_refs=list(refs))

    def rename(self, project_id: str, display_name: str) -> None:
        self._load()
        name = str(display_name or "").strip()
        if not name or len(name) > 120:
            raise ValueError("project name must contain 1 to 120 characters")
        projects = [dict(item, window_refs=list(item["window_refs"])) for item in self._projects]
        project = self._project(project_id, projects)
        project["display_name"] = name
        project["updated_at"] = time.time()
        self._commit(projects, set(self._hidden_refs))

    def delete(self, project_id: str) -> list[str]:
        """Remove only a logical project; unhide its windows for normal browsing."""
        self._load()
        project = self._project(project_id)
        removed_refs = list(project["window_refs"])
        projects = [dict(item, window_refs=list(item["window_refs"]))
                    for item in self._projects if item["project_id"] != project_id]
        for order, item in enumerate(projects):
            item["order"] = order
        hidden = set(self._hidden_refs) - set(removed_refs)
        self._commit(projects, hidden)
        return removed_refs

    def add_windows(self, project_id: str, window_refs: Iterable[str], *, move_existing: bool = False) -> None:
        self._load()
        refs = self._valid_refs(window_refs)
        projects = [dict(item, window_refs=list(item["window_refs"])) for item in self._projects]
        destination = self._project(project_id, projects)
        membership = {ref: item["project_id"] for item in projects for ref in item["window_refs"]}
        conflicts = {ref: membership[ref] for ref in refs
                     if ref in membership and membership[ref] != project_id}
        if conflicts and not move_existing:
            raise ValueError("Window already belongs to another project")
        now = time.time()
        for ref, source_id in conflicts.items():
            source = self._project(source_id, projects)
            source["window_refs"].remove(ref)
            source["updated_at"] = now
        for ref in refs:
            if ref not in destination["window_refs"]:
                destination["window_refs"].append(ref)
        destination["updated_at"] = now
        self._commit(projects, set(self._hidden_refs))

    def remove_windows(self, project_id: str, window_refs: Iterable[str]) -> None:
        self._load()
        refs = set(self._valid_refs(window_refs))
        projects = [dict(item, window_refs=list(item["window_refs"])) for item in self._projects]
        project = self._project(project_id, projects)
        project["window_refs"] = [ref for ref in project["window_refs"] if ref not in refs]
        project["updated_at"] = time.time()
        self._commit(projects, set(self._hidden_refs) - refs)

    def reorder_project(self, project_id: str, direction: str) -> None:
        self._load()
        projects = [dict(item, window_refs=list(item["window_refs"])) for item in
                    sorted(self._projects, key=lambda item: (item["order"], self._projects.index(item)))]
        index = next((i for i, item in enumerate(projects) if item["project_id"] == project_id), None)
        if index is None:
            raise KeyError(project_id)
        target = {"up": max(0, index - 1), "down": min(len(projects) - 1, index + 1),
                  "top": 0, "bottom": len(projects) - 1}.get(direction)
        if target is None:
            raise ValueError("invalid project order")
        projects.insert(target, projects.pop(index))
        for order, item in enumerate(projects):
            item["order"] = order
            item["updated_at"] = time.time()
        self._commit(projects, set(self._hidden_refs))

    def reorder_window(self, project_id: str, window_ref: str, direction: str) -> None:
        self._load()
        if not _WINDOW_REF.fullmatch(str(window_ref)):
            raise ValueError("invalid Window reference")
        projects = [dict(item, window_refs=list(item["window_refs"])) for item in self._projects]
        project = self._project(project_id, projects)
        refs = project["window_refs"]
        if window_ref not in refs:
            raise KeyError(window_ref)
        index = refs.index(window_ref)
        target = {"up": max(0, index - 1), "down": min(len(refs) - 1, index + 1),
                  "top": 0, "bottom": len(refs) - 1}.get(direction)
        if target is None:
            raise ValueError("invalid Window order")
        refs.insert(target, refs.pop(index))
        project["updated_at"] = time.time()
        self._commit(projects, set(self._hidden_refs))

    def toggle_project(self, project_id: str) -> None:
        self._load()
        projects = [dict(item, window_refs=list(item["window_refs"])) for item in self._projects]
        project = self._project(project_id, projects)
        project["collapsed"] = not project["collapsed"]
        project["updated_at"] = time.time()
        self._commit(projects, set(self._hidden_refs))

    def set_hidden(self, window_ref: str, hidden: bool) -> None:
        self._load()
        if not _WINDOW_REF.fullmatch(str(window_ref)):
            raise ValueError("invalid Window reference")
        membership = {ref for item in self._projects for ref in item["window_refs"]}
        if window_ref not in membership:
            raise KeyError(window_ref)
        refs = set(self._hidden_refs)
        if hidden:
            refs.add(window_ref)
        else:
            refs.discard(window_ref)
        self._commit([dict(item, window_refs=list(item["window_refs"])) for item in self._projects], refs)
