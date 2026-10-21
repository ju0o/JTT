"""JTT-only launcher identification and explicit local launcher install."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import shutil
import stat
import subprocess
import sys
import uuid

from . import update_manager as tower_updates


DEV_MARKER = "# JTT_DEV_LAUNCHER_V1"
OFFICIAL_MARKER = "# JTT_OFFICIAL_LAUNCHER_V1"


class JTTInstallError(RuntimeError):
    """A launcher installation or identity check failed safely."""


def _inside(path: Path, parent: Path) -> bool:
    try:
        return os.path.commonpath((str(path.resolve()), str(parent.resolve()))) == str(parent.resolve())
    except (OSError, ValueError):
        return False


def source_checkout_root(source: Path | None = None) -> Path | None:
    module = Path(source or __file__).resolve()
    root = module.parents[2]
    if (root / "pyproject.toml").is_file() and os.path.lexists(root / ".git"):
        return root
    return None


def _launcher_path(argv0: str | None = None) -> Path | None:
    selected = argv0 or os.environ.get("JTT_LAUNCHER_PATH") or sys.argv[0]
    if not selected:
        return None
    path = Path(selected).expanduser()
    if not path.is_absolute():
        found = shutil.which(selected)
        if not found:
            return None
        path = Path(found)
    return Path(os.path.abspath(os.fspath(path)))


def inspect_official_launcher(argv0: str | None = None, *, source: Path | None = None) -> dict:
    """Verify the invoking JTT launcher is the private, versioned official shim."""

    launcher = _launcher_path(argv0)
    if launcher is None:
        return {"managed": False, "reason": "JTT launcher path is unavailable."}
    path_command = shutil.which("jtt")
    if path_command and os.path.realpath(path_command) != os.path.realpath(launcher):
        return {"managed": False, "reason": "The running JTT launcher differs from the PATH jtt command."}
    try:
        info_before = launcher.lstat()
        launcher_fd = os.open(launcher, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            opened = os.fstat(launcher_fd)
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_uid != os.getuid()
                or opened.st_nlink != 1
                or opened.st_size > 1024 * 1024
                or (opened.st_dev, opened.st_ino) != (info_before.st_dev, info_before.st_ino)
            ):
                raise ValueError
            with os.fdopen(launcher_fd, "r", encoding="utf-8") as stream:
                launcher_fd = -1
                content = stream.read(1024 * 1024 + 1)
                if len(content) > 1024 * 1024:
                    raise ValueError
                info_after = os.fstat(stream.fileno())
        finally:
            if launcher_fd >= 0:
                os.close(launcher_fd)
        path_after = launcher.lstat()
        parent = launcher.parent.lstat()
    except (OSError, UnicodeError):
        return {"managed": False, "reason": "The invoking JTT launcher cannot be safely inspected."}
    except ValueError:
        return {"managed": False, "reason": "The invoking JTT launcher is not a private regular file."}
    stat_identity = lambda info: (
        info.st_dev, info.st_ino, info.st_uid, info.st_mode, info.st_nlink,
        info.st_size, info.st_mtime_ns, info.st_ctime_ns,
    )
    if (
        not stat.S_ISREG(info_before.st_mode)
        or stat.S_ISLNK(info_before.st_mode)
        or info_before.st_uid != os.getuid()
        or info_before.st_nlink != 1
        or info_before.st_mode & 0o022
        or stat_identity(info_before) != stat_identity(opened)
        or stat_identity(info_before) != stat_identity(info_after)
        or stat_identity(info_before) != stat_identity(path_after)
        or not stat.S_ISDIR(parent.st_mode)
        or parent.st_uid != os.getuid()
        or parent.st_mode & 0o022
        or OFFICIAL_MARKER not in content
    ):
        return {"managed": False, "reason": "The invoking launcher is not a private JTT-managed Release launcher."}
    site_line = next((line for line in content.splitlines() if line.startswith("# JTT_RELEASE_SITE=")), "")
    version_line = next((line for line in content.splitlines() if line.startswith("# JTT_RELEASE_VERSION=")), "")
    sha_line = next((line for line in content.splitlines() if line.startswith("# JTT_RELEASE_SHA=")), "")
    python_line = next((line for line in content.splitlines() if line.startswith("# JTT_PYTHON=")), "")
    if not (site_line and version_line and sha_line and python_line):
        return {"managed": False, "reason": "JTT Release launcher metadata is incomplete."}
    site_dir = Path(site_line.partition("=")[2])
    version = version_line.partition("=")[2]
    commit_sha = sha_line.partition("=")[2]
    python = python_line.partition("=")[2]
    try:
        parsed = tower_updates.parse_version(version)
        marker = site_dir.parent / "release.json"
        marker_info = marker.lstat()
        if (
            not stat.S_ISREG(marker_info.st_mode)
            or marker_info.st_uid != os.getuid()
            or marker_info.st_mode & 0o077
            or marker_info.st_nlink != 1
        ):
            raise ValueError
        marker_fd = os.open(marker, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            marker_open = os.fstat(marker_fd)
            if (
                not stat.S_ISREG(marker_open.st_mode)
                or marker_open.st_uid != os.getuid()
                or marker_open.st_mode & 0o077
                or marker_open.st_nlink != 1
                or marker_open.st_size > 1024 * 1024
                or (marker_open.st_dev, marker_open.st_ino) != (marker_info.st_dev, marker_info.st_ino)
            ):
                raise ValueError
            with os.fdopen(marker_fd, "r", encoding="utf-8") as stream:
                marker_fd = -1
                record = json.load(stream)
        finally:
            if marker_fd >= 0:
                os.close(marker_fd)
        if not isinstance(record, dict):
            raise ValueError
        source_path = Path(source or __file__).resolve()
        if (
            record.get("product") != "jtt"
            or record.get("version") != parsed.package
            or record.get("commit_sha") != commit_sha
            or record.get("python") != python
            or record.get("site") != str(site_dir)
            or not tower_updates._SHA_RE.fullmatch(commit_sha)
            or not _inside(source_path, site_dir)
        ):
            raise ValueError
    except (OSError, ValueError, KeyError, TypeError, tower_updates.UpdateError):
        return {"managed": False, "reason": "JTT Release metadata does not match the imported package."}
    return {
        "managed": True,
        "kind": "official",
        "launcher": launcher,
        "python": python,
        "site": site_dir,
        "version": version,
        "commit_sha": commit_sha,
        "reason": "",
    }


def installation_kind() -> str:
    """Return DEV, OFFICIAL, or UNMANAGED without touching persistent state."""

    root = source_checkout_root()
    configured_root = os.environ.get("JTT_DEV_ROOT", "")
    if root is not None and (not configured_root or _inside(Path(__file__).resolve(), Path(configured_root))):
        return "DEV"
    if os.environ.get("JTT_INSTALL_KIND") == "official":
        runtime_site = os.environ.get("JTT_RELEASE_SITE", "")
        if runtime_site and _inside(Path(__file__).resolve(), Path(runtime_site)):
            return "OFFICIAL"
        if inspect_official_launcher().get("managed"):
            return "OFFICIAL"
    if inspect_official_launcher().get("managed"):
        return "OFFICIAL"
    return "UNMANAGED"


def _private_file(path: Path) -> str | None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise JTTInstallError("Cannot inspect the existing jtt command safely.") from exc
    if (
        not stat.S_ISREG(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_nlink != 1
        or info.st_mode & 0o022
    ):
        raise JTTInstallError("Refusing to replace an unmanaged or unsafe jtt command.")
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise JTTInstallError("Refusing to replace an unreadable jtt command.") from exc


def _dev_launcher_text(root: Path, python: Path, launcher: Path) -> str:
    if any(ord(char) < 32 for char in (str(root) + str(launcher))):
        raise JTTInstallError("The development checkout path contains unsupported control characters.")
    return (
        "#!/bin/sh\n"
        f"{DEV_MARKER}\n"
        f"# JTT_DEV_ROOT={root}\n"
        "unset PYTHONPATH\n"
        "export PYTHONNOUSERSITE=1\n"
        "export JTT_INSTALL_KIND=dev\n"
        f"export JTT_DEV_ROOT={shlex.quote(str(root))}\n"
        f"export JTT_LAUNCHER_PATH={shlex.quote(str(launcher))}\n"
        f"export PYTHONPATH={shlex.quote(str(root / 'src'))}\n"
        f"exec {shlex.quote(str(python))} -m tmux_agent_tower.jtt \"$@\"\n"
    )


def install_dev_launcher(
    source_root: Path,
    python: Path,
    *,
    home: Path | None = None,
    path_lookup=shutil.which,
    verify=None,
) -> dict:
    """Explicitly install a checkout-pinned dev launcher without shell edits."""

    root = Path(source_root).resolve()
    python_path = Path(os.path.abspath(os.fspath(python)))
    expected_python = Path(os.path.abspath(os.fspath(root / ".venv-jtt" / "bin" / "python")))
    if (
        not (root / "pyproject.toml").is_file()
        or not (root / "src" / "tmux_agent_tower" / "jtt.py").is_file()
        or python_path != expected_python
        or not python_path.is_file()
        or not os.access(python_path, os.X_OK)
    ):
        raise JTTInstallError("Use the JTT development checkout's verified .venv-jtt/bin/python.")
    user_home = Path(home or Path.home()).expanduser().absolute()
    target = user_home / ".local" / "bin" / "jtt"
    found = path_lookup("jtt")
    if found and os.path.realpath(found) != os.path.realpath(target):
        raise JTTInstallError("Another jtt command is already on PATH; it was left unchanged.")
    try:
        tower_updates._ensure_directory_chain(target.parent)
        tower_updates._directory_chain(target.parent)
        parent = target.parent.lstat()
    except (OSError, tower_updates.UpdateError) as exc:
        raise JTTInstallError("The user jtt bin directory is not a safe owned path.") from exc
    if not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid() or parent.st_mode & 0o022:
        raise JTTInstallError("The user jtt bin directory has unsafe ownership or permissions.")

    existing = _private_file(target)
    content = _dev_launcher_text(root, python_path, target)
    if existing is not None:
        if existing == content:
            return {"path": target, "installed": False, "path_ready": str(target.parent) in os.environ.get("PATH", "").split(os.pathsep)}
        raise JTTInstallError("An existing jtt launcher is not this checkout's managed dev launcher; it was left unchanged.")

    temporary = target.parent / (".jtt-dev-" + uuid.uuid4().hex + ".tmp")
    descriptor = None
    created_identity = None
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o700)
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            descriptor = None
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary, 0o755)
        temporary_info = temporary.lstat()
        # link() is a no-clobber publication: a racing or newly appeared command
        # is never overwritten by this installer.
        if path_lookup("jtt"):
            raise JTTInstallError("A jtt command appeared on PATH during install; nothing was overwritten.")
        os.link(temporary, target)
        created_identity = target.lstat()
        if (created_identity.st_dev, created_identity.st_ino) != (temporary_info.st_dev, temporary_info.st_ino):
            raise JTTInstallError("The installed JTT launcher identity changed during verification; no other file was removed.")
        if verify is None:
            env = {
                **os.environ,
                "JTT_INSTALL_KIND": "dev",
                "JTT_DEV_ROOT": str(root),
                "PYTHONNOUSERSITE": "1",
                "PYTHONPATH": str(root / "src"),
            }
            result = subprocess.run([str(target), "--version"], capture_output=True, text=True, timeout=20, env=env, check=False)
            valid = result.returncode == 0 and result.stdout.strip() == f"jtt {__import__('tmux_agent_tower').__version__} [DEV]"
        else:
            valid = bool(verify(target))
        if not valid:
            info = target.lstat()
            if (
                stat.S_ISREG(info.st_mode)
                and info.st_nlink in (1, 2)
                and (info.st_dev, info.st_ino) == (created_identity.st_dev, created_identity.st_ino)
            ):
                target.unlink()
            created_identity = None
            raise JTTInstallError("The new development launcher failed its version check and was removed.")
        directory_fd = os.open(target.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        final_info = target.lstat()
        if (final_info.st_dev, final_info.st_ino) != (created_identity.st_dev, created_identity.st_ino):
            created_identity = None
            raise JTTInstallError("The JTT launcher changed during installation; no other file was removed.")
    except FileExistsError as exc:
        raise JTTInstallError("A jtt command appeared during install; it was left unchanged.") from exc
    except JTTInstallError:
        if created_identity is not None:
            try:
                info = target.lstat()
                if (
                    stat.S_ISREG(info.st_mode)
                    and info.st_nlink in (1, 2)
                    and (info.st_dev, info.st_ino) == (created_identity.st_dev, created_identity.st_ino)
                ):
                    target.unlink()
            except OSError:
                pass
        raise
    except (OSError, subprocess.SubprocessError) as exc:
        if created_identity is not None:
            try:
                info = target.lstat()
                if (
                    stat.S_ISREG(info.st_mode)
                    and info.st_nlink in (1, 2)
                    and (info.st_dev, info.st_ino) == (created_identity.st_dev, created_identity.st_ino)
                ):
                    target.unlink()
            except OSError:
                pass
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise JTTInstallError("Could not safely install the JTT dev launcher.") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return {"path": target, "installed": True, "path_ready": str(target.parent) in os.environ.get("PATH", "").split(os.pathsep)}


def main(argv=None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="jtt-install-dev")
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--python", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        result = install_dev_launcher(args.source, args.python)
    except JTTInstallError as exc:
        print(f"JTT DEV install refused: {exc}", file=sys.stderr)
        return 2
    print(f"JTT DEV launcher: {result['path']}")
    if result["installed"]:
        print("Installed the checkout-pinned development command; no shell startup file was changed.")
    else:
        print("This checkout's managed development launcher is already installed.")
    if not result["path_ready"]:
        print('For this shell only, add it with: export PATH="$HOME/.local/bin:$PATH"')
        print("To make that persistent, review and edit your shell profile yourself; JTT did not change it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
