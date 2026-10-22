# JTT Work Kits

A Work Kit is a local template for a future terminal workspace. It stores a
name, one-line description, a one-to-four-card layout, and each terminal's
name, role, and optional working-directory text.

Selecting a Work Kit only previews template cards. It does not create a shell,
run a command, probe or normalize a working-directory path, connect over SSH,
start an Agent, or restore a saved running workspace. Use the separate saved
workspace entries to reconnect to existing JTT terminal session IDs.

Work Kits are stored separately in the owner-private `work_kits.json` file in
the JTT state directory. The strict local JSON schema excludes commands,
environment variables, credentials, process IDs, session IDs, and runtime
state. Explicit creation, editing, renaming and deletion use atomic,
owner-private writes with snapshot revalidation. Esc cancels unsaved edits.
Work Kit execution is not part of this version.

Open LEFT Archive (보관함), then the Work Kit tab. The visual editor supports
adding/removing cards, role presets/custom text, and safe folder browsing or
direct directory entry. Browsing inspects directories only at the user's
request; selecting a template does not probe its paths.
See the [English Quick Start](JTT_QUICK_START.md) or
[한국어 빠른 시작](JTT_QUICK_START.ko.md).
