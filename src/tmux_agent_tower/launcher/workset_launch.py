"""Preflight and launch a Saved Work or work template as one logical group."""

from __future__ import annotations

import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path

from ..state.bindings import ProjectBindingStore
from ..state.worksets import Workset
from ..tmux import structure
from .browse import validate_local_path, validate_remote_path
from .config import resolve_agent_command
from .spawn import SpawnResult, SpawnTarget, spawn_local, spawn_remote

TMUX_LAYOUTS = {
    "focus": "tiled",
    "split-2": "even-horizontal",
    "grid-4": "tiled",
    "main-plus-side": "main-horizontal",
}


class WorksetLaunchError(ValueError):
    """Preflight or launch failed without silently changing the plan."""


@dataclass(frozen=True)
class WorksetLaunchResult:
    group_id: str
    group_name: str
    execution_host: str
    members: tuple[dict, ...]


def _target_host(tower, requested: str):
    if requested in ("", "auto", tower.local_host):
        return tower.local_host, tower.local_host, False
    host = next((row for row in tower.remote_hosts if requested in (row.get("alias"), row.get("name"))), None)
    if host is None or not host.get("alias"):
        raise WorksetLaunchError("저장된 실행 위치를 찾을 수 없습니다. 실행 위치를 다시 선택하세요.")
    return str(host["alias"]), str(host.get("name") or host["alias"]), True


def _rollback(tower, results: list[SpawnResult], remote_alias: str = "") -> bool:
    window_ids = {row.window_id for row in results if row.window_id}
    if len(window_ids) != 1:
        return False
    window_id = next(iter(window_ids))
    if not structure.valid_window_id(window_id):
        return False
    if not remote_alias:
        return structure.kill_window(tower.session, window_id, tower.own_pane_id).ok
    # Fixed cleanup command; the host comes from configured remote hosts and the id is validated.
    try:
        result = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", remote_alias, f"tmux kill-window -t {window_id}"],
            capture_output=True, text=True, timeout=6, check=False,
        )
        return result.returncode == 0
    except Exception:
        return False


def _rollback_batches(tower, batches) -> bool:
    ok = True
    for results, alias in reversed(batches):
        if not _rollback(tower, results, alias):
            ok = False
    return ok


def _rollback_message(tower, batches, message: str) -> str:
    notice = ("이번 실행을 정리했습니다." if _rollback_batches(tower, batches)
              else "정리 여부를 확인할 수 없습니다. 새 작업이 남아 있을 수 있습니다.")
    return f"{message} {notice}"


def launch_workset(
    tower,
    workset: Workset,
    state_dir: Path,
    *,
    agents_cfg: dict,
    project_override: tuple[str, str] | None = None,
    agent_overrides: dict[str, str] | None = None,
    execution_target: str | None = None,
    execution_targets: dict[str, str] | None = None,
    group_name: str | None = None,
) -> WorksetLaunchResult:
    if workset.kind not in {"saved_work", "template"} or not workset.members:
        raise WorksetLaunchError("저장된 작업 구성을 확인할 수 없습니다.")
    overrides = {} if agent_overrides is None else agent_overrides
    location_overrides = {} if execution_targets is None else execution_targets
    if (type(overrides) is not dict or type(location_overrides) is not dict
            or any(not isinstance(key, str) or not isinstance(value, str) for key, value in overrides.items())
            or any(not isinstance(key, str) or not isinstance(value, str) for key, value in location_overrides.items())):
        raise WorksetLaunchError("구성원 선택이 올바르지 않습니다.")
    member_ids = {row.member_id for row in workset.members}
    if set(overrides) - member_ids or set(location_overrides) - member_ids:
        raise WorksetLaunchError("구성원 선택이 올바르지 않습니다.")
    plans = []
    for member_id in workset.member_order:
        member = next(row for row in workset.members if row.member_id == member_id)
        requested_host = location_overrides.get(member_id) or execution_target or member.execution_target
        host_key, host_label, is_remote = _target_host(tower, requested_host)
        path = project_override[1] if project_override else member.project_path
        project_name = project_override[0] if project_override else member.project_name
        if not path:
            raise WorksetLaunchError("프로젝트 경로를 선택하세요.")
        checked = validate_remote_path(host_key, path) if is_remote else validate_local_path(path)
        if not checked.ok or checked.entry is None:
            raise WorksetLaunchError("프로젝트 경로를 확인할 수 없습니다.")
        agent = overrides.get(member_id, member.agent)
        command = agents_cfg.get(agent)
        if not command or (not is_remote and resolve_agent_command(agent, agents_cfg) is None):
            raise WorksetLaunchError(f"{agent}를 사용할 수 없습니다. Agent를 바꾸고 다시 시작하세요.")
        project_name = project_name or checked.entry.name
        plans.append((member, checked.entry, agent, host_key, host_label, is_remote,
                      SpawnTarget(checked.entry.path, project_name, agent)))

    physical_layout = TMUX_LAYOUTS.get(workset.layout)
    if not physical_layout:
        raise WorksetLaunchError("작업 배치를 확인할 수 없습니다.")
    actual_group_name = (group_name or workset.name).strip()
    if not actual_group_name:
        raise WorksetLaunchError("작업 묶음 이름을 확인할 수 없습니다.")

    tower.work_groups.all()  # Fail before creating panes if stored group data cannot be read.
    if getattr(tower.work_groups, "_corrupt", False):
        raise WorksetLaunchError("작업 묶음 저장 파일을 읽을 수 없어 시작을 취소했습니다.")
    # One physical launch window per execution host. Membership and order
    # remain one logical Work Group even when its members run on different hosts.
    batches = {}
    for plan in plans:
        batches.setdefault(plan[3], []).append(plan)
    launched = []
    created_batches = []
    for host_key, batch in batches.items():
        host_label, is_remote = batch[0][4], batch[0][5]
        targets = [plan[6] for plan in batch]
        results = spawn_remote(
            host_key, actual_group_name, targets, agents_cfg,
            layout=physical_layout, transactional=True,
        ) if is_remote else spawn_local(
            tower.session, targets, agents_cfg, layout=physical_layout,
            bindings=ProjectBindingStore(state_dir / "project-bindings.json"),
            overrides=tower.overrides, transactional=True,
        )
        created_batches.append((results, host_key if is_remote else ""))
        if len(results) != len(batch) or any(not row.ok for row in results):
            detail = next((row.detail for row in results if not row.ok), "작업을 시작하지 못했습니다.")
            raise WorksetLaunchError(_rollback_message(tower, created_batches, detail))
        launched.extend(zip(batch, results))
        if is_remote:
            tower._remote_cache.pop(host_key, None)
            tower._remote_last_fetch.pop(host_key, None)

    tower.load()
    live_rows = []
    row_by_member = {}
    for plan, result in launched:
        member = plan[0]
        is_remote = plan[5]
        host_key = plan[3]
        # pane_id/window_id identify the newly created tmux resource. The pane
        # PID can change after the start command replaces its shell, so use the
        # live row's PID for target identity and overrides below.
        row = next((item for item in tower.rows
                    if item.get("pane_id") == result.pane_id
                    and (not result.window_id or item.get("window_id") == result.window_id)
                    and bool(item.get("remote")) == is_remote
                    and (item.get("result_provider_host") == host_key if is_remote
                         else item.get("session") == result.session)), None)
        if row is None:
            raise WorksetLaunchError(_rollback_message(
                tower, created_batches, "시작된 작업을 목록에서 확인할 수 없습니다."
            ))
        row_by_member[member.member_id] = row
    live_rows = [row_by_member[member.member_id] for member, *_ in plans]
    checked_members = [(plan[0], plan[1], plan[2]) for plan in plans]

    for row, (member, entry, agent) in zip(live_rows, checked_members):
        key, session, pane_pid = row.get("key") or "", str(row.get("session") or ""), str(row.get("pane_pid") or "")
        tower.overrides.drop_if_stale(key, session, pane_pid)
        tower.overrides.set_agent(key, agent, session, pane_pid)
        tower.overrides.set_project(key, entry.name, session, pane_pid)
        if member.role:
            tower.overrides.set_role(key, member.role, session, pane_pid)
        else:
            tower.overrides.clear_field(key, "role", session, pane_pid)

    tower.load()
    refreshed_rows = [next((item for item in tower.rows if item.get("key") == row.get("key")), None) for row in live_rows]
    if any(row is None for row in refreshed_rows):
        raise WorksetLaunchError(_rollback_message(
            tower, created_batches, "작업 구성이 바뀌어 시작한 작업을 확인할 수 없습니다."
        ))
    live_rows = refreshed_rows
    preserve_name = {}
    for row, (member, _entry, _agent) in zip(live_rows, checked_members):
        if member.display_name and (member.name_origin == "user" or project_override is None):
            tower.overrides.set_task_name(
                row["key"], member.display_name, str(row.get("session") or ""),
                str(row.get("pane_pid") or ""), origin=member.name_origin,
            )
            preserve_name[str(row.get("key"))] = member.display_name

    tower.load()
    refreshed_rows = [next((item for item in tower.rows if item.get("key") == row.get("key")), None) for row in live_rows]
    if any(row is None for row in refreshed_rows):
        raise WorksetLaunchError(_rollback_message(
            tower, created_batches, "작업 이름과 구성을 확인할 수 없습니다."
        ))
    live_rows = refreshed_rows
    for row, (member, entry, agent) in zip(live_rows, checked_members):
        if (row.get("agent") != agent or row.get("project") != entry.name
                or (member.role and row.get("role") != member.role)
                or (str(row.get("key")) in preserve_name and row.get("display_name") != preserve_name[str(row.get("key"))])):
            raise WorksetLaunchError(_rollback_message(
                tower, created_batches, "작업 이름, 역할 또는 Agent를 저장하지 못했습니다."
            ))
    target_ids = [str(row_by_member[member_id]["target_id"]) for member_id in workset.member_order]
    labels = {target: row.get("display_name") or row.get("task_name") or "작업" for target, row in zip(target_ids, live_rows)}
    names = {entry.name for _member, entry, _agent in checked_members}
    paths = {entry.path for _member, entry, _agent in checked_members}
    binding = {"name": next(iter(names))} if len(names) == 1 else None
    if binding is not None and len(paths) == 1:
        binding["path"] = next(iter(paths))
    member_by_id = {member.member_id: member for member in workset.members}
    layout_slots = {
        str(row_by_member[member_id]["target_id"]): member_by_id[member_id].layout_slot
        for member_id in workset.member_order
    }
    try:
        group = tower.work_groups.create(
            actual_group_name, target_ids, project_binding=binding, labels=labels,
            layout=workset.layout, layout_slots=layout_slots,
        )
    except (OSError, ValueError, KeyError) as exc:
        raise WorksetLaunchError(_rollback_message(
            tower, created_batches, "작업 묶음을 저장하지 못했습니다."
        )) from exc
    launch_id = uuid.uuid4().hex
    resources = {
        str(row["target_id"]): {
            "session": str(row.get("session") or ""),
            "pane_id": str(row.get("pane_id") or ""),
            "pane_pid": str(row.get("pane_pid") or ""),
            "window_id": str(row.get("window_id") or ""),
            "tmux_host": str(row.get("tmux_host") or tower.local_host),
            "launch_id": launch_id,
        }
        for row in live_rows
        if not row.get("remote") and row.get("pane_id") and row.get("session")
    }
    try:
        if resources:
            tower.work_groups.register_managed_resources(resources)
    except (OSError, ValueError, KeyError) as exc:
        tower.work_groups.dissolve(group["group_id"])
        raise WorksetLaunchError(_rollback_message(
            tower, created_batches, "시작한 작업의 안전 정보를 저장하지 못했습니다."
        )) from exc
    tower.load()
    members = tuple(dict(row) for row in live_rows)
    execution_hosts = tuple(dict.fromkeys(plan[4] for plan in plans))
    return WorksetLaunchResult(group["group_id"], actual_group_name, ", ".join(execution_hosts), members)
