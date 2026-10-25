"""Exercise the CI status gate with synthetic job results."""

import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
GATE_SCRIPT = ROOT / ".github" / "scripts" / "ci_candidate_gate.py"
WORKFLOWS = ROOT / ".github" / "workflows"


def _run_gate(tmp_path, state, result, head_sha="abc123", checkout_sha="def456"):
    summary = tmp_path / "summary.md"
    env = os.environ.copy()
    env["GITHUB_STEP_SUMMARY"] = str(summary)
    completed = subprocess.run(
        [sys.executable, str(GATE_SCRIPT), state, result, head_sha, checkout_sha],
        cwd=str(ROOT),
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    return completed, summary.read_text(encoding="utf-8")


def test_draft_gate_is_successful_but_explicitly_not_tested(tmp_path):
    completed, summary = _run_gate(tmp_path, "draft", "skipped")

    assert completed.returncode == 0
    assert "NOT RUN (Draft)" in summary
    assert "not a test pass" in summary
    assert "PR HEAD: abc123" in summary
    assert "Checked-out SHA: def456" in summary
    assert "VERIFIED PASS" not in summary


def test_candidate_gate_accepts_only_success(tmp_path):
    completed, summary = _run_gate(tmp_path, "candidate", "success")

    assert completed.returncode == 0
    assert "VERIFIED PASS" in summary
    assert "PR HEAD: abc123" in summary
    assert "Checked-out SHA: def456" in summary


def test_candidate_gate_fails_closed_for_failed_skipped_or_cancelled_jobs(tmp_path):
    for result in ("failure", "skipped", "cancelled", "timed_out"):
        completed, summary = _run_gate(tmp_path, "candidate", result)

        assert completed.returncode == 1
        assert "Validation state: FAILED" in summary
        assert f"Required test job result: {result}" in summary


def test_linux_and_windows_candidate_jobs_are_draft_gated_and_have_summary_gates():
    linux = (WORKFLOWS / "ci.yml").read_text(encoding="utf-8")
    windows = (WORKFLOWS / "windows-feasibility.yml").read_text(encoding="utf-8")
    candidate_events = "types: [opened, synchronize, reopened, ready_for_review, converted_to_draft]"

    assert candidate_events in linux
    assert candidate_events in windows
    assert 'python-version: ["3.9", "3.11", "3.13"]' in linux
    assert "if: github.event_name != 'pull_request' || github.event.pull_request.draft == false" in linux
    assert "if: github.event.pull_request.draft == false" in windows
    assert "needs: [test]" in linux
    assert "needs: [conpty]" in windows
    assert "Linux candidate verification status' }}" in linux
    assert "Windows candidate verification status' }}" in windows
    assert "Linux draft validation (NOT RUN)" in linux
    assert "Windows draft validation (NOT RUN)" in windows
    assert "name: Candidate verification status" not in linux + windows
    expected_gate = 'ci_candidate_gate.py candidate "$TEST_RESULT" "$PR_HEAD_SHA" "$CHECKOUT_SHA"'
    assert expected_gate in linux
    assert expected_gate in windows


def test_windows_required_workflow_has_no_path_filter_or_soft_failure():
    windows = (WORKFLOWS / "windows-feasibility.yml").read_text(encoding="utf-8")

    assert "pull_request:\n    types:" in windows
    assert "    paths:" not in windows
    assert "continue-on-error" not in windows


def test_concurrency_cancels_only_pull_request_runs():
    group = "${{ github.workflow }}-${{ github.event_name }}-${{ github.event.pull_request.number || github.ref }}"
    cancel = "${{ github.event_name == 'pull_request' }}"

    for name in ("ci.yml", "windows-feasibility.yml"):
        workflow = (WORKFLOWS / name).read_text(encoding="utf-8")
        assert group in workflow
        assert f"cancel-in-progress: {cancel}" in workflow


def test_main_linux_push_and_release_separation_are_preserved():
    linux = (WORKFLOWS / "ci.yml").read_text(encoding="utf-8")

    assert "push:\n    branches: [main]" in linux
    assert "workflow_dispatch:" not in linux
    assert "release:" not in linux
