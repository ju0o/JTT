# OpenCode 1.19.0 TUI session binding

This optional plugin lets Tower read the session currently selected in the
OpenCode TUI. It is supported only by the Linux OpenCode 1.19.0 runtime. Tower
does not install or enable it automatically.

## Loader and dependency contract

The v1.19.0 TUI host creates the legacy TUI plugin host, resolves configured
TUI plugin origins, loads them with `kind: "tui"`, and runs the module's
`tui(api)` initializer. Its public TUI API exposes `api.route.current`; the
binding plugin reads that route rather than inferring a session from a command
line argument or a session list.

TUI plugins are configured through `tui.json` / `tui.jsonc`'s `plugin` list.
Do not confuse this with server plugins in `opencode.json` or the generic
server-plugin directory. For a local file, add its absolute file URL to the
existing TUI `plugin` array, for example:

```json
{
  "plugin": [
    "file:///home/REPLACE_WITH_USER/.config/opencode/plugins/opencode_session_binding.js"
  ]
}
```

The plugin imports `createEffect` from `solid-js`. OpenCode v1.19.0 pins
`solid-js` to `1.9.10`; the dependency must be available from the active
OpenCode config directory. If that directory already has `package.json`, merge
the dependency without replacing its existing fields:

```json
{
  "dependencies": {
    "solid-js": "1.9.10"
  }
}
```

This snippet shows the dependency to merge, not a replacement manifest. Keep
any existing dependencies. The v1.19.0 TUI configuration host prepares local
plugin dependencies during startup; OpenCode's plugin documentation describes
the local `package.json` / Bun dependency workflow. Let the OpenCode host do
that preparation rather than installing packages into a different runtime.

## Reversible installation procedure

These steps are documentation only; no ASUS or user OpenCode configuration
was changed for this PR.

1. Use the OpenCode user's actual config directory (commonly
   `~/.config/opencode`; account for that user's XDG configuration path).
2. Before editing, make owner-only backups of the existing `tui.json` or
   `tui.jsonc`, `package.json` if present, and whichever Bun lockfile exists.
   Record whether the plugin file already existed. Do not replace the config
   directory or existing manifests.
3. Copy `src/tmux_agent_tower/remote/opencode_session_binding.js` to a
   dedicated file under that config directory, such as
   `plugins/opencode_session_binding.js`.
4. Merge that file's absolute `file:` URL into the `plugin` array of the
   active `tui.json` / `tui.jsonc`; preserve every existing plugin and setting.
5. Merge the pinned `solid-js: 1.9.10` dependency into the config directory's
   existing `package.json`, or create a minimal manifest only if none exists.
   Preserve any existing lockfile. Review the diff before restarting OpenCode.
6. After separate approval, restart OpenCode so v1.19.0's TUI loader can load
   the configured module and prepare dependencies. Confirm a fresh binding
   using Tower's metadata-only diagnostics; do not log Result text.

The plugin publishes an owner-only activation marker and a short-lived binding
tied to the OpenCode PID, process start time, and shared nonce. A missing
binding permits the existing screen-history adapter only when the matching
activation marker is also absent. If the marker exists but its binding is
missing, stale, malformed, changed, or identity-mismatched, Tower rejects both
native extraction and screen fallback. The screen-history adapter continues
to require its completion-boundary and truncation checks.

## Removal and rollback

1. Exit the affected OpenCode TUI after approval; do not modify unrelated
   OpenCode sessions.
2. Remove only this plugin's exact file URL from `tui.json` / `tui.jsonc`,
   leaving other plugin entries and settings untouched.
3. Remove the plugin file only if it is the file added for this installation
   and no other configuration references it.
4. Remove `solid-js` only if this installation added it and no other plugin
   uses it. If the manifest or lockfile has since changed, merge the removal;
   restore a backup wholesale only when there have been no intervening user
   changes.
5. Restart OpenCode after approval. If rolling back a failed installation,
   restore only the backed-up files that remain unchanged since the install.

No user configuration, plugin, package manifest, or OpenCode process was
modified as part of this PR.

## Upstream v1.19.0 references

- [TUI plugin runtime](https://github.com/anomalyco/opencode/blob/v1.19.0/packages/opencode/src/plugin/tui/runtime.ts): external origins load with `kind: "tui"` and initialize the module's `tui` export.
- [TUI plugin API](https://github.com/anomalyco/opencode/blob/v1.19.0/packages/plugin/src/tui.ts): `TuiPluginModule` shape and `api.route.current` contract.
- [TUI config loader](https://github.com/anomalyco/opencode/blob/v1.19.0/packages/opencode/src/config/tui.ts): global/project `tui.json` and `tui.jsonc` sources, plugin origins, and startup dependency preparation.
- [v1.19.0 package catalog](https://github.com/anomalyco/opencode/blob/v1.19.0/package.json): `solid-js` is pinned to `1.9.10`.
- [Official plugin dependency guide](https://github.com/anomalyco/opencode/blob/v1.19.0/packages/web/src/content/docs/plugins.mdx): local external dependencies are declared in the config-directory `package.json` and prepared at startup.
