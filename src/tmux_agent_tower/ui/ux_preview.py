"""Isolated, fixture-only Control Room preview for Windows Terminal/WSL."""

from __future__ import annotations

import curses
import hashlib
import locale
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

EXPECTED_BASE_SHA = "c6c6e8837eb677b3306a4dd0a1e6cbdccf962d3b"
_SOURCE_ROOT = Path(__file__).resolve().parents[3]


class _MemoryStore:
    """Small in-memory store with the read surface used by navigation."""

    def __init__(self, items=()):
        self.items = [dict(item) for item in items]
        self.toggle_calls = 0

    def all(self):
        return [dict(item) for item in self.items]

    def hidden_refs(self):
        return set()

    def memberships(self):
        return {}

    def toggle_folder(self, item_id):
        self.toggle_calls += 1
        for item in self.items:
            if item.get("folder_id") == item_id:
                item["collapsed"] = not bool(item.get("collapsed"))

    def toggle_project(self, item_id):
        self.toggle_calls += 1
        for item in self.items:
            if item.get("project_id") == item_id:
                item["collapsed"] = not bool(item.get("collapsed"))


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _pane(*, host: str, session: str, session_id: str, window_id: str,
          window_index: int, pane_number: int, pane_index: int, window_ref: str,
          display_name: str, agent: str, transport: str = "local",
          transport_target: str = "", remote: bool = False, status: str = "IDLE") -> dict:
    pane_id = f"%{pane_number}"
    pid = str(80000 + pane_number)
    target_id = _digest("\0".join((host, session_id, pane_id, pid)))
    return {
        "kind": "pane",
        "key": pane_id,
        "target_id": target_id,
        "tmux_host": host,
        "execution_host": "ASUS (가상 예시)" if transport == "ssh" else host,
        "session": session,
        "session_id": session_id,
        "window_id": window_id,
        "window_index": str(window_index),
        "window_name": f"Window {window_index + 1}",
        "pane_id": pane_id,
        "pane_pid": pid,
        "pane_index": pane_index,
        "window_ref": window_ref,
        "display_name": display_name,
        "task_name": display_name,
        "agent": agent,
        "status": status,
        "attention": "none",
        "interaction": {},
        "result_state": "none",
        "transport": transport,
        "transport_target": transport_target,
        "remote": remote,
        "live_lines": [
            "검토를 시작했습니다.",
            "",
            "1. 요청 범위를 확인합니다.",
            "2. 관련 변경을 비교합니다.",
            "3. 결과를 정리합니다.",
            "",
            "완료 전 검증을 진행합니다.",
        ],
    }


def make_preview_tower():
    """Build only synthetic rows and stores; never reads Tower user state."""

    rows = []
    assets = []
    task_refs = []
    for window_index in range(9):
        window_id = f"@{100 + window_index}"
        window_ref = _digest(f"preview-mainpc\0$1\0{window_id}")
        task_refs.append(window_ref)
        assets.append({
            "window_ref": window_ref,
            "display_name": f"예시 Window {window_index + 1}",
            "window_name": f"Window {window_index + 1}",
            "window_index": str(window_index),
            "tmux_host": "preview-mainpc",
            "execution_host": "가상 MainPC",
            "host": "가상 MainPC",
            "remote": False,
        })
        pane_count = 6 if window_index == 0 else 1
        for pane_index in range(pane_count):
            pane_number = 101 + window_index * 10 + pane_index
            rows.append(_pane(
                host="preview-mainpc", session="preview-session", session_id="$1",
                window_id=window_id, window_index=window_index,
                pane_number=pane_number, pane_index=pane_index, window_ref=window_ref,
                display_name=(f"작업 {window_index + 1} · Pane {pane_index + 1}"),
                agent=("Codex" if pane_index % 3 == 0 else "Claude" if pane_index % 3 == 1 else "OpenCode"),
                status=("WORKING" if pane_index == 0 else "WAITING" if pane_index == 1 else "IDLE"),
            ))

    ssh_ref = _digest("preview-mainpc\0$2\0@200")
    peer_ref = _digest("preview-asus\0$9\0@200")
    assets.extend([
        {
            "window_ref": ssh_ref, "display_name": "SSH Client 예시",
            "window_name": "SSH Client 예시", "window_index": "0",
            "tmux_host": "preview-mainpc", "execution_host": "가상 ASUS 대상",
            "host": "가상 ASUS 대상", "remote": False,
        },
        {
            "window_ref": peer_ref, "display_name": "ASUS Peer 예시",
            "window_name": "ASUS Peer 예시", "window_index": "0",
            "tmux_host": "preview-asus", "execution_host": "가상 ASUS",
            "host": "가상 ASUS", "remote": True,
        },
    ])
    rows.extend([
        _pane(
            host="preview-mainpc", session="preview-ssh-session", session_id="$2",
            window_id="@200", window_index=0, pane_number=101, pane_index=0,
            window_ref=ssh_ref, display_name="가상 SSH Client Pane", agent="Claude",
            transport="ssh", transport_target="asus-preview-only", status="UNKNOWN",
        ),
        _pane(
            host="preview-asus", session="preview-peer-session", session_id="$9",
            window_id="@200", window_index=0, pane_number=101, pane_index=0,
            window_ref=peer_ref, display_name="가상 ASUS Peer Pane", agent="OpenCode",
            transport="remote_tmux", transport_target="asus-preview-only",
            remote=True, status="UNKNOWN",
        ),
    ])

    folders = _MemoryStore([
        {"folder_id": "preview-work", "display_name": "예시 작업 폴더", "window_refs": task_refs},
        {"folder_id": "preview-links", "display_name": "연결 구분 예시",
         "window_refs": [ssh_ref, peer_ref]},
    ])
    projects = _MemoryStore()
    # WorkGroupStore's existing target-ID contract is used by the fixture:
    # logical saved work may collect panes from different tmux Windows.
    rows_by_ref = {}
    for row in rows:
        rows_by_ref.setdefault(row["window_ref"], []).append(row)
    local_row_groups = [rows_by_ref[ref] for ref in task_refs]
    work_groups = _MemoryStore([
        {"group_id": "ai-team", "display_name": "AI 개발팀",
         "member_target_ids": [local_row_groups[0][0]["target_id"], local_row_groups[1][0]["target_id"]],
         "member_order": [local_row_groups[0][0]["target_id"], local_row_groups[1][0]["target_id"]]},
        {"group_id": "code-review", "display_name": "코드 리뷰",
         "member_target_ids": [local_row_groups[2][0]["target_id"], local_row_groups[3][0]["target_id"]],
         "member_order": [local_row_groups[2][0]["target_id"], local_row_groups[3][0]["target_id"]]},
        {"group_id": "server-admin", "display_name": "서버 관리",
         "member_target_ids": [local_row_groups[4][0]["target_id"], rows_by_ref[ssh_ref][0]["target_id"]],
         "member_order": [local_row_groups[4][0]["target_id"], rows_by_ref[ssh_ref][0]["target_id"]]},
    ])
    return SimpleNamespace(
        rows=rows,
        visible_rows=rows,
        selected=0,
        window_assets=assets,
        folders=folders,
        projects=projects,
        work_groups=work_groups,
        control_screens=None,
        own_pane_id="%999",
        local_host="preview-mainpc",
        remote_hosts=[],
        notice="",
        read_only_preview=True,
    )


def _preview(stdscr) -> None:
    from . import control_room, tower

    tower.setup_colors()
    control_room.open_control_room(stdscr, make_preview_tower(), read_only_preview=True)


def _source_head() -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(_SOURCE_ROOT), "rev-parse", "HEAD"],
            check=False, capture_output=True, text=True, timeout=3,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def main() -> int:
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print("Tower UX Preview는 Windows Terminal/WSL의 대화형 터미널에서 실행해야 합니다.", file=sys.stderr)
        return 2
    head = _source_head()
    if head != EXPECTED_BASE_SHA:
        print(
            f"Preview source mismatch: expected {EXPECTED_BASE_SHA}, found {head or 'unavailable'}.",
            file=sys.stderr,
        )
        return 2
    try:
        locale.setlocale(locale.LC_ALL, "")
    except locale.Error:
        pass
    os.environ.setdefault("ESCDELAY", "25")
    try:
        curses.wrapper(_preview)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
