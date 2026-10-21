"""Saved Work and project-neutral work templates."""

from __future__ import annotations

import json
import math
import os
import re
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from .overrides import ROLE_IDS
from ..paths import CONFIG_DIR

WORKSET_FILE = CONFIG_DIR / "worksets.json"
VERSION = 1
MAX_BYTES = 1024 * 1024
LAYOUTS = ("focus", "split-2", "grid-4", "main-plus-side")
SLOTS = {"main", "side-1", "side-2", "bottom"}
_ID = re.compile(r"[0-9a-f]{32}\Z")


class WorksetError(ValueError):
    """Invalid workset data; the on-disk document is left untouched."""


@dataclass(frozen=True)
class WorkMember:
    member_id: str
    role: str
    agent: str
    display_name: str = ""
    name_origin: str = "auto"
    project_name: str = ""
    project_path: str = ""
    execution_target: str = "auto"
    layout_slot: str = ""


@dataclass(frozen=True)
class Workset:
    workset_id: str
    kind: str
    name: str
    members: tuple[WorkMember, ...]
    member_order: tuple[str, ...]
    layout: str
    created_at: float = 0.0
    updated_at: float = 0.0


def member_slots(count: int) -> tuple[str, ...]:
    if count <= 1:
        return ("main",)[:count]
    base = ("main", "side-1", "side-2", "bottom")
    return tuple(base[index] if index < len(base) else f"side-{index}" for index in range(count))


def layout_for_count(count: int) -> str:
    return "focus" if count <= 1 else "split-2" if count == 2 else "grid-4" if count <= 4 else "main-plus-side"


def new_workset(*, kind: str, name: str, members, layout: str | None = None) -> Workset:
    now = time.time()
    rows = tuple(
        member if isinstance(member, WorkMember) else WorkMember(**member)
        for member in members
    )
    slots = member_slots(len(rows))
    rows = tuple(
        WorkMember(
            member_id=row.member_id or uuid.uuid4().hex,
            role=row.role,
            agent=row.agent,
            display_name=row.display_name if kind == "saved_work" else "",
            name_origin=row.name_origin if kind == "saved_work" else "auto",
            project_name=row.project_name,
            project_path=row.project_path,
            execution_target=row.execution_target,
            layout_slot=row.layout_slot if row.layout_slot else slots[index],
        )
        for index, row in enumerate(rows)
    )
    value = Workset(
        uuid.uuid4().hex, kind, name.strip(), rows,
        tuple(row.member_id for row in rows), layout or layout_for_count(len(rows)), now, now,
    )
    _validate(value)
    return value


class WorksetStore:
    """One versioned file; template records cannot contain project paths or pane IDs."""

    def __init__(self, path: Path | None = None):
        self.path = Path(path if path is not None else WORKSET_FILE)

    def list(self, kind: str | None = None) -> list[Workset]:
        rows = self._read()["worksets"]
        decoded = [_decode(row) for row in rows]
        return [row for row in decoded if kind is None or row.kind == kind]

    def save(self, workset: Workset) -> None:
        _validate(workset)
        document = self._read()
        rows = [_decode(row) for row in document["worksets"]]
        replaced = False
        for index, existing in enumerate(rows):
            if existing.workset_id == workset.workset_id:
                rows[index] = workset
                replaced = True
                break
        if not replaced:
            rows.append(workset)
        document["worksets"] = [_encode(row) for row in rows]
        self._atomic_write(document)

    def delete(self, workset_id: str) -> None:
        document = self._read()
        rows = [_decode(row) for row in document["worksets"]]
        remaining = [row for row in rows if row.workset_id != workset_id]
        if len(remaining) == len(rows):
            raise WorksetError("저장 항목을 찾을 수 없습니다.")
        document["worksets"] = [_encode(row) for row in remaining]
        self._atomic_write(document)

    def _read(self) -> dict:
        try:
            if self.path.is_symlink():
                raise WorksetError("저장 파일이 안전하지 않습니다. 파일을 그대로 보존했습니다.")
            with self.path.open("rb") as stream:
                raw = stream.read(MAX_BYTES + 1)
        except FileNotFoundError:
            return {"version": VERSION, "worksets": []}
        except WorksetError:
            raise
        except OSError as exc:
            raise WorksetError("저장 파일을 읽지 못했습니다. 파일을 그대로 보존했습니다.") from exc
        if len(raw) > MAX_BYTES:
            raise WorksetError("저장 파일이 너무 큽니다. 파일을 그대로 보존했습니다.")
        try:
            document = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_pairs)
        except (UnicodeError, ValueError, RecursionError) as exc:
            raise WorksetError("저장 파일이 손상되었습니다. 파일을 그대로 보존했습니다.") from exc
        if type(document) is not dict or set(document) != {"version", "worksets"}:
            raise WorksetError("저장 파일 형식이 올바르지 않습니다. 파일을 그대로 보존했습니다.")
        if type(document["version"]) is not int or document["version"] != VERSION:
            raise WorksetError("지원하지 않는 저장 파일 버전입니다. 파일을 그대로 보존했습니다.")
        if type(document["worksets"]) is not list:
            raise WorksetError("저장 목록이 손상되었습니다. 파일을 그대로 보존했습니다.")
        decoded = [_decode(row) for row in document["worksets"]]
        ids = [row.workset_id for row in decoded]
        if len(ids) != len(set(ids)):
            raise WorksetError("저장 항목 ID가 중복되었습니다. 파일을 그대로 보존했습니다.")
        return document

    def _atomic_write(self, document: dict) -> None:
        temp = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, temp_name = tempfile.mkstemp(prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent)
            temp = Path(temp_name)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                os.fchmod(stream.fileno(), 0o600)
                json.dump(document, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp, self.path)
            temp = None
            directory = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except OSError as exc:
            raise WorksetError("저장 항목을 안전하게 기록하지 못했습니다.") from exc
        finally:
            if temp is not None:
                try:
                    temp.unlink()
                except OSError:
                    pass


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _validate(workset: Workset) -> None:
    if not isinstance(workset, Workset) or not isinstance(workset.members, tuple) or not isinstance(workset.member_order, tuple):
        raise WorksetError("저장 항목 형식이 올바르지 않습니다.")
    if not isinstance(workset.kind, str) or workset.kind not in {"saved_work", "template"}:
        raise WorksetError("저장 항목 종류가 올바르지 않습니다.")
    if not isinstance(workset.workset_id, str) or not _ID.fullmatch(workset.workset_id):
        raise WorksetError("저장 항목 ID가 올바르지 않습니다.")
    if not isinstance(workset.name, str) or not workset.name or len(workset.name) > 80 or any(ord(ch) < 32 for ch in workset.name):
        raise WorksetError("이름은 1~80자로 입력하세요.")
    if not workset.members or len(workset.members) > 32 or not isinstance(workset.layout, str) or workset.layout not in LAYOUTS:
        raise WorksetError("구성과 배치를 확인하세요.")
    if any(not isinstance(member, WorkMember) for member in workset.members) or any(
        not isinstance(member_id, str) for member_id in workset.member_order
    ):
        raise WorksetError("구성원 순서가 올바르지 않습니다.")
    member_ids = [member.member_id for member in workset.members]
    if (
        len(member_ids) != len(set(member_ids))
        or len(workset.member_order) != len(set(workset.member_order))
        or set(member_ids) != set(workset.member_order)
    ):
        raise WorksetError("작업 순서가 올바르지 않습니다.")
    for member in workset.members:
        if not isinstance(member.role, str):
            raise WorksetError("구성원 역할이 올바르지 않습니다.")
        if not isinstance(member.member_id, str) or not _ID.fullmatch(member.member_id):
            raise WorksetError("구성원 ID가 올바르지 않습니다.")
        if member.role and member.role not in ROLE_IDS:
            raise WorksetError("지원하지 않는 역할입니다.")
        from ..launcher.config import AGENT_LABEL_TO_CONFIG_KEY
        if not isinstance(member.agent, str) or member.agent not in AGENT_LABEL_TO_CONFIG_KEY:
            raise WorksetError("Agent 이름이 올바르지 않습니다.")
        if not isinstance(member.name_origin, str) or member.name_origin not in {"auto", "user"}:
            raise WorksetError("작업 이름 출처가 올바르지 않습니다.")
        values = (member.agent, member.display_name, member.project_name, member.project_path, member.execution_target, member.layout_slot)
        if any(not isinstance(value, str) for value in values):
            raise WorksetError("구성원 정보가 올바르지 않습니다.")
        for value in values:
            if len(value) > 2048 or any(ord(ch) < 32 for ch in value):
                raise WorksetError("구성원 정보가 올바르지 않습니다.")
        if member.layout_slot not in SLOTS and not (member.layout_slot.startswith("side-") and member.layout_slot[5:].isdigit()):
            raise WorksetError("레이아웃 위치가 올바르지 않습니다.")
        if workset.kind == "template" and (member.project_name or member.project_path or member.execution_target != "auto"):
            raise WorksetError("작업 템플릿에는 프로젝트 경로나 개인 실행 위치를 저장할 수 없습니다.")
        if workset.kind == "saved_work" and not member.project_path:
            raise WorksetError("저장된 작업의 프로젝트 경로가 필요합니다.")
    if any(type(value) not in (int, float) or not math.isfinite(value) for value in (workset.created_at, workset.updated_at)):
        raise WorksetError("저장 시각이 올바르지 않습니다.")


def _decode(row) -> Workset:
    try:
        if type(row) is not dict or set(row) != {
            "id", "kind", "name", "members", "member_order", "layout", "created_at", "updated_at"
        }:
            raise TypeError
        members = []
        for value in row["members"]:
            if type(value) is not dict or set(value) != {
                "id", "role", "agent", "display_name", "name_origin", "project_name", "project_path", "execution_target", "layout_slot"
            }:
                raise TypeError
            members.append(WorkMember(
                value["id"], value["role"], value["agent"], value["display_name"],
                value["name_origin"], value["project_name"], value["project_path"], value["execution_target"], value["layout_slot"],
            ))
        if not isinstance(row["member_order"], list) or any(not isinstance(x, str) for x in row["member_order"]):
            raise TypeError
        workset = Workset(
            row["id"], row["kind"], row["name"], tuple(members), tuple(row["member_order"]),
            row["layout"], row["created_at"], row["updated_at"],
        )
        _validate(workset)
        return workset
    except (KeyError, TypeError, ValueError) as exc:
        raise WorksetError("저장 항목이 손상되었습니다. 파일을 그대로 보존했습니다.") from exc


def _encode(workset: Workset) -> dict:
    return {
        "id": workset.workset_id,
        "kind": workset.kind,
        "name": workset.name,
        "members": [{
            "id": row.member_id,
            "role": row.role,
            "agent": row.agent,
            "display_name": row.display_name if workset.kind == "saved_work" else "",
            "name_origin": row.name_origin if workset.kind == "saved_work" else "auto",
            "project_name": row.project_name if workset.kind == "saved_work" else "",
            "project_path": row.project_path if workset.kind == "saved_work" else "",
            "execution_target": row.execution_target if workset.kind == "saved_work" else "auto",
            "layout_slot": row.layout_slot,
        } for row in workset.members],
        "member_order": list(workset.member_order),
        "layout": workset.layout,
        "created_at": workset.created_at,
        "updated_at": workset.updated_at,
    }


def phone_summary(worksets: list[Workset]) -> list[dict]:
    """Phone-safe list metadata; project paths and execution hostnames stay local."""

    from ..i18n import t

    return [{
        "id": row.workset_id,
        "kind": row.kind,
        "name": row.name,
        "project": next((member.project_name for member in row.members if member.project_name), ""),
        "members": [{
            "id": member.member_id,
            "role": member.role,
            "role_label": t(f"role.{member.role}") if member.role in ROLE_IDS else "",
            "display_name": member.display_name,
            "agent": member.agent,
        } for member in row.members],
        "member_count": len(row.members),
        "layout": row.layout,
    } for row in worksets]
