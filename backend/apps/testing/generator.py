"""Deterministic test-case generation from an endpoint's schema.

Given an Endpoint, this produces the definitions of the test cases that should
be run against it. It is pure: it reads the endpoint's fields, touches no
database, and — by construction — sends no HTTP request. Executing the cases it
describes belongs to a later phase.

Two rules shape everything here:

*Deterministic.* The same schema always yields the same cases, with the same
names and the same values. Nothing is random and nothing depends on the clock,
so a re-generation can be compared against the previous one and a failure can be
reproduced exactly.

*One violation per case.* An invalid case breaks exactly one rule and is
otherwise a valid request. A body that is missing a field *and* has a malformed
email cannot tell you which of the two the API rejected.

``input_data`` always has the same four keys, so a consumer never has to test
for their presence::

    {"path": {...}, "query": {...}, "headers": {...}, "body": <any> | null}
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

from apps.testing.models import Endpoint, TestCase, TestCaseType

# How far into nested objects violations are generated. Deep schemas would
# otherwise produce a combinatorial number of cases for diminishing value.
_MAX_DEPTH = 4

_NAME_MAX_LENGTH = TestCase._meta.get_field("name").max_length

# The request components a case can carry. Body is separate: it is any JSON
# value, not a mapping of names.
_PARAMETER_LOCATIONS = {"path": "path", "query": "query", "header": "headers"}

# Deterministic stand-ins for the string formats AegisTest understands. A value
# here must genuinely satisfy its format: it is used in happy-path requests.
_FORMAT_EXAMPLES = {
    "email": "user@example.com",
    "idn-email": "user@example.com",
    "date": "2026-01-01",
    "date-time": "2026-01-01T00:00:00Z",
    "time": "00:00:00Z",
    "duration": "P1D",
    "uuid": "00000000-0000-4000-8000-000000000000",
    "uri": "https://example.com",
    "uri-reference": "/example",
    "url": "https://example.com",
    "hostname": "example.com",
    # Reserved documentation ranges, so a value can never reach a real host.
    "ipv4": "192.0.2.1",
    "ipv6": "2001:db8::1",
    "byte": "ZXhhbXBsZQ==",
    "binary": "example",
    "password": "example",
}

# Values that break a format while staying a string, so the case isolates the
# format rule rather than also violating the type.
_FORMAT_VIOLATIONS = {
    "email": "not-an-email",
    "idn-email": "not-an-email",
    "date": "not-a-date",
    "date-time": "not-a-date-time",
    "time": "not-a-time",
    "duration": "not-a-duration",
    "uuid": "not-a-uuid",
    "uri": "not a uri",
    "url": "not a url",
    "hostname": "not a hostname",
    "ipv4": "not-an-ipv4",
    "ipv6": "not-an-ipv6",
    "byte": "not-base64!",
}

_DEFAULT_STRING = "example"


@dataclass(frozen=True)
class ExpectedStatus:
    """The status codes a specification documents for one operation.

    Either may be None, which means the document says nothing and AegisTest
    therefore asserts nothing.
    """

    success: int | None = None
    client_error: int | None = None

    @classmethod
    def from_codes(cls, codes: list[str]) -> ExpectedStatus:
        """Choose the codes to assert from those an operation documents.

        Success is the lowest documented 2xx. For a rejected request the
        preference is 400, then 422, then the lowest other 4xx — never a guess:
        an API that documents no client error gets no expectation at all.
        """
        numeric = sorted({int(code) for code in codes if isinstance(code, str) and code.isdigit()})

        success = next((code for code in numeric if 200 <= code < 300), None)

        client_error = None
        if 400 in numeric:
            client_error = 400
        elif 422 in numeric:
            client_error = 422
        else:
            client_error = next((code for code in numeric if 400 <= code < 500), None)

        return cls(success=success, client_error=client_error)


@dataclass(frozen=True)
class ParameterCase:
    """A declared parameter: its happy-path value, its schema, and whether it
    may be omitted."""

    value: Any
    schema: dict[str, Any]
    required: bool


@dataclass(frozen=True)
class TestCaseDefinition:
    """One test case, before it is persisted."""

    name: str
    test_type: str
    description: str
    input_data: dict[str, Any]
    expected_status_code: int | None = None
    # For semantic cases: why accepting the request would be suspicious.
    oracle: dict[str, Any] = field(default_factory=dict)


@dataclass
class GeneratedTests:
    definitions: list[TestCaseDefinition] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not self.definitions


def generate_test_cases(
    endpoint: Endpoint, *, expected: ExpectedStatus | None = None
) -> GeneratedTests:
    """Produce the test cases for one endpoint.

    Returns no definitions at all when the endpoint declares a request body
    that cannot be realised — an unresolved `$ref`, or a composition the
    generator does not understand. Sending a request whose body is wrong for a
    reason AegisTest did not intend would produce a failure that means nothing.
    """
    expected = expected or ExpectedStatus()
    warnings: list[str] = []
    label = f"{endpoint.method} {endpoint.path}"

    parameters = _parameter_groups(endpoint.parameters, label, warnings)

    body_schema = endpoint.request_schema if isinstance(endpoint.request_schema, dict) else None
    body: Any = None
    if body_schema:
        if not _is_realisable(body_schema):
            warnings.append(
                f"{label}: request body schema is not supported "
                f"({_unsupported_reason(body_schema)}); no test cases generated."
            )
            return GeneratedTests(warnings=warnings)
        body = _example(body_schema, label, warnings)

    base = {
        location: {name: case.value for name, case in parameters[location].items()}
        for location in ("path", "query", "headers")
    }
    base["body"] = body

    definitions = [
        TestCaseDefinition(
            name="happy_path",
            test_type=TestCaseType.HAPPY_PATH,
            description=f"A valid request to {label}.",
            input_data=deepcopy(base),
            expected_status_code=expected.success,
        )
    ]

    definitions.extend(_parameter_cases(base, parameters, label, expected, warnings))
    if body_schema:
        definitions.extend(_body_cases(base, body_schema, label, expected, warnings))

    return GeneratedTests(definitions=keep_unique_names(definitions, warnings), warnings=warnings)


def keep_unique_names(
    definitions: list[TestCaseDefinition], warnings: list[str]
) -> list[TestCaseDefinition]:
    """Keep names inside the column that stores them, and keep them unique."""
    seen: set[str] = set()
    kept: list[TestCaseDefinition] = []
    for definition in definitions:
        name = definition.name
        if len(name) > _NAME_MAX_LENGTH:
            name = name[:_NAME_MAX_LENGTH]
        if name in seen:
            warnings.append(f"Dropped a duplicate test case name: {name}")
            continue
        seen.add(name)
        kept.append(
            definition if name == definition.name else _replace_name(definition, name)
        )
    return kept


def _replace_name(definition: TestCaseDefinition, name: str) -> TestCaseDefinition:
    return TestCaseDefinition(
        name=name,
        test_type=definition.test_type,
        description=definition.description,
        input_data=definition.input_data,
        expected_status_code=definition.expected_status_code,
        oracle=definition.oracle,
    )


# --- Parameters ---------------------------------------------------------------


def _parameter_groups(
    parameters: Any, label: str, warnings: list[str]
) -> dict[str, dict[str, ParameterCase]]:
    """Bucket an operation's parameters by location, with a value for each.

    Every declared parameter is included in the happy path, optional ones too:
    a request that exercises a documented filter is more useful than one that
    ignores it, and leaving them out would mean never generating a case for
    their constraints.
    """
    groups: dict[str, dict[str, ParameterCase]] = {"path": {}, "query": {}, "headers": {}}
    if not isinstance(parameters, list):
        return groups

    for parameter in parameters:
        if not isinstance(parameter, dict):
            continue
        name = parameter.get("name")
        location = parameter.get("in")
        if not isinstance(name, str) or not name:
            continue
        if location not in _PARAMETER_LOCATIONS:
            if location == "cookie":
                warnings.append(f"{label}: cookie parameter '{name}' is not supported.")
            continue

        schema = _parameter_schema(parameter)
        if not _is_realisable(schema):
            warnings.append(
                f"{label}: parameter '{name}' has an unsupported schema "
                f"({_unsupported_reason(schema)}); it is omitted."
            )
            continue

        groups[_PARAMETER_LOCATIONS[location]][name] = ParameterCase(
            value=_example(schema, label, warnings),
            schema=schema,
            # A path parameter is required by definition, whatever it declares.
            required=location == "path" or parameter.get("required") is True,
        )

    return groups


def _parameter_schema(parameter: dict[str, Any]) -> dict[str, Any]:
    """Return a parameter's schema for either specification dialect.

    OpenAPI 3 nests it under 'schema'; Swagger 2 puts the keywords directly on
    the parameter object.
    """
    schema = parameter.get("schema")
    if isinstance(schema, dict):
        return schema

    inline = {
        key: value
        for key, value in parameter.items()
        if key
        in {
            "type",
            "format",
            "enum",
            "default",
            "items",
            "minimum",
            "maximum",
            "exclusiveMinimum",
            "exclusiveMaximum",
            "minLength",
            "maxLength",
            "minItems",
            "maxItems",
            "pattern",
        }
    }
    return inline


def _parameter_cases(
    base: dict[str, Any],
    parameters: dict[str, dict[str, ParameterCase]],
    label: str,
    expected: ExpectedStatus,
    warnings: list[str],
) -> list[TestCaseDefinition]:
    definitions: list[TestCaseDefinition] = []

    for location in ("path", "query", "headers"):
        for name in sorted(parameters[location]):
            case = parameters[location][name]
            target = f"{location}.{name}"

            for test_type, value, reason in _violations(case.schema, label, target, warnings):
                request = deepcopy(base)
                request[location][name] = value
                definitions.append(
                    TestCaseDefinition(
                        name=f"{test_type}:{target}",
                        test_type=test_type,
                        description=f"{label}: {reason} for the {location} parameter '{name}'.",
                        input_data=request,
                        expected_status_code=expected.client_error,
                    )
                )

            # A path parameter cannot be omitted: the URL would not address this
            # operation at all, so the result would say nothing about it.
            if location != "path" and case.required:
                request = deepcopy(base)
                request[location].pop(name, None)
                definitions.append(
                    TestCaseDefinition(
                        name=f"{TestCaseType.MISSING_REQUIRED}:{target}",
                        test_type=TestCaseType.MISSING_REQUIRED,
                        description=(
                            f"{label}: omit the required {location} parameter '{name}'."
                        ),
                        input_data=request,
                        expected_status_code=expected.client_error,
                    )
                )

    return definitions


# --- Request body -------------------------------------------------------------


def _body_cases(
    base: dict[str, Any],
    schema: dict[str, Any],
    label: str,
    expected: ExpectedStatus,
    warnings: list[str],
) -> list[TestCaseDefinition]:
    definitions: list[TestCaseDefinition] = []

    for pointer, subschema in _walk(schema):
        target = ".".join(pointer) if pointer else "body"

        for test_type, value, reason in _violations(subschema, label, target, warnings):
            request = deepcopy(base)
            if not _set_at(request["body"], pointer, value, request):
                continue
            definitions.append(
                TestCaseDefinition(
                    name=f"{test_type}:{target}",
                    test_type=test_type,
                    description=f"{label}: {reason} for '{target}' in the request body.",
                    input_data=request,
                    expected_status_code=expected.client_error,
                )
            )

        definitions.extend(
            _required_field_cases(base, subschema, pointer, label, expected)
        )

    return definitions


def _required_field_cases(
    base: dict[str, Any],
    subschema: dict[str, Any],
    pointer: tuple[str, ...],
    label: str,
    expected: ExpectedStatus,
) -> list[TestCaseDefinition]:
    """One case per required field: omitted, and one per field: present but null."""
    required = subschema.get("required")
    if not isinstance(required, list):
        return []

    properties = subschema.get("properties")
    properties = properties if isinstance(properties, dict) else {}

    definitions: list[TestCaseDefinition] = []
    for name in sorted(item for item in required if isinstance(item, str)):
        child = (*pointer, name)
        target = ".".join(child)

        request = deepcopy(base)
        if _remove_at(request["body"], child):
            definitions.append(
                TestCaseDefinition(
                    name=f"{TestCaseType.MISSING_REQUIRED}:{target}",
                    test_type=TestCaseType.MISSING_REQUIRED,
                    description=f"{label}: omit the required field '{target}'.",
                    input_data=request,
                    expected_status_code=expected.client_error,
                )
            )

        # Null is a distinct violation from absence, and only a violation when
        # the field is not declared nullable.
        child_schema = properties.get(name)
        if isinstance(child_schema, dict) and not _is_nullable(child_schema):
            request = deepcopy(base)
            if _set_at(request["body"], child, None, request):
                definitions.append(
                    TestCaseDefinition(
                        name=f"{TestCaseType.EMPTY_VALUE}:{target}",
                        test_type=TestCaseType.EMPTY_VALUE,
                        description=f"{label}: send null for the required field '{target}'.",
                        input_data=request,
                        expected_status_code=expected.client_error,
                    )
                )

    return definitions


def _walk(
    schema: dict[str, Any], pointer: tuple[str, ...] = (), depth: int = 0
) -> list[tuple[tuple[str, ...], dict[str, Any]]]:
    """Every schema node violations are generated for, outermost first.

    Object properties are followed to a bounded depth. Array items are not:
    a wrong item type is already covered by the array's own invalid-type case,
    and descending would multiply cases without adding diagnoses.
    """
    found = [(pointer, schema)]
    if depth >= _MAX_DEPTH:
        return found

    properties = schema.get("properties")
    if isinstance(properties, dict):
        for name in sorted(key for key in properties if isinstance(key, str)):
            child = properties[name]
            if isinstance(child, dict) and _is_realisable(child):
                found.extend(_walk(child, (*pointer, name), depth + 1))

    return found


# --- Violations ---------------------------------------------------------------


def _violations(
    schema: dict[str, Any], label: str, target: str, warnings: list[str]
) -> list[tuple[str, Any, str]]:
    """Every single-rule violation of one schema node.

    Each entry is the case type, the value that breaks the rule, and a phrase
    describing it. A value here must genuinely violate the schema: a case whose
    input is actually valid would report a bug that does not exist.
    """
    violations: list[tuple[str, Any, str]] = []

    enum = schema.get("enum")
    if isinstance(enum, list) and enum:
        invalid = _outside_enum(enum)
        if invalid is not None:
            violations.append((TestCaseType.INVALID_ENUM, invalid, "a value outside the enum"))
        else:
            warnings.append(
                f"{label}: could not produce a value outside the enum for '{target}'."
            )
        return violations

    declared = _declared_type(schema)

    if declared == "string":
        violations.append((TestCaseType.INVALID_TYPE, 12345, "a number where a string is required"))

        fmt = schema.get("format")
        if isinstance(fmt, str) and fmt in _FORMAT_VIOLATIONS:
            violations.append(
                (TestCaseType.INVALID_FORMAT, _FORMAT_VIOLATIONS[fmt], f"a malformed {fmt}")
            )

        minimum_length = _as_int(schema.get("minLength"))
        if minimum_length is not None and minimum_length > 0:
            violations.append(
                (
                    TestCaseType.BELOW_MIN_LENGTH,
                    "a" * (minimum_length - 1),
                    f"a string shorter than minLength {minimum_length}",
                )
            )

        maximum_length = _as_int(schema.get("maxLength"))
        if maximum_length is not None and maximum_length >= 0:
            violations.append(
                (
                    TestCaseType.ABOVE_MAX_LENGTH,
                    "a" * (maximum_length + 1),
                    f"a string longer than maxLength {maximum_length}",
                )
            )

        if schema.get("pattern"):
            # Any value chosen here might happen to match the pattern, which
            # would report a bug that is not there. Silence is the honest answer.
            warnings.append(
                f"{label}: '{target}' constrains a pattern; "
                "no pattern violation is generated."
            )

    elif declared in {"integer", "number"}:
        invalid_type_value = (
            "not-an-integer" if declared == "integer" else "not-a-number"
        )
        violations.append(
            (
                TestCaseType.INVALID_TYPE,
                invalid_type_value,
                f"a string where {'an integer' if declared == 'integer' else 'a number'} "
                "is required",
            )
        )
        violations.extend(_numeric_violations(schema, declared))

    elif declared == "boolean":
        violations.append(
            (TestCaseType.INVALID_TYPE, "not-a-boolean", "a string where a boolean is required")
        )

    elif declared == "array":
        items = schema.get("items")
        if isinstance(items, dict) and _is_realisable(items):
            example = _example(items, label, warnings)
            wrong = "not-an-item" if _declared_type(items) != "string" else 12345
            violations.append(
                (
                    TestCaseType.INVALID_TYPE,
                    [example, wrong],
                    "an array containing an item of the wrong type",
                )
            )
        violations.extend(_size_violations(schema))

    elif declared == "object":
        violations.append(
            (TestCaseType.INVALID_TYPE, "not-an-object", "a string where an object is required")
        )

    return violations


def _numeric_violations(schema: dict[str, Any], declared: str) -> list[tuple[str, Any, str]]:
    """Boundary violations, honouring both exclusive-bound spellings.

    OpenAPI 3.0 writes ``exclusiveMinimum: true`` alongside ``minimum``; JSON
    Schema 2020-12, and so OpenAPI 3.1, writes ``exclusiveMinimum: 5``. Reading
    only one of them would produce a value that is actually valid.
    """
    violations: list[tuple[str, Any, str]] = []
    step = 1 if declared == "integer" else 1.0

    exclusive_minimum = schema.get("exclusiveMinimum")
    minimum = _as_number(schema.get("minimum"), declared)

    if isinstance(exclusive_minimum, bool):
        # Draft 4 spelling: the bound itself is the smallest invalid value.
        if exclusive_minimum and minimum is not None:
            violations.append(
                (TestCaseType.BELOW_MINIMUM, minimum, f"the excluded minimum {minimum}")
            )
        elif minimum is not None:
            violations.append(
                (TestCaseType.BELOW_MINIMUM, minimum - step, f"a value below the minimum {minimum}")
            )
    elif (bound := _as_number(exclusive_minimum, declared)) is not None:
        violations.append(
            (TestCaseType.BELOW_MINIMUM, bound, f"the excluded minimum {bound}")
        )
    elif minimum is not None:
        violations.append(
            (TestCaseType.BELOW_MINIMUM, minimum - step, f"a value below the minimum {minimum}")
        )

    exclusive_maximum = schema.get("exclusiveMaximum")
    maximum = _as_number(schema.get("maximum"), declared)

    if isinstance(exclusive_maximum, bool):
        if exclusive_maximum and maximum is not None:
            violations.append(
                (TestCaseType.ABOVE_MAXIMUM, maximum, f"the excluded maximum {maximum}")
            )
        elif maximum is not None:
            violations.append(
                (TestCaseType.ABOVE_MAXIMUM, maximum + step, f"a value above the maximum {maximum}")
            )
    elif (bound := _as_number(exclusive_maximum, declared)) is not None:
        violations.append(
            (TestCaseType.ABOVE_MAXIMUM, bound, f"the excluded maximum {bound}")
        )
    elif maximum is not None:
        violations.append(
            (TestCaseType.ABOVE_MAXIMUM, maximum + step, f"a value above the maximum {maximum}")
        )

    return violations


def _size_violations(schema: dict[str, Any]) -> list[tuple[str, Any, str]]:
    """minItems / maxItems violations for an array."""
    violations: list[tuple[str, Any, str]] = []

    minimum_items = _as_int(schema.get("minItems"))
    if minimum_items is not None and minimum_items > 0:
        violations.append(
            (
                TestCaseType.BELOW_MIN_LENGTH,
                [],
                f"an array with fewer than minItems {minimum_items}",
            )
        )

    maximum_items = _as_int(schema.get("maxItems"))
    if maximum_items is not None and maximum_items >= 0:
        violations.append(
            (
                TestCaseType.ABOVE_MAX_LENGTH,
                ["example"] * (maximum_items + 1),
                f"an array with more than maxItems {maximum_items}",
            )
        )

    return violations


def _outside_enum(enum: list[Any]) -> Any:
    """A value of the right shape that the enum does not allow."""
    if all(isinstance(member, bool) for member in enum):
        return None  # A boolean enum of both members has no value outside it.

    if all(isinstance(member, (int, float)) and not isinstance(member, bool) for member in enum):
        return max(enum) + 1

    candidate = "__invalid_enum__"
    while candidate in enum:
        candidate += "_"
    return candidate


# --- Example values -----------------------------------------------------------


def _example(schema: Any, label: str, warnings: list[str], depth: int = 0) -> Any:
    """A deterministic value that satisfies a schema."""
    if not isinstance(schema, dict) or depth > _MAX_DEPTH:
        return None

    if "const" in schema:
        return schema["const"]

    enum = schema.get("enum")
    if isinstance(enum, list) and enum:
        return enum[0]

    # The document's own example is deterministic and the most realistic value
    # available, so it wins over anything synthesised here.
    for key in ("example", "default"):
        if key in schema:
            return schema[key]

    if isinstance(schema.get("allOf"), list):
        return _example(_merge_all_of(schema["allOf"]), label, warnings, depth)

    for key in ("oneOf", "anyOf"):
        branches = schema.get(key)
        if isinstance(branches, list) and branches:
            warnings.append(f"{label}: '{key}' is resolved by taking its first branch.")
            return _example(branches[0], label, warnings, depth + 1)

    declared = _declared_type(schema)

    if declared == "string":
        return _example_string(schema)
    if declared == "integer":
        return _example_number(schema, integral=True)
    if declared == "number":
        return _example_number(schema, integral=False)
    if declared == "boolean":
        return True
    if declared == "null":
        return None
    if declared == "array":
        return _example_array(schema, label, warnings, depth)
    if declared == "object":
        return _example_object(schema, label, warnings, depth)

    return None


def _example_string(schema: dict[str, Any]) -> str:
    fmt = schema.get("format")
    if isinstance(fmt, str) and fmt in _FORMAT_EXAMPLES:
        # A formatted value is returned as-is: padding an email to reach
        # minLength would produce something that is no longer an email.
        return _FORMAT_EXAMPLES[fmt]

    value = _DEFAULT_STRING
    minimum_length = _as_int(schema.get("minLength"))
    maximum_length = _as_int(schema.get("maxLength"))

    if minimum_length is not None and len(value) < minimum_length:
        value = value + "a" * (minimum_length - len(value))
    if maximum_length is not None and len(value) > maximum_length:
        value = value[:maximum_length]
    return value


def _example_number(schema: dict[str, Any], *, integral: bool) -> int | float:
    declared = "integer" if integral else "number"
    step = 1 if integral else 1.0
    value: int | float = 1 if integral else 1.0

    minimum = _as_number(schema.get("minimum"), declared)
    exclusive_minimum = schema.get("exclusiveMinimum")
    if isinstance(exclusive_minimum, bool):
        if exclusive_minimum and minimum is not None:
            minimum = minimum + step
    elif (bound := _as_number(exclusive_minimum, declared)) is not None:
        minimum = bound + step if minimum is None else max(minimum, bound + step)

    maximum = _as_number(schema.get("maximum"), declared)
    exclusive_maximum = schema.get("exclusiveMaximum")
    if isinstance(exclusive_maximum, bool):
        if exclusive_maximum and maximum is not None:
            maximum = maximum - step
    elif (bound := _as_number(exclusive_maximum, declared)) is not None:
        maximum = bound - step if maximum is None else min(maximum, bound - step)

    if minimum is not None and value < minimum:
        value = minimum
    if maximum is not None and value > maximum:
        value = maximum
    return value


def _example_array(
    schema: dict[str, Any], label: str, warnings: list[str], depth: int
) -> list[Any]:
    items = schema.get("items")
    if not isinstance(items, dict) or not _is_realisable(items):
        return []

    element = _example(items, label, warnings, depth + 1)
    count = max(1, _as_int(schema.get("minItems")) or 1)
    maximum_items = _as_int(schema.get("maxItems"))
    if maximum_items is not None:
        count = min(count, maximum_items)
    return [deepcopy(element) for _ in range(count)]


def _example_object(
    schema: dict[str, Any], label: str, warnings: list[str], depth: int
) -> dict[str, Any]:
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return {}

    value: dict[str, Any] = {}
    for name in sorted(key for key in properties if isinstance(key, str)):
        child = properties[name]
        if not isinstance(child, dict):
            continue
        if not _is_realisable(child):
            warnings.append(
                f"{label}: property '{name}' has an unsupported schema "
                f"({_unsupported_reason(child)}); it is omitted."
            )
            continue
        value[name] = _example(child, label, warnings, depth + 1)
    return value


def _merge_all_of(branches: list[Any]) -> dict[str, Any]:
    """Shallow-merge an allOf into one object schema."""
    merged: dict[str, Any] = {"type": "object", "properties": {}, "required": []}
    for branch in branches:
        if not isinstance(branch, dict):
            continue
        properties = branch.get("properties")
        if isinstance(properties, dict):
            merged["properties"].update(properties)
        required = branch.get("required")
        if isinstance(required, list):
            merged["required"].extend(item for item in required if isinstance(item, str))
    merged["required"] = sorted(set(merged["required"]))
    return merged


# --- Schema inspection --------------------------------------------------------


def _declared_type(schema: dict[str, Any]) -> str | None:
    """The single JSON type a schema declares, inferring it where it is implied."""
    declared = schema.get("type")

    if isinstance(declared, list):
        # A union such as ["string", "null"]; the non-null member is the shape.
        concrete = [item for item in declared if isinstance(item, str) and item != "null"]
        declared = concrete[0] if len(concrete) == 1 else None

    if isinstance(declared, str):
        return declared

    if isinstance(schema.get("properties"), dict):
        return "object"
    if isinstance(schema.get("items"), dict):
        return "array"
    return None


def _is_nullable(schema: dict[str, Any]) -> bool:
    if schema.get("nullable") is True:  # OpenAPI 3.0 spelling.
        return True
    declared = schema.get("type")
    if isinstance(declared, list):
        return "null" in declared
    return declared == "null"


def _is_realisable(schema: Any) -> bool:
    """Whether a deterministic value can be produced for this schema."""
    if not isinstance(schema, dict):
        return False
    if "$ref" in schema:
        # Phase 3 leaves circular and external references in place; without the
        # whole document there is nothing here to expand them against.
        return False
    if "not" in schema:
        return False
    if "const" in schema or schema.get("enum") or "example" in schema or "default" in schema:
        return True
    if isinstance(schema.get("allOf"), list) or isinstance(schema.get("oneOf"), list):
        return True
    if isinstance(schema.get("anyOf"), list):
        return True
    return _declared_type(schema) is not None


def _unsupported_reason(schema: Any) -> str:
    if not isinstance(schema, dict):
        return "not an object"
    if "$ref" in schema:
        return f"unresolved reference {schema['$ref']}"
    if "not" in schema:
        return "'not' is not supported"
    return "no type could be determined"


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def _as_number(value: Any, declared: str) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value) if declared == "integer" else value


# --- Placing values into a request --------------------------------------------


def _set_at(body: Any, pointer: tuple[str, ...], value: Any, request: dict[str, Any]) -> bool:
    """Write a value into the body at a pointer. Returns whether it landed."""
    if not pointer:
        request["body"] = value
        return True

    container = _container_at(body, pointer)
    if container is None:
        return False
    container[pointer[-1]] = value
    return True


def _remove_at(body: Any, pointer: tuple[str, ...]) -> bool:
    """Delete the key a pointer addresses. Returns whether it was there."""
    if not pointer:
        return False

    container = _container_at(body, pointer)
    if container is None or pointer[-1] not in container:
        return False
    del container[pointer[-1]]
    return True


def _container_at(body: Any, pointer: tuple[str, ...]) -> dict[str, Any] | None:
    current = body
    for key in pointer[:-1]:
        if not isinstance(current, dict) or key not in current:
            return None
        current = current[key]
    return current if isinstance(current, dict) else None
