# JTT Windows terminal backend: integration boundary

## Status

This branch adds an experimental native Windows terminal registry backend. It
does **not** make JTT's full terminal UI a supported native Windows product.
The validated host is one isolated Windows 11 environment. Windows 10, native
Windows Terminal visual interaction, and the complete JTT three-panel flow are
not validated.

## Platform boundary

`tmux_agent_tower.terminal` is the UI-facing boundary. It exports the existing
`TerminalRegistry`, `TerminalClient`, renderer, identity, observation, screen,
and update contracts. On Linux it continues to select `terminal.manager`; on
Windows it selects `terminal.windows_manager`. The Linux worker and its socket,
UID, and POSIX process checks remain the Linux implementation.

The Windows backend uses a dedicated worker and a pywinpty ConPTY. An explicit
create request starts Windows PowerShell 5.1. `TerminalClient` exposes the same
main operations the UI needs: observe/poll, acquire/release input lease, send,
resize, detach, reconnect, and identity-checked stop. The backend does not
launch Agent-specific commands.

The current curses UI is not Windows-ready: `ui.jtt` imports `curses`, while
diagnostics and neighboring launcher/update code use POSIX-only UID and file
ownership checks. Those layers need a separate platform audit and port before
connecting this backend to the full three-panel application.

## Process and session safety

- Each registry record has a random session ID and generation. Worker and shell
  process creation times are checked on reconnect and control operations.
- Windows virtual-environment `python.exe` can be a redirector whose PID differs
  from the executing interpreter. The worker therefore launches using
  `sys._base_executable` with `-S`, an explicit JTT/runtime import path, and
  user-site imports disabled. This keeps the recorded worker PID tied to the
  process that owns the named pipe and Job Object.
- The PowerShell bootstrap waits for either a per-session release event or an
  abort event, with a bounded timeout. It exits on event-open failure, abort,
  or timeout; it cannot fall through to an interactive prompt. The worker
  assigns the retained PowerShell process handle to its Job Object before
  releasing the prompt. Assignment failure terminates only that retained
  child handle.
- The worker owns a Job Object configured with
  `KILL_ON_JOB_CLOSE`; explicit stop terminates that job. Startup-failure
  cleanup checks PID, creation time, and the expected PowerShell image before it
  can terminate the just-created shell. If ConPTY spawn was attempted but the
  worker could not publish a complete shell identity, startup fails closed and
  the registry record is retained for explicit recovery instead of signaling
  an unverified PID or deleting the evidence.
- Session directories and named pipes use an owner-SID-only DACL. The server
  checks the pipe caller SID, and the client verifies the named-pipe server
  PID. This separates Windows users; it does not isolate processes running as
  the same Windows user.
- A single pipe client is accepted at a time. Disconnect releases that
  connection's input lease. Simultaneous read-only Mini LIVE plus RIGHT input
  clients are not supported by this experimental implementation.
- PTY output is kept in bounded worker memory and is not persisted. Registry
  metadata records session/process identity, shell path and display name, pipe
  name, state, dimensions, working directory, and a path-free startup failure
  category; it does not contain terminal output.

## Evidence in this branch

On an isolated Windows 11 host, the backend test exercised PowerShell creation,
prompt/output, lease-gated input, 80x24 resize and reported console width,
Korean output, stale-generation rejection, detach/reconnect with the same
session ID/generation/shell PID, lease revocation, and identity-checked stop.
The test also checked the Windows venv worker executable's PID behavior and
that a wrong process creation time cannot terminate the shell.

The startup-failure regression injected an `OpenProcess` denial after a real
ConPTY PowerShell spawn. An independent observer captured that child PID and
creation time; the abort event caused that exact PowerShell process to exit.
The worker could not retain its own process handle, so its startup registry
record remained present. A separate native PowerShell check confirmed that a
missing named gate exits with the expected failure code instead of opening a
prompt. The manager cleanup tests also reject incomplete or inconsistent
startup identity without signaling a PID or removing the registry.

The existing isolated ConPTY feasibility suite also passed on that host. This
is automated backend evidence, not a native Windows Terminal visual or human
UX acceptance result.

## Not yet validated

- JTT's full three-panel curses UI, workspace store, and `jtt doctor` on native
  Windows.
- Windows 10, Windows Server, macOS, and real user-account/SID separation.
- Native Windows Terminal visual QA, mouse behavior, Unicode input edge cases,
  and broad application/TUI compatibility.
- Multiple simultaneous pipe clients and concurrent observe/input behavior.
- Process/job behavior under every host policy, nested Job Object configuration,
  endpoint security product, and forced OS shutdown.
- Full PTY semantics for terminal queries, bracketed paste, mouse protocols,
  truecolor, and xterm extensions.

Until those gates pass, Windows native support remains **not supported / under
validation**. The backend must not be advertised as completion of Windows
product support.
