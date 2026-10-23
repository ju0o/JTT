"""JTT-only safe Release installation, update, and rollback.

Tower's legacy ``tower`` updater is intentionally not used to classify or
switch the JTT command. Only published GitHub Releases whose tag resolves to
a verified commit are eligible. All package copies and launcher backups live
under the JTT-specific data root; JTT_HOME and running PTYs are not touched.
"""

from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import ctypes
import errno
from datetime import datetime, timezone
import uuid
from typing import Callable

from . import DISTRIBUTION_NAME, JTT_VERSION
from .release_support import ReleaseAPI
from .jtt_install import OFFICIAL_MARKER, installation_kind, inspect_official_launcher, source_checkout_root

JTT_RELEASE_NAMESPACE = "jtt-"
JTT_RELEASE_TAG_PREFIX = "jtt-v"
releases = ReleaseAPI("ju0o/JTT", user_agent="Ju0-Terminal-Tower-Update-Manager")


class JTTUpdateError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def data_root(environ=None, home: Path | None = None) -> Path:
    env = os.environ if environ is None else environ
    xdg = str(env.get("XDG_DATA_HOME") or "").strip()
    base = Path(xdg).expanduser() if xdg and Path(xdg).expanduser().is_absolute() else (Path.home() if home is None else Path(home)) / ".local" / "share"
    return base / "jtt"


def _ensure_private_directory(path: Path) -> None:
    try:
        releases._ensure_directory_chain(path)
        info = path.lstat()
    except (OSError, releases.UpdateError) as exc:
        raise JTTUpdateError("UNSAFE_UPDATE_PATH", "The JTT update directory cannot be prepared safely.") from exc
    if (
        not stat.S_ISDIR(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_mode & 0o077
    ):
        raise JTTUpdateError("UNSAFE_UPDATE_PATH", "JTT update directories must be private, user-owned directories.")


def _ensure_child(parent: Path, name: str) -> Path:
    child = parent / name
    try:
        child.mkdir(mode=0o700)
    except FileExistsError:
        pass
    except OSError as exc:
        raise JTTUpdateError("UNSAFE_UPDATE_PATH", "A JTT update directory could not be created.") from exc
    _ensure_private_directory(child)
    return child


@contextmanager
def _update_lock(root: Path):
    _ensure_private_directory(root)
    path = root / ".update.lock"
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    except FileExistsError as exc:
        raise JTTUpdateError("UPDATE_BUSY", "Another JTT install, update, or rollback is in progress.") from exc
    except OSError as exc:
        raise JTTUpdateError("UPDATE_LOCK_FAILED", "Could not create a private JTT update lock.") from exc
    info = os.fstat(fd)
    try:
        os.write(fd, f"pid={os.getpid()}\n".encode("ascii"))
        os.fsync(fd)
    finally:
        os.close(fd)
    identity = (info.st_dev, info.st_ino)
    try:
        yield
    finally:
        try:
            current = path.lstat()
            if stat.S_ISREG(current.st_mode) and (current.st_dev, current.st_ino) == identity:
                path.unlink()
        except OSError:
            pass


def _private_json(path: Path, *, max_bytes: int = 1024 * 1024) -> dict:
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags)
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
            or info.st_nlink != 1
            or info.st_size > max_bytes
        ):
            os.close(fd)
            raise JTTUpdateError("UNSAFE_UPDATE_METADATA", "JTT update metadata is not a private regular file.")
        with os.fdopen(fd, "r", encoding="utf-8") as stream:
            value = json.load(stream)
    except JTTUpdateError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise JTTUpdateError("INVALID_UPDATE_METADATA", "JTT update metadata cannot be read safely.") from exc
    if not isinstance(value, dict):
        raise JTTUpdateError("INVALID_UPDATE_METADATA", "JTT update metadata has an invalid shape.")
    return value


def _write_json(path: Path, value: dict) -> None:
    temporary = path.parent / ("." + path.name + "." + uuid.uuid4().hex + ".tmp")
    fd = None
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            fd = None
            json.dump(value, stream, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError as exc:
        raise JTTUpdateError("UPDATE_METADATA_WRITE", "Could not safely save JTT update metadata.") from exc
    finally:
        if fd is not None:
            os.close(fd)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _private_launcher_snapshot(path: Path) -> tuple:
    try:
        snapshot = _safe_launcher_snapshot(path)
    except (OSError, JTTUpdateError, releases.UpdateError) as exc:
        raise JTTUpdateError("UNSAFE_LAUNCHER", "The active JTT command cannot be safely inspected.") from exc
    try:
        content = snapshot[2].decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise JTTUpdateError("UNSAFE_LAUNCHER", "The active JTT command is not a valid text launcher.") from exc
    if OFFICIAL_MARKER not in content:
        raise JTTUpdateError("UNMANAGED_INSTALL", "Only the private JTT Release launcher can be updated.")
    return snapshot


def _safe_launcher_snapshot(path: Path) -> tuple:
    """Read a JTT launcher through a no-follow descriptor and pin its identity."""

    directories = releases._directory_chain(path.parent)
    before = path.lstat()
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        opened = os.fstat(fd)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_uid != os.getuid()
            or opened.st_nlink != 1
            or opened.st_mode & 0o022
            or opened.st_size > 1024 * 1024
            or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
        ):
            raise JTTUpdateError("UNSAFE_LAUNCHER", "The active JTT command is not a private regular file.")
        with os.fdopen(fd, "rb") as stream:
            fd = -1
            content = stream.read(1024 * 1024 + 1)
            after_fd = os.fstat(stream.fileno())
        after_path = path.lstat()
        identity = lambda info: (
            info.st_dev, info.st_ino, info.st_uid, info.st_mode,
            info.st_size, info.st_mtime_ns, info.st_ctime_ns,
        )
        if (
            len(content) > 1024 * 1024
            or identity(before) != identity(opened)
            or identity(before) != identity(after_fd)
            or identity(before) != identity(after_path)
        ):
            raise JTTUpdateError("LAUNCHER_CHANGED", "The active JTT command changed while it was inspected.")
        return directories, identity(before), content, stat.S_IMODE(before.st_mode)
    finally:
        if fd >= 0:
            os.close(fd)


def _launcher_text(python: str, site_dir: Path, release: releases.Release, launcher: Path) -> str:
    if any(ord(char) < 32 for char in (python + str(site_dir) + str(launcher))):
        raise JTTUpdateError("UNSAFE_PATH", "The JTT install path contains unsupported control characters.")
    import shlex

    return (
        "#!/bin/sh\n"
        f"{OFFICIAL_MARKER}\n"
        f"# JTT_RELEASE_SITE={site_dir}\n"
        f"# JTT_RELEASE_VERSION={release.package_version}\n"
        f"# JTT_RELEASE_SHA={release.commit_sha}\n"
        f"# JTT_PYTHON={python}\n"
        "unset PYTHONPATH JTT_DEV_ROOT\n"
        "export PYTHONNOUSERSITE=1\n"
        "export JTT_INSTALL_KIND=official\n"
        f"export JTT_RELEASE_SITE={shlex.quote(str(site_dir))}\n"
        f"export JTT_LAUNCHER_PATH={shlex.quote(str(launcher))}\n"
        f"export PYTHONPATH={shlex.quote(str(site_dir))}\n"
        f"exec {shlex.quote(python)} -m ju0_terminal_tower.bootstrap \"$@\"\n"
    )


def _run_launcher_version(path: Path, version: str, *, python: str | None = None) -> bool:
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env.pop("JTT_DEV_ROOT", None)
    env["PYTHONNOUSERSITE"] = "1"
    try:
        result = subprocess.run([str(path), "--version"], capture_output=True, text=True, timeout=20, env=env, check=False)
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0 and result.stdout.strip() == f"jtt {version} [OFFICIAL]"


def _verify_package(site_dir: Path, python: str, release: releases.Release) -> None:
    code = (
        "import importlib.metadata as m, json, os, ju0_terminal_tower as p; "
        f"d=m.distribution({DISTRIBUTION_NAME!r}); "
        "e=[x.value for x in d.entry_points if x.group=='console_scripts' and x.name=='jtt']; "
        f"print(json.dumps({{'version':p.__version__,'metadata':m.version({DISTRIBUTION_NAME!r}),"
        "'source':os.path.realpath(p.__file__),'jtt_entry':e}))"
    )
    env = os.environ.copy()
    env.pop("JTT_DEV_ROOT", None)
    env.pop("TOWER_DEV_ROOT", None)
    env["PYTHONNOUSERSITE"] = "1"
    env["PYTHONPATH"] = str(site_dir)
    try:
        # Restrict import and metadata discovery to the staged sidecar. The
        # current checkout, user site, or an installed legacy distribution
        # must not satisfy this verification on behalf of the candidate.
        result = subprocess.run(
            [python, "-S", "-c", code],
            capture_output=True,
            text=True,
            timeout=20,
            env=env,
            cwd=str(site_dir),
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise JTTUpdateError("VERIFY_FAILED", "The staged JTT package could not be inspected.") from exc
    if result.returncode != 0:
        raise JTTUpdateError("VERIFY_FAILED", "The staged JTT package could not be imported.")
    try:
        facts = json.loads((result.stdout or "").strip().splitlines()[-1])
    except (ValueError, IndexError) as exc:
        raise JTTUpdateError("VERIFY_FAILED", "The staged JTT package returned invalid metadata.") from exc
    if (
        facts.get("version") != release.package_version
        or facts.get("metadata") != release.package_version
        or facts.get("jtt_entry") != ["ju0_terminal_tower.bootstrap:main"]
        or not releases._inside(Path(facts.get("source", "/")), site_dir)
    ):
        raise JTTUpdateError("VERIFY_FAILED", "The staged package is not the verified JTT version and entry point.")


def _pip_install(source: Path, site_dir: Path, python: str) -> None:
    env = os.environ.copy()
    env["PIP_NO_INPUT"] = "1"
    env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    env["PYTHONNOUSERSITE"] = "1"
    old_umask = os.umask(0o077)
    try:
        result = subprocess.run(
            [python, "-m", "pip", "install", "--disable-pip-version-check", "--no-warn-script-location", "--no-cache-dir", "--target", str(site_dir), str(source)],
            capture_output=True,
            text=True,
            timeout=300,
            env=env,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise JTTUpdateError("INSTALL_FAILED", "Could not install the verified JTT source into its sidecar directory.") from exc
    finally:
        os.umask(old_umask)
    if result.returncode != 0:
        raise JTTUpdateError("INSTALL_FAILED", "pip could not install the verified JTT Release; the active launcher is unchanged.")


def _version_dir(root: Path, release: releases.Release) -> Path:
    return root / "versions" / f"{release.package_version}-{release.commit_sha[:12]}"


def _publish_staging_directory(stage: Path, target: Path, versions: Path) -> None:
    """Publish a complete sidecar without replacing any existing path."""

    try:
        libc = ctypes.CDLL(None, use_errno=True)
        renameat2 = libc.renameat2
    except (AttributeError, OSError) as exc:
        raise JTTUpdateError(
            "SAFE_PUBLISH_UNAVAILABLE",
            "This platform cannot atomically publish a JTT version without replacement; the active install was left unchanged.",
        ) from exc
    renameat2.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
    renameat2.restype = ctypes.c_int
    result = renameat2(
        -100, os.fsencode(stage), -100, os.fsencode(target), 1  # AT_FDCWD, RENAME_NOREPLACE
    )
    if result != 0:
        error_number = ctypes.get_errno()
        if error_number == errno.EEXIST:
            raise JTTUpdateError(
                "VERSION_PATH_COLLISION",
                "A JTT version path appeared during staging; existing data was preserved.",
            )
        raise JTTUpdateError(
            "SIDECAR_PUBLISH_FAILED",
            "The verified JTT sidecar could not be published safely; the active install was left unchanged.",
        )
    directory_fd = os.open(versions, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _read_release_record(directory: Path) -> dict | None:
    marker = directory / "release.json"
    try:
        value = _private_json(marker)
        if value.get("product") != "jtt":
            return None
        parsed_tag = _parse_jtt_tag(value.get("tag", ""))
        parsed_version = releases.parse_version(value.get("version", ""))
        sha = value.get("commit_sha", "")
        python = value.get("python", "")
        site = value.get("site", "")
        expected_name = f"{parsed_version.package}-{str(sha)[:12]}"
        if (
            parsed_tag.package != parsed_version.package
            or parsed_tag.key != parsed_version.key
            or value.get("channel") not in ("stable", "rc")
            or parsed_tag.prerelease != (value.get("channel") == "rc")
            or not releases._SHA_RE.fullmatch(str(sha))
            or not Path(python).is_file()
            or site != str(directory / "site")
            or directory.name != expected_name
        ):
            return None
        if not Path(site).is_dir() or not _path_owned_private(Path(site)):
            return None
        return {**value, "_key": parsed_version.key}
    except (JTTUpdateError, releases.UpdateError, OSError, TypeError, ValueError):
        return None


def _path_owned_private(path: Path) -> bool:
    try:
        info = path.lstat()
        return stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid() and not info.st_mode & 0o077
    except OSError:
        return False


def managed_releases(root: Path | None = None) -> list[dict]:
    versions = (root or data_root()) / "versions"
    try:
        info = versions.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            return []
        children = tuple(versions.iterdir())
    except OSError:
        return []
    records = []
    for child in children:
        if child.is_symlink() or not _path_owned_private(child):
            continue
        record = _read_release_record(child)
        if record is not None:
            records.append(record)
    return sorted(records, key=lambda item: item["_key"], reverse=True)


def _stage_release(
    release: releases.Release,
    python: str,
    *,
    root: Path,
    download_archive: Callable = releases._download_archive,
    install_archive: Callable = releases._install_archive,
    install_source: Callable = _pip_install,
    verify_package: Callable = _verify_package,
    source_root: Path | None = None,
) -> Path:
    target = _version_dir(root, release)
    site_dir = target / "site"
    if os.path.lexists(target):
        try:
            target_info = target.lstat()
        except OSError as exc:
            raise JTTUpdateError("VERSION_PATH_COLLISION", "The JTT version path cannot be inspected safely.") from exc
        if not stat.S_ISDIR(target_info.st_mode) or target_info.st_uid != os.getuid() or target_info.st_mode & 0o077:
            raise JTTUpdateError("VERSION_PATH_COLLISION", "An unsafe object already uses the JTT version path.")
        record = _read_release_record(target)
        if (
            record is None
            or record.get("tag") != release.tag
            or record.get("commit_sha") != release.commit_sha
            or record.get("version") != release.package_version
            or record.get("python") != python
        ):
            raise JTTUpdateError("VERSION_PATH_COLLISION", "An unverified JTT version directory already uses this path.")
        verify_package(site_dir, python, release)
        return site_dir

    versions = _ensure_child(root, "versions")
    stage = Path(tempfile.mkdtemp(prefix=".staging-", dir=versions))
    os.chmod(stage, 0o700)
    stage_site = stage / "site"
    try:
        if source_root is not None:
            install_source(source_root, stage_site, python)
        else:
            archive = stage / "release.tar.gz"
            download_archive(release.commit_sha, archive)
            old_umask = os.umask(0o077)
            try:
                install_archive(archive, stage_site, python)
            finally:
                os.umask(old_umask)
            try:
                archive.unlink()
            except FileNotFoundError:
                pass
        verify_package(stage_site, python, release)
        record = {
            "schema_version": 1,
            "product": "jtt",
            "tag": release.tag,
            "version": release.package_version,
            "channel": "rc" if release.prerelease else "stable",
            "commit_sha": release.commit_sha,
            "python": python,
            "site": str(target / "site"),
            "installed_at": datetime.now(timezone.utc).isoformat(),
        }
        _write_json(stage / "release.json", record)
        _publish_staging_directory(stage, target, versions)
    except (JTTUpdateError, releases.UpdateError):
        shutil.rmtree(stage, ignore_errors=True)
        raise
    except Exception as exc:
        shutil.rmtree(stage, ignore_errors=True)
        raise JTTUpdateError("INSTALL_FAILED", "JTT Release staging failed; the active launcher was not changed.") from exc
    except BaseException:
        shutil.rmtree(stage, ignore_errors=True)
        raise
    return site_dir


def _backup_launcher(snapshot: tuple, launcher: Path, root: Path, *, version: str) -> Path:
    backup_dir = _ensure_child(root, "backups")
    safe_version = "".join(char for char in version if char.isalnum() or char in ".-_")[:64] or "unknown"
    backup = backup_dir / f"jtt-{safe_version}-{uuid.uuid4().hex}.launcher"
    try:
        fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(snapshot[2])
            stream.flush()
            os.fsync(stream.fileno())
    except OSError as exc:
        raise JTTUpdateError("BACKUP_FAILED", "Could not preserve the current JTT launcher before switching.") from exc
    return backup


def _atomic_switch(launcher: Path, text: str, python: str, version: str, root: Path, *, current_snapshot: tuple) -> Path:
    backup = _backup_launcher(current_snapshot, launcher, root, version=version)
    candidate = launcher.parent / (".jtt-update-" + uuid.uuid4().hex + ".tmp")
    fd = None
    switched = False
    try:
        fd = os.open(candidate, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o700)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            fd = None
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(candidate, stat.S_IMODE(current_snapshot[1][3]))
        if not _run_launcher_version(candidate, version, python=python):
            raise JTTUpdateError("VERIFY_FAILED", "The candidate JTT launcher failed its version check; the active launcher is unchanged.")
        try:
            fresh = _safe_launcher_snapshot(launcher)
        except (JTTUpdateError, releases.UpdateError, OSError) as exc:
            raise JTTUpdateError("LAUNCHER_CHANGED", "The active JTT command changed during update; no switch was made.") from exc
        if fresh[:3] != current_snapshot[:3]:
            raise JTTUpdateError("LAUNCHER_CHANGED", "The active JTT command changed during update; no switch was made.")
        switched = True
        os.replace(candidate, launcher)
        if not _run_launcher_version(launcher, version, python=python):
            raise JTTUpdateError("VERIFY_FAILED", "The switched JTT launcher failed verification.")
        return backup
    except BaseException as exc:
        if switched:
            rollback = launcher.parent / (".jtt-rollback-" + uuid.uuid4().hex + ".tmp")
            try:
                rollback_fd = os.open(
                    rollback,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                    0o700,
                )
                with os.fdopen(rollback_fd, "wb") as stream:
                    stream.write(current_snapshot[2])
                    stream.flush()
                    os.fsync(stream.fileno())
                os.chmod(rollback, current_snapshot[3])
                os.replace(rollback, launcher)
            except OSError as rollback_error:
                raise JTTUpdateError("ROLLBACK_FAILED", f"JTT launcher restoration failed; preserved backup: {backup}") from rollback_error
            if isinstance(exc, JTTUpdateError):
                raise JTTUpdateError("ROLLBACK", f"JTT activation failed; the previous launcher was restored from {backup}.") from exc
        raise
    finally:
        if fd is not None:
            os.close(fd)
        for path_name in ("candidate", "rollback"):
            path = locals().get(path_name)
            if isinstance(path, Path):
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass


def apply_update(
    release: releases.Release,
    installation: dict,
    *,
    root: Path | None = None,
    download_archive: Callable = releases._download_archive,
    install_archive: Callable = releases._install_archive,
    verify_package: Callable = _verify_package,
) -> dict:
    _validate_release(release)
    if not installation.get("managed") or installation.get("kind") != "official":
        raise JTTUpdateError("UNMANAGED_INSTALL", "Automatic updates are available only for an official JTT Release installation.")
    launcher = Path(installation["launcher"])
    python = str(installation["python"])
    current = inspect_official_launcher(argv0=str(launcher))
    if (
        not current.get("managed")
        or current.get("version") != installation.get("version")
        or current.get("commit_sha") != installation.get("commit_sha")
        or current.get("site") != installation.get("site")
    ):
        raise JTTUpdateError("LAUNCHER_CHANGED", "The active JTT Release identity changed before update.")
    store = root or data_root()
    with _update_lock(store):
        locked_current = inspect_official_launcher(argv0=str(launcher))
        if (
            not locked_current.get("managed")
            or locked_current.get("version") != installation.get("version")
            or locked_current.get("commit_sha") != installation.get("commit_sha")
            or locked_current.get("site") != installation.get("site")
        ):
            raise JTTUpdateError("LAUNCHER_CHANGED", "The active JTT Release identity changed before update.")
        current_snapshot = _private_launcher_snapshot(launcher)
        versions = _ensure_child(store, "versions")
        del versions
        site_dir = _stage_release(
            release, python, root=store,
            download_archive=download_archive,
            install_archive=install_archive,
            verify_package=verify_package,
        )
        text = _launcher_text(python, site_dir, release, launcher)
        backup = _atomic_switch(launcher, text, python, release.package_version, store, current_snapshot=current_snapshot)
    return {"launcher": str(launcher), "site": str(site_dir), "backup": str(backup), "version": release.package_version}


def _validate_release(release: releases.Release) -> None:
    try:
        tag_version = _parse_jtt_tag(release.tag)
        version = releases.parse_version(release.package_version)
    except releases.UpdateError as exc:
        raise JTTUpdateError("UNVERIFIED_RELEASE", "The selected JTT Release has an invalid tag or version.") from exc
    if (
        tag_version.package != version.package
        or tag_version.key != release.key
        or tag_version.prerelease != release.prerelease
        or not releases._SHA_RE.fullmatch(str(release.commit_sha))
    ):
        raise JTTUpdateError("UNVERIFIED_RELEASE", "The selected JTT Release tag, channel, version, and commit do not agree.")


def _parse_jtt_tag(tag: str) -> releases.Version:
    """Parse a product-scoped JTT tag; legacy Tower semver tags are excluded."""

    if not isinstance(tag, str) or not tag.startswith(JTT_RELEASE_TAG_PREFIX):
        raise releases.UpdateError("JTT_TAG_REQUIRED", "JTT Releases must use the jtt-v<version> tag namespace.")
    return releases.parse_version(tag[len(JTT_RELEASE_NAMESPACE):], tag=True)


def _parse_jtt_release_item(item: dict) -> releases.Release | None:
    if not isinstance(item, dict):
        raise releases.UpdateError("INVALID_RELEASE_METADATA", "GitHub returned a non-object release entry.")
    tag = item.get("tag_name")
    if not isinstance(tag, str):
        raise releases.UpdateError("INVALID_RELEASE_METADATA", "GitHub release has no valid tag_name.")
    if not tag.startswith(JTT_RELEASE_NAMESPACE):
        # The repository also publishes legacy Tower Releases. Their tags do
        # not enter JTT version selection, even when they have newer semver.
        return None
    try:
        version = _parse_jtt_tag(tag)
    except releases.UpdateError as exc:
        raise releases.UpdateError("INVALID_JTT_RELEASE_TAG", "A JTT Release has an invalid product-scoped tag.") from exc
    if item.get("draft") is True:
        return None
    if item.get("draft") is not False:
        raise releases.UpdateError("INVALID_RELEASE_METADATA", "JTT Release is missing a valid draft flag.")
    if not isinstance(item.get("published_at"), str) or not item["published_at"]:
        raise releases.UpdateError("INVALID_RELEASE_METADATA", "JTT Release is not published.")
    prerelease = item.get("prerelease")
    if not isinstance(prerelease, bool) or prerelease != version.prerelease:
        raise releases.UpdateError("INVALID_RELEASE_METADATA", "JTT Release channel metadata conflicts with its tag.")
    return releases.Release(tag, version.package, prerelease, version.key)


def _select_jtt_release(release_items, channel: str) -> releases.Release:
    if channel not in ("stable", "rc"):
        raise releases.UpdateError("INVALID_CHANNEL", f"Unsupported JTT release channel: {channel}")
    candidates = [
        release for item in release_items
        if (release := _parse_jtt_release_item(item)) is not None
        and release.prerelease == (channel == "rc")
    ]
    if not candidates:
        raise releases.UpdateError("NO_RELEASE", f"No published JTT {channel} Release is available.")
    newest_key = max(release.key for release in candidates)
    newest = [release for release in candidates if release.key == newest_key]
    if len({release.tag for release in newest}) != 1:
        raise releases.UpdateError("AMBIGUOUS_RELEASE", "Multiple JTT Releases have the same version.")
    return newest[0]


def check_for_updates(current_version: str, requested_channel: str = "auto", fetch_json: Callable = releases._api_json) -> releases.UpdateStatus:
    """Select only published product-scoped JTT Releases and verify the Git tag."""

    current = releases.parse_version(current_version)
    if requested_channel not in ("auto", "stable", "rc"):
        raise releases.UpdateError("INVALID_CHANNEL", f"Unsupported JTT release channel: {requested_channel}")
    records = fetch_json("/releases?per_page=100")
    if not isinstance(records, list):
        raise releases.UpdateError("INVALID_GITHUB_RESPONSE", "GitHub Releases response is not a list.")
    channel = releases.default_channel(current_version) if requested_channel == "auto" else requested_channel
    latest = _select_jtt_release(records, channel)
    commit_sha = releases._tag_commit(latest.tag, fetch_json)
    latest = releases.Release(latest.tag, latest.package_version, latest.prerelease, latest.key, commit_sha)
    return releases.UpdateStatus(current, channel, latest, latest.key > current.key, tuple(records))


def verify_bootstrap(root: Path, version: str, release_items, fetch_json: Callable = releases._api_json) -> releases.Release:
    """Verify a clean checkout at a published, product-scoped JTT tag."""

    if releases._checkout_dirty(root) is not False:
        raise JTTUpdateError("BOOTSTRAP_DIRTY", "Use a fresh, clean checkout of an official published JTT Release.")
    try:
        tag = releases._git_value(root, "describe", "--tags", "--exact-match", "HEAD")
        tagged_version = _parse_jtt_tag(tag)
        expected_version = releases.parse_version(version)
    except releases.UpdateError as exc:
        code = "JTT_BOOTSTRAP_TAG_REQUIRED" if exc.code == "JTT_TAG_REQUIRED" else "BOOTSTRAP_TAG_INVALID"
        raise JTTUpdateError(code, "Bootstrap requires an exact jtt-v<version> Release tag.") from exc
    if tagged_version.package != expected_version.package:
        raise JTTUpdateError("BOOTSTRAP_VERSION_MISMATCH", "The JTT checkout version does not match its product Release tag.")
    matching = [
        release for item in release_items
        if (release := _parse_jtt_release_item(item)) is not None and release.tag == tag
    ]
    if len(matching) != 1:
        raise JTTUpdateError("BOOTSTRAP_NOT_PUBLISHED", "The JTT tag is not a unique published JTT Release.")
    try:
        remote_commit = releases._tag_commit(tag, fetch_json)
        local_commit = releases._git_value(root, "rev-parse", "HEAD")
    except releases.UpdateError as exc:
        raise JTTUpdateError("BOOTSTRAP_NOT_VERIFIED", "The JTT Release tag could not be resolved safely.") from exc
    if remote_commit != local_commit:
        raise JTTUpdateError("BOOTSTRAP_COMMIT_MISMATCH", "The checkout does not match the published JTT tag commit.")
    release = matching[0]
    return releases.Release(tag, release.package_version, release.prerelease, release.key, remote_commit)


def _current_is_dev() -> bool:
    return installation_kind() == "DEV" or source_checkout_root() is not None


def _confirmation(confirm: Callable | None, prompt: str) -> bool:
    if confirm is None and not sys.stdin.isatty():
        return False
    ask = confirm or input
    return ask(prompt).strip().lower() == "yes"


def run_update_cli(
    action: str,
    *,
    requested_channel: str = "auto",
    fetch_json: Callable = releases._api_json,
    confirm: Callable | None = None,
    output=None,
    error=None,
    current_facts: dict | None = None,
    installation: dict | None = None,
    root: Path | None = None,
) -> int:
    output = output or sys.stdout
    error = error or sys.stderr
    if _current_is_dev():
        print("DEV install detected: official updates are disabled for source checkouts; no Release request or file change was made.", file=error)
        return 2
    if installation_kind() == "PACKAGE_MANAGER":
        print("PACKAGE_MANAGER_OWNED: update or remove JTT with pipx or uv; no Release request or file change was made.", file=error)
        return 2
    installation = installation or inspect_official_launcher()
    if not installation.get("managed"):
        print(f"UPDATE_REFUSED: {installation.get('reason', 'Install an official JTT Release first.')}", file=error)
        return 2
    if requested_channel not in ("auto", "stable", "rc"):
        print("INVALID_CHANNEL: choose auto, stable, or rc.", file=error)
        return 2
    current_version = (current_facts or {}).get("version", JTT_VERSION)
    try:
        status = check_for_updates(current_version, requested_channel, fetch_json)
    except releases.UpdateError as exc:
        print(f"{exc.code}: {exc}", file=error)
        if exc.code == "OFFLINE":
            print("No files changed; JTT remains on the installed version.", file=output)
        return 1
    print(f"Release channel: {status.channel.upper()}", file=output)
    print(f"Current JTT: {status.current.package}", file=output)
    print(f"Verified published Release: {status.latest.tag} ({status.latest.commit_sha[:12]})", file=output)
    if not status.available:
        print("Update status: current version is up to date on this channel; no downgrade was made.", file=output)
        return 0
    target = _version_dir(root or data_root(), status.latest)
    print("Planned changes:", file=output)
    print(f"  verified Release sidecar: {target}", file=output)
    print(f"  JTT launcher: {installation['launcher']}", file=output)
    print("  keep previous version and launcher backup; do not touch JTT_HOME, PTYs, or running supervisors.", file=output)
    if action == "check":
        return 0
    if action == "dry-run":
        print("Dry run: no files changed.", file=output)
        return 0
    if action != "apply":
        print("INVALID_ACTION: update action is not supported.", file=error)
        return 2
    if not _confirmation(confirm, "Apply the verified JTT Release? Type 'yes': "):
        print("Confirmation was not provided; no files changed.", file=output)
        return 2 if not sys.stdin.isatty() else 0
    try:
        result = apply_update(status.latest, installation, root=root)
    except JTTUpdateError as exc:
        print(f"{exc.code}: {exc}", file=error)
        return 1
    except releases.UpdateError as exc:
        print(f"{exc.code}: {exc}", file=error)
        return 1
    except Exception:
        print("UPDATE_FAILED: JTT update failed safely; no automatic retry was attempted.", file=error)
        return 1
    print(f"JTT updated to {result['version']}; restart JTT when convenient.", file=output)
    print(f"Previous launcher retained at {result['backup']}.", file=output)
    return 0


def rollback_target(root: Path | None = None, current_version: str | None = None) -> dict | None:
    try:
        current = releases.parse_version(current_version or JTT_VERSION)
    except releases.UpdateError:
        return None
    for record in managed_releases(root):
        if record.get("_key", current.key) < current.key:
            return record
    return None


def rollback_to_previous(
    installation: dict,
    *,
    root: Path | None = None,
    current_version: str | None = None,
    verify_package: Callable = _verify_package,
) -> dict:
    if not installation.get("managed") or installation.get("kind") != "official":
        raise JTTUpdateError("UNMANAGED_INSTALL", "Rollback is available only for an official JTT Release installation.")
    store = root or data_root()
    launcher = Path(installation["launcher"])
    with _update_lock(store):
        current = inspect_official_launcher(argv0=str(launcher))
        if (
            not current.get("managed")
            or current.get("version") != installation.get("version")
            or current.get("commit_sha") != installation.get("commit_sha")
            or current.get("site") != installation.get("site")
        ):
            raise JTTUpdateError("LAUNCHER_CHANGED", "The active JTT Release identity changed before rollback.")
        snapshot = _private_launcher_snapshot(launcher)
        target = rollback_target(store, current_version or JTT_VERSION)
        if not target:
            raise JTTUpdateError("NO_ROLLBACK_TARGET", "No older verified JTT version is retained.")
        release = releases.Release(
            str(target["tag"]), str(target["version"]), target.get("channel") == "rc",
            releases.parse_version(str(target["version"])).key, str(target["commit_sha"]),
        )
        _validate_release(release)
        site_dir = Path(target["site"])
        python = str(target["python"])
        verify_package(site_dir, python, release)
        text = _launcher_text(python, site_dir, release, launcher)
        backup = _atomic_switch(launcher, text, python, release.package_version, store, current_snapshot=snapshot)
    return {"version": release.package_version, "launcher": str(launcher), "backup": str(backup), "site": str(site_dir)}


def run_rollback_cli(*, dry_run: bool = False, confirm: Callable | None = None, output=None, error=None, installation: dict | None = None, root: Path | None = None, current_version: str | None = None) -> int:
    output = output or sys.stdout
    error = error or sys.stderr
    if _current_is_dev():
        print("DEV install detected: rollback is disabled for source checkouts; no files changed.", file=error)
        return 2
    if installation_kind() == "PACKAGE_MANAGER":
        print("PACKAGE_MANAGER_OWNED: downgrade or remove JTT with pipx or uv; no file changes were made.", file=error)
        return 2
    installation = installation or inspect_official_launcher()
    if not installation.get("managed"):
        print(f"ROLLBACK_REFUSED: {installation.get('reason', 'Install an official JTT Release first.')}", file=error)
        return 2
    target = rollback_target(root or data_root(), current_version or JTT_VERSION)
    if not target:
        print("NO_ROLLBACK_TARGET: no older verified JTT version is retained.", file=error)
        return 1
    print(f"Rollback target: JTT {target['version']} ({target['commit_sha'][:12]})", file=output)
    print(f"Launcher: {installation['launcher']}; the current version will remain installed for recovery.", file=output)
    if dry_run:
        print("Dry run: no files changed.", file=output)
        return 0
    if not _confirmation(confirm, "Restore this previous JTT version? Type 'yes': "):
        print("Confirmation was not provided; no files changed.", file=output)
        return 2 if not sys.stdin.isatty() else 0
    try:
        result = rollback_to_previous(installation, root=root, current_version=current_version)
    except (JTTUpdateError, releases.UpdateError) as exc:
        print(f"{getattr(exc, 'code', 'ROLLBACK_FAILED')}: {exc}", file=error)
        return 1
    except Exception:
        print("ROLLBACK_FAILED: JTT rollback failed safely; no automatic retry was attempted.", file=error)
        return 1
    print(f"JTT rolled back to {result['version']}; retained current launcher at {result['backup']}.", file=output)
    return 0


def install_release_checkout(
    source_root: Path,
    *,
    fetch_json: Callable = releases._api_json,
    confirm: Callable | None = None,
    output=None,
    error=None,
    home: Path | None = None,
    python: str | None = None,
    path_lookup=shutil.which,
    install_source: Callable = _pip_install,
    verify_package: Callable = _verify_package,
    run_version: Callable = _run_launcher_version,
) -> dict:
    """Bootstrap only from a clean exact checkout of a published Release."""

    output = output or sys.stdout
    error = error or sys.stderr
    root = Path(source_root).resolve()
    if sys.version_info < (3, 9):
        raise JTTUpdateError("UNSUPPORTED_PYTHON", "Official JTT installation requires Python 3.9 or newer; no files were changed.")
    version = JTT_VERSION
    releases_list = fetch_json("/releases?per_page=100")
    if not isinstance(releases_list, list):
        raise JTTUpdateError("INVALID_GITHUB_RESPONSE", "GitHub Releases response is not a list.")
    try:
        release = verify_bootstrap(root, version, releases_list, fetch_json)
    except (JTTUpdateError, releases.UpdateError) as exc:
        raise JTTUpdateError(getattr(exc, "code", "BOOTSTRAP_NOT_VERIFIED"), str(exc)) from exc
    python = python or sys.executable
    target_launcher = (Path(home) if home is not None else Path.home()) / ".local" / "bin" / "jtt"
    existing_command = path_lookup("jtt")
    if existing_command:
        raise JTTUpdateError("LAUNCHER_CONFLICT", "A jtt command is already on PATH; it was left unchanged.")
    if os.path.lexists(target_launcher):
        raise JTTUpdateError("LAUNCHER_CONFLICT", "An existing jtt launcher is present; the Release installer will not replace it.")
    store = data_root(home=Path(home) if home is not None else None)
    print(f"Verified official JTT Release: {release.tag} ({release.commit_sha[:12]})", file=output)
    print(f"Versioned install: {_version_dir(store, release)}", file=output)
    print(f"New PATH launcher: {target_launcher}", file=output)
    print("JTT_HOME, saved workspaces, and running terminal sessions will not be changed.", file=output)
    if not _confirmation(confirm, "Install this official JTT Release? Type 'yes': "):
        print("Confirmation was not provided; no files changed.", file=output)
        return {}

    with _update_lock(store):
        site_dir = _stage_release(
            release, python, root=store, source_root=root,
            install_source=install_source, verify_package=verify_package,
        )
        try:
            releases._ensure_directory_chain(target_launcher.parent)
            info = target_launcher.parent.lstat()
        except (OSError, releases.UpdateError) as exc:
            raise JTTUpdateError("UNSAFE_LAUNCHER_PATH", "The user bin directory is not safe for a JTT launcher.") from exc
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
            raise JTTUpdateError("UNSAFE_LAUNCHER_PATH", "The user bin directory has unsafe ownership or permissions.")
        text = _launcher_text(python, site_dir, release, target_launcher)
        candidate = target_launcher.parent / (".jtt-install-" + uuid.uuid4().hex + ".tmp")
        candidate_fd = None
        created_identity = None
        try:
            candidate_fd = os.open(
                candidate,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o700,
            )
            with os.fdopen(candidate_fd, "w", encoding="utf-8") as stream:
                candidate_fd = None
                stream.write(text)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(candidate, 0o755)
            candidate_info = candidate.lstat()
            if not run_version(candidate, release.package_version, python=python):
                raise JTTUpdateError("VERIFY_FAILED", "The new JTT launcher failed validation; it was not activated.")
            if path_lookup("jtt") or os.path.lexists(target_launcher):
                raise JTTUpdateError("LAUNCHER_CONFLICT", "A jtt command appeared during install; it was left unchanged.")
            os.link(candidate, target_launcher)
            created_identity = target_launcher.lstat()
            if (created_identity.st_dev, created_identity.st_ino) != (candidate_info.st_dev, candidate_info.st_ino):
                created_identity = None
                raise JTTUpdateError("LAUNCHER_CHANGED", "The new JTT launcher identity changed during install; no other file was removed.")
            directory_fd = os.open(target_launcher.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
            final_info = target_launcher.lstat()
            if (final_info.st_dev, final_info.st_ino) != (created_identity.st_dev, created_identity.st_ino):
                created_identity = None
                raise JTTUpdateError("LAUNCHER_CHANGED", "The JTT launcher changed during install; no other file was removed.")
        except FileExistsError as exc:
            raise JTTUpdateError("LAUNCHER_CONFLICT", "A jtt launcher appeared during installation; it was left unchanged.") from exc
        except JTTUpdateError:
            if created_identity is not None:
                try:
                    info = target_launcher.lstat()
                    if (
                        stat.S_ISREG(info.st_mode)
                        and info.st_nlink in (1, 2)
                        and (info.st_dev, info.st_ino) == (created_identity.st_dev, created_identity.st_ino)
                    ):
                        target_launcher.unlink()
                except OSError:
                    pass
            raise
        except OSError as exc:
            if created_identity is not None:
                try:
                    info = target_launcher.lstat()
                    if (
                        stat.S_ISREG(info.st_mode)
                        and info.st_nlink in (1, 2)
                        and (info.st_dev, info.st_ino) == (created_identity.st_dev, created_identity.st_ino)
                    ):
                        target_launcher.unlink()
                except OSError:
                    pass
            raise JTTUpdateError("INSTALL_FAILED", "The official JTT launcher could not be safely installed.") from exc
        finally:
            if candidate_fd is not None:
                os.close(candidate_fd)
            try:
                candidate.unlink()
            except FileNotFoundError:
                pass
    return {"launcher": target_launcher, "site": site_dir, "version": release.package_version, "release": release}


def install_cli(argv=None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="jtt-install-release")
    parser.add_argument("--source", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        result = install_release_checkout(args.source)
    except (JTTUpdateError, releases.UpdateError) as exc:
        print(f"INSTALL_REFUSED: {getattr(exc, 'code', 'INSTALL_FAILED')}: {exc}", file=sys.stderr)
        return 2
    if not result:
        return 0
    print(f"Installed official JTT {result['version']}: {result['launcher']}")
    if str(result["launcher"].parent) not in os.environ.get("PATH", "").split(os.pathsep):
        print('For this shell only, add it with: export PATH="$HOME/.local/bin:$PATH"')
        print("JTT did not edit a shell profile; review any permanent PATH change yourself.")
    return 0


if __name__ == "__main__":
    if sys.argv[1:2] == ["install-release"]:
        raise SystemExit(install_cli(sys.argv[2:]))
    raise SystemExit("Use jtt update, jtt rollback, or the verified scripts/jtt-install-release bootstrap.")
