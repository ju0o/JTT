"""Opt-in smart ``Ctrl+b w``.

The binding itself never changes when Tower starts or exits. One tmux
command checks the session registration at key-press time:

* a live registered Tower pane in this session -> select that pane
* anything else -> tmux's own window/pane chooser

The key does not run ``tower`` from ``PATH`` and does not start a Tower.
The process that is already registered is the one that gets focused.

A custom ``w`` command is stored and put back only by
``tower keys restore``. It is not the command the key runs while this
block is installed, including a helper that opens a separate control
window.

The chooser is whatever a tmux server started with an empty config
reports. It is never a guessed string. The custom command is read from
the live key, then from the user's config with the Tower block removed
(so a ``source-file`` binding is kept), then from an older state file.

``~/.tmux.conf`` is not rewritten. Only the marked block is inserted,
replaced, or removed, and the file is copied aside before the first
change.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Callable, Optional, Tuple

from . import capture
from ..paths import APP_HOME_ISOLATED, CONFIG_DIR

BEGIN = "# >>> tmux-agent-tower smart-w >>>"
END = "# <<< tmux-agent-tower smart-w <<<"
OLD_BLOCKS = (
    (
        "# >>> tmux-agent-tower v0.1.1 focus binding >>>",
        "# <<< tmux-agent-tower v0.1.1 focus binding <<<",
    ),
)
STATE_NAME = "smart-w.json"
CHECK_SHELL = "tower --has-active"
FOCUS_SHELL = 'run-shell -b "tower --focus"'
_OWNED_MARKERS = (
    "tower --focus",
    "tower --has-active",
    "tmux-agent-tower",
    "@tmux_agent_tower_pane",
)


def config_path() -> Path:
    return Path.home() / ".tmux.conf"


def state_path() -> Path:
    return CONFIG_DIR / STATE_NAME


def parse_w_command(list_keys_output: str) -> str:
    """The command half of a ``list-keys -T prefix w`` result.

    ``bind-key -T prefix w choose-tree -Zw`` -> ``choose-tree -Zw``.
    Empty when no line is a prefix ``w`` binding. A bare command such as
    ``choose-tree -Zw`` is not a list-keys line and is not accepted.
    """

    for line in (list_keys_output or "").splitlines():
        text = line.strip()
        marker = " w "
        if "prefix" not in text or marker not in text:
            continue
        return text.split(marker, 1)[1].strip()
    return ""


def is_tower_owned(command: str) -> bool:
    return any(marker in (command or "") for marker in _OWNED_MARKERS)


def runtime_fallback(tmux_default: str) -> str:
    """The ``w`` command used while Tower is not running.

    Always the chooser reported by an empty tmux config. A custom
    command, including one that opens another control window, is not
    used here.
    """

    command = (tmux_default or "").strip()
    if not command or is_tower_owned(command):
        return ""
    return command


def preserved_command(list_keys_line: str, config_line: str = "", saved_restore: str = "") -> str:
    """The user's previous ``w`` command, kept for uninstall only."""

    for raw in (parse_w_command(list_keys_line), parse_w_command(config_line), saved_restore):
        command = (raw or "").strip()
        if command and not is_tower_owned(command):
            return command
    return ""


def tower_invocation() -> str:
    """Kept for callers that still ask which binary installed the key.

    The binding itself does not launch this program.
    """

    argv0 = sys.argv[0] if sys.argv else ""
    if argv0:
        exe = Path(argv0)
        if exe.is_file() and "tower" in exe.name:
            return str(exe.resolve())
    return "tower"


def focus_check_shell() -> str:
    """Shell test for ``if-shell``. Exit 0 only for a focusable Tower.

    ``##`` is one ``#`` after tmux expands the key. The inner ``tmux``
    then reads ``#{pane_dead}`` and ``#{pane_pid}`` for the registered
    pane, not for whichever pane had the key pressed.

    A registration with no pid is the older record: the pane being alive
    is enough. A pid that is dead, or alive outside that pane, is not.
    """

    return (
        'pane=$(tmux show-options -qv @tmux_agent_tower_pane); '
        'pid=$(tmux show-options -qv @tmux_agent_tower_pid); '
        '[ -n "$pane" ] && '
        'dead=$(tmux display-message -p -t "$pane" "##{pane_dead}" 2>/dev/null) && '
        '[ "$dead" = 0 ] && '
        '{ [ -z "$pid" ] || { kill -0 "$pid" 2>/dev/null && { '
        'shell=$(tmux display-message -p -t "$pane" "##{pane_pid}"); '
        'cur=$pid; ok=0; i=0; '
        'while [ "$i" -lt 8 ]; do '
        'if [ "$cur" = "$shell" ]; then ok=1; break; fi; '
        'cur=$(ps -o ppid= -p "$cur" 2>/dev/null | tr -d " "); '
        'if [ -z "$cur" ] || [ "$cur" = 0 ]; then break; fi; '
        'i=$((i+1)); done; '
        '[ "$ok" = 1 ]; }; }; }'
    )


def focus_tmux_command() -> str:
    """Select the registered pane. Does not start Tower.

    ``\\$pane`` is one ``$`` when tmux runs the command. A bare ``$pane``
    is expanded by tmux before the shell starts, so the select target is
    empty and the key reports failure.
    """

    return (
        'run-shell -b "'
        "pane=$(tmux show-options -qv @tmux_agent_tower_pane); "
        'tmux select-window -t \\$pane; tmux select-pane -t \\$pane"'
    )


def render_block(fallback: str, tower_cmd: str = "tower") -> str:
    quoted = "'" + fallback.replace("'", "'\\''") + "'"
    check = focus_check_shell()
    focus = focus_tmux_command()
    return "\n".join([
        BEGIN,
        "# Ctrl+b w: registered Tower pane when one is alive, otherwise",
        "# tmux's window chooser. A previous custom w is restore-only.",
        "unbind-key -T prefix w",
        f"bind-key -T prefix w if-shell '{check}' '{focus}' {quoted}",
        END,
        "",
    ])


def strip_managed(text: str) -> str:
    cleaned = text or ""
    pairs = ((BEGIN, END),) + OLD_BLOCKS
    for begin, end in pairs:
        while True:
            start = cleaned.find(begin)
            if start < 0:
                break
            stop = cleaned.find(end, start)
            if stop < 0:
                cleaned = cleaned[:start]
                break
            stop += len(end)
            if stop < len(cleaned) and cleaned[stop] == "\n":
                stop += 1
            cleaned = cleaned[:start] + cleaned[stop:]
    return cleaned


def upsert_block(text: str, fallback: str, tower_cmd: str = "tower") -> str:
    cleaned = strip_managed(text).rstrip()
    block = render_block(fallback, tower_cmd).rstrip()
    if cleaned:
        return cleaned + "\n\n" + block + "\n"
    return block + "\n"


def block_installed(text: str) -> bool:
    return BEGIN in (text or "") and END in (text or "")


def load_saved_record(path: Path) -> Tuple[str, str, str]:
    """``(runtime command, source, restore command)``.

    Older files stored a custom command in ``fallback``. That value is
    the restore command, not the key's runtime action.
    """

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return "", "", ""
    if not isinstance(data, dict):
        return "", "", ""
    fallback = data.get("fallback")
    command = fallback.strip() if isinstance(fallback, str) else ""
    source = data.get("source")
    if not isinstance(source, str) or not source.strip():
        source = "saved" if command else ""
    source = source.strip()
    restore = data.get("restore")
    if isinstance(restore, str) and restore.strip() and not is_tower_owned(restore):
        restore_command = restore.strip()
    elif source not in ("", "tmux-default") and command and not is_tower_owned(command):
        restore_command = command
    else:
        restore_command = ""
    return command, source, restore_command


def load_saved_fallback(path: Path) -> str:
    """Command ``restore`` should bind. Prefers the saved custom command."""

    command, _source, restore = load_saved_record(path)
    return restore or command


def binding_args(fallback: str, tower_cmd: str = "tower") -> list[str]:
    """``tower_cmd`` is ignored. The key reads session options, not PATH."""

    del tower_cmd
    return [
        "bind-key", "-T", "prefix", "w",
        "if-shell", focus_check_shell(),
        focus_tmux_command(),
        fallback,
    ]


def restore_args(fallback: str) -> list[str]:
    parts = shlex.split(fallback)
    return ["bind-key", "-T", "prefix", "w", *parts]


def _backup_once(path: Path) -> None:
    if not path.exists():
        return
    backup = path.with_name(path.name + ".tower-backup")
    if backup.exists():
        return
    shutil.copy2(path, backup)


def install_smart_w(
    conf_path: Path,
    state_file: Path,
    *,
    list_keys_line: str,
    tmux_default: str,
    apply: Optional[Callable[[list[str]], None]] = None,
    tower_cmd: str = "tower",
    config_line: str = "",
) -> str:
    """Write the managed block and return the fallback command.

    Raises ``RuntimeError`` when no fallback can be determined. Does not
    touch the files in that case.
    """

    _saved, _saved_source, saved_restore = load_saved_record(state_file)
    fallback = runtime_fallback(tmux_default)
    if not fallback:
        raise RuntimeError("no fallback")
    restore = preserved_command(list_keys_line, config_line, saved_restore) or fallback

    previous = conf_path.read_text(encoding="utf-8") if conf_path.exists() else ""
    updated = upsert_block(previous, fallback, tower_cmd)
    if updated != previous:
        _backup_once(conf_path)
        conf_path.parent.mkdir(parents=True, exist_ok=True)
        conf_path.write_text(updated, encoding="utf-8")

    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(
        json.dumps(
            {"fallback": fallback, "source": "tmux-default", "restore": restore},
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    if apply is not None:
        apply(binding_args(fallback, tower_cmd))
    return fallback


def restore_smart_w(
    conf_path: Path,
    state_file: Path,
    *,
    apply: Optional[Callable[[list[str]], None]] = None,
) -> str:
    """Remove only the managed block and re-bind ``w`` to the saved command."""

    fallback = load_saved_fallback(state_file)
    previous = conf_path.read_text(encoding="utf-8") if conf_path.exists() else ""
    updated = strip_managed(previous).rstrip()
    if updated:
        updated += "\n"
    if updated != previous and conf_path.exists():
        _backup_once(conf_path)
        conf_path.write_text(updated, encoding="utf-8")
    if apply is not None and fallback:
        apply(restore_args(fallback))
    return fallback


def query_w_line(argv_prefix: list[str]) -> str:
    """Raw ``list-keys -T prefix w`` output. Callers parse it themselves
    so a command is not stripped and then rejected as "not a list-keys line".
    """

    try:
        proc = subprocess.run(
            [*argv_prefix, "list-keys", "-T", "prefix", "w"],
            capture_output=True, text=True, timeout=5, check=False,
        )
    except Exception:
        return ""
    return proc.stdout or ""


def query_w_command(argv_prefix: list[str]) -> str:
    return parse_w_command(query_w_line(argv_prefix))


def query_configured_w(conf_path: Path) -> str:
    """``w`` as the user's config would bind it without the Tower block.

    Uses a private tmux socket so the running server is not touched.
    A ``source-file`` in that config still applies, which is how a custom
    ``w`` survives when the live key is already Tower's.
    """

    try:
        text = conf_path.read_text(encoding="utf-8")
    except Exception:
        return ""
    stripped = strip_managed(text)
    handle = tempfile.NamedTemporaryFile("w", prefix="tower-w-", suffix=".conf", delete=False, encoding="utf-8")
    tmp = Path(handle.name)
    try:
        handle.write(stripped)
        handle.close()
        sock = f"tower-config-w-{os.getpid()}"
        base = ["tmux", "-f", str(tmp), "-L", sock]
        try:
            return query_w_line(base)
        finally:
            subprocess.run([*base, "kill-server"], capture_output=True, timeout=5, check=False)
    finally:
        tmp.unlink(missing_ok=True)


def query_tmux_default_w() -> str:
    """``w`` from a tmux server that loaded no config file.

    ``-f /dev/null`` has to be on the same invocation that creates the
    server. A later ``list-keys`` without it can start a normal server
    and report the user's own binding back as if it were the default.
    A Tower-owned command is never a default.
    """

    sock = f"tower-default-w-{os.getpid()}"
    base = ["tmux", "-f", "/dev/null", "-L", sock]
    try:
        command = query_w_command(base)
    finally:
        subprocess.run([*base, "kill-server"], capture_output=True, timeout=5, check=False)
    if is_tower_owned(command):
        return ""
    return command


def install_for_user() -> str:
    if APP_HOME_ISOLATED:
        raise RuntimeError("tmux key bindings are unavailable in isolated Tower mode")
    default = query_tmux_default_w()
    live = query_w_line(["tmux"])
    configured = query_configured_w(config_path())
    fallback = install_smart_w(
        config_path(),
        state_path(),
        list_keys_line=live,
        tmux_default=default,
        apply=lambda args: capture.run_tmux(args, capture=False),
        tower_cmd=tower_invocation(),
        config_line=configured,
    )
    return fallback


def restore_for_user() -> str:
    if APP_HOME_ISOLATED:
        raise RuntimeError("tmux key bindings are unavailable in isolated Tower mode")
    return restore_smart_w(
        config_path(),
        state_path(),
        apply=lambda args: capture.run_tmux(args, capture=False),
    )


def installed_for_user() -> bool:
    try:
        return block_installed(config_path().read_text(encoding="utf-8"))
    except Exception:
        return False
