"""Test data that behaves like a careful client's, and cases a schema cannot see.

``generator.py`` turns a schema into one valid request and many single-rule
violations. Two things it cannot do on its own:

*Avoid the target's existing data.* The document's ``example`` values are the
most realistic ones available, which is exactly why the target is likely to
hold them already: a create sent with ``{"name": "MT"}`` collides with the MT
that exists, and the API's 409 — or its 500 — is then about the data, not the
request. Fields that look unique (names, codes, years, emails) are therefore
given fresh values. Fresh, not random: every value is derived from a seed the
run supplies and the case's name, so a run re-generates exactly the same
requests and a failure can be reproduced, while two runs never share values.

*Question what the schema allows.* A schema constrains fields one at a time.
``{"startYear": 2030, "endYear": 2020}`` satisfies every rule it states, and an
API that saves it is still wrong. Semantic cases send schema-valid but
suspicious input — a range that ends before it starts, a negative price,
29 February 2025, a name made of spaces — and carry an *oracle*: why accepting
the request would be suspicious, and how much that would matter.

It also defines the sequence cases (the same create sent twice; a create read
back), whose requests are sent by the execution service.

Like the generator this module is pure: no database, no network.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from copy import deepcopy
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Callable

from apps.testing.generator import (
    ExpectedStatus,
    GeneratedTests,
    TestCaseDefinition,
    _as_int,
    _as_number,
    _declared_type,
    _merge_all_of,
    _walk,
    generate_test_cases,
    keep_unique_names,
)
from apps.testing.models import (
    Endpoint,
    FindingConfidence,
    FindingSeverity,
    TestCaseType,
)

# --- Field names ----------------------------------------------------------------

# A field is treated as likely unique when one of the words of its name is one
# of these. "unitId" is excluded on purpose: an ID names another resource, and
# a fresh value would point at nothing.
_UNIQUE_WORDS = {
    "name", "code", "slug", "username", "login", "email", "title", "label",
    "handle", "key", "year", "sku", "number", "phone", "mobile", "reference",
    "alias", "nickname", "domain", "subdomain", "acronym", "abbreviation",
}  # fmt: skip

# Fields an empty or blank value makes meaningless.
_NAME_WORDS = {"name", "title", "label", "code", "username", "nickname"}

_MONEY_WORDS = {
    "price", "amount", "cost", "fee", "total", "balance", "salary", "budget",
    "charge", "payment", "tax", "discount", "fare", "revenue", "expense",
}  # fmt: skip
_QUANTITY_WORDS = {
    "quantity", "qty", "count", "seats", "capacity", "stock", "weight",
    "distance", "duration", "age", "volume", "units",
}  # fmt: skip

# Role words that make two fields of one object a range, start role first.
# "first"/"last" is left out: firstName and lastName are not a range.
_RANGE_ROLES = (
    ("start", "end"),
    ("from", "to"),
    ("begin", "end"),
    ("since", "until"),
    ("min", "max"),
    ("minimum", "maximum"),
    ("lower", "upper"),
)
# Pairs where equal or wide values are ordinary (minPrice == maxPrice), so only
# an inverted pair is suspicious.
_BOUND_ROLES = {("min", "max"), ("minimum", "maximum"), ("lower", "upper")}

_WORD = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+")

_DATE_FORMATS = {"date", "date-time"}


def words(name: str) -> list[str]:
    """The lowercased words of a camelCase, snake_case or kebab-case name."""
    return [match.group(0).lower() for match in _WORD.finditer(name)]


def looks_unique(name: str) -> bool:
    parts = words(name)
    if not parts or parts[-1] in {"id", "ids", "uuid"}:
        return False
    return any(part in _UNIQUE_WORDS for part in parts)


@dataclass(frozen=True)
class RangePair:
    start: str
    end: str
    # "number" or a date format.
    kind: str
    bound_only: bool


def range_pairs(properties: dict[str, Any]) -> list[RangePair]:
    """Pairs of sibling fields that together describe a range."""
    found: list[RangePair] = []
    seen: set[tuple[str, str]] = set()
    for name in sorted(key for key in properties if isinstance(key, str)):
        for start_role, end_role in _RANGE_ROLES:
            partner = _swap_word(name, start_role, end_role)
            if partner is None or partner not in properties or (name, partner) in seen:
                continue
            kind = _pair_kind(properties.get(name), properties.get(partner))
            if kind is None:
                continue
            seen.add((name, partner))
            found.append(
                RangePair(
                    start=name,
                    end=partner,
                    kind=kind,
                    bound_only=(start_role, end_role) in _BOUND_ROLES,
                )
            )
    return found


def _swap_word(name: str, word: str, replacement: str) -> str | None:
    """``name`` with the whole word ``word`` replaced, keeping its case."""
    for match in _WORD.finditer(name):
        token = match.group(0)
        if token.lower() != word:
            continue
        if token.isupper() and len(token) > 1:
            new = replacement.upper()
        elif token[0].isupper():
            new = replacement.capitalize()
        else:
            new = replacement
        return name[: match.start()] + new + name[match.end() :]
    return None


def _pair_kind(start: Any, end: Any) -> str | None:
    if not isinstance(start, dict) or not isinstance(end, dict):
        return None
    start_type, end_type = _declared_type(start), _declared_type(end)
    if start_type in {"integer", "number"} and end_type in {"integer", "number"}:
        return "number"
    if start_type == end_type == "string":
        fmt = start.get("format")
        if fmt in _DATE_FORMATS and end.get("format") == fmt:
            return fmt
    return None


# --- Fresh values ---------------------------------------------------------------


def token_for(seed: str, *parts: str) -> str:
    """Six hex characters, the same for the same inputs."""
    digest = hashlib.sha256("|".join((seed, *parts)).encode("utf-8")).hexdigest()
    return digest[:6]


def freshen(
    body: Any,
    schema: dict[str, Any] | None,
    token: str,
    *,
    skip: tuple[str, ...] | None = None,
    aggressive: bool = False,
) -> Any:
    """A copy of ``body`` whose likely-unique fields hold fresh values.

    ``skip`` is the pointer a case deliberately set; it is never changed, nor
    is the other end of a range it belongs to. ``aggressive`` also freshens
    every free-text string: it is used for a retry, when the first attempt
    collided with something the field names did not predict.

    A range keeps its width: a financial year 2025–2026 becomes, say,
    2061–2062, never 2061–2026.
    """
    body = deepcopy(body)
    if isinstance(body, dict) and isinstance(schema, dict):
        _freshen_object(body, schema, (), token, skip, aggressive, depth=0)
    return body


def _freshen_object(
    value: dict[str, Any],
    schema: dict[str, Any],
    pointer: tuple[str, ...],
    token: str,
    skip: tuple[str, ...] | None,
    aggressive: bool,
    depth: int,
) -> None:
    if depth > 4:
        return
    if isinstance(schema.get("allOf"), list):
        schema = _merge_all_of(schema["allOf"])
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return

    def skipped(name: str) -> bool:
        return skip is not None and skip[: len(pointer) + 1] == (*pointer, name)

    handled: set[str] = set()
    for pair in range_pairs(properties):
        handled.update((pair.start, pair.end))
        if skipped(pair.start) or skipped(pair.end) or pair.kind != "number":
            continue
        start, end = value.get(pair.start), value.get(pair.end)
        if not _is_number(start) or not _is_number(end):
            continue
        if not (aggressive or looks_unique(pair.start) or looks_unique(pair.end)):
            continue
        shifted = _fresh_range(
            properties[pair.start], properties[pair.end], start, end, f"{token}{pair.start}"
        )
        if shifted is not None:
            value[pair.start], value[pair.end] = shifted

    for name, current in value.items():
        child = properties.get(name)
        if name in handled or skipped(name) or not isinstance(child, dict):
            continue
        if isinstance(current, dict):
            _freshen_object(current, child, (*pointer, name), token, skip, aggressive, depth + 1)
        elif isinstance(current, str) and (aggressive or looks_unique(name)):
            fresh = fresh_string(child, current, token)
            if fresh is not None:
                value[name] = fresh
        elif _is_number(current) and looks_unique(name):
            fresh = fresh_number(child, current, f"{token}{name}")
            if fresh is not None:
                value[name] = fresh


def fresh_string(schema: dict[str, Any], current: str, token: str) -> str | None:
    """A new value for a string field, or None when one cannot be made safely."""
    if "enum" in schema or "const" in schema or schema.get("pattern"):
        return None
    fmt = schema.get("format")
    if fmt in {"email", "idn-email"}:
        return f"aegis.{token}@example.com"
    if fmt == "uuid":
        return str(uuid.uuid5(uuid.NAMESPACE_URL, f"aegistest:{token}"))
    if fmt:
        return None

    base = current or "aegis"
    value = f"{base}-{token}"
    maximum = _as_int(schema.get("maxLength"))
    if maximum is not None and len(value) > maximum:
        keep = maximum - len(token) - 1
        value = f"{base[:keep]}-{token}" if keep >= 1 else token[: max(maximum, 0)]
    minimum = _as_int(schema.get("minLength"))
    if minimum is not None and len(value) < minimum:
        value += "a" * (minimum - len(value))
    return value


def fresh_number(schema: dict[str, Any], current: Any, salt: str) -> int | float | None:
    """A valid number unlike the document's example, derived from ``salt``."""
    integral = _declared_type(schema) == "integer" or isinstance(current, int)
    low, high = number_bounds(schema, integral=integral)
    seed = int(hashlib.sha256(salt.encode("utf-8")).hexdigest()[:8], 16)

    low, high = _default_span(low, high, salt)
    if low > high:
        return None
    return _pick(int(low), int(high), seed, avoid=current, integral=integral)


def _default_span(low: Any, high: Any, salt: str) -> tuple[Any, Any]:
    """Fill in a missing bound with an unusual-but-valid span."""
    if low is None and high is None:
        return (2100, 2899) if "year" in salt.lower() else (100_000, 999_999)
    if low is None:
        return high - 100_000, high
    if high is None:
        return low, low + 100_000
    return low, high


def _fresh_range(
    start_schema: dict[str, Any], end_schema: dict[str, Any], start: Any, end: Any, salt: str
) -> tuple[Any, Any] | None:
    integral = isinstance(start, int) and isinstance(end, int)
    width = end - start
    start_low, start_high = number_bounds(start_schema, integral=integral)
    end_low, end_high = number_bounds(end_schema, integral=integral)

    # The start may move only as far as keeps both ends inside their bounds.
    lows = [x for x in (start_low, None if end_low is None else end_low - width) if x is not None]
    highs = [x for x in (start_high, None if end_high is None else end_high - width) if x is not None]
    low, high = _default_span(max(lows) if lows else None, min(highs) if highs else None, salt)
    if low > high:
        return None

    seed = int(hashlib.sha256(salt.encode("utf-8")).hexdigest()[:8], 16)
    new_start = _pick(int(low), int(high), seed, avoid=start, integral=integral)
    return new_start, new_start + width


def _pick(low: int, high: int, seed: int, *, avoid: Any, integral: bool) -> int | float:
    span = high - low + 1
    value = low + seed % span
    if value == avoid and span > 1:
        value = low + (seed + 1) % span
    return value if integral else float(value)


def number_bounds(
    schema: dict[str, Any], *, integral: bool
) -> tuple[int | float | None, int | float | None]:
    """Inclusive bounds, with both spellings of the exclusive keywords honoured."""
    declared = "integer" if integral else "number"
    step = 1 if integral else 0.01

    low = _as_number(schema.get("minimum"), declared)
    exclusive = schema.get("exclusiveMinimum")
    if isinstance(exclusive, bool):
        if exclusive and low is not None:
            low = low + step
    elif (bound := _as_number(exclusive, declared)) is not None:
        low = bound + step if low is None else max(low, bound + step)

    high = _as_number(schema.get("maximum"), declared)
    exclusive = schema.get("exclusiveMaximum")
    if isinstance(exclusive, bool):
        if exclusive and high is not None:
            high = high - step
    elif (bound := _as_number(exclusive, declared)) is not None:
        high = bound - step if high is None else min(high, bound - step)
    return low, high


def _within(schema: dict[str, Any], value: int | float) -> bool:
    low, high = number_bounds(schema, integral=isinstance(value, int))
    return (low is None or value >= low) and (high is None or value <= high)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def case_skip_pointer(case_name: str) -> tuple[str, ...] | None:
    """The body pointer a generated violation case targets, if any.

    Generated names are ``<type>:<target>``; a body target is a dotted pointer,
    a parameter target starts with its location.
    """
    if ":" not in case_name:
        return None
    target = case_name.split(":", 2)[1]
    if target == "body" or target.split(".", 1)[0] in {"path", "query", "headers"}:
        return None
    return tuple(target.split("."))


def refreshed_input(
    input_data: dict[str, Any],
    schema: dict[str, Any] | None,
    token: str,
    *,
    keep: tuple[str, ...] = (),
) -> dict[str, Any] | None:
    """The same request with fresh values everywhere it is safe to change them.

    Used to retry a request that collided with existing data. Returns None when
    nothing could be changed, so a retry would only repeat the collision.
    """
    body = input_data.get("body") if isinstance(input_data, dict) else None
    if not isinstance(body, dict) or not isinstance(schema, dict):
        return None
    fresh = freshen(body, schema, token, skip=None, aggressive=True)
    for name in keep:
        if name in body:
            fresh[name] = body[name]
    if fresh == body:
        return None
    retried = deepcopy(input_data)
    retried["body"] = fresh
    return retried


# --- Building a run's cases -----------------------------------------------------


def retry_input(
    endpoint: Endpoint,
    *,
    expected: ExpectedStatus | None,
    seed: str,
    case_name: str,
    attempt: int = 1,
) -> dict[str, Any] | None:
    """A semantic case built again from a retry seed, so that its mutation is
    applied to fresh base values. Each ``attempt`` draws different ones.

    Used when the first attempt collided with existing data: ``freshen`` would
    change the very fields the case deliberately set, and leaving them alone
    repeats the collision. Rebuilding the case keeps the relation under test
    — an end before its start stays an end before its start — on values the
    target has not seen. None when the case cannot be rebuilt.
    """
    rebuilt = build_test_cases(
        endpoint, expected=expected, seed=token_for(seed, f"retry{attempt}"), readable=False
    )
    for definition in rebuilt.definitions:
        if definition.name == case_name:
            return deepcopy(definition.input_data)
    return None


def build_test_cases(
    endpoint: Endpoint,
    *,
    expected: ExpectedStatus | None = None,
    seed: str | None = None,
    readable: bool = False,
) -> GeneratedTests:
    """Every case for one endpoint: the generator's, with fresh data, plus
    semantic and sequence cases.

    ``seed`` makes the fresh values; without one the document's examples are
    sent as they are. ``readable`` says the API offers a GET that a created
    resource can be read back from.
    """
    expected = expected or ExpectedStatus()
    generated = generate_test_cases(endpoint, expected=expected)
    if generated.is_empty:
        return generated

    label = f"{endpoint.method} {endpoint.path}"
    schema = endpoint.request_schema if isinstance(endpoint.request_schema, dict) else None
    warnings = list(generated.warnings)
    definitions: list[TestCaseDefinition] = []

    def fresh_input(input_data: dict[str, Any], name: str, skip=None) -> dict[str, Any]:
        if seed is None or schema is None:
            return deepcopy(input_data)
        result = deepcopy(input_data)
        result["body"] = freshen(
            input_data.get("body"), schema, token_for(seed, label, name), skip=skip
        )
        return result

    happy = generated.definitions[0]
    for definition in generated.definitions:
        definitions.append(
            _replace(
                definition,
                input_data=fresh_input(
                    definition.input_data, definition.name, case_skip_pointer(definition.name)
                ),
            )
        )

    body = happy.input_data.get("body")
    if schema is not None and isinstance(body, dict):
        for name, test_type, description, mutate in _semantic_mutations(body, schema, label):
            input_data = fresh_input(happy.input_data, name)
            oracle = mutate(input_data["body"])
            if oracle is None:
                continue
            definitions.append(
                TestCaseDefinition(
                    name=name,
                    test_type=test_type,
                    description=description,
                    input_data=input_data,
                    oracle=oracle,
                )
            )

        if endpoint.method == "POST" and expected.success == 201:
            definitions.append(
                TestCaseDefinition(
                    name=TestCaseType.DUPLICATE_CREATE,
                    test_type=TestCaseType.DUPLICATE_CREATE,
                    description=(
                        f"{label}: send the same valid create twice. The second must be "
                        f"refused cleanly (409 or 400) or accepted, never answered with a 5xx."
                    ),
                    input_data=fresh_input(happy.input_data, TestCaseType.DUPLICATE_CREATE),
                )
            )
            if readable:
                definitions.append(
                    TestCaseDefinition(
                        name=TestCaseType.CREATE_READ_BACK,
                        test_type=TestCaseType.CREATE_READ_BACK,
                        description=(
                            f"{label}: create a resource, then read it back and check it "
                            f"holds the values sent."
                        ),
                        input_data=fresh_input(happy.input_data, TestCaseType.CREATE_READ_BACK),
                    )
                )

    return GeneratedTests(definitions=keep_unique_names(definitions, warnings), warnings=warnings)


def _replace(definition: TestCaseDefinition, **changes: Any) -> TestCaseDefinition:
    values = {
        "name": definition.name,
        "test_type": definition.test_type,
        "description": definition.description,
        "input_data": definition.input_data,
        "expected_status_code": definition.expected_status_code,
        "oracle": definition.oracle,
    }
    values.update(changes)
    return TestCaseDefinition(**values)


# --- Semantic cases -------------------------------------------------------------

# A mutation edits a fresh copy of the happy-path body in place and returns the
# case's oracle, or None when it does not apply to the values found there.
Mutation = Callable[[dict[str, Any]], "dict[str, Any] | None"]


def _oracle(
    concern: str,
    *,
    severity: str,
    confidence: str,
    cause: str,
    fields: list[str],
    flag_acceptance: bool = True,
) -> dict[str, Any]:
    return {
        "concern": concern,
        "flag_acceptance": flag_acceptance,
        "severity": severity,
        "confidence": confidence,
        "probable_cause": cause,
        "fields": fields,
    }


def _semantic_mutations(
    body: dict[str, Any], schema: dict[str, Any], label: str
) -> list[tuple[str, str, str, Mutation]]:
    mutations: list[tuple[str, str, str, Mutation]] = []
    for pointer, node in _walk(schema):
        if isinstance(node.get("allOf"), list):
            node = _merge_all_of(node["allOf"])
        properties = node.get("properties")
        if not isinstance(properties, dict) or not isinstance(_get(body, pointer), dict):
            continue
        required = node.get("required") if isinstance(node.get("required"), list) else []
        pairs = range_pairs(properties)
        for pair in pairs:
            mutations.extend(_pair_mutations(pointer, pair, properties, label))
        roles = {pair.start: ("start", pair) for pair in pairs}
        roles.update({pair.end: ("end", pair) for pair in pairs})
        for name in sorted(key for key in properties if isinstance(key, str)):
            child = properties[name]
            if isinstance(child, dict):
                mutations.extend(
                    _value_mutations(pointer, name, child, name in required, roles.get(name), label)
                )
    return mutations


def _get(body: Any, pointer: tuple[str, ...]) -> Any:
    current = body
    for key in pointer:
        if not isinstance(current, dict) or key not in current:
            return None
        current = current[key]
    return current


def _target(pointer: tuple[str, ...], name: str) -> str:
    return ".".join((*pointer, name))


def _pair_mutations(
    pointer: tuple[str, ...], pair: RangePair, properties: dict[str, Any], label: str
) -> list[tuple[str, str, str, Mutation]]:
    start_schema, end_schema = properties[pair.start], properties[pair.end]
    start_name, end_name = _target(pointer, pair.start), _target(pointer, pair.end)
    prefix = f"{TestCaseType.CROSS_FIELD}:{start_name}/{end_name}"
    cause = (
        f"The API validates {pair.start} and {pair.end} one at a time and never checks "
        f"them against each other."
    )

    def setter(compute: Callable[[Any], tuple[Any, Any] | None], describe) -> Mutation:
        def mutate(body: dict[str, Any]) -> dict[str, Any] | None:
            container = _get(body, pointer)
            if not isinstance(container, dict):
                return None
            values = compute(container.get(pair.start))
            if values is None:
                return None
            container[pair.start], container[pair.end] = values
            return describe(*values)

        return mutate

    shift = _shift_number if pair.kind == "number" else _shift_date

    def before(start: Any) -> tuple[Any, Any] | None:
        if start is None:
            return None
        end = shift(start, -10)
        if end is None:
            return None
        if pair.kind == "number":
            low, _ = number_bounds(end_schema, integral=isinstance(end, int))
            if low is not None and end < low:
                end = low
                start = shift(end, 10)
            if not (_within(start_schema, start) and _within(end_schema, end)) or end >= start:
                return None
        return start, end

    def equal(start: Any) -> tuple[Any, Any] | None:
        if start is None or (pair.kind == "number" and not _within(end_schema, start)):
            return None
        return start, start

    def far(start: Any) -> tuple[Any, Any] | None:
        if start is None:
            return None
        if pair.kind == "number":
            _, high = number_bounds(end_schema, integral=isinstance(start, int))
            end = high if high is not None else shift(start, 1000)
            if end is None or end - start <= 10:
                return None
            return start, end
        end = _shift_years(start, 100)
        return (start, end) if end is not None else None

    mutations: list[tuple[str, str, str, Mutation]] = [
        (
            f"{prefix}:end_before_start",
            TestCaseType.CROSS_FIELD,
            f"{label}: {pair.end} earlier than {pair.start}; every value is schema-valid.",
            setter(
                before,
                lambda s, e: _oracle(
                    f"{pair.end} ({e}) is before {pair.start} ({s})"
                    if not pair.bound_only
                    else f"{pair.end} ({e}) is less than {pair.start} ({s})",
                    severity=FindingSeverity.MEDIUM,
                    confidence=FindingConfidence.HIGH,
                    cause=cause,
                    fields=[start_name, end_name],
                ),
            ),
        )
    ]
    if pair.bound_only:
        return mutations

    mutations += [
        (
            f"{prefix}:end_equals_start",
            TestCaseType.CROSS_FIELD,
            f"{label}: {pair.end} equal to {pair.start}, an empty range.",
            setter(
                equal,
                lambda s, e: _oracle(
                    f"{pair.end} equals {pair.start} ({s}), so the range is empty",
                    severity=FindingSeverity.LOW,
                    confidence=FindingConfidence.LOW,
                    cause=cause,
                    fields=[start_name, end_name],
                ),
            ),
        ),
        (
            f"{prefix}:end_far_after_start",
            TestCaseType.CROSS_FIELD,
            f"{label}: {pair.end} far beyond {pair.start}.",
            setter(
                far,
                lambda s, e: _oracle(
                    f"{pair.end} ({e}) is {_distance(s, e, pair.kind)} "
                    f"{pair.start} ({s}), a far wider range than usual",
                    severity=FindingSeverity.LOW,
                    confidence=FindingConfidence.LOW,
                    cause=cause,
                    fields=[start_name, end_name],
                ),
            ),
        ),
    ]
    return mutations


def _value_mutations(
    pointer: tuple[str, ...],
    name: str,
    schema: dict[str, Any],
    required: bool,
    role: tuple[str, RangePair] | None,
    label: str,
) -> list[tuple[str, str, str, Mutation]]:
    if "enum" in schema or "const" in schema:
        return []
    target = _target(pointer, name)
    prefix = f"{TestCaseType.SUSPICIOUS_VALUE}:{target}"
    declared = _declared_type(schema)
    parts = set(words(name))
    found: list[tuple[str, str, str, Mutation]] = []

    def put(value: Any, oracle: dict[str, Any], partner: tuple[str, Any] | None = None) -> Mutation:
        def mutate(body: dict[str, Any]) -> dict[str, Any] | None:
            container = _get(body, pointer)
            if not isinstance(container, dict) or name not in container:
                return None
            container[name] = value
            if partner is not None and partner[0] in container:
                container[partner[0]] = partner[1]
            return oracle

        return mutate

    if declared in {"integer", "number"} and parts & (_MONEY_WORDS | _QUANTITY_WORDS):
        integral = declared == "integer"
        kind = "amount" if parts & _MONEY_WORDS else "quantity"
        cause = f"{name} has no lower bound in the code, or none that matches the business rule."
        negative = -5 if integral else -10.5
        huge = 10**12 if integral else 1e12
        for suffix, value, concern, severity, confidence in (
            ("negative", negative, f"a negative {kind} for {name} ({negative})",
             FindingSeverity.MEDIUM, FindingConfidence.MEDIUM),
            ("zero", 0 if integral else 0.0, f"a {kind} of zero for {name}",
             FindingSeverity.LOW, FindingConfidence.LOW),
            ("huge", huge, f"an implausibly large {kind} for {name} ({huge:,})",
             FindingSeverity.LOW, FindingConfidence.LOW),
        ):  # fmt: skip
            if _within(schema, value):
                found.append(
                    (
                        f"{prefix}:{suffix}",
                        TestCaseType.SUSPICIOUS_VALUE,
                        f"{label}: {concern}; the schema allows it.",
                        put(
                            value,
                            _oracle(
                                concern,
                                severity=severity,
                                confidence=confidence,
                                cause=cause,
                                fields=[target],
                            ),
                        ),
                    )
                )

    elif declared == "string" and schema.get("format") in _DATE_FORMATS and not schema.get("pattern"):
        suffix_time = "T00:00:00Z" if schema.get("format") == "date-time" else ""
        position = role[0] if role else None
        partner = role[1] if role else None
        cause = f"{name} is checked for its format, not for a sensible value."
        cases: list[tuple[str, str, str, str, str, tuple[str, Any] | None]] = []
        if position != "end":
            cases.append(("far_past", "1900-01-01", f"{name} in the far past (1900-01-01)",
                          FindingSeverity.LOW, FindingConfidence.LOW, None))  # fmt: skip
        if position != "start":
            cases.append(("far_future", "2999-12-31", f"{name} in the far future (2999-12-31)",
                          FindingSeverity.LOW, FindingConfidence.LOW, None))  # fmt: skip
        other = None
        if partner is not None:
            other_name = partner.end if position == "start" else partner.start
            other_value = "2025-03-01" if position == "start" else "2025-02-01"
            other = (other_name, other_value + suffix_time)
        cases.append(("feb_29_non_leap", "2025-02-29",
                      f"{name} is 29 February 2025, a date that does not exist",
                      FindingSeverity.MEDIUM, FindingConfidence.HIGH, other))  # fmt: skip
        for suffix, value, concern, severity, confidence, partner_value in cases:
            found.append(
                (
                    f"{prefix}:{suffix}",
                    TestCaseType.SUSPICIOUS_VALUE,
                    f"{label}: {concern}.",
                    put(
                        value + suffix_time,
                        _oracle(
                            concern,
                            severity=severity,
                            confidence=confidence,
                            cause=(
                                "The date is parsed leniently (rolled over to 1 March) or "
                                "not validated at all."
                                if suffix == "feb_29_non_leap"
                                else cause
                            ),
                            fields=[target],
                        ),
                        partner_value,
                    ),
                )
            )

    elif (
        declared == "string"
        and required
        and parts & _NAME_WORDS
        and not schema.get("format")
        and not schema.get("pattern")
    ):
        minimum = _as_int(schema.get("minLength")) or 0
        maximum = _as_int(schema.get("maxLength"))
        blank_cause = f"{name} is checked for presence and type, not for content."
        if minimum == 0:
            found.append(
                (
                    f"{prefix}:empty",
                    TestCaseType.SUSPICIOUS_VALUE,
                    f"{label}: an empty {name}.",
                    put("", _oracle(
                        f"{name} is an empty string",
                        severity=FindingSeverity.MEDIUM, confidence=FindingConfidence.HIGH,
                        cause=blank_cause, fields=[target],
                    )),
                )
            )  # fmt: skip
        if minimum <= 3 and (maximum is None or maximum >= 3):
            found.append(
                (
                    f"{prefix}:whitespace",
                    TestCaseType.SUSPICIOUS_VALUE,
                    f"{label}: a {name} made only of spaces.",
                    put("   ", _oracle(
                        f"{name} is only whitespace",
                        severity=FindingSeverity.MEDIUM, confidence=FindingConfidence.HIGH,
                        cause=blank_cause + " Values are not trimmed before validation.",
                        fields=[target],
                    )),
                )
            )  # fmt: skip
        if maximum is None:
            found.append(
                (
                    f"{prefix}:very_long",
                    TestCaseType.SUSPICIOUS_VALUE,
                    f"{label}: a 5,000-character {name}; no maxLength is documented.",
                    _long_value(pointer, name, target),
                )
            )
        if maximum is None or maximum >= 24:
            found.append(
                (
                    f"{prefix}:unicode",
                    TestCaseType.SUSPICIOUS_VALUE,
                    f"{label}: a {name} with accented, CJK and symbol characters.",
                    _unicode_value(pointer, name, target, maximum),
                )
            )
    return found


def _long_value(pointer: tuple[str, ...], name: str, target: str) -> Mutation:
    def mutate(body: dict[str, Any]) -> dict[str, Any] | None:
        container = _get(body, pointer)
        if not isinstance(container, dict) or not isinstance(container.get(name), str):
            return None
        # Keep the fresh part at the front, so the value is still unique.
        container[name] = (container[name] + "-" + "x" * 5000)[:5000]
        return _oracle(
            f"{name} is 5,000 characters long",
            severity=FindingSeverity.LOW,
            confidence=FindingConfidence.LOW,
            cause="No length limit between the API and its storage.",
            fields=[target],
            flag_acceptance=False,
        )

    return mutate


def _unicode_value(
    pointer: tuple[str, ...], name: str, target: str, maximum: int | None
) -> Mutation:
    def mutate(body: dict[str, Any]) -> dict[str, Any] | None:
        container = _get(body, pointer)
        if not isinstance(container, dict) or not isinstance(container.get(name), str):
            return None
        value = f"Zoë Ünïcødé 名前 ✓ {container[name]}"
        container[name] = value[:maximum] if maximum is not None else value
        return _oracle(
            f"{name} holds non-ASCII characters",
            severity=FindingSeverity.MEDIUM,
            confidence=FindingConfidence.MEDIUM,
            cause="Text is stored or returned in a non-UTF-8 encoding.",
            fields=[target],
            flag_acceptance=False,
        )

    return mutate


# --- Value arithmetic -------------------------------------------------------------


def _shift_number(value: Any, amount: int) -> Any:
    if not _is_number(value):
        return None
    return value + amount if isinstance(value, int) else value + float(amount)


def _parse_date(value: Any) -> tuple[date, str] | None:
    if not isinstance(value, str) or len(value) < 10:
        return None
    try:
        return date.fromisoformat(value[:10]), value[10:]
    except ValueError:
        return None


def _shift_date(value: Any, days: int) -> str | None:
    parsed = _parse_date(value)
    if parsed is None:
        return None
    day, rest = parsed
    return (day + timedelta(days=days)).isoformat() + rest


def _shift_years(value: Any, years: int) -> str | None:
    parsed = _parse_date(value)
    if parsed is None:
        return None
    day, rest = parsed
    try:
        moved = day.replace(year=day.year + years)
    except ValueError:  # 29 February into a common year.
        moved = day.replace(year=day.year + years, day=28)
    return moved.isoformat() + rest


def _distance(start: Any, end: Any, kind: str) -> str:
    if kind == "number":
        return f"{end - start:g} more than"
    first, second = _parse_date(start), _parse_date(end)
    if first is None or second is None:
        return "far after"
    return f"{(second[0] - first[0]).days} days after"
