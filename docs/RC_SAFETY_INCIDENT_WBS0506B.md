# JTT RC Safety Incident Record — WBS-05-06B

Status: recorded; historical impact is not fully ascertainable. Public RC remains on HOLD.

## Incident evidence and limits

The WBS-05-06B operator handoff reports that an unqualified `tmux kill-server` was run from an interactive validation shell without `-S` or `-L`, while that shell inherited a `TMUX` environment. A subsequent read-only probe reported that no server was running at the default socket. The exact shell command history, source line, pre-command session/window/pane inventory, and server identity were not preserved.

Therefore the command could have addressed the ambient server selected by tmux, but the surviving evidence does not establish which server it reached, whether it had user sessions, what processes were affected, or whether there was data loss. We do not claim that the user's tmux environment was unchanged. This WBS did not query or modify the default tmux server.

## Repository audit and prevention

The test audit found live tmux calls in navigator, remote-session integration, structure/workspace dogfood, work-group/workset launch, and JTT terminal integration coverage. The old workspace dogfood also performed optional SSH probes and remote tmux window creation/cleanup without an enforceable remote isolation socket. That live remote dogfood was removed; remote tmux dogfood is now NOT RUN until a separately guaranteed remote test socket contract exists. Product code that intentionally operates on the user's selected tmux environment was not changed.

All pytest cases now receive a private test root through `tests/conftest.py`. The test PATH shim routes tmux invocations to an explicit `-S <private-root>/server.sock`, defaults to `/dev/null` config, clears inherited `TMUX` and `TMUX_PANE`, and maps a legacy `-L` selector only to a socket below that same private root. A config file is accepted only when it is a private regular file inside the test root containing only `bind-key` / `unbind-key` directives; external configs and configs with other commands are rejected before the tmux binary is invoked. External socket paths are likewise rejected. Destructive test commands require the canonical `/tmp/jtt-tmux-test-*` root, exact owner-only marker, current-user ownership and private mode for the root and socket, and a server identity owned by the current user. Server shutdown additionally checks PID start identity, waits for that server to exit, and only removes a stale socket node when `/proc/net/unix` confirms that the exact path is no longer bound. Any unverifiable path or identity fails closed and retains the sandbox.

The regression tests prove that an unqualified test invocation is routed to the private socket, an external socket shutdown is rejected, inherited tmux routing variables are removed, shutdown without an active verified fixture fails closed, and a stale private socket is removed only after the kernel socket table shows no listener. The pytest autouse fixture applies this guard to the full test run, so a missed subprocess call cannot fall through to the user's default server.

## Retained RC package evidence

The previously built local candidate artifacts remain available in the current build workspace. Their SHA-256 values were independently re-read for this record:

| Artifact | Candidate metadata | SHA-256 |
|---|---|---|
| `tmux_agent_tower-0.3.0rc2-py3-none-any.whl` | `tmux-agent-tower` `0.3.0rc2` | `a4a806cda2a4affc6a4415e44c6a9887fbd1c045d20fd5179d09f5b6c82c0ade` |
| `tmux_agent_tower-0.3.0rc2.tar.gz` | source distribution `0.3.0rc2` | `3920ed36f4d49cf700d771bf63baa7ed5788749d156a062ce147c23b86132c6f` |

These are local candidate artifacts, not a published JTT GitHub Release. No public tag or release was created. The package metadata still uses the repository's existing distribution name; the JTT-specific release/tag identity must be finalized before public distribution.

## Human Gate procedure

Run the visual check from a new Windows Terminal WSL tab that is not inside tmux. The candidate command below uses the previously verified clean wheel-install venv and a dedicated JTT_HOME; it does not invoke the `tower` legacy entrypoint or the user's normal JTT_HOME:

```bash
env -u TMUX -u TMUX_PANE JTT_HOME="$HOME/.local/state/jtt-rc-human-gate-0506b" /tmp/jtt-wbs0506b-build.jfe6K7/clean-install-venv/bin/jtt
```

Check launch and read-only `jtt doctor` output first, then create one ordinary test terminal only if you intend to exercise terminal creation. Confirm input/output, UI detach versus terminal exit, and reattachment. Do not use existing tmux sessions, SSH, Agent logins, or production data. The candidate path is under `/tmp` and is temporary; if it no longer exists, rebuild/install from the retained artifact hashes in a fresh isolated environment rather than substituting `tower` or a different checkout. Human visual/runtime acceptance remains pending.
