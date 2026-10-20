"""JTT launcher and Release lifecycle tests use isolated temporary paths."""

import io
import json
import os
from pathlib import Path
import stat

import pytest

from tmux_agent_tower import jtt, jtt_diagnostics, jtt_install, jtt_update, update_manager


def _release(tag="v0.3.1-rc0", version="0.3.1rc0", sha="a" * 40, prerelease=True):
    parsed = update_manager.parse_version(version)
    return update_manager.Release(tag, version, prerelease, parsed.key, sha)


def _published(tag, prerelease):
    return {"tag_name": tag, "draft": False, "prerelease": prerelease, "published_at": "2026-10-21T00:00:00Z"}


def _fake_release_api(releases):
    sha_by_tag = {item["tag_name"]: chr(97 + i) * 40 for i, item in enumerate(releases)}

    def fetch(path):
        if path == "/releases?per_page=100":
            return releases
        if path.startswith("/git/ref/tags/"):
            tag = path.rsplit("/", 1)[-1]
            return {"ref": "refs/tags/" + tag, "object": {"type": "commit", "sha": sha_by_tag[tag]}}
        raise AssertionError("unexpected API path")

    return fetch


def test_cli_version_reports_dev_and_tower_command_remains_separate(monkeypatch, capsys):
    monkeypatch.setattr(jtt_install, "installation_kind", lambda: "DEV")
    assert jtt.cli(["--version"]) == 0
    assert "[DEV]" in capsys.readouterr().out
    project = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(encoding="utf-8")
    assert 'tower = "tmux_agent_tower.main:cli"' in project
    assert 'jtt = "tmux_agent_tower.jtt:cli"' in project


def test_dev_update_refuses_before_release_network(monkeypatch, capsys):
    called = []
    monkeypatch.setattr(jtt_update, "_current_is_dev", lambda: True)
    result = jtt_update.run_update_cli("check", fetch_json=lambda *_: called.append(True))
    assert result == 2
    assert called == []
    assert "DEV install" in capsys.readouterr().err


def test_release_channel_selection_skips_drafts_and_resolves_tag_commit():
    records = [
        _published("v0.3.1", False),
        _published("v0.3.2-rc0", True),
        {"tag_name": "v0.9.0", "draft": True, "prerelease": False, "published_at": "2026-10-21"},
    ]
    fetch = _fake_release_api(records)
    stable = update_manager.check_for_updates("0.3.0rc2", "stable", fetch)
    rc = update_manager.check_for_updates("0.3.0rc2", "rc", fetch)
    assert stable.latest.tag == "v0.3.1"
    assert stable.latest.commit_sha == "a" * 40
    assert rc.latest.tag == "v0.3.2-rc0"
    assert rc.latest.commit_sha == "b" * 40


def test_update_check_and_dry_run_do_not_write_files(tmp_path, monkeypatch):
    records = [_published("v0.3.1-rc0", True)]
    fetch = _fake_release_api(records)
    launcher = tmp_path / "bin" / "jtt"
    installation = {"managed": True, "kind": "official", "launcher": launcher}
    root = tmp_path / "jtt-data"
    monkeypatch.setattr(jtt_update, "_current_is_dev", lambda: False)
    output = io.StringIO()
    checked = jtt_update.run_update_cli(
        "check", current_facts={"version": "0.3.0rc2"}, installation=installation,
        fetch_json=fetch, output=output, error=io.StringIO(), root=root,
    )
    planned = jtt_update.run_update_cli(
        "dry-run", current_facts={"version": "0.3.0rc2"}, installation=installation,
        fetch_json=fetch, output=output, error=io.StringIO(), root=root,
    )
    assert checked == planned == 0
    assert "Dry run: no files changed." in output.getvalue()
    assert not root.exists()
    assert not launcher.parent.exists()


def test_update_apply_requires_exact_interactive_confirmation(tmp_path, monkeypatch, capsys):
    records = [_published("v0.3.1-rc0", True)]
    monkeypatch.setattr(jtt_update, "_current_is_dev", lambda: False)
    installation = {"managed": True, "kind": "official", "launcher": tmp_path / "jtt"}
    result = jtt_update.run_update_cli(
        "apply", current_facts={"version": "0.3.0rc2"}, installation=installation,
        fetch_json=_fake_release_api(records), confirm=lambda _prompt: "No ", root=tmp_path / "data",
    )
    assert result == 2
    text = capsys.readouterr().out.lower()
    assert "confirmation was not provided" in text or "no files changed" in text
    assert not (tmp_path / "data").exists()


def _managed_launcher(tmp_path, version="0.3.0rc2"):
    launcher_dir = tmp_path / "home" / ".local" / "bin"
    launcher_dir.mkdir(parents=True, mode=0o700)
    os.chmod(launcher_dir.parent, 0o700)
    os.chmod(launcher_dir.parent.parent, 0o700)
    launcher = launcher_dir / "jtt"
    launcher.write_text(
        "#!/bin/sh\n" + jtt_install.OFFICIAL_MARKER + "\n" + f"# current={version}\n", encoding="utf-8"
    )
    launcher.chmod(0o755)
    return launcher


def _fake_installer(archive, site_dir, _python):
    site_dir.mkdir(mode=0o700, parents=True)
    package = site_dir / "tmux_agent_tower"
    package.mkdir(mode=0o700)
    (package / "__init__.py").write_text("__version__='0.3.1rc0'\n", encoding="utf-8")


def _versioned_fake_installer(version):
    def install(_source, site_dir, _python):
        site_dir.mkdir(mode=0o700, parents=True)
        package = site_dir / "tmux_agent_tower"
        package.mkdir(mode=0o700)
        (package / "__init__.py").write_text(f"__version__={version!r}\n", encoding="utf-8")

    return install


def test_verified_release_sidecar_switch_and_rollback_preserve_previous_version(tmp_path, monkeypatch):
    store = tmp_path / "home" / ".local" / "share" / "jtt"
    store.mkdir(parents=True, mode=0o700)
    launcher = _managed_launcher(tmp_path)
    before = launcher.read_bytes()
    install = {
        "managed": True, "kind": "official", "launcher": launcher,
        "python": sys_executable(), "version": "0.3.0rc2", "commit_sha": "b" * 40,
        "site": store / "versions" / ("0.3.0rc2-" + "b" * 12) / "site",
    }
    monkeypatch.setattr(jtt_update, "inspect_official_launcher", lambda **_kwargs: install)
    monkeypatch.setattr(jtt_update, "_run_launcher_version", lambda *_args, **_kwargs: True)
    target = _release()
    result = jtt_update.apply_update(
        target, install, root=store,
        download_archive=lambda _sha, destination: destination.write_bytes(b"synthetic archive"),
        install_archive=_fake_installer,
        verify_package=lambda *_args: None,
    )
    assert result["version"] == "0.3.1rc0"
    assert jtt_install.OFFICIAL_MARKER in launcher.read_text(encoding="utf-8")
    assert "0.3.1rc0" in launcher.read_text(encoding="utf-8")
    assert Path(result["backup"]).read_bytes() == before
    assert stat.S_IMODE(Path(result["backup"]).stat().st_mode) == 0o600

    old_version = store / "versions" / ("0.3.0rc2-" + "b" * 12)
    old_version.mkdir(mode=0o700)
    old_site = old_version / "site"
    old_site.mkdir(mode=0o700)
    old_record = {
        "schema_version": 1, "product": "jtt", "tag": "v0.3.0-rc2", "version": "0.3.0rc2",
        "channel": "rc", "commit_sha": "b" * 40, "python": sys_executable(),
        "site": str(old_site), "installed_at": "2026-10-20T00:00:00Z",
    }
    jtt_update._write_json(old_version / "release.json", old_record)
    assert jtt_update._read_release_record(old_version) is not None
    assert [item["version"] for item in jtt_update.managed_releases(store)] == ["0.3.1rc0", "0.3.0rc2"]
    rollback = jtt_update.rollback_to_previous(
        install, root=store, current_version="0.3.1rc0", verify_package=lambda *_args: None,
    )
    assert rollback["version"] == "0.3.0rc2"
    assert "0.3.0rc2" in launcher.read_text(encoding="utf-8")


def sys_executable():
    import sys

    return sys.executable


def test_failed_release_staging_keeps_active_launcher_byte_for_byte(tmp_path, monkeypatch):
    store = tmp_path / "data"
    store.mkdir(mode=0o700)
    launcher = _managed_launcher(tmp_path)
    original = launcher.read_bytes()
    install = {"managed": True, "kind": "official", "launcher": launcher, "python": sys_executable(), "version": "0.3.0rc2", "commit_sha": "b" * 40, "site": store / "old" / "site"}
    monkeypatch.setattr(jtt_update, "inspect_official_launcher", lambda **_kwargs: install)
    with pytest.raises(jtt_update.JTTUpdateError):
        jtt_update.apply_update(
            _release(), install, root=store,
            download_archive=lambda _sha, destination: destination.write_bytes(b"archive"),
            install_archive=lambda *_args: (_ for _ in ()).throw(RuntimeError("fixture failure")),
            verify_package=lambda *_args: None,
        )
    assert launcher.read_bytes() == original
    assert not list((store / "versions").glob(".staging-*"))


def test_failed_post_switch_verification_restores_exact_previous_launcher(tmp_path, monkeypatch):
    store = tmp_path / "data"
    store.mkdir(mode=0o700)
    launcher = _managed_launcher(tmp_path)
    original = launcher.read_bytes()
    installation = {
        "managed": True, "kind": "official", "launcher": launcher,
        "python": sys_executable(), "version": "0.3.0rc2", "commit_sha": "b" * 40,
        "site": store / "versions" / ("0.3.0rc2-" + "b" * 12) / "site",
    }
    monkeypatch.setattr(jtt_update, "inspect_official_launcher", lambda **_kwargs: installation)
    version_checks = iter((True, False))
    monkeypatch.setattr(jtt_update, "_run_launcher_version", lambda *_args, **_kwargs: next(version_checks))

    with pytest.raises(jtt_update.JTTUpdateError, match="previous launcher was restored"):
        jtt_update.apply_update(
            _release(), installation, root=store,
            download_archive=lambda _sha, destination: destination.write_bytes(b"archive"),
            install_archive=_fake_installer, verify_package=lambda *_args: None,
        )

    assert launcher.read_bytes() == original
    assert not list(launcher.parent.glob(".jtt-rollback-*.tmp"))


def test_staging_publish_refuses_existing_sidecar_without_replacing_it(tmp_path):
    versions = tmp_path / "versions"
    versions.mkdir(mode=0o700)
    stage = versions / ".staging-fixture"
    stage.mkdir(mode=0o700)
    (stage / "content").write_text("new", encoding="utf-8")
    target = versions / "versioned"
    target.mkdir(mode=0o700)
    (target / "content").write_text("preserve", encoding="utf-8")

    with pytest.raises(jtt_update.JTTUpdateError, match="appeared during staging"):
        jtt_update._publish_staging_directory(stage, target, versions)

    assert (target / "content").read_text(encoding="utf-8") == "preserve"
    assert (stage / "content").read_text(encoding="utf-8") == "new"


def test_unverified_release_tag_is_rejected_before_install(tmp_path):
    bad = _release(tag="v0.3.2-rc0", version="0.3.1rc0")
    with pytest.raises(jtt_update.JTTUpdateError, match="tag, channel, version"):
        jtt_update._validate_release(bad)


def test_dev_launcher_installs_only_when_no_command_conflicts(tmp_path):
    root = tmp_path / "checkout"
    (root / "src" / "tmux_agent_tower").mkdir(parents=True)
    (root / "pyproject.toml").write_text("[project]\nname='fixture'\n", encoding="utf-8")
    (root / "src" / "tmux_agent_tower" / "jtt.py").write_text("", encoding="utf-8")
    python = root / ".venv-jtt" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    python.chmod(0o700)
    home = tmp_path / "user-home"
    result = jtt_install.install_dev_launcher(
        root, python, home=home, path_lookup=lambda _name: None, verify=lambda _path: True,
    )
    assert result["installed"] is True
    launcher = home / ".local" / "bin" / "jtt"
    text = launcher.read_text(encoding="utf-8")
    assert jtt_install.DEV_MARKER in text
    assert str(root) in text and str(python) in text
    assert f"JTT_LAUNCHER_PATH={launcher}" in text
    assert stat.S_IMODE(launcher.stat().st_mode) == 0o755
    second = jtt_install.install_dev_launcher(
        root, python, home=home, path_lookup=lambda _name: str(launcher), verify=lambda _path: True,
    )
    assert second["installed"] is False


def test_dev_launcher_never_replaces_unmanaged_path_command(tmp_path):
    root = tmp_path / "checkout"
    (root / "src" / "tmux_agent_tower").mkdir(parents=True)
    (root / "pyproject.toml").write_text("[project]\nname='fixture'\n", encoding="utf-8")
    (root / "src" / "tmux_agent_tower" / "jtt.py").write_text("", encoding="utf-8")
    python = root / ".venv-jtt" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    python.chmod(0o700)
    home = tmp_path / "home"
    unmanaged = tmp_path / "usr-bin-jtt"
    unmanaged.write_text("unmanaged launcher\n", encoding="utf-8")
    with pytest.raises(jtt_install.JTTInstallError, match="already on PATH"):
        jtt_install.install_dev_launcher(root, python, home=home, path_lookup=lambda _name: str(unmanaged))
    assert unmanaged.read_text(encoding="utf-8") == "unmanaged launcher\n"
    assert not (home / ".local" / "bin" / "jtt").exists()


def test_dev_launcher_removes_only_its_failed_candidate(tmp_path):
    root = tmp_path / "checkout"
    (root / "src" / "tmux_agent_tower").mkdir(parents=True)
    (root / "pyproject.toml").write_text("[project]\nname='fixture'\n", encoding="utf-8")
    (root / "src" / "tmux_agent_tower" / "jtt.py").write_text("", encoding="utf-8")
    python = root / ".venv-jtt" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    python.chmod(0o700)
    home = tmp_path / "home"

    with pytest.raises(jtt_install.JTTInstallError, match="failed its version check"):
        jtt_install.install_dev_launcher(
            root, python, home=home, path_lookup=lambda _name: None, verify=lambda _path: False,
        )
    assert not (home / ".local" / "bin" / "jtt").exists()
    assert not list((home / ".local" / "bin").glob(".jtt-dev-*.tmp"))


def test_doctor_is_read_only_and_does_not_create_terminal_or_home(tmp_path, monkeypatch):
    workspace = tmp_path / "not-created" / "jtt"
    registry = tmp_path / "runtime" / "jtt" / "terminals"
    monkeypatch.setenv("JTT_HOME", str(workspace))
    reported_launcher = tmp_path / "bin" / "jtt"
    monkeypatch.setenv("JTT_LAUNCHER_PATH", str(reported_launcher))
    monkeypatch.setattr(jtt_diagnostics.terminal_manager, "_default_root", lambda: registry)
    monkeypatch.setattr(jtt_diagnostics, "_renderer_status", lambda: ("pyte fixture ready", True))
    monkeypatch.setattr(jtt_diagnostics, "installation_kind", lambda: "DEV")
    monkeypatch.setattr(jtt_diagnostics.terminal_manager.TerminalRegistry, "create", lambda *_args, **_kwargs: pytest.fail("doctor must not create a PTY"))
    before = set(tmp_path.rglob("*"))
    output = io.StringIO()
    assert jtt_diagnostics.run_doctor(output=output) == 0
    after = set(tmp_path.rglob("*"))
    assert before == after
    assert not workspace.exists() and not registry.exists()
    text = output.getvalue()
    assert "read-only" in text
    assert f"Executable: {reported_launcher}" in text
    assert "not created" in text
    assert "running=0" in text
    assert "No PTY, Agent, network request" in text


def test_doctor_reports_long_socket_path_without_creating_runtime_state(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace-not-created"
    long_root = tmp_path / ("r" * 90) / ("s" * 90) / "terminals"
    monkeypatch.setenv("JTT_HOME", str(workspace))
    monkeypatch.setattr(jtt_diagnostics.terminal_manager, "_default_root", lambda: long_root)
    monkeypatch.setattr(jtt_diagnostics, "_renderer_status", lambda: ("pyte fixture ready", True))
    monkeypatch.setattr(jtt_diagnostics, "installation_kind", lambda: "DEV")
    output = io.StringIO()

    assert jtt_diagnostics.run_doctor(output=output) == 0
    assert "estimated socket path " in output.getvalue()
    planned = jtt_diagnostics.terminal_manager._planned_socket_path(long_root / ("0" * 32), "0" * 32)
    assert len(os.fsencode(planned)) <= jtt_diagnostics.terminal_manager.MAX_UNIX_SOCKET_PATH_BYTES
    assert not workspace.exists()
    assert not long_root.exists()


def test_official_installer_refuses_existing_path_command_before_writing(tmp_path, monkeypatch):
    root = tmp_path / "checkout"
    root.mkdir()
    foreign = tmp_path / "existing-jtt"
    foreign.write_text("keep me", encoding="utf-8")
    release = _release(tag="v0.3.0-rc2", version="0.3.0rc2", sha="b" * 40)
    monkeypatch.setattr(update_manager, "verify_bootstrap", lambda *_args: release)
    with pytest.raises(jtt_update.JTTUpdateError, match="already on PATH"):
        jtt_update.install_release_checkout(
            root, fetch_json=lambda _path: [], confirm=lambda _prompt: "yes",
            home=tmp_path / "home", path_lookup=lambda _name: str(foreign),
        )
    assert foreign.read_text(encoding="utf-8") == "keep me"
    assert not (tmp_path / "home").exists()


def test_official_bootstrap_installs_only_verified_release_sidecar(tmp_path, monkeypatch):
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    source = tmp_path / "clean-tag-checkout"
    source.mkdir(mode=0o700)
    home = tmp_path / "home"
    selected = _release(tag="v0.3.0-rc2", version="0.3.0rc2", sha="b" * 40)
    monkeypatch.setattr(update_manager, "verify_bootstrap", lambda _root, _version, _items, _fetch: selected)
    output = io.StringIO()
    result = jtt_update.install_release_checkout(
        source,
        fetch_json=lambda _path: [_published(selected.tag, True)],
        confirm=lambda _prompt: "yes",
        output=output,
        home=home,
        python=sys_executable(),
        path_lookup=lambda _name: None,
        install_source=_versioned_fake_installer(selected.package_version),
        verify_package=lambda site, _python, expected: (
            pytest.fail("staged package version mismatch")
            if f"__version__={expected.package_version!r}" not in (site / "tmux_agent_tower" / "__init__.py").read_text(encoding="utf-8")
            else None
        ),
        run_version=lambda *_args, **_kwargs: True,
    )
    launcher = result["launcher"]
    version_dir = jtt_update._version_dir(home / ".local" / "share" / "jtt", selected)
    record = jtt_update._private_json(version_dir / "release.json")
    assert result["version"] == selected.package_version
    assert launcher.is_file() and stat.S_IMODE(launcher.stat().st_mode) == 0o755
    assert jtt_install.OFFICIAL_MARKER in launcher.read_text(encoding="utf-8")
    assert record["product"] == "jtt" and record["commit_sha"] == selected.commit_sha
    assert stat.S_IMODE((version_dir / "release.json").stat().st_mode) == 0o600
    assert stat.S_IMODE(version_dir.stat().st_mode) == 0o700
    assert "JTT_HOME" in output.getvalue()


def test_offline_check_does_not_create_install_or_touch_launcher(tmp_path, monkeypatch):
    launcher = _managed_launcher(tmp_path)
    original = launcher.read_bytes()
    root = tmp_path / "not-created"
    installation = {
        "managed": True, "kind": "official", "launcher": launcher,
        "version": "0.3.0rc2", "commit_sha": "b" * 40,
    }
    monkeypatch.setattr(jtt_update, "_current_is_dev", lambda: False)
    error = io.StringIO()
    output = io.StringIO()

    def offline(_path):
        raise update_manager.UpdateError("OFFLINE", "offline fixture")

    result = jtt_update.run_update_cli(
        "check", current_facts={"version": "0.3.0rc2"}, installation=installation,
        fetch_json=offline, output=output, error=error, root=root,
    )
    assert result == 1
    assert "OFFLINE" in error.getvalue()
    assert "No files changed" in output.getvalue()
    assert not root.exists()
    assert launcher.read_bytes() == original


def test_bootstrap_candidate_failure_leaves_no_launcher_or_partial_candidate(tmp_path, monkeypatch):
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    source = tmp_path / "tag-checkout"
    source.mkdir(mode=0o700)
    home = tmp_path / "home"
    release = _release(tag="v0.3.0-rc2", version="0.3.0rc2", sha="b" * 40)
    monkeypatch.setattr(update_manager, "verify_bootstrap", lambda *_args: release)

    with pytest.raises(jtt_update.JTTUpdateError, match="failed validation"):
        jtt_update.install_release_checkout(
            source, fetch_json=lambda _path: [_published(release.tag, True)],
            confirm=lambda _prompt: "yes", output=io.StringIO(), home=home,
            python=sys_executable(), path_lookup=lambda _name: None,
            install_source=_fake_installer, verify_package=lambda *_args: None,
            run_version=lambda *_args, **_kwargs: False,
        )
    bin_dir = home / ".local" / "bin"
    assert not (bin_dir / "jtt").exists()
    assert not list(bin_dir.glob(".jtt-install-*.tmp"))


def test_rollback_refuses_when_active_launcher_identity_changed(tmp_path, monkeypatch):
    launcher = _managed_launcher(tmp_path)
    original = launcher.read_bytes()
    store = tmp_path / "data"
    store.mkdir(mode=0o700)
    installation = {
        "managed": True, "kind": "official", "launcher": launcher,
        "version": "0.3.1rc0", "commit_sha": "a" * 40, "site": store / "new" / "site",
    }
    monkeypatch.setattr(jtt_update, "inspect_official_launcher", lambda **_kwargs: {"managed": False})
    with pytest.raises(jtt_update.JTTUpdateError, match="identity changed"):
        jtt_update.rollback_to_previous(installation, root=store, current_version="0.3.1rc0")
    assert launcher.read_bytes() == original
