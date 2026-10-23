# JTT — Ju0 Terminal Tower

**English | [한국어](README.ko.md)**

Your local terminal workspace: **Windows on the left, terminal cards in the center, a real interactive terminal on the right.**

Create independent shells, switch between them, detach the UI and reconnect to the same running sessions. No cloud service, model API, Agent account management, or required tmux server.

Unlike legacy Tower, which observes existing tmux panes, JTT owns its local terminals. You choose what to run inside them.

[Official RC3 prerelease](https://github.com/ju0o/tmux-agent-tower/releases/tag/jtt-v0.3.0-rc3) · [Quick Start](docs/JTT_QUICK_START.md) · [75-second walkthrough](media/jtt-rc3-intro.mp4)

## See the workflow

![JTT RC3 edited PTY demonstration with synthetic data: create local terminals, type in RIGHT, switch cards, detach the UI and reconnect.](media/jtt-rc3-showcase.gif)

Create → type in RIGHT → switch terminals → detach → reconnect.
Actual RC3 curses/PTY output, rendered as an **edited demonstration with synthetic data**; this is not a native Windows Terminal recording. Timing is edited, not a performance benchmark. Native Windows Terminal visual verification remains pending.

[Video / thumbnail](docs/JTT_MEDIA.md) · [English subtitles](media/jtt-rc3-intro.en.srt) · [한국어 자막](media/jtt-rc3-intro.ko.srt) · [Text alternative and verification](docs/JTT_MEDIA.md)

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
The proposed post-RC3 package identity and manual side-by-side transition are
documented in [JTT package transition](docs/JTT_PACKAGE_MIGRATION.md). The
`ju0-terminal-tower` candidate is not published to PyPI; the official RC3
installation instructions above remain the current public install path.

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
- [Showcase and capture boundary](docs/JTT_SHOWCASE.md) · [Accessible media description](docs/JTT_MEDIA.md)
- [Legacy Tower installation and usage](docs/TOWER_LEGACY.md) · [기존 Tower 한국어](docs/TOWER_LEGACY.ko.md)

For a checkout-pinned **DEV** installation, explicitly run `./scripts/jtt-setup-dev` then `./scripts/jtt-install-dev`. Setup may install dependencies into a private environment; ordinary startup does not install packages. DEV installations do not apply official updates.

JTT runs shells as your user and is **not a sandbox**. Its pyte renderer supports a bounded VT subset, not every xterm or Agent TUI. Clipboard and actual WSL restart recovery remain unverified. JTT is MIT; pyte is LGPL-3.0 and wcwidth is MIT. See [dependency notices](docs/JTT_TERMINAL_SUPPORT.md#renderer-dependency-and-license) and the [pyte license](licenses/pyte-LGPL-3.0.txt).
