"""Shared product-neutral primitives for verified GitHub Release installs.

Product-specific updater policy (repository identity, eligible tag namespace,
launcher ownership and update state) stays with each product.  This module
contains only the common version, GitHub, archive, and filesystem operations.
"""

from __future__ import annotations

import json
import os
import re
import socket
import stat
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional


_TAG_RE = re.compile(r"^v?(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:-rc(0|[1-9][0-9]*))?$")
_PACKAGE_RE = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:rc(0|[1-9][0-9]*))?$")
_REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_TIMEOUT = 10
_MAX_ARCHIVE_BYTES = 64 * 1024 * 1024


class UpdateError(RuntimeError):
    """A safe, user-displayable Release operation failure."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Version:
    package: str
    key: tuple
    prerelease: bool


@dataclass(frozen=True)
class Release:
    tag: str
    package_version: str
    prerelease: bool
    key: tuple
    commit_sha: str = ""


@dataclass(frozen=True)
class UpdateStatus:
    current: Version
    channel: str
    latest: Release
    available: bool
    releases: tuple


def parse_version(value: str, *, tag: bool = False) -> Version:
    match = (_TAG_RE if tag else _PACKAGE_RE).fullmatch(value or "")
    if not match:
        raise UpdateError("UNSUPPORTED_VERSION", f"Unsupported version/tag format: {value!r}")
    major, minor, patch = (int(match.group(i)) for i in (1, 2, 3))
    rc = int(match.group(4)) if match.group(4) is not None else None
    package = f"{major}.{minor}.{patch}" + (f"rc{rc}" if rc is not None else "")
    return Version(package, (major, minor, patch, 0 if rc is not None else 1, rc or 0), rc is not None)


def default_channel(version: str) -> str:
    return "rc" if parse_version(version).prerelease else "stable"


def api_json(repository: str, path: str, *, user_agent: str) -> object:
    if not _REPOSITORY_RE.fullmatch(repository) or not path.startswith("/") or path.startswith("//"):
        raise UpdateError("INVALID_RELEASE_SOURCE", "The configured GitHub Release source is invalid.")
    api_root = "https://api.github.com/repos/" + urllib.parse.quote(repository, safe="/")
    request = urllib.request.Request(
        api_root + path,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": user_agent,
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT) as response:
            final_url = urllib.parse.urlparse(response.geturl())
            if final_url.scheme != "https" or final_url.hostname not in ("api.github.com", "github.com"):
                raise UpdateError("UNTRUSTED_GITHUB_HOST", "GitHub API redirected to an unexpected URL.")
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise UpdateError("GITHUB_HTTP", f"GitHub API returned HTTP {exc.code}.") from exc
    except (urllib.error.URLError, TimeoutError, socket.timeout, OSError) as exc:
        raise UpdateError("OFFLINE", "Could not reach GitHub; check your network and try again.") from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise UpdateError("INVALID_GITHUB_RESPONSE", "GitHub returned invalid JSON.") from exc


def tag_commit(tag: str, fetch_json: Callable) -> str:
    path = "/git/ref/tags/" + urllib.parse.quote(tag, safe="")
    ref = fetch_json(path)
    if not isinstance(ref, dict) or ref.get("ref") != f"refs/tags/{tag}":
        raise UpdateError("UNVERIFIED_TAG", f"Could not verify the Git tag {tag}.")
    obj = ref.get("object")
    if not isinstance(obj, dict):
        raise UpdateError("UNVERIFIED_TAG", f"GitHub returned no object for {tag}.")
    seen = set()
    while obj.get("type") == "tag":
        sha = obj.get("sha")
        if not isinstance(sha, str) or not _SHA_RE.fullmatch(sha) or sha in seen or len(seen) >= 16:
            raise UpdateError("UNVERIFIED_TAG", f"Invalid annotated tag object for {tag}.")
        seen.add(sha)
        tagged = fetch_json(f"/git/tags/{sha}")
        if not isinstance(tagged, dict) or tagged.get("sha") != sha:
            raise UpdateError("UNVERIFIED_TAG", f"GitHub returned a different annotated tag object for {tag}.")
        obj = tagged.get("object")
        if not isinstance(obj, dict):
            raise UpdateError("UNVERIFIED_TAG", f"Could not resolve annotated tag {tag}.")
    sha = obj.get("sha") if obj.get("type") == "commit" else ""
    if not isinstance(sha, str) or not _SHA_RE.fullmatch(sha):
        raise UpdateError("UNVERIFIED_TAG", f"Tag {tag} does not resolve to a commit.")
    return sha


def checkout_dirty(root: Path) -> Optional[bool]:
    if not (root / ".git").exists():
        return None
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain", "--untracked-files=normal"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return bool(result.stdout.strip())


def inside(path: Path, parent: Path) -> bool:
    try:
        return os.path.commonpath([str(path.resolve()), str(parent.resolve())]) == str(parent.resolve())
    except (OSError, ValueError):
        return False


def directory_chain(path: Path) -> tuple:
    current = Path(os.path.abspath(path))
    chain = []
    writable_by_others = stat.S_IWGRP | stat.S_IWOTH
    while True:
        try:
            info = current.lstat()
        except OSError as exc:
            raise UpdateError("UNSAFE_PATH", f"Cannot inspect update path directory: {current}.") from exc
        if not stat.S_ISDIR(info.st_mode):
            raise UpdateError("UNSAFE_PATH", "Update paths cannot pass through symlinks or non-directory components.")
        if info.st_mode & writable_by_others and not (
            info.st_mode & stat.S_ISVTX and info.st_uid in (0, os.getuid())
        ):
            raise UpdateError("UNSAFE_PATH", "An update path ancestor is writable by other users.")
        chain.append((str(current), info.st_dev, info.st_ino, info.st_uid, info.st_mode))
        if current.parent == current:
            break
        current = current.parent
    return tuple(reversed(chain))


def ensure_directory_chain(path: Path) -> None:
    current = Path(os.path.abspath(path))
    missing = []
    while True:
        try:
            current.lstat()
            break
        except FileNotFoundError:
            missing.append(current)
            if current.parent == current:
                raise UpdateError("UNSAFE_PATH", "Cannot find a safe parent for the update directory.")
            current = current.parent
    directory_chain(current)
    for directory in reversed(missing):
        directory.mkdir(mode=0o700, exist_ok=True)
    directory_chain(path)


def download_archive(repository: str, commit_sha: str, destination: Path, *, user_agent: str) -> None:
    if not _REPOSITORY_RE.fullmatch(repository) or not _SHA_RE.fullmatch(commit_sha):
        raise UpdateError("UNVERIFIED_TAG", "Cannot download a source archive for an unverified Release.")
    api_root = "https://api.github.com/repos/" + urllib.parse.quote(repository, safe="/")
    request = urllib.request.Request(
        f"{api_root}/tarball/{commit_sha}",
        headers={"Accept": "application/vnd.github+json", "User-Agent": user_agent},
    )
    total = 0
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT) as response:
            final_url = urllib.parse.urlparse(response.geturl())
            if final_url.scheme != "https" or final_url.hostname not in (
                "api.github.com", "github.com", "codeload.github.com", "objects.githubusercontent.com"
            ):
                raise UpdateError("UNTRUSTED_ARCHIVE_HOST", "GitHub redirected the archive request to an unexpected host.")
            with destination.open("wb") as output:
                while True:
                    block = response.read(64 * 1024)
                    if not block:
                        break
                    total += len(block)
                    if total > _MAX_ARCHIVE_BYTES:
                        raise UpdateError("ARCHIVE_TOO_LARGE", "Release source archive exceeded the size limit.")
                    output.write(block)
    except urllib.error.HTTPError as exc:
        raise UpdateError("GITHUB_HTTP", f"GitHub archive returned HTTP {exc.code}.") from exc
    except (urllib.error.URLError, TimeoutError, socket.timeout, OSError) as exc:
        raise UpdateError("OFFLINE", "Could not download the official GitHub source archive.") from exc
    if total == 0:
        raise UpdateError("EMPTY_ARCHIVE", "GitHub returned an empty source archive.")


def install_archive(archive: Path, site_dir: Path, python: str) -> None:
    result = subprocess.run(
        [
            python,
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--no-warn-script-location",
            "--no-cache-dir",
            "--target",
            str(site_dir),
            str(archive),
        ],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
        env={**os.environ, "PIP_NO_INPUT": "1"},
    )
    if result.returncode != 0:
        raise UpdateError("INSTALL_FAILED", "pip could not install the verified Release into the isolated version directory.")


def git_value(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if result.returncode != 0:
        raise UpdateError("BOOTSTRAP_NOT_VERIFIED", "The migration checkout is not a clean, exact published Git tag.")
    return result.stdout.strip()


class ReleaseAPI:
    """Repository-scoped GitHub API and archive access for a product."""

    UpdateError = UpdateError
    Version = Version
    Release = Release
    UpdateStatus = UpdateStatus
    _SHA_RE = _SHA_RE
    parse_version = staticmethod(parse_version)
    default_channel = staticmethod(default_channel)
    _checkout_dirty = staticmethod(checkout_dirty)
    _inside = staticmethod(inside)
    _directory_chain = staticmethod(directory_chain)
    _ensure_directory_chain = staticmethod(ensure_directory_chain)
    directory_chain = staticmethod(directory_chain)
    ensure_directory_chain = staticmethod(ensure_directory_chain)
    _install_archive = staticmethod(install_archive)
    _git_value = staticmethod(git_value)

    def __init__(self, repository: str, *, user_agent: str):
        if not _REPOSITORY_RE.fullmatch(repository):
            raise ValueError("repository must be an owner/name pair")
        self.repository = repository
        self.user_agent = user_agent

    def _api_json(self, path: str):
        return api_json(self.repository, path, user_agent=self.user_agent)

    def _tag_commit(self, tag: str, fetch_json: Callable | None = None) -> str:
        return tag_commit(tag, fetch_json or self._api_json)

    def _download_archive(self, commit_sha: str, destination: Path) -> None:
        return download_archive(
            self.repository, commit_sha, destination, user_agent=self.user_agent
        )
