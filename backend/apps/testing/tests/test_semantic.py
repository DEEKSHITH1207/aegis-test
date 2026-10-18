"""Fresh test data, semantic cases and sequence cases (apps.testing.semantic)."""

from __future__ import annotations

from apps.testing import semantic
from apps.testing.generator import ExpectedStatus
from apps.testing.models import Endpoint, FindingSeverity, TestCaseType

YEAR = {"type": "integer", "minimum": 2000, "maximum": 2100}
FINANCIAL_YEAR = {
    "type": "object",
    "required": ["startYear", "endYear"],
    "properties": {
        "startYear": {**YEAR, "example": 2025},
        "endYear": {**YEAR, "example": 2026},
    },
}
UNIT = {
    "type": "object",
    "required": ["name"],
    "properties": {"name": {"type": "string", "example": "MT"}, "parentId": {"type": "integer"}},
}


def _endpoint(schema, *, method="POST", path="/things") -> Endpoint:
    return Endpoint(method=method, path=path, parameters=[], request_schema=schema)


def _cases(schema, *, seed="seed", expected=ExpectedStatus(success=201, client_error=400), **kw):
    built = semantic.build_test_cases(_endpoint(schema, **kw), expected=expected, seed=seed)
    return {d.name: d for d in built.definitions}


# --- Names ----------------------------------------------------------------------


def test_unique_looking_fields_are_recognised() -> None:
    for name in ("name", "unitName", "code", "startYear", "email", "user_name", "SKU"):
        assert semantic.looks_unique(name), name
    for name in ("unitId", "parent_id", "description", "age", "active"):
        assert not semantic.looks_unique(name), name


def test_range_pairs_are_found_by_whole_words() -> None:
    properties = {
        "startYear": YEAR,
        "endYear": YEAR,
        "dateFrom": {"type": "string", "format": "date"},
        "dateTo": {"type": "string", "format": "date"},
        "minPrice": {"type": "number"},
        "maxPrice": {"type": "number"},
        "firstName": {"type": "string"},
        "lastName": {"type": "string"},
        "total": {"type": "number"},
        "photo": {"type": "string"},
    }

    pairs = {(p.start, p.end): p for p in semantic.range_pairs(properties)}

    assert set(pairs) == {("startYear", "endYear"), ("dateFrom", "dateTo"), ("minPrice", "maxPrice")}
    assert pairs[("minPrice", "maxPrice")].bound_only
    assert pairs[("dateFrom", "dateTo")].kind == "date"


def test_a_pair_of_different_kinds_is_not_a_range() -> None:
    properties = {"start": {"type": "string", "format": "date"}, "end": YEAR}

    assert semantic.range_pairs(properties) == []


# --- Fresh values ------------------------------------------------------------------


def test_a_unique_name_gets_a_fresh_suffix_and_an_id_is_kept() -> None:
    fresh = semantic.freshen({"name": "MT", "parentId": 7}, UNIT, "abc123")

    assert fresh == {"name": "MT-abc123", "parentId": 7}


def test_a_year_range_moves_and_keeps_its_width() -> None:
    fresh = semantic.freshen({"startYear": 2025, "endYear": 2026}, FINANCIAL_YEAR, "abc123")

    assert fresh["startYear"] != 2025
    assert fresh["endYear"] == fresh["startYear"] + 1
    assert 2000 <= fresh["startYear"] and fresh["endYear"] <= 2100


def test_fresh_values_are_reproducible_and_differ_between_tokens() -> None:
    body = {"startYear": 2025, "endYear": 2026}

    assert semantic.freshen(body, FINANCIAL_YEAR, "aaa111") == semantic.freshen(
        body, FINANCIAL_YEAR, "aaa111"
    )
    produced = {
        semantic.freshen(body, FINANCIAL_YEAR, semantic.token_for("run", str(n)))["startYear"]
        for n in range(20)
    }
    assert len(produced) > 10


def test_the_field_a_case_targets_is_never_freshened() -> None:
    fresh = semantic.freshen(
        {"startYear": 1999, "endYear": 2026}, FINANCIAL_YEAR, "abc123", skip=("startYear",)
    )

    assert fresh == {"startYear": 1999, "endYear": 2026}


def test_formats_patterns_and_lengths_are_respected() -> None:
    schema = {
        "type": "object",
        "properties": {
            "email": {"type": "string", "format": "email"},
            "code": {"type": "string", "pattern": "^[A-Z]{3}$"},
            "title": {"type": "string", "maxLength": 8},
        },
    }

    fresh = semantic.freshen(
        {"email": "a@example.com", "code": "ABC", "title": "Quarterly"}, schema, "abc123"
    )

    assert fresh["email"] == "aegis.abc123@example.com"
    assert fresh["code"] == "ABC"  # A pattern could reject anything else.
    assert len(fresh["title"]) <= 8 and fresh["title"].endswith("abc123")


def test_a_retry_freshens_every_free_text_field() -> None:
    schema = {"type": "object", "properties": {"colour": {"type": "string"}}}

    retried = semantic.refreshed_input(
        {"path": {}, "query": {}, "headers": {}, "body": {"colour": "red"}}, schema, "abc123"
    )

    assert retried["body"] == {"colour": "red-abc123"}


def test_a_retry_that_would_change_nothing_is_not_offered() -> None:
    schema = {"type": "object", "properties": {"active": {"type": "boolean"}}}

    assert semantic.refreshed_input({"body": {"active": True}}, schema, "abc123") is None


# --- Building cases ----------------------------------------------------------------


def test_no_case_sends_the_documents_example_for_a_unique_field() -> None:
    cases = _cases(UNIT)

    for definition in cases.values():
        body = definition.input_data["body"]
        if isinstance(body, dict) and isinstance(body.get("name"), str):
            assert body["name"] != "MT", definition.name


def test_each_case_gets_its_own_fresh_values() -> None:
    cases = _cases(UNIT)

    names = [
        d.input_data["body"]["name"]
        for d in cases.values()
        if d.test_type in {TestCaseType.HAPPY_PATH, TestCaseType.DUPLICATE_CREATE}
    ]
    assert len(set(names)) == len(names)


def test_without_a_seed_the_examples_are_sent_as_written() -> None:
    cases = _cases(UNIT, seed=None)

    assert cases["happy_path"].input_data["body"]["name"] == "MT"


def test_a_year_range_produces_cross_field_cases() -> None:
    cases = _cases(FINANCIAL_YEAR)
    prefix = "cross_field:startYear/endYear"

    before = cases[f"{prefix}:end_before_start"]
    body = before.input_data["body"]
    assert body["endYear"] < body["startYear"]
    assert 2000 <= body["endYear"] and body["startYear"] <= 2100
    assert before.oracle["concern"] == (
        f"endYear ({body['endYear']}) is before startYear ({body['startYear']})"
    )
    assert before.oracle["severity"] == FindingSeverity.MEDIUM
    assert before.oracle["flag_acceptance"] is True
    assert before.expected_status_code is None

    equal = cases[f"{prefix}:end_equals_start"].input_data["body"]
    assert equal["endYear"] == equal["startYear"]
    far = cases[f"{prefix}:end_far_after_start"].input_data["body"]
    assert far["endYear"] == 2100


def test_a_retry_rebuilds_a_semantic_case_on_fresh_values() -> None:
    """After a collision the case is built again from a retry seed: the
    relation under test survives, the base values do not."""
    endpoint = _endpoint(FINANCIAL_YEAR)
    expected = ExpectedStatus(success=201, client_error=400)
    name = "cross_field:startYear/endYear:end_before_start"
    first = _cases(FINANCIAL_YEAR)[name].input_data["body"]

    retried = semantic.retry_input(endpoint, expected=expected, seed="seed", case_name=name)

    body = retried["body"]
    assert body["endYear"] < body["startYear"]
    assert 2000 <= body["endYear"] and body["startYear"] <= 2100
    assert body != first
    # Deterministic: the same retry for the same run and case; later attempts differ.
    assert semantic.retry_input(endpoint, expected=expected, seed="seed", case_name=name) == retried
    third = semantic.retry_input(endpoint, expected=expected, seed="seed", case_name=name, attempt=3)
    assert third["body"]["endYear"] < third["body"]["startYear"]
    assert third != retried
    assert semantic.retry_input(endpoint, expected=expected, seed="seed", case_name="no_such") is None


def test_a_min_max_pair_is_only_checked_for_inversion() -> None:
    schema = {
        "type": "object",
        "properties": {"minPrice": {"type": "integer"}, "maxPrice": {"type": "integer"}},
    }

    names = [name for name in _cases(schema) if name.startswith("cross_field")]

    assert names == ["cross_field:minPrice/maxPrice:end_before_start"]


def test_amounts_get_negative_zero_and_huge_values_the_schema_allows() -> None:
    schema = {
        "type": "object",
        "properties": {
            "price": {"type": "number"},
            "quantity": {"type": "integer", "minimum": 0},
        },
    }

    cases = _cases(schema)

    assert cases["suspicious_value:price:negative"].input_data["body"]["price"] < 0
    assert "suspicious_value:price:zero" in cases
    assert "suspicious_value:price:huge" in cases
    # quantity's minimum makes a negative value a schema violation, which the
    # generator already covers.
    assert "suspicious_value:quantity:negative" not in cases
    assert cases["suspicious_value:quantity:zero"].oracle["severity"] == FindingSeverity.LOW


def test_dates_get_far_past_far_future_and_a_29_february_that_does_not_exist() -> None:
    schema = {
        "type": "object",
        "properties": {
            "startDate": {"type": "string", "format": "date"},
            "endDate": {"type": "string", "format": "date", "example": "2026-12-31"},
        },
    }

    cases = _cases(schema)

    feb = cases["suspicious_value:startDate:feb_29_non_leap"]
    assert feb.input_data["body"]["startDate"] == "2025-02-29"
    # The other end moves too, so the case is only about the date.
    assert feb.input_data["body"]["endDate"] == "2025-03-01"
    assert "suspicious_value:startDate:far_past" in cases
    assert "suspicious_value:startDate:far_future" not in cases  # It would invert the range.
    assert "suspicious_value:endDate:far_future" in cases


def test_required_names_get_blank_long_and_unicode_values() -> None:
    cases = _cases(UNIT)

    assert cases["suspicious_value:name:empty"].input_data["body"]["name"] == ""
    assert cases["suspicious_value:name:whitespace"].input_data["body"]["name"] == "   "
    long = cases["suspicious_value:name:very_long"]
    assert len(long.input_data["body"]["name"]) == 5000
    assert long.oracle["flag_acceptance"] is False
    assert "名前" in cases["suspicious_value:name:unicode"].input_data["body"]["name"]


def test_sequence_cases_are_made_for_creates_only() -> None:
    created = semantic.build_test_cases(
        _endpoint(UNIT), expected=ExpectedStatus(success=201), seed="s", readable=True
    )
    updated = semantic.build_test_cases(
        _endpoint(UNIT, method="PUT"), expected=ExpectedStatus(success=200), seed="s", readable=True
    )
    unreadable = semantic.build_test_cases(
        _endpoint(UNIT), expected=ExpectedStatus(success=201), seed="s", readable=False
    )

    assert {TestCaseType.DUPLICATE_CREATE, TestCaseType.CREATE_READ_BACK} <= {
        d.test_type for d in created.definitions
    }
    assert TestCaseType.DUPLICATE_CREATE not in {d.test_type for d in updated.definitions}
    assert TestCaseType.CREATE_READ_BACK not in {d.test_type for d in unreadable.definitions}


def test_case_names_point_at_the_field_they_target() -> None:
    assert semantic.case_skip_pointer("below_minimum:profile.age") == ("profile", "age")
    assert semantic.case_skip_pointer("invalid_type:query.limit") is None
    assert semantic.case_skip_pointer("happy_path") is None
