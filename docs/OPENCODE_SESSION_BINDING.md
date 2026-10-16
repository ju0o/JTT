# OpenCode 1.19.0 / 1.19.1 TUI session binding

This optional plugin lets Tower read the session currently selected in the
OpenCode TUI. It is supported only by the Linux OpenCode 1.19.0 and 1.19.1
runtimes. Other versions are rejected; Tower does not install or enable it
automatically.

## Loader and dependency contract

Both supported TUI hosts create the legacy TUI plugin host, resolve configured
TUI plugin origins, load them with `kind: "tui"`, and run the module's
`tui(api)` initializer. Its public TUI API exposes `api.route.current`; the
binding plugin reads that route rather than inferring a session from a command
line argument or a session list.

TUI plugins are configured through `tui.json` / `tui.jsonc`'s `plugin` list.
Do not confuse this with server plugins in `opencode.json` or OpenCode's
automatic server-plugin discovery. OpenCode 1.19.1 scans
`{plugin,plugins}/*.{ts,js}` under the config directory as server plugins;
those modules are expected to export `server()`. Keep this TUI-only module in
a separate directory such as `tower-tui/`, outside both `plugin/` and
`plugins/`. Its default export has a stable, unique TUI ID and a `tui(api)`
entry point, and must not export `server`. For a local file, add its absolute
file URL to the existing TUI `plugin` array, for example:

```json
{
  "plugin": [
    "file:///home/REPLACE_WITH_USER/.config/opencode/tower-tui/opencode_session_binding.js"
  ]
}
```

The plugin imports `createEffect` from `solid-js`. OpenCode v1.19.0 and v1.19.1
both pin `solid-js` to `1.9.10`; the dependency must be available from the
active OpenCode config directory. If that directory already has `package.json`,
merge the dependency without replacing its existing fields:

```json
{
  "dependencies": {
    "solid-js": "1.9.10"
  }
}
```

This snippet shows the dependency to merge, not a replacement manifest. Keep
any existing dependencies. The supported TUI configuration host prepares local
plugin dependencies during startup; OpenCode's plugin documentation describes
the local `package.json` / Bun dependency workflow. Let the OpenCode host do
that preparation rather than installing packages into a different runtime.

## Reversible installation procedure

These steps are instructions, not an automatic installer. Apply them only
after approval and when the affected user's OpenCode TUI is not running.

1. Use the OpenCode user's actual config directory (commonly
   `~/.config/opencode`; account for that user's XDG configuration path).
2. Before editing, create an owner-only backup of the existing `tui.json` or
   `tui.jsonc`, `package.json` if present, the existing lockfile, and the Tower
   plugin file if present. Record hashes, ownership, permissions, and absent
   paths. Do not replace the config directory or existing manifests.
3. Check that no OpenCode process is using the config and that no other config
   entry references the old Tower file. Preserve every unrelated file under
   `plugin/` and `plugins/`.
4. Create `tower-tui/` with owner-only directory permissions. Copy
   `src/tmux_agent_tower/remote/opencode_session_binding.js` to
   `tower-tui/opencode_session_binding.js`; verify the source hash, owner, and
   file permissions before changing the TUI config.
5. Move only the old Tower file from `plugins/opencode_session_binding.js` to
   the verified owner-only backup, outside `plugin/` and `plugins/`. Do not
   leave a `.js`/`.ts` copy of the TUI plugin in either auto-discovery folder.
6. Atomically change only Tower's URL in the active `tui.json` / `tui.jsonc`
   to the new `tower-tui/` file URL. Preserve all other settings and plugin
   entries, then validate the JSON and the final file hash. Do not start
   OpenCode until both the move and config update have been verified.
7. Merge the pinned `solid-js: 1.9.10` dependency into the existing
   `package.json`, preserving all other fields. Keep the original lockfile in
   place rather than replacing it manually. OpenCode may prepare plugin
   dependencies at startup and update dependency metadata; back up those files
   first, review the resulting change, do not run a second package manager, and
   never restore whole manifests over intervening user changes.
8. After separate approval, start OpenCode once and check the plugin marker
   and binding metadata. Do not log Result text.

### Migrating an existing `plugins/` installation

An installation at `~/.config/opencode/plugins/opencode_session_binding.js`
is in the server-plugin auto-discovery folder and can be loaded as a server
plugin before the TUI plugin configuration is processed. The `tower-tui/`
location avoids that collision. Follow steps 2–6 above; move only the Tower
file, and leave all other auto-discovered plugins untouched. If any hash,
ownership, permission, or config check differs from the recorded baseline,
stop without starting OpenCode and report the changed path.

### Recovery

- Before starting OpenCode, if the new file or config check fails, keep the
  affected TUI stopped. Restore only files whose current hashes still match
  this migration's recorded post-change hashes; otherwise merge the recovery
  manually and preserve intervening changes.
- To disable Tower safely, remove only its new `tower-tui/` URL from the TUI
  plugin list and keep the Tower file in the owner-only backup outside
  `plugin/` and `plugins/`.
- Do not move the old TUI module back into `plugin/` or `plugins/` as a normal
  rollback: that recreates the confirmed server-plugin discovery collision.
  Restore the old layout only as an explicitly accepted return to the prior,
  known-failing state.
- Restore `package.json` or a lockfile wholesale only if its current hash
  still matches the migration's post-change hash. Otherwise remove only
  dependencies introduced by this installation after checking that no other
  plugin uses them.

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
3. Remove `tower-tui/opencode_session_binding.js` only if it is the file added
   for this installation and no other configuration references it. Keep it
   outside `plugin/` and `plugins/` while it is disabled.
4. Remove `solid-js` only if this installation added it and no other plugin
   uses it. If the manifest or lockfile has since changed, merge the removal;
   restore a backup wholesale only when there have been no intervening user
   changes.
5. Restart OpenCode after approval. If rolling back a failed installation,
   restore only the backed-up files that remain unchanged since the install.

This PR does not install plugins or modify user configuration automatically.

## Upstream references

- [TUI plugin runtime](https://github.com/anomalyco/opencode/blob/v1.19.0/packages/opencode/src/plugin/tui/runtime.ts): external origins load with `kind: "tui"` and initialize the module's `tui` export.
- [TUI plugin API](https://github.com/anomalyco/opencode/blob/v1.19.0/packages/plugin/src/tui.ts): `TuiPluginModule` shape and `api.route.current` contract.
- [TUI config loader](https://github.com/anomalyco/opencode/blob/v1.19.0/packages/opencode/src/config/tui.ts): global/project `tui.json` and `tui.jsonc` sources, plugin origins, and startup dependency preparation.
- [v1.19.0 package catalog](https://github.com/anomalyco/opencode/blob/v1.19.0/package.json): `solid-js` is pinned to `1.9.10`.
- [Official plugin dependency guide](https://github.com/anomalyco/opencode/blob/v1.19.0/packages/web/src/content/docs/plugins.mdx): local external dependencies are declared in the config-directory `package.json` and prepared at startup.
- [v1.19.1 server-plugin discovery](https://github.com/anomalyco/opencode/blob/v1.19.1/packages/opencode/src/config/plugin.ts): `ConfigPlugin.load()` discovers `.js` / `.ts` files directly under `plugin/` and `plugins/`.
- [v1.19.1 server-plugin runtime](https://github.com/anomalyco/opencode/blob/v1.19.1/packages/opencode/src/plugin/index.ts): the general plugin loader uses `kind: "server"` and requires the server plugin contract.
- [v1.19.1 TUI config loader](https://github.com/anomalyco/opencode/blob/v1.19.1/packages/opencode/src/config/tui.ts), [TUI plugin runtime](https://github.com/anomalyco/opencode/blob/v1.19.1/packages/opencode/src/plugin/tui/runtime.ts), [TUI plugin API](https://github.com/anomalyco/opencode/blob/v1.19.1/packages/plugin/src/tui.ts), and [package catalog](https://github.com/anomalyco/opencode/blob/v1.19.1/package.json): TUI config resolves its own plugin list and the TUI runtime loads it with the TUI contract; the relevant API/runtime and `solid-js` pin match the supported 1.19.x behavior.
