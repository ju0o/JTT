# Native Windows feasibility — not a supported JTT runtime

Run only on native Windows Python 3.13 in a dedicated venv:

```powershell
python -m pip install -r experiments/windows_native/requirements.txt
python -m pytest -q -s experiments/windows_native/test_probe.py
```

The test explicitly creates a new no-profile Windows PowerShell through the
low-level `winpty.PTY(..., backend=Backend.ConPTY)` API. There is no WSL fallback,
tmux, Agent, SSH, existing JTT registry, or user-profile import. Environment
variables passed to the shell are a small allowlist with temporary HOME,
USERPROFILE, APPDATA, LOCALAPPDATA, TEMP and JTT_HOME. This is configuration
isolation, **not an OS sandbox denying access to the account's files**.
Only the new fixture PowerShell sets its console input/output encodings to UTF-8;
no global code page or user profile is changed. Windows PowerShell's OEM default
can replace Korean with question marks on an English-language runner.

The separate worker owns the ConPTY and a Windows Job Object. Clients use a
local-only named pipe with an explicit current-user SID DACL, verified after
creation. The worker impersonates each caller to compare its SID. JSON frames
are bounded; no pickle and no terminal output is saved by the worker.
Session UUID + generation + retained process handle / creation time identify
the shell. Disconnect revokes input; a new client must acquire again. Stop
validates the supplied identity and terminates only the retained owned Job.
The test's failure cleanup only terminates its own retained worker handle,
whose Job closes. No PID-search kill or existing user session enumeration.

Verified locally: Windows 11 build 26200, native Python 3.13, pywinpty 3.1.0,
pywin32 312, pyte 0.8.2, Textual 8.2.8. Input/Enter and Korean input/output,
PowerShell-reported resize width, separate client-process reconnect to the same
shell, stale generation rejection, revoked input and explicit cleanup passed.
Both live-shell stop and shell-first-exit stop are covered. Injected Job
assignment failure terminates and waits on the newly created child's retained
handle; malformed requests are rejected without terminating the worker.
Textual `run_test` validates only headless 160×44 and 80×24 layout; it does not
prove native Windows Terminal visual UX or integrate the approved JTT UI.

**Hosted Windows investigation:** run `39815386508` passed initial UTF-8 Korean
output and shell-reported resize width, then failed the generic `입력` snapshot
assertion. Its codepoint-only timeout diagnostic was ambiguous: those code
points could have come from the echoed PowerShell source (`'입'+'력'`) rather
than the command's result, so that run did not establish that PowerShell emitted
the expected response or that pyte misplaced it.

The revised test creates a nonce-bearing `JTT_DIAG_<nonce>_한글_END` and
`JTT_DIAG_<nonce>_입력_END` only when PowerShell evaluates concatenated Korean
literal fragments. The complete marker is asserted absent from the submitted
command, preventing terminal echo from satisfying the result assertion. The
worker records only bounded diagnostic evidence for these synthetic markers:
the `pywinpty.PTY.read()` Python string before `pyte.Stream.feed`, UTF-8
round-trip bytes/codepoints, read sequence, resize sequence/dimensions, and
pyte display/cell coordinates. No general terminal body is retained in those
diagnostic records.

On local Windows 11 the marker appears in a single pywinpty read sequence and
as contiguous pyte display text. In the 80-column post-resize case, the two
wide Korean cells are at columns 18 and 20 (width 2 each); the echoed source
characters are separately located at columns 51 and 58 on the preceding row.
This demonstrates why codepoint presence alone was insufficient evidence. The
pinned pywinpty API returns `str`; it does **not** expose the underlying
ConPTY pipe bytes, so the UTF-8 hex is a round-trip of the returned string, not
a capture of raw pipe bytes. Hosted CI must pass with this unambiguous marker
test before the Windows failure is classified as resolved. Windows product
readiness remains NO-GO meanwhile; assertions are retained.

## Explicit limitations / follow-up gates

- Windows 10 and macOS not tested. Windows CI is a server runner, not a desktop.
- Fixed one-shell test worker, not a persistent product registry or installer.
- One pipe client at a time; no production multi-client observation, lease expiry,
  request deadlines, durable state, or reconnect discovery policy yet.
- Job assignment occurs after shell spawn. A production backend must close the
  spawn-to-job assignment gap (suspended creation / atomic job assignment).
- This PoC does not prove a lease expiry for a client that remains connected,
  real concurrent-user SID denial, a forced-kill race, or behavior with
  multiple client attempts. Its one-instance pipe and disconnect lease test do
  not establish production multi-client safety.
- ACL structure and same-user peer validation tested; a real different-user
  access attempt remains untested. No users or system ACLs are modified.
- Process handle retention protects this instance; reboot/restart recovery and
  user consent flows remain to be implemented.
- Textual is not a VT emulator. pyte's existing limitations (alternate-screen,
  extended keys, truecolor, terminal queries, mouse/paste) remain separate gates.
- The experiment uses low-level pywinpty, not `PtyProcess`, whose inspected 3.1.0
  implementation creates an internal loopback socket for its reader.
- Dependencies are experimental and are not added to product metadata.
- No new PyPI package, Release, tag, launcher or existing installation is made.
