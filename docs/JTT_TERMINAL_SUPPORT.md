# JTT local terminal support and safety

JTT is a standalone local PTY workspace. The legacy `tower` entry point remains
unchanged; `jtt` is a separate command. Merely opening JTT lists saved terminal
IDs and does not start a shell. A shell is started only by the explicit
`새 터미널` action.

## Session model

- Workspaces persist ordered terminal session IDs in the JTT-only state file.
- Each terminal has a private local supervisor, Unix socket, PTY, and login
  shell. Closing the JTT UI releases the input lease and disconnects the UI
  client; it does not request shell shutdown.
- Reopening JTT reconnects by session ID and validates generation, supervisor
  start identity, shell PID, and shell start identity before a client attaches.
- `터미널 종료` is a separate, explicit action. A machine reboot is not
  promised to restore a shell that was alive before reboot.
- State is stored under `JTT_HOME`, or `XDG_STATE_HOME/jtt`, or
  `~/.local/state/jtt`; state directories are owner-only and JSON files are
  owner-read/write. Existing Tower data is not migrated or modified.
- Sessions run as the current user. The child inherits the JTT process
  environment, except JTT removes its outer-tmux/session variables and sets
  `HOME`, working directory, and `TERM=xterm` for the shell. Normal login-shell
  startup files may run. This is not a security sandbox; commands typed into a
  shell can access files and network available to that user.

## Terminal rendering boundary

JTT uses pyte 0.8.2 as a VT screen model. It currently maps basic SGR
foreground colors and common text attributes into curses. The renderer does
not claim full xterm or Agent-TUI compatibility: truecolor/background palette
fidelity, Kitty keyboard protocol, mouse reporting, bracketed paste, arbitrary
OSC/DCS extensions, and every terminal query/response are not covered by this
implementation. Wide and combining Unicode behavior depends on both pyte's
cell representation and the host curses locale/terminal. A missing renderer,
stream gap, or stale session disables the input lease rather than accepting
input against an untrusted display.

The terminal surface reserves one row for JTT's input/observation status. The
selected PTY is resized to the available RIGHT content dimensions only. Other
sessions retain their last size until selected.

VT snapshots are bounded to 768 KiB (protocol response frames to 896 KiB).
If an unusually high-entropy screen exceeds that bound, JTT marks it
unsynchronized and blocks input; it retries on later polls and recovers only
when a complete snapshot fits. A deterministic randomized-color 300x120
fixture fits the current bound, but arbitrary terminal contents are not
claimed to fit every maximum-size viewport.

Input mode is explicitly entered on the RIGHT. `Ctrl+]` returns to workspace
navigation; `q` in navigation disconnects the UI without terminating sessions.
No AI executable, tmux command, SSH client, clipboard action, login, or model
request is started by JTT itself.

## Renderer dependency and license

JTT declares pyte `>=0.8.2,<0.9` as a runtime dependency. pyte is licensed under
LGPL-3.0; the license text is included at `licenses/pyte-LGPL-3.0.txt`. This
project remains MIT-licensed, but redistributors must satisfy the LGPL terms
for the pyte dependency and preserve its notices. pyte depends on `wcwidth`,
which is MIT-licensed. Dependency and notice handling should be rechecked when
the pyte version changes.
