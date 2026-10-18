"""Generator tests.

The generator never touches the database, so these build unsaved Endpoint
instances and need no django_db marker. What each case asserts is not only that
a case was produced, but that its value genuinely violates the rule it names —
a validation case whose input is actually valid would report a bug that is not
there.
"""

from __future__ import annotations

import pytest

from apps.testing import generator
from apps.testing.generator import ExpectedStatus, generate_test_cases
from apps.testing.models import Endpoint, TestCaseType


def endpoint(**overrides) -> Endpoint:
    """An unsaved endpoint. Never saved, so no database is involved."""
    fields = {
        "path": "/users",
        "method": "POST",
        "parameters": [],
        "request_schema": None,
        "response_schema": None,
    }
    fields.update(overrides)
    return Endpoint(**fields)


def body_endpoint(schema: dict, **overrides) -> Endpoint:
    return endpoint(request_schema=schema, **overrides)


def cases_by_name(result) -> dict[str, object]:
    return {definition.name: definition for definition in result.definitions}


def happy_body(result) -> object:
    return cases_by_name(result)["happy_path"].input_data["body"]


# --- Shape and determinism ----------------------------------------------------


def test_input_data_always_has_the_same_four_keys() -> None:
    result = generate_test_cases(endpoint())

    for definition in result.definitions:
        assert set(definition.input_data) == {"path", "query", "headers", "body"}


def test_an_endpoint_with_nothing_declared_still_gets_a_happy_path() -> None:
    result = generate_test_cases(endpoint(method="GET"))

    assert [d.name for d in result.definitions] == ["happy_path"]
    assert result.definitions[0].input_data["body"] is None


def test_generation_is_deterministic() -> None:
    schema = {
        "type": "object",
        "required": ["name", "age"],
        "properties": {
            "name": {"type": "string", "minLength": 3},
            "age": {"type": "integer", "minimum": 18},
        },
    }

    first = generate_test_cases(body_endpoint(schema))
    second = generate_test_cases(body_endpoint(schema))

    assert [(d.name, d.input_data) for d in first.definitions] == [
        (d.name, d.input_data) for d in second.definitions
    ]


def test_case_names_are_unique() -> None:
    from apps.testing.tests.fixtures import REALISTIC_SPECIFICATION

    schema = REALISTIC_SPECIFICATION["paths"]["/users"]["post"]["requestBody"]["content"][
        "application/json"
    ]["schema"]

    names = [d.name for d in generate_test_cases(body_endpoint(schema)).definitions]

    assert len(names) == len(set(names))


# --- Happy-path values for each type ------------------------------------------


@pytest.mark.parametrize(
    "schema, expected",
    [
        ({"type": "string"}, "example"),
        ({"type": "integer"}, 1),
        ({"type": "number"}, 1.0),
        ({"type": "boolean"}, True),
        ({"type": "array", "items": {"type": "string"}}, ["example"]),
        ({"type": "object", "properties": {}}, {}),
        ({"type": "null"}, None),
    ],
)
def test_each_basic_type_gets_a_deterministic_value(schema: dict, expected) -> None:
    result = generate_test_cases(
        body_endpoint({"type": "object", "properties": {"field": schema}})
    )

    assert happy_body(result)["field"] == expected


@pytest.mark.parametrize(
    "fmt, expected",
    [
        ("email", "user@example.com"),
        ("date", "2026-01-01"),
        ("date-time", "2026-01-01T00:00:00Z"),
        ("uuid", "00000000-0000-4000-8000-000000000000"),
        ("uri", "https://example.com"),
    ],
)
def test_string_formats_get_a_value_of_that_format(fmt: str, expected: str) -> None:
    result = generate_test_cases(
        body_endpoint(
            {"type": "object", "properties": {"field": {"type": "string", "format": fmt}}}
        )
    )

    assert happy_body(result)["field"] == expected


def test_the_generated_uuid_is_a_real_uuid() -> None:
    from uuid import UUID

    result = generate_test_cases(
        body_endpoint(
            {"type": "object", "properties": {"id": {"type": "string", "format": "uuid"}}}
        )
    )

    UUID(happy_body(result)["id"])  # must not raise


def test_a_happy_path_string_respects_its_length_bounds() -> None:
    result = generate_test_cases(
        body_endpoint(
            {
                "type": "object",
                "properties": {
                    "long": {"type": "string", "minLength": 20},
                    "short": {"type": "string", "maxLength": 3},
                },
            }
        )
    )

    body = happy_body(result)
    assert len(body["long"]) >= 20
    assert len(body["short"]) <= 3


def test_a_happy_path_number_respects_its_bounds() -> None:
    result = generate_test_cases(
        body_endpoint(
            {
                "type": "object",
                "properties": {
                    "age": {"type": "integer", "minimum": 18},
                    "capped": {"type": "integer", "maximum": -5},
                    "exclusive": {"type": "number", "exclusiveMinimum": 10},
                },
            }
        )
    )

    body = happy_body(result)
    assert body["age"] >= 18
    assert body["capped"] <= -5
    assert body["exclusive"] > 10


def test_an_enum_happy_path_uses_an_allowed_value() -> None:
    result = generate_test_cases(
        body_endpoint(
            {
                "type": "object",
                "properties": {"role": {"type": "string", "enum": ["admin", "member"]}},
            }
        )
    )

    assert happy_body(result)["role"] == "admin"


def test_const_and_example_win_over_a_synthesised_value() -> None:
    result = generate_test_cases(
        body_endpoint(
            {
                "type": "object",
                "properties": {
                    "kind": {"const": "fixed"},
                    "name": {"type": "string", "example": "Ada"},
                    "tier": {"type": "string", "default": "free"},
                },
            }
        )
    )

    body = happy_body(result)
    assert body["kind"] == "fixed"
    assert body["name"] == "Ada"
    assert body["tier"] == "free"


def test_a_nested_object_is_built_out() -> None:
    result = generate_test_cases(
        body_endpoint(
            {
                "type": "object",
                "properties": {
                    "profile": {
                        "type": "object",
                        "required": ["name"],
                        "properties": {"name": {"type": "string"}},
                    }
                },
            }
        )
    )

    assert happy_body(result)["profile"] == {"name": "example"}


def test_an_array_honours_min_items() -> None:
    result = generate_test_cases(
        body_endpoint(
            {
                "type": "object",
                "properties": {
                    "tags": {"type": "array", "items": {"type": "string"}, "minItems": 3}
                },
            }
        )
    )

    assert happy_body(result)["tags"] == ["example", "example", "example"]


# --- Required fields ----------------------------------------------------------


def test_each_required_field_gets_its_own_omission_case() -> None:
    result = generate_test_cases(
        body_endpoint(
            {
                "type": "object",
                "required": ["name", "email"],
                "properties": {
                    "name": {"type": "string"},
                    "email": {"type": "string", "format": "email"},
                },
            }
        )
    )

    by_name = cases_by_name(result)
    assert "missing_required:name" in by_name
    assert "missing_required:email" in by_name

    missing_name = by_name["missing_required:name"].input_data["body"]
    assert "name" not in missing_name
    # Exactly one rule is broken: everything else is still valid.
    assert missing_name["email"] == "user@example.com"


def test_a_nested_required_field_gets_its_own_case() -> None:
    result = generate_test_cases(
        body_endpoint(
            {
                "type": "object",
                "properties": {
                    "profile": {
                        "type": "object",
                        "required": ["displayName"],
                        "properties": {"displayName": {"type": "string"}},
                    }
                },
            }
        )
    )

    case = cases_by_name(result)["missing_required:profile.displayName"]
    assert case.input_data["body"]["profile"] == {}


def test_no_case_omits_more_than_one_field_at_a_time() -> None:
    schema = {
        "type": "object",
        "required": ["a", "b", "c"],
        "properties": {name: {"type": "string"} for name in "abc"},
    }

    result = generate_test_cases(body_endpoint(schema))

    for definition in result.definitions:
        if definition.test_type == TestCaseType.MISSING_REQUIRED:
            assert len(definition.input_data["body"]) == 2


def test_a_required_field_also_gets_a_null_case() -> None:
    result = generate_test_cases(
        body_endpoint(
            {"type": "object", "required": ["name"], "properties": {"name": {"type": "string"}}}
        )
    )

    case = cases_by_name(result)["empty_value:name"]
    assert case.test_type == TestCaseType.EMPTY_VALUE
    assert case.input_data["body"] == {"name": None}


def test_a_nullable_required_field_gets_no_null_case() -> None:
    # null is allowed here, so a case sending null would not be a violation.
    result = generate_test_cases(
        body_endpoint(
            {
                "type": "object",
                "required": ["name"],
                "properties": {"name": {"type": "string", "nullable": True}},
            }
        )
    )

    assert "empty_value:name" not in cases_by_name(result)


# --- String constraints -------------------------------------------------------


def test_min_length_violation_is_actually_too_short() -> None:
    result = generate_test_cases(
        body_endpoint(
            {"type": "object", "properties": {"name": {"type": "string", "minLength": 5}}}
        )
    )

    value = cases_by_name(result)["below_min_length:name"].input_data["body"]["name"]
    assert len(value) < 5


def test_max_length_violation_is_actually_too_long() -> None:
    result = generate_test_cases(
        body_endpoint(
            {"type": "object", "properties": {"name": {"type": "string", "maxLength": 5}}}
        )
    )

    value = cases_by_name(result)["above_max_length:name"].input_data["body"]["name"]
    assert len(value) > 5


def test_a_min_length_of_zero_produces_no_violation() -> None:
    # Nothing is shorter than an empty string, so there is no case to make.
    result = generate_test_cases(
        body_endpoint(
            {"type": "object", "properties": {"name": {"type": "string", "minLength": 0}}}
        )
    )

    assert "below_min_length:name" not in cases_by_name(result)


@pytest.mark.parametrize(
    "fmt, invalid",
    [
        ("email", "not-an-email"),
        ("date", "not-a-date"),
        ("date-time", "not-a-date-time"),
        ("uuid", "not-a-uuid"),
    ],
)
def test_format_violations_are_malformed_but_still_strings(fmt: str, invalid: str) -> None:
    result = generate_test_cases(
        body_endpoint(
            {"type": "object", "properties": {"field": {"type": "string", "format": fmt}}}
        )
    )

    value = cases_by_name(result)["invalid_format:field"].input_data["body"]["field"]
    assert value == invalid
    # A non-string would violate the type as well, confusing the diagnosis.
    assert isinstance(value, str)


def test_a_pattern_produces_a_warning_rather_than_a_guess() -> None:
    result = generate_test_cases(
        body_endpoint(
            {"type": "object", "properties": {"code": {"type": "string", "pattern": "^[A-Z]+$"}}}
        )
    )

    assert not any(d.name.startswith("invalid_pattern") for d in result.definitions)
    assert any("pattern" in warning for warning in result.warnings)


# --- Numeric constraints ------------------------------------------------------


def test_minimum_violation_is_below_the_minimum() -> None:
    result = generate_test_cases(
        body_endpoint(
            {"type": "object", "properties": {"age": {"type": "integer", "minimum": 18}}}
        )
    )

    assert cases_by_name(result)["below_minimum:age"].input_data["body"]["age"] == 17


def test_maximum_violation_is_above_the_maximum() -> None:
    result = generate_test_cases(
        body_endpoint(
            {"type": "object", "properties": {"age": {"type": "integer", "maximum": 100}}}
        )
    )

    assert cases_by_name(result)["above_maximum:age"].input_data["body"]["age"] == 101


def test_numeric_exclusive_bounds_use_the_bound_itself() -> None:
    # JSON Schema 2020-12 spelling: the value must be strictly greater, so the
    # bound itself is the smallest invalid value.
    result = generate_test_cases(
        body_endpoint(
            {
                "type": "object",
                "properties": {
                    "score": {"type": "number", "exclusiveMinimum": 0, "exclusiveMaximum": 100}
                },
            }
        )
    )

    by_name = cases_by_name(result)
    assert by_name["below_minimum:score"].input_data["body"]["score"] == 0
    assert by_name["above_maximum:score"].input_data["body"]["score"] == 100


def test_boolean_exclusive_bounds_use_the_bound_itself() -> None:
    # OpenAPI 3.0 spelling: exclusiveMinimum is a flag on minimum.
    result = generate_test_cases(
        body_endpoint(
            {
                "type": "object",
                "properties": {
                    "score": {
                        "type": "integer",
                        "minimum": 10,
                        "exclusiveMinimum": True,
                        "maximum": 20,
                        "exclusiveMaximum": True,
                    }
                },
            }
        )
    )

    by_name = cases_by_name(result)
    assert by_name["below_minimum:score"].input_data["body"]["score"] == 10
    assert by_name["above_maximum:score"].input_data["body"]["score"] == 20


def test_a_non_exclusive_flag_still_means_the_ordinary_bound() -> None:
    result = generate_test_cases(
        body_endpoint(
            {
                "type": "object",
                "properties": {
                    "n": {"type": "integer", "minimum": 5, "exclusiveMinimum": False}
                },
            }
        )
    )

    assert cases_by_name(result)["below_minimum:n"].input_data["body"]["n"] == 4


# --- Enums and types ----------------------------------------------------------


def test_the_invalid_enum_value_is_outside_the_enum() -> None:
    allowed = ["active", "inactive"]

    result = generate_test_cases(
        body_endpoint(
            {"type": "object", "properties": {"status": {"type": "string", "enum": allowed}}}
        )
    )

    value = cases_by_name(result)["invalid_enum:status"].input_data["body"]["status"]
    assert value not in allowed


def test_a_numeric_enum_gets_a_numeric_value_outside_it() -> None:
    result = generate_test_cases(
        body_endpoint({"type": "object", "properties": {"level": {"enum": [1, 2, 3]}}})
    )

    value = cases_by_name(result)["invalid_enum:level"].input_data["body"]["level"]
    assert value not in [1, 2, 3]
    assert isinstance(value, int)


def test_an_enum_gets_no_separate_type_case() -> None:
    # The enum already constrains the value; a type case would be redundant.
    result = generate_test_cases(
        body_endpoint(
            {"type": "object", "properties": {"status": {"type": "string", "enum": ["a", "b"]}}}
        )
    )

    assert "invalid_type:status" not in cases_by_name(result)


@pytest.mark.parametrize(
    "declared, invalid_is",
    [
        ("string", int),
        ("integer", str),
        ("number", str),
        ("boolean", str),
        ("object", str),
    ],
)
def test_the_invalid_type_value_really_is_the_wrong_type(declared: str, invalid_is) -> None:
    result = generate_test_cases(
        body_endpoint({"type": "object", "properties": {"field": {"type": declared}}})
    )

    value = cases_by_name(result)["invalid_type:field"].input_data["body"]["field"]
    assert isinstance(value, invalid_is)


def test_only_one_invalid_type_case_is_produced_per_field() -> None:
    result = generate_test_cases(
        body_endpoint(
            {
                "type": "object",
                "properties": {
                    "age": {"type": "integer", "minimum": 1, "maximum": 9},
                    "name": {"type": "string", "minLength": 2},
                },
            }
        )
    )

    type_case_names = sorted(
        d.name for d in result.definitions if d.test_type == TestCaseType.INVALID_TYPE
    )
    # One for each field, plus one for the body as a whole. Never two for the
    # same target, however many other constraints it declares.
    assert type_case_names == ["invalid_type:age", "invalid_type:body", "invalid_type:name"]


def test_the_body_itself_gets_a_type_case() -> None:
    result = generate_test_cases(
        body_endpoint({"type": "object", "properties": {"name": {"type": "string"}}})
    )

    assert cases_by_name(result)["invalid_type:body"].input_data["body"] == "not-an-object"


# --- Arrays -------------------------------------------------------------------


def test_an_array_gets_a_wrong_item_type_case() -> None:
    result = generate_test_cases(
        body_endpoint(
            {
                "type": "object",
                "properties": {"tags": {"type": "array", "items": {"type": "string"}}},
            }
        )
    )

    value = cases_by_name(result)["invalid_type:tags"].input_data["body"]["tags"]
    assert value[0] == "example"
    assert not isinstance(value[1], str)


def test_array_size_violations() -> None:
    result = generate_test_cases(
        body_endpoint(
            {
                "type": "object",
                "properties": {
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 2,
                        "maxItems": 4,
                    }
                },
            }
        )
    )

    by_name = cases_by_name(result)
    assert len(by_name["below_min_length:tags"].input_data["body"]["tags"]) < 2
    assert len(by_name["above_max_length:tags"].input_data["body"]["tags"]) > 4


# --- Parameters ---------------------------------------------------------------


def test_query_parameters_are_generated() -> None:
    result = generate_test_cases(
        endpoint(
            method="GET",
            parameters=[
                {"name": "page", "in": "query", "schema": {"type": "integer", "minimum": 1}},
                {
                    "name": "status",
                    "in": "query",
                    "schema": {"type": "string", "enum": ["active"]},
                },
            ],
        )
    )

    by_name = cases_by_name(result)
    assert by_name["happy_path"].input_data["query"] == {"page": 1, "status": "active"}
    assert by_name["below_minimum:query.page"].input_data["query"]["page"] == 0
    assert by_name["invalid_enum:query.status"].input_data["query"]["status"] != "active"


def test_a_required_query_parameter_gets_an_omission_case() -> None:
    result = generate_test_cases(
        endpoint(
            method="GET",
            parameters=[
                {"name": "limit", "in": "query", "required": True, "schema": {"type": "integer"}},
                {"name": "page", "in": "query", "required": False, "schema": {"type": "integer"}},
            ],
        )
    )

    by_name = cases_by_name(result)
    assert "limit" not in by_name["missing_required:query.limit"].input_data["query"]
    # An optional parameter cannot be "missing".
    assert "missing_required:query.page" not in by_name


def test_path_parameters_are_generated() -> None:
    result = generate_test_cases(
        endpoint(
            path="/users/{userId}",
            method="GET",
            parameters=[
                {
                    "name": "userId",
                    "in": "path",
                    "required": True,
                    "schema": {"type": "integer", "minimum": 1},
                }
            ],
        )
    )

    by_name = cases_by_name(result)
    assert by_name["happy_path"].input_data["path"] == {"userId": 1}
    assert isinstance(by_name["invalid_type:path.userId"].input_data["path"]["userId"], str)


def test_a_path_parameter_is_never_omitted() -> None:
    # The URL would no longer address this operation, so the result would say
    # nothing about it.
    result = generate_test_cases(
        endpoint(
            path="/users/{userId}",
            method="GET",
            parameters=[{"name": "userId", "in": "path", "required": True, "schema": {"type": "integer"}}],
        )
    )

    assert "missing_required:path.userId" not in cases_by_name(result)


def test_header_parameters_land_in_the_headers_bucket() -> None:
    result = generate_test_cases(
        endpoint(
            method="GET",
            parameters=[
                {"name": "X-Tenant", "in": "header", "required": True, "schema": {"type": "string"}}
            ],
        )
    )

    assert cases_by_name(result)["happy_path"].input_data["headers"] == {"X-Tenant": "example"}


def test_swagger_2_style_inline_parameter_types_are_understood() -> None:
    # Swagger 2 puts the keywords on the parameter itself, not under "schema".
    result = generate_test_cases(
        endpoint(
            method="GET",
            parameters=[{"name": "page", "in": "query", "type": "integer", "minimum": 1}],
        )
    )

    by_name = cases_by_name(result)
    assert by_name["happy_path"].input_data["query"] == {"page": 1}
    assert by_name["below_minimum:query.page"].input_data["query"]["page"] == 0


def test_a_cookie_parameter_is_reported_rather_than_guessed() -> None:
    result = generate_test_cases(
        endpoint(
            method="GET",
            parameters=[{"name": "session", "in": "cookie", "schema": {"type": "string"}}],
        )
    )

    assert any("cookie" in warning for warning in result.warnings)


# --- Expected status codes ----------------------------------------------------


def test_the_happy_path_uses_the_lowest_documented_2xx() -> None:
    expected = ExpectedStatus.from_codes(["500", "201", "200", "400"])

    result = generate_test_cases(endpoint(), expected=expected)

    assert cases_by_name(result)["happy_path"].expected_status_code == 200


def test_invalid_cases_prefer_a_documented_400() -> None:
    expected = ExpectedStatus.from_codes(["200", "400", "422"])

    result = generate_test_cases(
        body_endpoint(
            {"type": "object", "required": ["name"], "properties": {"name": {"type": "string"}}}
        ),
        expected=expected,
    )

    assert cases_by_name(result)["missing_required:name"].expected_status_code == 400


def test_a_documented_422_is_used_when_there_is_no_400() -> None:
    assert ExpectedStatus.from_codes(["200", "422"]).client_error == 422


def test_another_documented_4xx_is_used_when_there_is_neither() -> None:
    assert ExpectedStatus.from_codes(["200", "409"]).client_error == 409


def test_no_documented_client_error_means_no_expectation() -> None:
    # Assuming 400 here would manufacture failures against an API that never
    # promised one.
    expected = ExpectedStatus.from_codes(["200"])

    result = generate_test_cases(
        body_endpoint(
            {"type": "object", "required": ["name"], "properties": {"name": {"type": "string"}}}
        ),
        expected=expected,
    )

    assert cases_by_name(result)["missing_required:name"].expected_status_code is None


def test_no_documented_success_means_no_expectation() -> None:
    result = generate_test_cases(endpoint(), expected=ExpectedStatus.from_codes(["404"]))

    assert cases_by_name(result)["happy_path"].expected_status_code is None


def test_non_numeric_response_keys_are_ignored() -> None:
    assert ExpectedStatus.from_codes(["default", "2XX"]) == ExpectedStatus(None, None)


# --- Unsupported schemas ------------------------------------------------------


def test_an_unresolved_ref_body_yields_no_cases_and_a_warning() -> None:
    # Phase 3 leaves circular and external references in place; a body cannot be
    # built from one, and a wrong body would produce a meaningless failure.
    result = generate_test_cases(body_endpoint({"$ref": "#/components/schemas/Node"}))

    assert result.is_empty
    assert any("unresolved reference" in warning for warning in result.warnings)


def test_an_unresolved_ref_property_is_omitted_not_fatal() -> None:
    result = generate_test_cases(
        body_endpoint(
            {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "node": {"$ref": "#/components/schemas/Node"},
                },
            }
        )
    )

    body = happy_body(result)
    assert body == {"name": "example"}
    assert any("node" in warning for warning in result.warnings)


def test_a_one_of_takes_the_first_branch_and_says_so() -> None:
    result = generate_test_cases(
        body_endpoint(
            {
                "type": "object",
                "properties": {
                    "value": {"oneOf": [{"type": "string"}, {"type": "integer"}]}
                },
            }
        )
    )

    assert happy_body(result)["value"] == "example"
    assert any("oneOf" in warning for warning in result.warnings)


def test_an_all_of_is_merged() -> None:
    result = generate_test_cases(
        body_endpoint(
            {
                "allOf": [
                    {"type": "object", "properties": {"a": {"type": "string"}}},
                    {"type": "object", "properties": {"b": {"type": "integer"}}},
                ]
            }
        )
    )

    assert happy_body(result) == {"a": "example", "b": 1}


def test_a_not_schema_is_unsupported_rather_than_crashing() -> None:
    result = generate_test_cases(body_endpoint({"not": {"type": "string"}}))

    assert result.is_empty
    assert any("not" in warning for warning in result.warnings)


@pytest.mark.parametrize(
    "schema",
    [
        {},
        {"type": "object"},
        {"type": "object", "properties": {}},
        {"type": "object", "properties": {"x": None}},
        {"type": "object", "required": "not-a-list", "properties": {}},
        {"type": "array"},
        {"type": "unknown-type"},
        {"type": ["string", "null"]},
        {"type": "object", "properties": {"deep": {"type": "object", "properties": {}}}},
    ],
)
def test_odd_schemas_do_not_crash(schema: dict) -> None:
    generate_test_cases(body_endpoint(schema))  # must not raise


@pytest.mark.parametrize("parameters", [None, "nonsense", [None], [{"in": "query"}], [{}]])
def test_odd_parameter_lists_do_not_crash(parameters) -> None:
    generate_test_cases(endpoint(parameters=parameters))  # must not raise


def test_deeply_nested_objects_are_bounded() -> None:
    schema: dict = {"type": "string"}
    for _ in range(20):
        schema = {"type": "object", "properties": {"child": schema}}

    result = generate_test_cases(body_endpoint(schema))

    assert result.definitions  # produced something, and returned


# --- Safety -------------------------------------------------------------------


def test_the_generator_imports_nothing_that_can_reach_the_network() -> None:
    """Phase 4 creates records only; sending a request belongs to Phase 5."""
    import ast
    from pathlib import Path

    tree = ast.parse(Path(generator.__file__).read_text())

    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)

    # Compare on the root package so "urllib.request" is caught by "urllib".
    roots = {name.split(".", 1)[0] for name in imported}
    assert roots.isdisjoint(
        {"httpx", "requests", "aiohttp", "urllib", "socket", "http", "ftplib", "telnetlib"}
    )
