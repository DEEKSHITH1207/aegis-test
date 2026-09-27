"""The rules that turn a status code into a verdict and findings."""

from __future__ import annotations

from apps.testing.evaluation import evaluate
from apps.testing.models import (
    FindingCategory,
    FindingSeverity,
    TestCaseStatus,
    TestCaseType,
)


def _evaluate(status_code: int, *, test_type=TestCaseType.BELOW_MINIMUM, expected=400):
    return evaluate(
        label="POST /users",
        case_name="below_minimum:age",
        test_type=test_type,
        expected_status_code=expected,
        status_code=status_code,
    )


def test_the_documented_rejection_passes() -> None:
    verdict = _evaluate(400)

    assert verdict.status == TestCaseStatus.PASSED
    assert verdict.findings == ()


def test_a_documented_success_passes() -> None:
    verdict = _evaluate(201, test_type=TestCaseType.HAPPY_PATH, expected=201)

    assert verdict.status == TestCaseStatus.PASSED


def test_a_server_error_is_a_high_severity_bug() -> None:
    verdict = _evaluate(500)

    assert verdict.status == TestCaseStatus.FAILED
    (finding,) = verdict.findings
    assert finding.category == FindingCategory.BUG
    assert finding.severity == FindingSeverity.HIGH
    assert "500" in finding.title


def test_a_server_error_is_a_bug_even_with_no_expectation() -> None:
    verdict = _evaluate(503, expected=None)

    assert verdict.status == TestCaseStatus.FAILED
    assert verdict.findings[0].category == FindingCategory.BUG


def test_invalid_input_accepted_is_a_validation_gap() -> None:
    verdict = _evaluate(201)

    assert verdict.status == TestCaseStatus.FAILED
    (finding,) = verdict.findings
    assert finding.category == FindingCategory.VALIDATION_GAP
    assert finding.severity == FindingSeverity.MEDIUM


def test_invalid_input_accepted_is_a_gap_even_without_a_documented_rejection() -> None:
    # The schema forbids the input whether or not the document lists a 4xx.
    verdict = _evaluate(200, expected=None)

    assert verdict.findings[0].category == FindingCategory.VALIDATION_GAP


def test_a_different_client_error_is_a_low_inconsistency() -> None:
    verdict = _evaluate(422)

    assert verdict.status == TestCaseStatus.FAILED
    assert verdict.detail == "Expected 400, received 422."
    (finding,) = verdict.findings
    assert finding.category == FindingCategory.INCONSISTENCY
    assert finding.severity == FindingSeverity.LOW


def test_a_happy_path_404_warns_about_sample_data() -> None:
    verdict = _evaluate(404, test_type=TestCaseType.HAPPY_PATH, expected=200)

    assert verdict.status == TestCaseStatus.FAILED
    assert "false positive" in verdict.findings[0].description


def test_being_refused_access_is_not_a_verdict() -> None:
    for status_code in (401, 403, 429):
        verdict = _evaluate(status_code)

        assert verdict.status == TestCaseStatus.ERROR
        assert verdict.findings == ()


def test_access_refusal_that_was_documented_passes() -> None:
    assert _evaluate(401, expected=401).status == TestCaseStatus.PASSED


def test_no_expectation_asserts_nothing() -> None:
    verdict = _evaluate(404, test_type=TestCaseType.HAPPY_PATH, expected=None)

    assert verdict.status == TestCaseStatus.PASSED
    assert "documents no code" in verdict.detail
