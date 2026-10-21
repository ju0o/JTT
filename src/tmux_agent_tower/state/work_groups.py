"""Persistent logical groups of Tower work targets."""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from .worksets import LAYOUTS, layout_for_count, member_slots


class WorkGroupStore:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._cache: Optional[List[dict]] = None
        self._managed_resources: Dict[str, dict] = {}
        self._stamp = None
        self._corrupt = False

    def _load(self) -> List[dict]:
        try:
            stat = self.path.stat()
            stamp = (stat.st_mtime_ns, stat.st_size)
        except FileNotFoundError:
            stamp = None
        if self._cache is not None and stamp == self._stamp:
            return self._cache
        try:
            data = json.loads(self.path.read_text(encoding="utf-8")) if stamp else []
        except (OSError, ValueError):
            self._corrupt = True
            self._cache = []
            self._stamp = stamp
            return self._cache
        groups = data.get("groups", []) if isinstance(data, dict) else []
        resources = data.get("managed_resources", {}) if isinstance(data, dict) else {}
        self._managed_resources = {
            str(target): clean
            for target, value in resources.items()
            if isinstance(resources, dict)
            and (clean := self._clean_resource(value)) is not None
        } if isinstance(resources, dict) else {}
        self._cache = []
        if isinstance(groups, list):
            for value in groups:
                group = self._clean_group(value)
                if group:
                    self._cache.append(group)
        self._stamp = stamp
        self._corrupt = False
        return self._cache

    @staticmethod
    def _clean_group(value) -> Optional[dict]:
        if not isinstance(value, dict):
            return None
        group_id = str(value.get("group_id") or "").strip()
        name = str(value.get("display_name") or "").strip()
        raw_members = value.get("member_target_ids")
        members = list(dict.fromkeys(str(item) for item in raw_members if str(item))) if isinstance(raw_members, list) else []
        if not group_id or not name:
            return None
        order = list(dict.fromkeys(str(item) for item in value.get("member_order", []) if str(item) in members))
        order.extend(item for item in members if item not in order)
        labels = value.get("member_labels") if isinstance(value.get("member_labels"), dict) else {}
        binding = value.get("project_binding") if isinstance(value.get("project_binding"), dict) else None
        layout = value.get("layout")
        if layout not in LAYOUTS:
            layout = layout_for_count(len(members))
        raw_slots = value.get("layout_slots") if isinstance(value.get("layout_slots"), dict) else {}
        slots = {
            str(target): str(slot)
            for target, slot in raw_slots.items()
            if str(target) in members and isinstance(slot, str)
            and (slot in {"main", "side-1", "side-2", "bottom"}
                 or (slot.startswith("side-") and slot[5:].isdigit()))
        }
        defaults = member_slots(len(order))
        for index, target in enumerate(order):
            slots.setdefault(target, defaults[index])
        return {
            "group_id": group_id,
            "display_name": name,
            "project_binding": binding,
            "member_target_ids": members,
            "member_order": order,
            "member_labels": {str(k): str(v) for k, v in labels.items() if str(k) in members},
            "layout": layout,
            "layout_slots": slots,
            "created_at": value.get("created_at") or time.time(),
            "updated_at": value.get("updated_at") or time.time(),
        }

    @staticmethod
    def _clean_resource(value) -> Optional[dict]:
        if not isinstance(value, dict):
            return None
        fields = ("session", "pane_id", "pane_pid", "window_id", "tmux_host", "launch_id")
        result = {key: str(value.get(key) or "") for key in fields}
        if (not result["session"] or not result["pane_id"].startswith("%")
                or not result["pane_id"][1:].isdigit() or not result["pane_pid"].isdigit()
                or not result["window_id"].startswith("@") or not result["window_id"][1:].isdigit()
                or not result["tmux_host"] or not result["launch_id"]):
            return None
        return result

    def _save(self) -> None:
        if self._corrupt:
            raise OSError(f"Work Group state is unreadable: {self.path}")
        tmp = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        try:
            tmp.write_text(json.dumps({
                "groups": self._cache,
                "managed_resources": self._managed_resources,
            }, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, self.path)
        finally:
            if tmp.exists():
                tmp.unlink()
        stat = self.path.stat()
        self._stamp = (stat.st_mtime_ns, stat.st_size)

    def all(self) -> List[dict]:
        return [dict(group) for group in self._load()]

    def membership(self) -> Dict[str, str]:
        return {target: group["group_id"] for group in self._load() for target in group["member_target_ids"]}

    def create(self, display_name: str, target_ids: Iterable[str], *, project_binding=None, labels=None,
               layout: Optional[str] = None, layout_slots=None) -> dict:
        members = list(dict.fromkeys(str(item) for item in target_ids if str(item)))
        if not display_name.strip() or not members:
            raise ValueError("A Work Group needs a name and at least one work item")
        occupied = self.membership()
        if any(item in occupied for item in members):
            raise ValueError("A work item can belong to only one Work Group")
        now = time.time()
        group = {
            "group_id": uuid.uuid4().hex,
            "display_name": display_name.strip(),
            "project_binding": project_binding,
            "member_target_ids": members,
            "member_order": list(members),
            "member_labels": {str(k): str(v) for k, v in (labels or {}).items() if str(k) in members},
            "layout": layout if layout in LAYOUTS else layout_for_count(len(members)),
            "layout_slots": {
                target: str((layout_slots or {}).get(target))
                if isinstance((layout_slots or {}).get(target), str)
                and ((layout_slots or {}).get(target) in {"main", "side-1", "side-2", "bottom"}
                     or ((layout_slots or {}).get(target).startswith("side-")
                         and (layout_slots or {}).get(target)[5:].isdigit()))
                else member_slots(len(members))[index]
                for index, target in enumerate(members)
            },
            "created_at": now,
            "updated_at": now,
        }
        self._load().append(group)
        self._save()
        return dict(group)

    def _group(self, group_id: str) -> dict:
        group = next((item for item in self._load() if item["group_id"] == group_id), None)
        if group is None:
            raise KeyError(group_id)
        return group

    def rename(self, group_id: str, display_name: str) -> None:
        name = display_name.strip()
        if not name:
            raise ValueError("A Work Group name cannot be empty")
        group = self._group(group_id)
        group["display_name"] = name
        group["updated_at"] = time.time()
        self._save()

    def add_members(self, group_id: str, target_ids: Iterable[str], labels=None) -> None:
        group = self._group(group_id)
        occupied = self.membership()
        members = list(dict.fromkeys(str(item) for item in target_ids if str(item)))
        if any(item in occupied and occupied[item] != group_id for item in members):
            raise ValueError("A work item can belong to only one Work Group")
        for item in members:
            if item not in group["member_target_ids"]:
                group["member_target_ids"].append(item)
                group["member_order"].append(item)
        group["member_labels"].update({str(k): str(v) for k, v in (labels or {}).items() if str(k) in group["member_target_ids"]})
        group["updated_at"] = time.time()
        self._save()

    def remove_members(self, group_id: str, target_ids: Iterable[str]) -> None:
        group = self._group(group_id)
        removed = {str(item) for item in target_ids}
        group["member_target_ids"] = [item for item in group["member_target_ids"] if item not in removed]
        group["member_order"] = [item for item in group["member_order"] if item not in removed]
        group["member_labels"] = {key: value for key, value in group["member_labels"].items() if key not in removed}
        group["updated_at"] = time.time()
        self._save()

    def move_member(self, group_id: str, target_id: str, delta: int) -> None:
        group = self._group(group_id)
        order = group["member_order"]
        index = order.index(target_id)
        new_index = max(0, min(len(order) - 1, index + delta))
        order.insert(new_index, order.pop(index))
        group["updated_at"] = time.time()
        self._save()

    def reorder_member(self, group_id: str, target_id: str, destination: str) -> None:
        group = self._group(group_id)
        order = group["member_order"]
        if target_id not in order or destination not in {"up", "down", "top", "bottom"}:
            raise ValueError("Unknown work item or order")
        index = order.index(target_id)
        order.pop(index)
        new_index = {"up": max(0, index - 1), "down": min(len(order), index),
                     "top": 0, "bottom": len(order)}[destination]
        order.insert(new_index, target_id)
        group["updated_at"] = time.time()
        self._save()

    def move_target(self, target_id: str, destination_group_id: Optional[str] = None,
                    *, new_group_name: str = "", label: str = "", project_binding=None) -> None:
        """Change only logical membership. Pane ownership and processes stay put."""
        target_id = str(target_id)
        source_id = self.membership().get(target_id)
        if source_id == destination_group_id and not new_group_name:
            return
        source = self._group(source_id) if source_id else None
        if new_group_name.strip():
            if destination_group_id is not None:
                raise ValueError("Choose a group or a new group")
            now = time.time()
            destination = {
                "group_id": uuid.uuid4().hex,
                "display_name": new_group_name.strip(),
                "project_binding": project_binding,
                "member_target_ids": [], "member_order": [], "member_labels": {},
                "layout": "focus", "layout_slots": {}, "created_at": now, "updated_at": now,
            }
            self._load().append(destination)
        elif destination_group_id:
            destination = self._group(destination_group_id)
            if source_id == destination_group_id:
                return
        else:
            destination = None
        if source:
            source["member_target_ids"].remove(target_id)
            source["member_order"].remove(target_id)
            source["member_labels"].pop(target_id, None)
            source["layout_slots"].pop(target_id, None)
            source["updated_at"] = time.time()
        if destination:
            if target_id in destination["member_target_ids"]:
                raise ValueError("A work item can belong to only one Work Group")
            destination["member_target_ids"].append(target_id)
            destination["member_order"].append(target_id)
            if label:
                destination["member_labels"][target_id] = label
            destination["layout_slots"][target_id] = member_slots(len(destination["member_order"]))[-1]
            destination["updated_at"] = time.time()
        self._save()

    def set_layout(self, group_id: str, layout: str) -> None:
        if layout not in LAYOUTS:
            raise ValueError("Unknown work layout")
        group = self._group(group_id)
        group["layout"] = layout
        group["updated_at"] = time.time()
        self._save()

    def swap_layout_slots(self, group_id: str, first_target: str, second_target: str) -> None:
        group = self._group(group_id)
        slots = group["layout_slots"]
        if first_target not in slots or second_target not in slots:
            raise KeyError("Work item is not in this group")
        slots[first_target], slots[second_target] = slots[second_target], slots[first_target]
        group["updated_at"] = time.time()
        self._save()

    def register_managed_resources(self, resources: Dict[str, dict]) -> None:
        self._load()
        cleaned = {str(target): self._clean_resource(value) for target, value in resources.items()}
        if not resources or any(value is None for value in cleaned.values()):
            raise ValueError("Managed pane identity is incomplete")
        self._managed_resources.update(cleaned)
        self._save()

    def managed_resources(self, target_ids: Iterable[str]) -> Dict[str, dict]:
        self._load()
        return {str(target): dict(self._managed_resources[str(target)])
                for target in target_ids if str(target) in self._managed_resources}

    def update_managed_window(self, target_id: str, window_id: str) -> None:
        self._load()
        if target_id not in self._managed_resources or not str(window_id).startswith("@"):
            raise KeyError(target_id)
        self._managed_resources[target_id]["window_id"] = str(window_id)
        self._save()

    def update_labels(self, labels: Dict[str, str]) -> None:
        changed = False
        for group in self._load():
            group_changed = False
            for target in group["member_target_ids"]:
                label = labels.get(target)
                if label and group["member_labels"].get(target) != label:
                    group["member_labels"][target] = label
                    changed = True
                    group_changed = True
            if group_changed:
                group["updated_at"] = time.time()
        if changed:
            self._save()

    def dissolve(self, group_id: str) -> None:
        groups = self._load()
        self._cache = [group for group in groups if group["group_id"] != group_id]
        if len(self._cache) == len(groups):
            raise KeyError(group_id)
        self._save()


def aggregate(members: Iterable[dict]) -> Dict[str, int]:
    counts = {"error": 0, "attention": 0, "working": 0, "result": 0, "idle": 0, "unavailable": 0}
    for row in members:
        if row.get("stale") or row.get("offline"):
            counts["unavailable"] += 1
        elif row.get("attention") == "error":
            counts["error"] += 1
        elif row.get("attention") in {"approval_required", "input_required"} or row.get("status") == "WAITING":
            counts["attention"] += 1
        elif row.get("status") == "WORKING" and not row.get("agent_process_ended"):
            counts["working"] += 1
        elif row.get("result_state") == "ready":
            counts["result"] += 1
        elif row.get("agent_process_ended"):
            counts["unavailable"] += 1
        elif row.get("status") == "IDLE":
            counts["idle"] += 1
        elif row.get("status") in {"DEAD", "UNKNOWN"}:
            counts["unavailable"] += 1
        else:
            counts["unavailable"] += 1
    return counts
