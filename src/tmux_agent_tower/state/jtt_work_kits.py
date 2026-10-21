"""Private, local-only templates for composing future JTT terminal workspaces.

Work Kits describe terminals but never create or reconnect processes. Their
data is intentionally kept separate from the saved running-workspace store.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import stat
from pathlib import Path
from typing import Iterable

from .jtt_store_lock import state_write_lock


class WorkKitStoreError(RuntimeError):
    """Work Kit data is invalid or cannot be accessed safely."""


class WorkKitStoreCommittedDurabilityError(WorkKitStoreError):
    """A Work Kit replacement committed, but its directory sync failed."""

    committed = True

    def __init__(self, message: str, *, record_id: str | None = None):
        super().__init__(message)
        self.record_id = record_id


_LAYOUT_BY_COUNT = {
    1: "single",
    2: "split_horizontal",
    3: "two_top_one_wide",
    4: "grid_2x2",
}
_KIT_ID = re.compile(r"work-kit-[0-9a-f]{16}\Z")
_TERMINAL_ID = re.compile(r"template-[0-9a-f]{16}\Z")
_MAX_FILE_BYTES = 256 * 1024


def layout_for_terminal_count(count: int) -> str:
    """Return the existing JTT card layout name for one through four cards."""
    if type(count) is not int:
        raise WorkKitStoreError("A Work Kit must contain between one and four terminals")
    try:
        return _LAYOUT_BY_COUNT[count]
    except (KeyError, TypeError) as exc:
        raise WorkKitStoreError("A Work Kit must contain between one and four terminals") from exc


class JTTWorkKitStore:
    """Strict schema-v1 Work Kit JSON store with private atomic writes."""

    SCHEMA_VERSION = 1

    def __init__(self, root: str | Path):
        self.root = Path(root).expanduser()
        self.path = self.root / "work_kits.json"

    def kits(self) -> list[dict]:
        """Read and validate Work Kits without creating or modifying files."""
        if not os.path.lexists(self.path):
            return []
        self._validate_root()
        self._validate_file_path()
        fd = None
        try:
            fd = os.open(self.path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            info = os.fstat(fd)
            self._validate_file_info(info)
            with os.fdopen(fd, "r", encoding="utf-8") as stream:
                fd = None
                raw = stream.read(_MAX_FILE_BYTES + 1)
            if len(raw.encode("utf-8")) > _MAX_FILE_BYTES:
                raise WorkKitStoreError("Work Kit data exceeds the allowed size")
            payload = json.loads(raw, object_pairs_hook=_unique_object_pairs)
        except WorkKitStoreError:
            raise
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError, RecursionError) as exc:
            raise WorkKitStoreError("Work Kit data is unreadable or malformed") from exc
        finally:
            if fd is not None:
                os.close(fd)
        if not isinstance(payload, dict) or set(payload) != {"schema_version", "kits"}:
            raise WorkKitStoreError("Invalid Work Kit document")
        if type(payload.get("schema_version")) is not int or payload["schema_version"] != self.SCHEMA_VERSION:
            raise WorkKitStoreError("Unsupported Work Kit schema")
        values = payload.get("kits")
        if not isinstance(values, list) or len(values) > 1000:
            raise WorkKitStoreError("Invalid Work Kit list")
        result = []
        seen_ids: set[str] = set()
        seen_names: set[str] = set()
        seen_template_ids: set[str] = set()
        for value in values:
            kit = self._validate_kit(value)
            if kit["id"] in seen_ids or kit["name"].casefold() in seen_names:
                raise WorkKitStoreError("Duplicate Work Kit identity or name")
            template_ids = {terminal["id"] for terminal in kit["terminals"]}
            if seen_template_ids & template_ids:
                raise WorkKitStoreError("Duplicate Work Kit template ID")
            seen_ids.add(kit["id"])
            seen_names.add(kit["name"].casefold())
            seen_template_ids.update(template_ids)
            result.append(kit)
        return result

    def create_kit(self, name: str, description: str,
                   terminals: Iterable[dict]) -> dict:
        """Create a descriptive template; this method never starts terminals."""
        with state_write_lock(self.root, self._ensure_private_root, WorkKitStoreError):
            existing = self.kits()
            safe_name = " ".join(_text(name, "name", 80, one_line=True).split())
            safe_description = _text(description, "description", 240, one_line=True,
                                      allow_empty=True)
            if any(item["name"].casefold() == safe_name.casefold() for item in existing):
                raise WorkKitStoreError("A Work Kit with this name already exists")
            specs = list(terminals)
            layout = layout_for_terminal_count(len(specs))
            normalized = []
            names: set[str] = set()
            used_template_ids = {
                terminal["id"] for kit in existing for terminal in kit["terminals"]
            }
            for spec in specs:
                if not isinstance(spec, dict) or set(spec) - {"name", "role", "working_directory"}:
                    raise WorkKitStoreError("Invalid Work Kit terminal template")
                terminal_name = " ".join(
                    _text(spec.get("name"), "terminal name", 80, one_line=True).split()
                )
                role = " ".join(_text(spec.get("role"), "terminal role", 120,
                                      one_line=True).split())
                working_directory = spec.get("working_directory")
                if working_directory is not None:
                    working_directory = _text(working_directory, "working directory", 1024,
                                              one_line=True)
                folded = terminal_name.casefold()
                if folded in names:
                    raise WorkKitStoreError("Work Kit terminal names must be unique")
                names.add(folded)
                template_id = "template-" + secrets.token_hex(8)
                while template_id in used_template_ids:
                    template_id = "template-" + secrets.token_hex(8)
                used_template_ids.add(template_id)
                normalized.append({
                    "id": template_id,
                    "name": terminal_name,
                    "role": role,
                    "working_directory": working_directory,
                })
            kit_id = "work-kit-" + secrets.token_hex(8)
            used_kit_ids = {item["id"] for item in existing}
            while kit_id in used_kit_ids:
                kit_id = "work-kit-" + secrets.token_hex(8)
            kit = {
                "id": kit_id,
                "name": safe_name,
                "description": safe_description,
                "layout": layout,
                "terminals": normalized,
            }
            # Use the same strict validator for objects written by this API.
            self._validate_kit(kit)
            try:
                self._write([*existing, kit])
            except WorkKitStoreCommittedDurabilityError as exc:
                raise WorkKitStoreCommittedDurabilityError(
                    str(exc), record_id=kit["id"],
                ) from exc
            return kit

    def delete_kit(self, kit_id: str, *, expected_snapshot: dict) -> dict:
        """Delete one Work Kit only if its complete saved definition is unchanged."""
        with state_write_lock(self.root, self._ensure_private_root, WorkKitStoreError):
            kits = self.kits()
            matches = [kit for kit in kits if kit["id"] == kit_id]
            if len(matches) != 1:
                raise WorkKitStoreError("Work Kit no longer exists")
            current = matches[0]
            try:
                expected = self._validate_kit(expected_snapshot)
            except WorkKitStoreError as exc:
                raise WorkKitStoreError("Work Kit confirmation snapshot is invalid") from exc
            if current != expected:
                raise WorkKitStoreError("Work Kit changed before deletion")
            self._write([kit for kit in kits if kit["id"] != kit_id])
            return current

    def rename_kit(self, kit_id: str, name: str, *, expected_snapshot: dict) -> dict:
        """Rename one template without changing its terminal definitions."""
        safe_name = " ".join(_text(name, "name", 80, one_line=True).split())
        with state_write_lock(self.root, self._ensure_private_root, WorkKitStoreError):
            kits = self.kits()
            matches = [kit for kit in kits if kit["id"] == kit_id]
            if len(matches) != 1:
                raise WorkKitStoreError("Work Kit no longer exists")
            current = matches[0]
            try:
                expected = self._validate_kit(expected_snapshot)
            except WorkKitStoreError as exc:
                raise WorkKitStoreError("Work Kit confirmation snapshot is invalid") from exc
            if current != expected:
                raise WorkKitStoreError("Work Kit changed before rename")
            if any(kit["id"] != kit_id and kit["name"].casefold() == safe_name.casefold()
                   for kit in kits):
                raise WorkKitStoreError("A Work Kit with this name already exists")
            renamed = {**current, "name": safe_name}
            self._validate_kit(renamed)
            try:
                self._write([renamed if kit["id"] == kit_id else kit for kit in kits])
            except WorkKitStoreCommittedDurabilityError as exc:
                raise WorkKitStoreCommittedDurabilityError(
                    str(exc), record_id=kit_id,
                ) from exc
            return renamed

    def _validate_kit(self, value: object) -> dict:
        if not isinstance(value, dict) or set(value) != {
            "id", "name", "description", "layout", "terminals",
        }:
            raise WorkKitStoreError("Invalid Work Kit record")
        kit_id = value.get("id")
        if not isinstance(kit_id, str) or not _KIT_ID.fullmatch(kit_id):
            raise WorkKitStoreError("Invalid Work Kit ID")
        name = _text(value.get("name"), "name", 80, one_line=True)
        description = _text(value.get("description"), "description", 240,
                             one_line=True, allow_empty=True)
        terminals = value.get("terminals")
        if not isinstance(terminals, list) or not 1 <= len(terminals) <= 4:
            raise WorkKitStoreError("A Work Kit must contain between one and four terminals")
        expected_layout = layout_for_terminal_count(len(terminals))
        if value.get("layout") != expected_layout:
            raise WorkKitStoreError("Work Kit layout does not match its terminal count")
        normalized = []
        ids: set[str] = set()
        names: set[str] = set()
        for terminal in terminals:
            if not isinstance(terminal, dict) or set(terminal) != {
                "id", "name", "role", "working_directory",
            }:
                raise WorkKitStoreError("Invalid Work Kit terminal template")
            template_id = terminal.get("id")
            if not isinstance(template_id, str) or not _TERMINAL_ID.fullmatch(template_id):
                raise WorkKitStoreError("Invalid Work Kit template ID")
            terminal_name = _text(terminal.get("name"), "terminal name", 80, one_line=True)
            role = _text(terminal.get("role"), "terminal role", 120, one_line=True)
            working_directory = terminal.get("working_directory")
            if working_directory is not None:
                working_directory = _text(working_directory, "working directory", 1024,
                                          one_line=True)
            folded_name = terminal_name.casefold()
            if template_id in ids or folded_name in names:
                raise WorkKitStoreError("Duplicate Work Kit terminal identity or name")
            ids.add(template_id)
            names.add(folded_name)
            normalized.append({
                "id": template_id,
                "name": terminal_name,
                "role": role,
                "working_directory": working_directory,
            })
        return {"id": kit_id, "name": name, "description": description,
                "layout": expected_layout, "terminals": normalized}

    def _write(self, kits: list[dict]) -> None:
        temp = None
        fd = None
        replaced = False
        try:
            self._ensure_private_root()
            self._validate_file_path(allow_missing=True)
            payload = {"schema_version": self.SCHEMA_VERSION, "kits": kits}
            encoded = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
            if len(encoded) > _MAX_FILE_BYTES:
                raise WorkKitStoreError("Work Kit data exceeds the allowed size")
            temp = self.root / (".work_kits." + secrets.token_hex(8) + ".tmp")
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(temp, flags, 0o600)
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb", closefd=True) as stream:
                fd = None
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
        except Exception as exc:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
            if temp is not None:
                try:
                    temp.unlink()
                except OSError:
                    pass
            if isinstance(exc, WorkKitStoreError):
                raise
            if isinstance(exc, OSError):
                if replaced:
                    raise WorkKitStoreCommittedDurabilityError(
                        "Work Kit replacement completed but directory durability could not be confirmed"
                    ) from exc
                raise WorkKitStoreError("Unable to write private Work Kit data") from exc
            raise

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
                raise WorkKitStoreError("Unsafe JTT state directory")
        self._validate_root()

    def _validate_root(self) -> None:
        absolute = Path(os.path.abspath(os.fspath(self.root)))
        current = Path(absolute.anchor)
        for part in absolute.parts[1:]:
            current = current / part
            try:
                info = current.lstat()
            except OSError as exc:
                raise WorkKitStoreError("JTT state directory is unavailable") from exc
            if stat.S_ISLNK(info.st_mode):
                raise WorkKitStoreError("Unsafe JTT state directory")
        info = absolute.lstat()
        current_uid = getattr(os, "getuid", None)
        if not stat.S_ISDIR(info.st_mode):
            raise WorkKitStoreError("Unsafe JTT state directory")
        if ((current_uid is not None and info.st_uid != current_uid())
                or stat.S_IMODE(info.st_mode) & 0o077):
            raise WorkKitStoreError("JTT state directory is not private")

    def _validate_file_path(self, *, allow_missing: bool = False) -> None:
        if not os.path.lexists(self.path):
            if allow_missing:
                return
            raise WorkKitStoreError("Work Kit file is missing")
        try:
            info = self.path.lstat()
        except OSError as exc:
            raise WorkKitStoreError("Work Kit file is unavailable") from exc
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise WorkKitStoreError("Unsafe Work Kit file")
        self._validate_file_info(info)

    @staticmethod
    def _validate_file_info(info: os.stat_result) -> None:
        current_uid = getattr(os, "getuid", None)
        if (not stat.S_ISREG(info.st_mode)
                or (current_uid is not None and info.st_uid != current_uid())
                or stat.S_IMODE(info.st_mode) & 0o077 or info.st_nlink != 1):
            raise WorkKitStoreError("Work Kit file is not private")


def _text(value: object, field: str, maximum: int, *, one_line: bool,
          allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise WorkKitStoreError(f"Invalid Work Kit {field}")
    if len(value) > maximum or (not allow_empty and not value.strip()):
        raise WorkKitStoreError(f"Invalid Work Kit {field}")
    if one_line and any(char in value for char in "\r\n\x00"):
        raise WorkKitStoreError(f"Invalid Work Kit {field}")
    if any(ord(char) < 0x20 and char != "\t" for char in value):
        raise WorkKitStoreError(f"Invalid Work Kit {field}")
    return value


def _unique_object_pairs(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result
