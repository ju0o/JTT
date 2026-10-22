# JTT — Ju0 Terminal Tower

**English | [한국어](README.ko.md)**

Your local terminal workspace: **Windows on the left, terminal cards in the center, a real interactive terminal on the right.**

Create independent shells, switch between them, detach the UI and reconnect to the same running sessions. No cloud service, model API, Agent account management, or required tmux server.

[Official RC3 prerelease](https://github.com/ju0o/tmux-agent-tower/releases/tag/jtt-v0.3.0-rc3) · [Quick Start](docs/JTT_QUICK_START.md) · [Demo storyboard](docs/JTT_SHOWCASE.md)

## See the workflow

Create → type in RIGHT → switch terminals → detach → reconnect.
The [capture plan](docs/JTT_SHOWCASE.md) uses actual RC3 output with synthetic demo data. The 15–20 second GIF and 60–90 second video are **not yet recorded**. The legacy Tower GIF is not a JTT demo.

## Install official RC3

Requires Linux / WSL2, Python 3.9+ with pip and venv, Git, and a UTF-8 terminal.
Use a directory where a new `jtt-release` folder can be created. Installation needs network access and explicit confirmation. Existing `jtt` commands are protected, not overwritten.

```sh
if (
  set -eu
  JTT_TAG=jtt-v0.3.0-rc3
  JTT_REPO=https://github.com/ju0o/tmux-agent-tower.git
  if ! git ls-remote --exit-code --tags "$JTT_REPO" "refs/tags/$JTT_TAG" >/dev/null; then
    printf 'Cannot verify JTT tag %s (tag unavailable or network error); nothing installed.\n' "$JTT_TAG" >&2
    exit 1
  fi
  if ! git clone --branch "$JTT_TAG" --depth 1 "$JTT_REPO" jtt-release; then
    printf 'Clone failed; the installer was not run.\n' >&2
    exit 1
  fi
  cd jtt-release || exit 1
  ./scripts/jtt-install-release
); then
  :
else
  printf 'JTT installation did not complete; this shell remains open.\n' >&2
fi
```

If needed, add the launcher directory to **this shell's** PATH:

```sh
export PATH="$HOME/.local/bin:$PATH"
jtt --version
jtt doctor
jtt
```

Expect version `0.3.0rc3` and `OFFICIAL`. This is a **prerelease**, not stable.
JTT's `jtt-v0.3.0-rc3` is distinct from legacy Tower's `v0.3.0-rc2`, though both commands share a Python distribution.
The installer validates the published JTT Release, commit, metadata and entry point. It does not change `tower` or shell startup files.
See [updates and rollback](docs/JTT_UPDATING.md) for existing installations.

## First terminal

1. Open `jtt`; opening the UI does not create a shell.
2. Press **Ctrl+G, then n** within four seconds. The new terminal enters input mode when ready.
3. Type `printf 'Hello JTT\n'` and Enter in RIGHT.
4. Press **Ctrl+]** for navigation; Tab / Shift+Tab changes panels.
5. **Ctrl+G d** detaches the UI. Reopen `jtt` with the same state path to reconnect.

| Default key | Action |
| --- | --- |
| Ctrl+G n | New terminal in selected Window |
| Ctrl+G h / l | Previous / next card |
| Ctrl+G w | Focus LEFT |
| Ctrl+G x | Confirm selected terminal stop |
| Ctrl+G d | Detach UI; keep shells |
| Ctrl+G ? | Help |
| Ctrl+] | Input → navigation |
| Ctrl+G Ctrl+G / Ctrl+G Ctrl+] | Send literal reserved key in input mode |

Ordinary `q`, `n`, Tab and Esc go to the terminal in input mode. Navigation `q` does not quit. Prefix Esc cancels a pending command.
[Quick Start](docs/JTT_QUICK_START.md) covers the actual Korean menu labels and configurable-key caveats.

## Organize your work

- **Window:** a logical group, not an OS/tmux window. Creating one does not start a shell.
- **Saved Workspace:** references to existing sessions, reconnected only with matching identities; not process restoration after reboot.
- **Work Kit:** an editable 1–4-card template. Preview/edit/save never executes terminals or commands. RC3 has no Kit execution feature.
- Saved work and templates share **보관함 (Archive)**, in separate tabs.

## Documentation

- [Quick Start](docs/JTT_QUICK_START.md) · [한국어 빠른 시작](docs/JTT_QUICK_START.ko.md)
- [Terminal compatibility, keyboard policy and data safety](docs/JTT_TERMINAL_SUPPORT.md)
- [Work Kits](docs/JTT_WORK_KITS.md) · [Updates / rollback](docs/JTT_UPDATING.md)
- [Showcase production plan](docs/JTT_SHOWCASE.md)
- [Legacy Tower installation and usage](docs/TOWER_LEGACY.md) · [기존 Tower 한국어](docs/TOWER_LEGACY.ko.md)

For a checkout-pinned **DEV** installation, explicitly run `./scripts/jtt-setup-dev` then `./scripts/jtt-install-dev`. Setup may install dependencies into a private environment; ordinary startup does not install packages. DEV installations do not apply official updates.

JTT runs shells as your user and is **not a sandbox**. Its pyte renderer supports a bounded VT subset, not every xterm or Agent TUI. Clipboard and actual WSL restart recovery remain unverified. JTT is MIT; pyte is LGPL-3.0 and wcwidth is MIT. See [dependency notices](docs/JTT_TERMINAL_SUPPORT.md#renderer-dependency-and-license) and the [pyte license](licenses/pyte-LGPL-3.0.txt).
