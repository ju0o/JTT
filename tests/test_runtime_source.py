"""The dev launcher and doctor report the checkout that was imported."""

import os
import subprocess
from pathlib import Path

import pytest

from tmux_agent_tower.main import cli, inspect_path_tower, runtime_facts, tree_kind


def test_runtime_facts_point_at_this_checkout():
    facts = runtime_facts()
    root = Path(__file__).resolve().parents[1]
    assert facts["source"].startswith(str(root / "src"))
    assert facts["source"].endswith("tmux_agent_tower/__init__.py")
    assert facts["python"]
    assert facts["version"]
    assert facts["mismatch"] is False
    assert facts["tree"] == "dev"


def test_runtime_facts_flag_a_foreign_dev_root(monkeypatch):
    monkeypatch.setenv("TOWER_DEV_ROOT", "/tmp/not-this-checkout")
    assert runtime_facts()["mismatch"] is True


def test_packaged_install_is_not_labeled_as_the_working_tree():
    assert tree_kind("/usr/lib/python3/site-packages/tmux_agent_tower/__init__.py") == "packaged"
    assert tree_kind("/opt/checkout/src/tmux_agent_tower/__init__.py") == "dev"


def test_dev_launcher_pins_this_checkout():
    script = Path(__file__).resolve().parents[1] / "scripts" / "dev-python"
    text = script.read_text(encoding="utf-8")
    assert "TOWER_DEV_ROOT" in text
    assert 'PYTHONPATH="$ROOT/src' in text
    assert ".venv/bin/python" in text
    assert os.access(script, os.X_OK)


def test_repo_launcher_uses_dev_python_and_not_path_tower():
    script = Path(__file__).resolve().parents[1] / "scripts" / "tower"
    text = script.read_text(encoding="utf-8")
    assert "dev-python" in text
    assert "-m tmux_agent_tower.main" in text
    assert "which" not in text
    assert os.access(script, os.X_OK)


def test_jtt_has_separate_entrypoint_dev_guard_and_pyte_license_notice():
    root = Path(__file__).resolve().parents[1]
    project = (root / "pyproject.toml").read_text(encoding="utf-8")
    assert 'tower = "tmux_agent_tower.main:cli"' in project
    assert 'jtt = "tmux_agent_tower.jtt:cli"' in project
    assert 'pyte>=0.8.2,<0.9' in project
    assert '"share/licenses/tmux-agent-tower" = ["licenses/pyte-LGPL-3.0.txt"]' in project
    assert (root / "licenses" / "pyte-LGPL-3.0.txt").is_file()

    dev = (root / "scripts" / "jtt-dev").read_text(encoding="utf-8")
    setup = (root / "scripts" / "jtt-setup-dev").read_text(encoding="utf-8")
    assert 'venv_dir="$repo_dir/.venv-jtt"' in dev
    assert 'python_bin="$venv_dir/bin/python"' in dev
    assert "pyte" in dev and "exec \"$python_bin\" -m tmux_agent_tower.jtt" in dev
    assert "pip install --editable" in setup
    assert "패키지 인덱스" in setup
    assert "pip install" not in dev


def test_path_tower_from_another_checkout_is_not_called_the_same(tmp_path):
    script = tmp_path / "tower"
    script.write_text("#!/usr/bin/python3\n", encoding="utf-8")
    seen = {}

    def runner(python):
        seen["python"] = python
        return "/other/checkout/src/tmux_agent_tower/__init__.py"

    report = inspect_path_tower(which=str(script), runner=runner)
    assert report["found"] is True
    assert report["same"] is False
    assert seen["python"] == "/usr/bin/python3"


def test_path_tower_matching_this_import_is_the_same_checkout(tmp_path):
    script = tmp_path / "tower"
    script.write_text("#!/usr/bin/python3\n", encoding="utf-8")
    report = inspect_path_tower(which=str(script), runner=lambda _python: runtime_facts()["source"])
    assert report["same"] is True


def test_foreign_dev_root_refuses_to_start(monkeypatch, capsys):
    monkeypatch.setenv("TOWER_DEV_ROOT", "/tmp/not-this-checkout")
    with pytest.raises(SystemExit) as raised:
        cli(["--version"])
    assert raised.value.code == 1
    assert "Source mismatch" in capsys.readouterr().err


def test_launcher_doctor_stays_on_this_checkout_across_restarts():
    root = Path(__file__).resolve().parents[1]
    script = root / "scripts" / "tower"
    sources = []
    for _ in range(3):
        completed = subprocess.run(
            [str(script), "--doctor"],
            capture_output=True,
            text=True,
            cwd=root,
            timeout=20,
            check=False,
        )
        line = next(item for item in completed.stdout.splitlines() if item.startswith("Tower source:"))
        sources.append(line.split(": ", 1)[1])
    assert len(set(sources)) == 1
    assert sources[0].startswith(str(root / "src"))
    assert "OTHER CHECKOUT" in completed.stdout or "PATH tower:" in completed.stdout
