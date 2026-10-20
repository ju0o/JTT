"""Read-only environment diagnostics for the standalone JTT command."""

from __future__ import annotations

import importlib.metadata
import os
from pathlib import Path
import platform
import shutil
import stat
import sys

from . import __version__
from .jtt_install import installation_kind
from .state.jtt_workspaces import JTTWorkspaceStore, WorkspaceStoreError, default_jtt_root
from .terminal import manager as terminal_manager


def _private_directory_status(path: Path) -> tuple[str, bool | None]:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return "not created (JTT will not create it for diagnostics)", None
    except OSError:
        return "cannot inspect", False
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        return "unsafe: not a regular directory", False
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        return "unsafe: expected user-owned mode 0700", False
    return "private user-owned directory", True


def _renderer_status() -> tuple[str, bool]:
    try:
        import pyte

        version = importlib.metadata.version("pyte")
        screen = pyte.Screen(8, 2)
        pyte.Stream(screen).feed("\x1b[H JTT")
        if "JTT" not in "".join(screen.display):
            return f"pyte {version}: VT smoke test failed", False
        return f"pyte {version}: import and VT initialization ready", True
    except ImportError:
        return "pyte is missing; run the explicit JTT environment setup", False
    except Exception as exc:
        return f"pyte could not initialize ({type(exc).__name__})", False


def _platform_status() -> str:
    if platform.system() != "Linux":
        return f"{platform.system()} (JTT PTY runtime targets Linux/WSL2)"
    try:
        kernel = Path("/proc/sys/kernel/osrelease").read_text(encoding="ascii", errors="ignore").lower()
    except OSError:
        kernel = ""
    wsl = "microsoft" in kernel or bool(os.environ.get("WSL_DISTRO_NAME"))
    return "Linux / WSL2 detected" if wsl else "Linux (WSL2 not detected)"


def _workspace_summary(root: Path) -> tuple[str, bool]:
    try:
        workspaces = JTTWorkspaceStore(root=root).workspaces()
        return f"read-only: {len(workspaces)} workspace record(s)", True
    except WorkspaceStoreError:
        return "cannot safely read workspace data; no repair was attempted", False


def _session_summary() -> tuple[Path | None, str, bool]:
    try:
        root = terminal_manager._default_root()
        registry = terminal_manager.TerminalRegistry(root=root)
        sessions = registry.list_sessions()
    except terminal_manager.TerminalError:
        return None, "registry path or metadata could not be safely inspected", False
    counts = {state: 0 for state in ("running", "stopping", "orphaned", "exited")}
    for session in sessions:
        counts[session.state if session.state in counts else "orphaned"] += 1
    summary = ", ".join(f"{state}={count}" for state, count in counts.items())
    fake_id = "0" * 32
    try:
        candidate = terminal_manager._planned_socket_path(root / fake_id, fake_id)
        byte_count = len(os.fsencode(candidate))
        if byte_count > terminal_manager.MAX_UNIX_SOCKET_PATH_BYTES:
            return root, f"{summary}; estimated socket path too long ({byte_count} bytes)", False
        return root, f"{summary}; estimated socket path {byte_count}/{terminal_manager.MAX_UNIX_SOCKET_PATH_BYTES} bytes", True
    except (OSError, ValueError):
        return root, f"{summary}; socket path could not be estimated", False


def _path_collision() -> str:
    jtt_path = shutil.which("jtt")
    tower_path = shutil.which("tower")
    if not jtt_path:
        return "jtt not found on PATH; tower command is independent"
    if not tower_path:
        return f"jtt={jtt_path}; tower not found on PATH"
    same = os.path.realpath(jtt_path) == os.path.realpath(tower_path)
    if not same:
        try:
            same = os.path.samefile(jtt_path, tower_path)
        except OSError:
            pass
    return f"jtt={jtt_path}; tower={tower_path}; {'COLLISION' if same else 'separate commands'}"


def run_doctor(output=None) -> int:
    """Print local runtime facts without creating a PTY, socket, or data file."""

    output = output or sys.stdout
    problems = 0
    kind = installation_kind()
    executable = Path(os.environ.get("JTT_LAUNCHER_PATH") or sys.argv[0]).expanduser().absolute()
    module = Path(__file__).resolve()
    print("JTT — Ju0 Terminal Tower doctor (read-only)", file=output)
    print(f"Version: {__version__} [{kind}]", file=output)
    print(f"Executable: {executable}", file=output)
    print(f"Module: {module}", file=output)
    print(f"Python: {sys.version.split()[0]} ({sys.executable})", file=output)
    renderer, renderer_ok = _renderer_status()
    print(f"Renderer: {renderer}", file=output)
    problems += not renderer_ok
    print(f"Platform: {_platform_status()}", file=output)

    workspace_root = default_jtt_root()
    workspace_status, workspace_safe = _private_directory_status(workspace_root)
    print(f"JTT_HOME: {workspace_root} ({workspace_status})", file=output)
    if workspace_safe is False:
        problems += 1
    workspace_data, workspace_readable = _workspace_summary(workspace_root)
    print(f"Workspace data: {workspace_data}", file=output)
    problems += not workspace_readable

    registry_root, session_status, registry_ok = _session_summary()
    print(f"Supervisor registry: {registry_root if registry_root else 'unavailable'}", file=output)
    print(f"Supervisor status: {session_status}", file=output)
    problems += not registry_ok
    print(f"PATH check: {_path_collision()}", file=output)
    if kind == "DEV":
        print("Update status: disabled for development checkouts; no Release request was made.", file=output)
    print("No PTY, Agent, network request, or repair action was started.", file=output)
    print("Result: {}".format("review recommended" if problems else "diagnostics complete"), file=output)
    return 1 if problems else 0


__all__ = ["run_doctor"]
