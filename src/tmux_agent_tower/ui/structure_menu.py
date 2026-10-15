"""Local tmux structure: create, move, rename, layout, and close.

Phone remote does not call this module. Every write goes through
``tmux.structure``, which targets a window id or a pane id. Names are
labels. Destructive actions ask first, and cancel is the default.
"""

from __future__ import annotations

import os
import time
from typing import List, Optional

from ..i18n import t
from ..state.overrides import ROLE_IDS
from ..state.worksets import LAYOUTS
from ..tmux import structure
from .widgets import prompt_text, run_list_picker, show_message_screen


_STATUS_MARK = {
    "WORKING": "●",
    "IDLE": "○",
    "WAITING": "!",
    "UNKNOWN": "?",
    "DEAD": "✕",
}


def window_close_summary(window_name: str, panes: List[dict]) -> List[str]:
    """What the close screen shows before the user confirms.

    Show task names, Agents, and state without exposing tmux identifiers.
    """

    name = window_name or "-"
    lines = [
        t("struct.close_window_ask").format(name=name),
        t("struct.window_panes").format(n=len(panes)),
    ]
    for pane in panes:
        attention = pane.get("attention") or "none"
        status = pane.get("status") or ""
        mark = _STATUS_MARK.get(status, "○")
        state = t(f"status.{status}") if status else "-"
        if attention == "approval_required":
            mark = "!"
            state = t("state.approval")
        elif attention == "input_required":
            state = f"{state}  {t('state.input')}"
        project = pane.get("task_name") or pane.get("project") or t("project.no_name")
        agent = pane.get("agent") or "-"
        lines.append(f"{mark} {project} / {agent}  {state}".rstrip())
    if panes:
        lines.append(t("struct.close_window_effect").format(n=len(panes)))
    return lines


def run_zero_action(stdscr, tower, action: str) -> None:
    if action == "task":
        start_task(stdscr, tower)
    elif action == "window":
        create_blank_window(stdscr, tower)
    elif action == "pane":
        create_blank_pane(stdscr, tower)
    elif action == "workspace":
        from .tower import STATE_DIR
        from .launcher_wizard import run_launcher

        run_launcher(stdscr, tower, multi=True, state_dir=STATE_DIR)
    elif action == "saved":
        from .tower import STATE_DIR
        from .worksets import open_saved_collection

        open_saved_collection(stdscr, tower, STATE_DIR)
    elif action == "template":
        from .tower import STATE_DIR
        from .worksets import open_work_templates

        open_work_templates(stdscr, tower, STATE_DIR)
    elif action == "workflow":
        from .workflow_presets import open_workflow_presets
        from .tower import STATE_DIR

        open_workflow_presets(stdscr, tower, STATE_DIR)
    elif action == "group":
        create_work_group(stdscr, tower)
    elif action == "folder":
        create_folder(stdscr, tower)


def open_create_hub(stdscr, tower) -> None:
    pick = run_list_picker(
        stdscr,
        t("struct.create_title"),
        [
            ("task", t("struct.create_task")),
            ("folder", t("folder.create")),
            ("group", t("group.create")),
            ("saved", t("struct.create_saved")),
            ("template", t("struct.create_template")),
            ("advanced", t("settings.advanced")),
            ("cancel", t("menu.cancel")),
        ],
        footer_hint=t("wizard.hint_list"),
    )
    if pick.cancelled or pick.selected_key in (None, "cancel"):
        return
    if pick.selected_key == "task":
        start_task(stdscr, tower)
    elif pick.selected_key == "folder":
        create_folder(stdscr, tower)
    elif pick.selected_key == "window":
        create_empty_window(stdscr, tower)
    elif pick.selected_key == "pane":
        add_pane(stdscr, tower, _preferred_window(tower))
    elif pick.selected_key == "workspace":
        run_zero_action(stdscr, tower, "workspace")
    elif pick.selected_key == "saved":
        run_zero_action(stdscr, tower, "saved")
    elif pick.selected_key == "template":
        run_zero_action(stdscr, tower, "template")
    elif pick.selected_key == "workflow":
        run_zero_action(stdscr, tower, "workflow")
    elif pick.selected_key == "group":
        create_work_group(stdscr, tower)
    elif pick.selected_key == "advanced":
        extra = run_list_picker(stdscr, t("settings.advanced"), [
            ("workspace", t("struct.create_workspace")),
            ("workflow", t("struct.create_workflow")),
            ("terminal", t("nav.terminal_structure")),
            ("cancel", t("menu.cancel")),
        ], footer_hint=t("wizard.hint_list"))
        if extra.selected_key == "workspace":
            run_zero_action(stdscr, tower, "workspace")
        elif extra.selected_key == "workflow":
            run_zero_action(stdscr, tower, "workflow")
        elif extra.selected_key == "terminal":
            tower.toggle_navigator()


def open_saved_entry(stdscr, tower) -> None:
    from .tower import STATE_DIR
    from .worksets import open_saved_collection

    open_saved_collection(stdscr, tower, STATE_DIR)


def open_window_control(stdscr, tower, window_id: str) -> None:
    """Enter on a window. Does not select that window in tmux."""

    if not structure.valid_window_id(window_id):
        show_message_screen(stdscr, t("struct.failed"), [t("nav.stale")])
        return
    while True:
        meta = _window_meta(tower, window_id)
        pick = run_list_picker(
            stdscr,
            t("struct.window_title"),
            [
                ("add", t("struct.add_pane")),
                ("new", t("struct.create_window")),
                ("rename", t("struct.rename")),
                ("layout", t("struct.layout")),
                ("panes", t("struct.manage_panes")),
                ("close", t("struct.close_window")),
                ("cancel", t("menu.cancel")),
            ],
            footer_hint=t("wizard.hint_list"),
            preamble=[meta.get("window_name") or t("project.no_name"), t("struct.window_panes").format(n=meta.get("pane_count") or 0)],
        )
        if pick.cancelled or pick.selected_key in (None, "cancel"):
            return
        if pick.selected_key == "add":
            add_pane(stdscr, tower, window_id)
        elif pick.selected_key == "new":
            create_empty_window(stdscr, tower)
        elif pick.selected_key == "rename":
            rename_window(stdscr, tower, window_id)
        elif pick.selected_key == "layout":
            choose_layout(stdscr, tower, window_id)
        elif pick.selected_key == "panes":
            if _manage_panes(stdscr, tower, window_id) == "gone":
                return
        elif pick.selected_key == "close":
            if close_window(stdscr, tower, window_id):
                return
        tower.load()


def open_context_menu(stdscr, tower) -> None:
    if not tower.visible_rows:
        open_create_hub(stdscr, tower)
        return
    row = tower.visible_rows[tower.selected]
    kind = row.get("kind")
    if kind in {"folder", "other_section"}:
        try:
            open_folder_menu(stdscr, tower, row)
        except (KeyError, OSError, ValueError):
            show_message_screen(stdscr, t("folder.save_failed"), [])
        return
    if kind == "window_asset":
        try:
            open_window_asset_menu(stdscr, tower, row)
        except (KeyError, OSError, ValueError):
            show_message_screen(stdscr, t("folder.save_failed"), [])
        return
    if kind == "work_group":
        open_work_group_menu(stdscr, tower, row)
    elif kind == "work_group_stale":
        return
    elif kind == "window":
        _window_menu(stdscr, tower, row)
    elif kind == "zero":
        run_zero_action(stdscr, tower, row.get("action") or "")
    else:
        open_pane_menu(stdscr, tower, row)


def create_folder(stdscr, tower) -> None:
    name = prompt_text(stdscr, t("folder.name_prompt"))
    if name is None:
        return
    try:
        tower.folders.create(name)
    except (OSError, ValueError):
        show_message_screen(stdscr, t("folder.save_failed"), [])


def open_folder_menu(stdscr, tower, row: dict) -> None:
    folder_id = row.get("folder_id") or ""
    if row.get("kind") == "other_section":
        menu = [("create", t("folder.create")), ("cancel", t("menu.cancel"))]
        pick = run_list_picker(
            stdscr, row.get("display_name") or t("folder.other"), menu,
            footer_hint=t("wizard.hint_list"),
        )
        if pick.selected_key == "create":
            create_folder(stdscr, tower)
        return
    pick = run_list_picker(stdscr, row.get("display_name") or t("folder.title"), [
        ("rename", t("folder.rename")),
        ("new_window", t("folder.add_window")),
        ("move_in", t("folder.move_window_in")),
        ("order", t("folder.reorder")),
        ("delete", t("folder.delete")),
        ("cancel", t("menu.cancel")),
    ], footer_hint=t("wizard.hint_list"))
    if pick.cancelled or pick.selected_key in (None, "cancel"):
        return
    if pick.selected_key == "rename":
        name = prompt_text(stdscr, t("folder.rename_prompt"), initial=row.get("display_name") or "")
        if name:
            tower.folders.rename(folder_id, name)
    elif pick.selected_key == "new_window":
        before = {item.get("window_ref") for item in tower.window_assets}
        create_empty_window(stdscr, tower)
        tower.load()
        created = [item for item in tower.window_assets if item.get("window_ref") not in before]
        if len(created) == 1:
            tower.folders.move_window(created[0]["window_ref"], folder_id)
    elif pick.selected_key == "move_in":
        choices = [(item["window_ref"], item["display_name"]) for item in tower.window_assets
                   if item["window_ref"] not in row.get("window_refs", [])]
        if not choices:
            show_message_screen(stdscr, t("folder.no_windows"), [])
            return
        selected = run_list_picker(stdscr, t("folder.move_window_in"), choices, searchable=True,
                                   footer_hint=t("wizard.hint_single_search"))
        if not selected.cancelled and selected.selected_key:
            tower.folders.move_window(selected.selected_key, folder_id)
    elif pick.selected_key == "order":
        order = run_list_picker(stdscr, t("folder.reorder"), [
            ("up", t("folder.move_up")), ("down", t("folder.move_down")),
            ("top", t("folder.move_top")), ("bottom", t("folder.move_bottom")),
        ], footer_hint=t("wizard.hint_list"))
        if order.selected_key:
            tower.folders.reorder_folder(folder_id, order.selected_key)
    elif pick.selected_key == "delete":
        if _confirm(stdscr, t("folder.delete"), [t("folder.delete_effect")]):
            tower.folders.delete(folder_id)


def open_window_asset_menu(stdscr, tower, row: dict) -> None:
    pick = run_list_picker(stdscr, row.get("display_name") or t("folder.terminal"), [
        ("live", t("nav.live_view")),
        ("rename", t("folder.window_rename")),
        ("move", t("folder.move_window")),
        ("order", t("folder.reorder")),
        ("add", t("folder.add_task")),
        ("layout", t("folder.layout")),
        ("more", t("menu.more")),
        ("cancel", t("menu.cancel")),
    ], footer_hint=t("wizard.hint_list"))
    if pick.cancelled or pick.selected_key in (None, "cancel"):
        return
    if pick.selected_key == "live":
        from .live_view import open_window_live_view

        open_window_live_view(stdscr, tower, row.get("window_ref") or "")
    elif pick.selected_key == "rename":
        tower.rename_selected(stdscr)
    elif pick.selected_key == "move":
        items = [("__unfiled__", t("folder.other"))]
        items.extend((folder["folder_id"], folder["display_name"]) for folder in tower.folders.all()
                     if folder["folder_id"] != row.get("folder_id"))
        items.append(("new", t("folder.create")))
        destination = run_list_picker(stdscr, t("folder.move_window"), items, footer_hint=t("wizard.hint_list"))
        if destination.selected_key == "new":
            before = {folder["folder_id"] for folder in tower.folders.all()}
            create_folder(stdscr, tower)
            created = next((folder for folder in tower.folders.all()
                            if folder["folder_id"] not in before), None)
            if created is None:
                return
            destination_id = created["folder_id"]
        elif destination.cancelled:
            return
        else:
            destination_id = None if destination.selected_key == "__unfiled__" else destination.selected_key
        tower.folders.move_window(row["window_ref"], destination_id)
    elif pick.selected_key == "order":
        if not row.get("folder_id"):
            return
        order = run_list_picker(stdscr, t("folder.reorder"), [
            ("up", t("folder.move_up")), ("down", t("folder.move_down")),
            ("top", t("folder.move_top")), ("bottom", t("folder.move_bottom")),
        ], footer_hint=t("wizard.hint_list"))
        if order.selected_key:
            tower.folders.reorder_window(row["window_ref"], order.selected_key)
    elif pick.selected_key == "add":
        add_pane(stdscr, tower, row.get("window_id") or "")
    elif pick.selected_key == "layout":
        choose_layout(stdscr, tower, row.get("window_id") or "")
    elif pick.selected_key == "more":
        open_window_control(stdscr, tower, row.get("window_id") or "")


def open_pane_menu(stdscr, tower, row: dict) -> str:
    """Pane actions. Returns ``closed`` when the pane was killed."""

    if not row.get("key") or not row.get("pane_id"):
        show_message_screen(stdscr, t("control.remote_unsupported"), [])
        return ""
    local = not row.get("remote") and str(row.get("pane_id") or "").startswith("%")
    items = [
        ("control", t("nav.conversation")),
        ("result", t("nav.show_result")),
        ("copy", t("nav.copy_result")),
        ("edit", t("nav.task_settings")),
        ("move", t("menu.move_task")),
        ("layout", t("layout.change")),
        ("more", t("menu.more")),
    ]
    work_groups = getattr(tower, "work_groups", None)
    membership = work_groups.membership() if work_groups is not None else {}
    group_id = membership.get(str(row.get("target_id") or ""))
    if group_id:
        items.insert(-1, ("work_group", t("group.label")))
    if row.get("remote"):
        items.append(("role", t("role.change")))
    items.append(("cancel", t("menu.cancel")))
    pick = run_list_picker(
        stdscr,
        t("struct.pane_menu"),
        items,
        footer_hint=t("wizard.hint_list"),
        preamble=[
            row.get("task_name") or row.get("project") or "-",
            f'{t("detail.agent")}  {row.get("agent") or "-"}',
        ],
    )
    if pick.cancelled or pick.selected_key in (None, "cancel"):
        return ""
    if pick.selected_key == "control":
        from .control_view import open_control_view

        open_control_view(stdscr, tower, row.get("key"))
    elif pick.selected_key == "result":
        from .control_view import open_control_view

        open_control_view(stdscr, tower, row.get("key"), show_result_on_open=True)
    elif pick.selected_key == "copy":
        from .control_view import _copy_result

        message = _copy_result(tower, row.get("key"), stdscr)
        show_message_screen(stdscr, t("detail.result"), [message or t("control.no_result")])
    elif pick.selected_key == "role":
        change_role(stdscr, tower, row)
    elif pick.selected_key == "edit":
        tower.edit_selected(stdscr)
    elif pick.selected_key == "move":
        move_work(stdscr, tower, row)
    elif pick.selected_key == "layout":
        open_target_layout(stdscr, tower, row)
    elif pick.selected_key == "work_group":
        open_work_group_menu(stdscr, tower, {"group_id": group_id})
    elif pick.selected_key == "more":
        more_items = [
            ("saved", t("struct.create_saved")),
            ("live", t("nav.live_view")),
            ("settings", t("nav.settings")),
            ("terminal", t("nav.terminal_structure")),
            ("prompt", t("struct.act_prompt")),
            ("screen_copy", t("nav.copy_screen")),
            ("diagnostics", t("nav.diagnostics")),
        ]
        if local:
            more_items.extend([
                ("physical", t("layout.physical_move")),
                ("close", t("nav.end_task")),
            ])
        if row.get("target_id") and row.get("target_id") not in tower.work_groups.membership():
            more_items.append(("group", t("group.create_one")))
        more_items.append(("cancel", t("menu.cancel")))
        more = run_list_picker(stdscr, t("menu.more"), more_items, footer_hint=t("wizard.hint_list"))
        action = more.selected_key
        if action == "saved":
            open_saved_entry(stdscr, tower)
        elif action == "live":
            from .live_view import open_live_view

            open_live_view(stdscr, tower)
        elif action == "settings":
            from .settings_menu import open_settings

            open_settings(stdscr, tower)
        elif action == "terminal":
            tower.toggle_navigator()
        elif action == "prompt":
            from .control_view import _prompt

            _prompt(stdscr, tower, row)
        elif action == "screen_copy":
            from .control_view import _copy_screen

            message = _copy_screen(
                tower, row.get("key"), stdscr,
                expected_pane_pid=str(row.get("pane_pid") or ""),
            )
            show_message_screen(stdscr, t("nav.copy_screen"), [message or t("control.no_screen")])
        elif action == "diagnostics":
            from .control_view import pane_diagnostic_lines

            show_message_screen(stdscr, t("diagnostics.title"), pane_diagnostic_lines(tower, row))
        elif action == "physical":
            move_pane_physical(stdscr, tower, row)
        elif action == "close":
            from .control_view import _close

            if _close(stdscr, tower, row):
                return "closed"
        elif action == "group":
            create_work_group(stdscr, tower, [row.get("target_id")])
    return ""


def _work_label(row: dict) -> str:
    role = t(f'role.{row["role"]}') if row.get("role") in ROLE_IDS else ""
    return " · ".join(part for part in (
        row.get("display_name") or row.get("task_name") or row.get("project") or t("group.stale_member"),
        role,
        row.get("agent") or "",
    ) if part)


def _ordered_targets(rows: List[dict], target_ids) -> List[str]:
    role_order = {role: index for index, role in enumerate((*ROLE_IDS, "__none__"))}
    selected = set(target_ids)
    matching = [row for row in rows if row.get("target_id") in selected]
    matching.sort(key=lambda row: role_order.get(row.get("role") or "__none__", len(role_order)))
    return [row["target_id"] for row in matching]


def _write_group(stdscr, callback, *args, **kwargs):
    try:
        return callback(*args, **kwargs)
    except (OSError, ValueError, KeyError):
        show_message_screen(stdscr, t("group.save_failed"), [])
        return None


def _pick_targets(stdscr, title: str, rows: List[dict], preamble: List[str]):
    choices = [(row["target_id"], _work_label(row)) for row in rows if row.get("target_id")]
    if not choices:
        show_message_screen(stdscr, title, [t("wizard.no_matches")])
        return set()
    pick = run_list_picker(
        stdscr, title, choices, multi=True, searchable=True,
        footer_hint=t("wizard.hint_list") + "   Space " + t("group.select_hint"),
        preamble=preamble,
    )
    return set() if pick.cancelled else pick.selected_keys


def create_work_group(stdscr, tower, initial_target_ids=None) -> None:
    tower.load()
    occupied = tower.work_groups.membership()
    rows = [row for row in tower.rows if row.get("pane_id") and row.get("target_id") and row.get("target_id") not in occupied]
    if initial_target_ids is None:
        selected = _pick_targets(stdscr, t("group.create"), rows, [t("group.pick_create")])
    else:
        selected = set(initial_target_ids)
    if not selected:
        return
    labels = {row["target_id"]: _work_label(row) for row in rows if row.get("target_id") in selected}
    name = prompt_text(stdscr, t("group.name_prompt"))
    if not name:
        return
    members = _ordered_targets(rows, selected)
    projects = {str(row.get("project") or "") for row in rows if row.get("target_id") in selected}
    project_binding = None
    if len(projects) == 1 and next(iter(projects)):
        project_name = next(iter(projects))
        paths = {str(row.get("path") or "") for row in rows if row.get("target_id") in selected}
        project_binding = {"name": project_name}
        if len(paths) == 1 and next(iter(paths)):
            project_binding["path"] = next(iter(paths))
    _write_group(stdscr, tower.work_groups.create, name, members, project_binding=project_binding, labels=labels)
    tower.load()


def move_work(stdscr, tower, row: dict) -> None:
    """Move one stable task identity between logical groups; tmux is untouched."""
    target = str(row.get("target_id") or "")
    if not target:
        return
    membership = tower.work_groups.membership()
    current = membership.get(target)
    groups = tower.work_groups.all()
    items = [(group["group_id"], group["display_name"])
             for group in groups if group["group_id"] != current]
    if current:
        items.append(("__ungroup__", t("move.ungroup")))
    items.append(("__new__", t("move.new_group")))
    pick = run_list_picker(stdscr, t("move.pick_group"), items, footer_hint=t("wizard.hint_list"))
    if pick.cancelled or pick.selected_key is None:
        return
    if pick.selected_key == "__new__":
        name = prompt_text(stdscr, t("group.name_prompt"))
        if not name:
            return
        binding = None
        if row.get("project"):
            binding = {"name": str(row["project"])}
            if row.get("path"):
                binding["path"] = str(row["path"])
        _write_group(
            stdscr, tower.work_groups.move_target, target, None,
            new_group_name=name, label=_work_label(row), project_binding=binding,
        )
    else:
        destination = None if pick.selected_key == "__ungroup__" else pick.selected_key
        _write_group(stdscr, tower.work_groups.move_target, target, destination, label=_work_label(row))
    tower.load()


def _group_for_target(tower, target_id: str):
    group_id = tower.work_groups.membership().get(target_id)
    return next((group for group in tower.work_groups.all() if group["group_id"] == group_id), None)


def _swap_group_slots(stdscr, tower, group: dict) -> None:
    live = {row.get("target_id"): row for row in tower.rows if row.get("target_id")}
    choices = [(target, _work_label(live[target]) if target in live
                else group.get("member_labels", {}).get(target, t("group.stale_member")))
               for target in group.get("member_order", [])]
    if len(choices) < 2:
        return
    first = run_list_picker(stdscr, t("layout.slot_first"), choices, footer_hint=t("wizard.hint_list"))
    if first.cancelled or not first.selected_key:
        return
    second_choices = [item for item in choices if item[0] != first.selected_key]
    second = run_list_picker(stdscr, t("layout.slot_second"), second_choices, footer_hint=t("wizard.hint_list"))
    if second.cancelled or not second.selected_key:
        return
    _write_group(stdscr, tower.work_groups.swap_layout_slots, group["group_id"], first.selected_key, second.selected_key)


def open_group_layout(stdscr, tower, group: dict) -> None:
    items = [(layout, t(f"layout.{layout}")) for layout in LAYOUTS]
    items.extend([("swap", t("layout.slot_swap")), ("cancel", t("menu.cancel"))])
    pick = run_list_picker(stdscr, t("layout.select"), items, footer_hint=t("wizard.hint_list"))
    if pick.cancelled or not pick.selected_key:
        return
    if pick.selected_key == "swap":
        _swap_group_slots(stdscr, tower, group)
        return
    if pick.selected_key not in LAYOUTS:
        return
    mode = run_list_picker(stdscr, t("layout.change"), [
        ("view", t("layout.view_only")),
        ("physical", t("layout.physical_apply")),
        ("cancel", t("menu.cancel")),
    ], footer_hint=t("wizard.hint_list"))
    if mode.cancelled or mode.selected_key not in {"view", "physical"}:
        return
    if mode.selected_key == "physical":
        result = apply_group_terminal_layout(stdscr, tower, group, pick.selected_key, save=False)
        if not result:
            return
    _write_group(stdscr, tower.work_groups.set_layout, group["group_id"], pick.selected_key)
    show_message_screen(stdscr, t("layout.saved"), [])


def open_target_layout(stdscr, tower, row: dict) -> None:
    group = _group_for_target(tower, str(row.get("target_id") or ""))
    if not group:
        show_message_screen(stdscr, t("layout.group_required"), [])
        return
    open_group_layout(stdscr, tower, group)


def save_group_layout_to_workset(stdscr, tower, group: dict) -> None:
    """Explicitly copy a group's current logical layout into one Saved Work."""
    from dataclasses import replace
    from ..state.worksets import WorksetStore

    try:
        store = WorksetStore()
        saved = store.list("saved_work")
    except (OSError, ValueError):
        show_message_screen(stdscr, t("layout.save_failed"), [])
        return
    if not saved:
        show_message_screen(stdscr, t("layout.no_saved_work"), [])
        return
    pick = run_list_picker(stdscr, t("layout.save_to_work"),
                           [(item.workset_id, item.name) for item in saved],
                           footer_hint=t("wizard.hint_list"))
    if pick.cancelled or not pick.selected_key:
        return
    selected = next((item for item in saved if item.workset_id == pick.selected_key), None)
    if selected is None:
        return
    tower.load()
    by_target = {str(row.get("target_id")): row for row in tower.rows if row.get("target_id")}
    target_ids = list(group.get("member_order") or [])
    if len(target_ids) != len(selected.members) or any(target not in by_target for target in target_ids):
        show_message_screen(stdscr, t("layout.save_mismatch"), [])
        return
    rows = [by_target[target] for target in target_ids]
    original_members = {member.member_id: member for member in selected.members}
    expected_roles = [original_members[member_id].role for member_id in selected.member_order]
    if [row.get("role") or "" for row in rows] != expected_roles:
        show_message_screen(stdscr, t("layout.save_mismatch"), [])
        return
    slots = group.get("layout_slots") or {}
    members = dict(original_members)
    for index, member_id in enumerate(selected.member_order):
        members[member_id] = replace(members[member_id], layout_slot=slots.get(target_ids[index], ""))
    try:
        store.save(replace(
            selected,
            members=tuple(members[member.member_id] for member in selected.members),
            layout=group.get("layout") or selected.layout,
            updated_at=time.time(),
        ))
    except (OSError, ValueError):
        show_message_screen(stdscr, t("layout.save_failed"), [])
        return
    show_message_screen(stdscr, t("layout.saved"), [selected.name])


def _managed_group_window(tower, group: dict):
    """Return a fresh, fully-owned single-window snapshot or a user-facing error."""
    target_ids = list(group.get("member_order") or [])
    tower.load()
    by_target = {str(row.get("target_id")): row for row in tower.rows if row.get("target_id")}
    if not target_ids or any(target not in by_target for target in target_ids):
        return None, t("layout.stale")
    rows = [by_target[target] for target in target_ids]
    if any(row.get("remote") for row in rows):
        return None, t("layout.cross_host")
    if any(str(row.get("session") or "") != str(tower.session) for row in rows):
        return None, t("layout.cross_host")
    hosts = {str(row.get("tmux_host") or "") for row in rows}
    if len(hosts) != 1 or "" in hosts:
        return None, t("layout.cross_host")
    resources = tower.work_groups.managed_resources(target_ids)
    if set(resources) != set(target_ids):
        return None, t("layout.ownership_missing")
    for target, row in zip(target_ids, rows):
        resource = resources[target]
        if (resource["session"] != str(tower.session)
                or resource["pane_id"] != str(row.get("pane_id") or "")
                or resource["pane_pid"] != str(row.get("pane_pid") or "")
                or resource["tmux_host"] != str(row.get("tmux_host") or "")
                or resource["window_id"] != str(row.get("window_id") or "")
                or resource["pane_id"] not in structure.pane_ids(tower.session)):
            return None, t("layout.stale")
    windows = {structure.pane_window_id(str(row.get("pane_id") or "")) for row in rows}
    if len(windows) != 1 or "" in windows:
        return None, t("layout.single_window")
    window_id = next(iter(windows))
    pane_ids = {str(row.get("pane_id") or "") for row in rows}
    if set(structure.panes_of_window(window_id)) != pane_ids:
        return None, t("layout.unrelated")
    layout = structure.window_layout(tower.session, window_id)
    if not layout:
        return None, t("layout.stale")
    return {"rows": rows, "window_id": window_id, "layout": layout, "pane_ids": pane_ids}, ""


def apply_group_terminal_layout(stdscr, tower, group: dict, layout: Optional[str] = None, *, save: bool = True) -> bool:
    layout = layout or group.get("layout") or "focus"
    snapshot, error = _managed_group_window(tower, group)
    if snapshot is None:
        show_message_screen(stdscr, t("layout.apply_failed"), [error])
        return False
    lines = [t("layout.confirm_apply").format(n=len(snapshot["rows"]), untouched=0)]
    lines.extend(_work_label(row) for row in snapshot["rows"][:4])
    if len(snapshot["rows"]) > 4:
        lines.append("…")
    if not _confirm(stdscr, t("layout.physical_apply"), lines):
        return False
    from ..launcher.workset_launch import TMUX_LAYOUTS

    result = structure.apply_layout(tower.session, snapshot["window_id"], TMUX_LAYOUTS[layout])
    if not result.ok:
        structure.restore_layout(tower.session, snapshot["window_id"], snapshot["layout"])
        show_message_screen(stdscr, t("layout.apply_failed"), [result.detail or t("layout.stale")])
        return False
    if save:
        try:
            tower.work_groups.set_layout(group["group_id"], layout)
        except (OSError, ValueError, KeyError):
            structure.restore_layout(tower.session, snapshot["window_id"], snapshot["layout"])
            show_message_screen(stdscr, t("layout.apply_failed"), [t("group.save_failed")])
            return False
    return True


def open_work_group_menu(stdscr, tower, row: dict) -> None:
    group = next((item for item in tower.work_groups.all() if item["group_id"] == row.get("group_id")), None)
    if not group:
        tower.load()
        return
    pick = run_list_picker(
        stdscr, t("group.label"),
        [("view", t("nav.live_view")), ("rename", t("group.rename")), ("save", t("workset.save_group")), ("add", t("group.add")),
         ("remove", t("group.remove")), ("reorder", t("group.reorder")),
         ("layout", t("layout.change")), ("more", t("menu.more")),
         ("cancel", t("menu.cancel"))],
        footer_hint=t("wizard.hint_list"), preamble=[group["display_name"]],
    )
    if pick.cancelled or pick.selected_key in (None, "cancel"):
        return
    group_id = group["group_id"]
    if pick.selected_key == "rename":
        name = prompt_text(stdscr, t("group.rename_prompt"), initial=group["display_name"])
        if name:
            _write_group(stdscr, tower.work_groups.rename, group_id, name)
    elif pick.selected_key == "save":
        from .worksets import save_work_group

        save_work_group(stdscr, tower, group)
    elif pick.selected_key == "add":
        tower.load()
        occupied = tower.work_groups.membership()
        candidates = [item for item in tower.rows if item.get("pane_id") and item.get("target_id") and item.get("target_id") not in occupied]
        if not candidates:
            choice = run_list_picker(
                stdscr, t("group.add"),
                [("create", t("group.add_create")), ("back", t("menu.cancel"))],
                footer_hint=t("wizard.hint_list"), preamble=[t("group.add_empty")],
            )
            if choice.cancelled or choice.selected_key != "create":
                return
            before = {str(item.get("target_id")) for item in tower.rows if item.get("target_id")}
            start_task(stdscr, tower)
            tower.load()
            created = [
                item for item in tower.rows
                if item.get("pane_id") and item.get("target_id")
                and str(item["target_id"]) not in before
                and item["target_id"] not in tower.work_groups.membership()
            ]
            if len(created) != 1:
                show_message_screen(stdscr, t("group.add_create_failed"), [])
                return tower.load()
            target = created[0]["target_id"]
            _write_group(stdscr, tower.work_groups.add_members, group_id, [target], {target: _work_label(created[0])})
            return tower.load()
        selected = _pick_targets(stdscr, t("group.add"), candidates, [t("group.pick_add")])
        if not selected:
            return tower.load()
        labels = {item["target_id"]: _work_label(item) for item in candidates if item.get("target_id") in selected}
        _write_group(stdscr, tower.work_groups.add_members, group_id, _ordered_targets(candidates, selected), labels)
    elif pick.selected_key == "remove":
        live = {item.get("target_id"): item for item in tower.rows if item.get("target_id")}
        members = []
        for target in group["member_order"]:
            current = live.get(target) or {"target_id": target, "display_name": group["member_labels"].get(target) or t("group.stale_member")}
            members.append(current)
        selected = _pick_targets(stdscr, t("group.remove"), members, [t("group.pick_remove")])
        if selected:
            _write_group(stdscr, tower.work_groups.remove_members, group_id, selected)
    elif pick.selected_key == "reorder":
        live = {item.get("target_id"): item for item in tower.rows if item.get("target_id")}
        members = [live.get(target) or {"target_id": target, "display_name": group["member_labels"].get(target) or t("group.stale_member")} for target in group["member_order"]]
        selected = _pick_targets(stdscr, t("group.reorder"), members, [t("group.pick_reorder")])
        if len(selected) == 1:
            direction = run_list_picker(stdscr, t("group.reorder"), [
                ("up", t("group.move_up")), ("down", t("group.move_down")),
                ("top", t("group.move_top")), ("bottom", t("group.move_bottom")),
                ("cancel", t("menu.cancel")),
            ], footer_hint=t("wizard.hint_list"))
            if direction.selected_key in {"up", "down", "top", "bottom"}:
                _write_group(stdscr, tower.work_groups.reorder_member, group_id, next(iter(selected)), direction.selected_key)
    elif pick.selected_key == "layout":
        open_group_layout(stdscr, tower, group)
    elif pick.selected_key == "view":
        from .live_view import open_group_live_view

        open_group_live_view(stdscr, tower, group)
    elif pick.selected_key == "physical":
        apply_group_terminal_layout(stdscr, tower, group)
    elif pick.selected_key == "save_layout":
        save_group_layout_to_workset(stdscr, tower, group)
    elif pick.selected_key == "dissolve":
        confirm = run_list_picker(stdscr, t("group.dissolve"), [("confirm", t("group.dissolve_confirm")), ("cancel", t("menu.cancel"))], footer_hint=t("wizard.hint_list"), preamble=[group["display_name"]])
        if confirm.selected_key == "confirm":
            _write_group(stdscr, tower.work_groups.dissolve, group_id)
    elif pick.selected_key == "more":
        extra = run_list_picker(stdscr, t("menu.more"), [
            ("saved", t("struct.create_saved")),
            ("settings", t("nav.settings")),
            ("terminal", t("nav.terminal_structure")),
            ("physical", t("layout.physical_apply")),
            ("save_layout", t("layout.save_to_work")),
            ("dissolve", t("group.dissolve")),
            ("info", t("group.more")),
            ("cancel", t("menu.cancel")),
        ], footer_hint=t("wizard.hint_list"))
        if extra.selected_key == "saved":
            open_saved_entry(stdscr, tower)
        elif extra.selected_key == "settings":
            from .settings_menu import open_settings

            open_settings(stdscr, tower)
        elif extra.selected_key == "terminal":
            tower.toggle_navigator()
        elif extra.selected_key == "physical":
            apply_group_terminal_layout(stdscr, tower, group)
        elif extra.selected_key == "save_layout":
            save_group_layout_to_workset(stdscr, tower, group)
        elif extra.selected_key == "dissolve":
            confirm = run_list_picker(stdscr, t("group.dissolve"), [
                ("confirm", t("group.dissolve_confirm")), ("cancel", t("menu.cancel"))
            ], footer_hint=t("wizard.hint_list"), preamble=[group["display_name"]])
            if confirm.selected_key == "confirm":
                _write_group(stdscr, tower.work_groups.dissolve, group_id)
        elif extra.selected_key == "info":
            pick = type("Pick", (), {"selected_key": "more"})()
            # Keep the existing information summary below as the single renderer.
            if pick.selected_key == "more":
                pass
        if extra.selected_key != "info":
            tower.load()
            return
        tower.load()
        live = {item.get("target_id"): item for item in tower.rows if item.get("target_id")}
        members = [live.get(target) or {"target_id": target, "stale": True} for target in group["member_order"]]
        from ..state.work_groups import aggregate
        from .work_groups import summary_text

        binding = group.get("project_binding") or {}
        lines = [
            t("group.member_count").format(n=len(group["member_target_ids"])),
            f'{t("detail.project")}: {binding.get("name") or t("project.no_name")}',
            summary_text(aggregate(members)) or t("group.empty"),
        ]
        show_message_screen(stdscr, t("group.more_title"), lines)
    tower.load()


def change_role(stdscr, tower, row: dict) -> None:
    """Change descriptive role metadata for the current pane identity."""

    tower.load()
    current = next(
        (item for item in tower.rows if item.get("key") == row.get("key") and not item.get("placeholder")),
        None,
    )
    if current is None:
        show_message_screen(stdscr, t("role.stale"), [])
        return
    key = current["key"]
    session = str(current.get("session") or "")
    pane_pid = str(current.get("pane_pid") or "").strip()
    if not str(key or "").strip() or not session.strip() or not pane_pid:
        show_message_screen(stdscr, t("role.stale"), [])
        return
    current_role = current.get("role") if current.get("role") in ROLE_IDS else None
    items = [(role, t(f"role.{role}")) for role in ROLE_IDS]
    items.append(("__unassigned__", t("role.unassigned")))
    items.append(("cancel", t("menu.cancel")))
    pick = run_list_picker(
        stdscr,
        t("role.change"),
        items,
        footer_hint=t("wizard.hint_list"),
        preamble=[
            current.get("task_name") or current.get("project") or "",
            t("role.current").format(
                role=t(f"role.{current_role}") if current_role else t("role.unassigned")
            ),
        ],
    )
    if pick.cancelled or pick.selected_key in (None, "cancel"):
        return

    if pick.selected_key != "__unassigned__" and pick.selected_key not in ROLE_IDS:
        return
    tower.load()
    current = next(
        (item for item in tower.rows if item.get("key") == key and not item.get("placeholder")),
        None,
    )
    if (
        current is None
        or str(current.get("session") or "") != session
        or str(current.get("pane_pid") or "").strip() != pane_pid
    ):
        show_message_screen(stdscr, t("role.stale"), [])
        return

    from ..control.actions import apply_identity_edit

    apply_identity_edit(
        tower,
        key,
        {"role": None if pick.selected_key == "__unassigned__" else pick.selected_key},
    )


def start_task(stdscr, tower) -> None:
    """Start a task through environment, project, and Agent."""

    from .tower import STATE_DIR
    from .workspace_browser import run_workspace_create

    run_workspace_create(stdscr, tower, STATE_DIR, multi=False, open_tree=True, task_flow=True)


def create_blank_window(stdscr, tower) -> None:
    """Empty window. No workspace and no agent."""

    name = prompt_text(stdscr, t("struct.name_prompt"), context_lines=[t("struct.name_optional")])
    if name is None:
        return
    created = structure.create_window(tower.session, name=name)
    if not created.ok:
        show_message_screen(stdscr, t("struct.failed"), [created.detail])
        return
    show_message_screen(stdscr, t("struct.created"), [])


def create_blank_pane(stdscr, tower) -> None:
    """Empty pane in the current window."""

    window_id = structure.current_window_id(tower.session)
    if not structure.valid_window_id(window_id):
        show_message_screen(stdscr, t("struct.failed"), [t("nav.stale")])
        return
    direction = _pick_split(stdscr, window_id)
    if not direction:
        return
    created = structure.create_pane(window_id, direction=direction)
    if not created.ok:
        show_message_screen(stdscr, t("struct.failed"), [created.detail])
        return
    show_message_screen(stdscr, t("struct.created"), [])


def create_empty_window(stdscr, tower) -> None:
    name = prompt_text(stdscr, t("struct.name_prompt"), context_lines=[t("struct.name_optional")])
    if name is None:
        return
    how = run_list_picker(
        stdscr,
        t("struct.create_window"),
        [
            ("shell", t("struct.empty_shell")),
            ("project", t("struct.with_project")),
            ("cancel", t("menu.cancel")),
        ],
        footer_hint=t("wizard.hint_list"),
    )
    if how.cancelled or how.selected_key in (None, "cancel"):
        return
    if how.selected_key == "shell":
        created = structure.create_window(tower.session, name=name)
        if not created.ok:
            show_message_screen(stdscr, t("struct.failed"), [created.detail])
            return
        show_message_screen(stdscr, t("struct.created"), [])
        return

    from ..launcher.config import load_config, resolve_agent_command
    from ..launcher.discovery import record_recent
    from ..launcher.spawn import SpawnTarget, _finish_new_pane
    from ..state.bindings import ProjectBindingStore
    from .tower import STATE_DIR
    from .workspace_browser import WINDOW_LAYOUT, _path_preamble, _pick_agent, _pick_layout, launch_workspaces, pick_workspaces

    picks = pick_workspaces(stdscr, tower, STATE_DIR, multi=False)
    if not picks:
        return
    preamble = _path_preamble(picks)
    agent = _pick_agent(stdscr, preamble)
    if not agent:
        return
    laid = _pick_layout(stdscr, preamble + [agent])
    if laid is None:
        return
    layout_choice, _label = laid
    if picks[0].is_remote:
        results = launch_workspaces(
            session=tower.session,
            state_dir=STATE_DIR,
            picks=picks,
            agent=agent,
            placement="new",
            layout_choice=layout_choice,
            overrides=tower.overrides,
        )
        show_message_screen(
            stdscr,
            t("struct.created") if results and results[0].ok else t("struct.failed"),
            [results[0].detail] if results else [],
        )
        return

    entry = picks[0].entry
    created = structure.create_window(tower.session, name=name, cwd=entry.path)
    if not created.ok:
        show_message_screen(stdscr, t("struct.failed"), [created.detail])
        return
    cfg = load_config()
    command = resolve_agent_command(agent, cfg["agents"])
    _finish_new_pane(
        tower.session,
        SpawnTarget(entry.path, entry.name, agent),
        command,
        created.pane_id,
        cfg["agents"],
        ProjectBindingStore(STATE_DIR / "project-bindings.json"),
        tower.overrides,
    )
    structure.apply_layout(tower.session, created.window_id, WINDOW_LAYOUT.get(layout_choice, "tiled"))
    record_recent(STATE_DIR, picks[0].host_key, entry.path)
    show_message_screen(stdscr, t("struct.created"), [f"{entry.name} · {agent}", entry.path])


def add_pane(stdscr, tower, window_id: str) -> None:
    if not structure.valid_window_id(window_id):
        window_id = _pick_window(stdscr, tower) or ""
    if not structure.valid_window_id(window_id):
        show_message_screen(stdscr, t("struct.failed"), [t("nav.stale")])
        return
    how = run_list_picker(
        stdscr,
        t("struct.create_pane"),
        [
            ("shell", t("struct.empty_shell")),
            ("project", t("struct.with_project")),
            ("cancel", t("menu.cancel")),
        ],
        footer_hint=t("wizard.hint_list"),
        preamble=[_window_meta(tower, window_id).get("window_name") or t("project.no_name")],
    )
    if how.cancelled or how.selected_key in (None, "cancel"):
        return
    if how.selected_key == "shell":
        direction = _pick_split(stdscr, window_id)
        if not direction:
            return
        created = structure.create_pane(window_id, direction=direction)
        if not created.ok:
            show_message_screen(stdscr, t("struct.failed"), [created.detail])
            return
        show_message_screen(stdscr, t("struct.created"), [])
        return
    from .tower import STATE_DIR
    from .workspace_browser import run_workspace_create

    run_workspace_create(stdscr, tower, STATE_DIR, multi=False, bound_window=window_id)


def _pick_split(stdscr, window_id: str) -> str:
    direction = run_list_picker(
        stdscr,
        t("struct.split"),
        [
            ("auto", t("struct.split_auto")),
            ("horizontal", t("struct.split_h")),
            ("vertical", t("struct.split_v")),
            ("cancel", t("menu.cancel")),
        ],
        footer_hint=t("wizard.hint_list"),
    )
    if direction.cancelled or direction.selected_key in (None, "cancel"):
        return ""
    return direction.selected_key or ""


def choose_layout(stdscr, tower, window_id: str) -> None:
    items = [
        (name, f'{t("struct.layout." + name)}   {structure.LAYOUT_PREVIEW[name]}')
        for name in structure.LAYOUTS
    ]
    items.append(("cancel", t("menu.cancel")))
    pick = run_list_picker(
        stdscr,
        t("struct.layout"),
        items,
        footer_hint=t("wizard.hint_list"),
        preamble=[_window_meta(tower, window_id).get("window_name") or t("project.no_name")],
    )
    if pick.cancelled or pick.selected_key in (None, "cancel"):
        return
    result = structure.apply_layout(tower.session, window_id, pick.selected_key)
    if not result.ok:
        show_message_screen(stdscr, t("struct.failed"), [result.detail])


def rename_window(stdscr, tower, window_id: str) -> None:
    meta = _window_meta(tower, window_id)
    name = prompt_text(
        stdscr,
        t("struct.rename_window"),
        initial=meta.get("window_name") or "",
        context_lines=[meta.get("window_name") or t("project.no_name"), t("struct.name_is_label")],
    )
    if not name:
        return
    result = structure.rename_window(tower.session, window_id, name)
    if not result.ok:
        show_message_screen(stdscr, t("struct.failed"), [result.detail])


def _managed_window_rows(tower, window_id: str):
    tower.load()
    panes = structure.panes_of_window(window_id)
    by_pane = {str(row.get("pane_id")): row for row in tower.rows
               if row.get("pane_id") and not row.get("remote")}
    rows = [by_pane.get(pane) for pane in panes]
    if not panes or any(row is None or str(row.get("session") or "") != str(tower.session) for row in rows):
        return None
    resources = tower.work_groups.managed_resources(
        str(row.get("target_id") or "") for row in rows
    )
    for row in rows:
        target = str(row.get("target_id") or "")
        resource = resources.get(target)
        try:
            os.kill(int(row.get("pane_pid") or ""), 0)
        except (OSError, ValueError):
            return None
        if (not resource or resource["session"] != str(tower.session)
                or resource["pane_id"] != str(row.get("pane_id") or "")
                or resource["pane_pid"] != str(row.get("pane_pid") or "")
                or resource["tmux_host"] != str(row.get("tmux_host") or "")
                or resource["window_id"] != str(window_id)):
            return None
    return rows


def _rebind_result_after_move(tower, row: dict, new_window_id: str) -> bool:
    tracker = getattr(tower, "results", None)
    if tracker is None:
        return True
    from ..detection.result import pane_result_identity

    old_identity = pane_result_identity(row)
    new_identity = pane_result_identity({**row, "window_id": new_window_id})
    if old_identity is None or new_identity is None:
        return True
    return tracker.rebind_identity(str(row.get("key") or row.get("pane_id") or ""), old_identity, new_identity)


def move_pane_physical(stdscr, tower, row: dict) -> None:
    """Explicit terminal move. Both windows must contain Tower-owned panes only."""
    target_id = str(row.get("target_id") or "")
    tower.load()
    row = next((item for item in tower.rows if item.get("target_id") == target_id), None)
    if not row or row.get("remote"):
        show_message_screen(stdscr, t("layout.cross_host"), [])
        return
    resource = tower.work_groups.managed_resources([target_id]).get(target_id)
    pane_id = str(row.get("pane_id") or "")
    source = structure.pane_window_id(pane_id)
    if (not resource or resource["session"] != str(tower.session)
            or resource["pane_id"] != pane_id
            or resource["pane_pid"] != str(row.get("pane_pid") or "")
            or resource["tmux_host"] != str(row.get("tmux_host") or "")
            or not structure.valid_window_id(source)):
        show_message_screen(stdscr, t("layout.ownership_missing"), [])
        return
    if resource["window_id"] != source:
        show_message_screen(stdscr, t("layout.stale"), [])
        return
    source_rows = _managed_window_rows(tower, source)
    if not source_rows:
        show_message_screen(stdscr, t("layout.unrelated"), [])
        return
    windows = []
    for item in structure.list_windows(tower.session):
        window_id = item["window_id"]
        if window_id != source and _managed_window_rows(tower, window_id):
            windows.append(item)
    choices = [("detach", t("layout.detach"))]
    for item in windows:
        members = _managed_window_rows(tower, item["window_id"]) or []
        label = next((member.get("work_group_name") for member in members if member.get("work_group_name")), "")
        if not label:
            label = " · ".join(str(member.get("display_name") or member.get("project") or "작업") for member in members[:2])
        choices.append((item["window_id"], label or t("project.no_name")))
    choices.append(("cancel", t("menu.cancel")))
    pick = run_list_picker(stdscr, t("layout.physical_move"), choices, footer_hint=t("wizard.hint_list"),
                           preamble=[_work_label(row), t("layout.move_plan")])
    if pick.cancelled or pick.selected_key in (None, "cancel"):
        return
    destination_id = pick.selected_key
    if destination_id == "detach":
        if len(structure.panes_of_window(source)) < 2:
            show_message_screen(stdscr, t("struct.already_alone"), [])
            return
        if not _confirm(stdscr, t("layout.physical_move"), [_work_label(row), t("layout.move_plan")]):
            return
        moved = structure.break_pane(tower.session, pane_id, name=row.get("display_name") or "")
        if not moved.ok:
            show_message_screen(stdscr, t("layout.apply_failed"), [t("struct." + moved.detail) if moved.detail else t("layout.stale")])
            return
        if not _rebind_result_after_move(tower, row, moved.window_id):
            structure.move_pane(tower.session, pane_id, source)
            show_message_screen(stdscr, t("layout.apply_failed"), [t("layout.result_rebind")])
            return
        try:
            tower.work_groups.update_managed_window(target_id, moved.window_id)
        except (OSError, KeyError, ValueError):
            structure.move_pane(tower.session, pane_id, source)
            _rebind_result_after_move(tower, {**row, "window_id": moved.window_id}, source)
            show_message_screen(stdscr, t("layout.apply_failed"), [t("group.save_failed")])
            return
    else:
        target = next((item for item in windows if item["window_id"] == destination_id), None)
        if target is None or not _managed_window_rows(tower, destination_id):
            show_message_screen(stdscr, t("layout.stale"), [])
            return
        if len(structure.panes_of_window(source)) < 2:
            show_message_screen(stdscr, t("layout.single_window"), [])
            return
        if not _confirm(stdscr, t("layout.physical_move"), [
            _work_label(row), t("struct.move_confirm").format(name=choices[[key for key, _ in choices].index(destination_id)][1]),
            t("layout.move_plan"),
        ]):
            return
        source_layout = structure.window_layout(tower.session, source)
        destination_layout = structure.window_layout(tower.session, destination_id)
        moved = structure.move_pane(tower.session, pane_id, destination_id)
        if not moved.ok:
            actual = structure.pane_window_id(pane_id)
            if actual == destination_id:
                structure.move_pane(tower.session, pane_id, source)
            structure.restore_layout(tower.session, source, source_layout)
            structure.restore_layout(tower.session, destination_id, destination_layout)
            show_message_screen(stdscr, t("layout.apply_failed"), [t("layout.stale")])
            return
        if not _rebind_result_after_move(tower, row, destination_id):
            structure.move_pane(tower.session, pane_id, source)
            structure.restore_layout(tower.session, source, source_layout)
            structure.restore_layout(tower.session, destination_id, destination_layout)
            show_message_screen(stdscr, t("layout.apply_failed"), [t("layout.result_rebind")])
            return
        try:
            tower.work_groups.update_managed_window(target_id, destination_id)
        except (OSError, KeyError, ValueError):
            structure.move_pane(tower.session, pane_id, source)
            _rebind_result_after_move(tower, {**row, "window_id": destination_id}, source)
            structure.restore_layout(tower.session, source, source_layout)
            structure.restore_layout(tower.session, destination_id, destination_layout)
            show_message_screen(stdscr, t("layout.apply_failed"), [t("group.save_failed")])
            return
    tower.load()
    show_message_screen(stdscr, t("layout.apply_ok"), [_work_label(row)])


def break_pane(stdscr, tower, row: dict) -> None:
    pane_id = row.get("pane_id") or ""
    label = row.get("task_name") or row.get("project") or t("project.no_name")
    if not _confirm(stdscr, t("struct.break_title"), [label, t("struct.break_hint")]):
        return
    result = structure.break_pane(tower.session, pane_id)
    if not result.ok:
        key = "struct." + result.detail
        show_message_screen(stdscr, t("struct.failed"), [t(key) if key != t(key) else result.detail])
        return
    show_message_screen(stdscr, t("struct.created"), [])


def close_window(stdscr, tower, window_id: str) -> bool:
    """True when the window was closed."""

    windows = structure.list_windows(tower.session)
    inside = structure.panes_of_window(window_id)
    reason = structure.window_close_block(inside, tower.own_pane_id, len(windows))
    if reason == "tower_pane":
        show_message_screen(stdscr, t("struct.tower_window"), [])
        return False
    if reason == "last_window":
        show_message_screen(stdscr, t("struct.last_window"), [])
        return False
    if reason:
        show_message_screen(stdscr, t("struct.failed"), [reason])
        return False
    meta = _window_meta(tower, window_id)
    panes = [row for row in tower.rows if row.get("window_id") == window_id and row.get("pane_id") and not row.get("remote")]
    # Include Tower's own pane id if it lives here even though the list hides it.
    known = {row.get("pane_id") for row in panes}
    for pane_id in inside:
        if pane_id not in known:
            panes.append({"pane_id": pane_id, "agent": "-", "status": "-", "attention": "none"})
    pick = run_list_picker(
        stdscr,
        t("struct.close_window_title"),
        [("cancel", t("control.cancel")), ("close", t("struct.close_window_yes"))],
        footer_hint=t("wizard.hint_list"),
        preamble=window_close_summary(meta.get("window_name") or "", panes),
    )
    if pick.cancelled or pick.selected_key != "close":
        return False
    result = structure.kill_window(tower.session, window_id, tower.own_pane_id)
    if not result.ok:
        label = {
            "tower_pane": t("struct.tower_window"),
            "last_window": t("struct.last_window"),
            "not_found": t("nav.stale"),
        }.get(result.detail, result.detail)
        show_message_screen(stdscr, t("struct.failed"), [label])
        return False
    return True


def _window_menu(stdscr, tower, row: dict) -> None:
    window_id = row.get("window_id") or ""
    pick = run_list_picker(
        stdscr,
        t("struct.window_title"),
        [
            ("add", t("struct.add_pane")),
            ("rename", t("struct.rename")),
            ("layout", t("struct.layout")),
            ("close", t("struct.close_window")),
            ("cancel", t("menu.cancel")),
        ],
        footer_hint=t("wizard.hint_list"),
        preamble=[row.get("project") or t("project.no_name")],
    )
    if pick.cancelled or pick.selected_key in (None, "cancel"):
        return
    if pick.selected_key == "add":
        add_pane(stdscr, tower, window_id)
    elif pick.selected_key == "rename":
        rename_window(stdscr, tower, window_id)
    elif pick.selected_key == "layout":
        choose_layout(stdscr, tower, window_id)
    elif pick.selected_key == "close":
        close_window(stdscr, tower, window_id)


def _manage_panes(stdscr, tower, window_id: str) -> str:
    tower.load()
    panes = [row for row in tower.rows if row.get("window_id") == window_id and row.get("pane_id") and not row.get("remote")]
    if not panes:
        show_message_screen(stdscr, t("struct.manage_panes"), [t("zero.title")])
        return ""
    items = [
        (row["key"], f'{index + 1}. {row.get("task_name") or row.get("project") or t("project.no_name")} · {row.get("agent") or "-"}')
        for index, row in enumerate(panes)
    ]
    items.append(("cancel", t("menu.cancel")))
    pick = run_list_picker(stdscr, t("struct.manage_panes"), items, footer_hint=t("wizard.hint_list"))
    if pick.cancelled or pick.selected_key in (None, "cancel"):
        return ""
    from .control_view import open_control_view

    open_control_view(stdscr, tower, pick.selected_key)
    return ""


def _confirm(stdscr, title: str, lines: List[str]) -> bool:
    pick = run_list_picker(
        stdscr,
        title,
        [("cancel", t("control.cancel")), ("yes", t("struct.confirm_yes"))],
        footer_hint=t("wizard.hint_list"),
        preamble=lines,
    )
    return not pick.cancelled and pick.selected_key == "yes"


def _preferred_window(tower) -> str:
    if tower.visible_rows and 0 <= tower.selected < len(tower.visible_rows):
        row = tower.visible_rows[tower.selected]
        if row.get("window_id"):
            return row["window_id"]
    return structure.current_window_id(tower.session)


def _pick_window(stdscr, tower) -> Optional[str]:
    windows = structure.list_windows(tower.session)
    items = [
        (item["window_id"], f'{index + 1}. {item["window_name"] or t("project.no_name")}')
        for index, item in enumerate(windows)
    ]
    items.append(("cancel", t("menu.cancel")))
    pick = run_list_picker(stdscr, t("struct.place_pick"), items, footer_hint=t("wizard.hint_list"))
    if pick.cancelled or pick.selected_key in (None, "cancel"):
        return None
    return pick.selected_key


def _window_meta(tower, window_id: str) -> dict:
    for item in structure.list_windows(tower.session):
        if item["window_id"] == window_id:
            return item
    for row in tower.rows:
        if row.get("window_id") == window_id:
            return {
                "window_id": window_id,
                "window_name": row.get("window_name") or "",
                "pane_count": 0,
            }
    return {"window_id": window_id, "window_name": "", "pane_count": 0}


def _state_dir():
    from .tower import STATE_DIR

    return STATE_DIR
