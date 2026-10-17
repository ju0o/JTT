"""User-facing editor for logical Tower projects.

All operations here update ProjectStore metadata only. No tmux command,
Agent request, or LIVE capture is performed by project editing.
"""

from __future__ import annotations

from ..i18n import t
from ..state.projects import ProjectStore
from .widgets import prompt_text, run_list_picker, show_message_screen


def _store(tower) -> ProjectStore | None:
    value = getattr(tower, "projects", None)
    return value if value is not None else None


def _asset_map(tower) -> dict[str, dict]:
    return {str(item.get("window_ref")): item for item in
            getattr(tower, "window_assets", ()) or () if item.get("window_ref")}


def _window_label(tower, ref: str, *, hidden: bool = False) -> str:
    asset = _asset_map(tower).get(ref)
    if asset is None:
        saved = getattr(getattr(tower, "folders", None), "window", lambda _ref: {})(ref)
        name = str(saved.get("display_name") or t("project_manage.disconnected"))
        label = f'{name} · {t("project_manage.disconnected")}'
    else:
        name = str(asset.get("display_name") or asset.get("window_name") or t("folder.terminal"))
        host = str(asset.get("execution_host") or asset.get("host") or asset.get("tmux_host") or t("state.unknown"))
        session = str(asset.get("session") or t("state.unknown"))
        label = t("project_manage.window_label").format(name=name, host=host, session=session)
    if hidden:
        label += f' · {t("project_manage.hidden")}'
    return label


def _changed(tower) -> None:
    apply_filter = getattr(tower, "_apply_filter", None)
    if callable(apply_filter):
        apply_filter()


def _error(stdscr, exc: Exception) -> None:
    if isinstance(exc, KeyError):
        message = t("project_manage.item_missing")
    elif isinstance(exc, ValueError):
        message = t("project_manage.invalid")
    else:
        message = t("project_manage.save_failed")
    show_message_screen(stdscr, t("project_manage.title"), [message])


def _confirm(stdscr, title: str, lines: list[str]) -> bool:
    pick = run_list_picker(
        stdscr, title,
        [("cancel", t("menu.cancel")), ("yes", t("project_manage.confirm_yes"))],
        footer_hint=t("wizard.hint_list"), preamble=lines,
    )
    return not pick.cancelled and pick.selected_key == "yes"


def _select_windows_to_add(stdscr, tower, project: dict) -> list[str]:
    store = _store(tower)
    if store is None:
        return []
    current = set(project.get("window_refs") or ())
    assets = _asset_map(tower)
    refs = list(assets)
    items = [(ref, _window_label(tower, ref)) for ref in refs if ref not in current]
    if not items:
        show_message_screen(stdscr, t("project_manage.add_windows"), [t("project_manage.no_addable_windows")])
        return []
    selected = run_list_picker(
        stdscr, t("project_manage.add_windows"), items, multi=True,
        footer_hint=t("project_manage.multi_select_hint"),
    )
    if selected.cancelled:
        return []
    return [str(ref) for ref in selected.selected_keys]


def _add_windows(stdscr, tower, project: dict) -> None:
    store = _store(tower)
    refs = _select_windows_to_add(stdscr, tower, project)
    if store is None or not refs:
        return
    membership = store.memberships()
    projects = {item["project_id"]: item for item in store.all()}
    conflicts = sorted({membership[ref] for ref in refs if ref in membership and membership[ref] != project["project_id"]})
    move_existing = False
    if conflicts:
        labels = [projects.get(project_id, {}).get("display_name", t("project_manage.unknown_project"))
                  for project_id in conflicts]
        if not _confirm(stdscr, t("project_manage.move_windows_title"), [
            t("project_manage.move_windows_warning"), *labels,
        ]):
            return
        move_existing = True
    try:
        store.add_windows(project["project_id"], refs, move_existing=move_existing)
        _changed(tower)
    except (OSError, ValueError, KeyError) as exc:
        _error(stdscr, exc)


def _project_windows(stdscr, tower, project: dict, *, removable: bool = False):
    store = _store(tower)
    if store is None:
        return None
    refs = list(project.get("window_refs") or ())
    if not refs:
        show_message_screen(stdscr, t("project_manage.title"), [t("project_manage.no_windows")])
        return None
    hidden = store.hidden_refs()
    return run_list_picker(
        stdscr,
        t("project_manage.remove_windows") if removable else t("project_manage.visibility"),
        [(ref, _window_label(tower, ref, hidden=ref in hidden)) for ref in refs],
        searchable=True,
        footer_hint=t("wizard.hint_single_search"),
    )


def _reorder_project(stdscr, tower, project_id: str) -> None:
    pick = run_list_picker(stdscr, t("project_manage.project_order"), [
        ("up", t("project_manage.move_up")), ("down", t("project_manage.move_down")),
        ("top", t("project_manage.move_top")), ("bottom", t("project_manage.move_bottom")),
    ], footer_hint=t("wizard.hint_list"))
    if pick.cancelled or not pick.selected_key:
        return
    try:
        _store(tower).reorder_project(project_id, str(pick.selected_key))
        _changed(tower)
    except (OSError, ValueError, KeyError) as exc:
        _error(stdscr, exc)


def _reorder_window(stdscr, tower, project: dict) -> None:
    store = _store(tower)
    selected = _project_windows(stdscr, tower, project, removable=True)
    if store is None or selected is None or selected.cancelled or not selected.selected_key:
        return
    direction = run_list_picker(stdscr, t("project_manage.window_order"), [
        ("up", t("project_manage.move_up")), ("down", t("project_manage.move_down")),
        ("top", t("project_manage.move_top")), ("bottom", t("project_manage.move_bottom")),
    ], footer_hint=t("wizard.hint_list"))
    if direction.cancelled or not direction.selected_key:
        return
    try:
        store.reorder_window(project["project_id"], str(selected.selected_key), str(direction.selected_key))
        _changed(tower)
    except (OSError, ValueError, KeyError) as exc:
        _error(stdscr, exc)


def open_project_editor(stdscr, tower, project_id: str) -> None:
    store = _store(tower)
    if store is None:
        return
    project = next((item for item in store.all() if item["project_id"] == project_id), None)
    if project is None:
        _error(stdscr, KeyError(project_id))
        return
    pick = run_list_picker(stdscr, project["display_name"], [
        ("rename", t("project_manage.rename")),
        ("add", t("project_manage.add_windows")),
        ("remove", t("project_manage.remove_windows")),
        ("window_order", t("project_manage.window_order")),
        ("visibility", t("project_manage.visibility")),
        ("collapse", t("project_manage.toggle_collapse")),
        ("project_order", t("project_manage.project_order")),
        ("delete", t("project_manage.delete")),
        ("cancel", t("menu.cancel")),
    ], footer_hint=t("wizard.hint_list"))
    if pick.cancelled or pick.selected_key in (None, "cancel"):
        return

    action = str(pick.selected_key)
    try:
        if action == "rename":
            name = prompt_text(stdscr, t("project_manage.rename_prompt"), initial=project["display_name"])
            if name is None:
                return
            store.rename(project_id, name)
        elif action == "add":
            _add_windows(stdscr, tower, project)
            return
        elif action == "remove":
            selected = _project_windows(stdscr, tower, project, removable=True)
            if selected is None or selected.cancelled or not selected.selected_key:
                return
            store.remove_windows(project_id, [str(selected.selected_key)])
        elif action == "window_order":
            _reorder_window(stdscr, tower, project)
            return
        elif action == "visibility":
            selected = _project_windows(stdscr, tower, project)
            if selected is None or selected.cancelled or not selected.selected_key:
                return
            ref = str(selected.selected_key)
            store.set_hidden(ref, ref not in store.hidden_refs())
        elif action == "collapse":
            store.toggle_project(project_id)
        elif action == "project_order":
            _reorder_project(stdscr, tower, project_id)
            return
        elif action == "delete":
            if not _confirm(stdscr, t("project_manage.delete"), [t("project_manage.delete_effect")]):
                return
            store.delete(project_id)
        else:
            return
        _changed(tower)
    except (OSError, ValueError, KeyError) as exc:
        _error(stdscr, exc)


def create_project(stdscr, tower, row: dict | None = None) -> None:
    store = _store(tower)
    if store is None:
        return
    name = prompt_text(stdscr, t("project_manage.create_prompt"))
    if name is None:
        return
    try:
        ref = str((row or {}).get("window_ref") or "")
        move_existing = False
        if ref:
            membership = store.memberships().get(ref)
            if membership:
                existing = next((item for item in store.all() if item["project_id"] == membership), None)
                old_name = (existing or {}).get("display_name") or t("project_manage.unknown_project")
                if not _confirm(stdscr, t("project_manage.move_windows_title"), [
                    t("project_manage.move_existing_one").format(name=old_name),
                ]):
                    return
                move_existing = True
        project = store.create(name, [ref] if ref else (), move_existing=move_existing)
        _changed(tower)
        open_project_editor(stdscr, tower, project["project_id"])
    except (OSError, ValueError, KeyError) as exc:
        _error(stdscr, exc)


def open_project_picker(stdscr, tower, row: dict | None = None, *, centered: bool = False) -> None:
    store = _store(tower)
    if store is None:
        return
    items = [("create", t("project_manage.create"))]
    items.extend((item["project_id"], item["display_name"]) for item in store.all())
    pick = run_list_picker(
        stdscr, t("project_manage.title"), items,
        footer_hint=t("wizard.hint_list"), centered=centered,
    )
    if pick.cancelled or not pick.selected_key:
        return
    if pick.selected_key == "create":
        create_project(stdscr, tower, row)
    else:
        open_project_editor(stdscr, tower, str(pick.selected_key))
