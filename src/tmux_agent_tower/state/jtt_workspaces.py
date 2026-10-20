"""Private persistence for JTT workspaces and their terminal-session IDs.

The legacy Tower stores are deliberately not imported here. Opening JTT does
not create a workspace file or start a shell; writes occur only after an
explicit workspace/configuration action or terminal creation.
"""

from __future__ import annotations

import json
import os
import secrets
import stat
from pathlib import Path
from typing import Iterable

from .jtt_store_lock import state_write_lock


class WorkspaceStoreError(RuntimeError):
    """The JTT workspace file cannot be read or safely updated."""


class WorkspaceStoreCommittedDurabilityError(WorkspaceStoreError):
    """A workspace replacement committed, but its directory sync failed."""

    committed = True

    def __init__(self, message: str, *, record_id: str | None = None):
        super().__init__(message)
        self.record_id = record_id


def default_jtt_root(environ: dict[str, str] | None = None) -> Path:
    env = os.environ if environ is None else environ
    explicit = env.get("JTT_HOME")
    if explicit:
        return Path(explicit).expanduser()
    state_home = env.get("XDG_STATE_HOME")
    if state_home:
        return Path(state_home).expanduser() / "jtt"
    return Path.home() / ".local" / "state" / "jtt"


class JTTWorkspaceStore:
    """Small schema-v1 store with private atomic writes and no legacy migration."""

    SCHEMA_VERSION = 1
    DEFAULT_ID = "workspace-default"

    def __init__(self, root: str | Path | None = None):
        self.root = Path(root).expanduser() if root is not None else default_jtt_root()
        self.path = self.root / "workspaces.json"

    def workspaces(self) -> list[dict]:
        if not os.path.lexists(self.path):
            return [{"id": self.DEFAULT_ID, "name": "내 작업공간", "terminal_ids": [], "saved": False}]
        self._validate_existing_root()
        self._validate_path(self.path, allow_missing=False)
        try:
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(self.path, flags)
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) & 0o077 or info.st_nlink != 1):
                os.close(fd)
                raise WorkspaceStoreError("JTT workspace file is not private")
            with os.fdopen(fd, "r", encoding="utf-8") as stream:
                raw = stream.read(1024 * 1024 + 1)
            if len(raw.encode("utf-8")) > 1024 * 1024:
                raise WorkspaceStoreError("JTT workspace file exceeds the allowed size")
            payload = json.loads(raw)
        except WorkspaceStoreError:
            raise
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise WorkspaceStoreError("JTT workspace data is unreadable") from exc
        if not isinstance(payload, dict) or payload.get("schema_version") != self.SCHEMA_VERSION:
            raise WorkspaceStoreError("Unsupported JTT workspace schema")
        values = payload.get("workspaces")
        if not isinstance(values, list):
            raise WorkspaceStoreError("Invalid JTT workspace list")
        result = []
        seen = set()
        for value in values:
            if not isinstance(value, dict):
                raise WorkspaceStoreError("Invalid JTT workspace record")
            workspace_id = value.get("id")
            name = value.get("name")
            terminals = value.get("terminal_ids")
            if (not isinstance(workspace_id, str) or not workspace_id
                    or workspace_id in seen or not isinstance(name, str) or not name.strip()
                    or not isinstance(terminals, list)
                    or any(not isinstance(item, str) or not item for item in terminals)):
                raise WorkspaceStoreError("Invalid JTT workspace record")
            seen.add(workspace_id)
            result.append({"id": workspace_id, "name": name,
                           "terminal_ids": list(dict.fromkeys(terminals)),
                           "saved": workspace_id != self.DEFAULT_ID})
        if not any(item["id"] == self.DEFAULT_ID for item in result):
            result.insert(0, {"id": self.DEFAULT_ID, "name": "내 작업공간",
                              "terminal_ids": [], "saved": False})
        return result

    def save_configuration(self, workspace_id: str, name: str, terminal_ids: Iterable[str]) -> dict:
        """Create or update a named workspace snapshot; never starts terminals."""
        safe_name = " ".join(str(name).split())
        ids = list(dict.fromkeys(str(item) for item in terminal_ids if str(item)))
        if not safe_name or len(safe_name) > 80 or any("\x00" in item for item in ids):
            raise WorkspaceStoreError("Invalid workspace name or terminal ID")
        with state_write_lock(self.root, self._ensure_private_root, WorkspaceStoreError):
            values = self.workspaces()
            target = next((item for item in values if item["id"] == workspace_id), None)
            if target is None:
                target = {"id": "workspace-" + secrets.token_hex(8), "name": safe_name,
                          "terminal_ids": ids, "saved": True}
                values.append(target)
            else:
                target["name"] = safe_name
                target["terminal_ids"] = ids
                target["saved"] = target["id"] != self.DEFAULT_ID
            try:
                self._write(values)
            except WorkspaceStoreCommittedDurabilityError as exc:
                raise WorkspaceStoreCommittedDurabilityError(
                    str(exc), record_id=target["id"],
                ) from exc
            return dict(target)

    def create_workspace(self, name: str) -> dict:
        return self.save_configuration("", name, ())

    def delete_workspace(self, workspace_id: str, *, expected_snapshot: dict) -> dict:
        """Delete one saved workspace and preserve terminal references atomically.

        Terminal sessions are never stopped here. Any session ID whose final
        workspace reference would be removed is transferred to the default
        workspace in the same private atomic rewrite.
        """
        if workspace_id == self.DEFAULT_ID:
            raise WorkspaceStoreError("The default workspace cannot be deleted")
        with state_write_lock(self.root, self._ensure_private_root, WorkspaceStoreError):
            values = self.workspaces()
            matches = [item for item in values if item["id"] == workspace_id]
            if len(matches) != 1:
                raise WorkspaceStoreError("Workspace no longer exists")
            target = matches[0]
            if not _workspace_snapshot_matches(target, expected_snapshot):
                raise WorkspaceStoreError("Workspace changed before deletion")

            remaining = [item for item in values if item["id"] != workspace_id]
            default = next((item for item in remaining if item["id"] == self.DEFAULT_ID), None)
            if default is None:
                default = {"id": self.DEFAULT_ID, "name": "내 작업공간",
                           "terminal_ids": [], "saved": False}
                remaining.insert(0, default)
            retained_ids = {terminal_id for item in remaining
                            for terminal_id in item.get("terminal_ids", ())}
            default_ids = list(default.get("terminal_ids", ()))
            for terminal_id in target["terminal_ids"]:
                if terminal_id not in retained_ids and terminal_id not in default_ids:
                    default_ids.append(terminal_id)
            default["terminal_ids"] = default_ids
            self._write(remaining)
            return dict(target)

    def remove_terminal_reference(self, workspace_id: str, terminal_id: str, *,
                                  expected_snapshot: dict) -> dict:
        """Remove one workspace card reference without stopping its PTY.

        If this was the final reference, preserve the session ID in the default
        workspace, or in a uniquely named recovery workspace when removing it
        from the default workspace itself.
        """
        if not isinstance(terminal_id, str) or not terminal_id or "\x00" in terminal_id:
            raise WorkspaceStoreError("Invalid terminal ID")
        with state_write_lock(self.root, self._ensure_private_root, WorkspaceStoreError):
            values = self.workspaces()
            matches = [item for item in values if item["id"] == workspace_id]
            if len(matches) != 1:
                raise WorkspaceStoreError("Workspace no longer exists")
            source = matches[0]
            if not _workspace_snapshot_matches(source, expected_snapshot):
                raise WorkspaceStoreError("Workspace changed before card cleanup")
            if terminal_id not in source["terminal_ids"]:
                raise WorkspaceStoreError("Terminal card is no longer in this workspace")

            source["terminal_ids"] = [item for item in source["terminal_ids"]
                                      if item != terminal_id]
            other_references = [item for item in values if item["id"] != workspace_id
                                and terminal_id in item["terminal_ids"]]
            recovery_workspace_id = None
            if not other_references:
                if workspace_id != self.DEFAULT_ID:
                    default = next((item for item in values
                                    if item["id"] == self.DEFAULT_ID), None)
                    if default is None:
                        default = {"id": self.DEFAULT_ID, "name": "내 작업공간",
                                   "terminal_ids": [], "saved": False}
                        values.insert(0, default)
                    default["terminal_ids"] = list(dict.fromkeys(
                        [*default["terminal_ids"], terminal_id]
                    ))
                else:
                    names = {item["name"].casefold() for item in values}
                    recovery_name = "복구 보관함"
                    suffix = 2
                    while recovery_name.casefold() in names:
                        recovery_name = f"복구 보관함 {suffix}"
                        suffix += 1
                    recovery_workspace_id = "workspace-" + secrets.token_hex(8)
                    used_ids = {item["id"] for item in values}
                    while recovery_workspace_id in used_ids:
                        recovery_workspace_id = "workspace-" + secrets.token_hex(8)
                    values.append({"id": recovery_workspace_id, "name": recovery_name,
                                   "terminal_ids": [terminal_id], "saved": True})
            self._write(values)
            return {"workspace_id": workspace_id, "terminal_id": terminal_id,
                    "recovery_workspace_id": recovery_workspace_id,
                    "preserved_elsewhere": bool(other_references)}

    def set_terminal_ids(self, workspace_id: str, terminal_ids: Iterable[str]) -> None:
        with state_write_lock(self.root, self._ensure_private_root, WorkspaceStoreError):
            values = self.workspaces()
            target = next((item for item in values if item["id"] == workspace_id), None)
            if target is None:
                raise WorkspaceStoreError("Workspace no longer exists")
            ids = list(dict.fromkeys(str(item) for item in terminal_ids if str(item)))
            if any("\x00" in item for item in ids):
                raise WorkspaceStoreError("Invalid terminal ID")
            target["terminal_ids"] = ids
            self._write(values)

    def _write(self, values: list[dict]) -> None:
        self._ensure_private_root()
        self._validate_path(self.path, allow_missing=True)
        payload = {
            "schema_version": self.SCHEMA_VERSION,
            "workspaces": [{"id": item["id"], "name": item["name"],
                            "terminal_ids": list(item.get("terminal_ids") or ())}
                           for item in values],
        }
        encoded = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        temp = self.root / (".workspaces." + secrets.token_hex(8) + ".tmp")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(temp, flags, 0o600)
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
            try:
                os.close(fd)
            except OSError:
                pass
            if not replaced:
                try:
                    temp.unlink()
                except OSError:
                    pass
                raise
            raise WorkspaceStoreCommittedDurabilityError(
                "Workspace replacement completed but directory durability could not be confirmed"
            ) from exc
        except Exception:
            try:
                os.close(fd)
            except OSError:
                pass
            if not replaced:
                try:
                    temp.unlink()
                except OSError:
                    pass
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
            except OSError as exc:
                raise WorkspaceStoreError("Cannot inspect JTT state directory") from exc
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise WorkspaceStoreError("Unsafe JTT state directory")
        self._validate_existing_root()

    def _validate_existing_root(self) -> None:
        absolute = Path(os.path.abspath(os.fspath(self.root)))
        current = Path(absolute.anchor)
        for part in absolute.parts[1:]:
            current = current / part
            try:
                component = current.lstat()
            except OSError as exc:
                raise WorkspaceStoreError("JTT state directory is unavailable") from exc
            if stat.S_ISLNK(component.st_mode):
                raise WorkspaceStoreError("Unsafe JTT state directory")
        info = absolute.lstat()
        if not stat.S_ISDIR(info.st_mode):
            raise WorkspaceStoreError("Unsafe JTT state directory")
        if (hasattr(os, "getuid") and info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) & 0o077):
            raise WorkspaceStoreError("JTT state directory is not private")

    def _validate_path(self, path: Path, *, allow_missing: bool) -> None:
        if not os.path.lexists(path):
            if allow_missing:
                return
            raise WorkspaceStoreError("JTT workspace file is missing")
        if path.is_symlink() or not path.is_file():
            raise WorkspaceStoreError("Unsafe JTT workspace file")
        info = path.stat()
        if hasattr(os, "getuid") and info.st_uid != os.getuid():
            raise WorkspaceStoreError("JTT workspace file has an unexpected owner")
        if stat.S_IMODE(info.st_mode) & 0o077 or info.st_nlink != 1:
            raise WorkspaceStoreError("JTT workspace file is not private")


def _workspace_snapshot_matches(current: dict, expected: object) -> bool:
    if not isinstance(expected, dict):
        return False
    return (expected.get("id") == current["id"]
            and expected.get("name") == current["name"]
            and expected.get("terminal_ids") == current["terminal_ids"]
            and ("saved" not in expected or expected.get("saved") == current["saved"]))
