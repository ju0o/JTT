"""Curses TUI for the user's work list and advanced terminal structure.

The default view is Folder → work screen → task. Enter opens the persistent
Conversation Surface; the host/window/pane navigator is an explicit advanced
view. Quitting this TUI leaves the optional phone service running.
"""

from __future__ import annotations

import curses
import copy
import hashlib
import os
import time
from threading import Event, Thread
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from ..adapters import adapter_named, resolve_adapter
from ..detection.identity import identify_agent, prefer_override
from ..detection.topology import classify_transport, peer_topology, resolve_runtime
from ..hostreg import HostRegistry, openssh_hostname
from ..adapters.base import PaneContext, ResultCandidate
from ..clipboard_dest import AUTO as COPY_AUTO, LOCAL_HOST, CURRENT_TERMINAL, TMUX_BUFFER
from ..clipboard_dest import load_preference, observe as observe_access_client
from ..detection.result import ResultTracker, pane_result_identity, shared_result_state_path
from ..detection.status import StatusEngine, STATUS_DEAD
from ..i18n import t
from ..launcher.config import AGENT_LAUNCH_ORDER, load_config
from .. import notify
from ..remote.collector import fetch_remote, HOST_STATUS_ONLINE
from ..state.bindings import ProjectBindingStore
from ..state.folders import FolderStore, window_ref
from ..state.overrides import ROLE_IDS, OverrideStore
from ..state.work_groups import WorkGroupStore
from ..detection.project import git_project_name
from ..state.visits import VisitStore
from ..tmux import capture as tmux_capture
from ..tmux import discovery
from ..tmux import registration
from ..control.actions import enter_intent, update_identity
from . import render
from . import folder_tree
from .perf_trace import draw as trace_draw, event, refresh as trace_refresh
from .widgets import is_ctrl_c, is_enter, is_escape, matches_letter, prompt_text, read_key, run_list_picker, safe_add

REFRESH_SECONDS = 2.0
REMOTE_REFRESH_SECONDS = 6.0
PANE_TITLE_SYNC_INTERVAL_SECONDS = 15.0
PANE_TITLE_SYNC_CONFLICT_WINDOW_SECONDS = 120.0
PANE_TITLE_SYNC_MAX_CONFLICTS = 3
CAPTURE_LINES = 30
USER_WORK_VIEW = "user_work"
TERMINAL_STRUCTURE_VIEW = "terminal_structure"


def _target_id(host: str, session_id: str, pane_id: str, pane_pid: str) -> str:
    identity = "\0".join((host or "", session_id or "", pane_id or "", pane_pid or ""))
    return hashlib.sha256(identity.encode("utf-8", "replace")).hexdigest()

STATE_DIR = Path.home() / ".cache" / "tmux-agent-tower"
CONFIG_DIR = Path.home() / ".config" / "tmux-agent-tower"
HOST_FILE = CONFIG_DIR / "host"
REMOTE_HOSTS_FILE = CONFIG_DIR / "remote-hosts.txt"

STATUS_SYMBOL = render.STATUS_SYMBOL
STATUS_ORDER = render.STATUS_ORDER


def _disable_xon_flow_control(fd: int = 0) -> None:
    """Let Ctrl+S reach the Result View shortcut while curses is active."""

    try:
        import termios
    except ImportError:
        return
    try:
        attrs = termios.tcgetattr(fd)
        if attrs[0] & termios.IXON:
            attrs[0] &= ~termios.IXON
            termios.tcsetattr(fd, termios.TCSANOW, attrs)
    except (OSError, ValueError, termios.error):
        return


def _raw_hostname() -> str:
    """The actual OS hostname tmux/a shell would default a pane's title to
    -- deliberately NOT ``local_host_label()``'s (possibly user-overridden)
    display name. A pane titled after the raw hostname carries no real
    information ("just the default"); one that happens to share text with
    a *chosen* display label like "laptop" would not mean the same thing.
    """

    return os.uname().nodename


def local_host_label() -> str:
    try:
        value = HOST_FILE.read_text(encoding="utf-8").strip()
        if value:
            return value
    except Exception:
        pass
    return os.uname().nodename


def load_remote_hosts() -> List[Dict[str, str]]:
    """Load legacy SSH destinations as generic execution environments."""

    from ..state.environments import load_profiles

    try:
        return load_profiles(REMOTE_HOSTS_FILE)
    except (OSError, UnicodeError):
        return []


def location_lines(row: Dict) -> List[str]:
    """Execution host, the tmux object that receives input, and the link."""

    execution = row.get("execution_host") or row.get("host") or "-"
    tmux_host = row.get("tmux_host") or execution
    session = row.get("session") or "-"
    window = row.get("window_index")
    window_text = "-" if window is None else str(window)
    pane = row.get("pane_id") or row.get("key") or "-"
    lines = [
        f'{t("detail.execution_host")}  {execution}',
        f'{t("detail.tmux_place")}  {tmux_host} → Session {session} → Window {window_text} → Pane {pane}',
    ]
    if row.get("transport") == "ssh":
        target = row.get("transport_target") or execution
        lines.append(f'{t("detail.transport")}  SSH → {target}')
    elif row.get("remote"):
        lines.append(t("detail.peer_readonly"))
    else:
        lines.append(f'{t("detail.transport")}  {t("detail.transport_local")}')
    return lines


def _place_label(topology: Dict, pane: Dict) -> str:
    """Where an SSH client pane sits, without pretending it is that host's tmux."""

    if topology.get("transport") != "ssh":
        return ""
    index = pane.get("window_index")
    return t("nav.via_tmux").format(
        tmux_host=topology.get("tmux_host") or "",
        index="" if index is None else index,
        pane=pane.get("pane_id") or "",
    )


def _interaction_payload(adapter, ctx, attention: str):
    """Same object the phone status payload copies. None when there is nothing to ask."""

    if attention == "none":
        return None
    found = adapter.detect_interaction(ctx)
    if found is None:
        from ..detection.interaction import unknown

        found = unknown(adapter.extract_attention_prompt(ctx), f"{adapter.name.lower()}-unmapped")
    return found.as_dict()


def _window_group(row: Dict) -> Tuple:
    """Physical tmux window. Names, execution host, and pane id are not part of it.

    An SSH client pane stays in the window that contains it. Execution
    host only decides which heading the row is drawn under. A peer tmux
    snapshot uses that server's own identity, so the same window id on
    two machines is not one fold.
    """

    tmux_host = str(row.get("tmux_host") or "")
    session = str(row.get("session") or "")
    window_id = str(row.get("window_id") or "")
    index = str(row.get("window_index") if row.get("window_index") is not None else "")
    if row.get("remote"):
        # A peer row may only have the display host. That is still the
        # other tmux server, not this pane's execution host.
        peer = tmux_host or str(row.get("host") or "")
        return ("remote", peer, session, window_id or index or "?")
    if window_id:
        return ("local", tmux_host, window_id)
    return ("local", tmux_host, session, index)


def _fold_key(sample: Dict) -> str:
    """Key stored when a window is collapsed, and the key used to expand it.

    A tmux host plus session plus window id stays put when the window is
    renamed or the execution host changes. Rows built before a tmux host
    was recorded keep the bare window id so one server's ``@1`` is stable.
    """

    session = str(sample.get("session") or "")
    window_id = str(sample.get("window_id") or "")
    index = str(sample.get("window_index") if sample.get("window_index") is not None else "")
    tmux_host = str(sample.get("tmux_host") or "")
    ident = window_id or index
    if sample.get("remote"):
        host = tmux_host or str(sample.get("host") or "")
        return f"{host}:{session}:{ident}"
    if tmux_host:
        return f"{tmux_host}:{session}:{ident}"
    if window_id:
        return window_id
    return f"win:{session}:{index}"


def _assign_tree_guides(rows: List[Dict]) -> None:
    """Box-drawing prefixes. Width is measured with ``display_width``
    at draw time, so a Korean name does not shift the badge.
    """

    grouped: List[List[Dict]] = []
    for row in rows:
        host = str(row.get("host") or "")
        if not grouped or str(grouped[-1][0].get("host") or "") != host:
            grouped.append([])
        grouped[-1].append(row)
    for group in grouped:
        windows = [row for row in group if row.get("kind") == "window"]
        for index, window in enumerate(windows):
            last = index == len(windows) - 1
            if window.get("collapsed"):
                window["guide"] = "└▸ " if last else "├▸ "
            else:
                window["guide"] = "└─ " if last else "├─ "
            panes = [row for row in group if row.get("kind") == "pane" and row.get("tree_window") == window.get("key")]
            stem = "   " if last else "│  "
            for pane_index, pane in enumerate(panes):
                end = pane_index == len(panes) - 1
                pane["guide"] = stem + ("└─ " if end else "├─ ")


def navigator_rows(panes: List[Dict], collapsed: Optional[set] = None) -> List[Dict]:
    """Host, then a selectable window row, then that window's panes.

    The window row's key is the tmux window id on this server, and the
    peer server plus session plus id for a remote window. Execution host
    and pane id are not part of it. A renamed window stays the same row.
    Enter on it opens Window Control and does not move the client.
    ``collapsed`` only hides child rows.
    """

    closed = collapsed or set()
    order: List[Tuple] = []
    buckets: Dict[Tuple, List[Dict]] = {}
    for row in panes:
        if row.get("kind") == "zero" or not row.get("pane_id"):
            continue
        key = _window_group(row)
        buckets.setdefault(key, []).append(row)
        if key not in order:
            order.append(key)

    out: List[Dict] = []
    for key in order:
        members = buckets[key]
        sample = members[0]
        session = str(sample.get("session") or "")
        index = str(sample.get("window_index") if sample.get("window_index") is not None else "")
        name = str(sample.get("window_name") or "")
        window_id = str(sample.get("window_id") or "")
        remote = bool(sample.get("remote"))
        work_pane = next((pane for pane in members if pane.get("pane_active")), sample)
        group_name = work_pane.get("project") or t("project.no_name")
        row_key = _fold_key(sample)
        if remote:
            label = t("nav.peer_tmux").format(index=index, name=group_name)
        else:
            label = t("nav.window_row").format(index=index, name=group_name)
        group = f"win:{sample.get('host') or ''}:{session}:{window_id or index}"
        summary = render.window_summary(members)
        hidden = row_key in closed
        out.append(
            {
                "kind": "window",
                "key": row_key,
                "window_id": window_id,
                "rename_pane_id": work_pane.get("pane_id"),
                "project": label,
                "agent": "",
                "summary": summary,
                "status": "IDLE",
                "host": sample.get("host") or "",
                "session": session,
                "window_index": index,
                "window_name": name,
                "pane_count": len(members),
                "nav_group": group,
                "nav_group_label": label,
                "remote": remote,
                "collapsed": hidden,
            }
        )
        if hidden:
            continue
        for row in members:
            item = dict(row)
            item["kind"] = "pane"
            item["nav_group"] = group
            item["nav_group_label"] = label
            item["tree_window"] = row_key
            out.append(item)
    _assign_tree_guides(out)
    return out


def zero_state_rows() -> List[Dict]:
    """What to show when Tower has no working pane yet."""

    title = t("zero.title")
    actions = (
        ("task", t("zero.task")),
        ("saved", t("zero.saved")),
        ("template", t("zero.template")),
    )
    return [
        {
            "kind": "zero",
            "key": f"zero:{action}",
            "action": action,
            "project": label,
            "agent": "",
            "status": "IDLE",
            "host": title,
            "remote": False,
        }
        for action, label in actions
    ]


class Tower:
    def __init__(self, session: str, own_pane_id: str = "", status_engine: Optional[StatusEngine] = None, results: Optional[ResultTracker] = None):
        self.session = session
        self.own_pane_id = own_pane_id
        self.local_host = local_host_label()
        self.access_context = None
        self.remote_hosts = load_remote_hosts()
        self._host_registry = HostRegistry(self.local_host, _raw_hostname(), self.remote_hosts)

        self.visits = VisitStore(STATE_DIR)
        self.overrides = OverrideStore(STATE_DIR / "overrides.json")
        self.bindings = ProjectBindingStore(STATE_DIR / "project-bindings.json")
        self.work_groups = WorkGroupStore(STATE_DIR / "work-groups.json")
        self.folders = FolderStore(STATE_DIR / "folders.json")
        self.window_assets: List[Dict] = []
        # The TUI owns one engine for its lifetime. Remote passes the
        # server's engine in so hysteresis survives across HTTP polls.
        self.status_engine = status_engine or StatusEngine()
        self.results = results

        # Loaded once per Tower run, not re-read every refresh -- matches
        # remote_hosts above. A user editing config.toml mid-session picks
        # it up on the next `tower` restart, not live.
        self.config = load_config()
        # This connection only. A gone client must not reuse it.
        self.copy_override = None
        self.notifier = notify.NotificationTracker(
            enabled=self.config["notifications"],
            notify_waiting=self.config["notification_kinds"]["waiting"],
            notify_dead=self.config["notification_kinds"]["dead"],
        )

        self.rows: List[Dict] = []           # all selectable data rows (unfiltered)
        self.visible_rows: List[Dict] = []   # rows after the search filter is applied
        self.visual: List[Dict] = []         # visible_rows incl. host headers, for drawing
        self.attention_mode = False          # A sorts the flat task list by what needs attention
        self.view_mode = USER_WORK_VIEW      # terminal structure is an explicit secondary view
        self.collapsed: set = set()          # window row keys hidden in the UI only
        self.collapsed_work_groups: set = set()
        # Incidental shells discovered from tmux stay out of the way. This is
        # presentation state for this Tower run; saved folders are untouched.
        self.collapsed_other = True
        self.collapsed_agents = False
        self.notice = ""
        self.selected = 0                    # index into visible_rows
        self.last_refresh = 0.0
        self.filter_text = ""
        self._remote_cache: Dict[str, Dict] = {}
        self._remote_last_fetch: Dict[str, float] = {}
        self._refresh_thread = None
        self._refresh_ready = None
        self._refresh_snapshot = None
        self.remote_state = None
        self._last_title_sync_scan = 0.0
        self._title_sync_last_applied: Dict[Tuple[str, str, str], float] = {}

    # -- data ---------------------------------------------------------

    def _activity_and_duration(self, key: str, adapter, ctx: PaneContext, status: str, dead: bool):
        """Shared by local/remote row-building: the (possibly None)
        activity text to show, and the current status's duration in
        seconds -- each respecting its own config display toggle.
        """

        activity_text = None
        if self.config["show_activity"] and not dead:
            activity = adapter.extract_activity(ctx)
            # Low-confidence guesses are computed (testable at the adapter
            # level) but never shown -- a plausible-sounding wrong answer
            # is worse than no activity line at all.
            if activity is not None and activity.confidence != "low":
                activity_text = activity.text

        duration_seconds = self.status_engine.duration_seconds(key) if self.config["show_status_duration"] else 0.0

        return activity_text, duration_seconds

    def _result_state(self, key: str, status: str, adapter, ctx: PaneContext, dead: bool, identity=None) -> str:
        """Uses the capture already taken for status. No second
        ``capture-pane``. Remote rows never call this: their captures
        are title-only and must not be invented into a result.
        """

        if self.results is None:
            return "none"
        candidate = None if dead else adapter.extract_result(ctx)
        # A shared-state poll needs only the fingerprint. Keep body text
        # out of tracker caches until the explicit Result view/copy path
        # re-extracts it from the pane.
        if candidate is not None and self.results.state_path is not None:
            candidate = ResultCandidate(
                "", candidate.fingerprint, candidate.confidence, candidate.complete,
                turn_complete=candidate.turn_complete, body_complete=candidate.body_complete,
            )
        return self.results.observe(key, status, candidate, identity=identity).state

    def _binding_name(self, key: str, session: Optional[str], pane_pid: Optional[str]) -> Optional[str]:
        """Launcher binding for this pane, or None when it does not match.

        The git name at the bound path wins over the name stored at
        creation, but only while that directory is still there.
        """

        record = self.bindings.usable(key, session or "", pane_pid or "")
        if not record:
            return None
        if str(record.get("transport") or "") == "ssh":
            # The path is on the far side. A local directory with the same
            # text is not that project.
            return (record.get("project_name") or "").strip() or None
        git_name = git_project_name(record.get("project_path") or "")
        return git_name or (record.get("project_name") or "").strip() or None

    def _identity(
        self,
        key: str,
        command: str,
        title: str,
        cmdline: str,
        lines,
        git_project: Optional[str],
        basename: Optional[str],
        host: str,
        no_name: str,
        binding_name: Optional[str] = None,
        process_git: Optional[str] = None,
        session: str = "",
        pane_pid: str = "",
    ) -> Dict:
        """Displayed names plus where they came from.

        The override, when present, replaces the label only. ``auto_*``
        stays the fresh detection so the next refresh can show a shell
        again after Claude exits, and so Reset Auto has something to
        return to.
        """

        auto_agent, auto_agent_source = identify_agent(command, title, cmdline, lines)
        title_project, title_task, title_role = render.title_identity(title, host)
        auto_project, auto_project_source = render.resolve_project_identity(
            None,
            git_project,
            title_project or (None if title_task else title),
            basename,
            host,
            no_name,
            binding_name=binding_name,
            process_git_name=process_git,
        )
        project, project_source = prefer_override(
            auto_project, auto_project_source, self.overrides.get_project(key, session, pane_pid)
        )
        agent, agent_source = prefer_override(
            auto_agent, auto_agent_source, self.overrides.get_agent(key, session, pane_pid)
        )
        saved_task_name = self.overrides.get_task_name(key, session, pane_pid)
        role = self.overrides.get_role(key, session, pane_pid) or title_role
        suggested_task_name = title_task or render.suggest_task_name(
            project, role, agent, title, host, no_name,
            lambda value: t(f"role.{value}"), t("task.terminal"), t("task.unnamed"),
        )
        task_name = saved_task_name or suggested_task_name
        name_origin = self.overrides.get_task_name_origin(key, session, pane_pid)
        return {
            "project": project,
            "task_name": task_name,
            "display_name": task_name,
            "name_origin": name_origin or "auto",
            "suggested_name": suggested_task_name,
            "role": role,
            "auto_project": auto_project,
            "project_source": project_source,
            "auto_project_source": auto_project_source,
            "agent": agent,
            "auto_agent": auto_agent,
            "agent_source": agent_source,
            "auto_agent_source": auto_agent_source,
            "tower_runtime": (
                Path(command or "").name.casefold() in {"tower", "tmux-agent-tower"}
                or "tmux_agent_tower.main" in (cmdline or "").casefold()
            ),
        }

    def _resolve_ssh(self, token: str) -> str:
        """OpenSSH's local reading of an alias. Cached on the registry."""

        return openssh_hostname(token)

    def _local_rows(self) -> List[Dict]:
        panes = discovery.list_panes(self.session, self.own_pane_id, CAPTURE_LINES)
        out = []
        no_name = t("project.no_name")

        for pane in panes:
            adapter = resolve_adapter(
                pane["command"], pane["title"], pane.get("cmdline") or "", pane.get("lines") or ()
            )
            ctx = PaneContext(
                title=pane["title"], command=pane["command"], lines=tuple(pane["lines"]),
                pane_pid=str(pane.get("pane_pid") or ""),
            )
            visit = self.visits.visit_label(self.session, pane["pane_id"])

            key = pane["pane_id"]
            session_name = pane.get("session") or ""
            pane_pid = pane.get("pane_pid") or ""
            bound = self.bindings.usable(key, session_name, pane_pid) or {}
            topology = classify_transport(
                tmux_host=self.local_host,
                registry=self._host_registry,
                ssh_target=pane.get("ssh_target") or "",
                ssh_stale=bool(pane.get("ssh_stale")),
                resolver=self._resolve_ssh,
                binding=bound,
                override_host=self.overrides.get_execution_host(key, session_name, pane_pid) or "",
            )
            via_ssh = topology["transport"] == "ssh"
            effective_title = pane.get("title") or ""
            if not render.looks_meaningful_title(effective_title, _raw_hostname()):
                effective_title = pane.get("window_name") or effective_title
            if via_ssh and effective_title.casefold() in {
                str(topology.get("transport_target") or "").casefold(),
                str(topology.get("execution_host") or "").casefold(),
            }:
                effective_title = ""
            identity = self._identity(
                key,
                pane["command"],
                effective_title,
                pane.get("cmdline") or "",
                pane.get("lines") or (),
                None if via_ssh else pane.get("git_project"),
                None if via_ssh else pane.get("path_basename"),
                _raw_hostname(),
                no_name,
                binding_name=self._binding_name(key, session_name, pane_pid),
                process_git=None if via_ssh else pane.get("process_git"),
                session=session_name,
                pane_pid=pane_pid,
            )
            auto_name, auto_source, screen_adapter = resolve_runtime(
                pane["command"],
                effective_title,
                pane.get("cmdline") or "",
                pane.get("lines") or (),
            )
            if identity["agent_source"] == "override":
                named = adapter_named(identity["agent"])
                # A display label such as CommandCode has no adapter.
                # Status, attention, and result still follow the screen.
                if named.name != "Shell" or identity["agent"] == "Shell":
                    adapter = named
                else:
                    adapter = screen_adapter
            else:
                identity["agent"] = auto_name
                identity["agent_source"] = auto_source
                adapter = screen_adapter
            status = self.status_engine.evaluate(
                pane["pane_id"], pane["dead"], adapter, ctx,
                runtime_identity=pane.get("pane_pid") or None,
            )
            project = identity["project"]
            agent = identity["agent"]
            auto_project = identity["auto_project"]
            title_line = render.title_secondary_line(project, effective_title, _raw_hostname())
            activity_text, duration_seconds = self._activity_and_duration(key, adapter, ctx, status, pane["dead"])
            result_identity = pane_result_identity({**pane, "tmux_host": topology.get("tmux_host") or ""})
            result_state = self._result_state(key, status, adapter, ctx, pane["dead"], result_identity)
            attention = "none" if pane["dead"] else adapter.detect_attention(ctx)
            attention_prompt = adapter.extract_attention_prompt(ctx) if attention != "none" else ""
            notify_status = "WAITING" if attention in ("approval_required", "input_required") else status

            self.notifier.observe(key, notify_status, project)

            out.append(
                {
                    "host": topology["execution_host"],
                    "observer_host": topology["observer_host"],
                    "tmux_host": topology["tmux_host"],
                    "execution_host": topology["execution_host"],
                    "transport": topology["transport"],
                    "transport_target": topology["transport_target"],
                    "target_id": _target_id(self.local_host, session_name, key, pane_pid),
                    "tower_pane_id": key,
                    "tower_pane_pid": pane_pid,
                    "remote_session_identity": bound.get("remote_session_identity") or "",
                    "result_provider_type": bound.get("result_provider_type") or (
                        "ssh_visible_history" if via_ssh else "local_tmux"
                    ),
                    "result_provider_endpoint": bound.get("result_provider_endpoint") or "",
                    "result_provider_session_id": bound.get("result_provider_session_id") or "",
                    "result_provider_pane_id": bound.get("result_provider_pane_id") or "",
                    "result_provider_pane_pid": bound.get("result_provider_pane_pid") or "",
                    "result_provider_liveness": bound.get("result_provider_liveness") or "live",
                    "topology_source": topology["topology_source"],
                    "place_label": _place_label(topology, pane),
                    "project": project,
                    "task_name": identity["task_name"],
                    "display_name": identity["display_name"],
                    "name_origin": identity["name_origin"],
                    "suggested_name": identity["suggested_name"],
                    "role": identity["role"],
                    "project_path": bound.get("project_path") or "",
                    "auto_project": auto_project,
                    "project_source": identity["project_source"],
                    "auto_project_source": identity["auto_project_source"],
                    "agent": agent,
                    "auto_agent": identity["auto_agent"],
                    "auto_detected_agent": auto_name,
                    "result_adapter": adapter.name,
                    "agent_source": identity["agent_source"],
                    "auto_agent_source": identity["auto_agent_source"],
                    "tower_runtime": bool(pane.get("tower_runtime") or identity["tower_runtime"]),
                    "title_line": title_line,
                    "live_lines": list(pane.get("lines") or ()),
                    "activity_text": activity_text,
                    "duration_seconds": duration_seconds,
                    "pane_title": effective_title,
                    "path": pane["path"],
                    "status": status,
                    "attention": attention,
                    "attention_prompt": attention_prompt,
                    "interaction": _interaction_payload(adapter, ctx, attention),
                    "approval_known": bool(attention == "approval_required" and adapter.approve(ctx)),
                    "reject_known": bool(attention == "approval_required" and adapter.reject(ctx)),
                    "result_state": result_state,
                    "visit": visit,
                    "key": key,
                    "session": session_name,
                    "window_id": pane.get("window_id") or "",
                    "window_index": pane["window_index"],
                    "window_name": pane.get("window_name") or "",
                    "pane_index": pane.get("pane_index"),
                    "pane_active": bool(pane.get("pane_active")),
                    "pane_id": pane["pane_id"],
                    "pane_pid": pane.get("pane_pid") or "",
                    "kind": "pane",
                    "remote": False,
                }
            )

        return out

    def _remote_rows(self, now: float) -> List[Dict]:
        out = []

        for host_cfg in self.remote_hosts:
            alias, name = host_cfg["alias"], host_cfg["name"]
            last = self._remote_last_fetch.get(alias, 0.0)

            if now - last >= REMOTE_REFRESH_SECONDS or alias not in self._remote_cache:
                self._remote_cache[alias] = fetch_remote(alias, display_name=name)
                self._remote_last_fetch[alias] = now

            snapshot = self._remote_cache[alias]

            if snapshot["status"] != HOST_STATUS_ONLINE:
                out.append(
                    {
                        "host": name,
                        "project": None,
                        "placeholder": "remote.unreachable",
                        "agent": "-",
                        "status": "UNKNOWN",
                        "visit": None,
                        "key": f"remote:{alias}:offline",
                        "remote": True,
                        "offline": True,
                    }
                )
                continue

            if not snapshot["panes"]:
                out.append(
                    {
                        "host": name,
                        "project": None,
                        "placeholder": "remote.no_server",
                        "agent": "-",
                        "status": "IDLE",
                        "visit": None,
                        "key": f"remote:{alias}:empty",
                        "remote": True,
                        "offline": False,
                    }
                )
                continue

            remote_session = f"remote:{alias}"
            no_name = t("project.no_name")

            for pane in snapshot["panes"]:
                composite_key = f"{alias}:{pane['pane_id']}"
                adapter = resolve_adapter(
                    pane["command"], pane["title"], pane.get("cmdline") or "", pane.get("lines") or ()
                )
                ctx = PaneContext(title=pane["title"], command=pane["command"], lines=tuple(pane["lines"]))
                status = self.status_engine.evaluate(
                    composite_key, pane["dead"], adapter, ctx,
                    runtime_identity=pane.get("pane_pid") or None,
                )
                visit = self.visits.visit_label(remote_session, pane["pane_id"])

                # Remote title overrides aren't pushed to the real remote
                # tmux (no remote install -- see docs/ARCHITECTURE.md), so
                # the override itself is the effective title here.
                effective_title = (
                    self.overrides.get_title(composite_key, pane.get("session") or "", pane.get("pane_pid") or "")
                    or pane["title"]
                )
                basename = Path(pane["path"] or "").name or pane["path"]
                identity = self._identity(
                    composite_key,
                    pane["command"],
                    effective_title,
                    pane.get("cmdline") or "",
                    pane.get("lines") or (),
                    None,
                    basename,
                    name,
                    no_name,
                    session=pane.get("session") or "",
                    pane_pid=pane.get("pane_pid") or "",
                )
                project = identity["project"]
                agent = identity["agent"]
                auto_project = identity["auto_project"]
                title_line = render.title_secondary_line(project, effective_title, name)
                # Remote captures are title-only (see docs/ARCHITECTURE.md),
                # so activity extraction has nothing to search -- almost
                # always None here, which is honest given the evidence.
                activity_text, duration_seconds = self._activity_and_duration(
                    composite_key, adapter, ctx, status, pane["dead"]
                )

                self.notifier.observe(composite_key, status, project)
                topo = peer_topology(self.local_host, name)

                out.append(
                    {
                        "host": topo["execution_host"],
                        "observer_host": topo["observer_host"],
                        "tmux_host": topo["tmux_host"],
                        "execution_host": topo["execution_host"],
                        "transport": topo["transport"],
                        "transport_target": topo["transport_target"],
                        "topology_source": topo["topology_source"],
                        "place_label": t("nav.peer_tmux").format(
                            index=pane.get("window_index") if pane.get("window_index") is not None else "",
                            name=pane.get("window_name") or "",
                        ),
                        "project": project,
                        "task_name": identity["task_name"],
                        "display_name": identity["display_name"],
                        "name_origin": identity["name_origin"],
                        "suggested_name": identity["suggested_name"],
                        "role": identity["role"],
                        "project_path": pane.get("path") or "",
                        "auto_project": auto_project,
                        "project_source": identity["project_source"],
                        "auto_project_source": identity["auto_project_source"],
                        "agent": agent,
                        "auto_agent": identity["auto_agent"],
                        "auto_detected_agent": identity["auto_agent"],
                        "result_adapter": adapter.name,
                        "agent_source": identity["agent_source"],
                        "auto_agent_source": identity["auto_agent_source"],
                        "tower_runtime": identity["tower_runtime"],
                        "title_line": title_line,
                        "activity_text": activity_text,
                        "duration_seconds": duration_seconds,
                        "pane_title": effective_title,
                        "path": pane["path"],
                        "status": status,
                        "result_state": "none",
                        "visit": visit,
                        "key": composite_key,
                        "session": pane.get("session"),
                        "session_id": pane.get("session_id") or "",
                        "window_id": pane.get("window_id") or "",
                        "window_index": pane.get("window_index"),
                        "window_name": pane.get("window_name") or "",
                        "window_created": pane.get("window_created") or "",
                        "pane_index": pane.get("pane_index"),
                        "pane_id": pane.get("pane_id"),
                        "pane_pid": pane.get("pane_pid") or "",
                        "command": pane.get("command") or "",
                        "cmdline": pane.get("cmdline") or "",
                        "result_provider_host": alias,
                        "target_id": _target_id(
                            alias, pane.get("session_id") or "", pane.get("pane_id") or "",
                            str(pane.get("pane_pid") or ""),
                        ),
                        "tower_pane_id": "",
                        "tower_pane_pid": "",
                        "result_provider_type": "remote_tmux",
                        "result_provider_endpoint": alias,
                        "result_provider_session_id": pane.get("session_id") or "",
                        "result_provider_pane_id": pane.get("pane_id") or "",
                        "result_provider_pane_pid": str(pane.get("pane_pid") or ""),
                        "result_provider_liveness": "live" if not pane.get("dead") else "stale",
                        "kind": "pane",
                        "remote": True,
                        "offline": False,
                    }
                )

        return out

    @trace_refresh
    def load(self, *, update_group_labels: bool = True) -> None:
        previous_key = None
        if self.visible_rows and 0 <= self.selected < len(self.visible_rows):
            previous_key = self.visible_rows[self.selected]["key"]

        now = time.monotonic()
        self.access_context = observe_access_client()[0].access_context
        windows = discovery.list_windows(self.session)
        own_window_id = discovery.window_for_pane(self.own_pane_id)
        for window in windows:
            window["tmux_host"] = self.local_host
            window["window_ref"] = window_ref(window)
        local_by_id = {(str(row.get("session") or ""), str(row.get("window_id") or "")): row for row in windows}
        self.rows = self._local_rows() + self._remote_rows(now)
        live_windows = {row["window_ref"]: row for row in windows}
        for row in self.rows:
            if not row.get("pane_id") or row.get("placeholder"):
                continue
            if not row.get("remote"):
                asset = local_by_id.get((str(row.get("session") or ""), str(row.get("window_id") or "")))
                if asset:
                    row.update({key: asset.get(key) for key in ("session_id", "window_created", "window_ref")})
                else:
                    row["tmux_host"] = self.local_host
                    row["window_ref"] = window_ref(row)
            else:
                row["window_ref"] = window_ref(row)
            ref = row.get("window_ref")
            if ref and ref not in live_windows:
                live_windows[ref] = {
                    key: row.get(key) for key in (
                        "tmux_host", "host", "session", "session_id", "window_id",
                        "window_index", "window_name", "window_created", "window_ref", "remote",
                    )
                }
        task_rows = [row for row in self.rows if row.get("pane_id") and not row.get("tower_runtime")]
        live_windows = folder_tree.visible_windows(live_windows.values(), self.rows, own_window_id)
        self.window_assets = folder_tree.infer_window_assets(live_windows, task_rows, self.local_host)
        groups = self.work_groups.all()
        group_by_target = {target: group for group in groups for target in group["member_target_ids"]}
        for row in self.rows:
            group = group_by_target.get(str(row.get("target_id") or ""))
            if group:
                row["work_group_id"] = group["group_id"]
                row["work_group_name"] = group["display_name"]
        if update_group_labels:
            try:
                self.work_groups.update_labels({
                    str(row["target_id"]): str(row.get("display_name") or row.get("task_name") or row.get("project") or t("group.stale_member"))
                    for row in self.rows if row.get("target_id")
                })
            except OSError:
                pass
        self._apply_filter(previous_key)
        selected_kind = self.visible_rows[self.selected].get("kind", "") if self.visible_rows else ""
        event(
            "PROJECTION_READY", rows=len(self.visible_rows),
            row_kinds=[row.get("kind", "") for row in self.visible_rows],
            selected_index=self.selected, selected_kind=selected_kind,
            selected_collapsed=bool(self.visible_rows and self.visible_rows[self.selected].get("collapsed")),
        )

    def start_background_refresh(self) -> bool:
        """Refresh a private Tower snapshot; the curses thread never waits."""
        if self._refresh_thread and self._refresh_thread.is_alive():
            return False
        if self._refresh_ready is not None and not self._refresh_ready.is_set():
            return False

        snapshot = copy.copy(self)
        for name in ("status_engine", "notifier", "visits", "overrides", "bindings", "work_groups", "folders", "_host_registry"):
            setattr(snapshot, name, copy.deepcopy(getattr(self, name)))
        snapshot.results = (
            ResultTracker(self.results.state_path)
            if self.results is not None and self.results.state_path is not None
            else copy.deepcopy(self.results)
        )
        snapshot.rows = [dict(row) for row in self.rows]
        snapshot.visible_rows = list(self.visible_rows)
        snapshot.visual = list(self.visual)
        snapshot.window_assets = [dict(row) for row in self.window_assets]
        snapshot._remote_cache = copy.deepcopy(self._remote_cache)
        snapshot._remote_last_fetch = dict(self._remote_last_fetch)
        self._refresh_snapshot = None
        self._refresh_ready = Event()
        ready = self._refresh_ready

        def refresh() -> None:
            try:
                snapshot.load(update_group_labels=False)
                try:
                    from ..server import service

                    snapshot.remote_state = service.status().state
                except Exception:
                    snapshot.remote_state = "error"
                self._refresh_snapshot = snapshot
            except Exception as exc:
                event("DATA_REFRESH_FAILED", error=type(exc).__name__)
            finally:
                ready.set()

        self.last_refresh = time.monotonic()
        self._refresh_thread = Thread(target=refresh, name="tower-data-refresh", daemon=True)
        event("DATA_REFRESH_SCHEDULED")
        self._refresh_thread.start()
        return True

    def apply_background_refresh(self) -> bool:
        """Adopt completed data on the UI thread, preserving its current selection."""
        ready = self._refresh_ready
        if ready is None or not ready.is_set():
            return False
        self._refresh_ready = None
        snapshot_data = self._refresh_snapshot
        self._refresh_snapshot = None
        if snapshot_data is None:
            return False

        snapshot = snapshot_data
        current_key = self.visible_rows[self.selected].get("key") if self.visible_rows and self.selected < len(self.visible_rows) else None
        for name in ("access_context", "rows", "window_assets", "status_engine", "notifier", "_host_registry", "_remote_cache", "_remote_last_fetch", "remote_state"):
            setattr(self, name, getattr(snapshot, name))
        self._apply_filter(current_key)
        try:
            self.work_groups.update_labels({
                str(row["target_id"]): str(row.get("display_name") or row.get("task_name") or row.get("project") or t("group.stale_member"))
                for row in self.rows if row.get("target_id")
            })
        except OSError:
            pass
        event("DATA_REFRESH_APPLIED")
        return True

    def _apply_filter(self, previous_key: Optional[str] = None) -> None:
        """Recomputes ``visible_rows``/``visual`` from ``rows`` + the
        current search filter, trying to keep the same row selected by
        key (falling back to clamping the index) -- called both after a
        fresh ``load()`` and whenever the filter text itself changes.
        """

        if previous_key is None and self.visible_rows and 0 <= self.selected < len(self.visible_rows):
            previous_key = self.visible_rows[self.selected]["key"]

        filtered = [r for r in self.rows if render.row_matches_filter(r, self.filter_text)]

        panes = [r for r in self.rows if r.get("pane_id")]
        work_panes = [r for r in panes if not r.get("tower_runtime")]
        groups = self.work_groups.all()
        if (not self.filter_text and self.view_mode != TERMINAL_STRUCTURE_VIEW
                and not work_panes and not groups and not self.folders.all()):
            self.visible_rows = zero_state_rows()
        elif self.view_mode == TERMINAL_STRUCTURE_VIEW:
            self.visible_rows = navigator_rows(filtered, set() if self.filter_text else self.collapsed)
        else:
            if self.attention_mode:
                matching = [r for r in work_panes if render.row_matches_filter(r, self.filter_text)]
                self.visible_rows = render.sort_by_attention(matching)
            else:
                self.visible_rows = folder_tree.build_rows(
                    self.folders, self.window_assets, work_panes, self.filter_text,
                    self.collapsed_other, self.collapsed_agents,
                )

        if not self.visible_rows:
            self.selected = 0
            self.visual = []
            return

        if previous_key:
            for idx, row in enumerate(self.visible_rows):
                if row["key"] == previous_key:
                    self.selected = idx
                    break
            else:
                self.selected = min(self.selected, len(self.visible_rows) - 1)
        else:
            self.selected = min(self.selected, len(self.visible_rows) - 1)

        self._build_visual()

    def set_filter(self, text: str) -> None:
        self.filter_text = text
        self._apply_filter()

    def clear_filter(self) -> None:
        self.set_filter("")

    def _build_visual(self) -> None:
        if self.view_mode == TERMINAL_STRUCTURE_VIEW and any(row.get("kind") == "window" for row in self.visible_rows):
            # Host is the divider. Window and pane are both rows.
            visual = []
            seen = []
            for idx, row in enumerate(self.visible_rows):
                label = row.get("host") or ""
                if label and label not in seen:
                    seen.append(label)
                    visual.append({"type": "header", "host": label})
                visual.append({"type": "data", "row": row, "row_index": idx})
            self.visual = visual
            return

        if self.attention_mode:
            # Flat, priority-sorted, deliberately NOT grouped by host --
            # the point is "what needs me right now," not "what's on
            # which machine."
            self.visual = [{"type": "header", "host": t("attention.title")}] + [
                {"type": "data", "row": row, "row_index": idx} for idx, row in enumerate(self.visible_rows)
            ]
            return

        visual = [{"type": "header", "host": t("list.title")}]
        clustered = None
        for idx, row in enumerate(self.visible_rows):
            project = row.get("_project_cluster")
            if project and project != clustered:
                visual.append({"type": "header", "host": str(project), "level": "project"})
            clustered = project
            visual.append({"type": "data", "row": row, "row_index": idx})
        self.visual = visual

    def toggle_attention(self) -> None:
        self.attention_mode = not self.attention_mode
        self.view_mode = USER_WORK_VIEW
        self._apply_filter()

    def toggle_navigator(self) -> None:
        """Switch between user work and the advanced terminal structure."""
        self.attention_mode = False
        self.view_mode = (
            TERMINAL_STRUCTURE_VIEW
            if self.view_mode == USER_WORK_VIEW
            else USER_WORK_VIEW
        )
        self._apply_filter()

    @property
    def navigator_mode(self) -> bool:
        """Compatibility name for callers that still inspect the old flag."""
        return self.view_mode == TERMINAL_STRUCTURE_VIEW

    @navigator_mode.setter
    def navigator_mode(self, enabled: bool) -> None:
        self.view_mode = TERMINAL_STRUCTURE_VIEW if enabled else USER_WORK_VIEW

    def toggle_work_group(self, group_id: str) -> None:
        if group_id in self.collapsed_work_groups:
            self.collapsed_work_groups.remove(group_id)
        else:
            self.collapsed_work_groups.add(group_id)
        self._apply_filter(f"group:{group_id}")

    def toggle_folder(self, folder_id: str) -> None:
        if folder_id == "__unfiled__":
            self.collapsed_other = not self.collapsed_other
        elif folder_id == "__agents__":
            self.collapsed_agents = not self.collapsed_agents
        else:
            try:
                self.folders.toggle_folder(folder_id)
            except (KeyError, OSError):
                self.notice = t("folder.save_failed")
        self._apply_filter(f"folder:{folder_id}")

    def toggle_window_asset(self, ref: str) -> None:
        row = next((item for item in self.window_assets if item.get("window_ref") == ref), None)
        if row is None:
            return
        try:
            self.folders.update_window(ref, collapsed=not bool(self.folders.window(ref).get("collapsed", False)))
        except (ValueError, OSError):
            self.notice = t("folder.save_failed")
        self._apply_filter("windowasset:" + ref)

    def collapse_selected(self) -> None:
        """Hide a window's panes. tmux is not changed."""

        row = self.visible_rows[self.selected] if self.visible_rows else None
        if not row:
            return
        if row.get("kind") == "folder":
            if row.get("folder_id") == "__unfiled__":
                self.collapsed_other = True
            else:
                try:
                    folder = next(item for item in self.folders.all() if item["folder_id"] == row.get("folder_id"))
                    if not folder.get("collapsed"):
                        self.folders.toggle_folder(folder["folder_id"])
                except (KeyError, StopIteration, OSError):
                    self.notice = t("folder.save_failed")
            self._apply_filter(row.get("key"))
            return
        if row.get("kind") == "other_section":
            if row.get("folder_id") == "__agents__":
                self.collapsed_agents = True
            else:
                self.collapsed_other = True
            self._apply_filter(row.get("key"))
            return
        if row.get("kind") == "work_group":
            self.collapsed_work_groups.add(row.get("group_id"))
            self._apply_filter(row.get("key"))
            return
        if row.get("kind") == "window_asset":
            try:
                self.folders.update_window(row["window_ref"], collapsed=True)
            except (KeyError, OSError, ValueError):
                self.notice = t("folder.save_failed")
            self._apply_filter(row.get("key"))
            return
        key = row.get("key") if row.get("kind") == "window" else row.get("tree_window")
        if not key:
            return
        self.collapsed.add(key)
        self._apply_filter(key)

    def expand_selected(self) -> None:
        """Show a window's panes again. tmux is not changed."""

        row = self.visible_rows[self.selected] if self.visible_rows else None
        if not row:
            return
        if row.get("kind") == "folder":
            if row.get("folder_id") == "__unfiled__":
                self.collapsed_other = False
            else:
                try:
                    folder = next(item for item in self.folders.all() if item["folder_id"] == row.get("folder_id"))
                    if folder.get("collapsed"):
                        self.folders.toggle_folder(folder["folder_id"])
                except (KeyError, StopIteration, OSError):
                    pass
            self._apply_filter(row.get("key"))
            return
        if row.get("kind") == "other_section":
            if row.get("folder_id") == "__agents__":
                self.collapsed_agents = False
            else:
                self.collapsed_other = False
            self._apply_filter(row.get("key"))
            return
        if row.get("kind") == "window_asset":
            try:
                if self.folders.window(row["window_ref"]).get("collapsed"):
                    self.folders.update_window(row["window_ref"], collapsed=False)
            except (KeyError, OSError, ValueError):
                pass
            self._apply_filter(row.get("key"))
            return
        if row.get("kind") == "work_group":
            self.collapsed_work_groups.discard(row.get("group_id"))
            self._apply_filter(row.get("key"))
            return
        key = row.get("key") if row.get("kind") == "window" else row.get("tree_window")
        if not key:
            return
        self.collapsed.discard(key)
        self._apply_filter(key)

    # -- actions --------------------------------------------------------

    def move_up(self) -> None:
        if not self.visible_rows:
            return
        self.selected = (self.selected - 1) % len(self.visible_rows)
        row = self.visible_rows[self.selected]
        event("SELECTION_UPDATED", index=self.selected, kind=row.get("kind", ""), collapsed=bool(row.get("collapsed")))

    def move_down(self) -> None:
        if not self.visible_rows:
            return
        self.selected = (self.selected + 1) % len(self.visible_rows)
        row = self.visible_rows[self.selected]
        event("SELECTION_UPDATED", index=self.selected, kind=row.get("kind", ""), collapsed=bool(row.get("collapsed")))

    def control_key(self) -> Optional[str]:
        """Enter. Opens control for the selected row. Does not move tmux.

        A pane returns its pane key. A window returns its window id.
        A zero-state row returns its action key.
        """

        if not self.visible_rows:
            return None
        row = self.visible_rows[self.selected]
        intent = enter_intent(row)
        if intent == "control":
            if row.get("pane_id"):
                self.visits.mark_seen(self.session, row["pane_id"])
            return row.get("key")
        if intent == "window":
            return row.get("window_id")
        if intent == "zero":
            return row.get("key")
        return None

    def rename_selected(self, stdscr) -> None:
        """Rename a task in Tower; local tmux title sync is an explicit opt-in."""

        if not self.visible_rows:
            return
        row = self.visible_rows[self.selected]
        if row.get("kind") == "folder":
            if row.get("folder_id") == "__unfiled__":
                return
            name = prompt_text(stdscr, t("folder.rename_prompt"), initial=row.get("display_name") or "")
            if name and name.strip():
                try:
                    self.folders.rename(row["folder_id"], name)
                except (KeyError, OSError, ValueError):
                    self.notice = t("folder.save_failed")
            self.load()
            return
        if row.get("kind") == "window_asset":
            name = prompt_text(stdscr, t("folder.window_rename_prompt"), initial=row.get("display_name") or "")
            if name and name.strip():
                try:
                    self.folders.update_window(row["window_ref"], display_name=name)
                except (OSError, ValueError):
                    self.notice = t("folder.save_failed")
            self.load()
            return
        if row.get("kind") == "work_group":
            current = row.get("display_name") or ""
            name = prompt_text(stdscr, t("group.rename_prompt"), initial=current)
            if name:
                self.work_groups.rename(row["group_id"], name)
            self.load()
            return
        if row.get("kind") == "window":
            window = row
            pane_id = window.get("rename_pane_id")
            row = next((
                pane for pane in self.rows
                if pane.get("pane_id") == pane_id
                and str(pane.get("session") or "") == str(window.get("session") or "")
                and bool(pane.get("remote")) == bool(window.get("remote"))
                and (not window.get("window_id") or pane.get("window_id") == window.get("window_id"))
            ), {})
        if row.get("offline") or not row.get("pane_id"):
            return

        key = row["key"]
        session = str(row.get("session") or "")
        pane_pid = str(row.get("pane_pid") or "")
        if not key or not session or not pane_pid:
            self.notice = t("rename.stale")
            return
        current = row.get("display_name") or row.get("task_name") or row.get("project") or ""
        suggested = row.get("suggested_name") or current or "-"
        is_remote = not self._supports_local_pane_title_sync(row)
        name = prompt_text(
            stdscr,
            t("rename.prompt"),
            initial=current,
            context_lines=[
                t("rename.current", name=current or "-"),
                t("rename.auto", name=suggested),
                t("rename.default_scope"),
                t("rename.remote_scope") if is_remote else t("rename.local_scope"),
            ],
        )
        if name is None:
            return

        cleaned_name = name.strip()
        sync_state = self.overrides.get_task_title_sync(key, session, pane_pid)
        if not cleaned_name:
            if sync_state["enabled"] and not is_remote and not self._restore_managed_pane_title(row, sync_state):
                self.notice = t("rename.auto_restore_failed")
                self.load()
                return
            self.overrides.clear_field(key, "task_name", session, pane_pid)
            self.notice = t("rename.auto_restored")
            self.load()
            return

        sync_selected = False
        if not is_remote:
            pick = run_list_picker(
                stdscr,
                t("rename.scope_title"),
                [("sync", t("rename.sync_option"))],
                multi=True,
                checked={"sync"} if sync_state["enabled"] else set(),
                footer_hint=t("rename.scope_hint"),
                preamble=[
                    t("rename.current", name=cleaned_name),
                    t("rename.auto", name=suggested),
                    t("rename.default_scope"),
                ],
            )
            if pick.cancelled:
                return
            sync_selected = "sync" in pick.selected_keys

        self.overrides.set_task_name(key, cleaned_name, session, pane_pid)
        if sync_selected:
            ok, _reason = self._sync_one_pane_title(row, cleaned_name, time.monotonic(), initial=True)
            if ok:
                self.notice = t("rename.changed_both")
            else:
                self.overrides.set_task_title_sync(
                    key, session, pane_pid, enabled=False, blocked=False, conflicts=0,
                )
                self.notice = t("rename.display_only")
        else:
            self.overrides.set_task_title_sync(
                key, session, pane_pid, enabled=False, blocked=False, conflicts=0,
            )
            self.notice = t("rename.changed_tower")
        self.load()

    def _restore_managed_pane_title(self, row: Dict, sync_state: Dict[str, object]) -> bool:
        """On explicit auto-reset, restore only a title Tower still owns."""

        if not self._supports_local_pane_title_sync(row) or not sync_state.get("last_title"):
            return True
        from ..tmux import structure

        session = str(row.get("session") or "")
        pane_id = str(row.get("pane_id") or "")
        pane_pid = str(row.get("pane_pid") or "")
        live = structure.pane_title_identity(session, pane_id)
        if not live or live["pane_pid"] != pane_pid:
            return True
        if live["pane_title"] != sync_state.get("last_title"):
            # An Agent or user already changed the title; do not overwrite it.
            return True
        result = structure.rename_pane(
            session,
            pane_id,
            str(sync_state.get("original_title") or ""),
            expected_pane_pid=pane_pid,
            allow_empty=True,
        )
        return result.ok

    def _sync_one_pane_title(
        self, row: Dict, task_name: str, now: float, *, initial: bool = False,
    ) -> Tuple[bool, str]:
        """Set only this local pane's title after a fresh session/id/pid check."""

        if not self._supports_local_pane_title_sync(row):
            return False, "unsupported"
        key = str(row.get("key") or "")
        session = str(row.get("session") or "")
        pane_id = str(row.get("pane_id") or "")
        pane_pid = str(row.get("pane_pid") or "").strip()
        if not key or not session or not pane_pid:
            return False, "stale"

        from ..tmux import structure

        cleaned_title = structure.clean_name(task_name, limit=80)
        if not cleaned_title or cleaned_title != task_name:
            return False, "bad_title"
        live = structure.pane_title_identity(session, pane_id)
        if not live or live["pane_pid"] != pane_pid:
            return False, "stale"

        state = self.overrides.get_task_title_sync(key, session, pane_pid)
        previous_last = str(state.get("last_title") or "")
        original_title = (
            str(state.get("original_title") or "")
            if previous_last and live["pane_title"] == previous_last
            else live["pane_title"]
        )
        attempt_key = (key, session, pane_pid)

        if live["pane_title"] != cleaned_title and not initial:
            last_attempt = self._title_sync_last_applied.get(attempt_key)
            conflict_count = int(state.get("conflicts") or 0)
            if (last_attempt is not None and now - last_attempt <= PANE_TITLE_SYNC_CONFLICT_WINDOW_SECONDS) or (
                last_attempt is None and conflict_count > 0
            ):
                conflict_count += 1
                if conflict_count >= PANE_TITLE_SYNC_MAX_CONFLICTS:
                    self.overrides.set_task_title_sync(
                        key, session, pane_pid, enabled=False, blocked=True,
                        conflicts=conflict_count,
                    )
                    self._title_sync_last_applied.pop(attempt_key, None)
                    self.notice = t("rename.title_sync_conflict")
                    return False, "conflict"
            else:
                conflict_count = 0
        else:
            conflict_count = 0

        result = structure.rename_pane(
            session, pane_id, cleaned_title, expected_pane_pid=pane_pid,
        )
        if not result.ok:
            return False, result.detail or "rename_failed"
        self.overrides.set_task_title_sync(
            key, session, pane_pid, enabled=True, blocked=False,
            conflicts=conflict_count, original_title=original_title, last_title=cleaned_title,
        )
        if live["pane_title"] != cleaned_title:
            self._title_sync_last_applied[attempt_key] = now
        else:
            self._title_sync_last_applied.pop(attempt_key, None)
        return True, "changed" if live["pane_title"] != cleaned_title else "unchanged"

    def sync_opted_in_pane_titles(self, now: Optional[float] = None) -> bool:
        """Rate-limited reconciliation; at most one pane title write per scan."""

        now = time.monotonic() if now is None else now
        if now - self._last_title_sync_scan < PANE_TITLE_SYNC_INTERVAL_SECONDS:
            return False
        self._last_title_sync_scan = now
        for row in self.rows:
            if row.get("kind") != "pane" or not self._supports_local_pane_title_sync(row):
                continue
            key = str(row.get("key") or "")
            session = str(row.get("session") or "")
            pane_pid = str(row.get("pane_pid") or "").strip()
            if not self.overrides.get_task_title_sync(key, session, pane_pid).get("enabled"):
                continue
            task_name = self.overrides.get_task_name(key, session, pane_pid)
            if self.overrides.get_task_name_origin(key, session, pane_pid) != "user" or not task_name:
                self.overrides.set_task_title_sync(
                    key, session, pane_pid, enabled=False, blocked=False, conflicts=0,
                )
                continue
            before_notice = self.notice
            ok, _reason = self._sync_one_pane_title(row, str(task_name), now)
            if not ok:
                if _reason != "conflict":
                    self.overrides.set_task_title_sync(
                        key, session, pane_pid, enabled=False, blocked=False, conflicts=0,
                    )
                    self.notice = t("rename.display_only")
                return self.notice != before_notice
            if _reason != "unchanged":
                return False
        return False

    def _supports_local_pane_title_sync(self, row: Dict) -> bool:
        """Only a locally observed tmux pane may receive physical title writes."""

        if row.get("remote") or row.get("transport") == "ssh" or row.get("offline") or row.get("placeholder"):
            return False
        tmux_host = str(row.get("tmux_host") or "").strip().casefold()
        local_host = str(self.local_host or "").strip().casefold()
        return not tmux_host or not local_host or tmux_host == local_host

    def edit_selected(self, stdscr) -> None:
        """Edit task identity first; keep terminal labels under More."""

        if not self.visible_rows:
            return
        row = self.visible_rows[self.selected]
        if row.get("offline") or row.get("remote"):
            return

        menu_items = [
            ("rename", t("menu.rename_task")),
            ("agent", t("menu.change_agent")),
            ("role", t("role.change")),
            ("project", t("menu.change_project")),
            ("more", t("menu.more")),
            ("cancel", t("menu.cancel")),
        ]
        pick = run_list_picker(stdscr, t("menu.edit_task"), menu_items, footer_hint=t("wizard.hint_list"))

        if pick.cancelled or pick.selected_key in (None, "cancel"):
            return

        if pick.selected_key == "rename":
            self.rename_selected(stdscr)
            return

        if pick.selected_key == "role":
            from .structure_menu import change_role

            change_role(stdscr, self, row)
            self.load()
            return

        if pick.selected_key == "move":
            from .structure_menu import move_work

            move_work(stdscr, self, row)
            self.load()
            return

        if pick.selected_key == "layout":
            from .structure_menu import open_target_layout

            open_target_layout(stdscr, self, row)
            self.load()
            return

        key = row["key"]
        session = str(row.get("session") or "")
        pane_pid = str(row.get("pane_pid") or "")

        def _context(current: Optional[str], auto: Optional[str]) -> List[str]:
            lines = []
            if current:
                lines.append(f'{t("edit.current_label")}: {current}')
            if auto:
                lines.append(f'{t("edit.auto_label")}: {auto}')
            return lines

        if pick.selected_key == "project":
            from .workspace_browser import pick_project_for_target

            chosen = pick_project_for_target(stdscr, self, STATE_DIR, row)
            if chosen:
                ok = self.bindings.rebind_project(
                    key,
                    session,
                    pane_pid,
                    chosen.entry.path,
                    chosen.entry.name,
                    row,
                )
                if ok:
                    if row.get("name_origin") == "auto":
                        self.overrides.set_task_name(
                            key, row.get("display_name") or row.get("task_name") or "",
                            session, pane_pid, origin="auto",
                        )
                    self.overrides.clear_field(key, "project", session, pane_pid)

        elif pick.selected_key == "agent":
            agent_items = [(label, label) for label in AGENT_LAUNCH_ORDER]
            agent_items.append(("__custom__", t("menu.manual_agent_entry")))
            agent_items.append(("__auto__", t("menu.use_auto_agent")))
            agent_pick = run_list_picker(stdscr, t("menu.agent_name"), agent_items, footer_hint=t("wizard.hint_list"))

            if agent_pick.cancelled or agent_pick.selected_key is None:
                pass
            elif agent_pick.selected_key == "__custom__":
                context = _context(self.overrides.get_agent(key, session, pane_pid), row.get("auto_agent"))
                custom = prompt_text(stdscr, t("prompt.agent_name"), context_lines=context)
                if custom:
                    update_identity(self, key, {"agent": custom})
            elif agent_pick.selected_key == "__auto__":
                update_identity(self, key, {"agent": None})
            else:
                update_identity(self, key, {"agent": agent_pick.selected_key})

        elif pick.selected_key == "more":
            more_items = [
                ("execution", t("menu.execution_host")),
                ("title", t("menu.pane_title")),
                ("reset", t("menu.reset_auto")),
                ("cancel", t("menu.cancel")),
            ]
            more = run_list_picker(stdscr, t("menu.more"), more_items, footer_hint=t("wizard.hint_list"))
            if not more.cancelled and more.selected_key not in (None, "cancel"):
                if more.selected_key == "execution":
                    context = _context(self.overrides.get_execution_host(key, session, pane_pid), row.get("execution_host"))
                    name = prompt_text(stdscr, t("prompt.execution_host"), context_lines=context)
                    if name:
                        update_identity(self, key, {"execution_host": name})
                elif more.selected_key == "title":
                    context = _context(self.overrides.get_title(key, session, pane_pid), row.get("pane_title"))
                    title = prompt_text(stdscr, t("prompt.pane_title"), context_lines=context)
                    if title:
                        update_identity(self, key, {"title": title})
                elif more.selected_key == "reset":
                    update_identity(self, key, {"reset": True})

        self.load()

    def status_counts(self, host: Optional[str] = None) -> Dict[str, int]:
        """Counts reflect the search filter (if any) -- see the module
        docstring's note on ``visible_rows`` vs. ``rows``.
        """

        rows = [r for r in self.visible_rows if r.get("pane_id")]
        if host is not None:
            rows = [r for r in rows if r.get("host") == host]
        return {name: sum(1 for r in rows if r.get("status") == name) for name in STATUS_ORDER}


def setup_colors():
    if not curses.has_colors():
        return
    curses.start_color()
    try:
        curses.use_default_colors()
    except curses.error:
        pass
    curses.init_pair(1, curses.COLOR_GREEN, -1)
    curses.init_pair(2, curses.COLOR_YELLOW, -1)
    curses.init_pair(3, curses.COLOR_WHITE, -1)
    curses.init_pair(4, curses.COLOR_CYAN, -1)
    curses.init_pair(5, curses.COLOR_RED, -1)
    curses.init_pair(6, curses.COLOR_BLACK, curses.COLOR_WHITE)


def status_attr(status: str) -> int:
    if not curses.has_colors():
        return 0
    return {
        "WORKING": curses.color_pair(1) | curses.A_BOLD,
        "WAITING": curses.color_pair(2) | curses.A_BOLD,
        "IDLE": curses.color_pair(3) | curses.A_DIM,
        "UNKNOWN": curses.color_pair(4) | curses.A_BOLD,
        "DEAD": curses.color_pair(5) | curses.A_BOLD,
    }.get(status, 0)


def _hosts_in_order(tower: Tower) -> List[str]:
    hosts: List[str] = []
    for row in tower.visible_rows:
        if row["host"] not in hosts:
            hosts.append(row["host"])
    return hosts


def _status_label(status: str) -> str:
    return t("status." + status)


def _project_text(row: Dict) -> str:
    project = row.get("project")
    if project and project != t("project.no_name"):
        return str(project)
    if row.get("placeholder"):
        return t(row["placeholder"])
    return ""


def _duration_text(tower: Tower, row: Dict) -> str:
    if not tower.config.get("show_status_duration"):
        return ""
    seconds = row.get("duration_seconds")
    if not seconds:
        return ""
    return render.format_duration(seconds)


def _badge_text(symbol: str, key: str) -> str:
    return f"{symbol} {t(key)}"


def _build_detail_fields(tower: Tower, row: Dict, advanced: bool = False) -> List[Tuple[str, str]]:
    if row.get("kind") == "work_group":
        return [
            (t("group.label"), row.get("display_name") or ""),
            (t("detail.project"), row.get("project") or t("project.no_name")),
            (t("group.members"), str(row.get("member_count") or 0)),
            (t("detail.status"), row.get("summary") or t("group.empty")),
        ]
    symbol, key = render.execution_badge(row.get("status") or "")
    status_text = _badge_text(symbol, key)
    duration = _duration_text(tower, row)
    if duration:
        status_text = f"{status_text} · {duration}"

    kind = row.get("attention") or "none"
    if kind == "approval_required":
        attention_text = _badge_text("!", "state.approval")
    elif kind == "input_required" or row.get("status") == "WAITING":
        _mark, attention_key = render.primary_badge(row)
        attention_text = _badge_text("?", attention_key)
    elif kind == "error":
        attention_text = _badge_text("!", "state.error")
    else:
        attention_text = "-"
    if row.get("result_state") == "ready":
        result_text = _badge_text("✓", "state.result")
    elif row.get("result_state") == "read":
        result_text = t("control.result_read")
    else:
        result_text = "-"

    fields = [
        (t("detail.task_name"), row.get("display_name") or row.get("task_name") or _project_text(row)),
        (t("detail.project"), _project_text(row)),
        (t("detail.agent"), row.get("agent")),
        (t("detail.role"), t(f'role.{row["role"]}') if row.get("role") in ROLE_IDS else t("role.unassigned")),
        (t("detail.status"), status_text),
        (t("detail.attention"), attention_text),
        (t("detail.result"), result_text),
        (t("detail.execution_host"), row.get("execution_host") or row.get("host") or t("state.unknown")),
    ]
    if tower.config.get("show_activity"):
        fields.append((t("detail.activity"), row.get("activity_text")))
    if advanced:
        fields.extend([
            (t("detail.advanced"), ""),
            (t("detail.pane_title"), row.get("pane_title")),
            (t("detail.path"), None if row.get("transport") == "ssh" else row.get("path")),
            (t("detail.project_path"), row.get("project_path")),
            (t("detail.result_source"), row.get("result_provider_endpoint") or row.get("result_provider_host") or row.get("execution_host") or row.get("host")),
            (t("detail.transport"), (
                f'SSH → {row.get("transport_target") or row.get("execution_host") or ""}'
                if row.get("transport") == "ssh"
                else (t("detail.peer_readonly") if row.get("remote") else t("detail.transport_local"))
            )),
        ])
        fields.extend(_location_fields(row))
        if row.get("window_id"):
            fields.append((t("detail.window_id"), row["window_id"]))
        if row.get("pane_id"):
            fields.append((t("detail.pane_id"), row["pane_id"]))
    return fields


def _build_selected_summary(tower: Tower, row: Dict) -> List[str]:
    """Short, user-facing summary for the default Work view."""
    if row.get("kind") in {"folder", "other_section"}:
        return [row.get("display_name") or "", row.get("summary") or t("group.empty")]
    if row.get("kind") == "window_asset":
        return [
            row.get("display_name") or t("folder.terminal"),
            t("folder.window_tasks").format(n=len(row.get("task_target_ids") or [])),
            row.get("summary") or t("group.empty"),
        ]
    if row.get("kind") == "work_group":
        return [
            row.get("display_name") or "",
            t("group.member_count").format(n=row.get("member_count") or 0),
            row.get("summary") or t("group.empty"),
        ]

    project = _project_text(row)
    name = row.get("display_name") or row.get("task_name") or project or t("task.terminal")
    role = t(f'role.{row["role"]}') if row.get("role") in ROLE_IDS else ""
    agent = row.get("agent") or t("task.terminal")
    identity = [label for label in (role, agent) if label]
    if role and role.casefold() in str(name).casefold():
        identity.remove(role)
    badges = " · ".join(_badge_text(symbol, key) for symbol, key in render.detail_badges(row))
    duration = _duration_text(tower, row)
    if duration and row.get("status") == "WORKING":
        badges = f"{badges} · {duration}"
    execution_host = row.get("execution_host") or row.get("host") or ""
    details = []
    if project:
        details.append(f'{t("detail.project")} {project}')
    if execution_host and execution_host.casefold() not in {"unknown", t("state.unknown").casefold()}:
        details.append(f'{t("detail.execution_host")} {execution_host}')
    identity_line = " · ".join(identity + ([badges] if badges else []))
    return [str(name), identity_line] + ([" · ".join(details)] if details else [])


def _enter_blocked_reason(row: Optional[Dict]) -> str:
    """Localization key for why a selected row cannot be opened with Enter."""

    if not row:
        return "enter.no_selection"
    if row.get("offline") or row.get("placeholder"):
        return "enter.offline"
    if row.get("kind") == "work_group_stale" or row.get("stale"):
        return "enter.stale"
    if row.get("kind") == "window" and row.get("remote"):
        return "enter.remote_readonly"
    if row.get("kind") == "window" and not row.get("window_id"):
        return "enter.window_missing"
    return "enter.unavailable"


def _selected_enter_hint(row: Optional[Dict]) -> str:
    intent = enter_intent(row)
    return {
        "toggle": t("hint.enter_expand"),
        "group": t("hint.enter_expand"),
        "control": t("hint.enter_conversation"),
        "window": t("hint.enter_window"),
        "zero": t("hint.enter_start"),
        "ignore": t("hint.enter_unavailable"),
    }.get(intent, t("hint.enter_unavailable"))


def _help_lines() -> List[str]:
    return [
        t("help.navigation"), t("help.actions"), t("help.saved"),
        t("help.results"), t("help.copy_safety"), t("help.copy_safety_block"),
        t("help.remote"), t("help.remote_limits"), t("help.manage"),
        t("help.detail"), t("help.detail_advanced"), t("help.detail_reject"),
        t("help.troubleshoot"), t("help.advanced"),
        t("help.start"), t("help.existing"), t("help.destinations"), t("help.exit"),
    ]


def _home_footer_lines(tower: Tower, width: int) -> List[str]:
    row = tower.visible_rows[tower.selected] if tower.visible_rows and 0 <= tower.selected < len(tower.visible_rows) else None
    enter_hint = _selected_enter_hint(row)
    keys = [t("hint.move"), enter_hint]
    if row and row.get("kind") == "pane":
        keys.append(t("hint.result"))
    keys.extend([
        t("hint.actions"),
        t("hint.user_work_view") if getattr(tower, "view_mode", USER_WORK_VIEW) == TERMINAL_STRUCTURE_VIEW else t("hint.create"),
        t("hint.help"),
    ])
    return render.wrap_items(keys, max(1, width - 4))


def _location_fields(row: Dict) -> List[Tuple[str, str]]:
    """Where this pane sits in tmux, from the row's own metadata."""

    fields: List[Tuple[str, str]] = []
    if row.get("session"):
        fields.append((t("detail.session"), str(row["session"])))
    if row.get("window_index") is not None and row.get("window_name") is not None and "window_index" in row:
        fields.append((t("detail.window"), f'{row.get("window_index")}: {row.get("window_name")}'))
    if "pane_index" in row and row.get("pane_index") is not None:
        fields.append((t("detail.pane_index"), str(row["pane_index"])))
    if "pane_active" in row:
        fields.append(
            (t("detail.active"), t("detail.active_yes") if row.get("pane_active") else t("detail.active_no"))
        )
    return fields


def _build_physical_lines(tower: Tower, narrow: bool) -> List[Dict]:
    """Flattens ``tower.visual`` (header/data items) into one entry per
    *physical* terminal line, since narrow rows can show project, task name,
    and agent/status separately -- viewport scrolling paginates over this
    list, not over ``visual``.
    """

    physical: List[Dict] = []

    for i, item in enumerate(tower.visual):
        if item["type"] == "header":
            physical.append({"kind": "header", "item_index": i, "host": item["host"], "level": item.get("level", "host")})
            continue

        row = item["row"]
        row_index = item["row_index"]
        physical.append({"kind": "primary", "row": row, "row_index": row_index})

        if narrow and row.get("kind") in {"folder", "other_section", "window_asset"}:
            physical.append({"kind": "group_summary", "row": row, "row_index": row_index})
        elif narrow and row.get("kind") in {"pane", "work_group_stale"}:
            physical.append({"kind": "agent_status", "row": row, "row_index": row_index})
        elif not narrow and row.get("activity_text"):
            physical.append({"kind": "activity", "row": row, "row_index": row_index})

    return physical


@trace_draw("home")
def draw(stdscr, tower: Tower, remote_state: str = "stopped") -> None:
    stdscr.erase()
    height, width = stdscr.getmaxyx()
    narrow = render.use_narrow_layout(width)

    # -- row 0: title + per-host mini summaries (right-aligned) ---------

    title = t("home.title")
    if getattr(tower, "view_mode", USER_WORK_VIEW) == TERMINAL_STRUCTURE_VIEW:
        title = f'{title} · {t("nav.terminal_structure")}'
    safe_add(stdscr, 0, 2, title, curses.A_BOLD)
    from .remote_menu import badge_text

    view_mode = getattr(tower, "view_mode", USER_WORK_VIEW)
    badge = badge_text(remote_state) if view_mode == TERMINAL_STRUCTURE_VIEW else ""
    badge_x = 2 + render.display_width(title) + 3
    if badge:
        safe_add(stdscr, 0, badge_x, badge, curses.A_DIM)

    host_line = render.format_watch_header(render.watch_counts(tower.visible_rows), t)
    if host_line and not narrow:
        min_x = badge_x + render.display_width(badge) + (2 if badge else 0)
        x = max(min_x, width - render.display_width(host_line) - 2)
        if x + render.display_width(host_line) < width:
            safe_add(stdscr, 0, x, host_line, curses.A_BOLD)

    # -- row 1: access and copy context ----------------------------------
    context = getattr(tower, "access_context", None)
    if context:
        preference = load_preference()
        if preference == COPY_AUTO:
            destination = (
                context.display_name
                if context.clipboard_capability in {"host", "bridge"}
                else t("access.unknown_destination")
            )
            access_line = t("access.copy_auto", client=context.display_name, destination=destination)
        else:
            destination = {
                LOCAL_HOST: context.display_name,
                CURRENT_TERMINAL: t("access.terminal"),
                TMUX_BUFFER: t("access.tower_buffer"),
            }.get(preference, t("access.unknown_destination"))
            access_line = t("access.copy_manual", client=context.display_name, destination=destination)
    else:
        access_line = t("access.unknown_context")
    safe_add(stdscr, 1, 2, render.truncate_to_width(access_line, max(0, width - 4)), curses.A_DIM)
    start_y = 2

    if tower.visible_rows and all(row.get("kind") == "zero" for row in tower.visible_rows):
        safe_add(stdscr, start_y, 2, t("zero.title"), curses.A_BOLD)
        safe_add(stdscr, start_y + 1, 2, render.truncate_to_width(t("zero.description"), max(0, width - 4)), curses.A_DIM)
        start_y += 2
    footer_lines = _home_footer_lines(tower, width)
    footer_y = max(0, height - len(footer_lines) - (1 if tower.notice else 0))

    # -- selected-item detail, immediately below the work list -----------

    detail_lines: List[str] = []
    if (render.should_show_detail_panel(height) and tower.visible_rows
            and tower.visible_rows[tower.selected].get("kind") != "zero"):
        selected_row = tower.visible_rows[tower.selected]
        if getattr(tower, "view_mode", USER_WORK_VIEW) == TERMINAL_STRUCTURE_VIEW:
            detail_fields = _build_detail_fields(tower, selected_row, advanced=True)
        else:
            detail_fields = []
        detail_lines = (
            render.format_detail_panel(detail_fields)
            if detail_fields
            else _build_selected_summary(tower, selected_row)
        )

    detail_block = (1 + 1 + len(detail_lines)) if detail_lines else 0
    max_rows = max(0, footer_y - start_y - detail_block)

    if not tower.visible_rows:
        msg = t("wizard.no_matches") if tower.filter_text else t("empty.no_panes")
        safe_add(stdscr, start_y, 2, msg)
        for index, line in enumerate(footer_lines):
            safe_add(stdscr, footer_y + index, 2, line, curses.A_DIM)
        stdscr.noutrefresh()
        curses.doupdate()
        return

    physical = _build_physical_lines(tower, narrow)

    selected_phys_index = 0
    for i, p in enumerate(physical):
        if p["kind"] == "primary" and p["row_index"] == tower.selected:
            selected_phys_index = i
            break

    top = 0
    if selected_phys_index >= max_rows:
        top = selected_phys_index - max_rows + 1

    visible_physical = physical[top : top + max_rows]

    for offset, p in enumerate(visible_physical):
        y = start_y + offset

        if p["kind"] == "header":
            if p.get("level") == "window":
                # A window is a divider above its panes, not a row.
                label = f'  ┄ {p["host"]} '
                safe_add(stdscr, y, 0, label + "┄" * max(0, width - render.display_width(label) - 1), curses.A_DIM)
                continue
            label = f'── {p["host"]} '
            safe_add(stdscr, y, 0, label + "─" * max(0, width - render.display_width(label) - 1), curses.A_BOLD)
            continue

        row = p["row"]
        is_selected = p["row_index"] == tower.selected

        user_view = view_mode == USER_WORK_VIEW
        base_attr = (curses.A_BOLD if is_selected else 0) if user_view else curses.A_REVERSE
        if not user_view and is_selected and curses.has_colors():
            base_attr = curses.color_pair(6) | curses.A_BOLD

        if is_selected and not user_view:
            safe_add(stdscr, y, 0, " " * max(1, width - 1), base_attr)

        if p["kind"] == "primary":
            parts = render.list_row_parts(row, width, t, _duration_text(tower, row))
            prefix = ("› " if is_selected else "  ") + (parts["guide"] or "") if user_view else (parts["guide"] or " ")
            project_text = parts["project"] if row.get("kind") != "zero" else _project_text(row)
            if parts.get("task") and (narrow or row.get("kind") != "work_group"):
                project_text = f'{project_text} · {parts["task"]}'
            state_text = parts["badge"]
            agent_text = parts["agent"]
            state_width = render.display_width(state_text)
            prefix_width = render.display_width(prefix)
            state_reserve = state_width + 2 if state_text else 0
            agent_reserve = render.display_width(agent_text) + 2 if agent_text else 0
            project_budget = max(8, width - prefix_width - state_reserve - agent_reserve - 4)
            text = render.truncate_to_width(project_text, project_budget)

            safe_add(stdscr, y, 0, prefix, base_attr if is_selected else (
                curses.A_BOLD if row.get("kind") in {"folder", "other_section", "window_asset"} else 0
            ))
            safe_add(stdscr, y, prefix_width, text, base_attr)

            if agent_text:
                agent_x = prefix_width + render.display_width(text) + 2
                safe_add(stdscr, y, agent_x, agent_text, base_attr)
            state_x = width - state_width - 2
            if state_text and state_x > prefix_width + 1:
                if row.get("kind") in {"window", "folder", "other_section", "window_asset", "work_group"}:
                    status_attr_here = base_attr if is_selected else curses.A_DIM
                else:
                    status_attr_here = base_attr if is_selected else status_attr(row.get("status") or "")
                safe_add(stdscr, y, state_x, state_text, status_attr_here)

        elif p["kind"] == "activity":
            attr = base_attr if is_selected else curses.A_DIM
            indent = render.display_width(row.get("guide") or "") or 4
            safe_add(stdscr, y, indent, row["activity_text"], attr)

        elif p["kind"] == "agent_status":
            _symbol, key = render.primary_badge(row)
            state = f'{_symbol} {t(key)}'
            duration = _duration_text(tower, row)
            if duration:
                state += " · " + duration
            role = t(f'role.{row["role"]}') if row.get("role") in ROLE_IDS else ""
            line = render.agent_status_line(row.get("agent") or "-", state, role=role)
            indent = render.display_width(row.get("guide") or "") + 2
            safe_add(stdscr, y, indent, line, base_attr if is_selected else curses.A_DIM)

        elif p["kind"] == "group_summary":
            indent = render.display_width(row.get("guide") or "") + 2
            summary = row.get("summary_compact") if narrow else row.get("summary")
            safe_add(stdscr, y, indent, summary or t("group.empty"), base_attr if is_selected else curses.A_DIM)

    # -- detail panel ------------------------------------------------------

    if detail_lines and visible_physical:
        divider_y = footer_y - detail_block
        safe_add(stdscr, divider_y, 0, "─" * max(0, width - 1), curses.A_DIM)
        safe_add(stdscr, divider_y + 1, 2, t("detail.title"), curses.A_BOLD)
        for i, line in enumerate(detail_lines):
            safe_add(stdscr, divider_y + 2 + i, 2, render.truncate_to_width(line, max(0, width - 4)))

    if tower.notice:
        safe_add(stdscr, footer_y, 2, render.truncate_to_width(tower.notice, max(0, width - 4)), curses.A_BOLD)
    for index, line in enumerate(footer_lines):
        safe_add(stdscr, footer_y + index + (1 if tower.notice else 0), 2, line, curses.A_DIM)

    stdscr.noutrefresh()
    curses.doupdate()


def main(stdscr, session: Optional[str] = None) -> None:
    setup_colors()
    stdscr.keypad(True)
    _set_input_delay()
    stdscr.timeout(25)
    curses.noecho()
    curses.cbreak()
    _disable_xon_flow_control()

    try:
        curses.curs_set(0)
    except curses.error:
        pass

    session = session or tmux_capture.current_session()
    if not session:
        raise SystemExit(t("cli.no_tmux_session"))

    own_pane_id = tmux_capture.current_pane_id()
    if own_pane_id:
        import tmux_agent_tower

        registration.register(
            session,
            own_pane_id,
            pid=str(os.getpid()),
            source=os.path.realpath(tmux_agent_tower.__file__),
        )

    try:
        _run_loop(stdscr, session, own_pane_id)
    finally:
        # Remote keeps running after Q. Stopping it is M → 원격 종료.
        from ..server import service

        service.on_tui_exit()
        if own_pane_id:
            registration.unregister_if_self(session, own_pane_id)


def _remote_state() -> str:
    from ..server import service

    try:
        return service.status().state
    except Exception:
        return "error"


def _open_selected_row(stdscr, tower) -> None:
    row = tower.visible_rows[tower.selected] if tower.visible_rows else None
    intent = enter_intent(row)
    if intent == "control" and row:
        event("VIEW_STATE_CHANGED", view="conversation")
    if intent == "group" and row:
        tower.toggle_work_group(row.get("group_id") or "")
    elif intent == "toggle" and row:
        if row.get("kind") in {"folder", "other_section"}:
            tower.toggle_folder(row.get("folder_id") or "__unfiled__")
        elif row.get("kind") == "window_asset":
            tower.toggle_window_asset(row.get("window_ref") or "")
    elif intent == "control":
        pane_key = tower.control_key()
        if pane_key:
            from .control_view import open_control_view

            open_control_view(stdscr, tower, pane_key)
    elif intent == "window" and row:
        from .structure_menu import open_window_control

        open_window_control(stdscr, tower, row.get("window_id") or "")
    elif intent == "zero" and row:
        from .structure_menu import run_zero_action

        run_zero_action(stdscr, tower, row.get("action") or "")
    elif intent == "ignore" and row:
        tower.notice = t(_enter_blocked_reason(row))


def _show_selected_result_copy(stdscr, tower) -> None:
    row = tower.visible_rows[tower.selected] if tower.visible_rows else None
    from .widgets import show_message_screen

    if not row or row.get("kind") != "pane" or not row.get("key"):
        show_message_screen(stdscr, t("nav.copy_result"), [t("control.copy_requires_pane")])
        return

    from .control_view import _copy_result

    message = _copy_result(tower, row["key"], stdscr)
    show_message_screen(stdscr, t("detail.result"), [message or t("control.no_result")])


def _run_loop(stdscr, session: str, own_pane_id: str) -> None:
    from ..server import service

    tower = Tower(session, own_pane_id, results=ResultTracker(shared_result_state_path()))
    tower.load()
    tower.last_refresh = time.monotonic()
    try:
        # Autostart binds the remote to this Tower's session -- never a guess.
        service.maybe_autostart(session=tower.session, own_pane_id=tower.own_pane_id)
    except Exception:
        pass
    remote_state = _remote_state()
    draw(stdscr, tower, remote_state=remote_state)
    needs_draw = False

    while True:
        if _apply_background_refresh(tower):
            remote_state = tower.remote_state or remote_state
            needs_draw = True
        now = time.monotonic()

        sync_titles = getattr(tower, "sync_opted_in_pane_titles", None)
        if callable(sync_titles) and sync_titles(now):
            needs_draw = True

        if now - tower.last_refresh >= REFRESH_SECONDS:
            _request_background_refresh(tower)

        if needs_draw:
            draw(stdscr, tower, remote_state=remote_state)
            needs_draw = False

        try:
            stdscr.timeout(25)
            key = read_key(stdscr)
        except KeyboardInterrupt:
            break

        if key == -1:
            continue

        if key == curses.KEY_RESIZE:
            draw(stdscr, tower, remote_state=remote_state)
            continue

        if is_ctrl_c(key):
            break

        if key == "/":
            from .palette import open_palette

            open_palette(stdscr, tower)
            _request_background_refresh(tower)
            draw(stdscr, tower, remote_state=remote_state)
            continue

        if is_escape(key) and tower.filter_text:
            tower.clear_filter()
            draw(stdscr, tower, remote_state=remote_state)
            continue

        if key == curses.KEY_LEFT:
            tower.collapse_selected()
            draw(stdscr, tower, remote_state=remote_state)
            continue

        if key == curses.KEY_RIGHT:
            tower.expand_selected()
            draw(stdscr, tower, remote_state=remote_state)
            continue

        if key == curses.KEY_UP or matches_letter(key, "k"):
            tower.move_up()
            draw(stdscr, tower, remote_state=remote_state)
            continue

        if key == curses.KEY_DOWN or matches_letter(key, "j"):
            tower.move_down()
            draw(stdscr, tower, remote_state=remote_state)
            continue

        if is_enter(key):
            _open_selected_row(stdscr, tower)
            _request_background_refresh(tower)
            draw(stdscr, tower, remote_state=remote_state)
            continue

        if key == " ":
            event("VIEW_STATE_CHANGED", view="menu")
            from .structure_menu import open_context_menu

            open_context_menu(stdscr, tower)
            _request_background_refresh(tower)
            draw(stdscr, tower, remote_state=remote_state)
            continue

        if key == "+":
            from .structure_menu import start_task

            start_task(stdscr, tower)
            _request_background_refresh(tower)
            draw(stdscr, tower, remote_state=remote_state)
            continue

        if matches_letter(key, "s"):
            from .worksets import open_saved_collection

            open_saved_collection(stdscr, tower, STATE_DIR)
            _request_background_refresh(tower)
            draw(stdscr, tower, remote_state=remote_state)
            continue

        if matches_letter(key, "y"):
            _show_selected_result_copy(stdscr, tower)
            draw(stdscr, tower, remote_state=remote_state)
            continue

        if matches_letter(key, "g"):
            row = tower.visible_rows[tower.selected] if tower.visible_rows else None
            if row and row.get("kind") == "pane":
                from .control_view import _go
                from .widgets import show_message_screen

                message = _go(tower, row)
                if message != t("control.focused"):
                    show_message_screen(stdscr, t("nav.terminal_structure"), [message])
            draw(stdscr, tower, remote_state=remote_state)
            continue

        if key == "?":
            from .widgets import show_message_screen

            show_message_screen(stdscr, t("help.title"), _help_lines(), paginate=True)
            draw(stdscr, tower, remote_state=remote_state)
            continue

        if matches_letter(key, "l"):
            row = tower.visible_rows[tower.selected] if tower.visible_rows else None
            if row and row.get("kind") in {"folder", "other_section"}:
                from .live_view import open_folder_live_view

                open_folder_live_view(stdscr, tower, row)
            elif row and row.get("kind") == "work_group":
                from .live_view import open_group_live_view

                store = getattr(tower, "work_groups", None)
                group = next((item for item in store.all()
                              if item.get("group_id") == row.get("group_id")), row) if store else row
                open_group_live_view(stdscr, tower, group)
            elif row and row.get("kind") == "window_asset":
                from .live_view import open_window_live_view

                open_window_live_view(stdscr, tower, row.get("window_ref") or "")
            elif row and row.get("kind") == "pane" and row.get("key"):
                from .live_view import open_live_view

                open_live_view(stdscr, tower, row["key"])
            else:
                from .live_view import open_live_view

                open_live_view(stdscr, tower)
            _request_background_refresh(tower)
            draw(stdscr, tower, remote_state=remote_state)
            continue

        if matches_letter(key, "e"):
            tower.rename_selected(stdscr)
            draw(stdscr, tower, remote_state=remote_state)
            continue

        if matches_letter(key, "r"):
            _request_background_refresh(tower)
            continue

        if matches_letter(key, "a"):
            tower.toggle_attention()
            draw(stdscr, tower, remote_state=remote_state)
            continue

        if matches_letter(key, "v"):
            tower.toggle_navigator()
            draw(stdscr, tower, remote_state=remote_state)
            continue

        if matches_letter(key, "m"):
            from .remote_menu import open_remote_menu

            open_remote_menu(stdscr, tower)
            remote_state = _remote_state()
            draw(stdscr, tower, remote_state=remote_state)
            continue

        if matches_letter(key, "c"):
            from .settings_menu import open_settings

            open_settings(stdscr, tower)
            draw(stdscr, tower, remote_state=remote_state)
            continue

        if matches_letter(key, "n"):
            from .structure_menu import open_create_hub

            open_create_hub(stdscr, tower)
            _request_background_refresh(tower)
            draw(stdscr, tower, remote_state=remote_state)
            continue

        if matches_letter(key, "w"):
            from .structure_menu import open_create_hub

            open_create_hub(stdscr, tower)
            _request_background_refresh(tower)
            draw(stdscr, tower, remote_state=remote_state)
            continue

        if matches_letter(key, "q"):
            break


def run() -> None:
    # Must happen before curses.wrapper()'s initscr() for ncurses to fully
    # honor the terminal's UTF-8 locale in get_wch() (see widgets.read_key).
    # The default Esc delay is about a second, so Back felt ignored.
    import locale
    import os

    locale.setlocale(locale.LC_ALL, "")
    os.environ.setdefault("ESCDELAY", "25")

    try:
        curses.wrapper(main)
    except KeyboardInterrupt:
        pass


def _set_input_delay() -> None:
    setter = getattr(curses, "set_escdelay", None)
    if setter:
        try:
            setter(25)
        except curses.error:
            pass


def _request_background_refresh(tower) -> bool:
    start = getattr(tower, "start_background_refresh", None)
    if callable(start):
        return bool(start())
    tower.load()
    tower.last_refresh = time.monotonic()
    return True


def _apply_background_refresh(tower) -> bool:
    apply = getattr(tower, "apply_background_refresh", None)
    return bool(apply()) if callable(apply) else False
