"""Versioned saved views for the read-only Control Room.

This store contains only layout and pane identity metadata. It never stores
terminal output and never launches or moves tmux/Agent resources.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import tempfile
import time
import uuid
from pathlib import Path

from .worksets import LAYOUTS

SCHEMA_VERSION = 1
MAX_BYTES = 1024 * 1024
_UNLOADED = object()
_IDENTITY_FIELDS = (
    "target_id", "tmux_host", "session", "session_id", "window_id", "pane_id",
    "pane_pid", "transport", "transport_target", "remote",
)
_REQUIRED_IDENTITY = {"target_id", "session", "session_id", "window_id", "pane_id", "pane_pid"}


class ControlScreenError(ValueError):
    """Invalid, corrupt, or concurrently changed saved-screen state."""


def pane_reference(row: dict) -> dict:
    """Return the minimal persisted identity; deliberately excludes live_lines."""

    if not isinstance(row, dict):
        raise ControlScreenError("invalid Pane reference")
    identity = {field: row.get(field) for field in _IDENTITY_FIELDS}
    if any(not isinstance(identity[field], str) for field in _IDENTITY_FIELDS[:-1]) or any(
        not identity[field].strip() for field in _REQUIRED_IDENTITY
    ):
        raise ControlScreenError("incomplete Pane identity")
    if not isinstance(identity["remote"], bool):
        raise ControlScreenError("invalid Pane transport")
    window_ref = row.get("window_ref")
    if window_ref is None:
        window_ref = ""
    if not isinstance(window_ref, str):
        raise ControlScreenError("invalid Window reference")
    return {"identity": identity, "window_ref": window_ref, "display_name": ""}


def _clean_reference(value) -> dict:
    if not isinstance(value, dict) or set(value) != {"identity", "window_ref", "display_name"}:
        raise ControlScreenError("invalid Pane reference")
    identity = value.get("identity")
    if not isinstance(identity, dict) or set(identity) != set(_IDENTITY_FIELDS):
        raise ControlScreenError("invalid Pane identity")
    if any(not isinstance(identity[field], str) for field in _IDENTITY_FIELDS[:-1]) or any(
        not identity[field].strip() for field in _REQUIRED_IDENTITY
    ):
        raise ControlScreenError("incomplete Pane identity")
    if not isinstance(identity["remote"], bool):
        raise ControlScreenError("invalid Pane transport")
    window_ref = value.get("window_ref")
    display_name = value.get("display_name")
    if (not isinstance(window_ref, str) or len(window_ref) > 1024
            or not isinstance(display_name, str) or len(display_name) > 256):
        raise ControlScreenError("invalid Pane reference")
    return {"identity": dict(identity), "window_ref": window_ref, "display_name": display_name}


def _clean_view(value, *, with_name: bool) -> dict:
    expected = {"context", "panes", "selected", "layout", "updated_at"}
    if with_name:
        expected |= {"screen_id", "name", "created_at"}
    if not isinstance(value, dict) or set(value) != expected:
        raise ControlScreenError("invalid saved view fields")
    layout = value.get("layout")
    if layout not in LAYOUTS:
        raise ControlScreenError("unsupported view layout")
    context = value.get("context")
    if not isinstance(context, dict) or set(context) != {"project_id", "window_ref"}:
        raise ControlScreenError("invalid view context")
    cleaned_context = {}
    for field in ("project_id", "window_ref"):
        item = context.get(field)
        if item is not None and (not isinstance(item, str) or len(item) > 1024):
            raise ControlScreenError("invalid view context")
        cleaned_context[field] = item
    raw_panes = value.get("panes")
    if not isinstance(raw_panes, list) or not 1 <= len(raw_panes) <= 4:
        raise ControlScreenError("a view must contain one to four Panes")
    panes = [_clean_reference(item) for item in raw_panes]
    signatures = [json.dumps(item["identity"], sort_keys=True) + item["window_ref"] for item in panes]
    if len(signatures) != len(set(signatures)):
        raise ControlScreenError("duplicate Pane reference")
    selected = value.get("selected")
    if selected is not None:
        selected = _clean_reference(selected)
        if not any(item == selected for item in panes):
            raise ControlScreenError("selected Pane is not in the view")
    result = {
        "context": cleaned_context,
        "panes": panes,
        "selected": selected,
        "layout": layout,
        "updated_at": _timestamp(value.get("updated_at")),
    }
    if with_name:
        screen_id, name = value.get("screen_id"), value.get("name")
        if (not isinstance(screen_id, str) or len(screen_id) != 32
                or any(char not in "0123456789abcdef" for char in screen_id)
                or not isinstance(name, str) or not name.strip() or len(name) > 120):
            raise ControlScreenError("invalid saved screen identity")
        result.update({
            "screen_id": screen_id, "name": name.strip(),
            "created_at": _timestamp(value.get("created_at")),
        })
    return result


def _timestamp(value) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ControlScreenError("invalid saved view timestamp")
    return float(value)


class ControlScreenStore:
    """Atomic, optimistic-concurrency-protected saved screen metadata."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._digest = _UNLOADED
        self._document = {"schema_version": SCHEMA_VERSION, "screens": [], "templates": [], "last_view": None}
        self._corrupt = False
        self._external_change = False

    def _load(self) -> None:
        if self.path.is_symlink():
            self._corrupt = True
            return
        try:
            raw = self.path.read_bytes()
            if len(raw) > MAX_BYTES:
                raise ControlScreenError("saved screens exceed size limit")
            digest = hashlib.sha256(raw).hexdigest()
        except FileNotFoundError:
            raw, digest = None, None
        except (OSError, ControlScreenError):
            self._corrupt = True
            return
        if digest == self._digest:
            return
        if self._digest is not _UNLOADED:
            self._external_change = True
        try:
            document = json.loads(raw.decode("utf-8")) if raw is not None else {
                "schema_version": SCHEMA_VERSION, "screens": [], "templates": [], "last_view": None,
            }
            self._document = self._clean_document(document)
            self._corrupt = False
        except (UnicodeError, ValueError, TypeError, json.JSONDecodeError):
            self._corrupt = True
        self._digest = digest

    @staticmethod
    def _clean_document(value) -> dict:
        if not isinstance(value, dict) or set(value) != {"schema_version", "screens", "templates", "last_view"}:
            raise ControlScreenError("unsupported saved screen document")
        if value.get("schema_version") != SCHEMA_VERSION:
            raise ControlScreenError("unsupported saved screen version")
        raw_screens, raw_templates = value.get("screens"), value.get("templates")
        if not isinstance(raw_screens, list) or not isinstance(raw_templates, list):
            raise ControlScreenError("invalid saved screen lists")
        screens = [_clean_view(item, with_name=True) for item in raw_screens]
        if len({item["screen_id"] for item in screens}) != len(screens):
            raise ControlScreenError("duplicate saved screen ID")
        names = [item["name"].casefold() for item in screens]
        if len(set(names)) != len(names):
            raise ControlScreenError("duplicate saved screen name")
        templates = []
        template_ids, template_names = set(), set()
        for item in raw_templates:
            if (not isinstance(item, dict) or set(item) != {"template_id", "name", "layout", "created_at", "updated_at"}
                    or item.get("layout") not in LAYOUTS):
                raise ControlScreenError("invalid layout template")
            template_id, name = item.get("template_id"), item.get("name")
            if (not isinstance(template_id, str) or len(template_id) != 32
                    or any(char not in "0123456789abcdef" for char in template_id)
                    or not isinstance(name, str) or not name.strip() or len(name) > 120
                    or template_id in template_ids or name.casefold() in template_names):
                raise ControlScreenError("invalid layout template identity")
            template_ids.add(template_id)
            template_names.add(name.casefold())
            templates.append({
                "template_id": template_id, "name": name.strip(), "layout": item["layout"],
                "created_at": _timestamp(item["created_at"]), "updated_at": _timestamp(item["updated_at"]),
            })
        last_view = value.get("last_view")
        if last_view is not None:
            last_view = _clean_view(last_view, with_name=False)
        return {"schema_version": SCHEMA_VERSION, "screens": screens, "templates": templates, "last_view": last_view}

    @property
    def corrupt(self) -> bool:
        self._load()
        return self._corrupt

    def screens(self) -> list[dict]:
        self._load()
        return copy.deepcopy(self._document["screens"])

    def templates(self) -> list[dict]:
        self._load()
        return copy.deepcopy(self._document["templates"])

    def get_screen(self, screen_id: str) -> dict | None:
        return next((item for item in self.screens() if item["screen_id"] == screen_id), None)

    def last_view(self) -> dict | None:
        self._load()
        return copy.deepcopy(self._document["last_view"])

    def _write(self, document: dict) -> None:
        self._load()
        if self._corrupt:
            raise ControlScreenError("saved screen state is unreadable")
        if self._external_change:
            raise ControlScreenError("saved screen state changed outside Tower")
        cleaned = self._clean_document(document)
        try:
            current = self.path.read_bytes()
            current_digest = hashlib.sha256(current).hexdigest()
        except FileNotFoundError:
            current_digest = None
        except OSError as exc:
            raise ControlScreenError("saved screen state cannot be checked") from exc
        if current_digest != self._digest:
            self._external_change = True
            raise ControlScreenError("saved screen state changed outside Tower")
        payload = json.dumps(cleaned, ensure_ascii=False, indent=2).encode("utf-8") + b"\n"
        if len(payload) > MAX_BYTES:
            raise ControlScreenError("saved screens exceed size limit")
        fd, tmp_name = tempfile.mkstemp(prefix=f".{self.path.name}.", suffix=".tmp", dir=str(self.path.parent))
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "wb") as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                current = self.path.read_bytes()
                current_digest = hashlib.sha256(current).hexdigest()
            except FileNotFoundError:
                current_digest = None
            if current_digest != self._digest:
                self._external_change = True
                raise ControlScreenError("saved screen state changed outside Tower")
            os.replace(tmp, self.path)
            try:
                directory_fd = os.open(self.path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except OSError:
                pass
        finally:
            if tmp.exists():
                tmp.unlink()
        self._document = cleaned
        self._digest = hashlib.sha256(payload).hexdigest()
        self._corrupt = self._external_change = False

    def save_screen(self, name: str, rows, selected: dict | None, layout: str,
                    context: dict | None = None, *, screen_id: str | None = None) -> dict:
        self._load()
        name = str(name or "").strip()
        if not name or len(name) > 120:
            raise ControlScreenError("screen name must contain 1 to 120 characters")
        if layout not in LAYOUTS:
            raise ControlScreenError("unsupported view layout")
        panes = [pane_reference(row) for row in list(rows or ())[:4]]
        if not panes:
            raise ControlScreenError("there are no Pane references to save")
        selected_ref = pane_reference(selected) if selected else None
        now = time.time()
        screens = copy.deepcopy(self._document["screens"])
        existing = next((item for item in screens if item["screen_id"] == screen_id), None) if screen_id else None
        if existing is None and any(item["name"].casefold() == name.casefold() for item in screens):
            raise ControlScreenError("a saved screen already uses this name")
        screen = {
            "screen_id": screen_id or uuid.uuid4().hex, "name": name,
            "context": {"project_id": (context or {}).get("project_id"),
                        "window_ref": (context or {}).get("window_ref")},
            "panes": panes, "selected": selected_ref, "layout": layout,
            "created_at": existing["created_at"] if existing else now, "updated_at": now,
        }
        if existing:
            screens[screens.index(existing)] = screen
        else:
            screens.append(screen)
        document = copy.deepcopy(self._document)
        document["screens"] = screens
        self._write(document)
        return copy.deepcopy(screen)

    def rename_screen(self, screen_id: str, name: str) -> None:
        self._load()
        name = str(name or "").strip()
        if not name or len(name) > 120:
            raise ControlScreenError("screen name must contain 1 to 120 characters")
        if any(item["screen_id"] != screen_id and item["name"].casefold() == name.casefold()
               for item in self._document["screens"]):
            raise ControlScreenError("a saved screen already uses this name")
        document = copy.deepcopy(self._document)
        item = next((value for value in document["screens"] if value["screen_id"] == screen_id), None)
        if item is None:
            raise ControlScreenError("saved screen not found")
        item["name"], item["updated_at"] = name, time.time()
        self._write(document)

    def delete_screen(self, screen_id: str) -> None:
        self._load()
        document = copy.deepcopy(self._document)
        remaining = [item for item in document["screens"] if item["screen_id"] != screen_id]
        if len(remaining) == len(document["screens"]):
            raise ControlScreenError("saved screen not found")
        document["screens"] = remaining
        self._write(document)

    def save_template(self, name: str, layout: str, *, template_id: str | None = None) -> dict:
        self._load()
        name = str(name or "").strip()
        if not name or len(name) > 120 or layout not in LAYOUTS:
            raise ControlScreenError("invalid layout template")
        templates = copy.deepcopy(self._document["templates"])
        existing = next((item for item in templates if item["template_id"] == template_id), None) if template_id else None
        if existing is None and any(item["name"].casefold() == name.casefold() for item in templates):
            raise ControlScreenError("a layout template already uses this name")
        now = time.time()
        value = {
            "template_id": template_id or uuid.uuid4().hex, "name": name, "layout": layout,
            "created_at": existing["created_at"] if existing else now, "updated_at": now,
        }
        if existing:
            templates[templates.index(existing)] = value
        else:
            templates.append(value)
        document = copy.deepcopy(self._document)
        document["templates"] = templates
        self._write(document)
        return copy.deepcopy(value)

    def delete_template(self, template_id: str) -> None:
        self._load()
        document = copy.deepcopy(self._document)
        remaining = [item for item in document["templates"] if item["template_id"] != template_id]
        if len(remaining) == len(document["templates"]):
            raise ControlScreenError("layout template not found")
        document["templates"] = remaining
        self._write(document)

    def save_last_view(self, rows, selected: dict | None, layout: str, context: dict | None = None) -> None:
        self._load()
        panes = [pane_reference(row) for row in list(rows or ())[:4]]
        if not panes or layout not in LAYOUTS:
            return
        selected_ref = pane_reference(selected) if selected else None
        document = copy.deepcopy(self._document)
        document["last_view"] = _clean_view({
            "context": {"project_id": (context or {}).get("project_id"),
                        "window_ref": (context or {}).get("window_ref")},
            "panes": panes, "selected": selected_ref, "layout": layout, "updated_at": time.time(),
        }, with_name=False)
        self._write(document)
