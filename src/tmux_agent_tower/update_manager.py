"""Explicit, fail-closed update checks and transactional CLI launcher updates."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import site
import stat
import subprocess
import sys
import tempfile
import urllib.parse
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from .paths import APP_HOME_ISOLATED, DATA_DIR
from .release_support import (
    Release,
    ReleaseAPI,
    UpdateError,
    UpdateStatus,
    Version,
    _SHA_RE,
    checkout_dirty as _checkout_dirty,
    default_channel,
    directory_chain as _directory_chain,
    ensure_directory_chain as _ensure_directory_chain,
    git_value as _git_value,
    inside as _inside,
    install_archive as _install_archive,
    parse_version,
)

REPOSITORY = "ju0o/tmux-agent-tower"
_MARKER = "# TOWER_UPDATE_MANAGER_V1"


_TOWER_RELEASES = ReleaseAPI(
    REPOSITORY,
    user_agent="Tmux-Agent-Tower-Update-Manager",
)
API_ROOT = f"https://api.github.com/repos/{REPOSITORY}"
_api_json = _TOWER_RELEASES._api_json
_tag_commit = _TOWER_RELEASES._tag_commit
_download_archive = _TOWER_RELEASES._download_archive


@dataclass(frozen=True)
class InstallInfo:
    managed: bool
    kind: str
    reason: str
    launcher: Optional[Path]
    python: str
    module_source: str
    version: str
    source_root: Optional[Path] = None


def _parse_release_item(item: dict) -> Optional[Release]:
    if not isinstance(item, dict):
        raise UpdateError("INVALID_RELEASE_METADATA", "GitHub returned a non-object release entry.")
    if item.get("draft") is True:
        return None
    if item.get("draft") is not False:
        raise UpdateError("INVALID_RELEASE_METADATA", "GitHub release is missing a valid draft flag.")
    tag = item.get("tag_name")
    if not isinstance(tag, str):
        raise UpdateError("INVALID_RELEASE_METADATA", "GitHub release has no valid tag_name.")
    try:
        version = parse_version(tag, tag=True)
    except UpdateError:
        if tag[:1].lower() in ("v", "0", "1", "2", "3", "4", "5", "6", "7", "8", "9"):
            raise UpdateError("INVALID_RELEASE_TAG", f"Unsupported published release tag: {tag!r}")
        return None
    prerelease = item.get("prerelease")
    if not isinstance(prerelease, bool) or version.prerelease and not prerelease:
        raise UpdateError("INVALID_RELEASE_METADATA", f"Release prerelease flag conflicts with tag {tag}.")
    if not isinstance(item.get("published_at"), str) or not item["published_at"]:
        raise UpdateError("INVALID_RELEASE_METADATA", f"Release {tag} is not published.")
    return Release(tag, version.package, prerelease, version.key)


def select_release(releases, channel: str) -> Release:
    if channel not in ("stable", "rc"):
        raise UpdateError("INVALID_CHANNEL", f"Unsupported release channel: {channel}")
    candidates = []
    for item in releases:
        release = _parse_release_item(item)
        if release and (release.prerelease == (channel == "rc")):
            candidates.append(release)
    if not candidates:
        raise UpdateError("NO_RELEASE", f"No published {channel} Release is available.")
    newest_key = max(release.key for release in candidates)
    newest = [release for release in candidates if release.key == newest_key]
    if len({release.tag for release in newest}) != 1:
        raise UpdateError("AMBIGUOUS_RELEASE", "Multiple Releases have the same version.")
    return newest[0]


def _installed_channel(current_version: str) -> str:
    # A stable-shaped installed version must not move to an RC based on mutable Release metadata.
    return default_channel(current_version)


def check_for_updates(current_version: str, requested_channel: str = "auto", fetch_json: Callable = _api_json) -> UpdateStatus:
    current = parse_version(current_version)
    if requested_channel not in ("auto", "stable", "rc"):
        raise UpdateError("INVALID_CHANNEL", f"Unsupported release channel: {requested_channel}")
    records = fetch_json("/releases?per_page=100")
    if not isinstance(records, list):
        raise UpdateError("INVALID_GITHUB_RESPONSE", "GitHub Releases response is not a list.")
    if requested_channel == "auto":
        channel = _installed_channel(current_version)
    else:
        channel = requested_channel
    latest = select_release(records, channel)
    commit_sha = _tag_commit(latest.tag, fetch_json)
    latest = Release(latest.tag, latest.package_version, latest.prerelease, latest.key, commit_sha)
    return UpdateStatus(current, channel, latest, latest.key > current.key, tuple(records))


def _read_shebang(path: Path) -> str:
    try:
        line = path.read_text(encoding="utf-8", errors="replace").splitlines()[0]
    except (OSError, IndexError):
        return ""
    if not line.startswith("#!"):
        return ""
    parts = line[2:].strip().split()
    if not parts:
        return ""
    if Path(parts[0]).name == "env":
        args = [part for part in parts[1:] if part != "-S"]
        if not args:
            return ""
        return shutil.which(args[0]) or ""
    candidate = parts[0]
    return candidate if Path(candidate).is_file() else ""


def _marker_site(path: Path) -> Optional[Path]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    if _MARKER not in text:
        return None
    match = re.search(r"^# TOWER_UPDATE_SITE=(.+)$", text, re.M)
    if not match:
        return None
    return Path(match.group(1)).expanduser()


def inspect_path_tower(executable: Optional[str] = None, runner=subprocess.run) -> dict:
    executable = executable or shutil.which("tower")
    if not executable:
        return {"found": False, "executable": "", "python": "", "version": "", "source": "", "direct_url": ""}
    path = Path(os.path.abspath(os.path.expanduser(executable)))
    python = _read_shebang(path)
    if not python:
        return {"found": True, "executable": str(path), "python": "", "version": "", "source": "", "direct_url": ""}
    site_path = _marker_site(path)
    env = os.environ.copy()
    env.pop("TOWER_DEV_ROOT", None)
    env.pop("PYTHONPATH", None)
    if site_path:
        env["PYTHONPATH"] = str(site_path)
    code = (
        "import importlib.metadata as m, json, os, site, sys, tmux_agent_tower as p; "
        "d=m.distribution('tmux-agent-tower'); "
        "print(json.dumps({'version':p.__version__,'source':os.path.realpath(p.__file__),"
        "'direct_url':d.read_text('direct_url.json') or '', 'prefix':sys.prefix,"
        "'base_prefix':sys.base_prefix,'user_base':site.USER_BASE}))"
    )
    try:
        completed = runner(
            [python, "-c", code],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
            env=env,
        )
    except (OSError, subprocess.SubprocessError):
        completed = None
    info = {}
    if completed and completed.returncode == 0:
        try:
            info = json.loads((completed.stdout or "").strip().splitlines()[-1])
        except (ValueError, IndexError):
            pass
    return {
        "found": True,
        "executable": str(path),
        "python": python,
        "version": str(info.get("version", "")),
        "source": str(info.get("source", "")),
        "direct_url": str(info.get("direct_url", "")),
        "prefix": str(info.get("prefix", "")),
        "base_prefix": str(info.get("base_prefix", "")),
        "user_base": str(info.get("user_base", "")),
        "managed_site": str(site_path or ""),
    }


def _looks_like_tower_script(path: Path) -> bool:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return text.startswith("#!") and "tmux_agent_tower.main" in text and "cli" in text


def _official_checkout(root: Path) -> bool:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    if result.returncode != 0:
        return False
    remote = (result.stdout or "").strip()
    if re.fullmatch(r"(?:git@)?github\.com:ju0o/tmux-agent-tower(?:\.git)?", remote, re.I):
        return True
    parsed = urllib.parse.urlparse(remote)
    if (
        parsed.hostname != "github.com"
        or parsed.path.rstrip("/").removesuffix(".git").lower() != "/ju0o/tmux-agent-tower"
        or parsed.query
        or parsed.fragment
    ):
        return False
    if parsed.scheme == "https":
        return not parsed.username and not parsed.password
    return parsed.scheme == "ssh" and parsed.username in (None, "git") and not parsed.password


def classify_installation(path_info: dict, *, invoked_from_path: bool = True) -> InstallInfo:
    executable = Path(path_info["executable"]) if path_info.get("executable") else None
    version = str(path_info.get("version", ""))
    python = str(path_info.get("python", ""))
    source = str(path_info.get("source", ""))
    if not executable or not executable.exists() or not source or not version:
        return InstallInfo(False, "unknown", "PATH tower could not be inspected.", executable, python, source, version)
    if not invoked_from_path:
        return InstallInfo(False, "unmanaged", "Run the update through the same tower command that is on PATH.", executable, python, source, version)
    try:
        mode = executable.lstat().st_mode
    except OSError:
        return InstallInfo(False, "unmanaged", "The PATH launcher cannot be inspected.", executable, python, source, version)
    if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
        return InstallInfo(False, "unmanaged", "The PATH launcher is a symlink or non-regular file.", executable, python, source, version)
    if not hasattr(os, "getuid") or executable.stat().st_uid != os.getuid():
        return InstallInfo(False, "unmanaged", "The PATH launcher is not owned by this user.", executable, python, source, version)
    try:
        parent_info = executable.parent.lstat()
    except OSError:
        parent_info = None
    if (
        parent_info is None
        or not stat.S_ISDIR(parent_info.st_mode)
        or parent_info.st_uid != os.getuid()
        or parent_info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        or mode & (stat.S_IWGRP | stat.S_IWOTH)
        or not os.access(executable.parent, os.W_OK)
        or not os.access(executable, os.W_OK)
    ):
        return InstallInfo(False, "unmanaged", "The PATH launcher or directory has unsafe ownership or permissions; sudo is not used.", executable, python, source, version)
    try:
        _directory_chain(executable.parent)
    except UpdateError as exc:
        return InstallInfo(False, "unmanaged", str(exc), executable, python, source, version)

    managed_site = path_info.get("managed_site")
    if managed_site and _looks_like_tower_script(executable):
        site_path = Path(managed_site)
        marker_path = site_path.parent / "release.json"
        try:
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            marker = {}
        if not isinstance(marker, dict):
            marker = {}
        if (
            _inside(Path(source), site_path)
            and marker.get("version") == version
            and (
                marker.get("launcher_path") == str(executable)
                or _launcher_registered(site_path.parent, executable, marker)
            )
            and _SHA_RE.fullmatch(str(marker.get("commit_sha", "")))
        ):
            return InstallInfo(True, "update-manager", "", executable, python, source, version, site_path.parent)

    if not _looks_like_tower_script(executable):
        return InstallInfo(False, "unmanaged", "The PATH entry is not a standard Tower Python console script.", executable, python, source, version)
    try:
        metadata = json.loads(path_info.get("direct_url", "") or "{}")
        editable = metadata.get("dir_info", {}).get("editable") is True
        parsed = urllib.parse.urlparse(metadata.get("url", ""))
        if not editable or parsed.scheme != "file":
            raise ValueError
        source_root = Path(urllib.parse.unquote(parsed.path)).resolve()
        module_path = Path(source).resolve()
        if not _inside(module_path, source_root):
            raise ValueError
    except (ValueError, TypeError, json.JSONDecodeError):
        return InstallInfo(False, "unmanaged", "Only a verified editable install or updater-managed install can be changed automatically.", executable, python, source, version)

    dirty = _checkout_dirty(source_root)
    if dirty is None:
        return InstallInfo(False, "unmanaged", "Developer checkout cleanliness could not be verified.", executable, python, source, version, source_root)
    if dirty:
        return InstallInfo(False, "dirty-developer-checkout", "The editable source checkout is dirty; no files will be changed.", executable, python, source, version, source_root)
    if not _official_checkout(source_root):
        return InstallInfo(False, "unmanaged", "The editable source is not the official Tower repository.", executable, python, source, version, source_root)

    user_base = Path(path_info.get("user_base", "") or site.USER_BASE).resolve()
    user_bin = user_base / ("Scripts" if os.name == "nt" else "bin")
    prefix = Path(path_info.get("prefix", sys.prefix)).resolve()
    base_prefix = Path(path_info.get("base_prefix", sys.base_prefix)).resolve()
    if executable.parent.resolve() == user_bin.resolve():
        kind = "editable-user"
    elif prefix != base_prefix and executable.parent.resolve() == (prefix / ("Scripts" if os.name == "nt" else "bin")).resolve() and (source_root / ".venv").resolve() == prefix:
        kind = "editable-venv"
    else:
        return InstallInfo(False, "unmanaged", "Editable install does not use the supported user or installer-fallback venv launcher.", executable, python, source, version, source_root)
    return InstallInfo(True, kind, "", executable, python, source, version, source_root)


def _launcher_registration_path(release_dir: Path, launcher: Path) -> Path:
    identity = os.path.abspath(str(launcher))
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return release_dir / "launchers" / f"{digest}.json"


def _launcher_registered(release_dir: Path, launcher: Path, release: dict) -> bool:
    identity = os.path.abspath(str(launcher))
    directory = release_dir / "launchers"
    record = _launcher_registration_path(release_dir, launcher)
    try:
        directory_info = directory.lstat()
        record_info = record.lstat()
        if (
            not stat.S_ISDIR(directory_info.st_mode)
            or directory_info.st_uid != os.getuid()
            or directory_info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
            or not stat.S_ISREG(record_info.st_mode)
            or record_info.st_uid != os.getuid()
            or record_info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        ):
            return False
        data = json.loads(record.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return False
    if not isinstance(data, dict):
        return False
    return (
        data.get("schema_version") == 1
        and data.get("launcher_path") == identity
        and data.get("version") == release.get("version")
        and data.get("commit_sha") == release.get("commit_sha")
    )


def _register_launcher(release_dir: Path, launcher: Path, release: Release) -> None:
    directory = release_dir / "launchers"
    directory.mkdir(mode=0o700, exist_ok=True)
    directory_info = directory.lstat()
    if (
        not stat.S_ISDIR(directory_info.st_mode)
        or directory_info.st_uid != os.getuid()
        or directory_info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
    ):
        raise UpdateError("UNSAFE_UPDATE_DIRECTORY", "The release launcher registry is not a private user-owned directory.")
    target = _launcher_registration_path(release_dir, launcher)
    identity = os.path.abspath(str(launcher))
    payload = {
        "schema_version": 1,
        "launcher_path": identity,
        "version": release.package_version,
        "commit_sha": release.commit_sha,
    }
    fd, temporary_name = tempfile.mkstemp(prefix=".launcher-", dir=directory)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            output.write(json.dumps(payload, sort_keys=True) + "\n")
            output.flush()
            os.fsync(output.fileno())
        try:
            os.link(temporary, target)
        except FileExistsError:
            if not _launcher_registered(release_dir, launcher, payload):
                raise UpdateError("LAUNCHER_IDENTITY_COLLISION", "An existing launcher registration does not match this path and Release.")
        directory_fd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except UpdateError:
        raise
    except OSError as exc:
        raise UpdateError("LAUNCHER_REGISTRATION", "Could not safely register this launcher for the verified Release.") from exc
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def data_root() -> Path:
    if APP_HOME_ISOLATED:
        return DATA_DIR
    xdg = os.environ.get("XDG_DATA_HOME", "")
    base = Path(xdg).expanduser() if xdg and Path(xdg).expanduser().is_absolute() else Path.home() / ".local" / "share"
    return base / "tmux-agent-tower"


def _launcher_snapshot(launcher: Path) -> tuple:
    try:
        directories = _directory_chain(launcher.parent)
        before = launcher.lstat()
        contents = launcher.read_bytes()
        after = launcher.lstat()
    except OSError as exc:
        raise UpdateError("UNSAFE_LAUNCHER", "The PATH launcher changed or could not be inspected.") from exc
    identity = lambda info: (
        info.st_dev,
        info.st_ino,
        info.st_uid,
        info.st_mode,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_uid != os.getuid()
        or identity(before) != identity(after)
        or len(contents) != before.st_size
    ):
        raise UpdateError("UNSAFE_LAUNCHER", "The PATH launcher or its directory has unsafe ownership or permissions.")
    return directories, identity(before), contents, stat.S_IMODE(before.st_mode)


def managed_releases(root: Optional[Path] = None) -> list:
    versions = (root or data_root()) / "versions"
    result = []
    if not versions.is_dir():
        return result
    for child in versions.iterdir():
        marker = child / "release.json"
        try:
            data = json.loads(marker.read_text(encoding="utf-8"))
            parsed = parse_version(data["version"])
            commit = data["commit_sha"]
            if not _SHA_RE.fullmatch(commit):
                continue
            data["_key"] = parsed.key
            result.append(data)
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError, UpdateError):
            continue
    return sorted(result, key=lambda item: item["_key"], reverse=True)


def newer_managed_release(version: str, root: Optional[Path] = None) -> Optional[dict]:
    current = parse_version(version)
    for release in managed_releases(root):
        if release["_key"] > current.key:
            return release
    return None


def _verify_package(site_dir: Path, python: str, release: Release) -> None:
    code = (
        "import importlib.metadata as m, json, os, tmux_agent_tower as p; "
        "print(json.dumps({'version':p.__version__,'metadata':m.version('tmux-agent-tower'),"
        "'source':os.path.realpath(p.__file__)}))"
    )
    env = os.environ.copy()
    env.pop("TOWER_DEV_ROOT", None)
    env["PYTHONPATH"] = str(site_dir)
    result = subprocess.run(
        [python, "-c", code],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
        env=env,
    )
    if result.returncode != 0:
        raise UpdateError("VERIFY_FAILED", "The staged package could not be imported.")
    try:
        facts = json.loads((result.stdout or "").strip().splitlines()[-1])
    except (ValueError, IndexError) as exc:
        raise UpdateError("VERIFY_FAILED", "The staged package returned invalid diagnostics.") from exc
    if facts.get("version") != release.package_version or facts.get("metadata") != release.package_version:
        raise UpdateError("VERSION_MISMATCH", "The source package version does not match the published Release tag.")
    if not _inside(Path(facts.get("source", "/")), site_dir):
        raise UpdateError("VERIFY_FAILED", "The staged package imported from outside the isolated version directory.")


def _launcher_text(python: str, site_dir: Path, version: str) -> str:
    return (
        f"#!{python}\n"
        f"{_MARKER}\n"
        f"# TOWER_UPDATE_SITE={site_dir}\n"
        f"# TOWER_UPDATE_VERSION={version}\n"
        "import os\n"
        "import sys\n"
        "os.environ.pop('TOWER_DEV_ROOT', None)\n"
        f"sys.path.insert(0, {str(site_dir)!r})\n"
        "from tmux_agent_tower.main import cli\n"
        "if __name__ == '__main__':\n"
        "    cli()\n"
    )


def _run_version(executable: Path, python: str, version: str) -> bool:
    env = os.environ.copy()
    env.pop("TOWER_DEV_ROOT", None)
    try:
        result = subprocess.run(
            [str(executable), "--version"],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
            env=env,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0 and result.stdout.strip() == f"tower {version}"


def apply_update(
    release: Release,
    installation: InstallInfo,
    *,
    root: Optional[Path] = None,
    download_archive: Callable = _download_archive,
    install_archive: Callable = _install_archive,
    verify_package: Callable = _verify_package,
    run_version: Callable = _run_version,
) -> dict:
    tag_version = parse_version(release.tag, tag=True)
    package_version = parse_version(release.package_version)
    if (
        tag_version.package != package_version.package
        or tag_version.key != release.key
        or tag_version.prerelease and not release.prerelease
        or not _SHA_RE.fullmatch(release.commit_sha)
    ):
        raise UpdateError("UNVERIFIED_RELEASE", "Release tag, version, channel, and commit do not agree.")
    if not installation.managed or not installation.launcher:
        raise UpdateError("UNMANAGED_INSTALL", installation.reason or "This installation cannot be changed automatically.")
    if os.name != "posix" or not hasattr(os, "geteuid") or os.geteuid() == 0:
        raise UpdateError("UNSUPPORTED_PRIVILEGE", "Run Tower as a normal user; updater never uses sudo.")
    launcher = installation.launcher
    original_launcher = _launcher_snapshot(launcher)
    store = root or data_root()
    _ensure_directory_chain(store.parent)
    versions = store / "versions"
    for directory in (store, versions):
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise UpdateError("UNSAFE_UPDATE_DIRECTORY", "The updater data directory is not a user-owned directory.")
    final_name = f"{release.package_version}-{release.commit_sha[:12]}"
    final_dir = versions / final_name
    site_dir = final_dir / "site"
    if final_dir.exists():
        try:
            dir_info = final_dir.lstat()
            if not stat.S_ISDIR(dir_info.st_mode) or dir_info.st_uid != os.getuid() or dir_info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
                raise UpdateError("UNSAFE_UPDATE_DIRECTORY", "The version directory is not user-owned.")
            site_info = site_dir.lstat()
            if not stat.S_ISDIR(site_info.st_mode) or site_info.st_uid != os.getuid():
                raise UpdateError("UNSAFE_UPDATE_DIRECTORY", "The staged package directory is not user-owned.")
            existing = json.loads((final_dir / "release.json").read_text(encoding="utf-8"))
            if (
                existing.get("tag") != release.tag
                or existing.get("version") != release.package_version
                or existing.get("commit_sha") != release.commit_sha
            ):
                raise UpdateError("VERSION_PATH_COLLISION", "An unverified directory already uses the release path.")
            verify_package(site_dir, installation.python, release)
        except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
            raise UpdateError("VERSION_PATH_COLLISION", "The release directory exists but could not be verified.") from exc
    else:
        stage = Path(tempfile.mkdtemp(prefix=".staging-", dir=versions))
        stage_site = stage / "site"
        archive = stage / "source.tar.gz"
        try:
            download_archive(release.commit_sha, archive)
            install_archive(archive, stage_site, installation.python)
            verify_package(stage_site, installation.python, release)
            archive.unlink()
            (stage / "release.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "tag": release.tag,
                        "version": release.package_version,
                        "commit_sha": release.commit_sha,
                        "launcher_path": str(installation.launcher),
                        "installed_at": datetime.now(timezone.utc).isoformat(),
                    },
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            os.replace(stage, final_dir)
            site_dir = final_dir / "site"
        except BaseException:
            shutil.rmtree(stage, ignore_errors=True)
            raise

    _register_launcher(final_dir, launcher, release)

    store_backups = store / "backups"
    store_backups.mkdir(mode=0o700, parents=True, exist_ok=True)
    backup_info = store_backups.lstat()
    if (
        not stat.S_ISDIR(backup_info.st_mode)
        or backup_info.st_uid != os.getuid()
        or backup_info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
    ):
        raise UpdateError("UNSAFE_UPDATE_DIRECTORY", "The updater backup directory is not a user-owned directory.")
    backup = store_backups / f"tower-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}-{uuid.uuid4().hex[:8]}"
    backup.write_bytes(original_launcher[2])
    os.chmod(backup, original_launcher[3])
    candidate = launcher.parent / f".tower-update-{uuid.uuid4().hex}.tmp"
    switched = False
    candidate_identity = None
    candidate_text = _launcher_text(installation.python, site_dir, release.package_version)
    try:
        with candidate.open("w", encoding="utf-8") as output:
            output.write(candidate_text)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(candidate, stat.S_IMODE(launcher.stat().st_mode))
        if not run_version(candidate, installation.python, release.package_version):
            raise UpdateError("VERIFY_FAILED", "The staged Tower launcher failed its version check; active launcher is unchanged.")
        current_snapshot = _launcher_snapshot(launcher)
        if current_snapshot[:3] != original_launcher[:3]:
            raise UpdateError("LAUNCHER_CHANGED", "The PATH launcher changed during the update; no replacement was made.")
        candidate_identity = _launcher_snapshot(candidate)[1][:2]
        switched = True
        os.replace(candidate, launcher)
        if not run_version(launcher, installation.python, release.package_version):
            raise UpdateError("VERIFY_FAILED", "The switched Tower launcher failed its version check.")
    except BaseException as exc:
        try:
            current = _launcher_snapshot(launcher)
            switched = switched and candidate_identity == current[1][:2] and current[2] == candidate_text.encode("utf-8")
        except UpdateError:
            switched = False
        if switched:
            rollback = launcher.parent / f".tower-rollback-{uuid.uuid4().hex}.tmp"
            try:
                rollback.write_bytes(original_launcher[2])
                os.chmod(rollback, original_launcher[3])
                os.replace(rollback, launcher)
            except OSError as rollback_error:
                raise UpdateError("ROLLBACK_FAILED", f"Could not restore the prior launcher. Backup preserved at {backup}.") from rollback_error
            if isinstance(exc, UpdateError):
                raise UpdateError("ROLLBACK", f"The new launcher failed validation; the previous launcher was restored from {backup}.") from exc
            if isinstance(exc, Exception):
                raise UpdateError("ROLLBACK", f"Update activation failed; the previous launcher was restored from {backup}.") from exc
            raise
        raise
    finally:
        try:
            candidate.unlink()
        except FileNotFoundError:
            pass
        if "rollback" in locals():
            try:
                rollback.unlink()
            except FileNotFoundError:
                pass
    return {"launcher": str(launcher), "site": str(site_dir), "backup": str(backup), "version": release.package_version}


def verify_bootstrap(root: Path, version: str, releases, fetch_json: Callable = _api_json) -> Release:
    if _checkout_dirty(root) is not False:
        raise UpdateError("BOOTSTRAP_DIRTY", "Use a fresh, clean checkout of an official published Release.")
    tag = _git_value(root, "describe", "--tags", "--exact-match", "HEAD")
    expected = parse_version(version)
    tag_version = parse_version(tag, tag=True)
    if expected.package != tag_version.package:
        raise UpdateError("BOOTSTRAP_VERSION_MISMATCH", "The bootstrap checkout version does not match its Git tag.")
    matching = []
    for item in releases:
        release = _parse_release_item(item)
        if release and release.tag == tag:
            matching.append(release)
    if len(matching) != 1:
        raise UpdateError("BOOTSTRAP_NOT_PUBLISHED", "The bootstrap Git tag is not a published official Release.")
    remote_commit = _tag_commit(tag, fetch_json)
    local_commit = _git_value(root, "rev-parse", "HEAD")
    if remote_commit != local_commit:
        raise UpdateError("BOOTSTRAP_COMMIT_MISMATCH", "The bootstrap checkout does not match the official Release tag.")
    published = matching[0]
    return Release(tag, published.package_version, published.prerelease, published.key, remote_commit)


def _print_runtime(facts: dict, path_info: dict, output) -> None:
    print(f"Current runtime version: {facts.get('version', 'unknown')}", file=output)
    print(f"Current module: {facts.get('source', 'unknown')}", file=output)
    print(f"Current Python: {facts.get('python', 'unknown')}", file=output)
    if path_info.get("found"):
        print(f"PATH tower: {path_info.get('executable') or 'unreadable'}", file=output)
        print(f"PATH version: {path_info.get('version') or 'unreadable'}", file=output)
        print(f"PATH module: {path_info.get('source') or 'unreadable'}", file=output)
    else:
        print("PATH tower: not found", file=output)


def run_cli(
    action: str,
    *,
    current_facts: Optional[dict] = None,
    requested_channel: str = "auto",
    dry_run: bool = False,
    bootstrap: bool = False,
    argv0: Optional[str] = None,
    fetch_json: Callable = _api_json,
    confirm: Optional[Callable] = None,
    output=None,
    error=None,
) -> int:
    output = output or sys.stdout
    error = error or sys.stderr
    if current_facts is None:
        from .main import runtime_facts, checkout_root

        current_facts = runtime_facts()
        source_root = Path(checkout_root(current_facts["source"]))
    else:
        source_root = Path(current_facts.get("checkout_root", Path.cwd()))
    path_command = shutil.which("tower")
    path_info = inspect_path_tower(path_command)
    invoked_path = bool(
        path_info.get("executable")
        and os.path.realpath(argv0 or sys.argv[0]) == os.path.realpath(path_info["executable"])
    )
    print(f"Invoked executable: {argv0 or sys.argv[0]}", file=output)
    _print_runtime(current_facts, path_info, output)
    path_runtime_matches = bool(
        invoked_path
        and path_info.get("source")
        and current_facts.get("source")
        and path_info.get("version") == current_facts.get("version")
        and os.path.realpath(path_info["source"]) == os.path.realpath(current_facts["source"])
    )
    if path_info.get("source") and current_facts.get("source") and os.path.realpath(path_info["source"]) != os.path.realpath(current_facts["source"]):
        print("WARNING: PATH tower imports a different package than this process.", file=output)
    sidecar = newer_managed_release(path_info.get("version") or current_facts.get("version", "0.0.0"))
    if sidecar:
        print(
            f"WARNING: newer managed version {sidecar.get('version')} exists; managed launcher: {sidecar.get('launcher_path', 'unknown')}",
            file=output,
        )

    if bootstrap:
        try:
            releases = fetch_json("/releases?per_page=100")
            if not isinstance(releases, list):
                raise UpdateError("INVALID_GITHUB_RESPONSE", "GitHub Releases response is not a list.")
            target = verify_bootstrap(source_root, current_facts.get("version", ""), releases, fetch_json)
            channel = (
                _installed_channel(path_info.get("version") or current_facts["version"])
                if requested_channel == "auto"
                else requested_channel
            )
            if channel not in ("stable", "rc") or target.prerelease != (channel == "rc"):
                raise UpdateError("CHANNEL_MISMATCH", "The migration checkout does not match the selected release channel.")
            if path_info.get("version"):
                installed = parse_version(path_info["version"])
                if target.key < installed.key:
                    raise UpdateError("DOWNGRADE_REFUSED", "The verified migration Release is older than the PATH installation.")
            status = UpdateStatus(parse_version(path_info.get("version") or current_facts["version"]), channel, target, True, tuple(releases))
        except UpdateError as exc:
            print(f"{exc.code}: {exc}", file=error)
            return 1
    else:
        try:
            status = check_for_updates(current_facts.get("version", ""), requested_channel, fetch_json)
        except UpdateError as exc:
            print(f"{exc.code}: {exc}", file=error)
            if exc.code == "OFFLINE":
                print("Update status: OFFLINE; Tower itself remains usable.", file=output)
                return 0
            return 1

    print(f"Release channel: {status.channel.upper()}", file=output)
    print(f"Latest official Release: {status.latest.tag} ({status.latest.commit_sha[:12]})", file=output)
    if not bootstrap and not status.available:
        print("Update status: UP_TO_DATE", file=output)
        return 0
    if bootstrap:
        print(f"Migration target: {status.latest.package_version}", file=output)
    elif status.latest.key < status.current.key:
        print(f"Explicit channel switch: {status.current.package} -> {status.latest.package_version}", file=output)
    else:
        print(f"Update available: {status.current.package} -> {status.latest.package_version}", file=output)

    if action == "check":
        return 0
    invoked = invoked_path
    if bootstrap:
        invoked = True
    installation = classify_installation(path_info, invoked_from_path=invoked)
    if not installation.managed:
        print(f"Automatic update: UNAVAILABLE ({installation.reason})", file=output)
        print("Use a fresh clean clone of a published Release and run scripts/migrate-update.sh for the one-time migration.", file=output)
        return 2
    if not path_runtime_matches and not bootstrap:
        print("Automatic update: REFUSED (the running module does not match the PATH launcher runtime).", file=output)
        return 2
    if current_facts.get("mismatch") and not bootstrap:
        print("Automatic update: REFUSED (this process is outside TOWER_DEV_ROOT).", file=output)
        return 2
    launcher = installation.launcher
    store = data_root()
    final_dir = store / "versions" / f"{status.latest.package_version}-{status.latest.commit_sha[:12]}"
    backup = store / "backups" / "<created-on-confirmation>"
    print("Planned changes:", file=output)
    print(f"  install verified release into: {final_dir}", file=output)
    print(f"  atomically switch launcher: {launcher}", file=output)
    print(f"  preserve prior launcher under: {backup}", file=output)
    print("  no config/cache/tmux session or running Tower process is changed.", file=output)
    if dry_run:
        print("Dry run: no files changed.", file=output)
        return 0
    if not sys.stdin.isatty():
        print("CONFIRMATION_REQUIRED: run interactively and type 'yes' to apply.", file=error)
        return 2
    confirm = confirm or input
    answer = confirm("Apply this update? Type 'yes' to continue: ")
    if answer.strip().lower() != "yes":
        print("Update cancelled; no files changed.", file=output)
        return 0
    try:
        result = apply_update(status.latest, installation, root=store)
    except UpdateError as exc:
        print(f"{exc.code}: {exc}", file=error)
        return 1
    except Exception as exc:
        print(f"UPDATE_FAILED: {type(exc).__name__}; the current launcher was not intentionally changed.", file=error)
        return 1
    print(f"Update installed: {result['version']}", file=output)
    print(f"Launcher: {result['launcher']}", file=output)
    print("Existing Tower processes were left running. Restart Tower manually to use the new version.", file=output)
    return 0
