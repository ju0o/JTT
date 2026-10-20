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
- On Linux, explicit stop terminates processes still in that PTY's process
  session, including jobs placed in separate process groups, with TERM then
  KILL escalation. A child that deliberately creates a new session with
  `setsid()` leaves that boundary and cannot be guaranteed to stop with JTT.
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
OSC/DCS extensions, private SGR extensions (ignored), and every terminal
query/response are not covered by this implementation. Wide and combining
Unicode behavior depends on both pyte's cell representation and the host
curses locale/terminal. A missing renderer, stream gap, or stale session
disables the input lease rather than accepting input against an untrusted
display.

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
JTT reserves `Ctrl+G` as a global prefix, including while a terminal has input
focus. Within 4 seconds, the next key selects a JTT command: `n` creates a
terminal, `h`/`l` changes cards, `w` selects the workspace panel, `x` opens a
cancel-first confirmation to stop only the selected terminal, `d` detaches the
UI, and `?` opens help. `Esc` cancels the prefix. Only in RIGHT input mode
with a valid lease, `Ctrl+G Ctrl+G` sends one literal Ctrl+G byte to the selected
PTY; the prefix byte itself is never sent.
An unknown prefix command is consumed. Consequently, an application that needs
Ctrl+G must use the doubled sequence, and applications that require other
unsupported terminal protocols remain outside the compatibility claim.

`Ctrl+]` remains the direct input-mode escape and is consumed by JTT. `Ctrl+G d`
and navigation-mode `q` disconnect only the UI and release its input lease;
they do not terminate the shell. A terminal is stopped only through its
explicit `터미널 종료` action. No AI executable, tmux command, SSH client,
clipboard action, login, or model request is started by JTT itself; commands
typed into a terminal are ordinary user processes and inherit the current
account's normal access.

## Development setup

From a source checkout, run `scripts/jtt-setup-dev` explicitly to create or use
the checkout-local `.venv-jtt` and install the project's Python dependencies
there. This explicit setup may contact the configured package index. It does
not change the system Python. `scripts/jtt-dev` uses that environment and
checks pyte before starting JTT; it never installs dependencies automatically.
If the environment is missing or the VT smoke test fails, it exits before a
new PTY is created and prints the setup command.

The package provides the separate `jtt` console entry point. The legacy
`tower` entry point remains unchanged and continues to run the tmux Agent
monitor; it is not the standalone JTT terminal workspace.

## Local data and cleanup

JTT uses `JTT_HOME` when set, otherwise `$XDG_STATE_HOME/jtt`, otherwise
`~/.local/state/jtt`. The workspace file and terminal supervisor metadata are
JTT-only. Terminal screen output and shell environment variables are not
persisted by the supervisor. To clean up, explicitly stop each terminal from
JTT first, then quit and back up any workspace configuration you want to keep.
Only after verifying that no JTT terminal is running should you remove the
JTT-specific state directory. Removing state files while a shell is running
does not stop that shell and can make it harder to reconnect.

## Renderer dependency and license

JTT declares pyte `>=0.8.2,<0.9` as a runtime dependency. pyte is licensed under
LGPL-3.0; the license text is included at `licenses/pyte-LGPL-3.0.txt`. This
project remains MIT-licensed, but redistributors must satisfy the LGPL terms
for the pyte dependency and preserve its notices. pyte depends on `wcwidth`,
which is MIT-licensed. Dependency and notice handling should be rechecked when
the pyte version changes.
