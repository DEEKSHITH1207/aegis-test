"""Response oracles and the verdicts built on them."""

from __future__ import annotations

from apps.testing import evaluation, oracles
from apps.testing.models import (
    FindingCategory,
    FindingConfidence,
    TestCaseStatus,
    TestCaseType,
)

# --- Conflicts -----------------------------------------------------------------------


def test_a_conflict_is_a_409_or_a_4xx_that_says_so() -> None:
    assert oracles.is_conflict(409, None)
    assert oracles.is_conflict(400, {"message": "Financial year already exists"})
    assert oracles.is_conflict(422, {"errors": [{"detail": "Duplicate entry for name"}]})
    assert not oracles.is_conflict(400, {"message": ["name should not be empty"]})
    assert not oracles.is_conflict(500, {"message": "duplicate key value violates unique"})
    assert not oracles.is_conflict(None, None)


# --- Echo ------------------------------------------------------------------------------


def test_harmless_normalisations_are_not_mismatches() -> None:
    sent = {"name": " MT ", "amount": 10.5, "starts": "2026-01-01", "email": "A@X.COM"}
    received = {
        "id": 1,
        "name": "MT",
        "amount": "10.50",
        "starts": "2026-01-01T00:00:00.000Z",
        "email": "a@x.com",
    }

    assert oracles.echo_mismatches(sent, received) == []


def test_a_changed_value_is_a_mismatch_even_inside_an_envelope() -> None:
    sent = {"name": "MT", "starts": "2026-01-01", "address": {"city": "Kuppam"}}
    received = {
        "data": {
            "name": "MT",
            "starts": "2025-12-31T18:30:00.000Z",
            "address": {"city": "Chittoor"},
        }
    }

    mismatches = oracles.echo_mismatches(sent, received)

    assert [m.field for m in mismatches] == ["starts", "address.city"]


def test_secrets_and_expanded_references_are_not_compared() -> None:
    sent = {"password": "hunter2", "unit": 3}
    received = {"password": "$2b$10$hash", "unit": {"id": 3, "name": "MT"}}

    assert oracles.echo_mismatches(sent, received) == []


# --- Derived fields ------------------------------------------------------------------


def test_a_label_built_from_an_inverted_range_is_flagged() -> None:
    (problem,) = oracles.derived_field_problems(
        {"startYear": 2030, "endYear": 2020}, {"id": 4, "startYear": 2030, "endYear": 2020, "name": "2030-20"}
    )

    assert problem.field == "name"
    assert "2030 to 2020" in problem.explanation


def test_a_correct_label_is_not_flagged() -> None:
    assert oracles.derived_field_problems(
        {"startYear": 2030, "endYear": 2031}, {"name": "2030-31", "code": "FY2030"}
    ) == []


def test_a_label_naming_a_value_that_was_not_sent_is_flagged() -> None:
    (problem,) = oracles.derived_field_problems(
        {"startYear": 2030, "endYear": 2031}, {"name": "2030-45"}
    )

    assert "45" in problem.explanation


# --- Response schema -------------------------------------------------------------------


def test_schema_problems_name_the_place_and_the_rule() -> None:
    schema = {
        "type": "object",
        "required": ["id", "name"],
        "properties": {
            "id": {"type": "integer"},
            "status": {"type": "string", "enum": ["open", "closed"]},
            "note": {"type": "string", "nullable": True},
            "tags": {"type": "array", "items": {"type": "string"}},
        },
    }

    problems = oracles.schema_problems(
        {"id": "7", "status": "pending", "note": None, "tags": ["a", 2]}, schema
    )

    assert problems == [
        "$.name: required but missing",
        "$.id: expected integer, got string",
        "$.status: 'pending' is not one of ['open', 'closed']",
        "$.tags[1]: expected string, got integer",
    ]


def test_an_unresolved_reference_is_not_held_against_the_response() -> None:
    assert oracles.schema_problems({"x": 1}, {"$ref": "#/components/schemas/Missing"}) == []


# --- Finding what was created -----------------------------------------------------------


def test_ids_and_list_items_are_found_in_common_envelopes() -> None:
    assert oracles.resource_id({"id": 12, "name": "MT"}) == "12"
    assert oracles.resource_id({"data": {"_id": "abc"}}) == "abc"
    assert oracles.resource_id({"name": "MT"}) is None
    # The item route's own parameter name is read first.
    assert oracles.resource_id({"userId": 7, "name": "x"}, "userId") == "7"
    assert oracles.resource_id({"id": 3, "userId": 7}, "userId") == "7"
    assert oracles.resource_id({"id": 3}, "userId") == "3"

    assert oracles.list_items([1, 2]) == [1, 2]
    assert oracles.list_items({"items": [1], "total": 1}) == [1]
    assert oracles.list_items({"meta": {}, "rows": [3]}) == [3]


def test_a_created_item_is_found_by_id_or_else_by_its_values() -> None:
    items = [{"id": 1, "name": "MT"}, {"id": 2, "name": "MT-abc123"}]

    assert oracles.find_created(items, "2", {"name": "x"}) == items[1]
    assert oracles.find_created(items, None, {"name": "MT-abc123"}) == items[1]
    assert oracles.find_created(items, "9", {"name": "MT-abc123"}) is None


# --- Verdicts ---------------------------------------------------------------------------


def _evaluate(status_code, test_type, *, body=None, oracle=None, expected=None):
    return evaluation.evaluate(
        label="POST /years",
        case_name="case",
        test_type=test_type,
        expected_status_code=expected,
        status_code=status_code,
        body=body,
        oracle=oracle,
    )


ORACLE = {
    "concern": "endYear (2020) is before startYear (2030)",
    "flag_acceptance": True,
    "severity": "medium",
    "confidence": "high",
    "probable_cause": "Not cross-checked.",
}


def test_suspicious_input_that_is_accepted_is_reported() -> None:
    verdict = _evaluate(201, TestCaseType.CROSS_FIELD, oracle=ORACLE)

    assert verdict.status == TestCaseStatus.FAILED
    (finding,) = verdict.findings
    assert finding.title == (
        "POST /years: suspicious acceptance - endYear (2020) is before startYear (2030)"
    )
    assert "the record was created" in finding.description
    assert (finding.category, finding.confidence) == (
        FindingCategory.VALIDATION_GAP,
        FindingConfidence.HIGH,
    )


def test_suspicious_input_that_is_rejected_passes() -> None:
    verdict = _evaluate(400, TestCaseType.CROSS_FIELD, oracle=ORACLE)

    assert verdict.status == TestCaseStatus.PASSED


def test_a_probe_that_is_accepted_passes() -> None:
    verdict = _evaluate(201, TestCaseType.SUSPICIOUS_VALUE, oracle={**ORACLE, "flag_acceptance": False})

    assert verdict.status == TestCaseStatus.PASSED


def test_a_collision_with_existing_data_is_not_judged() -> None:
    verdict = _evaluate(
        409, TestCaseType.HAPPY_PATH, body={"message": "Financial year already exists"}, expected=201
    )

    assert verdict.status == TestCaseStatus.ERROR
    assert verdict.findings == ()
    assert "Financial year already exists" in verdict.detail


def test_a_duplicate_create_that_crashes_is_a_crash() -> None:
    verdict = evaluation.duplicate_create("POST /units", 201, 500, None)

    (finding,) = verdict.findings
    assert finding.title == "POST /units: duplicate create returns 500 (unhandled unique constraint)"
    assert finding.category == FindingCategory.CRASH


def test_a_duplicate_refused_or_allowed_passes() -> None:
    assert evaluation.duplicate_create("POST /units", 201, 409, None).status == TestCaseStatus.PASSED
    assert evaluation.duplicate_create("POST /units", 201, 201, None).status == TestCaseStatus.PASSED


def test_an_undocumented_conflict_is_only_a_note() -> None:
    note = evaluation.undocumented_conflict("POST /years", 409, ["201", "400"])

    assert note.category == FindingCategory.SPEC_GAP
    assert note.fails_case is False
    assert evaluation.undocumented_conflict("POST /years", 409, ["201", "409"]) is None


def test_every_2xx_is_checked_for_echo_and_schema() -> None:
    drafts = evaluation.response_findings(
        "POST /units",
        sent={"name": "MT-abc"},
        received={"id": "1", "name": "MT"},
        response_schema={"type": "object", "properties": {"id": {"type": "integer"}}},
        check_echo=True,
    )

    assert [d.category for d in drafts] == [FindingCategory.STATE_BUG, FindingCategory.SPEC_GAP]
