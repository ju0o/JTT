"""Workspace Launcher configuration.

Deliberately does NOT hardcode any user-specific path into the product
(see ``default_project_roots``). ``~/.config/tmux-agent-tower/config.toml``
is optional; everything here has a safe default so a fresh install works
with zero configuration.

This reads a *subset* of TOML sufficient for our own controlled file shape
(top-level string/array-of-strings keys, plus one ``[agents]`` table of
string values). It is not a general TOML parser -- adding a dependency for
that felt disproportionate to a zero-runtime-dependency local tool. If the
file doesn't parse, we fail safe to defaults rather than crashing.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Dict, List, Optional

from ..paths import CONFIG_DIR

CONFIG_FILE = CONFIG_DIR / "config.toml"

# Label used throughout the UI/adapters -> the config key a user would write
# in [agents] to override its launch command.
AGENT_LABEL_TO_CONFIG_KEY = {
    "Codex": "codex",
    "Claude": "claude",
    "OpenCode": "opencode",
    "Grok": "grok",
    "Cursor": "cursor",
    "Shell": "shell",
}

AGENT_LAUNCH_ORDER = ["Codex", "Claude", "OpenCode", "Grok", "Cursor", "Shell"]


def _default_shell() -> str:
    import os

    return os.environ.get("SHELL") or "bash"


def default_agent_commands() -> Dict[str, str]:
    return {
        "Codex": "codex",
        "Claude": "claude",
        "OpenCode": "opencode",
        "Grok": "grok",
        # tmux/ps report cursor-agent's own process as "agent" (see
        # adapters/cursor.py) but the command a user actually types is
        # "cursor-agent".
        "Cursor": "cursor-agent",
        "Shell": _default_shell(),
    }


# Never assume any specific user's directory layout exists -- only offer a
# root if it's actually there.
_CANDIDATE_ROOT_NAMES = ["Projects", "projects", "code", "Code", "dev", "src", "workspace", "Workspace"]


def default_project_roots() -> List[str]:
    home = Path.home()
    return [str(home / name) for name in _CANDIDATE_ROOT_NAMES if (home / name).is_dir()]


_ARRAY_RE = re.compile(r'^\s*project_roots\s*=\s*\[(.*)\]\s*$')
_SECTION_RE = re.compile(r'^\s*\[(\w+)\]\s*$')
_KV_RE = re.compile(r'^\s*(\w+)\s*=\s*"([^"]*)"\s*$')
_BOOL_KV_RE = re.compile(r'^\s*(\w+)\s*=\s*(true|false)\s*$', re.IGNORECASE)
_STRING_ITEM_RE = re.compile(r'"([^"]*)"')


def _parse_config_text(text: str) -> Dict:
    # "notification_kinds" (the [notifications] section's own sub-keys,
    # e.g. waiting/dead) is deliberately a different dict key than the
    # top-level "notifications" boolean toggle -- they'd otherwise collide
    # (a bare `notifications = true` and a `[notifications]` section both
    # wanting to write into the same `result["notifications"]` slot).
    result: Dict = {"project_roots": None, "agents": {}, "notification_kinds": {}, "remote": {}}
    section: Optional[str] = None

    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0]
        if not line.strip():
            continue

        section_match = _SECTION_RE.match(line)
        if section_match:
            section = section_match.group(1)
            continue

        if section is None:
            array_match = _ARRAY_RE.match(line)
            if array_match:
                items = _STRING_ITEM_RE.findall(array_match.group(1))
                result["project_roots"] = [str(Path(p).expanduser()) for p in items]
                continue

        bool_match = _BOOL_KV_RE.match(line)
        if bool_match:
            key, value = bool_match.group(1), bool_match.group(2).lower() == "true"
            if section == "notifications":
                result["notification_kinds"][key] = value
            elif section == "remote":
                result["remote"][key] = value
            elif section is None:
                result[key] = value
            continue

        kv_match = _KV_RE.match(line)
        if kv_match:
            key, value = kv_match.group(1), kv_match.group(2)
            if section == "agents":
                result["agents"][key] = value
            elif section is None:
                result[key] = value

    return result


def load_config() -> Dict:
    """Returns ``{"language": str|None, "project_roots": [...],
    "agents": {Label: command}, "show_activity": bool,
    "show_status_duration": bool, "notifications": bool,
    "notification_kinds": {"waiting": bool, "dead": bool},
    "remote_autostart": bool, "remote_intro_seen": bool}``.

    Notifications default OFF (safe/quiet by default -- see
    docs/ROADMAP.md's Task Awareness section); activity and duration
    display default ON since they're purely informational.
    """

    agents = default_agent_commands()
    project_roots: Optional[List[str]] = None
    language = None
    show_activity = True
    show_status_duration = True
    notifications = False
    notification_kinds = {"waiting": True, "dead": False}
    remote_autostart = False
    remote_intro_seen = False

    try:
        text = CONFIG_FILE.read_text(encoding="utf-8")
        parsed = _parse_config_text(text)
        if parsed.get("project_roots"):
            project_roots = parsed["project_roots"]
        language = parsed.get("language")

        if "show_activity" in parsed:
            show_activity = bool(parsed["show_activity"])
        if "show_status_duration" in parsed:
            show_status_duration = bool(parsed["show_status_duration"])
        if "notifications" in parsed:
            notifications = bool(parsed["notifications"])

        for kind, value in parsed.get("notification_kinds", {}).items():
            notification_kinds[kind] = bool(value)

        remote_cfg = parsed.get("remote") or {}
        if "autostart" in remote_cfg:
            remote_autostart = bool(remote_cfg["autostart"])
        if "intro_seen" in remote_cfg:
            remote_intro_seen = bool(remote_cfg["intro_seen"])

        config_key_to_label = {v: k for k, v in AGENT_LABEL_TO_CONFIG_KEY.items()}
        for config_key, command in parsed.get("agents", {}).items():
            label = config_key_to_label.get(config_key)
            if label and command:
                agents[label] = command
    except FileNotFoundError:
        pass
    except Exception:
        # Malformed config: fail safe to defaults.
        pass

    if project_roots is None:
        project_roots = default_project_roots()

    return {
        "language": language,
        "project_roots": project_roots,
        "agents": agents,
        "show_activity": show_activity,
        "show_status_duration": show_status_duration,
        "notifications": notifications,
        "notification_kinds": notification_kinds,
        "remote_autostart": remote_autostart,
        "remote_intro_seen": remote_intro_seen,
    }


_REMOTE_SECTION_RE = re.compile(r"^\s*\[remote\]\s*$")
_ANY_SECTION_RE = re.compile(r"^\s*\[")


def set_remote_flag(name: str, value: bool) -> None:
    """Set one bool inside the ``[remote]`` table (``autostart`` or
    ``intro_seen``) without rewriting any other key in the file.
    """

    if name not in ("autostart", "intro_seen"):
        return

    new_line = f"{name} = {'true' if value else 'false'}"

    try:
        lines = CONFIG_FILE.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        lines = []
    except Exception:
        return

    start = next((i for i, line in enumerate(lines) if _REMOTE_SECTION_RE.match(line)), None)
    if start is None:
        if lines and lines[-1].strip():
            lines.append("")
        lines.extend(["[remote]", new_line])
    else:
        end = next((j for j in range(start + 1, len(lines)) if _ANY_SECTION_RE.match(lines[j])), len(lines))
        replaced = False
        for j in range(start + 1, end):
            key = lines[j].split("=", 1)[0].strip()
            if key == name:
                lines[j] = new_line
                replaced = True
                break
        if not replaced:
            lines.insert(start + 1, new_line)

    try:
        CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except Exception:
        pass


def resolve_agent_command(agent_label: str, agents_cfg: Dict[str, str]) -> Optional[str]:
    """Full command string if its binary is on PATH, else ``None``."""

    command = agents_cfg.get(agent_label)
    if not command:
        return None

    binary = command.split()[0]
    if shutil.which(binary) is None:
        return None

    return command
