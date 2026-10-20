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
state. Files are read-only until the user explicitly creates a Work Kit; writes
are atomic and owner-private. Work Kit execution is not part of this version.
