"""User data roots, with an opt-in app-only home for safe sidecars.

``TMUX_AGENT_TOWER_HOME`` redirects Tower-owned config/state while leaving
``HOME`` untouched for OpenSSH, project paths, and other user tools. The
default remains the user's normal home directory.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping

APP_HOME_ENV = "TMUX_AGENT_TOWER_HOME"


def resolve_app_home(environ: Mapping[str, str] | None = None, home: Path | None = None) -> tuple[Path, bool]:
    """Return Tower's data root and whether it was explicitly isolated."""

    env = os.environ if environ is None else environ
    raw = str(env.get(APP_HOME_ENV) or "").strip()
    if not raw:
        return (Path.home() if home is None else Path(home)), False
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise ValueError(f"{APP_HOME_ENV} must be an absolute path")
    return path, True


def configured_path(name: str, default: Path, environ: Mapping[str, str] | None = None) -> Path:
    """Resolve an optional absolute path override without changing defaults."""

    env = os.environ if environ is None else environ
    raw = str(env.get(name) or "").strip()
    if not raw:
        return Path(default)
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise ValueError(f"{name} must be an absolute path")
    return path


APP_HOME, APP_HOME_ISOLATED = resolve_app_home()
CONFIG_DIR = APP_HOME / ".config" / "tmux-agent-tower"
STATE_DIR = APP_HOME / ".cache" / "tmux-agent-tower"
LOCAL_STATE_DIR = APP_HOME / ".local" / "state" / "tmux-agent-tower"
DATA_DIR = APP_HOME / ".local" / "share" / "tmux-agent-tower"
