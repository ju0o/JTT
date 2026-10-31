"""Platform-neutral tests for Windows diagnostic evidence helpers."""

import json

import pytest

from test_windows_terminal_backend import (
    _R02_ADDITIONAL_AB_SCENARIOS,
    _R02_BASELINE_INPUT_SCENARIOS,
    _R02_BASELINE_INPUT_GROUPS,
    _R02_DIRECT_WORKER_COMPARE_SCENARIOS,
    _R02_INPUT_CASE_PLANS,
    _R02_PATH_FACTOR_SCENARIOS,
    _R02_UNICODE_FACTOR_SCENARIOS,
    _append_r02_evidence_record,
    _conpty_command_template,
    _conpty_powershell_error_categories,
    _resolve_r02_evidence_path,
    _r02_failure_categories,
)


def test_original_comparison_scenarios_remain_and_new_controls_are_isolated():
    assert _R02_BASELINE_INPUT_SCENARIOS == (
        "baseline",
        "unicode_first",
        "ascii_then_unicode",
        "ascii_then_unicode_per_key",
        "ascii_then_unicode_crlf",
        "unicode_output_only",
        "path_standalone_single_write",
        "path_standalone_single_write_crlf",
        "path_after_ascii_single_write",
        "path_after_ascii_per_key",
    )
    assert _R02_ADDITIONAL_AB_SCENARIOS == (
        "short_path_change_standalone",
        "script_path_change_standalone",
    )
    assert "baseline" in _R02_BASELINE_INPUT_SCENARIOS
    assert "short_ascii_standalone" not in _R02_ADDITIONAL_AB_SCENARIOS
    assert not set(_R02_BASELINE_INPUT_SCENARIOS).intersection(
        _R02_ADDITIONAL_AB_SCENARIOS
    )
    assert tuple(
        name for name in _R02_INPUT_CASE_PLANS
        if name in _R02_BASELINE_INPUT_SCENARIOS
    ) == _R02_BASELINE_INPUT_SCENARIOS
    assert {
        name: _R02_INPUT_CASE_PLANS[name]
        for name in _R02_BASELINE_INPUT_SCENARIOS
    } == {
        "baseline": (
            "SHORT_ASCII", "LONG_ASCII", "MULTI_STEP",
            "UNICODE_INPUT", "PATH_CHANGE",
        ),
        "unicode_first": ("UNICODE_INPUT",),
        "ascii_then_unicode": ("SHORT_ASCII", "UNICODE_INPUT"),
        "ascii_then_unicode_per_key": ("SHORT_ASCII", "UNICODE_INPUT"),
        "ascii_then_unicode_crlf": ("SHORT_ASCII", "UNICODE_INPUT"),
        "unicode_output_only": ("UNICODE_OUTPUT_ONLY",),
        "path_standalone_single_write": ("PATH_CHANGE",),
        "path_standalone_single_write_crlf": ("PATH_CHANGE",),
        "path_after_ascii_single_write": ("SHORT_ASCII", "PATH_CHANGE"),
        "path_after_ascii_per_key": ("SHORT_ASCII", "PATH_CHANGE"),
    }
    assert {
        name: _R02_INPUT_CASE_PLANS[name]
        for name in _R02_ADDITIONAL_AB_SCENARIOS
    } == {
        "short_path_change_standalone": ("SHORT_PATH_CHANGE",),
        "script_path_change_standalone": ("SCRIPT_PATH_CHANGE",),
    }


def test_comparison_groups_partition_all_scenarios_and_keep_factors_together():
    group_names = tuple(name for name, _members in _R02_BASELINE_INPUT_GROUPS)
    flattened = tuple(
        scenario
        for _name, members in _R02_BASELINE_INPUT_GROUPS
        for scenario in members
    )
    assert group_names == ("baseline", "unicode", "path")
    assert flattened == _R02_BASELINE_INPUT_SCENARIOS
    assert len(flattened) == len(set(flattened))

    membership = {
        scenario: name
        for name, members in _R02_BASELINE_INPUT_GROUPS
        for scenario in members
    }
    assert {membership[name] for name in _R02_UNICODE_FACTOR_SCENARIOS} == {
        "unicode"
    }
    assert {membership[name] for name in _R02_PATH_FACTOR_SCENARIOS} == {"path"}
    assert {
        name for name, members in _R02_BASELINE_INPUT_GROUPS
        if set(scenario for scenario, _label in _R02_DIRECT_WORKER_COMPARE_SCENARIOS)
        & set(members)
    } == {"unicode", "path"}
    assert _R02_DIRECT_WORKER_COMPARE_SCENARIOS == (
        ("unicode_first", "UNICODE_INPUT"),
        ("ascii_then_unicode", "UNICODE_INPUT"),
        ("unicode_output_only", "UNICODE_OUTPUT_ONLY"),
        ("path_standalone_single_write", "PATH_CHANGE"),
    )


def test_powershell_error_categories_distinguish_policy_and_parser_errors():
    policy_error = "\n".join((
        "& : File C:\\test\\probe.ps1 cannot be loaded because running scripts is disabled on this system.",
        "At line:1 char:1",
        "+ & '.\\probe.ps1'",
        "+ CategoryInfo          : SecurityError: (:) [], PSSecurityException",
        "+ FullyQualifiedErrorId : UnauthorizedAccess",
    ))
    parser_error = "\n".join((
        "At line:1 char:8",
        "+ Set-Location 'C:\\unterminated",
        "+ CategoryInfo          : ParserError: (:) [], ParentContainsErrorRecordException",
        "+ FullyQualifiedErrorId : TerminatorExpectedAtEndOfString",
    ))

    policy_categories = _conpty_powershell_error_categories(policy_error)
    parser_categories = _conpty_powershell_error_categories(parser_error)

    assert "PSSecurityException" in policy_categories
    assert "ExecutionPolicy" in policy_categories
    assert "UnauthorizedAccessException" in policy_categories
    assert "ParserError" not in policy_categories
    assert parser_categories == ["ParserError", "FullyQualifiedErrorId", "PowerShellErrorRecord"]
    assert "ExecutionPolicy" not in parser_categories


def test_powershell_error_categories_ignore_error_words_in_command_echo():
    normal_echo_and_output = "\n".join((
        "PS C:\\test> Write-Output 'PSSecurityException ParserError running scripts is disabled'",
        "PSSecurityException ParserError running scripts is disabled",
        "PS C:\\test>",
    ))
    multiline_echo = "\n".join((
        "PS C:\\test> Write-Output `",
        ">> 'UnauthorizedAccessException CommandNotFoundException'",
        "normal output",
        "PS C:\\test>",
    ))

    assert _conpty_powershell_error_categories(normal_echo_and_output) == []
    assert _conpty_powershell_error_categories(multiline_echo) == []


def test_powershell_error_categories_recognize_other_execution_errors():
    command_not_found = "+ CategoryInfo          : ObjectNotFound: (jtt_missing:String) [], CommandNotFoundException"
    access_denied = "+ CategoryInfo          : PermissionDenied: (C:\\test\\private.txt:FileInfo) [Get-Content], UnauthorizedAccessException"

    assert "CommandNotFoundException" in _conpty_powershell_error_categories(
        command_not_found
    )
    assert "UnauthorizedAccessException" in _conpty_powershell_error_categories(
        access_denied
    )


def test_r02_failure_categories_preserve_simultaneous_failure_sources():
    categories = _r02_failure_categories(
        product_command="command_execution_unproven",
        diagnostic_evidence="PermissionError",
        cleanup="STOP_ERROR:AccessDenied",
    )

    assert categories == {
        "product_command": "command_execution_unproven",
        "diagnostic_evidence": "PermissionError",
        "cleanup": "STOP_ERROR:AccessDenied",
    }


def test_command_template_hash_input_normalizes_test_root_and_nonce():
    first = _conpty_command_template(
        "Set-Location 'C:\\temp\\one\\work'; Write-Output 'N1'",
        r"C:\temp\one\work",
        "N1",
    )
    second = _conpty_command_template(
        "Set-Location 'D:\\runner\\two\\work'; Write-Output 'N2'",
        r"D:\runner\two\work",
        "N2",
    )

    assert first == second
    assert first == "Set-Location '<JTT_TEST_START>'; Write-Output '<NONCE>'"


def test_jsonl_evidence_path_stays_anchored_after_chdir(tmp_path, monkeypatch):
    root = tmp_path / "runner"
    root.mkdir()
    (root / "test-results").mkdir()
    evidence_path = _resolve_r02_evidence_path(
        "test-results/r02-input-factor.jsonl", root
    )
    other = tmp_path / "different-working-directory"
    other.mkdir()
    monkeypatch.chdir(other)

    _append_r02_evidence_record(
        evidence_path, root, {"stage": "variant_start", "ordinal": 1}
    )

    records = [json.loads(line) for line in evidence_path.read_text().splitlines()]
    assert records == [{"stage": "variant_start", "ordinal": 1}]
    assert not (other / "test-results" / "r02-input-factor.jsonl").exists()


def test_jsonl_evidence_path_rejects_directory_and_file_symlinks(tmp_path):
    root = tmp_path / "runner"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "r02-input-factor.jsonl").write_text("outside\n")
    (root / "test-results").symlink_to(outside, target_is_directory=True)
    with pytest.raises(OSError):
        _resolve_r02_evidence_path("test-results/r02-input-factor.jsonl", root)

    (root / "test-results").unlink()
    safe_results = root / "test-results"
    safe_results.mkdir()
    (safe_results / "r02-input-factor.jsonl").symlink_to(
        outside / "r02-input-factor.jsonl"
    )
    with pytest.raises(OSError):
        _resolve_r02_evidence_path("test-results/r02-input-factor.jsonl", root)


def test_jsonl_evidence_path_rejects_directory_replacement_before_append(tmp_path):
    root = tmp_path / "runner"
    root.mkdir()
    results = root / "test-results"
    results.mkdir()
    evidence_path = _resolve_r02_evidence_path(
        "test-results/r02-input-factor.jsonl", root
    )
    outside = tmp_path / "outside"
    outside.mkdir()
    results.rmdir()
    results.symlink_to(outside, target_is_directory=True)

    with pytest.raises(OSError):
        _append_r02_evidence_record(
            evidence_path, root, {"stage": "must_not_escape"}
        )
    assert not (outside / "r02-input-factor.jsonl").exists()
