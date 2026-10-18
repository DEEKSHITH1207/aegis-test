"""The prompt module: what the model reads, and checking what it answers.

Pure functions, so most tests need no database. The briefs are built from
unsaved model instances wired together in memory.
"""

from __future__ import annotations

import json

import pytest

from apps.testing.analysis import prompts
from apps.testing.models import (
    Endpoint,
    Finding,
    ScenarioResult,
    TestCase,
)


def _test_case_finding(finding_id: int = 7, **evidence) -> Finding:
    endpoint = Endpoint(
        id=1,
        path="/users",
        method="POST",
        summary="Create a user",
        request_schema={"type": "object", "properties": {"age": {"type": "integer"}}},
    )
    test_case = TestCase(
        id=2,
        endpoint=endpoint,
        name="POST /users: age below minimum",
        test_type="below_minimum",
        expected_status_code=400,
    )
    return Finding(
        id=finding_id,
        test_case=test_case,
        title="POST /users accepted an age below the minimum",
        severity="medium",
        category="validation_gap",
        evidence=evidence or {"response": {"status_code": 201}},
    )


def _assessment(finding_id: int, **overrides) -> dict:
    return {
        "finding_id": finding_id,
        "verdict": "likely_real",
        "confidence": "high",
        "suggested_severity": "medium",
        "reasoning": "The API answered 201 to an age of 17.",
        "likely_cause": "No minimum check on age.",
        "recommendation": "Validate age >= 18.",
        **overrides,
    }


# --- Briefs --------------------------------------------------------------------


def test_a_test_case_brief_carries_the_endpoint_case_and_evidence() -> None:
    entry = prompts.brief(_test_case_finding())

    assert entry["finding_id"] == 7
    assert entry["source"] == "generated test case"
    assert entry["endpoint"]["method"] == "POST"
    assert entry["endpoint"]["request_schema"]["properties"]["age"] == {"type": "integer"}
    assert entry["test_case"]["type"] == "below_minimum"
    assert entry["test_case"]["expected_status_code"] == 400
    assert entry["evidence"] == {"response": {"status_code": 201}}


def test_a_rules_brief_names_its_scenario() -> None:
    finding = Finding(
        id=9,
        scenario_result=ScenarioResult(id=3, name="leave_balance"),
        title="balance: expected 10, got 12",
        severity="high",
        category="business_rule_violation",
        evidence={"expected": 10, "actual": 12},
    )

    entry = prompts.brief(finding)

    assert entry["source"] == "business rules"
    assert entry["scenario"] == "leave_balance"
    assert "endpoint" not in entry


def test_oversized_evidence_is_cut() -> None:
    entry = prompts.brief(_test_case_finding(body="x" * 20_000))

    assert isinstance(entry["evidence"], str)
    assert entry["evidence"].endswith("[truncated]")
    assert len(entry["evidence"]) < 7_000


def test_the_prompt_is_deterministic_and_lists_every_id() -> None:
    briefs = [prompts.brief(_test_case_finding(7)), prompts.brief(_test_case_finding(8))]
    context = {"name": "Nightly", "results": {"failed": 2}}

    first = prompts.assessment_prompt(context, briefs, include_summary=False)
    second = prompts.assessment_prompt(context, briefs, include_summary=False)

    assert first == second
    assert "(7, 8)" in first
    assert 'write "summary"' not in first
    assert 'write "summary"' in prompts.assessment_prompt(context, briefs, include_summary=True)


# --- Schemas -------------------------------------------------------------------


def _objects(schema):
    if isinstance(schema, dict):
        if schema.get("type") == "object":
            yield schema
        for value in schema.values():
            yield from _objects(value)
    elif isinstance(schema, list):
        for value in schema:
            yield from _objects(value)


@pytest.mark.parametrize(
    "schema",
    [
        prompts.ASSESSMENTS_SCHEMA,
        prompts.SUMMARY_SCHEMA,
        prompts.ASSESSMENTS_AND_SUMMARY_SCHEMA,
    ],
)
def test_every_schema_object_is_closed_and_fully_required(schema) -> None:
    # Structured outputs need additionalProperties: false and every property
    # listed as required.
    for obj in _objects(schema):
        assert obj["additionalProperties"] is False
        assert set(obj["required"]) == set(obj["properties"])


# --- Parsing assessments -------------------------------------------------------


def test_a_well_formed_reply_is_parsed() -> None:
    reply = json.dumps({"assessments": [_assessment(7), _assessment(8, verdict="uncertain")]})

    parsed = prompts.parse_assessments(reply, sent_ids=[7, 8], include_summary=False)

    assert [a.finding_id for a in parsed.assessments] == [7, 8]
    assert parsed.assessments[1].verdict == "uncertain"
    assert parsed.warnings == []


def test_an_assessment_of_a_finding_that_was_not_sent_is_dropped() -> None:
    reply = json.dumps({"assessments": [_assessment(7), _assessment(99)]})

    parsed = prompts.parse_assessments(reply, sent_ids=[7], include_summary=False)

    assert [a.finding_id for a in parsed.assessments] == [7]
    assert any("99" in warning and "not sent" in warning for warning in parsed.warnings)


def test_a_duplicate_keeps_the_first_and_a_missing_one_is_reported() -> None:
    reply = json.dumps(
        {"assessments": [_assessment(7), _assessment(7, verdict="uncertain")]}
    )

    parsed = prompts.parse_assessments(reply, sent_ids=[7, 8], include_summary=False)

    assert len(parsed.assessments) == 1
    assert parsed.assessments[0].verdict == "likely_real"
    assert any("twice" in warning for warning in parsed.warnings)
    assert any("did not assess finding(s) 8" in warning for warning in parsed.warnings)


@pytest.mark.parametrize(
    "bad",
    [
        {"verdict": "definitely"},
        {"confidence": "total"},
        {"suggested_severity": "catastrophic"},
        {"reasoning": "   "},
        {"finding_id": "7"},
        {"finding_id": True},
    ],
)
def test_a_malformed_assessment_is_ignored(bad) -> None:
    reply = json.dumps({"assessments": [{**_assessment(7), **bad}]})

    parsed = prompts.parse_assessments(reply, sent_ids=[7], include_summary=False)

    assert parsed.assessments == []
    assert any("malformed" in warning for warning in parsed.warnings)


@pytest.mark.parametrize("text", ["not json", "[]", '{"other": 1}'])
def test_an_unusable_reply_is_an_error(text: str) -> None:
    with pytest.raises(prompts.ReplyError):
        prompts.parse_assessments(text, sent_ids=[7], include_summary=False)


# --- Parsing the summary -------------------------------------------------------


def _group(ids, title="No validation on POST /users") -> dict:
    return {
        "title": title,
        "finding_ids": ids,
        "explanation": "Only presence is checked.",
        "recommendation": "Validate the body against the schema.",
        "severity": "high",
    }


def test_a_group_keeps_only_assessed_findings() -> None:
    data = {"summary": "One real problem.", "groups": [_group([7, 99, 7])]}

    parsed = prompts.parse_summary(json.dumps(data), assessed_ids=[7, 8])

    assert parsed.summary == "One real problem."
    assert parsed.groups[0].finding_ids == (7,)
    assert any("99" in warning for warning in parsed.warnings)


def test_a_group_citing_nothing_assessed_is_dropped() -> None:
    data = {"summary": "s", "groups": [_group([99])]}

    parsed = prompts.parse_summary(json.dumps(data), assessed_ids=[7])

    assert parsed.groups == []
    assert any("dropped" in warning for warning in parsed.warnings)


def test_a_combined_reply_carries_assessments_and_summary() -> None:
    data = {
        "assessments": [_assessment(7)],
        "summary": "All real.",
        "groups": [_group([7, 8])],
    }

    parsed = prompts.parse_assessments(json.dumps(data), sent_ids=[7, 8], include_summary=True)

    # 8 was sent but not assessed, so a group may not cite it.
    assert parsed.summary == "All real."
    assert parsed.groups[0].finding_ids == (7,)


def test_a_reply_without_a_summary_is_an_error_when_one_was_asked_for() -> None:
    with pytest.raises(prompts.ReplyError):
        prompts.parse_summary(json.dumps({"groups": []}), assessed_ids=[7])


def test_the_prompt_module_imports_nothing_that_can_reach_the_network() -> None:
    import ast
    from pathlib import Path

    tree = ast.parse(Path(prompts.__file__).read_text())
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])

    assert not imported & {"anthropic", "httpx", "httpx2", "requests", "urllib", "socket"}
