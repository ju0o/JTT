"""Tower Remote's HTTP surface: a tiny, stdlib-only read API plus one
narrow, explicit write action.

Deliberately reuses the exact same modules the curses TUI uses for
discovery/status/activity/identity (a headless ``ui.tower.Tower``
instance, never re-implemented here) -- see docs/ARCHITECTURE.md. This
module only adds: JSON serialization of that data, pairing/token auth,
and the one allowed write action (typing an explicit, user-composed
prompt into one explicitly selected, still-live, non-dead pane).

Hard boundaries enforced here, not just documented:

* No endpoint accepts a raw shell command, a file path, or a pane_id with
  no corresponding live row -- ``/api/prompt`` re-resolves its target
  against a *fresh* pane snapshot on every call and rejects anything that
  doesn't match by key AND by the project/agent the client believes it's
  targeting (stale/wrong-pane rejection).
* ``/api/status`` never includes captured terminal content -- only the
  same small set of fields the TUI's row already carries (see
  ``build_status_payload``). Raw pane text exists in exactly one place,
  ``GET /api/panes/<key>/screen`` (the live view): paired token required,
  capture-pane only, hard line/byte caps, ``Cache-Control: no-store``,
  never written to disk or a log.
* Every request other than ``/api/health`` and ``/api/pair`` requires a
  valid bearer token (see ``auth.py``); a same-LAN attacker who hasn't
  seen the pairing code shown on the PC's own terminal gets nothing.
"""

from __future__ import annotations

import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, List, Optional, Tuple
from urllib.parse import unquote

from .. import __version__
from ..paths import CONFIG_DIR
from ..control.actions import (
    MAX_IDENTITY_CHARS,
    MAX_PROMPT_CHARS,
    MAX_SCREEN_BYTES,
    MAX_SCREEN_LINE_CHARS,
    MAX_SCREEN_LINES,
    apply_identity_edit,
    build_screen_payload,
    find_prompt_target,
    find_row,
    find_screen_target,
    focus_pane,
    get_pane_screen,
    get_result,
    respond_attention,
    respond_interaction,
    respond_interaction_text,
    send_prompt,
    send_prompt_to_pane,
)
from ..detection.result import ResultTracker, pane_result_identity, shared_result_state_path
from ..detection.status import StatusEngine
from ..i18n import t
from ..state.overrides import ROLE_IDS
from ..state.work_groups import aggregate
from ..state.worksets import LAYOUTS, WorksetError, WorksetStore, phone_summary
from ..tmux import capture as tmux_capture
from ..ui.tower import Tower
from . import auth
from .webui import PAGE_HTML

MAX_BODY_BYTES = 8 * 1024
# A 32,000-character Korean prompt is about 96KB of UTF-8, plus JSON
# quoting. Other endpoints stay on the small cap.
PROMPT_BODY_BYTES = MAX_PROMPT_CHARS * 4 + 64 * 1024


def build_status_payload(tower: Tower) -> dict:
    """Minimal, JSON-safe snapshot of what the TUI would show right now.

    Never includes ``path`` (local filesystem layout) or any captured
    terminal content -- only display-level fields already meant to be
    shown to the user.
    """

    tower.load()
    panes: List[dict] = []
    rows_by_target = {str(row.get("target_id")): row for row in tower.rows if row.get("target_id")}

    for row in tower.rows:
        if row.get("placeholder"):
            panes.append(
                {
                    "key": row["key"],
                    "target_id": row.get("target_id"),
                    "host": row["host"],
                    "offline": True,
                    "status": row.get("status"),
                    "remote": True,
                }
            )
            continue

        panes.append(
            {
                "key": row["key"],
                "target_id": row.get("target_id"),
                "work_group_id": row.get("work_group_id"),
                "work_group_name": row.get("work_group_name"),
                "host": row["host"],
                "observer_host": row.get("observer_host") or "",
                "tmux_host": row.get("tmux_host") or row.get("host"),
                "execution_host": row.get("execution_host") or row.get("host"),
                "transport": row.get("transport") or "",
                "transport_target": row.get("transport_target") or "",
                "place_label": row.get("place_label") or "",
                "topology_source": row.get("topology_source") or "",
                "project": row.get("project"),
                "display_name": row.get("display_name") or row.get("task_name"),
                "task_name": row.get("task_name"),
                "name_origin": row.get("name_origin") or "auto",
                "suggested_name": row.get("suggested_name") or row.get("display_name") or row.get("task_name"),
                "role": row.get("role"),
                "role_label": t(f'role.{row["role"]}') if row.get("role") in ROLE_IDS else "",
                "agent": row.get("agent"),
                "auto_project": row.get("auto_project"),
                "auto_agent": row.get("auto_agent"),
                "project_source": row.get("project_source"),
                "agent_source": row.get("agent_source"),
                "auto_project_source": row.get("auto_project_source"),
                "auto_agent_source": row.get("auto_agent_source"),
                "pane_title": row.get("pane_title"),
                "status": row.get("status"),
                "attention": row.get("attention") or "none",
                "attention_prompt": row.get("attention_prompt") or "",
                "interaction": row.get("interaction"),
                "approval_known": bool(row.get("approval_known")),
                "reject_known": bool(row.get("reject_known")),
                "result_state": row.get("result_state") or "none",
                "duration_seconds": round(row.get("duration_seconds") or 0.0, 1),
                "activity": row.get("activity_text"),
                "visit": row.get("visit"),
                "remote": bool(row.get("remote")),
                "offline": False,
                "session": row.get("session"),
                "window_id": row.get("window_id"),
                "window_index": row.get("window_index"),
                "window_name": row.get("window_name"),
                "pane_id": row.get("pane_id"),
                "pane_index": row.get("pane_index"),
                "active": bool(row.get("pane_active")),
            }
        )

    groups = []
    store = getattr(tower, "work_groups", None)
    if store is not None:
        for group in store.all():
            members = [rows_by_target.get(target) or {"target_id": target, "stale": True} for target in group["member_order"]]
            binding = group.get("project_binding") or {}
            groups.append({
                "group_id": group["group_id"],
                "display_name": group["display_name"],
                "project": binding.get("name") or "",
                "member_target_ids": list(group["member_order"]),
                "member_labels": dict(group.get("member_labels") or {}),
                "layout": group.get("layout") or "focus",
                "layout_slots": dict(group.get("layout_slots") or {}),
                "summary_counts": aggregate(members),
            })

    folder_store = getattr(tower, "folders", None)
    workspace = folder_store.workspace(
        getattr(tower, "window_assets", []),
        [row for row in tower.rows if row.get("pane_id") and not row.get("tower_runtime")],
    ) if folder_store is not None else {
        "schema_version": 1, "folders": [], "windows": [], "unfiled_windows": [], "tasks": [],
    }

    try:
        phone_worksets = phone_summary(WorksetStore().list())
        saved_work = [row for row in phone_worksets if row["kind"] == "saved_work"]
        work_templates = [row for row in phone_worksets if row["kind"] == "template"]
        worksets_error = ""
    except WorksetError:
        saved_work, work_templates, worksets_error = [], [], "unavailable"

    return {
        "ok": True,
        "panes": panes,
        "groups": groups,
        "workspace": workspace,
        "saved_work": saved_work,
        "work_templates": work_templates,
        "worksets_error": worksets_error,
        "execution_targets": [
            {"id": "auto", "name": "자동"},
            {"id": getattr(tower, "local_host", ""), "name": "이 컴퓨터", "transport": "local"},
            *[{
                "id": str(row.get("alias") or ""),
                "name": str(row.get("display_name") or row.get("name") or row.get("alias") or ""),
                "transport": "ssh",
            } for row in getattr(tower, "remote_hosts", []) if row.get("alias")],
        ],
        "session": tower.session,
        "generated_at": time.time(),
        "version": __version__,
    }


def source_session_missing_payload(session: str) -> dict:
    """What the phone gets instead of a pane list once the tmux session
    this remote was bound to no longer exists. Deliberately no ``panes``
    key: configured SSH hosts must not show up alone and look like a
    healthy Tower with "no local panes".
    """

    return {"ok": False, "error": "source_session_missing", "session": session}


class TowerRemoteHandler(BaseHTTPRequestHandler):
    server_version = "TowerRemote/1"

    # -- helpers ------------------------------------------------------

    def _tower(self) -> Tower:
        return self.server.make_tower()  # type: ignore[attr-defined]

    def _send_json(self, status: int, payload: dict, no_store: bool = False) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        if no_store:
            self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    @staticmethod
    def _pane_route(path: str) -> Optional[Tuple[str, str]]:
        """``/api/panes/<key>/<action>`` -> ``(key, action)``. The key is
        URL-encoded by the client (tmux ids contain ``%``)."""

        parts = path.split("?", 1)[0].split("/")
        if len(parts) != 5 or parts[1:3] != ["api", "panes"] or not parts[3]:
            return None
        return unquote(parts[3]), parts[4]

    def _send_html(self, status: int, html: str) -> None:
        body = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _host_header_allowed(self) -> bool:
        """Defense-in-depth against DNS rebinding: the ``Host`` header
        must name one of the addresses/ports this server is actually
        bound to and printed to the user, not an arbitrary attacker
        domain that happens to resolve to this LAN IP.
        """

        allowed: List[str] = self.server.allowed_hosts  # type: ignore[attr-defined]
        host = (self.headers.get("Host") or "").strip()
        if not host:
            return False
        host_no_port = host.split(":", 1)[0]
        return host_no_port in allowed

    def _read_json_body(self, limit: int = MAX_BODY_BYTES) -> Optional[dict]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > limit:
            return None
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception:
            return None
        return data if isinstance(data, dict) else None

    def _bearer_token(self) -> Optional[str]:
        header = self.headers.get("Authorization") or ""
        if header.startswith("Bearer "):
            return header[len("Bearer "):].strip()
        return None

    def _require_auth(self) -> bool:
        token_store: auth.TokenStore = self.server.token_store  # type: ignore[attr-defined]
        if token_store.is_valid(self._bearer_token()):
            return True
        self._send_json(401, {"ok": False, "error": "unauthorized"})
        return False

    # -- routing --------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 (stdlib-mandated name)
        if not self._host_header_allowed():
            self._send_json(400, {"ok": False, "error": "invalid_host"})
            return

        if self.path == "/":
            self._send_html(200, PAGE_HTML)
            return
        if self.path == "/api/health":
            self._send_json(200, {"ok": True, "version": __version__})
            return
        if self.path == "/api/status":
            if not self._require_auth():
                return
            if not self.server.source_session_alive():  # type: ignore[attr-defined]
                self._send_json(503, source_session_missing_payload(self.server.session))  # type: ignore[attr-defined]
                return
            self._send_json(200, build_status_payload(self._tower()), no_store=True)
            return
        if self.path == "/api/workspace":
            if not self._require_auth():
                return
            if not self.server.source_session_alive():  # type: ignore[attr-defined]
                self._send_json(503, source_session_missing_payload(self.server.session))  # type: ignore[attr-defined]
                return
            payload = build_status_payload(self._tower())
            self._send_json(200, {"ok": True, "workspace": payload["workspace"],
                                  "generated_at": payload["generated_at"],
                                  "version": payload["version"]}, no_store=True)
            return

        route = self._pane_route(self.path)
        if route and route[1] == "screen":
            self._get_screen(route[0])
            return
        if route and route[1] == "result":
            self._get_result(route[0])
            return

        self._send_json(404, {"ok": False, "error": "not_found"})

    def _get_screen(self, pane_key: str) -> None:
        """Recent pane text for the live view. Read-only (``capture-pane``
        only), never written to disk or logged, ``Cache-Control: no-store``.
        This is the only endpoint that returns raw terminal text."""

        if not self._require_auth():
            return
        if not self.server.source_session_alive():  # type: ignore[attr-defined]
            self._send_json(409, source_session_missing_payload(self.server.session), no_store=True)  # type: ignore[attr-defined]
            return
        server = self.server
        ok, reason, payload = get_pane_screen(server.session, pane_key, server._own_pane_id)  # type: ignore[attr-defined]
        if not ok:
            self._send_json(404 if reason == "not_found" else 409, {"ok": False, "error": reason}, no_store=True)
            return
        self._send_json(200, payload, no_store=True)

    def _get_result(self, pane_key: str) -> None:
        """Extracted final answer only. Does not mark it read -- opening
        the live pane, or this GET by itself, is not "the user looked at
        the result". Text stays in the process; ``no-store``.
        """

        if not self._require_auth():
            return
        if not self.server.source_session_alive():  # type: ignore[attr-defined]
            self._send_json(409, source_session_missing_payload(self.server.session), no_store=True)  # type: ignore[attr-defined]
            return
        ok, reason, payload = get_result(self._tower(), pane_key)
        if not ok:
            self._send_json(404 if reason == "not_found" else 409, {"ok": False, "error": reason}, no_store=True)
            return
        self._send_json(200, {"ok": True, **payload}, no_store=True)

    def _post_result_read(self, pane_key: str) -> None:
        if not self._require_auth():
            return
        body = self._read_json_body()
        fingerprint = (body or {}).get("fingerprint")
        if not isinstance(fingerprint, str) or not fingerprint:
            self._send_json(400, {"ok": False, "error": "bad_request"})
            return
        tower = self._tower()
        row = find_row(tower, pane_key)
        identity = pane_result_identity(row) if row and not row.get("remote") else None
        ok = self.server.results.mark_read(pane_key, fingerprint, identity=identity)  # type: ignore[attr-defined]
        if not ok:
            self._send_json(409, {"ok": False, "error": "stale_result"})
            return
        self._send_json(200, {"ok": True, "state": "read"})

    def _post_identity(self, pane_key: str) -> None:
        if not self._require_auth():
            return
        body = self._read_json_body()
        if not body:
            self._send_json(400, {"ok": False, "error": "bad_request"})
            return
        tower = self._tower()
        ok, reason = apply_identity_edit(tower, pane_key, body)
        if not ok:
            self._send_json(404 if reason == "not_found" else 400, {"ok": False, "error": reason})
            return
        row = find_row(tower, pane_key) or {}
        self._send_json(
            200,
            {
                "ok": True,
                "identity": {
                    "project": row.get("project"),
                    "display_name": row.get("display_name") or row.get("task_name"),
                    "task_name": row.get("task_name"),
                    "name_origin": row.get("name_origin") or "auto",
                    "role": row.get("role"),
                    "role_label": t(f'role.{row["role"]}') if row.get("role") in ROLE_IDS else "",
                    "agent": row.get("agent"),
                    "pane_title": row.get("pane_title"),
                    "auto_project": row.get("auto_project"),
                    "auto_agent": row.get("auto_agent"),
                },
            },
        )

    def do_POST(self) -> None:  # noqa: N802
        if not self._host_header_allowed():
            self._send_json(400, {"ok": False, "error": "invalid_host"})
            return

        if self.path == "/api/pair":
            body = self._read_json_body()
            code = (body or {}).get("code") if body else None
            label = (body or {}).get("label") if body else None
            pairing: auth.PairingSession = self.server.pairing  # type: ignore[attr-defined]
            token = pairing.try_pair(
                str(code) if code is not None else "",
                label=label if isinstance(label, str) else None,
            )
            if token is None:
                self._send_json(400, {"ok": False, "error": "invalid_code"})
            else:
                self._send_json(200, {"ok": True, "token": token})
            return

        if self.path == "/api/prompt":
            if not self._require_auth():
                return

            body = self._read_json_body(PROMPT_BODY_BYTES)
            if not body:
                self._send_json(400, {"ok": False, "error": "bad_request"})
                return

            pane_key = body.get("pane_key")
            text = body.get("text")
            expected_project = body.get("project")
            expected_agent = body.get("agent")

            if not isinstance(pane_key, str) or not pane_key:
                self._send_json(400, {"ok": False, "error": "missing_pane_key"})
                return
            if not isinstance(text, str) or not text:
                self._send_json(400, {"ok": False, "error": "missing_text"})
                return
            if len(text) > MAX_PROMPT_CHARS:
                self._send_json(413, {"ok": False, "error": "prompt_too_long"})
                return
            if not self.server.source_session_alive():  # type: ignore[attr-defined]
                self._send_json(409, source_session_missing_payload(self.server.session))  # type: ignore[attr-defined]
                return

            outcome = send_prompt(self._tower(), pane_key, text, expected_project, expected_agent)
            ok, reason = outcome.as_tuple()
            if not ok and reason == "send_failed":
                self._send_json(502, {"ok": False})
                return
            if not ok:
                self._send_json(409, {"ok": False, "error": reason})
                return
            self._send_json(
                200,
                {"ok": True, "submitted": bool(outcome.submitted), "reason": outcome.reason},
                no_store=True,
            )
            return

        if self.path == "/api/worksets/start":
            self._post_workset_start()
            return
        if self.path == "/api/groups/action":
            self._post_group_action()
            return

        route = self._pane_route(self.path)
        if route and route[1] == "identity":
            self._post_identity(route[0])
            return
        if route and route[1] == "result":
            self._post_result_read(route[0])
            return
        if route and route[1] == "focus":
            self._post_focus(route[0])
            return
        if route and route[1] == "attention":
            self._post_attention(route[0])
            return

        self._send_json(404, {"ok": False, "error": "not_found"})

    def _post_group_action(self) -> None:
        if not self._require_auth():
            return
        if not self.server.source_session_alive():  # type: ignore[attr-defined]
            self._send_json(409, source_session_missing_payload(self.server.session), no_store=True)  # type: ignore[attr-defined]
            return
        body = self._read_json_body() or {}
        group_id = body.get("group_id")
        target_id = body.get("target_id")
        action = body.get("action")
        destination = body.get("destination_group_id")
        valid_group = lambda value: isinstance(value, str) and re.fullmatch(r"[0-9a-f]{32}", value)
        valid_target = lambda value: isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value)
        if (not isinstance(group_id, str) or (group_id and not valid_group(group_id))
                or action not in {"move", "reorder", "layout", "swap"}
                or (target_id is not None and not valid_target(target_id))
                or (destination is not None and not valid_group(destination))
                or (action == "move" and "destination_group_id" not in body)):
            self._send_json(400, {"ok": False, "error": "bad_request"})
            return
        tower = self._tower()
        store = tower.work_groups
        groups = {row["group_id"]: row for row in store.all()}
        if action != "move" and group_id not in groups:
            self._send_json(404, {"ok": False, "error": "not_found"})
            return
        try:
            if action == "layout":
                layout = body.get("layout")
                if layout not in LAYOUTS:
                    raise ValueError("bad_layout")
                store.set_layout(group_id, layout)
            elif action == "reorder":
                direction = body.get("direction")
                if target_id not in groups[group_id]["member_target_ids"]:
                    raise ValueError("stale_target")
                store.reorder_member(group_id, target_id, direction)
            elif action == "swap":
                second = body.get("second_target_id")
                if not valid_target(second):
                    raise ValueError("bad_request")
                store.swap_layout_slots(group_id, target_id, second)
            else:
                current = store.membership().get(target_id)
                if current != (group_id or None):
                    raise ValueError("stale_target")
                if destination is not None and destination not in groups:
                    raise ValueError("not_found")
                if destination == current:
                    raise ValueError("same_group")
                store.move_target(target_id, destination)
        except (OSError, KeyError, ValueError) as exc:
            reason = str(exc)
            status = 404 if reason == "not_found" else 409 if reason == "stale_target" else 400
            self._send_json(status, {"ok": False, "error": reason}, no_store=True)
            return
        self._send_json(200, {"ok": True, "groups": build_status_payload(tower).get("groups", [])}, no_store=True)

    def _post_workset_start(self) -> None:
        if not self._require_auth():
            return
        body = self._read_json_body()
        if type(body) is not dict:
            self._send_json(400, {"ok": False, "error": "bad_request"})
            return
        workset_id = body.get("id")
        kind = body.get("kind")
        path = body.get("project_path")
        target = body.get("execution_target")
        targets = body.get("execution_targets") or {}
        agents = body.get("agents") or {}
        if (
            not isinstance(workset_id, str) or len(workset_id) != 32
            or kind not in {"saved_work", "template"}
            or (path is not None and (not isinstance(path, str) or len(path) > 2048))
            or (target is not None and (not isinstance(target, str) or len(target) > 255))
            or type(targets) is not dict or len(targets) > 32
            or any(not isinstance(key, str) or not isinstance(value, str) or len(value) > 255
                   for key, value in targets.items())
            or type(agents) is not dict or len(agents) > 32
            or any(not isinstance(key, str) or not isinstance(value, str) for key, value in agents.items())
        ):
            self._send_json(400, {"ok": False, "error": "bad_request"})
            return
        if not self.server.source_session_alive():  # type: ignore[attr-defined]
            self._send_json(409, source_session_missing_payload(self.server.session), no_store=True)  # type: ignore[attr-defined]
            return
        try:
            workset = next((row for row in WorksetStore().list(kind) if row.workset_id == workset_id), None)
            if workset is None:
                self._send_json(404, {"ok": False, "error": "not_found"}, no_store=True)
                return
            from ..launcher.config import AGENT_LABEL_TO_CONFIG_KEY, load_config
            from ..launcher.workset_launch import WorksetLaunchError, launch_workset
            from ..ui.tower import STATE_DIR

            if path is not None and not path.strip():
                raise WorksetLaunchError("프로젝트 경로를 입력하세요.")
            member_ids = {row.member_id for row in workset.members}
            if any(member_id not in member_ids or agent not in AGENT_LABEL_TO_CONFIG_KEY
                   for member_id, agent in agents.items()) or any(member_id not in member_ids for member_id in targets):
                raise WorksetLaunchError("구성원 또는 Agent 선택이 올바르지 않습니다.")
            tower = self._tower()
            name = workset.name
            project_override = None
            if kind == "template" and not path:
                raise WorksetLaunchError("프로젝트 경로를 입력하세요.")
            if path:
                from ..launcher.browse import validate_local_path, validate_remote_path
                selected_target = target or targets.get(workset.member_order[0]) or "auto"
                if selected_target == "auto" or selected_target == tower.local_host:
                    checked = validate_local_path(path)
                    selected_target = tower.local_host
                else:
                    remote = next((row for row in tower.remote_hosts if selected_target == row.get("alias")), None)
                    if remote is None:
                        raise WorksetLaunchError("실행 위치를 찾을 수 없습니다.")
                    checked = validate_remote_path(selected_target, path)
                if not checked.ok or checked.entry is None:
                    raise WorksetLaunchError("프로젝트 경로를 확인할 수 없습니다.")
                project_override = (checked.entry.name, checked.entry.path)
                if kind == "template":
                    name = f"{checked.entry.name} · {workset.name}"
            result = launch_workset(
                tower, workset, STATE_DIR, agents_cfg=load_config()["agents"],
                project_override=project_override, agent_overrides=agents,
                execution_target=target,
                execution_targets=targets,
                group_name=name,
            )
        except (WorksetError, WorksetLaunchError, ValueError) as exc:
            self._send_json(409, {"ok": False, "error": str(exc)}, no_store=True)
            return
        except OSError:
            self._send_json(409, {"ok": False, "error": "작업을 안전하게 시작하지 못했습니다."}, no_store=True)
            return
        self._send_json(200, {
            "ok": True,
            "group_name": result.group_name,
            "member_count": len(result.members),
        }, no_store=True)

    def _post_focus(self, pane_key: str) -> None:
        """Move the attached tmux client to this pane. Not a prompt and
        not a keystroke: ``select-window`` + ``select-pane`` only, and
        only inside the session this remote was bound to.
        """

        if not self._require_auth():
            return
        if not self.server.source_session_alive():  # type: ignore[attr-defined]
            self._send_json(409, source_session_missing_payload(self.server.session), no_store=True)  # type: ignore[attr-defined]
            return
        server = self.server
        ok, reason = focus_pane(server.session, pane_key, server._own_pane_id)  # type: ignore[attr-defined]
        if not ok:
            self._send_json(404 if reason == "not_found" else 409, {"ok": False, "error": reason}, no_store=True)
            return
        self._send_json(200, {"ok": True, "pane_id": pane_key}, no_store=True)

    def _post_attention(self, pane_key: str) -> None:
        """Explicit approve/reject. Not a shell command and not a prompt."""

        if not self._require_auth():
            return
        body = self._read_json_body() or {}
        action = body.get("action")
        if action == "option":
            self._post_interaction_key(pane_key, str(body.get("key") or ""))
            return
        if action == "text":
            self._post_interaction_text(pane_key, body.get("text") if isinstance(body.get("text"), str) else "")
            return
        if action not in ("approve", "reject"):
            self._send_json(400, {"ok": False, "error": "bad_request"})
            return
        if not self.server.source_session_alive():  # type: ignore[attr-defined]
            self._send_json(409, source_session_missing_payload(self.server.session), no_store=True)  # type: ignore[attr-defined]
            return
        ok, reason = respond_attention(self._tower(), pane_key, action)
        if not ok:
            code = 404 if reason == "not_found" else 409
            self._send_json(code, {"ok": False, "error": reason}, no_store=True)
            return
        self._send_json(200, {"ok": True, "action": action}, no_store=True)

    def _post_interaction_key(self, pane_key: str, key: str) -> None:
        if not self._require_auth():
            return
        if not self.server.source_session_alive():  # type: ignore[attr-defined]
            self._send_json(409, source_session_missing_payload(self.server.session), no_store=True)  # type: ignore[attr-defined]
            return
        ok, reason = respond_interaction(self._tower(), pane_key, key)
        if not ok:
            code = 404 if reason == "not_found" else 409
            self._send_json(code, {"ok": False, "error": reason}, no_store=True)
            return
        self._send_json(200, {"ok": True, "action": "option"}, no_store=True)

    def _post_interaction_text(self, pane_key: str, text: str) -> None:
        if not self._require_auth():
            return
        if not self.server.source_session_alive():  # type: ignore[attr-defined]
            self._send_json(409, source_session_missing_payload(self.server.session), no_store=True)  # type: ignore[attr-defined]
            return
        outcome = respond_interaction_text(self._tower(), pane_key, text)
        if not outcome.ok:
            code = 404 if outcome.reason == "not_found" else 409
            self._send_json(code, {"ok": False, "error": outcome.reason}, no_store=True)
            return
        self._send_json(200, {"ok": True, "action": "text"}, no_store=True)

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        # Silence BaseHTTPRequestHandler's default stderr access log --
        # this runs in a user's foreground terminal via `tower serve`,
        # not a service with its own log pipeline.
        pass


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, handler, session: str, own_pane_id: str, allowed_hosts: List[str], token_path, result_state_path=None):
        super().__init__(address, handler)
        self._session = session
        self._own_pane_id = own_pane_id
        self.allowed_hosts = allowed_hosts
        self.token_store = auth.TokenStore(token_path)
        self.pairing = auth.PairingSession(self.token_store)
        self._tower_lock = threading.Lock()
        # Result metadata is shared with the TUI through one local SQLite
        # file. Result text remains in memory only.
        self.status_engine = StatusEngine()
        self.results = ResultTracker(result_state_path or shared_result_state_path())

    @property
    def session(self) -> str:
        return self._session

    def source_session_alive(self) -> bool:
        """Checked per request. The session is fixed at start; if it has
        been deleted since, every status/prompt call says so instead of
        returning an empty local list.
        """

        return tmux_capture.session_exists(self._session)

    def make_tower(self) -> Tower:
        # A fresh, headless Tower per request: cheap (no curses, no
        # persistent socket), and avoids sharing mutable UI-only state
        # (attention_mode, filter_text, selection) between concurrent
        # requests from possibly multiple paired devices.
        with self._tower_lock:
            return Tower(
                self._session,
                self._own_pane_id,
                status_engine=self.status_engine,
                results=self.results,
            )


def default_token_path():
    return CONFIG_DIR / "remote-tokens.json"


def create_server(
    host: str,
    port: int,
    session: str,
    own_pane_id: str,
    allowed_hosts: List[str],
    token_path=None,
    result_state_path=None,
) -> _Server:
    """``allowed_hosts`` must be the exact set of hostnames/IPs the caller
    is about to print/QR to the user (e.g. ``["localhost", "127.0.0.1"]``,
    plus the detected LAN IP for ``--lan``) -- see ``TowerRemoteHandler``'s
    ``Host`` header check. Never pass a wildcard here.
    """

    return _Server(
        (host, port),
        TowerRemoteHandler,
        session,
        own_pane_id,
        list(allowed_hosts),
        token_path or default_token_path(),
        result_state_path,
    )
