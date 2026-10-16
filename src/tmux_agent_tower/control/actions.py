"""One action layer for the Tower TUI and the phone remote.

View, prompt, identity, focus, result, and closing one pane all go
through here. Neither UI grows its own tmux calls for those actions.
Approval sends one key the adapter already named for the current widget.
Nothing here restarts a fleet, runs a shell, or guesses Enter or y.
"""

from __future__ import annotations

import os
import hashlib
import json
import re
import subprocess
import time
from dataclasses import dataclass, replace
from typing import List, Optional, Tuple

from ..adapters.base import ResultCandidate
from ..state.overrides import FIELDS as OVERRIDE_FIELDS, ROLE_IDS
from ..detection.result import pane_result_identity
from ..detection.providers import ResultProvider, ResultTarget, normalize_candidate
from ..tmux import capture as tmux_capture
from ..tmux.navigation import focus_local_pane

MAX_PROMPT_CHARS = 32000
SEND_TIMEOUT = 3.0
MAX_SCREEN_LINES = 60
MAX_SCREEN_LINE_CHARS = 400
MAX_SCREEN_BYTES = 16 * 1024
MAX_IDENTITY_CHARS = 80
# Explicit Y/S only. The 2s refresh stays on CAPTURE_LINES (30).
RECOVERY_LINES = 800
RECOVERY_BYTES = 256 * 1024
LONG_PROMPT_CONFIRM_CHARS = 2000

_ANSI_RE = re.compile(
    r"\x1b\[[0-?]*[ -/]*[@-~]"
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"
    r"|\x1b[@-Z\\-_]"
)
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_LOCAL_PANE_KEY_RE = re.compile(r"^%\d+$")


def enter_intent(row: Optional[dict]) -> str:
    """What Enter does. It never focuses a pane or a window.

    A window row opens Window Control. A pane row opens Pane Control.
    A zero-state row starts a create action. Nothing here moves tmux.
    """

    if not row or row.get("offline") or row.get("placeholder"):
        return "ignore"
    if row.get("kind") == "work_group":
        return "group"
    if row.get("kind") in {"folder", "other_section", "window_asset"}:
        return "toggle"
    if row.get("kind") == "work_group_stale":
        return "ignore"
    if row.get("kind") == "window":
        if row.get("remote") or not row.get("window_id"):
            return "ignore"
        return "window"
    if row.get("kind") == "zero" and row.get("action"):
        return "zero"
    if row.get("pane_id") or row.get("key"):
        return "control"
    return "ignore"


def sanitize_screen_line(line: str) -> str:
    """Plain text only: drop ANSI and control bytes, clamp width."""

    text = _ANSI_RE.sub("", line)
    text = _CONTROL_RE.sub("", text)
    if len(text) > MAX_SCREEN_LINE_CHARS:
        text = text[: MAX_SCREEN_LINE_CHARS - 1] + "…"
    return text


def build_screen_payload(pane_key: str, raw_lines: List[str]) -> dict:
    """Recent screen text within hard limits. The tail is kept."""

    lines = [sanitize_screen_line(line) for line in raw_lines]
    while lines and not lines[-1].strip():
        lines.pop()
    truncated = len(lines) > MAX_SCREEN_LINES
    lines = lines[-MAX_SCREEN_LINES:]

    def size(items: List[str]) -> int:
        return sum(len(item.encode("utf-8")) + 1 for item in items)

    while lines and size(lines) > MAX_SCREEN_BYTES:
        lines.pop(0)
        truncated = True

    return {
        "ok": True,
        "pane_key": pane_key,
        "lines": lines,
        "truncated": truncated,
        "generated_at": time.time(),
    }


def find_screen_target(session: str, pane_key: str, own_pane_id: str = "") -> Tuple[bool, str, Optional[str]]:
    """``(ok, reason, tmux_pane_id)`` for a local pane in ``session``.

    Reasons: ``not_found``, ``remote_unsupported``, ``stale``.
    """

    if not _LOCAL_PANE_KEY_RE.match(pane_key or ""):
        return False, ("remote_unsupported" if pane_key and ":" in pane_key else "not_found"), None
    if own_pane_id and pane_key == own_pane_id:
        return False, "not_found", None
    listed = tmux_capture.run_tmux(["list-panes", "-s", "-t", session, "-F", "#{pane_id}"]).split("\n")
    if pane_key not in listed:
        return False, "not_found", None
    if not tmux_capture.pane_exists(pane_key):
        return False, "stale", None
    return True, "", pane_key


def _pane_matches_live_identity(session: str, pane_id: str, expected_pane_pid: str) -> bool:
    listed = tmux_capture.run_tmux(
        ["list-panes", "-s", "-t", session, "-F", "#{pane_id}\t#{pane_pid}\t#{pane_dead}"]
    )
    return any(
        len(parts := line.split("\t")) == 3
        and parts[0] == pane_id
        and parts[1] == expected_pane_pid
        and parts[2] == "0"
        for line in listed.split("\n")
    )


def get_pane_screen(
    session: str,
    pane_key: str,
    own_pane_id: str = "",
    *,
    expected_pane_pid: str = "",
) -> Tuple[bool, str, dict]:
    """Recent terminal output of one local pane. ``capture-pane`` only."""

    ok, reason, pane_id = find_screen_target(session, pane_key, own_pane_id)
    if not ok or not pane_id:
        return False, reason or "not_found", {}
    if expected_pane_pid and not _pane_matches_live_identity(session, pane_id, expected_pane_pid):
        return False, "stale", {}
    raw = tmux_capture.capture_pane(pane_id, lines=MAX_SCREEN_LINES + 1)
    if expected_pane_pid and not _pane_matches_live_identity(session, pane_id, expected_pane_pid):
        return False, "stale", {}
    return True, "", build_screen_payload(pane_key, raw)


def _clean_identity_value(value) -> Optional[str]:
    if not isinstance(value, str):
        return None
    text = "".join(ch for ch in value if ch.isprintable()).strip()
    if not text or len(text) > MAX_IDENTITY_CHARS:
        return None
    return text


def apply_identity_edit(tower, pane_key: str, body: dict) -> Tuple[bool, str]:
    """Display metadata only, on the same OverrideStore the TUI uses."""

    tower.load()
    row = next((r for r in tower.rows if r.get("key") == pane_key and not r.get("placeholder")), None)
    if row is None:
        return False, "not_found"

    if body.get("reset") is True:
        tower.overrides.reset(pane_key)
        return True, ""

    session = str(row.get("session") or "")
    pane_pid = str(row.get("pane_pid") or "").strip()
    # A label without a process id cannot be checked when this pane id is reused.
    if not session or not pane_pid:
        return False, "stale"

    task_name_edited = "task_name" in body
    if (
        row.get("name_origin") == "auto"
        and not task_name_edited
        and any(field in body for field in ("project", "agent", "title"))
    ):
        tower.overrides.set_task_name(
            pane_key,
            row.get("display_name") or row.get("task_name") or "",
            session,
            pane_pid,
            origin="auto",
        )

    touched = False
    for field in OVERRIDE_FIELDS:
        if field not in body:
            continue
        value = body[field]
        if value is None:
            tower.overrides.clear_field(pane_key, field)
            touched = True
            continue
        cleaned = _clean_identity_value(value)
        if cleaned is None:
            return False, f"invalid_{field}"
        if field == "role" and cleaned not in ROLE_IDS:
            return False, "invalid_role"
        getattr(tower.overrides, f"set_{field}")(pane_key, cleaned, session, pane_pid)
        touched = True
        if field == "title" and not row.get("remote"):
            tmux_capture.run_tmux(["select-pane", "-t", row["pane_id"], "-T", cleaned], capture=False)

    if not touched:
        return False, "bad_request"
    if (
        "role" in body
        and row.get("name_origin") == "auto"
        and ("task_name" not in body or body.get("task_name") is None)
    ):
        tower.load()
        updated = next(
            (
                item for item in tower.rows
                if item.get("key") == pane_key
                and str(item.get("session") or "") == session
                and str(item.get("pane_pid") or "").strip() == pane_pid
            ),
            None,
        )
        if updated and updated.get("suggested_name"):
            tower.overrides.set_task_name(
                pane_key, updated["suggested_name"], session, pane_pid, origin="auto"
            )
    return True, ""


def update_identity(tower, pane_key: str, body: dict) -> Tuple[bool, str]:
    return apply_identity_edit(tower, pane_key, body)


def find_row(tower, pane_key: str):
    tower.load()
    return next((r for r in tower.rows if r.get("key") == pane_key), None)


def find_prompt_target(
    tower, pane_key: str, expected_project: Optional[str], expected_agent: Optional[str],
    expected_identity: Optional[dict] = None, *, require_new_prompt: bool = False,
) -> Tuple[bool, str, Optional[str]]:
    """Fresh snapshot. Refuses remote, dead, and selected identity mismatch."""

    tower.load()
    for row in tower.rows:
        if row.get("key") != pane_key or row.get("placeholder"):
            continue
        if row.get("remote"):
            return False, "remote_unsupported", None
        if row.get("status") == "DEAD":
            return False, "dead", None
        if expected_project is not None and row.get("project") != expected_project:
            return False, "mismatch", None
        if expected_agent is not None and row.get("agent") != expected_agent:
            return False, "mismatch", None
        if require_new_prompt and (
            row.get("attention") not in (None, "", "none") or row.get("interaction") is not None
        ):
            return False, "interaction_unknown", None
        if expected_identity is not None:
            fields = {
                "pane_id": "pane_id",
                "session": "session",
                "pane_pid": "pane_pid",
                "host": "execution_host",
                "transport": "transport",
                "path": "path",
                "provider": "auto_agent",
                "task_label": "task_name",
                "project": "project",
            }
            for expected_field, row_field in fields.items():
                if expected_field in expected_identity and str(row.get(row_field) or "") != str(
                    expected_identity[expected_field] or ""
                ):
                    return False, "mismatch", None
        return True, "", row.get("pane_id") or row.get("key")
    return False, "not_found", None


SUBMIT_CONFIRM_SECONDS = 3.0
SUBMIT_POLL_SECONDS = 0.25
_SAFE_SUBMIT_KEYS = frozenset({"Enter"})


@dataclass
class SubmitResult:
    """``ok`` means the text reached the pane. ``submitted`` means the
    agent was seen taking it as a new turn. They are reported separately
    so the phone never claims a submit it did not observe."""

    ok: bool
    submitted: bool = False
    reason: str = ""
    pane_id: str = ""

    def as_tuple(self) -> Tuple[bool, str]:
        return self.ok, self.reason


def send_text(pane_id: str, text: str) -> bool:
    """Deliver text as one bracketed paste.

    Measured live: ``send-keys -l`` followed by Enter is read by Codex as
    one paste burst and the Enter becomes a newline, so the prompt sat in
    the composer. ``load-buffer`` + ``paste-buffer -p`` tells the TUI
    where the paste ends. Korean, multiline, quotes, and code fences
    arrived intact in Codex, Claude Code, and OpenCode. ``shell=False``.
    """

    body = text or ""
    if not body:
        return False
    name = f"tower-prompt-{os.getpid()}-{time.monotonic_ns()}"
    try:
        loaded = subprocess.run(
            ["tmux", "load-buffer", "-b", name, "-"],
            input=body.encode("utf-8"),
            capture_output=True,
            timeout=SEND_TIMEOUT,
            check=False,
        )
        if loaded.returncode != 0:
            return False
        pasted = subprocess.run(
            ["tmux", "paste-buffer", "-d", "-p", "-b", name, "-t", pane_id],
            capture_output=True,
            timeout=SEND_TIMEOUT,
            check=False,
        )
        return pasted.returncode == 0
    except Exception:
        subprocess.run(["tmux", "delete-buffer", "-b", name], capture_output=True, check=False)
        return False


def submit_input(pane_id: str, key: str = "Enter") -> bool:
    """Exactly one submit key. No retry lives here or in any caller."""

    if key not in _SAFE_SUBMIT_KEYS:
        return False
    try:
        result = subprocess.run(
            ["tmux", "send-keys", "-t", pane_id, key],
            capture_output=True,
            timeout=SEND_TIMEOUT,
            check=False,
        )
        return result.returncode == 0
    except Exception:
        return False


def send_prompt_to_pane(pane_id: str, text: str, submit_key: str = "Enter") -> bool:
    """Paste once, then one submit key."""

    if not send_text(pane_id, text):
        return False
    return submit_input(pane_id, submit_key)


def pane_title_and_command(pane_id: str) -> Optional[Tuple[str, str]]:
    """Title and command for one pane id.

    ``list-panes -t %id`` lists every pane in that window. The first
    separator then glues the rest of the window onto the command, and
    Y resolves Shell instead of the agent on the selected pane.
    """

    raw = tmux_capture.run_tmux(
        ["display-message", "-p", "-t", pane_id, "#{pane_title}\x1f#{pane_current_command}"]
    )
    line = (raw or "").splitlines()[0] if raw else ""
    if "\x1f" not in line:
        return None
    title, command = line.split("\x1f", 1)
    return title.strip(), command.strip()


def pane_process_id(pane_id: str) -> str:
    return tmux_capture.run_tmux(
        ["display-message", "-p", "-t", pane_id, "#{pane_pid}"]
    ).strip()


def _observe(pane_id: str, *, lines: int = 40):
    """Adapter and screen for one pane right now. None if it is gone."""

    from ..adapters import resolve_adapter
    from ..adapters.base import PaneContext

    found = pane_title_and_command(pane_id)
    if found is None:
        return None
    title, command = found
    captured = tmux_capture.capture_pane(pane_id, lines=lines)
    ctx = PaneContext(
        title=title, command=command, lines=tuple(captured), pane_pid=pane_process_id(pane_id),
    )
    adapter = resolve_adapter(command, title, "")
    watermark = getattr(adapter, "turn_watermark", None)
    if callable(watermark):
        ctx = replace(ctx, turn_watermark=watermark(ctx))
    return adapter, ctx


def confirm_submission(
    pane_id: str, adapter, before, text: str, timeout: Optional[float] = None, *, lines: int = 40,
) -> bool:
    """Watch the pane briefly for evidence the agent took the text.

    Nothing is sent from here. False means not confirmed, not failed.
    """

    deadline = time.monotonic() + (SUBMIT_CONFIRM_SECONDS if timeout is None else timeout)
    while True:
        fresh = _observe(pane_id, lines=lines)
        if fresh is not None:
            _adapter, after = fresh
            if adapter.confirm_submitted(before, after, text) is True:
                return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(SUBMIT_POLL_SECONDS)


def send_prompt(
    tower, pane_key: str, text: str, expected_project: Optional[str] = None, expected_agent: Optional[str] = None,
    expected_identity: Optional[dict] = None, *, require_new_prompt: bool = False,
) -> SubmitResult:
    """One explicit prompt into one still-live local pane, then a short
    before/after check. One paste, one Enter, no blind retry."""

    if not isinstance(text, str) or not text.strip():
        return SubmitResult(False, reason="missing_text")
    if expected_identity is None and not require_new_prompt:
        ok, reason, pane_id = find_prompt_target(tower, pane_key, expected_project, expected_agent)
    else:
        ok, reason, pane_id = find_prompt_target(
            tower, pane_key, expected_project, expected_agent, expected_identity,
            require_new_prompt=require_new_prompt,
        )
    if not ok or not pane_id:
        return SubmitResult(False, reason=reason or "not_found")
    confirm_lines = RECOVERY_LINES if len(text) > LONG_PROMPT_CONFIRM_CHARS else 40
    observed = _observe(pane_id, lines=confirm_lines)
    adapter, before = observed if observed is not None else (None, None)
    tracker = getattr(tower, "results", None)
    identity = None
    watermark = None
    if tracker is not None:
        row = find_row(tower, pane_key)
        identity = pane_result_identity(row) if row is not None else None
        watermark = tmux_capture.pane_history_position(pane_id)
    submit_key = adapter.submit_key(before) if adapter is not None else "Enter"
    if not send_prompt_to_pane(pane_id, text, submit_key):
        return SubmitResult(False, reason="send_failed", pane_id=pane_id)
    if adapter is None or before is None:
        return SubmitResult(True, submitted=False, reason="submit_not_confirmed", pane_id=pane_id)
    if confirm_submission(pane_id, adapter, before, text, lines=confirm_lines):
        if tracker is not None and watermark is not None:
            tracker.record_turn_start(pane_key, watermark, identity)
        return SubmitResult(True, submitted=True, reason="submitted", pane_id=pane_id)
    fresh = _observe(pane_id, lines=confirm_lines)
    extra = None
    if fresh is not None:
        extra = adapter.followup_submit_key(before, fresh[1], text)
    if not extra:
        return SubmitResult(True, submitted=False, reason="submit_not_confirmed", pane_id=pane_id)
    if not submit_input(pane_id, extra):
        return SubmitResult(True, submitted=False, reason="needs_submit", pane_id=pane_id)
    if confirm_submission(pane_id, adapter, before, text):
        if tracker is not None and watermark is not None:
            tracker.record_turn_start(pane_key, watermark, identity)
        return SubmitResult(True, submitted=True, reason="submitted", pane_id=pane_id)
    return SubmitResult(True, submitted=False, reason="submit_not_confirmed", pane_id=pane_id)


def focus_pane(session: str, pane_key: str, own_pane_id: str = "") -> Tuple[bool, str]:
    """``select-window`` + ``select-pane`` for one live local pane id."""

    ok, reason, pane_id = find_screen_target(session, pane_key, own_pane_id)
    if not ok or not pane_id:
        return False, reason or "not_found"
    if not focus_local_pane(session, pane_id):
        return False, "stale"
    return True, ""


def get_result(
    tower, pane_key: str, *, expected_pane_pid: Optional[str] = None,
) -> Tuple[bool, str, dict]:
    """Result state and, when ready or read, the extracted body.

    A fresh Tower process has an empty tracker. Y and S then read a
    bounded slice of this pane's tmux history once. Polling does not.
    """

    row = find_row(tower, pane_key)
    if row is None or row.get("placeholder"):
        return False, "not_found", {}
    if expected_pane_pid is not None and not row.get("remote"):
        row_pid = str(row.get("pane_pid") or "")
        if (
            not expected_pane_pid or not row_pid or row_pid != expected_pane_pid
            or not _pane_matches_live_identity(
                getattr(tower, "session", "") or "", str(row.get("pane_id") or ""), expected_pane_pid,
            )
        ):
            return False, "stale", {"reason_code": "UNKNOWN"}
    target = ResultTarget.from_row(row)
    if row.get("remote"):
        provider = _RecoveryProvider(lambda target: _recover_remote_result(target.remote_provider_row()))
        candidate = provider.get_latest_complete_result(target)
        return True, "", _candidate_payload(candidate) if candidate else provider.observation
    if target.transport == "ssh" and (target.agent == "OpenCode" or row.get("result_adapter") == "OpenCode"):
        if row.get("result_adapter") not in (None, "", "OpenCode"):
            return True, "", _remote_opencode_failure("AGENT_UNBOUND")
        session_probe = _get_ssh_opencode_result(tower, row, target, pane_key, expected_pane_pid)
        if session_probe[2].get("reason_code") != "SESSION_ID_NOT_EXPOSED":
            return session_probe
        # TUI launch arguments do not prove which session is currently selected;
        # its adapter must prove both result boundaries from the visible history.
    if target.transport == "ssh" and target.agent == "Grok":
        from ..detection.sshdest import ssh_connection_for_pane

        connection = ssh_connection_for_pane(target.tower_pane_pid, target.transport_target)
        if connection is None:
            return True, "", _remote_grok_failure("REMOTE_PROVIDER_UNBOUND")
        target = replace(
            target,
            provider_type="remote_grok",
            provider_endpoint=target.transport_target,
            ssh_client_pid=connection["ssh_client_pid"],
            ssh_connection=(
                connection["client_ip"], connection["client_port"],
                connection["server_ip"], connection["server_port"],
            ),
            ssh_local_socket=tuple(connection.get("local_socket") or ()),
            ssh_connection_mode=str(connection.get("connection_mode") or "direct"),
        )
        provider = _RecoveryProvider(
            lambda resolved: _recover_ssh_grok_result(resolved.remote_provider_row())
        )
        candidate = provider.get_latest_complete_result(target)
        if candidate is None:
            return True, "", provider.observation or _remote_grok_failure("REMOTE_PROVIDER_UNBOUND")
        payload = _candidate_payload(candidate, provider.observation.get("state") or "ready")
        payload.update({key: value for key, value in provider.observation.items() if key.startswith("remote_") or key in {
            "grok_pid", "session_id", "turn_number", "native_length", "native_utf8_bytes", "native_sha256",
            "local_ssh_pid", "ssh_connection", "ssh_local_socket", "ssh_connection_mode",
        }})
        return True, "", payload
    identity = pane_result_identity(row)
    tracker = getattr(tower, "results", None)
    if tracker is None:
        return True, "", {
            "state": row.get("result_state") or "none", "text": "", "fingerprint": "",
            "generated_at": 0.0, "complete": False,
        }
    if row.get("status") == "DEAD":
        snap = tracker.snapshot(pane_key, identity=identity)
        return True, "", _payload(snap)
    # Y copies the newest finished body in this pane's history. The
    # 30-line poll can start mid-answer, so a stored fragment is not
    # the payload. A screen that cannot be read keeps the stored body.
    source = target.provider_type if target.provider_type in {
        "local_tmux", "ssh_visible_history",
    } else ("ssh_visible_history" if row.get("transport") == "ssh" else "local_tmux")
    provider = _RecoveryProvider(
        lambda resolved: _recover_result(
            tower, resolved.tower_pane_id or pane_key, tracker, identity, source=source,
            expected_pane_pid=expected_pane_pid or target.tower_pane_pid,
        )
    )
    candidate = provider.get_latest_complete_result(target)
    recovered = provider.observation
    if candidate is not None:
        return True, "", _candidate_payload(candidate, recovered.get("state") or "ready")
    if row.get("status") != "WORKING" and (
        recovered is None or (not recovered.get("complete") and not recovered.get("working"))
    ):
        remote_target = ResultTarget.from_row(row)
        remote_provider = _RecoveryProvider(
            lambda resolved: _recover_remote_result(resolved.remote_provider_row())
        )
        remote_candidate = remote_provider.get_latest_complete_result(remote_target)
        if remote_candidate is not None:
            return True, "", _candidate_payload(remote_candidate, remote_provider.observation.get("state") or "ready")
    if recovered is None:
        snap = tracker.snapshot(pane_key, identity=identity)
        if row.get("status") == "WORKING" or not snap.complete:
            return True, "", _empty_incomplete(snap)
        return True, "", _payload(snap)
    return True, "", recovered


def _payload(
    snap, source: str = "tracker", turn_identity: str = "", timestamp: Optional[float] = None,
    confidence: str = "unknown",
) -> dict:
    return {
        "state": snap.state,
        "text": snap.text or "",
        "fingerprint": snap.fingerprint,
        "generated_at": snap.generated_at,
        "complete": bool(getattr(snap, "complete", False)),
        "turn_complete": bool(getattr(snap, "turn_complete", snap.complete)),
        "body_complete": bool(getattr(snap, "body_complete", snap.complete)),
        "source": source,
        "turn_identity": turn_identity,
        "timestamp": snap.generated_at if timestamp is None else timestamp,
        "confidence": confidence,
    }


def _candidate_payload(candidate, state: str = "ready") -> dict:
    return {
        "state": state,
        "text": candidate.text,
        "fingerprint": candidate.fingerprint,
        "generated_at": candidate.timestamp,
        "complete": candidate.complete,
        "turn_complete": candidate.turn_complete,
        "body_complete": candidate.body_complete,
        "turn_complete": candidate.turn_complete,
        "body_complete": candidate.body_complete,
        "source": candidate.source,
        "turn_identity": candidate.turn_identity,
        "timestamp": candidate.timestamp,
        "confidence": candidate.confidence,
    }


class _RecoveryProvider(ResultProvider):
    """Small adapter that normalizes each bounded reader to ResultCandidate."""

    def __init__(self, reader):
        self.reader = reader
        self.observation = None

    def get_latest_complete_result(self, target: ResultTarget):
        self.observation = self.reader(target)
        payload = self.observation or {}
        if (
            payload.get("turn_complete") is not True
            or payload.get("body_complete") is not True
            or not payload.get("complete")
            or not payload.get("text")
        ):
            return None
        candidate = ResultCandidate(
            payload["text"], payload.get("fingerprint") or "",
            payload.get("confidence") or "unknown", True,
            payload.get("source") or "", payload.get("turn_identity") or "",
            payload.get("timestamp") or payload.get("generated_at") or 0.0,
            payload["turn_complete"], payload["body_complete"],
        )
        return normalize_candidate(
            candidate, target, source=payload.get("source") or target.provider_type or "local_tmux",
            timestamp=payload.get("timestamp") or payload.get("generated_at") or time.time(),
        )


def _empty_incomplete(snap) -> dict:
    payload = _payload(snap)
    payload.update(text="", complete=False, turn_complete=False, body_complete=False)
    return payload


def _recover_remote_result(row: dict) -> dict:
    """Resolve one live, explicitly registered remote tmux target."""

    from ..adapters.base import PaneContext
    from ..detection.topology import resolve_runtime
    from ..remote.collector import fetch_remote_history

    provider_type = row.get("result_provider_type") or ""
    endpoint = row.get("result_provider_endpoint") or row.get("result_provider_host") or ""
    pane_id = row.get("result_provider_pane_id") or row.get("pane_id") or ""
    pane_pid = str(row.get("result_provider_pane_pid") or row.get("pane_pid") or "")
    session_id = row.get("result_provider_session_id") or row.get("session_id") or ""
    if provider_type != "remote_tmux" or not endpoint or not session_id or row.get("result_provider_liveness") == "stale":
        return {"state": "none", "text": "", "fingerprint": "", "generated_at": 0.0, "complete": False}
    lines = fetch_remote_history(
        endpoint, pane_id, pane_pid, lines=RECOVERY_LINES, session_id=session_id, join_wrapped=True,
    )
    if lines is None:
        return {"state": "none", "text": "", "fingerprint": "", "generated_at": 0.0, "complete": False}
    lines = _bounded_history(lines)
    title = row.get("pane_title") or ""
    command = row.get("command") or ""
    _name, _source, adapter = resolve_runtime(command, title, row.get("cmdline") or "", lines)
    ctx = PaneContext(title=title, command=command, lines=tuple(lines))
    if adapter.classify(ctx).status == "WORKING":
        return {
            "state": "none", "text": "", "fingerprint": "", "generated_at": 0.0,
            "complete": False, "working": True,
        }
    candidate = adapter.extract_result(ctx)
    if candidate is None or not candidate.text or not candidate.complete:
        return {
            "state": "none", "text": "", "fingerprint": "", "generated_at": 0.0,
            "complete": False,
            "turn_complete": bool(candidate and candidate.turn_complete),
            "body_complete": bool(candidate and candidate.body_complete),
        }
    turn_identity = ":".join((endpoint, session_id, pane_id, pane_pid, candidate.fingerprint))
    candidate = type(candidate)(
        candidate.text, candidate.fingerprint, candidate.confidence, candidate.complete,
        source="remote_tmux", turn_identity=turn_identity, timestamp=time.time(),
        turn_complete=candidate.turn_complete, body_complete=candidate.body_complete,
    )
    return {
        "state": "ready",
        "text": candidate.text,
        "fingerprint": candidate.fingerprint,
        "generated_at": candidate.timestamp,
        "complete": True,
        "turn_complete": candidate.turn_complete,
        "body_complete": candidate.body_complete,
        "source": candidate.source,
        "turn_identity": candidate.turn_identity,
        "timestamp": candidate.timestamp,
        "confidence": candidate.confidence,
    }


def _remote_grok_failure(source: str) -> dict:
    return {
        "state": "none", "text": "", "fingerprint": "", "generated_at": 0.0,
        "complete": False, "turn_complete": False, "body_complete": False, "source": source,
    }


def _remote_opencode_failure(source: str) -> dict:
    return {
        "state": "none", "text": "", "fingerprint": "", "generated_at": 0.0,
        "complete": False, "turn_complete": False, "body_complete": False,
        "source": source, "reason_code": source,
    }


def _recover_ssh_opencode_result(row: dict) -> dict:
    """Probe a pane-bound OpenCode TUI and fail closed if its active session is unknown."""

    from ..remote.opencode import fetch_remote_opencode_result

    endpoint = str(row.get("result_provider_endpoint") or "")
    connection = tuple(row.get("ssh_connection") or ())
    if (
        row.get("result_provider_type") != "remote_opencode" or not endpoint
        or not str(row.get("ssh_client_pid") or "").isdecimal() or len(connection) != 4
    ):
        return _remote_opencode_failure("REMOTE_PROVIDER_UNBOUND")
    requested = {
        "ssh_client_pid": str(row["ssh_client_pid"]),
        "client_ip": connection[0], "client_port": connection[1],
        "server_ip": connection[2], "server_port": connection[3],
    }
    result = fetch_remote_opencode_result(endpoint, requested)
    if not isinstance(result, dict):
        return _remote_opencode_failure("REMOTE_PROVIDER_UNAVAILABLE")
    status = str(result.get("status") or "UNKNOWN")
    if status != "ok":
        if status == "REMOTE_RESULT_INCOMPLETE":
            reason_code = str(result.get("reason_code") or status)
            if reason_code not in {"REMOTE_RESULT_INCOMPLETE", "SESSION_BINDING_CHANGED", "OUTPUT_RANGE_EXCEEDED"}:
                reason_code = "REMOTE_RESULT_INCOMPLETE"
            return _remote_opencode_failure(reason_code)
        if (
            status == "AGENT_SESSION_UNBOUND"
            and result.get("session_binding") == "active_tui_session_unexposed"
        ):
            return _remote_opencode_failure("SESSION_ID_NOT_EXPOSED")
        if status not in {
            "AGENT_SESSION_UNBOUND", "REMOTE_PROVIDER_UNBOUND", "REMOTE_PROVIDER_UNAVAILABLE",
            "REMOTE_PROVIDER_TIMEOUT", "REMOTE_RESULT_INCOMPLETE", "OUTPUT_RANGE_EXCEEDED",
        }:
            status = "UNKNOWN"
        return _remote_opencode_failure(status)
    if result.get("ssh_connection") != list(connection):
        return _remote_opencode_failure("REMOTE_PROVIDER_UNBOUND")
    session_id = str(result.get("session_id") or "")
    user_id = str(result.get("user_id") or "")
    message_id = str(result.get("message_id") or "")
    remote_session_pid = str(result.get("remote_session_pid") or "")
    remote_sshd_pid = str(result.get("remote_sshd_pid") or "")
    opencode_pid = str(result.get("opencode_pid") or "")
    text = result.get("text")
    if (
        not re.fullmatch(r"ses_[A-Za-z0-9_-]{8,128}", session_id)
        or not user_id.startswith("msg_") or not message_id.startswith("msg_")
        or not all(value.isdecimal() for value in (remote_session_pid, remote_sshd_pid, opencode_pid))
        or result.get("turn_complete") is not True or result.get("body_complete") is not True
        or not isinstance(text, str) or not text
    ):
        return _remote_opencode_failure("REMOTE_RESULT_INCOMPLETE")
    body = text.encode("utf-8", "strict")
    native_hash = hashlib.sha256(body).hexdigest()
    if (
        isinstance(result.get("native_length"), bool)
        or not isinstance(result.get("native_length"), int)
        or len(text) != result.get("native_length")
        or len(body) != result.get("native_utf8_bytes")
        or native_hash != result.get("native_sha256")
    ):
        return _remote_opencode_failure("REMOTE_RESULT_INCOMPLETE")
    turn_identity = ":".join((
        str(row["ssh_client_pid"]), *connection, remote_session_pid, remote_sshd_pid,
        opencode_pid, session_id, user_id, message_id, native_hash,
    ))
    now = time.time()
    return {
        "state": "ready", "text": text, "fingerprint": native_hash,
        "generated_at": now, "complete": True, "turn_complete": True, "body_complete": True,
        "source": "remote_opencode", "turn_identity": turn_identity, "timestamp": now, "confidence": "high",
        "remote_session_pid": remote_session_pid, "remote_sshd_pid": remote_sshd_pid,
        "opencode_pid": opencode_pid, "session_id": session_id, "user_id": user_id, "message_id": message_id,
        "native_length": len(text), "native_utf8_bytes": len(body), "native_sha256": native_hash,
        "local_ssh_pid": str(row["ssh_client_pid"]), "ssh_connection": connection,
        "ssh_local_socket": tuple(row.get("ssh_local_socket") or ()),
        "ssh_connection_mode": str(row.get("ssh_connection_mode") or "direct"),
    }


def _recover_ssh_grok_result(row: dict) -> dict:
    """Read the newest complete Grok export from this exact SSH socket."""

    from ..remote.collector import fetch_remote_grok_result

    endpoint = str(row.get("result_provider_endpoint") or "")
    connection = tuple(row.get("ssh_connection") or ())
    if (
        row.get("result_provider_type") != "remote_grok"
        or not endpoint
        or not str(row.get("ssh_client_pid") or "").isdecimal()
        or len(connection) != 4
    ):
        return _remote_grok_failure("REMOTE_PROVIDER_UNBOUND")
    requested = {
        "ssh_client_pid": str(row["ssh_client_pid"]),
        "client_ip": connection[0], "client_port": connection[1],
        "server_ip": connection[2], "server_port": connection[3],
    }
    result = fetch_remote_grok_result(endpoint, requested)
    if not isinstance(result, dict):
        return _remote_grok_failure("REMOTE_PROVIDER_UNAVAILABLE")
    status = str(result.get("status") or "UNKNOWN")
    if status != "ok":
        return _remote_grok_failure(status)
    if result.get("ssh_connection") != list(connection):
        return _remote_grok_failure("REMOTE_PROVIDER_UNBOUND")
    session_id = str(result.get("session_id") or "")
    remote_session_pid = str(result.get("remote_session_pid") or "")
    remote_sshd_pid = str(result.get("remote_sshd_pid") or "")
    grok_pid = str(result.get("grok_pid") or "")
    turn_number = result.get("turn_number")
    text = result.get("text")
    if (
        not re.fullmatch(r"[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}", session_id)
        or not all(value.isdecimal() for value in (remote_session_pid, remote_sshd_pid, grok_pid))
        or not isinstance(turn_number, int)
        or turn_number < 0
        or result.get("turn_complete") is not True
        or result.get("body_complete") is not True
        or not isinstance(text, str)
        or not text
    ):
        return _remote_grok_failure("REMOTE_RESULT_INCOMPLETE")
    body = text.encode("utf-8")
    native_hash = hashlib.sha256(body).hexdigest()
    if (
        len(text) != result.get("native_length")
        or len(body) != result.get("native_utf8_bytes")
        or native_hash != result.get("native_sha256")
    ):
        return _remote_grok_failure("REMOTE_RESULT_INCOMPLETE")
    turn_identity = ":".join((
        str(row.get("ssh_client_pid") or ""), *connection, remote_session_pid,
        remote_sshd_pid, grok_pid, session_id, str(turn_number), native_hash,
    ))
    now = time.time()
    return {
        "state": "ready", "text": text, "fingerprint": native_hash,
        "generated_at": now, "complete": True, "turn_complete": True, "body_complete": True,
        "source": "remote_grok", "turn_identity": turn_identity, "timestamp": now, "confidence": "high",
        "remote_session_pid": remote_session_pid, "remote_sshd_pid": remote_sshd_pid,
        "grok_pid": grok_pid, "session_id": session_id, "turn_number": turn_number,
        "native_length": len(text), "native_utf8_bytes": len(body), "native_sha256": native_hash,
        "local_ssh_pid": str(row["ssh_client_pid"]), "ssh_connection": connection,
        "ssh_local_socket": tuple(row.get("ssh_local_socket") or ()),
        "ssh_connection_mode": str(row.get("ssh_connection_mode") or "direct"),
    }


def _recover_registered_remote_result(row: dict) -> Optional[dict]:
    """Use a remote provider only with an exact, pane-bound registration."""

    provider_row = {
        "result_provider_type": row.get("result_provider_type") or "",
        "result_provider_endpoint": row.get("result_provider_endpoint") or "",
        "result_provider_session_id": row.get("result_provider_session_id") or "",
        "result_provider_pane_id": row.get("result_provider_pane_id") or "",
        "result_provider_pane_pid": row.get("result_provider_pane_pid") or "",
        "result_provider_liveness": row.get("result_provider_liveness") or "",
        "pane_title": row.get("pane_title") or "",
        "command": row.get("command") or "",
        "cmdline": row.get("cmdline") or "",
    }
    if not all((
        provider_row["result_provider_type"], provider_row["result_provider_endpoint"],
        provider_row["result_provider_session_id"], provider_row["result_provider_pane_id"],
        provider_row["result_provider_pane_pid"],
    )):
        return None
    return _recover_remote_result(provider_row)


def _recover_result(
    tower, pane_key: str, tracker, identity=None, *, source: str = "local_tmux", expected_pane_pid: str = "",
) -> Optional[dict]:
    """Newest finished body in bounded history, using the screen adapter.

    ``None`` means this pane's screen was not read. A working turn and a
    screen with no finished body return an empty payload instead of the
    text already stored from a short poll.
    """

    ok, _reason, pane_id = find_screen_target(
        getattr(tower, "session", "") or "",
        pane_key,
        getattr(tower, "own_pane_id", "") or "",
    )
    if not ok or not pane_id:
        return None
    session = getattr(tower, "session", "") or ""
    if expected_pane_pid and not _pane_matches_live_identity(session, pane_id, expected_pane_pid):
        return {"state": "none", "text": "", "complete": False, "reason_code": "UNKNOWN"}
    found = pane_title_and_command(pane_id)
    if found is None:
        return None
    title, command = found
    watermark = tracker.turn_start_watermark(pane_key, identity) if tracker is not None else None
    capture_lines = RECOVERY_LINES
    history_position = tmux_capture.pane_history_position(pane_id)
    watermark_scoped = False
    if watermark is not None:
        current = history_position
        start_size, start_limit = watermark
        if (
            current is not None
            and start_size < start_limit
            and current[1] == start_limit
            and start_size <= current[0] < current[1]
        ):
            capture_lines = min(RECOVERY_LINES, max(0, current[0] - start_size))
            watermark_scoped = True
    raw_lines = tmux_capture.capture_pane(
        pane_id, lines=capture_lines, join_wrapped=True,
    )
    if expected_pane_pid and not _pane_matches_live_identity(session, pane_id, expected_pane_pid):
        return {"state": "none", "text": "", "complete": False, "reason_code": "UNKNOWN"}
    lines, range_exceeded = _bounded_history_with_truncation(raw_lines)
    if not watermark_scoped and history_position is not None:
        range_exceeded = range_exceeded or history_position[0] > capture_lines
    if not lines:
        return {
            "state": "none", "text": "", "fingerprint": "", "generated_at": 0.0,
            "complete": False, "reason_code": "OUTPUT_RANGE_EXCEEDED" if range_exceeded else "UNKNOWN",
        }
    from ..adapters.base import PaneContext
    from ..detection.topology import resolve_runtime

    _name, _source, adapter = resolve_runtime(command, title, "", lines)
    ctx = PaneContext(
        title=title, command=command, lines=tuple(lines), pane_pid=pane_process_id(pane_id),
    )
    if adapter.classify(ctx).status == "WORKING":
        snap = tracker.observe(pane_key, "WORKING", None, identity=identity)
        return {
            "state": snap.state, "text": "", "fingerprint": snap.fingerprint,
            "generated_at": snap.generated_at, "complete": False, "reason_code": "TURN_NOT_COMPLETE",
        }
    candidate = adapter.extract_result(ctx)
    if candidate is None or not candidate.text:
        reason_code = "OUTPUT_RANGE_EXCEEDED" if range_exceeded else "UNKNOWN"
        if getattr(adapter, "name", "") == "Shell":
            reason_code = "AGENT_UNSUPPORTED"
        return {
            "state": "none", "text": "", "fingerprint": "", "generated_at": 0.0,
            "complete": False, "reason_code": reason_code,
        }
    snap = tracker.observe(
        pane_key, "IDLE", candidate, identity=identity, full_recovery=candidate.complete,
    )
    if not candidate.complete:
        # Keep the observation for UI state, but never let an older complete
        # tracker body turn this newer fragment into a copy payload.
        return {
            "state": snap.state, "text": "", "fingerprint": candidate.fingerprint,
            "generated_at": snap.generated_at, "complete": False,
            "turn_complete": candidate.turn_complete,
            "body_complete": candidate.body_complete,
            "reason_code": (
                "OUTPUT_RANGE_EXCEEDED" if range_exceeded else
                "SOURCE_PARTIAL" if candidate.turn_complete and not candidate.body_complete else
                "TURN_NOT_COMPLETE"
            ),
        }
    turn_identity = hashlib.sha256(
        json.dumps(identity or {}, sort_keys=True, default=str).encode("utf-8")
        + candidate.fingerprint.encode("ascii", "ignore")
    ).hexdigest()
    return _payload(
        snap, source=source, turn_identity=turn_identity, timestamp=time.time(),
        confidence=candidate.confidence or "unknown",
    )


def _bounded_history(lines: List[str]) -> List[str]:
    """Keep the newest lines, and stop if the bytes exceed the cap."""

    return _bounded_history_with_truncation(lines)[0]


def _bounded_history_with_truncation(lines: List[str]) -> Tuple[List[str], bool]:
    """Return bounded history and whether this function actually discarded rows."""

    kept = list(lines)
    truncated = len(kept) > RECOVERY_LINES
    kept = kept[-RECOVERY_LINES:]
    while kept and len("\n".join(kept).encode("utf-8", "replace")) > RECOVERY_BYTES:
        kept.pop(0)
        truncated = True
    return kept, truncated


_SAFE_ATTENTION_KEY = re.compile(r"^[0-9yn]$")
_SAFE_NAMED_KEYS = frozenset({"Enter", "Escape"})


def _attention_now(pane_id: str):
    """Fresh title, command, and screen for one pane. None if it is gone."""

    from ..adapters.base import PaneContext
    from ..detection.topology import resolve_runtime

    found = pane_title_and_command(pane_id)
    if found is None:
        return None
    title, command = found
    lines = tmux_capture.capture_pane(pane_id, lines=40)
    ctx = PaneContext(title=title, command=command, lines=tuple(lines))
    _name, _source, adapter = resolve_runtime(command, title, "", lines)
    return adapter, ctx


def send_attention_key(pane_id: str, key: str) -> bool:
    """Send one documented approval key. Not a prompt and not Enter."""

    if key not in _SAFE_NAMED_KEYS and not _SAFE_ATTENTION_KEY.match(key or ""):
        return False
    try:
        result = subprocess.run(
            ["tmux", "send-keys", "-t", pane_id, key],
            capture_output=True,
            timeout=SEND_TIMEOUT,
            check=False,
        )
        return result.returncode == 0
    except Exception:
        return False


def _get_ssh_opencode_result(tower, row: dict, target: ResultTarget, pane_key: str, expected_pane_pid):
    failure = _remote_opencode_failure("REMOTE_PROVIDER_UNBOUND")
    pane_id = target.tower_pane_id
    pane_pid = target.tower_pane_pid
    session = str(row.get("session") or "")
    host = target.transport_target
    interaction = row.get("interaction")
    if (
        row.get("status") != "IDLE" or row.get("attention") not in (None, "", "none")
        or (interaction and (not isinstance(interaction, dict) or interaction.get("type") not in (None, "none")))
    ):
        return True, "", _remote_opencode_failure("TURN_NOT_COMPLETE")
    if (
        pane_id != pane_key or not _LOCAL_PANE_KEY_RE.fullmatch(pane_id or "")
        or not pane_pid.isdecimal() or (expected_pane_pid and pane_pid != expected_pane_pid)
        or not session or not host
        or not _pane_matches_live_identity(session, pane_id, pane_pid)
    ):
        return True, "", failure
    live = pane_title_and_command(pane_id)
    if not live or live[1].casefold() not in {"ssh", "ssh.exe"}:
        return True, "", failure
    pane_title = live[0]

    from ..detection.sshdest import ssh_connection_for_pane

    connection = ssh_connection_for_pane(pane_pid, host)
    if connection is None:
        return True, "", failure
    target = replace(
        target, provider_type="remote_opencode", provider_endpoint=host, title=pane_title,
        ssh_client_pid=connection["ssh_client_pid"],
        ssh_connection=(
            connection["client_ip"], connection["client_port"],
            connection["server_ip"], connection["server_port"],
        ),
        ssh_local_socket=tuple(connection.get("local_socket") or ()),
        ssh_connection_mode=str(connection.get("connection_mode") or "direct"),
    )
    provider = _RecoveryProvider(
        lambda resolved: _recover_ssh_opencode_result(resolved.remote_provider_row())
    )
    candidate = provider.get_latest_complete_result(target)

    # The title is a stability check only; session identity comes from explicit --session argv.
    live_after = pane_title_and_command(pane_id)
    connection_after = ssh_connection_for_pane(pane_pid, host)
    if (
        not _pane_matches_live_identity(session, pane_id, pane_pid)
        or live_after != live or not connection_after
        or connection_after.get("ssh_client_pid") != connection["ssh_client_pid"]
        or tuple(connection_after.get(key) for key in ("client_ip", "client_port", "server_ip", "server_port")) != target.ssh_connection
        or tuple(connection_after.get("local_socket") or ()) != target.ssh_local_socket
    ):
        return True, "", _remote_opencode_failure("REMOTE_PROVIDER_UNBOUND")
    if candidate is None:
        return True, "", provider.observation or _remote_opencode_failure("REMOTE_RESULT_INCOMPLETE")
    payload = _candidate_payload(candidate, provider.observation.get("state") or "ready")
    payload.update({key: value for key, value in provider.observation.items() if key.startswith("remote_") or key in {
        "opencode_pid", "session_id", "user_id", "message_id", "native_length", "native_utf8_bytes",
        "native_sha256", "local_ssh_pid", "ssh_connection", "ssh_local_socket", "ssh_connection_mode",
    }})
    return True, "", payload


def respond_interaction(tower, pane_key: str, key: str) -> Tuple[bool, str]:
    """Send one key that the current interaction still lists as safe."""

    ok, reason, pane_id = find_prompt_target(tower, pane_key, None, None)
    if not ok or not pane_id:
        return False, reason or "not_found"
    fresh = _attention_now(pane_id)
    if fresh is None:
        return False, "stale"
    adapter, ctx = fresh
    if adapter.detect_execution(ctx) == "WORKING":
        return False, "working"
    interaction = adapter.detect_interaction(ctx)
    if interaction is None or interaction.verified_key(key) is None:
        return False, "interaction_unknown"
    if not send_attention_key(pane_id, key):
        return False, "send_failed"
    return True, ""


def respond_interaction_text(tower, pane_key: str, text: str) -> SubmitResult:
    """Answer the current text question. Not a new-turn prompt."""

    if not isinstance(text, str) or not text.strip():
        return SubmitResult(False, reason="missing_text")
    ok, reason, pane_id = find_prompt_target(tower, pane_key, None, None)
    if not ok or not pane_id:
        return SubmitResult(False, reason=reason or "not_found")
    fresh = _attention_now(pane_id)
    if fresh is None:
        return SubmitResult(False, reason="stale")
    adapter, ctx = fresh
    interaction = adapter.detect_interaction(ctx)
    if interaction is None or not interaction.input_allowed:
        return SubmitResult(False, reason="not_text", pane_id=pane_id)
    if not send_prompt_to_pane(pane_id, text):
        return SubmitResult(False, reason="send_failed", pane_id=pane_id)
    return SubmitResult(True, submitted=False, reason="sent", pane_id=pane_id)


def respond_attention(tower, pane_key: str, action: str) -> Tuple[bool, str]:
    """Approve or reject only when the adapter names the key on a fresh screen."""

    if action not in ("approve", "reject"):
        return False, "bad_request"
    ok, reason, pane_id = find_prompt_target(tower, pane_key, None, None)
    if not ok or not pane_id:
        return False, reason or "not_found"
    fresh = _attention_now(pane_id)
    if fresh is None:
        return False, "stale"
    adapter, ctx = fresh
    if adapter.detect_attention(ctx) != "approval_required":
        return False, "not_approval"
    key = adapter.approve(ctx) if action == "approve" else adapter.reject(ctx)
    if not key:
        return False, "approval_unknown"
    if not send_attention_key(pane_id, key):
        return False, "send_failed"
    return True, ""


def close_warning(status: str) -> str:
    """Confirmation copy. The default choice is always cancel."""

    from ..i18n import t

    return {
        "WORKING": t("control.close_working"),
        "WAITING": t("control.close_waiting"),
        "DEAD": t("control.close_dead"),
    }.get(status or "", t("control.close_idle"))


def close_notice_lines(status: str, attention: str = "none") -> List[str]:
    """Lines shown before a pane close. Working states the job will die.

    Approval and input are named even when execution is idle, because
    those widgets are not the execution status.
    """

    from ..i18n import t

    lines = [close_warning(status)]
    if attention == "approval_required":
        lines.append(t("control.close_approval"))
    elif attention == "input_required":
        lines.append(t("control.close_input"))
    return lines


def close_choices() -> List[Tuple[str, str]]:
    """Cancel is first so Enter without moving does not kill the pane."""

    from ..i18n import t

    return [("cancel", t("control.cancel")), ("close", t("control.close_yes"))]


def close_pane(session: str, pane_key: str, own_pane_id: str = "") -> Tuple[bool, str]:
    """``tmux kill-pane -t <pane_id>`` for one pane in the bound session.

    Tower's own pane is refused. Remote keys are refused. A stale id
    is refused. No window name, no session delete, no ``shell=True``.
    """

    if own_pane_id and pane_key == own_pane_id:
        return False, "tower_pane"
    ok, reason, pane_id = find_screen_target(session, pane_key, own_pane_id)
    if not ok or not pane_id:
        return False, reason or "not_found"
    living = [
        line
        for line in tmux_capture.run_tmux(
            ["list-panes", "-s", "-t", session, "-F", "#{pane_id}"]
        ).split("\n")
        if line.startswith("%")
    ]
    # The last pane in the session is the session. Do not delete it.
    if len(living) <= 1:
        return False, "last_pane"
    try:
        result = subprocess.run(
            ["tmux", "kill-pane", "-t", pane_id],
            capture_output=True,
            timeout=SEND_TIMEOUT,
            check=False,
        )
    except Exception:
        return False, "close_failed"
    if result.returncode != 0:
        return False, "close_failed"
    return True, ""
