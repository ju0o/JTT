"""The JTT-only wheel uses a non-overlapping Python import namespace."""

import os
from pathlib import Path
import shutil
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]


def _isolated_package(tmp_path):
    site = tmp_path / "site"
    site.mkdir()
    shutil.copytree(ROOT / "src" / "tmux_agent_tower", site / "ju0_terminal_tower")
    return site


def _run(site, code):
    env = {
        "PATH": os.defpath,
        "PYTHONPATH": str(site),
        "PYTHONNOUSERSITE": "1",
    }
    return subprocess.run(
        [sys.executable, "-S", "-c", code],
        capture_output=True,
        text=True,
        env=env,
        check=False,
        timeout=10,
    )


def test_candidate_package_alias_loads_shared_modules_under_one_identity(tmp_path):
    site = _isolated_package(tmp_path)
    result = _run(
        site,
        "import ju0_terminal_tower; import tmux_agent_tower; "
        "assert ju0_terminal_tower is tmux_agent_tower; "
        "from tmux_agent_tower.jtt import cli; "
        "assert callable(cli); assert ju0_terminal_tower.__version__ == '0.3.1rc0'",
    )
    assert result.returncode == 0, result.stderr


def test_candidate_worker_bootstraps_renamed_namespace_without_starting_a_session(tmp_path):
    site = _isolated_package(tmp_path)
    worker = site / "ju0_terminal_tower" / "terminal" / "_worker.py"
    env = {
        "PATH": os.defpath,
        "PYTHONNOUSERSITE": "1",
    }
    result = subprocess.run(
        [sys.executable, "-S", str(worker)],
        capture_output=True,
        text=True,
        env=env,
        check=False,
        timeout=10,
    )
    # The worker imports the renamed package, pyte probe, and PTY engine, then
    # rejects the deliberately incomplete argv before creating any session.
    assert result.returncode == 2
    assert result.stderr == ""


def test_candidate_runtime_refuses_same_environment_as_legacy_distribution(tmp_path):
    site = _isolated_package(tmp_path)
    legacy_dist = site / "tmux_agent_tower-0.3.0rc3.dist-info"
    legacy_dist.mkdir()
    (legacy_dist / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: tmux-agent-tower\nVersion: 0.3.0rc3\n\n",
        encoding="utf-8",
    )

    result = _run(site, "import ju0_terminal_tower")
    assert result.returncode != 0
    assert "must use separate Python environments" in result.stderr


def test_candidate_runtime_refuses_foreign_import_path_before_aliasing(tmp_path):
    site = _isolated_package(tmp_path)
    legacy = site / "tmux_agent_tower"
    legacy.mkdir()
    (legacy / "__init__.py").write_text("__version__ = '0.3.0rc3'\n", encoding="utf-8")

    result = _run(site, "import ju0_terminal_tower")
    assert result.returncode != 0
    assert "another tmux_agent_tower source is visible" in result.stderr


def test_package_manager_install_refuses_self_update_and_rollback(tmp_path):
    site = _isolated_package(tmp_path)
    dist = site / "ju0_terminal_tower-0.3.1rc0.dist-info"
    dist.mkdir()
    (dist / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: ju0-terminal-tower\nVersion: 0.3.1rc0\n\n",
        encoding="utf-8",
    )

    result = _run(
        site,
        "import ju0_terminal_tower; from tmux_agent_tower.jtt import cli; "
        "assert cli(['update','--check']) == 2; "
        "assert cli(['rollback','--dry-run']) == 2",
    )
    assert result.returncode == 0, result.stderr
    assert result.stderr.count("PACKAGE_MANAGER_OWNED") == 2
