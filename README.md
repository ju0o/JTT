# JTT — Ju0 Terminal Tower

**English | [한국어](README.ko.md)**

JTT is a local, open-source workspace for independent interactive terminals.
Each terminal owns a local PTY and shell; the selected terminal is rendered
and operated directly in the RIGHT panel. JTT has no hosted service and does
not manage Agent accounts, prompts, or model APIs.

## Current JTT RC

This repository also retains the earlier tmux Agent monitor. The standalone
JTT application is the separate `jtt` command; the existing `tower` command
and its legacy behavior are unchanged.

For a source checkout, run `./scripts/jtt-setup-dev` to create/use the
checkout-local `.venv-jtt` and install dependencies, then run
`./scripts/jtt-dev`. Setup is explicit and may contact the configured Python
package index; JTT startup does not install packages. Opening JTT does not
create a shell. Use the explicit new-terminal action when one is wanted.

JTT keys: within 4 seconds after `Ctrl+G`, `n` creates a terminal; `h`/`l`
changes cards; `w` focuses the workspace panel; `x` opens a cancel-first
confirmation to stop only the selected running terminal (for a proven-exited
terminal it removes only that card reference from the current workspace); `d`
detaches the UI while keeping terminals running; `?` shows help; `Esc` cancels a pending prefix. In terminal input
mode with the selected terminal's valid input lease, `Ctrl+G Ctrl+G` sends one
literal Ctrl+G byte to that terminal. In terminal input mode,
`Ctrl+]` returns to navigation and `q`, `Q`, and `Esc` are sent to the PTY.
In navigation, `q`, `Q`, and `Esc` do not close JTT; use `Ctrl+G d` to
disconnect the UI while keeping shells running. `Esc` also closes an active
dialog or cancels a pending prefix. See [JTT terminal support and safety](docs/JTT_TERMINAL_SUPPORT.md)
for data locations, cleanup guidance, and VT compatibility limits. The default
prefix/input escape (`Ctrl+G` / `Ctrl+]`) can be changed in the private JTT-only
`keybindings.json`; the support guide documents its strict schema and key list.
Work Kits are local templates rather than saved running terminals: selecting one
only previews non-running cards and never starts a shell or command. See
[JTT Work Kits](docs/JTT_WORK_KITS.md) for the local storage scope.

JTT logical folders and windows are an additive organization layer. Existing
`workspaces.json` `terminal_ids` remain the canonical session references, and
Work Kits are not rewritten. Layout assignments are stored separately in the
private JTT-only `jtt_layouts.json`; legacy or unassigned terminal references
appear once in a virtual default folder/window until the user organizes them.
Deleting a folder or logical window only returns its terminal references to
that default location; it never stops a PTY. Mini LIVE previews are in-memory,
read-only observations of visible cards only. They do not acquire input leases,
resize PTYs, consume the interactive output cursor, or persist screen contents;
stale or unsynchronized previews are cleared. An output-change marker means
only that terminal output changed, not that a command or Agent turn completed.

JTT is an interactive local terminal, not a security sandbox. Shells run as
the current user and can access that user's normal files and network. The
current pyte-based renderer supports a bounded VT subset; it does not claim
full xterm or every Agent TUI's compatibility. The user-confirmed Windows
Terminal/WSL2 core UX result is separate from automated tests.

## Legacy Tmux Agent Tower

The following documentation describes the existing `tower` tmux Agent
monitor, not the standalone JTT terminal workspace.

![Tmux Agent Tower showing synthetic Shell demo tasks](media/tower-readme-loop.gif)

> Demo data: synthetic Shell task rows; this image does not show a live Agent
> run or a verified Result.

[Try the v0.3.0-rc2 pre-release](https://github.com/ju0o/tmux-agent-tower/releases/tag/v0.3.0-rc2) · [Install](#install)

## Quick Start

1. Install Tower using the instructions below.
2. Run `tower` inside tmux.
3. Press `+`, choose an environment if prompted, then a project and Agent.

## What Tower helps you do

- See work and attention across tmux panes.
- Start tasks locally or in a configured SSH environment.
- Result copy depends on Agent/provider evidence; in one isolated test, Tower v0.3.0-rc2 with Codex CLI 0.161.0 did not return the full native response.

## Supported Agents and limitations

Recognized CLIs: Codex, Claude Code, OpenCode, Grok CLI, and Cursor Agent CLI.
Cline native full Result copy is not supported; other commands use the generic
Shell/SSH fallback.

Status detection is best-effort and can change when Agent CLIs change. Result
copy depends on a verifiable complete turn and varies by Agent, SSH setup,
terminal, and clipboard. Generic remote overview does not show full pane
contents. v0.3.0-rc2 is a pre-release; see
[`Limitations`](#limitations).

[Install](#install) · [Documentation](docs/STATUS_ENGINE.md) · [Contributing](CONTRIBUTING.md) · [Report an issue](https://github.com/ju0o/tmux-agent-tower/issues)

If you run Codex, Claude Code, OpenCode, Grok CLI, or Cursor Agent CLI in
several tmux panes at once (maybe across more than one machine), it's easy
to lose track of which one is still working, which one is stuck waiting
for your approval, and which one finished ten minutes ago. Tmux Agent
Tower is a small, local-first TUI that lists them all in one screen and
jumps you straight to the one that needs you.

## What it is (and isn't)

* **Local-first**: no server, no account, no cloud backend. It reads your
  own tmux server (and, optionally, a second host over your own SSH
  connection) and shows it to you.
* **No background typing**: watching a pane does not type into it.
  A prompt, an approval key, a rename, or a close runs only after you
  choose it, and it targets the tmux pane id on the tmux host. It does
  not follow the host name used to group the row, and it does not touch
  agent credentials.
* **Best-effort status**, not a guarantee. Status is inferred from
  terminal output patterns and process activity, not each agent's
  internal API (most don't expose one). New versions of an agent's CLI can
  change its UI and break detection for that agent -- see
  [`docs/STATUS_ENGINE.md`](docs/STATUS_ENGINE.md).

## Features

* Work Groups keep related tasks together. Group rows summarize work in
  progress, attention, and new results; ungrouped tasks remain visible on
  their own. Task rows put the role, Agent, and status first.
* The default home organizes work as **Folder → work screen → task**.
  Folder names, work-screen names, ordering, and collapse state persist in
  Tower without changing tmux windows or panes. Unfiled windows appear under
  **Other**. Work Groups remain a separate way to describe an Agent team.
* A compact selected-task summary shows its name, Agent, role, status,
  project, and execution location. Internal terminal identifiers are in
  the advanced terminal view.
* Persistent conversations keep the live output and message composer on
  one screen. Multiline prompts and per-task drafts are supported. Result
  copy is available only when the selected Agent's provider can verify a
  complete turn; see [Limitations](#limitations) for tested exceptions.
* `L` opens Live directly for the selected task or Work Group. A task opens in
  focus view; a group uses its saved focus, split, grid, or main-and-side
  layout. Groups with more visible members move through pages instead of
  squeezing every task into the screen. With no task selected, choose a layout
  and its members. `Enter` opens the selected conversation, `Y` requests the newest Result
  that Tower marks complete (see Limitations), `Space` opens its existing task menu, and
  `G` focuses its terminal. PageUp/PageDown scroll output; Home returns to the
  latest output. Short or narrow terminals show one focused task. Remote,
  ended, or identity-changed panes never reuse old output. Live reads with
  `capture-pane` and does not change tmux layout. Focus, split, grid,
  main-and-side, the 42×14 fallback, and the 160×45 layout were exercised in
  an isolated tmux session.
* `/` opens one search and command palette. Search tasks, Work Groups,
  projects, Agent names, folders, and screens, or choose a matching command:
  create a task, add or view SSH environments, open Live, saved setups,
  templates, settings, or advanced workflows. Use `↑`/`↓` to move, `Enter`
  to open, and `Esc` to close.
* Project name is auto-discovered with a real priority chain: your own
  override, then the enclosing git repo's name, then a meaningful pane
  title, then the directory name -- a meaningless single-letter or
  mount-point-ish basename (`f`, `mnt`, ...) is never shown as-is if
  anything better is available.
* Per-host summary counts hide any status with a zero count instead of
  spelling out `?0 ×0` for statuses that aren't happening.
* Works across a second host over SSH, degrading gracefully to
  `UNKNOWN`/offline if that host isn't reachable -- the local TUI never
  blocks waiting on it.
* A one-line **current activity** guess per pane (e.g. `Calling
  some_tool...`, `~ Preparing write...`), extracted purely from
  already-captured terminal text -- no LLM calls, nothing leaves your
  machine, and raw terminal content is never written to disk. Shown only
  when there's real evidence for it; hidden rather than guessed when
  there isn't.
* **Status duration**: how long Tower has continuously observed a pane in
  its current status (`대기 · 4m`), shown next to the status and in the
  detail panel.
* Execution status, attention, and complete-result availability stay
  separate. The home view and Work Group summaries surface approval,
  questions, and new complete results without exposing terminal IDs.
* Optional, **off-by-default** notifications on a genuine status change
  (e.g. an agent starts waiting on approval or a question), via `notify-send` or a
  `tmux display-message` fallback wherever there's no desktop
  notification daemon. One-shot per transition -- no repeat spam while a
  pane just sits in the same status.

## Install

Requires Python 3.9+, tmux, and a terminal `TERM` that supports curses
(any normal one does).

```bash
git clone https://github.com/ju0o/tmux-agent-tower
cd tmux-agent-tower
./scripts/install.sh
```

The installer:

* installs the `tower` command for your user (`pip install --user -e .`,
  or into a venv if `pip install --user` is externally managed),
* appends a single `source` line to your `~/.bashrc` (only if not already
  present),
* backs up any dotfile it touches first (`<file>.bak.<timestamp>`),
* does **not** touch `~/.tmux.conf` or rebind any tmux key by default --
  see "Optional: `Ctrl+b w` shortcut" below.

Run `./scripts/uninstall.sh` to remove what it added (it will tell you
exactly what it's about to undo before doing it).

## Usage

Inside any tmux pane:

```
tower
```

runs the TUI right there, in that pane. Press `M` → **Start phone
remote** when you want the same view on your phone (no shell command).
If you start another Tower in a different pane later, that one becomes
"the" active Tower -- older ones
keep running, nothing is killed, but `tower --focus` (see below) will jump
to the newest one.

To open a Tower session on another SSH host while keeping this device as
the clipboard destination:

```bash
tower connect work-server
```

Use an existing OpenSSH config alias or address. The remote host must have
one active Tower session; the connector follows `ssh -G` configuration and
passes the originating Access Client through nested SSH. If several Tower
sessions are active, select one with `--session`.

Tower remembers which pane it's running in for the rest of that tmux
session (by pane id, not by window name or position -- a renamed or
reused window never confuses it). From anywhere else in that session:

```
tower --focus
```

jumps straight back to it without starting a new one. This is what the
optional `Ctrl+b w` shortcut below is for -- it's a plain navigation
command, not another TUI, so it's safe to bind to a key.

| Key | Action |
|---|---|
| `↑` / `↓` (or `j`/`k`) | Move selection |
| `Enter` | Fold/expand a folder or work screen, or open a task |
| `Space` | Open actions for the selected folder, work screen, or task |
| `+` | Start a task with environment → project → Agent |
| `S` | Open a saved setup or template |
| `L` | Open Live; a selected Work Group opens as one view |
| `C` | Open settings |
| `/` | Find work, folders, screens, and Agents, or run a command |
| `Y` | Request the latest result Tower marks complete; availability varies by Agent (see Limitations) |
| `G` | Open the selected task in its real terminal |
| `Esc` | Go back one level |
| `?` | Show the basic help |
| `Ctrl+C` | Quit Tower |

### Task identity and advanced details

The main UI uses one task name. Quick Start names a task from the project and
Agent (for example, `ExampleProject · Codex`). The task menu's **Rename** action sets a name that
persists across refreshes. Duplicate task names are allowed.

The task menu separates identity settings from moving and layout actions.
Project means the bound project and path; changing it keeps the task name.
Agent and role changes refresh only an automatic suggestion. A name set by
the user stays as-is. The phone displays the same task name as the PC.

The actual terminal title, path, execution host, tmux location, and IDs are
advanced details. Pane and Window names are not separate task identities and
are kept out of the basic task edit menu. The Agent label is display metadata;
changing it does not change or restart the real process.

The default list groups only work the user explicitly selects. Ungrouped work
stays visible, and grouping, removing a member, or ungrouping changes saved
Tower metadata only; it never moves, renames, or closes a tmux pane or window.
Work Group names, membership, and member order persist across Tower restarts
and appear in the same order in the phone view.

Moving a task changes its Work Group membership only. Tower view layout and
member slots are separate from the physical terminal layout. Applying a
terminal layout or moving a pane is a distinct confirmed action, currently
limited to fresh, Tower-managed local panes in windows with no unrelated panes.
Phone controls change logical membership and layout only.

### Saved setup and templates

Choose **Save** from a Work Group menu to save the current setup. A **Saved
setup** reopens a project and Agent setup you used before. A **template** reuses
an Agent and role setup in another project while leaving out
concrete project paths and personal execution hosts, so it can be reused for a
different project.

Open saved setups and templates with `S`. A template can use a
different project path and Agent per role.
Missing paths or unavailable local Agents fail before launch. Each launch uses
new Tower-created resources and makes one logical Work Group; stored layouts
contain no tmux pane or window IDs. Partial local launch failures clean up only
the new window. Phone summaries omit project paths and execution hosts.

The versioned store is `~/.config/tmux-agent-tower/worksets.json`. Writes are
atomic and private to the current user. Corrupt or newer-format data is kept
untouched. The older `workspace-presets.json` remains for existing Workflow
integration and is separate from Work Group based Saved Work and templates.

### Advanced work creation

Asks where to work, then opens that host's workspace browser: recent
paths, a search under the configured roots, a folder tree, or a path you
type. Every row shows the full path. A plain folder is allowed, not only
a git repo. The folder view is a tree: opening a row reads that directory only,
and the folders above it stay on screen. Enter expands. Space chooses
the workspace.
Next you pick an agent, where it should live (a new window, a pane in
the current window, or an existing window), and a layout, then create.
This flow is under the advanced create menu, reachable with `N` or `W`. The keys open the same
new-work chooser for keyboard compatibility. A remote host uses the same screens. Listing, path checks, and search are
read-only SSH, one directory at a time, and they do not install Tower
there. tmux runs only after you confirm. The new panes are `cd`'d into
the path you chose, run the agent you chose, and show up in Tower.

This is explicitly *not* the same thing as controlling an already-running
agent: the launcher only ever creates new panes and never sends input to,
closes, or reconfigures anything that existed before it ran -- see
[`docs/ROADMAP.md`](docs/ROADMAP.md) for why that distinction matters and
what's still deliberately unbuilt (the "Action Layer").

### Workflow order (Advanced)

**Advanced settings → Workflows** previews role recipes and creates a step-by-step
run from a separately selected saved setup. A saved setup describes
the host, paths, and available agents; a workflow definition contains only its
name and ordered roles. The built-ins are Quick Fix (Builder → QA), Feature
Development (Planner → Builder → Reviewer → QA), and Focused Improvement
(Orchestrator → Planner → Builder → QA → Dogfood → End to end). Recipes with
a human step show it explicitly. Previewing or creating a run does not send
anything to an Agent.

Project search locations come from `~/.config/tmux-agent-tower/config.toml`:

```toml
project_roots = ["~/Projects", "~/code"]

[agents]
claude = "claude-beta"   # override the command used to launch an agent
```

Both keys are optional -- with no config at all, the launcher looks for a
handful of common directory names under your home folder (`Projects`,
`code`, `dev`, `src`, ...) that actually exist, and uses each agent's
default command name. A command is checked with `command -v` (locally, or
on the target host for a remote launch) right before spawning; if it's
missing, that one project's pane still opens as a plain shell with a
"command not found" title instead of aborting the rest.

### Optional: `Ctrl+b w` shortcut

`Ctrl+b w` stays tmux's window list until you opt in. Tower never
rewrites `~/.tmux.conf` on install. Inside Tower, `C` → **tmux shortcut**
→ **Use smart Ctrl+b w** (or `tower keys install`) adds one marked block:

* Tower is running in this session → `Ctrl+b w` jumps to that pane
  (`tower --focus`, pane id, not a window name).
* Tower is not running, or the registration is stale → tmux's window/pane
  chooser. That command is whatever a tmux server with an empty config
  reports (normally `choose-tree -Zw`). It is not assumed, and it is not
  a separate control-window command.
* A custom `w` binding, including one from a `source-file`, is saved and
  put back only by `tower keys restore`. It does not run while the smart
  binding is installed.

Quitting Tower does not unbind the key. The same binding checks again
the next time you press it. `tower keys restore` removes only that block
and puts the recorded `w` command back. `tower --focus` still only
navigates; it never starts a new TUI.

The default home shows **Folder → work screen → task**. Enter folds or opens a
folder/work screen; Enter on a task opens its conversation. Windows without a
folder appear under **Other**. Work Groups remain a separate logical Agent-team
membership and are shown with their tasks. The physical host/window/pane view
is available from **More → Terminal structure**. In a task conversation, `Y`
requests the latest result Tower marks complete. Copy availability and
completeness depend on the Agent provider; this is not verified for every
Agent version. See [Limitations](#limitations).

`+` starts a task with environment, project, and Agent. `N` and `W` open the
advanced create menu for folders, Work Groups, multi-project launches, and
terminal structure. `S` opens saved setups and templates together. Space opens actions for the
selected folder, work screen, or task. If Tower has no work yet, the same
start choices are shown in the list.

Every structure write uses the window id or pane id tmux returned.
A window name is only a label, so two windows with the same name stay
separate. New windows and splits use `-d`, so the client stays where it
is. Closing a pane warns when work, an approval, or an input question
is in progress. Tower's own pane, and any window that contains it,
cannot be closed from these menus. The phone shows the same tree and
the current location. It does not create or close windows.

Pane Control keeps the live pane capture above an inline composer. Enter
sends the message and leaves the detail open with the composer ready for
the next turn. Ctrl+O inserts a manual newline. Multiline pastes keep
Korean text, CRLF/LF line breaks, and code fences intact.
The composer says `Message` for an Agent, `Command` for a shell, and
`Answer` for a measured text question. A measured approval or choice
replaces the composer with its observed safe controls; an unknown
interaction blocks input. Submit confirmation is based on visible
agent-side evidence, with no blind retry.

In Pane Control, `Ctrl+Y` requests a copy of the latest result Tower marks
complete, `Ctrl+S` shows it,
`Ctrl+E` edits the display name, `Ctrl+G` moves to the pane, and `Ctrl+X`
asks before closing that pane. Cancel is the default. Tower's own pane
cannot be closed there. `Ctrl+b w` still comes back. Tower does not guess
approval keys or Enter. `Ctrl+I` opens advanced actions; `Ctrl+L` copies
the current live screen as a separate action.

### Hosts

The tree groups panes by where the work runs. Each pane still has three hosts:

* **Observer host** — the machine where this Tower process is running.
  An SSH login used to reach that machine does not change it.
* **Tmux host** — the machine whose tmux server owns the session and
  pane id. Prompt, focus, rename, and close use that pane.
* **Execution host** — where the shell or agent is working. An `ssh`
  client inside a local pane is grouped under the destination and
  labeled `via` the tmux host. If the remote directory is not known,
  the project stays unnamed.

A configured peer's own tmux is a separate read-only snapshot. The same
pane id on two servers is not one pane. See
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

### `tower --doctor`

Runs a quick environment check (tmux/Python/curses availability, whether
you're inside tmux, configured remote hosts) and prints PASS/WARN/FAIL per
check.

### Checking and applying updates

Tower checks for updates only when asked. Run `tower --check-update` to compare
the installed runtime with the official Release channel, or
`tower --update --dry-run` to preview a safe update. See [`docs/UPDATING.md`](docs/UPDATING.md)
for channel selection, confirmation, migration, and rollback behavior.

### A second host

Use **Settings → Connections → Execution environments → Add SSH environment**
to save a display name and an SSH alias (or `user@host`). Existing entries in
`~/.config/tmux-agent-tower/remote-hosts.txt` remain supported:

```
remote-workstation:Remote workstation
```

The Tower will then show that host's panes too (title/command-based
status only -- see limitations below), refreshed less frequently than the
local host, and marked `UNKNOWN`/offline gracefully if it's unreachable.
Nothing is installed on the remote host. New task runs use environment →
project → Agent and start the process on the selected remote host. An offline
profile requires an explicit retry or another environment; Tower never falls
back to local execution.
Directory listings, path checks, and project search are read-only SSH.
One directory is fetched at a time and cached. If the host does not
answer, Tower says it cannot connect and the local session keeps
working. Search stays inside the usual project directories, with a
depth and result cap.

### From your phone (experimental; included in v0.3.0-rc1)

Inside Tower, press `M` and choose **Start phone remote**. Tower prints
an `https://…ts.net` address and a pairing code on screen. Save that
address on your phone (home screen or bookmark). The header shows
`Remote: ● connected` while it is actually up.

Quitting Tower with `Q` does not stop the phone remote. Stop it from
`M` → **Stop remote**. `M` → **Autostart** can start it for you the next
time you run `tower`.

The same service is available from a shell for debugging
(`tower serve`, `tower serve --lan`, `tower serve --tailscale`). You
don't need those for normal use. Read `docs/REMOTE.md` first -- it is
the security model and the list of what this deliberately does not do.

### First task and language

An empty Tower home offers **Start your first task**, **Open saved setup**,
and **Use a work template**. Starting a task asks for an environment, project,
and available Agent, then opens its conversation. Tower chooses the task name
automatically. When the current
folder looks like a project, Tower offers it as the first choice. No
remote connection or clipboard setup is required; copy destination starts
on automatic.

Tower opens directly to Home on first launch. Change Korean/English later
from **Settings → Language**; the choice is also stored in
`~/.config/tmux-agent-tower/config.toml` (`language = "ko"` / `"en"`).

### Task awareness settings

Also in `~/.config/tmux-agent-tower/config.toml`, all optional and all
defaulting to the quiet/conservative choice:

```toml
show_activity = true          # default: true
show_status_duration = true   # default: true
notifications = false         # default: false -- opt in explicitly

[notifications]
waiting = true   # default: true (once notifications = true)
dead = false     # default: false
```

`notifications = true` alone is not enough to get every kind of alert --
each transition kind (`waiting`, `dead`) is independently toggleable, and
a WORKING → IDLE transition is deliberately not offered as a notification
kind at all: Tower can tell a pane stopped changing, not that a task is
actually "done."

## Supported agents

Codex, Claude Code, OpenCode, Grok CLI, Cursor Agent CLI, and a generic
shell/SSH fallback for anything else. Each adapter owns its own
execution and attention detection. There is no shared regex that marks
every agent as waiting. Codex and Claude Code numbered approval menus
are the only ones Tower will answer with a key (`1` / `3`). OpenCode
and Cursor can be recognized as approval from fixtures, but Tower does
not send a key for them because those widgets were not verified live.
See [`docs/STATUS_ENGINE.md`](docs/STATUS_ENGINE.md). Detection is
inherently best-effort and will drift as these CLIs' UIs change; PRs
updating a pattern (with a sanitized fixture) are welcome.

## Status meanings

* `● WORKING` -- actively producing output or showing an agent-specific
  "I'm working" signal.
* `○ IDLE` -- alive, with no agent-specific working signal. An approval
  or a question can still be set on the attention axis at the same time.
* `! 승인 필요` / `? 입력 필요` -- attention, not a status. Approval is a
  permission widget. Input is a free-text or choice question. They are
  never the same mark.
* `◇ UNKNOWN` -- not enough evidence either way (`확인 불가`). This is a
  deliberate, honest fallback -- see `docs/STATUS_ENGINE.md` for why
  "unknown" beats a confident wrong answer. The mark is not `?`, which
  is reserved for input needed.
* `× DEAD` -- the pane or its process has exited.

Visited state is completely independent of status -- it only tracks
whether you've opened that pane from the Tower before, shown as a small
`NEW` marker in front of a row you haven't (nothing extra once you have).

## Limitations

* Status detection is pattern-based against each CLI's *current* terminal
  UI. It will drift out of date as those tools change; PRs updating a
  pattern (with a sanitized fixture) are welcome.
* Cline CLI is shown through the generic Shell/SSH fallback. Native Cline
  transcript extraction and complete Result copy are not supported.
* Some agents do not expose a reliable completion boundary. In one isolated
  test of Tower v0.3.0-rc2 with Codex CLI 0.161.0, both short and long Result
  payloads differed from their native responses, and the long candidate was
  marked complete despite containing only a suffix. Avoid Ctrl+Y in this tested
  setup until a maintenance fix is available. Other Codex versions and
  configurations were not tested.
* SSH execution and clipboard delivery depend on the host, terminal, and
  clipboard combination. Not every combination has been verified.
* The remote/multi-host view only sees pane titles, not full content (a
  deliberate simplification to avoid installing anything remotely -- see
  `docs/ARCHITECTURE.md`), so remote status is coarser than local.
* An `E` override (project/agent/title) is kept only while the session
  and the pane's process id still match. A reused pane id does not
  inherit the previous label. Setting the label again on the pane you
  are looking at makes it current.
* Linux/WSL + tmux is the tested target. macOS should mostly work (same
  Python + tmux + curses stack) but hasn't been verified here.
* v0.3.0-rc2 is a pre-release, not a stable release.

## Uninstall

```bash
./scripts/uninstall.sh
```

## Roadmap

See [`docs/ROADMAP.md`](docs/ROADMAP.md). Automatic relay, killing or
restarting an agent, and auto-approving prompts are not in this version.
On this branch you can send one prompt you typed into the pane you
selected, and you can create, move, and close local windows and panes
after you confirm. The phone shows the same rows and can send that same
prompt. It does not create or close windows.

## License

MIT -- see [`LICENSE`](LICENSE).
