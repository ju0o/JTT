# Architecture

```
src/tmux_agent_tower/
├── adapters/       agent-specific status opinions (Codex, Claude, ...)
├── resources/      packaged role-specific Markdown harness defaults
├── role_harness.py loads local role text; no Agent/provider binding
├── detection/      status engine, project auto-discovery, process helpers
├── tmux/           tmux binary wrappers: read, navigate, and
│                   id-targeted structure changes (local TUI only)
├── state/          visits, identity overrides, folders, Work Groups, worksets
├── remote/         minimal SSH-based multi-host prototype (read-only)
├── launcher/       config, discovery, read-only browse, spawn
│                   (browse never creates a pane -- see docs/ROADMAP.md)
├── i18n/           translator layer (ko/en catalogs); UI text only, never
│                   business logic
├── ui/             the curses TUI, folder/work tree, browser, and menus
├── notify.py       opt-in, off-by-default status-transition notifications
│                   (notify-send, falling back to tmux display-message)
└── main.py         `tower` CLI entry point
```

`Ctrl+b w` does not start Tower and does not run a `tower` binary from
`PATH`. The key reads `@tmux_agent_tower_pane` and `@tmux_agent_tower_pid`.
A live registration selects that pane. Anything else, including a dead
pid or a pane id whose process is no longer that Tower, runs
`choose-tree -Zw`. A helper such as `tmux-control-open` is not the key's
action.

`launcher/` is deliberately separate from `remote/`: `remote/collector.py`
only ever reads (P3), while `launcher/spawn.py` writes -- it creates new
windows/panes, locally via direct `tmux` calls and remotely via a
generated shell script run once over SSH (see its module docstring for the
exact rules it enforces). `launcher/browse.py` stays on the read side for
both machines: one directory per listing, no tmux until create is
confirmed. Keeping that as its own package makes the read-only vs.
write-capable boundary obvious at the directory level.

## Folders and work screens

The default home projects a shared workspace tree: **Folder → tmux Window asset →
task/Agent**. A Folder is a Tower label and membership list, not a filesystem
directory, tmux session/window, or Work Group. A window asset keeps an opaque
stable reference derived from its tmux host, session, window identity, and
creation marker; its user-facing name is stored separately from tmux's title.
Tasks continue to use their existing stable target IDs and status/result model.

`state/folders.py` persists folder membership, order, collapse state, and
user-facing window names in `~/.cache/tmux-agent-tower/folders.json`. `FolderStore`
operations are logical only: moving a window between folders, renaming, folding,
or deleting a folder never invokes a tmux structure command. Deleting a folder
leaves its windows unfiled. Unknown or stale window references are retained and
shown as unavailable rather than being discarded. Unfiled live windows appear
under **기타 / Other**.

`FolderStore.workspace()` is the normalized read projection used by both the
TUI and the authenticated `/api/workspace` endpoint. The projection contains
folder/window membership and order, task identity, status, attention, result
state, and aggregate counts; it omits filesystem paths, pane IDs, process IDs,
and captured terminal text. This is the stable Tower Core shape intended for a
future Juact Explorer, Cards, or Canvas renderer. The phone uses the same folder,
window, and task projection in a vertical accordion.

## Work Groups

`state/work_groups.py` stores explicit membership by stable `target_id`, plus
the display name, member order, optional project label, and last-known member
labels. A Work Group describes a team/goal; it is independent of the Folder →
Window tree. Work Group membership is still displayed with its tasks and its
own Live/status summary. The terminal-structure view remains available from
`Space` → `더보기` → `터미널 구조 보기`; it is a separate advanced view and
never changes the default launch mode.

Group membership, ordering, renaming, and dissolution write only the local
Work Group state. They do not call tmux structure operations, move or rename
panes/windows, or combine member results. Existing tasks are not grouped
automatically. Missing members remain saved and render as unavailable. The
phone status API publishes the same group IDs, names, membership, order, and
summary; phone group cards open the existing member detail view.

`작업 옮기기` changes only Work Group membership. Member order, logical layout,
and slot assignment are saved separately from the physical terminal layout.
The Tower Live view can use the logical layout without changing tmux. Applying
that layout to a terminal is a separate confirmed action and currently requires
every pane in one local window to have fresh Tower ownership records. Moving a
pane to another managed window or detaching it preserves its pane ID and PID;
the Result identity and ownership record are rebound to the new window. A
window with an unowned pane, stale process, or another tmux host is rejected.
Phone controls only change logical membership, order, and layout.

## TUI refresh and input

The Home, Conversation, and Live loops poll keyboard input every 25 ms. A
single background worker refreshes tmux, process, SSH, status, and Result
observations into a private Tower snapshot; the curses thread adopts that
snapshot between key events and reuses the current workspace projection while
the selection moves. Conversation and Live render the latest captured pane
lines carried by that snapshot instead of running `capture-pane` during every
draw. Refresh subprocesses receive no terminal stdin, so they cannot consume
keys intended for curses.

Workset launches record local managed-resource identity (session, pane, PID,
window, host, and launch ID) in the Work Group store. Existing or manually
created panes without that record remain protected as user-owned. Physical
rearrangement never moves or resizes an unrelated pane.

## Saved Work and Work Templates

`state/worksets.py` stores both concepts in a versioned, atomic
`~/.config/tmux-agent-tower/worksets.json`. Saved Work records a Work Group's
project paths, member display names, roles, Agents, order, execution preferences,
and a logical layout. A Work Template keeps the role/member structure, default
Agent preferences, order, and layout slots, and rejects concrete project paths,
project names, execution hosts, and member display names. Neither form stores
tmux pane, window, session, or process IDs.

`launcher/workset_launch.py` validates all paths and local Agent commands before
creating resources. Members are grouped into one newly created window per
execution host; the resulting targets are then registered as one Work Group in
the saved member order. Per-host launches use the existing local/SSH spawn
functions with transactional cleanup, targeting only windows created for that
launch. A failed group registration rolls those windows back. Phone status
summaries use `phone_summary()` and omit filesystem paths and execution hosts;
the authenticated start route uses the same preflight/launch service.

The older `workspace-presets.json` remains intact for the existing Workflow
integration. It is separate from the new Work Group based Saved Work and
template store.

## Data flow, one refresh cycle

```
tmux list-panes  ──▶  discovery.list_panes()  ──▶  for each pane:
                                                       adapters.resolve_adapter()
                                                       detection.status.StatusEngine.evaluate()
                                                       detection.status.StatusEngine.duration_seconds()
                                                       adapters.<agent>.extract_activity()  (v0.2.0)
                                                       state.visits.VisitStore.visit_label()
                                                       state.overrides.OverrideStore.get()
                                                       notify.NotificationTracker.observe()  (v0.2.0)
                                                     ──▶  display row
remote/collector (per configured host, throttled)  ──▶  display rows
                                                     ──▶  ui.tower.draw()
```

`extract_activity()` and `duration_seconds()` are pure read/observe
calls over data already captured for status detection -- neither adds a
new source of I/O. `NotificationTracker.observe()` is off by default
(see `launcher/config.py`'s `notifications` key) and, when enabled, can
only ever call `notify.send()` (a `notify-send`/`tmux display-message`
fire-and-forget), never anything that reaches into a monitored pane.

Nothing in this path sends input to a monitored pane or touches any
process other than `tmux` itself, `ps` (read-only process listing), and
`ssh` (for the optional remote prototype, also read-only on the far end).
`ssh -G` may also run locally to read an alias from the user's SSH
config. It does not open a connection, and the resolved name stays in
memory.

## Host topology

A pane has three hosts, not one label:

* `observer_host` — the OS where this Tower process is running. An SSH
  login that attached to this machine does not change it.
* `tmux_host` — the machine whose tmux server owns the session, window,
  and pane id. Prompt, focus, rename, and close use this plus
  `window_id` / `pane_id`. They do not follow the execution host.
* `execution_host` — where the shell or agent is actually working.
  A local pane whose process tree contains an `ssh` client is drawn
  under this host. Folding uses the tmux window (`tmux_host`, session,
  `window_id`), not the execution host, the window name, or the pane id.
  The pane's place line still says which tmux the SSH client belongs to.

Configured peer hosts (`remote-hosts.txt`) stay a read-only snapshot of
that peer's own tmux. Those rows are not the same object as an SSH
client pane on this machine, even when the pane ids look alike. To
control the peer's tmux, attach there and run Tower on that machine.
This pass does not add remote write.

## Why a local `tower --collect-json`-on-the-remote-host design was *not*
chosen for P3

The simplest possible multi-host design would install this same package on
the second machine and have it print a JSON snapshot for the first machine
to fetch over SSH. That was deliberately avoided for the v0.1.0 prototype:
it would mean silently installing software on a machine that might have
its own live, unrelated agent sessions running, which conflicts with this
project's "never touch a machine you weren't explicitly asked to touch"
default. Instead, `remote/collector.py` runs a small inline shell snippet
over the user's *existing* SSH alias that only calls `tmux list-panes`
(nothing installed, nothing written, one round trip). The tradeoff is a
coarser remote status (title-only, see `docs/STATUS_ENGINE.md`) in exchange
for zero remote footprint. This can be revisited later if a richer remote
view is wanted badly enough to justify the extra installation step.

## Dev runtime

Start this checkout with `./scripts/tower`. That launcher runs
`scripts/dev-python -m tmux_agent_tower.main` and does not call `tower`
on `PATH`. `dev-python` puts this tree's `src` first on `PYTHONPATH`
and sets `TOWER_DEV_ROOT`. A `.venv` is used only when it sits inside
this checkout. Another checkout's venv and editable install are left
unchanged. A `tower` already on `PATH` can still point at an older
checkout; this launcher does not replace it.

`./scripts/tower --doctor` prints `Tower source:`, `Python:`, `Version:`,
and `Working tree: DEV` or `packaged`. When `TOWER_DEV_ROOT` is set and
the imported file is outside it, the process exits before the TUI and
the doctor reports a source mismatch. `PATH tower:` is `same checkout`,
`not found`, or `OTHER CHECKOUT` plus that command's import path. The
probe drops `PYTHONPATH` so this shell cannot hide the other install.
The phone child is `sys.executable -m tmux_agent_tower.server.service`
and inherits the same environment, so it imports the same file.

## Prompt composer

Opening an Agent with Enter goes to the persistent detail composer. The old
standalone composer remains for legacy callers; `prompt_text()` (names,
paths, agent names, settings) stays single-line and is not touched. `Ctrl+O`
adds a manual newline and Enter submits.

The standalone composer and persistent detail view enable bracketed paste
on entry (`\e[?2004h`) and disable it (`\e[?2004l`) in `finally`, so submit,
cancel, and an exception all restore the mode. The detail view routes ESC
through the same Composer parser before handling bare ESC as Back. Inside
`\e[200~` … `\e[201~`, newlines are text. CRLF is normalized to one LF.
Enter after the end marker submits once. `send_prompt()` preserves
the returned string, including trailing newlines, in one `load-buffer` /
`paste-buffer -p`; the submit key is sent separately.

Without markers the composer uses, in order: input already queued behind
the newline (the newline came with the paste); a newline that follows its
previous key within 25ms (the `assume-paste-time` rule tmux itself uses);
and, only after a paste was already recognised this way, a newline within
300ms of the previous key. A newline after a longer pause with nothing
queued is the user's Enter. The footer then says the pasted line breaks
are kept and one Enter sends. Once a marker has been seen, cadence is not
consulted, so a quick Enter after a wrapped paste still submits.

Measured with a tmux 3.6 client pty, the same `get_wch` path as Tower
(`TOWER_PASTE_TRACE` records event types only, never the body):
the client stream contains `\e[?2004h`, so tmux asks the terminal for
markers. With the pane mode on, `ESC [ 2 0 0 ~` arrives, pasted CRs reach
curses as `\n`, `ESC [ 2 0 1 ~` arrives, and the manual Enter is a
separate `\n` hundreds of milliseconds later. With the pane mode off,
tmux strips the markers, which is why enabling the mode is required.
After the composer closes, a second client paste arrives without
markers, so the mode was restored. One write without markers, and lines
delivered 80ms apart including a lone blank line, keep all 11 lines and
submit on the later Enter.

An older `tower` earlier on `PATH` can import a different checkout. Live
verification therefore uses the repo-local launcher and checks the runtime
import path and doctor output before exercising input.

## Submit and interaction

Opening a local Agent pane enters the persistent control surface: live pane
output remains visible above an inline `Composer`. Ctrl+O adds a manual line;
Enter submits and clears the draft only after `send_prompt` confirms
submission. Failed or uncertain sends keep the draft on screen. A draft is
kept in Tower memory by pane key, so changing panes cannot show another
Agent's text. The composer uses the same 32,000-character limit and paste
handling as the standalone composer. Agent panes say
`메시지`, shell panes say `명령 입력`, and measured text questions say `답변`.
The resolved `agent` identity determines the label. Auto-detected process
identity is used only when that resolved identity is absent and the detection
source is trustworthy; an SSH host or transport by itself does not imply
that the pane is a shell.
Measured approval or choice replaces the composer with only the observed safe
actions. Unknown interactions state that Tower cannot confirm the action and
offer no guessed response. Result, execution, and attention remain separate.
An answered text question clears its draft after the shared action confirms
delivery only if the draft still equals the sent text, preserving edits made
while the request was pending. Ordinary prompts still clear only after a new
turn is confirmed.
Ctrl+Y copies Result, Ctrl+S shows it, Ctrl+E edits names, Ctrl+G focuses the
pane, Ctrl+N rejects only a measured rejection, Ctrl+X closes where allowed,
and Esc returns to the list.

## Confirmed Agent focus handoff

Control Room remains an observation screen until the user chooses `Agent
direct control` and confirms the named task, Agent, computer, and Window. Tower
does not relay arbitrary keys: after the handoff, the selected local tmux Pane
and tmux handle keyboard input. A MainPC SSH Client Pane can be eligible only
when its live process-tree destination matches the observed SSH target; an
ASUS `remote_tmux` peer is not a local handoff target.

The handoff rechecks the Tower target ID, Host, Session name and ID, Window ID,
Pane ID/PID, transport, and SSH target against a fresh read-only tmux/process
snapshot immediately before focusing. Because focusing uses separate
`select-window` and `select-pane` commands, tmux cannot make identity check and
selection atomic. The checks reduce but do not eliminate the race. Identity
change or uncertain command completion stops the sequence; if the Window was
already selected, Tower reports a partial handoff and does not attempt an
automatic rollback or another target. Tower itself is not terminated. Return
is manual through the user's existing tmux navigation; Tower promises no
fixed prefix or one-key return. Its existing snapshot refresh continues, and
a disappeared/reused Pane is shown as stale rather than inheriting old LIVE
output.

## Isolated local UX sidecars

For an explicitly isolated local UI run, `TMUX_AGENT_TOWER_HOME` redirects
Tower-owned config, cache, saved projects/screens, result metadata, remote
service metadata, and update data beneath a separate absolute directory. The
process `HOME` is not changed, so OpenSSH continues to use the existing user's
SSH profile and authentication-agent socket. Optional
`TMUX_AGENT_TOWER_REMOTE_HOSTS_FILE` and `TMUX_AGENT_TOWER_HOST_FILE` point to
existing read-only Tower host profiles; Tower only reads these files.

This does not isolate tmux session options. Tower registration is
session-scoped and last-write-wins, so a sidecar must run in its own tmux
session on the existing tmux server; using the same session can replace the
active Tower registration. Isolated mode refuses Tower's tmux key-binding
install/restore actions and physical Pane-title synchronization. For useful
read-only discovery from that separate session, isolated mode lists panes and
windows server-wide (`tmux list-panes -a` / `list-windows -a`) while excluding
only its own Pane; normal Tower runs remain scoped to their own session. Keep
its config absent/defaulted so Remote autostart stays off. This mode alone
does not authorize creating that session, launching the TUI, connecting to
remote hosts, or handing focus to an Agent.

`send_prompt` sends one initial Enter. Only Cursor can receive one more,
after a fresh capture shows the prompt head as the first nonblank text
below the current `Add a follow-up` composer. A prompt echo above that
line is transcript, not proof the composer still holds the text. Any
`Running … tokens`, a braille spinner plus `Working`, or other confirmed
submission evidence suppresses the second Enter; in particular, a first
Enter that already shows `Running` is never repeated. Codex, Claude, and
OpenCode never receive a second Enter. Their submission checks compare
agent-specific current-turn evidence with the pre-send capture, so an old
working marker, completion row, or identical old prompt echo is not enough.
Missing evidence stays `submit_not_confirmed`.

`detect_interaction` reads the current bottom widget. Approval, numbered
choice, yes/no confirmation, text answer, and unknown are separate types.
Codex and Claude expose only measured keys. Cursor's visible `(y/n)` is a
confirmation with no safe keys until its key behavior is measured; the
screen directs the user to choose in the real terminal. Unsupported or
incomplete evidence is shown as unknown. A question with no menu is text
and reuses the prompt composer. Nothing selects an option by itself.

Automated evidence is in `tests/test_input_transport.py`,
`tests/test_interaction.py`, and `tests/test_prompt_composer.py`, including
the ESC-prefixed paste path through the persistent detail view. On
2026-10-05, a live OpenCode Go run in a private tmux server sent one Korean
multiline code-fence turn and an immediate follow-up through this view.
Both turns showed `WORKING` and completed to `IDLE`; Tower used exactly one
buffer load, one paste, and one Enter per turn, with no other Agent keys.
The same detail stayed open with live output and an empty composer after
each completion. Sanitized pane/PID identifiers, state transitions, screen
hashes/counts, and payload hashes are in
`/tmp/tower-wbs05/evidence/wbs05-live-e2e.json`; prompt/result bodies and
the temporary OpenCode session data were removed. Approval/choice actions
remain covered by focused tests; this live run did not encounter one.

## Tower LIVE view

`L` opens Live for the selected Work Group or task; a selected task opens in
focus view and a group reuses its saved `focus`, `split-2`, `grid-4`, or
`main-plus-side` descriptor. A group with more tasks than the visible slots
uses `n`/`p` pages. With no selected task or group, Live keeps the explicit
layout and member picker. A selected card routes Enter to the existing
Conversation Surface, Y to the complete Result resolver and clipboard router,
Space to the existing task menu, and G to the existing terminal-focus helper.
PageUp/PageDown scroll the selected output while Home resumes following the
latest output.

Refreshes keep the selected target IDs and compare pane ID, session, pane
process ID, host, and transport before updating a tile. Missing or changed
identity is shown as unavailable; stale cards cannot copy, open, or focus a
replacement pane. The renderer uses the existing `get_pane_screen` read-only
`capture-pane` path and never invokes tmux layout or pane mutation. At widths
below the existing narrow threshold or heights below 18 rows, multi-card views
show the selected card instead of squeezing the grid. Grid, split, main-and-side,
focus, CJK clipping, stale identity, and terminal sizes from 42×14 through
160×45 are covered by tests; an isolated populated runtime also exercised
Work Group navigation, attention, Codex output, and complete-result copy.

## Clipboard destinations

Y copies only a complete candidate for the newest finished turn. The
30-line poll is status evidence, never a Y payload. A candidate without
a visible turn boundary is partial, even when its body is long; Y recovers
bounded history and refuses to route text unless the adapter can identify
the complete turn. User prompts, prior turns, tool rows, and UI chrome are
excluded by the adapter. If completeness cannot be established, the UI says
"최신 결과 전체를 찾지 못했습니다" and writes nothing. Recent terminal
output copy is separate: open details, press `Ctrl+I`, then `Ctrl+L`. It copies
up to the last 60 captured lines and reports when earlier output was omitted;
it does not claim to be a complete Agent Result.

Configured remote tmux hosts are read-only `remote_tmux` result providers.
Each target is identified by provider endpoint, tmux session ID, pane ID, and
pane PID. A result request rechecks that identity and liveness before it
captures bounded history and normalizes it through `ResultCandidate`. A local
SSH pane first uses its local tmux history; if that history is incomplete,
Tower uses a remote source only when that exact local pane has a persisted
provider binding. A plain `ssh workstation-b` TUI without such a binding fails closed;
Tower does not infer a remote Agent pane from its execution host.
Local SSH Grok panes use a copy-time binding: Tower matches the pane's unique
SSH process and established socket to one remote `SSH_CONNECTION`, sshd
ancestry, and Grok process/session. WSL sockets may use only their unique
WinNAT source-tuple mapping. Grok Result comes from the newest ended native
turn and `grok export`; an ambiguous binding or failed provider never falls
back to the local screen suffix.
`turn_complete` records the Agent's completion boundary; `body_complete` records
proof that the copied body starts at this turn's boundary. A result is ready or
copyable only when both are true. Tower-submitted turns keep only the pane's
numeric tmux history position as an in-memory watermark; external turns must
prove their start from bounded history or a registered Result Provider. History
recovery joins terminal-wrapped rows and rejects a suffix when its start is
unproven.
Result cleanup preserves internal blank lines and fenced-code indentation so
the copied text remains faithful to the Agent's logical body.
An authoritative deep-history read may restore readiness after the fast
visible-screen poll saw only a suffix; that suffix cannot replace the full body.
One Y writes one primary destination. The notice names that destination and
confirms a complete result.
A Codex trust chooser appearing after the last `Worked for` marker
invalidates that older screen candidate; recovery returns no new Result.
Codex completion markers inside fenced or indented Markdown code are ignored,
and an elapsed-time line must be followed by the exact idle prompt. An earlier
turn boundary also requires Codex model and shortcut status chrome before the
next prompt. Unknown status text between a prior completion and a later prompt
is ambiguous too; it cannot be treated as harmless UI. A prompt-shaped line
without that structure cannot reset the turn. A result without an unambiguous
current user-turn start remains incomplete, including a capture that may begin inside
code. History is bounded to 800 lines and 256 KiB;
`OUTPUT_RANGE_EXCEEDED` is used only when capture or that bound proves rows
were discarded. Other uncertain boundaries report an incomplete/unknown
reason. Remote provider failures preserve their distinct reason codes.
The pane menu's Diagnostics view reads the current row and in-memory copy
metadata only; it does not extract a Result or update ResultTracker/SQLite.
It shows provider, completeness, failure, and clipboard-stage metadata without
showing prompt or Result text.
The TUI and Phone Remote share ready/read metadata in the local SQLite
store; only a current pane extraction can supply the body after restart.
The stored fields and stale-pane rules are documented in
[`REMOTE.md`](REMOTE.md#what-it-can-do-v0-scope).

The Access Client, Tower host, Agent execution host, and Result Source are
separate identities. The resolver reads the newest complete Result from its
registered source; the clipboard router independently writes to the Access
Client. `tower connect <target>` resolves an existing OpenSSH target and
preserves the originating Access Client through nested SSH. It does not infer
the Access Client from the Tower or execution host. An unregistered SSH client
or ambiguous client set never becomes a guessed destination.

The default copy destination is automatic. A local client uses its verified
native clipboard provider; a remote terminal needs an explicit client-bound
provider. If the client or destination is ambiguous, Tower writes nothing and
asks the user to choose. A stale client choice is discarded. Each copy writes
to one destination only, and successful native clipboard writes are verified
by reading back the content. Phone copy uses the browser clipboard and does
not depend on PC settings.

The selected destination is configurable in Settings. The user-facing labels
are "자동으로 선택 (추천)", "이 컴퓨터", "현재 접속한 터미널", and
"Tower 복사함"; implementation details remain outside the default screen.

## Create and move tasks

The ordinary create entry is `새 작업`: it collects a separate Tower
task name, execution host, project path, Agent, descriptive task role, and
placement, then delegates creation to the existing workspace browser and
launcher. Agent identifies the provider/program; role is independent metadata
with the default choices 조율, 계획, 구현, 검수, 확인, 실사용, and 전체 흐름.
An unassigned role is allowed. Role metadata does not change the provider,
command, project, attention, execution, or result state. Local placement is
`새 작업 묶음` or `현재 작업 옆`; the latter targets the selected task's
existing work group. The task name and optional role are stored as
pane/session/process-bound Tower metadata, separately from project identity
and tmux names. Local keys use the pane ID; remote keys use
`<alias>:<pane_id>`. A mismatched session or pane process makes the metadata
inapplicable. The existing 더 보기 menu offers `역할 바꾸기` for local and
remote tasks. Ordinary wide rows show project, task name, and assigned role;
narrow rows give task and role their own line while keeping Agent and status
visible.

`작업 옮기기` changes membership only; it does not call `move-pane` or alter
the running process. `배치 바꾸기` updates the Work Group layout and member
slots, which the Tower Live view renders independently from tmux. The separate
actual-terminal actions require explicit confirmation and exact ownership and
liveness checks. They reject mixed or unowned windows and cross-host targets.
An isolated tmux/Tower run verified logical moves leave pane IDs, PIDs, and
layouts unchanged; managed physical moves and detach preserve pane IDs/PIDs,
update the Result identity, and leave unrelated panes unchanged. Result Y was
verified after a physical move using a deterministic complete Result fixture;
live Agent extraction after a move remains `NOT_PROVEN` by this fixture.

Remote task creation continues through the existing SSH launcher. Its result
returns the exact pane ID from the create/split command, plus its session and
process ID; Tower stores the task label under the same `<alias>:<pane_id>` key
and process-bound identity used by remote observation. In particular, a
detached split's pane ID is not inferred from the active pane in its window.
No rename or write is sent to an existing remote pane. Mocked protocol tests
cover this identity path; live remote create remains `NOT_PROVEN` /
`HUMAN_GATE_PENDING`.

## Legacy workspace presets

`workspace-presets.json` remains a separate store used by the existing
Workflow integration. It is not the source for **저장된 작업** or **작업
템플릿**, which now use the Work Group based `worksets.json` described above.
The older file is retained and is not rewritten or deleted by Workset actions.

## 단계별 Workflow 실행

**고급 설정 → 작업 흐름**에서 **새 단계별 실행**을 고르면 기존
`WorkspacePreset`과 별도의 `WorkflowPreset`을 각각 선택한다. Workspace는
Host·경로·사용 가능한 Agent를 정하고 Workflow는 역할 순서만 정한다. 두 모델은
별도로 유지한다. 기본 순서는 빠른 수정(구현→확인), 기능 개발(계획→구현→검수→확인),
집중 고도화(조율→계획→구현→확인→실사용→전체 흐름)이며 빠른 수정과 집중
고도화에는 provider와 pane이 없는 사람 확인 단계가 끝에 표시된다. 미리보기와
실행 생성만으로 Agent에게 보내지 않는다.

`WorkflowPreset`은 ID, 이름, 순서가 있는 알려진 역할/사람 확인 단계만 가진다.
중복·미지원 단계는 거부한다. 기본 순서는 빠른 수정(구현→확인→사람 확인),
기능 개발(계획→구현→검수→확인), 집중 고도화(조율→계획→구현→확인→실사용→
전체 흐름→사람 확인)이다. 사용자 정의 순서는
`~/.config/tmux-agent-tower/workflow-presets.json`의 버전 1 JSON만 읽는다.
각 항목은 고유한 소문자 영문 ID, 고유한 이름, 알려진 role ID와 선택적
`human_gate`로 된 `stages`만 허용한다. 파일은 64 KiB로 제한하며 알 수 없는
필드·역할과 중복 값은 거부한다. 파일은 JSON 데이터로 파싱할 뿐 가져오거나
평가하거나 실행하지 않는다. 잘못된 파일은 사용자 순서에서 제외되고 한국어
오류를 표시한다. Workflow 정의 모델에는 Host, 경로, provider, pane ID,
prompt 필드가 없다.

현재 실행 기록은 `~/.cache/tmux-agent-tower/workflow-runs.json`에 버전이 있는
JSON으로 저장한다. 같은 디렉터리의 임시 파일에 쓰고 파일과 디렉터리를 fsync한
뒤 원자 교체하며 파일 권한은 `0600`이다. 저장 허용 목록은 실행 ID, Workspace와
Workflow 스냅샷, 단계 역할/상태/시각, pane key·session·pane PID, provider 및
작업 표시명뿐이다. prompt, Harness, 결과/대화 본문, 명령과 비밀값은 저장하지
않는다. 잘못된 데이터는 덮어쓰지 않는다. 앱을 다시 열면 실행을 불러오며 현재
pane 신원이 맞지 않으면 자동 재할당하지 않고 수동 처리가 필요한 상태를 보인다.

상태는 `PENDING`에서 사용자의 시작 동작으로 `RUNNING`, 그 뒤 사용자가 고른
`PASS`, `FAIL`, `BLOCKED` 중 하나로만 바뀐다. PASS를 감지하거나 다음 단계를
자동 실행하지 않는다. FAIL/BLOCKED는 사용자가 다시 시도하기 전까지 멈춘다.
RUNNING은 한 단계만 허용한다. Human Gate 역시 사용자가 시작한 뒤 결과를
명시해야 끝나며 Agent dispatch 경로에 들어가지 않는다.

Agent 역할 단계는 `load_harness(role_id)`로 Harness를 화면에 미리 보여준다.
사용자가 기존 pane과 작업 brief를 고르고 최종 전송을 확인하면 Workspace의
Host·경로·허용 provider, pane key, session, PID를 다시 비교한다. active
interaction이 없고 실제 provider가 Shell이 아닌 로컬 pane만 대상이 된다.
전송은 `control.actions.send_prompt()`를 통해 한 번 수행하며 이 실행 경로는
fresh identity와 상호작용 부재를 추가 검증한다. role metadata는 신원 확인과
전송 성공 뒤에만 기록한다. 원격 pane, SSH 명령, Agent 실행, 승인/질문 답변을
자동화하지 않는다. 검증은 격리된 mock/fake state 테스트를 사용하며 실제
provider 및 curses/TUI 사용은 `HUMAN_GATE_PENDING`이다.

원격 Workspace 실행은 저장할 수 있지만 현재 안전한 입력 API가 원격 pane 쓰기를
지원하지 않는다. 해당 Agent 단계는 `PENDING` 상태와 제한 안내를 유지하며 SSH로
우회하지 않는다. 실제 원격 입력 API가 별도로 승인·구현되기 전까지는 로컬
Workspace만 Agent 단계 dispatch 대상이다.

## Role harness loading

`role_harness.load_harness(role_id)` validates against `state.overrides.ROLE_IDS`
and reads a packaged Markdown default from `resources/harnesses/<role>.md`.
`~/.config/tmux-agent-tower/harnesses/<role>.md` takes precedence when present.
Overrides are local UTF-8 regular files capped at 64 KiB; symlink files and
symlink harness directories are rejected. Missing overrides use the packaged
default, while malformed or oversized overrides raise `HarnessError` instead
of silently falling back. The loader has no Agent/provider argument and does
not call adapters or launcher APIs. Workflow UI previews the loaded text before
pane selection; only a later explicit send confirmation includes it with the
task brief in the prompt sent to the selected local pane. Harness text is never
executed and is not stored in workspace/workflow presets or run state. Package
data configuration includes all seven Markdown defaults in built distributions.

## Extension points

* **New agent**: add `adapters/<name>.py` implementing `AgentAdapter`,
  register it in `adapters/__init__.py`'s `ADAPTERS` list order (order
  matters: first match wins), add fixtures + tests.
* **New execution environment**: use Settings → Connections → Execution
  environments → Add SSH environment. The profile stores a display name and
  SSH destination in the compatible `remote-hosts.txt` format
  (`alias` or `alias:Display Name`). Existing entries migrate in memory;
  transport is SSH and the alias remains the execution target.
