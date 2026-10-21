"""Load local, role-specific guidance without involving an Agent provider."""

from __future__ import annotations

import errno
import os
import stat
from importlib.resources import files
from pathlib import Path

from .state.overrides import ROLE_IDS
from .paths import CONFIG_DIR

DEFAULT_HARNESS_DIR = CONFIG_DIR / "harnesses"
MAX_HARNESS_BYTES = 64 * 1024


class HarnessError(ValueError):
    """A role harness could not be safely loaded."""


def load_harness(role_id: str, *, harness_dir: Path | None = None) -> str:
    """Return the local override for a role, or its packaged Markdown default."""

    if not isinstance(role_id, str) or role_id not in ROLE_IDS:
        raise HarnessError("지원하지 않는 작업 역할입니다.")

    override = _read_override(role_id, Path(harness_dir) if harness_dir is not None else DEFAULT_HARNESS_DIR)
    if override is not None:
        return _decode_harness(override, f"사용자 역할 안내 ({role_id})")
    return _read_default(role_id)


def _read_override(role_id: str, harness_dir: Path) -> bytes | None:
    harness_dir = harness_dir.expanduser()
    try:
        info = harness_dir.lstat()
    except FileNotFoundError:
        return None
    except (OSError, RuntimeError) as exc:
        raise HarnessError("사용자 역할 안내 경로를 안전하게 확인할 수 없습니다.") from exc
    if stat.S_ISLNK(info.st_mode):
        raise HarnessError("사용자 역할 안내 디렉터리는 심볼릭 링크일 수 없습니다.")
    try:
        root = harness_dir.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise HarnessError("사용자 역할 안내 경로를 안전하게 확인할 수 없습니다.") from exc

    if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
        raise HarnessError("이 환경에서는 사용자 역할 안내 경로를 안전하게 확인할 수 없습니다.")

    directory_fd = None
    file_fd = None
    try:
        directory_fd = os.open(
            root,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
        )
        try:
            file_fd = os.open(
                f"{role_id}.md",
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0),
                dir_fd=directory_fd,
            )
        except FileNotFoundError:
            return None
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise HarnessError("사용자 역할 안내 파일은 심볼릭 링크일 수 없습니다.") from exc
            raise HarnessError("사용자 역할 안내 파일을 안전하게 열 수 없습니다.") from exc

        info = os.fstat(file_fd)
        if not stat.S_ISREG(info.st_mode):
            raise HarnessError("사용자 역할 안내는 일반 파일이어야 합니다.")
        if info.st_size > MAX_HARNESS_BYTES:
            raise HarnessError(f"사용자 역할 안내가 {MAX_HARNESS_BYTES}바이트 제한을 초과했습니다.")

        with os.fdopen(file_fd, "rb") as stream:
            file_fd = None
            data = stream.read(MAX_HARNESS_BYTES + 1)
        if len(data) > MAX_HARNESS_BYTES:
            raise HarnessError(f"사용자 역할 안내가 {MAX_HARNESS_BYTES}바이트 제한을 초과했습니다.")
        return data
    except HarnessError:
        raise
    except OSError as exc:
        if exc.errno == errno.ENOENT:
            return None
        raise HarnessError("사용자 역할 안내 디렉터리를 읽을 수 없습니다.") from exc
    finally:
        if file_fd is not None:
            os.close(file_fd)
        if directory_fd is not None:
            os.close(directory_fd)


def _read_default(role_id: str) -> str:
    resource = files("tmux_agent_tower").joinpath("resources", "harnesses", f"{role_id}.md")
    try:
        with resource.open("rb") as stream:
            data = stream.read(MAX_HARNESS_BYTES + 1)
    except (OSError, FileNotFoundError) as exc:
        raise HarnessError(f"기본 역할 안내 파일을 읽을 수 없습니다: {role_id}") from exc
    return _decode_harness(data, f"기본 역할 안내 ({role_id})")


def _decode_harness(data: bytes, source: str) -> str:
    if len(data) > MAX_HARNESS_BYTES:
        raise HarnessError(f"{source}가 {MAX_HARNESS_BYTES}바이트 제한을 초과했습니다.")
    try:
        text = data.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise HarnessError(f"{source}는 올바른 UTF-8 파일이 아닙니다.") from exc
    if not text.strip() or "\x00" in text:
        raise HarnessError(f"{source}가 비어 있거나 텍스트 형식이 아닙니다.")
    return text
