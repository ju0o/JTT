"""Atomic local persistence for reusable workspace launch choices."""

from __future__ import annotations

import json
import os
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Tuple

from .plan import LaunchPlan, WorkspaceRef, validate_launch_plan
from ..paths import CONFIG_DIR

PRESET_FILE = CONFIG_DIR / "workspace-presets.json"
_VERSION = 1


class PresetStoreError(ValueError):
    """Preset data is invalid or could not be safely persisted."""


@dataclass(frozen=True)
class WorkspacePreset:
    preset_id: str
    name: str
    host_key: str
    host_label: str
    is_remote: bool
    workspaces: Tuple[WorkspaceRef, ...]
    agents: Tuple[str, ...]
    layout: str

    def launch_plan(self) -> LaunchPlan:
        return LaunchPlan(
            host_key=self.host_key,
            host_label=self.host_label,
            is_remote=self.is_remote,
            workspaces=self.workspaces,
            agents=self.agents,
            layout=self.layout,
        )


def new_preset(
    *,
    name: str,
    host_key: str,
    host_label: str,
    is_remote: bool,
    workspaces: Tuple[WorkspaceRef, ...],
    agents: Tuple[str, ...],
    layout: str,
    preset_id: str = "",
) -> WorkspacePreset:
    preset = WorkspacePreset(
        preset_id=preset_id or uuid.uuid4().hex,
        name=name.strip(),
        host_key=host_key,
        host_label=host_label,
        is_remote=is_remote,
        workspaces=tuple(workspaces),
        agents=tuple(agents),
        layout=layout,
    )
    validate_preset(preset)
    return preset


def validate_preset(preset: WorkspacePreset) -> None:
    if not preset.preset_id or len(preset.preset_id) > 80:
        raise PresetStoreError("저장된 작업 ID가 올바르지 않습니다.")
    if not preset.name or len(preset.name) > 80 or any(ord(ch) < 32 for ch in preset.name):
        raise PresetStoreError("이름은 1~80자로 입력하세요.")
    try:
        validate_launch_plan(preset.launch_plan())
    except ValueError as exc:
        raise PresetStoreError(str(exc)) from exc


class WorkspacePresetStore:
    def __init__(self, path: Path | None = None):
        self.path = Path(path if path is not None else PRESET_FILE)

    def list(self) -> list[WorkspacePreset]:
        document = self._read_document()
        return _decode_presets(document)

    def save(self, preset: WorkspacePreset) -> None:
        validate_preset(preset)
        document = self._read_document()
        presets = _decode_presets(document)
        replaced = False
        for index, existing in enumerate(presets):
            if existing.preset_id == preset.preset_id:
                presets[index] = preset
                replaced = True
                break
        if not replaced:
            presets.append(preset)
        document["presets"] = [_encode_preset(item) for item in presets]
        document["version"] = _VERSION
        self._atomic_write(document)

    def delete(self, preset_id: str) -> None:
        document = self._read_document()
        presets = _decode_presets(document)
        remaining = [item for item in presets if item.preset_id != preset_id]
        if len(remaining) == len(presets):
            raise PresetStoreError("저장된 작업을 찾을 수 없습니다.")
        document["presets"] = [_encode_preset(item) for item in remaining]
        document["version"] = _VERSION
        self._atomic_write(document)

    def _read_document(self) -> dict:
        try:
            document = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"version": _VERSION, "presets": []}
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise PresetStoreError("저장된 작업 파일을 읽을 수 없습니다. 파일을 그대로 보존했습니다.") from exc
        if not isinstance(document, dict):
            raise PresetStoreError("저장된 작업 파일 형식이 올바르지 않습니다. 파일을 그대로 보존했습니다.")
        version = document.get("version", 0)
        if type(version) is not int or version not in (0, _VERSION):
            raise PresetStoreError("지원하지 않는 저장된 작업 파일 버전입니다.")
        if not isinstance(document.get("presets", []), list):
            raise PresetStoreError("저장된 작업 목록이 손상되었습니다. 파일을 그대로 보존했습니다.")
        document.setdefault("presets", [])
        return document

    def _atomic_write(self, document: dict) -> None:
        temp_path = None
        fd = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, temp_name = tempfile.mkstemp(prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent)
            temp_path = Path(temp_name)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                fd = None
                os.fchmod(stream.fileno(), 0o600)
                json.dump(document, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_path, self.path)
            temp_path = None
        except OSError as exc:
            raise PresetStoreError("저장된 작업을 안전하게 저장하지 못했습니다.") from exc
        finally:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
            if temp_path is not None:
                try:
                    temp_path.unlink()
                except OSError:
                    pass


def _decode_preset(item) -> WorkspacePreset:
    if not isinstance(item, dict):
        raise PresetStoreError("저장된 작업 항목이 손상되었습니다. 파일을 그대로 보존했습니다.")
    try:
        workspaces = item["workspaces"]
        if not isinstance(workspaces, list):
            raise TypeError
        if any(
            not isinstance(row, dict)
            or not isinstance(row.get("name"), str)
            or not isinstance(row.get("path"), str)
            for row in workspaces
        ):
            raise TypeError
        if not isinstance(item["id"], str) or not isinstance(item["name"], str):
            raise TypeError
        if not isinstance(item["host_key"], str) or not isinstance(item["host_label"], str):
            raise TypeError
        if not isinstance(item["layout"], str) or not isinstance(item["agents"], list):
            raise TypeError
        if any(not isinstance(agent, str) for agent in item["agents"]):
            raise TypeError
        refs = tuple(WorkspaceRef(row["name"], row["path"]) for row in workspaces)
        if type(item["is_remote"]) is not bool:
            raise TypeError
        preset = WorkspacePreset(
            preset_id=item["id"],
            name=item["name"],
            host_key=item["host_key"],
            host_label=item["host_label"],
            is_remote=item["is_remote"],
            workspaces=refs,
            agents=tuple(item["agents"]),
            layout=item["layout"],
        )
        validate_preset(preset)
        return preset
    except (KeyError, TypeError, ValueError) as exc:
        raise PresetStoreError("저장된 작업 항목이 손상되었습니다. 파일을 그대로 보존했습니다.") from exc


def _decode_presets(document: dict) -> list[WorkspacePreset]:
    presets = [_decode_preset(item) for item in document["presets"]]
    ids = [preset.preset_id for preset in presets]
    if len(ids) != len(set(ids)):
        raise PresetStoreError("저장된 작업 ID가 중복되었습니다. 파일을 그대로 보존했습니다.")
    return presets


def _encode_preset(preset: WorkspacePreset) -> dict:
    """Whitelist persisted fields; transient IDs, prompts, and results stay out."""

    return {
        "id": preset.preset_id,
        "name": preset.name,
        "host_key": preset.host_key,
        "host_label": preset.host_label,
        "is_remote": preset.is_remote,
        "workspaces": [{"name": row.name, "path": row.path} for row in preset.workspaces],
        "agents": list(preset.agents),
        "layout": preset.layout,
    }
