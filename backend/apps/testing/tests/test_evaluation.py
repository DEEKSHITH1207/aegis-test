"""The rules that turn a status code into a verdict and findings."""

from __future__ import annotations

from apps.testing.evaluation import deleted_read_back, evaluate
from apps.testing.models import (
    FindingCategory,
    FindingConfidence,
    FindingSeverity,
    TestCaseStatus,
    TestCaseType,
)


def _evaluate(
    status_code: int,
    *,
    test_type=TestCaseType.BELOW_MINIMUM,
    expected=400,
    body=None,
    guessed_path=False,
    created_resource=False,
):
    return evaluate(
        label="POST /users",
        case_name="below_minimum:age",
        test_type=test_type,
        expected_status_code=expected,
        status_code=status_code,
        body=body,
        guessed_path=guessed_path,
        created_resource=created_resource,
    )


def test_the_documented_rejection_passes() -> None:
    verdict = _evaluate(400)

    assert verdict.status == TestCaseStatus.PASSED
    assert verdict.findings == ()


def test_a_documented_success_passes() -> None:
    verdict = _evaluate(201, test_type=TestCaseType.HAPPY_PATH, expected=201)

    assert verdict.status == TestCaseStatus.PASSED


def test_a_server_error_is_a_high_severity_crash() -> None:
    verdict = _evaluate(500)

    assert verdict.status == TestCaseStatus.FAILED
    (finding,) = verdict.findings
    assert finding.category == FindingCategory.CRASH
    assert finding.confidence == FindingConfidence.HIGH
    assert finding.probable_cause
    assert finding.severity == FindingSeverity.HIGH
    assert "500" in finding.title


def test_a_server_error_is_a_crash_even_with_no_expectation() -> None:
    verdict = _evaluate(503, expected=None)

    assert verdict.status == TestCaseStatus.FAILED
    assert verdict.findings[0].category == FindingCategory.CRASH


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


def test_invalid_input_refused_with_an_undocumented_code_passes_with_a_note() -> None:
    # The API enforces the rule; only the code differs from the document.
    verdict = _evaluate(422)

    assert verdict.status == TestCaseStatus.PASSED
    assert "Rejected with 422" in verdict.detail
    (note,) = verdict.findings
    assert note.category == FindingCategory.SPEC_GAP
    assert note.severity == FindingSeverity.LOW
    assert note.confidence == FindingConfidence.MEDIUM
    assert not note.fails_case
    assert note.title == "POST /users: rejects invalid input with 422, not the documented 400"


def test_a_happy_path_with_an_undocumented_code_is_a_low_spec_gap() -> None:
    verdict = _evaluate(200, test_type=TestCaseType.HAPPY_PATH, expected=201)

    assert verdict.status == TestCaseStatus.FAILED
    assert verdict.detail == "Expected 201, received 200."
    (finding,) = verdict.findings
    assert finding.category == FindingCategory.SPEC_GAP
    assert finding.severity == FindingSeverity.LOW
    assert finding.fails_case


def test_a_404_for_a_sample_path_value_is_inconclusive() -> None:
    # Whatever the case, a 404 for a guessed ID says nothing about the operation.
    for test_type, expected in (
        (TestCaseType.HAPPY_PATH, 200),
        (TestCaseType.BELOW_MINIMUM, 400),
        (TestCaseType.HAPPY_PATH, None),
    ):
        verdict = _evaluate(404, test_type=test_type, expected=expected, guessed_path=True)

        assert verdict.status == TestCaseStatus.ERROR, test_type
        assert verdict.findings == ()
        assert "Inconclusive" in verdict.detail


def test_a_documented_404_passes_even_for_a_sample_path_value() -> None:
    verdict = _evaluate(404, test_type=TestCaseType.HAPPY_PATH, expected=404, guessed_path=True)

    assert verdict.status == TestCaseStatus.PASSED


def test_a_404_for_a_resource_the_case_created_is_a_state_bug() -> None:
    verdict = _evaluate(404, test_type=TestCaseType.HAPPY_PATH, expected=200, created_resource=True)

    assert verdict.status == TestCaseStatus.FAILED
    (finding,) = verdict.findings
    assert finding.category == FindingCategory.STATE_BUG
    assert finding.severity == FindingSeverity.MEDIUM
    assert finding.confidence == FindingConfidence.MEDIUM
    assert "created a moment earlier" in finding.title


def test_a_deleted_resource_that_is_still_readable_is_a_state_bug() -> None:
    verdict = deleted_read_back("DELETE /users/{id}", how="GET /users/{id}", status_code=200, identifier="7")

    assert verdict.status == TestCaseStatus.FAILED
    (finding,) = verdict.findings
    assert finding.category == FindingCategory.STATE_BUG
    assert finding.severity == FindingSeverity.MEDIUM
    assert finding.confidence == FindingConfidence.HIGH
    assert "still readable" in finding.title


def test_a_deleted_resource_that_is_gone_passes_and_a_crash_or_refusal_does_not() -> None:
    assert deleted_read_back("D", how="G", status_code=404, identifier="7").status == TestCaseStatus.PASSED
    assert deleted_read_back("D", how="G", status_code=410, identifier="7").status == TestCaseStatus.PASSED

    crashed = deleted_read_back("D", how="G", status_code=500, identifier="7")
    assert crashed.status == TestCaseStatus.FAILED
    assert crashed.findings[0].category == FindingCategory.CRASH

    assert deleted_read_back("D", how="G", status_code=401, identifier="7").status == TestCaseStatus.ERROR


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
