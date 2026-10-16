"""Persistent, logical folders for tmux window assets."""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import uuid
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from .work_groups import aggregate

_REF = re.compile(r"^[a-f0-9]{64}$")
_VERSION = 1


def window_ref(window: dict) -> str:
    """Opaque identity that survives rename/reorder, but not window reuse."""
    parts = (
        window.get("tmux_host") or window.get("host") or "",
        window.get("session_id") or window.get("session") or "",
        window.get("window_id") or "",
        window.get("window_created") or "",
        "" if window.get("window_id") else window.get("window_index", ""),
    )
    return hashlib.sha256("\0".join(str(part) for part in parts).encode()).hexdigest()


class FolderStore:
    """Atomic local folder/window metadata. It never invokes tmux."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._stamp = None
        self._folders: List[dict] = []
        self._windows: Dict[str, dict] = {}
        self._corrupt = False

    @staticmethod
    def _clean(data) -> tuple[List[dict], Dict[str, dict]]:
        if not isinstance(data, dict) or data.get("schema_version") != _VERSION:
            raise ValueError("unsupported folder state")
        folders = []
        refs = set()
        values = data.get("folders")
        if not isinstance(values, list):
            raise ValueError("invalid folder list")
        for value in values:
            if not isinstance(value, dict):
                continue
            folder_id = str(value.get("folder_id") or "").strip()
            name = str(value.get("display_name") or "").strip()[:120]
            if not folder_id or not name:
                continue
            window_refs = []
            raw_refs = value.get("window_refs")
            if isinstance(raw_refs, list):
                for ref in raw_refs:
                    ref = str(ref)
                    if _REF.fullmatch(ref) and ref not in refs and ref not in window_refs:
                        window_refs.append(ref)
                        refs.add(ref)
            folders.append({
                "folder_id": folder_id,
                "display_name": name,
                "window_refs": window_refs,
                "order": max(0, int(value.get("order") or 0)),
                "collapsed": bool(value.get("collapsed", False)),
                "created_at": value.get("created_at") or time.time(),
                "updated_at": value.get("updated_at") or time.time(),
            })
        raw_windows = data.get("windows", {})
        if not isinstance(raw_windows, dict):
            raise ValueError("invalid window map")
        windows = {}
        for ref, value in raw_windows.items():
            ref = str(ref)
            if not _REF.fullmatch(ref) or not isinstance(value, dict):
                continue
            name = str(value.get("display_name") or "").strip()[:120]
            windows[ref] = {
                "display_name": name,
                "order": max(0, int(value.get("order") or 0)),
                "collapsed": bool(value.get("collapsed", False)),
            }
        return folders, windows

    def _load(self) -> None:
        try:
            stat = self.path.stat()
            stamp = (stat.st_mtime_ns, stat.st_size)
        except FileNotFoundError:
            stamp = None
        if stamp == self._stamp:
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8")) if stamp else {
                "schema_version": _VERSION, "folders": [], "windows": {}
            }
            self._folders, self._windows = self._clean(data)
            self._corrupt = False
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            self._folders, self._windows = [], {}
            self._corrupt = True
        self._stamp = stamp

    def _save(self) -> None:
        if self._corrupt:
            raise OSError(f"folder state is unreadable: {self.path}")
        tmp = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        try:
            tmp.write_text(json.dumps({
                "schema_version": _VERSION,
                "folders": self._folders,
                "windows": self._windows,
            }, ensure_ascii=False, indent=2), encoding="utf-8")
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        finally:
            if tmp.exists():
                tmp.unlink()
        stat = self.path.stat()
        self._stamp = (stat.st_mtime_ns, stat.st_size)

    def all(self) -> List[dict]:
        self._load()
        return [dict(folder, window_refs=list(folder["window_refs"])) for folder in
                sorted(self._folders, key=lambda item: (item["order"], self._folders.index(item)))]

    def window(self, ref: str) -> dict:
        self._load()
        return dict(self._windows.get(ref) or {})

    def create(self, display_name: str) -> dict:
        self._load()
        name = str(display_name or "").strip()[:120]
        if not name:
            raise ValueError("folder name cannot be empty")
        now = time.time()
        folder = {"folder_id": uuid.uuid4().hex, "display_name": name,
                  "window_refs": [], "order": len(self._folders), "collapsed": False,
                  "created_at": now, "updated_at": now}
        self._folders.append(folder)
        self._save()
        return dict(folder)

    def _folder(self, folder_id: str) -> dict:
        folder = next((item for item in self._folders if item["folder_id"] == folder_id), None)
        if folder is None:
            raise KeyError(folder_id)
        return folder

    def rename(self, folder_id: str, display_name: str) -> None:
        self._load()
        name = str(display_name or "").strip()[:120]
        if not name:
            raise ValueError("folder name cannot be empty")
        folder = self._folder(folder_id)
        folder["display_name"] = name
        folder["updated_at"] = time.time()
        self._save()

    def delete(self, folder_id: str) -> List[str]:
        """Remove logical membership only; return refs now unfiled."""
        self._load()
        folder = self._folder(folder_id)
        refs = list(folder["window_refs"])
        self._folders.remove(folder)
        for index, item in enumerate(self._folders):
            item["order"] = index
        self._save()
        return refs

    def move_window(self, ref: str, folder_id: Optional[str]) -> None:
        self._load()
        if not _REF.fullmatch(str(ref)):
            raise ValueError("invalid window reference")
        destination = self._folder(folder_id) if folder_id else None
        for folder in self._folders:
            if ref in folder["window_refs"]:
                folder["window_refs"].remove(ref)
                folder["updated_at"] = time.time()
        if destination:
            destination["window_refs"].append(ref)
            destination["updated_at"] = time.time()
        self._windows.setdefault(ref, {"display_name": "", "order": 0, "collapsed": False})
        self._save()

    def update_window(self, ref: str, *, display_name: Optional[str] = None,
                      collapsed: Optional[bool] = None) -> None:
        self._load()
        if not _REF.fullmatch(str(ref)):
            raise ValueError("invalid window reference")
        config = self._windows.setdefault(ref, {"display_name": "", "order": 0, "collapsed": False})
        if display_name is not None:
            name = str(display_name).strip()[:120]
            if not name:
                raise ValueError("window name cannot be empty")
            config["display_name"] = name
        if collapsed is not None:
            config["collapsed"] = bool(collapsed)
        self._save()

    def toggle_folder(self, folder_id: str) -> None:
        self._load()
        folder = self._folder(folder_id)
        folder["collapsed"] = not folder["collapsed"]
        folder["updated_at"] = time.time()
        self._save()

    def reorder_folder(self, folder_id: str, direction: str) -> None:
        self._load()
        ordered = sorted(self._folders, key=lambda item: (item["order"], self._folders.index(item)))
        index = next(i for i, item in enumerate(ordered) if item["folder_id"] == folder_id)
        target = {"up": max(0, index - 1), "down": min(len(ordered) - 1, index + 1),
                  "top": 0, "bottom": len(ordered) - 1}.get(direction)
        if target is None:
            raise ValueError("invalid folder order")
        ordered.insert(target, ordered.pop(index))
        for order, item in enumerate(ordered):
            item["order"] = order
        self._save()

    def reorder_window(self, ref: str, direction: str) -> None:
        self._load()
        folder = next((item for item in self._folders if ref in item["window_refs"]), None)
        refs = folder["window_refs"] if folder else []
        if folder is None:
            raise KeyError(ref)
        index = refs.index(ref)
        target = {"up": max(0, index - 1), "down": min(len(refs) - 1, index + 1),
                  "top": 0, "bottom": len(refs) - 1}.get(direction)
        if target is None:
            raise ValueError("invalid window order")
        refs.insert(target, refs.pop(index))
        folder["updated_at"] = time.time()
        self._save()

    def workspace(self, windows: Iterable[dict], tasks: Iterable[dict]) -> dict:
        """Stable, display-only tree projection shared by TUI and Juact clients."""
        self._load()
        live = {}
        task_list = []
        for task in tasks:
            target = str(task.get("target_id") or task.get("key") or "")
            if not target:
                continue
            task_list.append({key: task.get(key) for key in (
                "target_id", "key", "display_name", "task_name", "project", "agent", "role",
                "status", "attention", "result_state", "execution_host", "work_group_id",
                "work_group_name", "window_ref", "agent_process_ended",
                "remote",
            )})
        for source in windows:
            ref = str(source.get("window_ref") or window_ref(source))
            config = self._windows.get(ref, {})
            members = [task for task in task_list if task.get("window_ref") == ref]
            name = config.get("display_name") or source.get("inferred_name") or source.get("display_name") or "터미널"
            live[ref] = {
                "window_ref": ref,
                "display_name": name,
                "folder_id": next((folder["folder_id"] for folder in self._folders if ref in folder["window_refs"]), None),
                "order": config.get("order", source.get("order", 0)),
                "collapsed": bool(config.get("collapsed", False)),
                "stale": False,
                "task_target_ids": [str(task.get("target_id") or task.get("key")) for task in members],
                "summary_counts": aggregate(members),
            }
        folders = []
        assigned = set()
        for folder in sorted(self._folders, key=lambda item: (item["order"], self._folders.index(item))):
            refs = list(folder["window_refs"])
            assigned.update(refs)
            assets = []
            for index, ref in enumerate(refs):
                if ref in live:
                    assets.append({**live[ref], "order": index})
                else:
                    config = self._windows.get(ref, {})
                    assets.append({"window_ref": ref, "display_name": config.get("display_name") or "확인 불가",
                                   "folder_id": folder["folder_id"], "order": index,
                                   "collapsed": bool(config.get("collapsed", False)), "stale": True,
                                   "task_target_ids": [], "summary_counts": aggregate([{"stale": True}])})
            folder_tasks = [task for task in task_list if task.get("window_ref") in refs]
            folders.append({
                "folder_id": folder["folder_id"], "display_name": folder["display_name"],
                "window_refs": refs, "order": folder["order"], "collapsed": folder["collapsed"],
                "summary_counts": aggregate(folder_tasks + [{"stale": True} for w in assets if w["stale"]]),
                "windows": assets,
            })
        unfiled = sorted((asset for ref, asset in live.items() if ref not in assigned),
                         key=lambda item: item["order"])
        return {
            "schema_version": _VERSION,
            "folders": folders,
            "windows": list(live.values()),
            "unfiled_windows": unfiled,
            "tasks": task_list,
        }
