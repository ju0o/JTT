# JTT RC3 Quick Start

[한국어](JTT_QUICK_START.ko.md) · [README](../README.md)

This guide targets official `jtt-v0.3.0-rc3` / package `0.3.0rc3` on Linux/WSL2.
The RC3 UI uses Korean menu labels; their English meanings appear below.
Install using the [README's guarded official installer](../README.md#install-official-rc3).
Then run `jtt --version`, `jtt doctor`, and `jtt`.

## 1. Create and type

Opening JTT alone starts no shell. Select a LEFT Window, then press Ctrl+G followed by n within four seconds.
The new CENTER card becomes selected; RIGHT enters input mode only when the session, renderer and input lease are ready.
If it reports an error, read the error instead of repeatedly creating terminals.

In RIGHT type this harmless command and Enter:

```sh
printf 'Hello JTT\n'
```

Ordinary keys now belong to that shell. Ctrl+] returns to navigation. In navigation use Tab / Shift+Tab to move panels; select a CENTER card and Enter to focus RIGHT, then Enter or click RIGHT to request input.
A failed/stale connection must not accept input.

## 2. Switch terminals and Windows

Ctrl+G n explicitly creates another independent terminal in the current Window.
Ctrl+G h / l selects the previous / next card and returns to navigation; enter RIGHT again to type.
Selection must match the input-target status displayed in RIGHT.

Ctrl+G w focuses LEFT. Choose `+ 새 Window` (New Window) and enter a name.
This creates an **empty logical group**, not a shell or OS/tmux window.
Selecting a Window keeps LEFT focused and updates CENTER/RIGHT. Existing shells keep running.
Use Tab to leave LEFT. Rename/remove and existing folder access are in `설정 · 메뉴` (Settings/menu).
Removing a logical Window does not terminate its terminals; references return to the default location.

## 3. Archive: references versus templates

Open `보관함` (Archive) from LEFT.

| Tab | Meaning | What opening it does |
| --- | --- | --- |
| 저장한 작업 (Saved work) | Existing terminal session references | Load/reconnect exact sessions where identity still matches |
| Work Kit | Names, roles, optional directories and a 1–4-card layout | Preview only; no shell, Agent, SSH or command starts |

Tab / Shift+Tab cycles list, action buttons and tabs. On the tab strip use Left/Right; in lists use Up/Down.
Enter on a list item moves to actions; select an action and Enter. Mouse selection is also supported. Esc closes/cancels.

For a Kit, choose `새 Work Kit` (New Work Kit), add/remove cards, edit terminal names, roles and optional working directories, then save.
Roles offer presets and custom text; directories can be browsed or typed.
Saving reports success and selects the template preview. `편집` (Edit) changes an existing template; Esc discards unsaved edits.
Preview is not execution. RC3 does not run a Work Kit.

`현재 구성 저장` (Save current configuration) belongs to Saved work. It stores references, not terminal output or a process snapshot.
Missing sessions stay disconnected; loading does not recreate them or restore processes after reboot.
Rename/delete actions identify the target and require confirmation where applicable. Kit deletion affects only the template.
Workspace deletion does not stop shells; the default workspace cannot be deleted.

## 4. Detach, reconnect, stop

- **Ctrl+G d:** detach UI, keep terminals. Reopen `jtt` under the same user and state path to reconnect.
- **Ctrl+G x:** cancel-first confirmation to stop the selected terminal. For a proven-exited session it removes that card reference.
- An uncertain connection is not proof of exit. Use recheck (Ctrl+G r) or explicit reference-management actions; do not delete socket/state files to “repair” it.
- Navigation q/Q/Esc does not detach. In input mode those keys go to the terminal. Esc cancels dialogs or a pending prefix.

UI separation is not reboot persistence. A supervisor crash or machine restart can end sessions.

## 5. Reserved keys and limitations

Ctrl+G is a JTT prefix; the next command must arrive within four seconds.
Ctrl+G Esc cancels it. Ctrl+G ? opens help.
In input mode, Ctrl+G Ctrl+G sends literal Ctrl+G; Ctrl+G Ctrl+] sends literal Ctrl+].
This matters for programs that reserve those keys. User-configured bindings may differ: follow the displayed hint, not this default-key table.
See [binding configuration and protocol limits](JTT_TERMINAL_SUPPORT.md).

JTT is not a sandbox. Shells run with normal user permissions and login configuration.
No automatic AI login/model calls are part of JTT.
The renderer is not a complete xterm emulator; advanced keys, mouse reporting, bracketed paste and arbitrary terminal queries have limits.
Clipboard and actual WSL restart recovery remain unverified.

[Updates / rollback](JTT_UPDATING.md) · [Legacy Tower](TOWER_LEGACY.md)
