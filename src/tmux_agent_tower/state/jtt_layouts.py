"""Additive logical folder/window layout for JTT workspaces.

The existing workspace ``terminal_ids`` arrays remain the canonical session
references. This store contains only per-workspace placement metadata; absent
placements are projected into an in-memory default folder/window on read.
"""

from __future__ import annotations

import json
import os
import secrets
import stat
from pathlib import Path

from .jtt_store_lock import state_write_lock


class JTTLayoutStoreError(RuntimeError):
    """Logical workspace layout is invalid or cannot be safely updated."""


class JTTLayoutStoreCommittedDurabilityError(JTTLayoutStoreError):
    """A layout replace committed, but syncing its directory was not confirmed."""

    def __init__(self, workspace_id: str, item_id: str):
        super().__init__("Layout committed but directory durability is unconfirmed")
        self.committed = True
        self.workspace_id = workspace_id
        self.item_id = item_id


_MAX_FILE_BYTES = 512 * 1024
_DEFAULT_FOLDER = {"id": "folder-default", "name": "기본 폴더"}
_DEFAULT_WINDOW = {"id": "window-default", "folder_id": "folder-default", "name": "기본 Window"}


class JTTLayoutStore:
    """Private schema-v1 placement store that never owns or stops PTYs."""

    SCHEMA_VERSION = 1

    def __init__(self, root: str | Path):
        self.root = Path(root).expanduser()
        self.path = self.root / "jtt_layouts.json"

    def layout(self, workspace_id: str, terminal_ids) -> dict:
        canonical = _canonical_ids(terminal_ids)
        records = self._read()
        record = records.get(workspace_id)
        if record is None:
            return _project(workspace_id, canonical, [], [], {})
        folders = [dict(item) for item in record["folders"]]
        windows = [dict(item) for item in record["windows"]]
        placements = {key: value for key, value in record["placements"].items()
                      if key in set(canonical)}
        return _project(workspace_id, canonical, folders, windows, placements)

    def create_folder(self, workspace_id: str, name: str) -> dict:
        safe_name = _name(name)
        with state_write_lock(self.root, self._ensure_private_root, JTTLayoutStoreError):
            records = self._read()
            record = _record(records, workspace_id)
            if any(item["name"].casefold() == safe_name.casefold()
                   for item in record["folders"] + [_DEFAULT_FOLDER]):
                raise JTTLayoutStoreError("Folder name already exists")
            item = {"id": "folder-" + secrets.token_hex(8), "name": safe_name}
            record["folders"].append(item)
            self._write(records, workspace_id=workspace_id, item_id=item["id"])
            return dict(item)

    def rename_folder(self, workspace_id: str, folder_id: str, name: str) -> dict:
        safe_name = _name(name)
        if folder_id == _DEFAULT_FOLDER["id"]:
            raise JTTLayoutStoreError("The default folder cannot be renamed")
        with state_write_lock(self.root, self._ensure_private_root, JTTLayoutStoreError):
            records = self._read()
            record = _record(records, workspace_id)
            target = _find(record["folders"], folder_id, "Folder")
            if any(item["id"] != folder_id and item["name"].casefold() == safe_name.casefold()
                   for item in record["folders"] + [_DEFAULT_FOLDER]):
                raise JTTLayoutStoreError("Folder name already exists")
            target["name"] = safe_name
            self._write(records, workspace_id=workspace_id, item_id=folder_id)
            return dict(target)

    def delete_folder(self, workspace_id: str, folder_id: str) -> dict:
        if folder_id == _DEFAULT_FOLDER["id"]:
            raise JTTLayoutStoreError("The default folder cannot be deleted")
        with state_write_lock(self.root, self._ensure_private_root, JTTLayoutStoreError):
            records = self._read()
            record = _record(records, workspace_id)
            target = _find(record["folders"], folder_id, "Folder")
            moving = {item["id"] for item in record["windows"]
                      if item["folder_id"] == folder_id}
            record["placements"] = {session_id: window_id
                                     for session_id, window_id in record["placements"].items()
                                     if window_id not in moving}
            record["windows"] = [item for item in record["windows"]
                                 if item["folder_id"] != folder_id]
            record["folders"] = [item for item in record["folders"]
                                 if item["id"] != folder_id]
            self._write(records, workspace_id=workspace_id, item_id=folder_id)
            return dict(target)

    def create_window(self, workspace_id: str, folder_id: str, name: str) -> dict:
        safe_name = _name(name)
        with state_write_lock(self.root, self._ensure_private_root, JTTLayoutStoreError):
            records = self._read()
            record = _record(records, workspace_id)
            _folder(record, folder_id)
            if any(item["folder_id"] == folder_id
                   and item["name"].casefold() == safe_name.casefold()
                   for item in record["windows"] + [_DEFAULT_WINDOW]):
                raise JTTLayoutStoreError("Window name already exists in this folder")
            item = {"id": "window-" + secrets.token_hex(8),
                    "folder_id": folder_id, "name": safe_name}
            record["windows"].append(item)
            self._write(records, workspace_id=workspace_id, item_id=item["id"])
            return dict(item)

    def rename_window(self, workspace_id: str, window_id: str, name: str) -> dict:
        safe_name = _name(name)
        if window_id == _DEFAULT_WINDOW["id"]:
            raise JTTLayoutStoreError("The default Window cannot be renamed")
        with state_write_lock(self.root, self._ensure_private_root, JTTLayoutStoreError):
            records = self._read()
            record = _record(records, workspace_id)
            target = _find(record["windows"], window_id, "Window")
            if any(item["id"] != window_id and item["folder_id"] == target["folder_id"]
                   and item["name"].casefold() == safe_name.casefold()
                   for item in record["windows"] + [_DEFAULT_WINDOW]):
                raise JTTLayoutStoreError("Window name already exists in this folder")
            target["name"] = safe_name
            self._write(records, workspace_id=workspace_id, item_id=window_id)
            return dict(target)

    def delete_window(self, workspace_id: str, window_id: str) -> dict:
        if window_id == _DEFAULT_WINDOW["id"]:
            raise JTTLayoutStoreError("The default Window cannot be deleted")
        with state_write_lock(self.root, self._ensure_private_root, JTTLayoutStoreError):
            records = self._read()
            record = _record(records, workspace_id)
            target = _find(record["windows"], window_id, "Window")
            record["placements"] = {session_id: placed_window
                                     for session_id, placed_window in record["placements"].items()
                                     if placed_window != window_id}
            record["windows"] = [item for item in record["windows"]
                                 if item["id"] != window_id]
            self._write(records, workspace_id=workspace_id, item_id=window_id)
            return dict(target)

    def move_terminal(self, workspace_id: str, session_id: str, window_id: str,
                      canonical_terminal_ids) -> None:
        canonical = set(_canonical_ids(canonical_terminal_ids))
        if session_id not in canonical:
            raise JTTLayoutStoreError("Terminal is not a member of this workspace")
        with state_write_lock(self.root, self._ensure_private_root, JTTLayoutStoreError):
            records = self._read()
            record = _record(records, workspace_id)
            _window(record, window_id)
            if window_id == _DEFAULT_WINDOW["id"]:
                record["placements"].pop(session_id, None)
            else:
                record["placements"][session_id] = window_id
            self._write(records, workspace_id=workspace_id, item_id=session_id)

    def _read(self) -> dict:
        if not os.path.lexists(self.path):
            return {}
        self._validate_root()
        self._validate_file()
        fd = os.open(self.path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) & 0o077 or info.st_nlink != 1):
            os.close(fd)
            raise JTTLayoutStoreError("JTT layout file is not private")
        try:
            with os.fdopen(fd, "rb") as stream:
                raw = stream.read(_MAX_FILE_BYTES + 1)
            if len(raw) > _MAX_FILE_BYTES:
                raise JTTLayoutStoreError("JTT layout data exceeds the allowed size")
            payload = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
        except JTTLayoutStoreError:
            raise
        except (OSError, UnicodeError, ValueError, RecursionError) as exc:
            raise JTTLayoutStoreError("JTT layout data is unreadable") from exc
        if not isinstance(payload, dict) or payload.get("schema_version") != self.SCHEMA_VERSION:
            raise JTTLayoutStoreError("Unsupported JTT layout schema")
        values = payload.get("workspaces")
        if not isinstance(values, list) or len(values) > 1000:
            raise JTTLayoutStoreError("Invalid JTT layout workspaces")
        records = {}
        for value in values:
            record = _validate_record(value)
            if record["workspace_id"] in records:
                raise JTTLayoutStoreError("Duplicate JTT layout workspace")
            records[record["workspace_id"]] = record
        return records

    def _write(self, records: dict, *, workspace_id: str = "", item_id: str = "") -> None:
        self._ensure_private_root()
        self._validate_file(allow_missing=True)
        payload = {"schema_version": self.SCHEMA_VERSION,
                   "workspaces": list(records.values())}
        encoded = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        if len(encoded) > _MAX_FILE_BYTES:
            raise JTTLayoutStoreError("JTT layout data exceeds the allowed size")
        temp = self.root / (".jtt_layouts." + secrets.token_hex(8) + ".tmp")
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                     getattr(os, "O_NOFOLLOW", 0), 0o600)
        replaced = False
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb", closefd=True) as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp, self.path)
            replaced = True
            dir_fd = os.open(self.root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError as exc:
            if not replaced:
                try:
                    temp.unlink()
                except OSError:
                    pass
                raise JTTLayoutStoreError("Unable to write private JTT layout data") from exc
            raise JTTLayoutStoreCommittedDurabilityError(workspace_id, item_id) from exc

    def _ensure_private_root(self) -> None:
        absolute = Path(os.path.abspath(os.fspath(self.root)))
        current = Path(absolute.anchor)
        for part in absolute.parts[1:]:
            current = current / part
            try:
                info = current.lstat()
            except FileNotFoundError:
                try:
                    current.mkdir(mode=0o700)
                except FileExistsError:
                    pass
                info = current.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise JTTLayoutStoreError("Unsafe JTT layout directory")
        self._validate_root()

    def _validate_root(self) -> None:
        absolute = Path(os.path.abspath(os.fspath(self.root)))
        current = Path(absolute.anchor)
        for part in absolute.parts[1:]:
            current = current / part
            try:
                info = current.lstat()
            except OSError as exc:
                raise JTTLayoutStoreError("JTT layout directory is unavailable") from exc
            if stat.S_ISLNK(info.st_mode):
                raise JTTLayoutStoreError("Unsafe JTT layout directory")
        info = absolute.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) & 0o077):
            raise JTTLayoutStoreError("JTT layout directory is not private")

    def _validate_file(self, *, allow_missing: bool = False) -> None:
        if not os.path.lexists(self.path):
            if allow_missing:
                return
            raise JTTLayoutStoreError("JTT layout file is missing")
        info = self.path.lstat()
        if (stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077
                or info.st_nlink != 1):
            raise JTTLayoutStoreError("JTT layout file is not private")


def _canonical_ids(values) -> list[str]:
    if not isinstance(values, (list, tuple)):
        raise JTTLayoutStoreError("Invalid canonical terminal references")
    result = []
    for value in values:
        if not isinstance(value, str) or not value or "\x00" in value:
            raise JTTLayoutStoreError("Invalid canonical terminal reference")
        if value not in result:
            result.append(value)
    return result


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("Duplicate JSON key")
        value[key] = item
    return value


def _name(value: str) -> str:
    if not isinstance(value, str):
        raise JTTLayoutStoreError("Folder or Window name must be text")
    safe = " ".join(value.split())
    if not safe or len(safe) > 64 or any(ord(char) < 32 for char in safe):
        raise JTTLayoutStoreError("Folder or Window name must be 1-64 printable characters")
    return safe


def _record(records: dict, workspace_id: str) -> dict:
    if not isinstance(workspace_id, str) or not workspace_id or "\x00" in workspace_id:
        raise JTTLayoutStoreError("Invalid workspace identity")
    return records.setdefault(workspace_id, {"workspace_id": workspace_id,
        "folders": [], "windows": [], "placements": {}})


def _find(items: list[dict], item_id: str, label: str) -> dict:
    matches = [item for item in items if item["id"] == item_id]
    if len(matches) != 1:
        raise JTTLayoutStoreError(f"{label} no longer exists")
    return matches[0]


def _folder(record: dict, folder_id: str) -> dict:
    if folder_id == _DEFAULT_FOLDER["id"]:
        return _DEFAULT_FOLDER
    return _find(record["folders"], folder_id, "Folder")


def _window(record: dict, window_id: str) -> dict:
    if window_id == _DEFAULT_WINDOW["id"]:
        return _DEFAULT_WINDOW
    return _find(record["windows"], window_id, "Window")


def _validate_record(value: object) -> dict:
    if not isinstance(value, dict) or set(value) != {
        "workspace_id", "folders", "windows", "placements",
    }:
        raise JTTLayoutStoreError("Invalid JTT layout record")
    workspace_id = value["workspace_id"]
    folders, windows, placements = value["folders"], value["windows"], value["placements"]
    if not isinstance(workspace_id, str) or not workspace_id or "\x00" in workspace_id:
        raise JTTLayoutStoreError("Invalid JTT layout workspace")
    if (not isinstance(folders, list) or len(folders) > 1000
            or not isinstance(windows, list) or len(windows) > 2000
            or not isinstance(placements, dict) or len(placements) > 10000):
        raise JTTLayoutStoreError("Invalid JTT layout collections")
    normalized_folders = []
    ids = {_DEFAULT_FOLDER["id"]}
    names = {_DEFAULT_FOLDER["name"].casefold()}
    for item in folders:
        if not isinstance(item, dict) or set(item) != {"id", "name"}:
            raise JTTLayoutStoreError("Invalid JTT folder")
        item_id, name = item["id"], _name(item["name"])
        if (not isinstance(item_id, str) or not item_id.startswith("folder-")
                or item_id in ids or name.casefold() in names):
            raise JTTLayoutStoreError("Duplicate or invalid JTT folder")
        ids.add(item_id); names.add(name.casefold())
        normalized_folders.append({"id": item_id, "name": name})
    normalized_windows = []
    window_ids = {_DEFAULT_WINDOW["id"]}
    window_names = {(_DEFAULT_FOLDER["id"], _DEFAULT_WINDOW["name"].casefold())}
    folder_ids = ids
    for item in windows:
        if not isinstance(item, dict) or set(item) != {"id", "folder_id", "name"}:
            raise JTTLayoutStoreError("Invalid JTT Window")
        item_id, folder_id, name = item["id"], item["folder_id"], _name(item["name"])
        if (not isinstance(item_id, str) or not item_id.startswith("window-")
                or item_id in window_ids or folder_id not in folder_ids
                or (folder_id, name.casefold()) in window_names):
            raise JTTLayoutStoreError("Duplicate or invalid JTT Window")
        window_ids.add(item_id); window_names.add((folder_id, name.casefold()))
        normalized_windows.append({"id": item_id, "folder_id": folder_id, "name": name})
    normalized_placements = {}
    for session_id, window_id in placements.items():
        if (not isinstance(session_id, str) or not session_id or "\x00" in session_id
                or not isinstance(window_id, str) or window_id not in window_ids
                or window_id == _DEFAULT_WINDOW["id"] or session_id in normalized_placements):
            raise JTTLayoutStoreError("Invalid JTT terminal placement")
        normalized_placements[session_id] = window_id
    return {"workspace_id": workspace_id, "folders": normalized_folders,
            "windows": normalized_windows, "placements": normalized_placements}


def _project(workspace_id: str, canonical: list[str], folders: list[dict],
             windows: list[dict], placements: dict[str, str]) -> dict:
    all_folders = [dict(_DEFAULT_FOLDER), *folders]
    all_windows = [dict(_DEFAULT_WINDOW), *windows]
    canonical_set = set(canonical)
    valid_window_ids = {item["id"] for item in all_windows}
    assigned = {session_id: window_id for session_id, window_id in placements.items()
                if session_id in canonical_set and window_id in valid_window_ids}
    projected = []
    for window in all_windows:
        ids = [session_id for session_id in canonical
               if assigned.get(session_id, _DEFAULT_WINDOW["id"]) == window["id"]]
        folder = next(item for item in all_folders if item["id"] == window["folder_id"])
        projected.append({**window, "terminal_ids": ids, "folder_name": folder["name"]})
    return {"workspace_id": workspace_id, "folders": all_folders,
            "windows": projected, "placements": assigned,
            "terminal_ids": canonical}
