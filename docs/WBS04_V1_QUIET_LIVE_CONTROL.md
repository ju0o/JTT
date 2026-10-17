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

## Saved-data compatibility and rollback

This stage adds no persistent file, schema, migration, or automatic screen
save:

- `folders.json` remains schema version 1 (`folders` and `windows`).
- `work-groups.json` retains its existing `groups` and `managed_resources`
  shape, including member order, layout slots, and exact managed Pane identity.
- `worksets.json` remains version 1. Saved Work and templates are untouched;
  templates continue to exclude concrete project paths and Pane IDs.
- Result, visit, name override, binding, and remote-host state are not rewritten
  by entering or drawing this screen.

Rollback is a source-code rollback to the preceding commit. Since no new saved
state is introduced, no user data migration or restoration is required.

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
- WBS-04-2 and later: focus switching between panel regions, project editing,
  saved control-room layouts, and direct input are outside this change.

Verification is limited to repository tests with fake curses screens. It is
not a test against a user's real tmux panes, Windows Clipboard, or ASUS TUI.
