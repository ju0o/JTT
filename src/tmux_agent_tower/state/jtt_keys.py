"""Private, strict configuration for JTT's two reserved terminal keys.

The configuration is deliberately read-only at startup. JTT never creates or
rewrites ``keybindings.json`` implicitly.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import stat


PREFIX_TIMEOUT_SECONDS = 4.0
_MAX_CONFIG_BYTES = 4096
_SCHEMA_KEYS = {"version", "prefix", "input_escape"}

# These control chords do not arrive as an unambiguous curses character on
# common POSIX terminals: terminal signals, flow control, line editing, and
# canonical key encodings can intercept or alias them.
_UNOBSERVABLE_CONTROL_LETTERS = {"C", "H", "I", "J", "M", "Q", "S", "Z"}


class JTTKeybindingsError(ValueError):
    """The optional JTT keybinding file is unsafe or invalid."""


@dataclass(frozen=True)
class JTTKeybindings:
    prefix: str = "Ctrl+G"
    input_escape: str = "Ctrl+]"
    prefix_byte: int = 7
    input_escape_byte: int = 29
    timeout_seconds: float = PREFIX_TIMEOUT_SECONDS

    def matches_prefix(self, key) -> bool:
        return _matches_byte(key, self.prefix_byte)

    def matches_input_escape(self, key) -> bool:
        return _matches_byte(key, self.input_escape_byte)


DEFAULT_JTT_KEYBINDINGS = JTTKeybindings()


def _matches_byte(key, byte: int) -> bool:
    if isinstance(key, str):
        return len(key) == 1 and ord(key) == byte
    return isinstance(key, int) and not isinstance(key, bool) and key == byte


def _parse_key_name(value: object, field: str) -> tuple[str, int]:
    if not isinstance(value, str):
        raise JTTKeybindingsError(f"{field} must be a supported Ctrl key name")
    if value == "Ctrl+ ]":
        raise JTTKeybindingsError(f"{field} is not a canonical key name")
    if value == "Ctrl+]":
        return value, 0x1D
    if len(value) == 6 and value.startswith("Ctrl+") and value[-1] in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
        letter = value[-1]
        if letter in _UNOBSERVABLE_CONTROL_LETTERS:
            raise JTTKeybindingsError(f"{field} is ambiguous or intercepted by the terminal")
        return value, ord(letter) & 0x1F
    raise JTTKeybindingsError(
        f"{field} must be Ctrl+A..Ctrl+Z (excluding ambiguous keys) or Ctrl+]"
    )


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise JTTKeybindingsError("JTT keybinding JSON contains a duplicate field")
        result[key] = value
    return result


def _private_config_bytes(path: Path) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    nofollow = getattr(os, "O_NOFOLLOW", None)
    directory_flag = getattr(os, "O_DIRECTORY", None)
    if nofollow is None or directory_flag is None:
        raise JTTKeybindingsError("This platform cannot safely inspect JTT keybindings")
    root_fd = None
    file_fd = None
    try:
        try:
            root_fd = os.open(path.parent, os.O_RDONLY | directory_flag | nofollow
                              | getattr(os, "O_CLOEXEC", 0))
            root_info = os.fstat(root_fd)
            if (not stat.S_ISDIR(root_info.st_mode) or root_info.st_uid != os.getuid()
                    or stat.S_IMODE(root_info.st_mode) & 0o077):
                raise JTTKeybindingsError("JTT state directory must be an owner-only directory")
            file_fd = os.open(path.name, flags | nofollow | getattr(os, "O_NONBLOCK", 0),
                              dir_fd=root_fd)
        except OSError as exc:
            raise JTTKeybindingsError("JTT keybindings cannot be opened safely") from exc
        info = os.fstat(file_fd)
        file_mode = stat.S_IMODE(info.st_mode)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or file_mode & 0o077 or file_mode & 0o111 or not file_mode & 0o400
                or info.st_nlink != 1):
            raise JTTKeybindingsError(
                "JTT keybindings must be an owner-only regular file without links"
            )
        with os.fdopen(file_fd, "rb", closefd=False) as stream:
            raw = stream.read(_MAX_CONFIG_BYTES + 1)
        if len(raw) > _MAX_CONFIG_BYTES:
            raise JTTKeybindingsError("JTT keybindings exceed the allowed size")
        return raw
    finally:
        if file_fd is not None:
            os.close(file_fd)
        if root_fd is not None:
            os.close(root_fd)


def load_jtt_keybindings(jtt_home: str | Path) -> JTTKeybindings:
    """Load safe JTT-only JSON; absent config uses immutable built-in defaults."""

    root = Path(jtt_home).expanduser()
    config_path = root / "keybindings.json"
    try:
        root_info = root.lstat()
    except FileNotFoundError:
        return DEFAULT_JTT_KEYBINDINGS
    except OSError as exc:
        raise JTTKeybindingsError("JTT state directory cannot be inspected safely") from exc
    if (not stat.S_ISDIR(root_info.st_mode) or root_info.st_uid != os.getuid()
            or stat.S_IMODE(root_info.st_mode) & 0o077):
        raise JTTKeybindingsError("JTT state directory must be an owner-only directory")
    if not os.path.lexists(config_path):
        return DEFAULT_JTT_KEYBINDINGS

    raw = _private_config_bytes(config_path)
    try:
        payload = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
    except JTTKeybindingsError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise JTTKeybindingsError("JTT keybindings must contain valid UTF-8 JSON") from exc
    if not isinstance(payload, dict) or set(payload) != _SCHEMA_KEYS:
        raise JTTKeybindingsError("JTT keybindings must match schema version 1 exactly")
    if type(payload.get("version")) is not int or payload.get("version") != 1:
        raise JTTKeybindingsError("Unsupported JTT keybindings schema version")
    prefix, prefix_byte = _parse_key_name(payload.get("prefix"), "prefix")
    input_escape, input_escape_byte = _parse_key_name(payload.get("input_escape"), "input_escape")
    if prefix_byte == input_escape_byte:
        raise JTTKeybindingsError("JTT prefix and input escape must be different keys")
    return JTTKeybindings(prefix, input_escape, prefix_byte, input_escape_byte)
