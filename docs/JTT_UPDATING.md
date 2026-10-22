# JTT installation, updates, and rollback

JTT and the legacy `tower` command are separate products and launchers. This
guide applies only to the standalone `jtt` command. Tower's updater remains
documented in [`UPDATING.md`](UPDATING.md).

## Development launcher

From a source checkout, explicitly prepare the checkout-local environment and
install its development-only command:

```sh
./scripts/jtt-setup-dev
./scripts/jtt-install-dev
jtt --version
```

The installer pins `~/.local/bin/jtt` to that checkout and `.venv-jtt`. It
refuses a different command on PATH and does not edit `.bashrc`, `.zshrc`, or
other shell startup files. If needed, `export PATH="$HOME/.local/bin:$PATH"`
affects only the current shell. `DEV` is shown in version/doctor output.
Network checks and automatic application of Releases are refused for a source
checkout. `scripts/jtt-dev` and `scripts/jtt-setup-dev` remain available.

## Official installation

The JTT release is currently on **HOLD**. No release, installation, or runtime
switch is performed as part of development CI. After a JTT version is
published, start from a clean checkout at that exact tag:

```sh
git clone --branch <published-JTT-tag> --depth 1 https://github.com/ju0o/tmux-agent-tower.git jtt-release
cd jtt-release
./scripts/jtt-install-release
```

JTT tags use a product namespace: `jtt-vMAJOR.MINOR.PATCH` for stable and
`jtt-vMAJOR.MINOR.PATCH-rcN` for an RC (for example, the tag payload
`v0.3.0-rc2` maps to Python package version `0.3.0rc2`). The legacy Tower tag
`v0.3.0-rc2` is not a JTT Release candidate. This repository currently builds
both commands from the `tmux-agent-tower` distribution, so the numeric package
version may overlap across products; the full JTT identity is the `jtt-` tag
namespace, matching package version, JTT console entry point, and private
`product: jtt` install record. The installer and updater reject a legacy Tower
tag even when its version is newer.

The bootstrap verifies a clean exact Git tag, a matching published GitHub
Release, and the tag's resolved commit before it asks for confirmation. It
installs into a new versioned directory under `${XDG_DATA_HOME:-~/.local/share}/jtt/`
and creates `~/.local/bin/jtt` only when that path and PATH do not already
contain another command. It never changes `tower` or a shell profile. If the
user bin directory is not on PATH, the installer prints a current-shell PATH
hint and leaves persistent shell settings to the user.

## Commands

| Command | Behavior |
| --- | --- |
| `jtt` | Opens the interactive workspace; it does not check for updates or create a terminal automatically. |
| `jtt --version` | Shows package version and `DEV`, `OFFICIAL`, or `UNMANAGED` installation identity. |
| `jtt doctor` | Read-only local diagnosis; it does not create a PTY, start a service, or contact GitHub. |
| `jtt update --check` | Contacts the official GitHub Releases API and checks the current channel. |
| `jtt update --dry-run` | Shows the verified Release and sidecar/launcher plan; writes no files. |
| `jtt update` | Requires an interactive `yes` before staging and switching the JTT launcher. |
| `jtt rollback --dry-run` | Shows the newest retained older version without changing files. |
| `jtt rollback` | Requires interactive confirmation before restoring the retained older Release. |

`--channel stable` and `--channel rc` select a track explicitly; `auto` follows
the installed version track. Only published JTT Releases in the `jtt-v...`
namespace are considered. Draft releases, legacy Tower tags, arbitrary
branches, tags without a published Release, malformed versions, channel/tag
mismatches, and tags that cannot be resolved to a verified commit are rejected.
The updater downloads source only for the
verified Release commit. Python runtime dependencies are resolved by pip from
the configured package index during the explicitly confirmed installation;
if this fails, the current launcher remains active. Offline check or download
failure reports an error and does not switch versions.

Development checkouts are never updated through this path. An unmanaged PATH
launcher is never replaced. JTT installs each version separately, verifies
the staged package version, JTT entry point, and import path, then performs an
atomic launcher replacement. Existing version directories and 0600 launcher
backups are retained; no automatic cleanup occurs. `jtt rollback` selects a
previous verified sidecar and does not contact GitHub.

Updates and rollback do not modify JTT_HOME, saved workspaces, terminal
metadata, Supervisor processes, PTYs, Agent programs, SSH/tmux state, or the
legacy `tower` installation. The currently running JTT process is not
restarted; start JTT again when convenient to use a newly selected version.

## JTT doctor

`jtt doctor` reports the imported package and executable, Python/pyte readiness,
Linux/WSL detection, JTT_HOME permissions, workspace readability, Supervisor
registry state, estimated Unix socket path size, and `jtt`/`tower` PATH
separation. It reads existing JTT metadata but does not create missing
directories or probe a live Supervisor socket. A reported running process is
based on the local registry's PID/start identity check, not an interactive
terminal test.

## Release status

Release remains on HOLD until the Draft PR and the actual Windows Terminal/WSL2
user experience are reviewed. A code test using synthetic GitHub Release
responses is not a public Release or a user installation.
