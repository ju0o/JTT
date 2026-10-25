"""Guard the GitHub Actions draft/candidate policy without extra dependencies."""

from pathlib import Path


WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"


def _read(name):
    return (WORKFLOWS / name).read_text(encoding="utf-8")


def test_linux_matrix_is_preserved_for_main_and_non_draft_candidates():
    workflow = _read("ci.yml")

    assert "push:\n    branches: [main]" in workflow
    assert 'python-version: ["3.9", "3.11", "3.13"]' in workflow
    assert "if: github.event_name != 'pull_request' || github.event.pull_request.draft == false" in workflow
    assert "needs: [test]" in workflow
    assert 'TEST_RESULT: ${{ needs.test.result }}' in workflow
    assert 'if [[ "$TEST_RESULT" != "success" ]]' in workflow


def test_draft_and_ready_pull_request_events_are_explicit():
    expected = "types: [opened, synchronize, reopened, ready_for_review, converted_to_draft]"
    assert expected in _read("ci.yml")
    assert expected in _read("windows-feasibility.yml")


def test_draft_status_is_explicitly_not_a_test_pass():
    for name in ("ci.yml", "windows-feasibility.yml"):
        workflow = _read(name)
        assert "Validation state: NOT RUN (Draft)" in workflow
        assert "This status is not evidence" in workflow


def test_windows_candidate_check_cannot_be_path_filtered_or_soft_failed():
    workflow = _read("windows-feasibility.yml")

    assert "pull_request:\n    types:" in workflow
    assert "    paths:" not in workflow
    assert "if: github.event.pull_request.draft == false" in workflow
    assert "needs: [conpty]" in workflow
    assert 'TEST_RESULT: ${{ needs.conpty.result }}' in workflow
    assert 'if [[ "$TEST_RESULT" != "success" ]]' in workflow
    assert "continue-on-error" not in workflow


def test_concurrency_cancels_only_pull_request_runs():
    expected_group = "${{ github.workflow }}-${{ github.event_name }}-${{ github.event.pull_request.number || github.ref }}"
    expected_cancel = "${{ github.event_name == 'pull_request' }}"

    for name in ("ci.yml", "windows-feasibility.yml"):
        workflow = _read(name)
        assert expected_group in workflow
        assert f"cancel-in-progress: {expected_cancel}" in workflow
