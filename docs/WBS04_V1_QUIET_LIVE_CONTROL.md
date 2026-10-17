# WBS-04 V1 Quiet Live Control — Baseline and UI Contract

Status: implementation in progress; release remains on hold.

## Baseline and protected behavior

- Repository: `ju0o/tmux-agent-tower`, branch `rc/wbs05-integrated-20261015`.
- Baseline: `3a944e4e7675dfd6647d4d30f0ce812e864bffed`.
- PR #8 was Draft at this baseline. The existing CI jobs for Python 3.9,
  3.11, and 3.13 were successful when this work began.
- The existing Home/terminal-structure views, background discovery, LIVE
  renderer, Pane Control, Result Copy, SSH session binding, and remote
  collector remain the source of truth. This WBS adds a read-only view over
  their current snapshots; it does not replace those engines.

Relevant regression contracts are exercised by `tests/test_live_view.py`,
`tests/test_control_actions.py`, `tests/test_opencode_ssh_result.py`,
`tests/test_remote_session_binding.py`, `tests/test_folders.py`,
`tests/test_work_groups.py`, and `tests/test_worksets.py`. They cover LIVE
membership and stale-Pane identity, action routing, complete Result
extraction, SSH binding, and logical saved-state preservation.

## Pane identity and selection

A selected Pane is matched by its stable Tower `target_id` together with
tmux host, session/session ID, window ID, pane ID, Pane PID, transport, and
transport target. A refresh that reuses a Pane ID with a different PID (or
changes another identity field) invalidates the current selection. The old
captured output is hidden; the view does not silently transfer selection to
the replacement Pane. Arrow navigation can explicitly select another current
Pane. The selected item is synchronized back to Home only when the same
identity is still present there.

The left tree is a display projection of discovered Pane rows:
computer → session → window → project/Work Group label → task. It does not
write Folder, Work Group, Workset, or tmux state. A Work Group's saved member
order is used for its LIVE grid; otherwise the selected Pane's existing
physical Window defines the related grid. At most four tiles are shown and a
selected Pane is kept in that bounded set. No physical tmux layout is applied.

## Saved-data compatibility at WBS-04-1/04-2

The initial read-only stages added no persistent file, schema, migration, or
automatic screen save:

- `folders.json` remains schema version 1 (`folders` and `windows`).
- `work-groups.json` retains its existing `groups` and `managed_resources`
  shape, including member order, layout slots, and exact managed Pane identity.
- `worksets.json` remains version 1. Saved Work and templates are untouched;
  templates continue to exclude concrete project paths and Pane IDs.
- Result, visit, name override, binding, and remote-host state are not rewritten
  by entering or drawing this screen.

Rollback for those initial stages is a source-code rollback to the preceding
commit. No user data migration or restoration was required.

## LIVE and direct-input boundary

The central grid and selected-Pane panel reuse the existing LIVE tile renderer
and only the `live_lines` already captured by the normal Tower refresh. Drawing
does not call `capture-pane`, SSH, or another subprocess. If Pane identity
changes, old output is hidden. A configured peer's remote tmux rows are a
metadata-only, read-only snapshot; they do not provide a remote terminal input
channel. By contrast, a local tmux Pane running an SSH client is still a local
Pane: its captured screen is the SSH client's terminal, not a direct view of
the remote host's own tmux tree.

Static source review found two existing, separate paths:

1. `control.actions.send_prompt()` resolves a local Pane and identity before
   sending a bounded text payload through tmux `load-buffer`/`paste-buffer`,
   then submits one approved key and looks for acceptance evidence. This is
   deliberate Agent input, not arbitrary terminal-key forwarding.
2. `tmux.navigation.focus_local_pane()` selects an existing local Window and
   Pane. Its current check verifies session membership and Pane existence, but
   does not bind the final focus action to an expected Pane PID. It is not
   sufficient evidence for a future identity-sensitive live-control mode.

The current curses loops consume navigation and application keys; the LIVE
screen is a read-only mirror. Forwarding arbitrary keystrokes would require a
separate explicit control mode, a non-conflicting escape chord, and identity
revalidation at the point of action. tmux does not provide an atomic
"send only if pane PID still equals X" condition, so a check-then-send race
must be handled or the action rejected. WBS-04-5 is therefore not implemented
here. The candidate alternative is a separately approved, identity-revalidated
switch to an existing local SSH Pane, where the user types into their own
terminal; remote peer panes remain unsupported from MainPC. No such focus or
input action is invoked by this screen.

## WBS sequence and current scope

- WBS-04-0: preserve the above baseline and safety contracts with fake-screen
  and fixture-based regression tests.
- WBS-04-1: the existing `L` LIVE entry opens a read-only three-panel room.
  Wide viewports (110+ columns and 22+ rows) show location, related LIVE grid,
  and selected-Pane LIVE side by side. Smaller viewports stack those same
  panels. Opening from a selected Work Group, Folder, Window, or Pane keeps
  that scope as the initial selection when the same identity is available.
  Up/Down selects a Pane; `Y` delegates to the existing Result Copy handler;
  Escape returns to Home. No Agent input is sent.
- WBS-04-2: Tab/Shift+Tab move focus between the three panels; arrows navigate
  the current panel; Enter opens identity details or expands/collapses a tree
  node; Space opens a one-level menu to the existing LIVE layouts, Result Copy,
  Pane details, and settings. The left-tree collapse state is process-local and
  is not written to the Folder or Work Group stores. Work Group and Window LIVE
  entries delegate to the existing layout implementations. Result Copy still
  delegates to the existing _copy_result handler; ambiguous duplicate Pane
  keys are refused before that handler so another host cannot be selected by
  key alone. Direct Agent input and saved control-room layouts remain outside
  this WBS.

## WBS-04-3 logical project model

Projects are intentionally a separate, optional metadata store because the
existing concepts have different ownership:

- `folders.json` (`FolderStore`) classifies Window assets in the normal
  computer-centered view. Existing folders and their names are not converted,
  renamed, or rewritten by Project operations.
- `work-groups.json` (`WorkGroupStore`) groups Agent Pane targets and retains
  its member labels, order, LIVE layout slots, and managed-resource records.
- `worksets.json` (`WorksetStore`) stores launch recipes/templates, not live
  Window membership.
- `projects.json` (`ProjectStore`, schema version 1) groups exact `window_ref`
  values across hosts. It stores project order, per-project Window order,
  project collapse state, and Tower-only hidden Window references. It is
  distinct from WBS-04-4 saved control-room layouts.

There is no migration or write to any prior store. A Window can belong to at
most one Tower Project; moving it requires confirmation and changes only the
logical reference. Removing a Window or deleting its Project does not move,
rename, stop, or recreate the physical Window or Agent. Project deletion is
confirmed and canceled by default. Hidden Windows are omitted from Tower's
normal list and LIVE selection but remain in Project editing so they can be
restored. Missing `window_ref` values remain in the Project and display as
disconnected; a newly discovered Window is never substituted by Pane ID.

The Project file is created only on the first explicit Project edit, with
schema validation, mode `0600`, a private temporary file, fsync, and atomic
replacement. Corrupt, unsupported, or externally changed state refuses writes
instead of replacing the file. A source rollback does not require migrating or
restoring Folder, Work Group, or Workset data.

The Control Room adds a “My projects” tree only when a Project exists. Space on
a Pane opens the optional Project manager/create flow; Space on a Project
opens its editor. Project selection scopes the center LIVE grid by exact Window
references, and the right LIVE pane continues to follow the complete selected
Pane identity. The 4-card display bound remains unchanged; the selected Pane is
kept in the visible slice.

- WBS-04-4 and later: saved control-room layouts and direct input remain
  outside this stage.

## WBS-04-2 keyboard and feature-access contract

- The active panel title is reverse-highlighted, and the selected Pane card
  remains the source for the right-hand read-only LIVE panel.
- Tab and curses KEY_BTAB cycle panel focus. KEY_BTAB is used only when the
  terminal/curses layer reports it distinctly.
- Left-tree Up/Down selects Pane and parent nodes; Left/Right and Enter
  collapse or expand parent nodes. This state is in memory only.
- Center-panel arrows select a current Pane by the full Pane identity. The
  right panel follows that identity. Right-panel Up/Down scrolls only the
  already-captured live_lines snapshot.
- Space exposes established routes: saved Work Group LIVE, selected Window
  LIVE, the existing LIVE layout chooser, the existing Result Copy handler,
  metadata-only Pane details, and Tower settings. Remote peers are not offered
  unsupported Pane/Window LIVE routes from this menu. No Agent-input action is
  added to this menu.
- A Pane key appearing more than once in the current snapshot is not passed to the
  existing key-only Result Copy interface or legacy key-only LIVE surfaces.
  Ambiguous copy/LIVE routes are refused rather than guessing which host owns
  that key. Existing PID and binding checks remain unchanged.
- Fake-screen tests cover 60×18, 80×24, 100×40, and 120×40 plus panel focus,
  selection identity, duplicate Pane keys, and the legacy LIVE menu routes.
  They do not establish real-user terminal behavior.

Verification is limited to repository tests with fake curses screens. It is
not a test against a user's real tmux panes, Windows Clipboard, or ASUS TUI.
