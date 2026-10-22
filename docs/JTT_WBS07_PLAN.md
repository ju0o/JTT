# WBS-07 initial decision record — JTT-only / native Windows

Base: `e06291bebbdaadcc96d8690cc0054da489af3fb1`. This is an initial audit and
native feasibility PR, **not completed Windows product support**. Published
RC3, existing Tower installations and user state remain immutable.

## 07A: inventory and removal order

[Full tracked-file inventory](JTT_WBS07_INVENTORY.json) classifies 252 baseline
source, test, script, documentation, packaging and CI files. Entries are
planning dispositions, not authorization to delete blindly. The inventory is
complete for those baseline path sets; media and immutable releases are not
removal targets. Dynamic imports and shared behavior still require tests.

Keep `jtt*.py`, `terminal/`, `state/jtt_*`, `ui/jtt.py`, JTT scripts and docs.
Initially retain the internal `tmux_agent_tower` namespace: live workers validate
that argv/import path. Distribution name and console-script name are separate.
Do not migrate or reconnect old supervisors by guessing new identities.

Extract these mixed dependencies **before** excluding legacy code:

| Mixed source | JTT dependency to preserve |
| --- | --- |
| `ui/ux_preview_v2.py` | Approved layout, cards, Mini LIVE, RIGHT, key/prefix logic |
| `ui/render.py` | Unicode display width / truncation, independent of detection |
| `ui/widgets.py`, `ui/perf_trace.py`, `i18n/` | Input/dialog utilities and translations |
| `launcher/browse.py`, `launcher/discovery.py` | Safe local directory chooser, not SSH discovery |
| `update_manager.py` | Version/Release validation and installation helpers used by JTT |

After extraction, exclude Tower's `main.py` and `tower` entry point; adapters,
control, detection, tmux, remote, server, role harness resources, legacy state/UI,
and Tower launch/install scripts. Exact files and conditional dispositions are
in the inventory. Retain historical Tower documentation/media and the tmux safety
incident record. Keep all Linux regression tests during extraction; later
explicitly separate retired Tower tests from JTT tests instead of counting their
deletion as a passing regression. Keep the fail-closed guard for any tmux tests.

Repository rename is not required for a new PyPI distribution or `jtt` command.
Defer it: URLs in installers, Release verification, OIDC repository claims,
badges and immutable RC3 documentation need a compatibility plan first.

## 07B: name and installer ownership

PyPI JSON API queried 2026-10-23 KST:

| Candidate | Result | Decision |
| --- | --- | --- |
| `jtt` | HTTP 404 | Short but generic; no reservation proof |
| `ju0-terminal-tower` | HTTP 404 | Recommended distinctive distribution candidate |
| `jtt-terminal` | HTTP 404 | Alternative |
| `ju0-terminal` | HTTP 404 | Alternative |

A 404 is **not** proof PyPI will accept the name: prohibited/reserved names and
registration races remain. No account action, name registration or upload was
performed. Console entry point in a future JTT-only wheel must be exactly `jtt`;
current RC3 still intentionally has both old entry points and is unchanged.

Use isolated tool environments, never install both distributions into one venv:

| Install owner | Upgrade/remove contract |
| --- | --- |
| pipx | `pipx upgrade <approved-name>` / `pipx uninstall <approved-name>` |
| uv | `uv tool upgrade <approved-name>` / `uv tool uninstall <approved-name>` |
| old JTT managed release | Existing frozen release contract; no automatic migration |
| developer checkout | Explicit development preparation; refuse official self-update |

Future `jtt update`/rollback must refuse mutation of pipx/uv-owned environments
and show the correct manager command. Read-only checks may inspect metadata;
unknown ownership fails closed. Uninstall removes the tool environment/launcher,
not JTT state or live supervisors. Windows open-file behavior and old worker
runtime retention must be tested before enabling upgrades with active sessions.
These new installer behaviors are **planned, not implemented in this PoC**.

Trusted Publishing plan: build/test a wheel and sdist, validate only-`jtt`
entrypoint and manifest, then a separate explicit environment-approved job with
`id-token: write`. Register the exact repository/workflow/environment in
TestPyPI first; separate protected `pypi` environment for production. Never grant
OIDC write permission to ordinary PR jobs. No publishing workflow is enabled
by this PR; no TestPyPI/PyPI upload or Founder account changes performed.

## 07C: platform boundaries and native evidence

Current imports block Windows even for `jtt --version`:
`jtt.py → ui.jtt → curses / terminal.manager → pwd`, and shared renderer/state
imports reach `fcntl`, `termios`, POSIX PTY, UID/mode checks and `/proc`.
Changing a dependency alone cannot make the current application portable.

| Contract | Linux today | Windows candidate | macOS follow-up |
| --- | --- | --- | --- |
| PTY | openpty/ioctl/setsid | Explicit ConPTY handle | POSIX PTY |
| Process identity | PID + `/proc` start/argv | Retained handle + creation time + generation | Native process start identity API |
| Ownership/cleanup | Session/group and descendants | Owned Job Object, atomic assignment needed | Process/session cleanup proof |
| IPC peer | AF_UNIX/SO_PEERCRED | Local named pipe + owner SID DACL + peer SID | getpeereid/LOCAL_PEERCRED |
| Private state/lock | UID/mode, flock, fsync | DACL, native locking, atomic replace semantics | POSIX lock + platform identity |
| UI | curses mixed controller/rendering | Textual or console adapter, decision pending | Shared controller + tested driver |

The new `experiments/windows_native` PoC is independent of the production
runtime. Native Windows Python owns ConPTY and Windows PowerShell; WSL is only
the orchestration host, not a runtime dependency of the worker or client.
Verified on Windows 11 build 26200 / Python 3.13: actual command input/output,
Korean output and input, shell-reported width after resize, same ID/generation/
PID/creation time across a separate client-process reconnect, input revocation,
stale-generation rejection, owner-only pipe DACL, peer SID validation, retained
Job cleanup. No default HOME/state or existing sessions opened.

Independent read-only QA found a shell-first-exit cleanup defect in the initial
probe. The added native regression failed against the original probe (stop
returned `process_exited`), then passed after allowing **identity-validated** stop
on the retained owned Job after shell exit. Assignment-failure cleanup is also
tested with a retained handle. These changes are experiment-only, not product fixes.

The first hosted Windows run failed the Korean output assertion while local
Windows (OEM CP949) passed. An isolated encoding experiment reproduced Korean
replacement as two question marks at CP437 and correct output at CP65001.
Hosted diagnostic run `39815092957` confirmed CP437, zero non-ASCII output
codepoints and exactly two question marks, matching that local reproduction.
The fixture shell now explicitly sets UTF-8 input/output encoding; user/system
code pages are untouched. CI remains a separate gate from local results.

Textual headless runs at 160×44 and 80×24 pass, but do not prove native Windows
Terminal visual acceptance, cursor/color fidelity or the complete approved UI.
Window/Work Kit product logic is unchanged, not ported by this prototype.

### Candidate comparison (no production adoption yet)

- **pywinpty 3.1.0**: real ConPTY smoke passed; MIT. Low-level PTY avoids the
  high-level wrapper's internal loopback socket. Native wheel/Python support,
  I/O backpressure, query handling and atomic Job assignment require gates.
  Its installed metadata requires Python >=3.10; current Linux Python 3.9 support
  is not evidence of Windows 3.9 support. Windows Python floor is still a decision.
- **Direct Win32 ConPTY**: avoids wrapper dependencies but requires careful
  CreateProcess attribute lists, pipe drain and close ordering; higher maintenance.
- **Textual 8.2.8**: cross-platform UI candidate, MIT; headless geometry tested.
  It does not emulate terminals or replace IPC/security. Existing curses renderer
  cannot be directly reused; controller/model extraction and parity tests needed.
- **windows-curses**: smaller possible drawing migration but does not solve the
  POSIX process/state imports; keyboard, wide Unicode and native console behavior
  remain untested. Do not select on perceived porting ease alone.
- **pyte 0.8.2**: existing LGPL-3.0 renderer, no full-xterm claim. Preserve notices;
  compare terminal compatibility before a production Windows renderer decision.
- **pywin32 312**: installed metadata reports PSF license; experiment-only Win32
  IPC/job bindings. Retain its distribution notices if adopted later.

Windows 10 desktop, alternate screen and extended key protocols, child escape
races, multi-client leases/expiry, denial from a different real Windows account,
crash/reboot recovery and live-update runtime retention remain unverified.
The PoC checks explicit ACLs rather than silently skipping Linux checks.

## 07D: macOS is design-only

Reuse the backend contract, not Linux `/proc`, prctl or SO_PEERCRED assumptions.
Add a macOS feasibility runner for PTY creation, process identity, private IPC,
locks, detach/reconnect and owned descendant cleanup. Do not publish a macOS
support classifier before those tests and native UI checks pass.

## Ordered Draft PR plan / acceptance

1. **This audit + native feasibility**: product unchanged; Linux regression and
   separate Windows probe workflow. This is the initial report, not WBS-07 completion.
2. **Shared JTT extraction**: lazy CLI imports; move reusable drawing/chooser/
   release utilities; verify no runtime Tower/SSH/tmux/server dependency.
3. **JTT-only packaging**: approved name, only `jtt`, package-manager ownership,
   pipx/uv install/upgrade/remove tests with retained state/live sessions.
4. **Platform contracts**: move Linux implementations without semantics changes;
   preserve identity/stop/lease tests and fail-closed safety.
5. **Windows product integration**: vetted ConPTY/process/IPC/state adapters,
   approved UI parity, real Windows Terminal and Windows 10/11 gates.
6. **Publishing readiness**: TestPyPI approval + OIDC, metadata/wheel audits,
   public PyPI upload only after separate Founder approval. macOS remains gated.

Public repository standard GitHub-hosted runners offer free Linux/Windows/macOS
CI. Use hosted runners, not self-hosted machines with user sessions. This PR adds
only a read-only Windows feasibility job; existing Linux matrix remains intact.
No cloud service, paid infrastructure or account setup is introduced.

## Primary references

- [Microsoft ConPTY creation and I/O deadlock guidance](https://learn.microsoft.com/en-us/windows/console/creating-a-pseudoconsole-session)
- [pywinpty source and licensing](https://github.com/andfoy/pywinpty)
- [Textual supported environments](https://textual.textualize.io/getting_started/)
- [PyPI Trusted Publishing](https://docs.pypi.org/trusted-publishers/using-a-publisher/)
- [GitHub standard hosted runners](https://docs.github.com/en/actions/reference/runners/github-hosted-runners)
- [pipx tool lifecycle](https://pipx.pypa.io/stable/how-to/manage-installed-apps.html)
- [uv tool ownership](https://docs.astral.sh/uv/concepts/tools/)

No main merge, Force Push, public package, tag or Release operation is part of
this work. Existing RC3 remains Linux/WSL2 scoped and immutable.
