"""Checks on what a successful response *says*, beyond its status code.

A 201 proves only that the API did not refuse. These oracles look inside:

* **Echo** — the values sent come back unchanged.
* **Derived fields** — a value the API computed from the input, such as a
  name "2030-20" built from startYear 2030 and endYear 2020, is consistent
  with that input. Heuristic, so reported with low confidence.
* **Response schema** — the body has the shape the document promises.

It also holds the helpers a sequence needs: recognising a conflict with
existing data, finding a created resource's ID, and finding it again in a
list. Pure: no database, no network.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

# Containers APIs commonly wrap a resource or a page of resources in.
_WRAPPERS = ("data", "item", "result", "resource")
_LIST_WRAPPERS = ("data", "items", "results", "content", "records", "rows", "list")
_ID_KEYS = ("id", "_id", "uuid", "ID", "Id")

# Never compared: an API is right not to return them.
_SECRET_WORDS = ("password", "secret", "token", "pin", "otp")

_CONFLICT_WORDS = re.compile(r"already exists|duplicate|conflict|unique|already (taken|in use|registered)", re.I)


# --- Conflicts ------------------------------------------------------------------


def is_conflict(status_code: int | None, body: Any) -> bool:
    """Whether a response refuses a request because the data already exists."""
    if status_code == 409:
        return True
    if status_code is None or not 400 <= status_code < 500:
        return False
    return bool(_CONFLICT_WORDS.search(message_text(body)))


def message_text(body: Any) -> str:
    if isinstance(body, str):
        return body[:2000]
    if isinstance(body, dict):
        parts = []
        for key in ("message", "error", "detail", "errors", "title", "description"):
            if key in body:
                parts.append(message_text(body[key]) if not isinstance(body[key], str) else body[key])
        return " ".join(parts)
    if isinstance(body, list):
        return " ".join(message_text(item) for item in body[:20])
    return ""


# --- Echo -----------------------------------------------------------------------


@dataclass(frozen=True)
class Mismatch:
    field: str
    sent: Any
    received: Any


def resource_of(body: Any) -> Any:
    """The resource in a response, unwrapped from a {"data": {...}} envelope."""
    if isinstance(body, dict):
        for key in _WRAPPERS:
            inner = body.get(key)
            if isinstance(inner, dict) and not _looks_like_resource(body):
                return inner
    return body


def _looks_like_resource(body: dict[str, Any]) -> bool:
    return any(key in body for key in _ID_KEYS)


def echo_mismatches(sent: Any, received: Any, *, prefix: str = "") -> list[Mismatch]:
    """Fields sent whose value came back different.

    Only scalars sent and returned as scalars are compared: a reference sent
    as an ID and returned expanded into an object is a representation choice,
    not a defect. Differences an API commonly and harmlessly introduces — a
    trimmed or lowercased string, 10.5 returned as "10.50", a date returned as
    midnight of that date — are not reported.
    """
    received = resource_of(received) if not prefix else received
    found: list[Mismatch] = []
    if not isinstance(sent, dict) or not isinstance(received, dict):
        return found
    for name, value in sent.items():
        if not isinstance(name, str) or name not in received or _is_secret(name):
            continue
        path = f"{prefix}{name}"
        other = received[name]
        if isinstance(value, dict):
            found.extend(echo_mismatches(value, other, prefix=f"{path}."))
        elif isinstance(value, list) or isinstance(other, (dict, list)):
            continue
        elif not same_value(value, other):
            found.append(Mismatch(field=path, sent=value, received=other))
    return found


def same_value(sent: Any, received: Any) -> bool:
    if sent == received and type(sent) is type(received):
        return True
    if sent is None or received is None:
        return sent is None and received is None
    if isinstance(sent, bool) or isinstance(received, bool):
        return sent == received
    number = _as_float(sent)
    if number is not None and _as_float(received) is not None:
        return abs(number - _as_float(received)) < 1e-9
    if isinstance(sent, str) and isinstance(received, str):
        if sent.strip().casefold() == received.strip().casefold():
            return True
        first, second = _as_moment(sent), _as_moment(received)
        if first is not None and second is not None:
            return first == second
    return False


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str) and re.fullmatch(r"-?\d+(\.\d+)?", value.strip()):
        return float(value)
    return None


def _as_moment(value: str) -> datetime | date | None:
    text = value.strip()
    try:
        if len(text) == 10:
            return date.fromisoformat(text)
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    # A date sent as "2026-01-01" and returned as "2026-01-01T00:00:00Z" is
    # the same date; one returned as "2025-12-31T18:30:00Z" is not.
    if moment.time() == datetime.min.time():
        return moment.date()
    return moment


def _is_secret(name: str) -> bool:
    lowered = name.lower()
    return any(word in lowered for word in _SECRET_WORDS)


# --- Derived fields ---------------------------------------------------------------

_NUMBER = re.compile(r"\d+")


@dataclass(frozen=True)
class DerivedProblem:
    field: str
    value: str
    explanation: str


def derived_field_problems(sent: Any, received: Any) -> list[DerivedProblem]:
    """String fields the API made up that disagree with the numbers sent.

    A field counts as derived when it is in the response but was not sent.
    Its numbers are read the way people write ranges: "2030-31" is 2030 to
    2031, the second number abbreviated to its last two digits. A derived
    value whose range runs backwards, or that mentions a number matching
    nothing sent while the rest of it matches, is reported.
    """
    received = resource_of(received)
    if not isinstance(sent, dict) or not isinstance(received, dict):
        return []
    inputs = [
        int(value)
        for value in sent.values()
        if isinstance(value, (int, float)) and not isinstance(value, bool) and float(value).is_integer()
    ]
    if not inputs:
        return []

    problems: list[DerivedProblem] = []
    for name, value in received.items():
        if name in sent or not isinstance(value, str) or len(value) > 200:
            continue
        tokens = [int(match.group(0)) for match in _NUMBER.finditer(value)]
        if not tokens or tokens[0] not in inputs:
            continue
        problem = _range_problem(tokens, value, inputs)
        if problem:
            problems.append(DerivedProblem(field=name, value=value, explanation=problem))
    return problems


def _range_problem(tokens: list[int], value: str, inputs: list[int]) -> str:
    first = tokens[0]
    for token in tokens[1:]:
        if token in inputs:
            if token < first:
                return f"'{value}' reads as a range from {first} back to {token}"
            continue
        # An abbreviated number: "31" in "2030-31".
        if token < 100 and first >= 100:
            matches = [n for n in inputs if n % 100 == token and n != first]
            if not matches:
                return (
                    f"'{value}' contains {token}, which matches none of the values sent "
                    f"({', '.join(str(n) for n in inputs)})"
                )
            implied = min(matches, key=lambda n: abs(n - first))
            if implied < first:
                return (
                    f"'{value}' reads as {first} to {implied}, a range that runs backwards "
                    f"(built from the values sent: {', '.join(str(n) for n in inputs)})"
                )
        elif first >= 100 and token >= 100:
            return (
                f"'{value}' contains {token}, which matches none of the values sent "
                f"({', '.join(str(n) for n in inputs)})"
            )
    return ""


# --- Response schema ----------------------------------------------------------------

_JSON_TYPES = {
    "object": dict,
    "array": list,
    "string": str,
    "boolean": bool,
}

_MAX_SCHEMA_PROBLEMS = 10


def schema_problems(value: Any, schema: Any, path: str = "$", depth: int = 0) -> list[str]:
    """Where a value departs from a JSON schema; empty when it conforms.

    Checks type, nullability, required properties, enums and array items. A
    keyword it cannot evaluate — an unresolved $ref, "not" — is passed over:
    reporting a mismatch it is unsure of would be a false positive.
    """
    if not isinstance(schema, dict) or depth > 8 or "$ref" in schema:
        return []
    problems: list[str] = []

    if isinstance(schema.get("allOf"), list):
        for branch in schema["allOf"]:
            problems += schema_problems(value, branch, path, depth + 1)
    for key in ("oneOf", "anyOf"):
        branches = schema.get(key)
        if isinstance(branches, list) and branches:
            if all(schema_problems(value, branch, path, depth + 1) for branch in branches):
                problems.append(f"{path}: matches none of the {key} alternatives")

    if value is None:
        if _nullable(schema) or not schema.get("type"):
            return problems
        return problems + [f"{path}: is null, which the schema does not allow"]

    declared = schema.get("type")
    types = [t for t in declared if t != "null"] if isinstance(declared, list) else [declared]
    types = [t for t in types if isinstance(t, str)]
    if types and not any(_is_type(value, t) for t in types):
        return problems + [f"{path}: expected {' or '.join(types)}, got {_type_name(value)}"]

    enum = schema.get("enum")
    if isinstance(enum, list) and enum and value not in enum:
        problems.append(f"{path}: {value!r} is not one of {enum}")

    if isinstance(value, dict):
        properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
        required = schema.get("required") if isinstance(schema.get("required"), list) else []
        for name in required:
            if isinstance(name, str) and name not in value:
                problems.append(f"{path}.{name}: required but missing")
        for name, child in properties.items():
            if name in value:
                problems += schema_problems(value[name], child, f"{path}.{name}", depth + 1)
    elif isinstance(value, list) and isinstance(schema.get("items"), dict):
        for index, item in enumerate(value[:20]):
            problems += schema_problems(item, schema["items"], f"{path}[{index}]", depth + 1)

    return problems[:_MAX_SCHEMA_PROBLEMS]


def _nullable(schema: dict[str, Any]) -> bool:
    declared = schema.get("type")
    return (
        schema.get("nullable") is True
        or declared == "null"
        or (isinstance(declared, list) and "null" in declared)
    )


def _is_type(value: Any, declared: str) -> bool:
    if declared == "integer":
        return isinstance(value, int) and not isinstance(value, bool) or (
            isinstance(value, float) and value.is_integer()
        )
    if declared == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    expected = _JSON_TYPES.get(declared)
    return expected is None or isinstance(value, expected)


def _type_name(value: Any) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


# --- Finding a created resource -------------------------------------------------------


def resource_id(body: Any, parameter: str | None = None) -> str | None:
    """The ID of the resource a create returned, as text.

    ``parameter`` is the item route's path parameter (``userId`` in
    ``/users/{userId}``): an API that names its key that way in the body too
    is read before the generic ``id`` spellings are tried.
    """
    resource = resource_of(body)
    if not isinstance(resource, dict):
        return None
    keys = (parameter, *_ID_KEYS) if parameter else _ID_KEYS
    for key in keys:
        value = resource.get(key)
        if isinstance(value, (str, int)) and not isinstance(value, bool) and str(value):
            return str(value)
    return None


def list_items(body: Any) -> list[Any] | None:
    """The items of a list response, unwrapped from a page envelope."""
    if isinstance(body, list):
        return body
    if isinstance(body, dict):
        for key in _LIST_WRAPPERS:
            if isinstance(body.get(key), list):
                return body[key]
        lists = [value for value in body.values() if isinstance(value, list)]
        if len(lists) == 1:
            return lists[0]
    return None


def find_created(items: list[Any], identifier: str | None, sent: Any) -> Any:
    """The item that is the created resource: by ID, else by every value sent."""
    for item in items:
        if not isinstance(item, dict):
            continue
        if identifier is not None and resource_id(item) == identifier:
            return item
    if identifier is None and isinstance(sent, dict):
        scalars = {k: v for k, v in sent.items() if isinstance(v, (str, int, float, bool))}
        for item in items:
            if isinstance(item, dict) and scalars and all(
                k in item and same_value(v, item[k]) for k, v in scalars.items()
            ):
                return item
    return None
