# JTT package transition plan

The public JTT RC3 remains the `tmux-agent-tower` distribution at version
`0.3.0rc3`. Its tag, Release assets, installer, and existing installations are
not changed by this candidate.

The proposed follow-up distribution is `ju0-terminal-tower` version
`0.3.1rc0`. It is an unpublished development candidate and is not available from
PyPI. The candidate wheel installs Python code under `ju0_terminal_tower` and
exposes only the `jtt` console command. A guarded compatibility alias supports
shared internal imports only when the legacy distribution or another
`tmux_agent_tower` source is absent. Keep the candidate and RC3 in separate
Python environments.

## Installation ownership

The package manager owns pipx and uv installations. Use that manager to install,
upgrade, or remove the package. The JTT self-updater and rollback command refuse
to modify package-manager-owned installations. The package name has not been
verified as available or owned on PyPI; do not install it from an index until a
separate publication is approved.

Example isolated manager configuration for testing only:

```sh
PIPX_HOME=/tmp/jtt-test/pipx PIPX_BIN_DIR=/tmp/jtt-test/pipx-bin pipx install ./dist/ju0_terminal_tower-0.3.1rc0-py3-none-any.whl
UV_TOOL_DIR=/tmp/jtt-test/uv-tools UV_TOOL_BIN_DIR=/tmp/jtt-test/uv-bin uv tool install ./dist/ju0_terminal_tower-0.3.1rc0-py3-none-any.whl
```

These paths are examples for a disposable test environment, not current user
installation instructions. Always check the absolute `jtt` path and
`jtt --version` after installation. If a pre-existing `jtt` command is found,
preserve it and stop to resolve the conflict explicitly.

In isolated collision tests, pipx 1.18.4 returned exit code 0 while warning
that it left an existing `jtt` file untouched and did not expose its new app;
uv 0.13.5 returned exit code 2 and left the existing `jtt` and `tower` files
unchanged. Do not pass an overwrite/force option. Use a distinct manager bin
directory when an existing `jtt` command must remain available.

## Manual RC3 transition

There is no automatic migration from RC3. Keep its launcher and Python
environment intact. A future, separately approved transition should:

1. Record the RC3 launcher path and verify `jtt --version` reports the expected
   official `0.3.0rc3` identity.
2. Install the new distribution through pipx or uv into a separate manager
   environment and a separate executable directory.
3. Verify the new absolute launcher path, candidate version, and read-only
   `jtt doctor` output before changing any shell or PATH preference.
4. Reuse an existing JTT_HOME only after verifying the product's data format and
   recording the session IDs, generations, and PIDs. Installation itself must
   not rewrite session state or restart a process.
5. If verification fails, keep using the original RC3 launcher, uninstall only
   the candidate with its owning package manager, and leave JTT_HOME untouched.

An active RC3 Supervisor is bound by identity validation to the RC3
`_worker.py` path. The candidate intentionally refuses to attach to it: allowing
another worker path without a versioned trust record would weaken stale-session
protection. Do not point the candidate at an RC3 JTT_HOME while RC3 sessions are
active. Keep the RC3 launcher and JTT_HOME together, and use a separate
candidate JTT_HOME until the RC3 sessions have been ended through the old
runtime. Preserving the files does not imply cross-version live-session
reconnection.

Do not terminate the Supervisor or PTY as part of package installation,
upgrade, rollback, or uninstall. After RC3 has no active sessions, a future
explicit data-transfer procedure must back up the old JTT_HOME and verify saved
workspace compatibility before reusing it; automatic copy or migration is not
implemented here.

This plan describes the safety boundary; it does not authorize moving a user
installation, re-pointing PATH, or migrating live sessions.
