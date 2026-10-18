"""Judging a response: did the API do what its specification promises?

Pure, like the generator: it takes a case's expectation and what came back,
and returns a verdict and any findings. No database, no network, so every rule
below is testable as a plain function call.

The rules for a single request, in the order they are applied:

1. **5xx** — always a failure and a *crash* finding. Whatever the input, a
   server error means the API did not handle it.
2. **A conflict with existing data** — a 409, or a 4xx whose message says
   "already exists", "duplicate" or "conflict" — no verdict. The request
   collided with data already in the target, so it says nothing about the
   rule the case is about. (A happy path is retried with fresh values before
   it gets here; see the execution service.)
3. **An invalid case answered with 2xx** — the API accepted input its own
   schema forbids: a *validation gap*.
4. **A semantic case answered with 2xx** — schema-valid but suspicious input
   (an end before its start) was accepted: a *suspicious acceptance*, with the
   severity and confidence its oracle states. A semantic case answered with a
   4xx passes: the API checks the rule.
5. **401, 403 or 429** where something else was expected — no verdict. The
   request was turned away before the API looked at it.
6. **A happy path's 404** — for a resource the case itself created, a
   *state bug*: the create said it worked and the read says it is not there.
   For a sample path value (``id: 1``) that nothing could replace with a real
   one, no verdict: the ID probably does not exist, so the case is
   inconclusive rather than failed.
7. **An invalid case rejected with an undocumented 4xx** — the case passes:
   the API enforces the rule, it just answers 400 where the document says
   404. One low *spec gap* note per operation records the difference.
8. **A documented code that was not returned** — a *spec gap*: the API
   behaves, but not as documented.
9. Otherwise the case passes.

Every finding states a category, a confidence and a one-line probable cause.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from apps.testing import oracles
from apps.testing.models import (
    FindingCategory,
    FindingConfidence,
    FindingSeverity,
    TestCaseStatus,
    TestCaseType,
)

# Responses that mean "not now" or "not you" rather than "not this input".
_ACCESS_REFUSED = {401: "unauthorised", 403: "forbidden", 429: "rate limited"}

_SEMANTIC_TYPES = {TestCaseType.CROSS_FIELD, TestCaseType.SUSPICIOUS_VALUE}


@dataclass(frozen=True)
class FindingDraft:
    title: str
    description: str
    severity: str
    category: str
    confidence: str = FindingConfidence.MEDIUM
    probable_cause: str = ""
    # A documentation note rides along with a case that otherwise passed.
    fails_case: bool = True


@dataclass(frozen=True)
class Verdict:
    status: str
    detail: str
    findings: tuple[FindingDraft, ...] = ()


def evaluate(
    *,
    label: str,
    case_name: str,
    test_type: str,
    expected_status_code: int | None,
    status_code: int,
    body: Any = None,
    oracle: dict[str, Any] | None = None,
    guessed_path: bool = False,
    created_resource: bool = False,
) -> Verdict:
    """Decide what one response means for the case that produced it.

    ``guessed_path`` says the request's path parameters hold generated sample
    values; ``created_resource`` says they name a resource the case created
    first. Only one of them can be true, and both matter only to a 404.
    """
    happy = test_type == TestCaseType.HAPPY_PATH
    semantic = test_type in _SEMANTIC_TYPES
    oracle = oracle or {}
    expected = f"{expected_status_code}" if expected_status_code else "no documented code"

    if status_code >= 500:
        return Verdict(
            status=TestCaseStatus.FAILED,
            detail=f"Server error {status_code}; expected {expected}.",
            findings=(_crash(label, case_name, status_code, happy, oracle),),
        )

    if oracles.is_conflict(status_code, body):
        return Verdict(
            status=TestCaseStatus.ERROR,
            detail=(
                f"The request collided with data already in the target ({status_code}: "
                f"{_short(body)}), so the case could not be judged. This is not a defect."
            ),
        )

    if status_code == 404 and guessed_path and expected_status_code != 404:
        return Verdict(
            status=TestCaseStatus.ERROR,
            detail=(
                f"Received 404 where {expected} is documented. The path used generated "
                f"sample values and the API documents no create operation that could make "
                f"a real resource, so the request may never have reached the operation. "
                f"Inconclusive; this is not a defect."
            ),
        )

    if semantic and 200 <= status_code < 300:
        if not oracle.get("flag_acceptance"):
            return Verdict(
                status=TestCaseStatus.PASSED,
                detail=f"Accepted with {status_code}; accepting this input is reasonable.",
            )
        return Verdict(
            status=TestCaseStatus.FAILED,
            detail=f"Suspicious input was accepted with {status_code}.",
            findings=(_suspicious_acceptance(label, status_code, oracle),),
        )
    if semantic and 400 <= status_code < 500 and status_code not in _ACCESS_REFUSED:
        return Verdict(
            status=TestCaseStatus.PASSED,
            detail=f"Rejected with {status_code}: the API checks this.",
        )

    if not happy and not semantic and 200 <= status_code < 300:
        return Verdict(
            status=TestCaseStatus.FAILED,
            detail=f"Invalid input was accepted with {status_code}; expected {expected}.",
            findings=(
                FindingDraft(
                    title=f"{label} accepted invalid input ({case_name})",
                    description=(
                        f"The request broke exactly one rule of the specification "
                        f"({case_name}) and the API accepted it with {status_code}. "
                        f"Input the schema forbids is reaching the application."
                    ),
                    severity=FindingSeverity.MEDIUM,
                    category=FindingCategory.VALIDATION_GAP,
                    confidence=FindingConfidence.HIGH,
                    probable_cause=(
                        f"The documented rule behind '{case_name}' is not enforced by the "
                        f"API's validation."
                    ),
                ),
            ),
        )

    if status_code in _ACCESS_REFUSED and status_code != expected_status_code:
        return Verdict(
            status=TestCaseStatus.ERROR,
            detail=(
                f"The request was {_ACCESS_REFUSED[status_code]} ({status_code}), so the "
                f"case could not be judged. If the API needs credentials, set them in "
                f"the run's headers."
            ),
        )

    if expected_status_code is not None and status_code != expected_status_code:
        if happy and status_code == 404 and created_resource:
            return Verdict(
                status=TestCaseStatus.FAILED,
                detail=(
                    f"Expected {expected_status_code}, received 404 for a resource "
                    f"created a moment earlier."
                ),
                findings=(_missing_created(label, case_name),),
            )
        if (
            not happy
            and not semantic
            and 400 <= status_code < 500
            and 400 <= expected_status_code < 500
        ):
            return Verdict(
                status=TestCaseStatus.PASSED,
                detail=(
                    f"Rejected with {status_code}; the specification documents "
                    f"{expected_status_code}. The rule is enforced."
                ),
                findings=(
                    _rejection_code_note(label, case_name, status_code, expected_status_code),
                ),
            )
        return Verdict(
            status=TestCaseStatus.FAILED,
            detail=f"Expected {expected_status_code}, received {status_code}.",
            findings=(
                _mismatch(
                    label,
                    case_name,
                    happy=happy,
                    status_code=status_code,
                    expected=expected_status_code,
                ),
            ),
        )

    if expected_status_code is None:
        return Verdict(
            status=TestCaseStatus.PASSED,
            detail=f"Received {status_code}; the specification documents no code to assert.",
        )
    return Verdict(status=TestCaseStatus.PASSED, detail=f"Received {status_code} as documented.")


def _crash(
    label: str, case_name: str, status_code: int, happy: bool, oracle: dict[str, Any]
) -> FindingDraft:
    if happy:
        title = f"{label} crashes with {status_code} on a valid request"
        situation = "The request was valid according to the specification."
        cause = "An exception escapes the handler for ordinary, valid input."
    elif oracle.get("concern"):
        title = f"{label} crashes with {status_code} when {oracle['concern']}"
        situation = (
            f"The request was schema-valid but unusual: {oracle['concern']}. The API should "
            f"have accepted it or refused it with a 4xx."
        )
        cause = "Input the validation lets through reaches code that throws."
    else:
        title = f"{label} crashes with {status_code} on invalid input ({case_name})"
        situation = "The request was invalid, and the API should have rejected it with a 4xx."
        cause = f"Input breaking '{case_name}' passes validation and reaches code that throws."
    return FindingDraft(
        title=title,
        description=(
            f"The server failed with {status_code} instead of handling the request. "
            f"{situation} A 5xx means the error was not handled, and often points to an "
            f"unguarded exception."
        ),
        severity=FindingSeverity.HIGH,
        category=FindingCategory.CRASH,
        confidence=FindingConfidence.HIGH,
        probable_cause=cause,
    )


def _suspicious_acceptance(label: str, status_code: int, oracle: dict[str, Any]) -> FindingDraft:
    concern = str(oracle.get("concern") or "the input was suspicious")
    created = " and the record was created" if status_code == 201 else ""
    return FindingDraft(
        title=f"{label}: suspicious acceptance - {concern}",
        description=(
            f"Every field was valid on its own, but together they do not make sense: "
            f"{concern}. The API accepted the request with {status_code}{created}. "
            f"A schema cannot express this rule, so the API has to check it."
        ),
        severity=str(oracle.get("severity") or FindingSeverity.MEDIUM),
        category=FindingCategory.VALIDATION_GAP,
        confidence=str(oracle.get("confidence") or FindingConfidence.MEDIUM),
        probable_cause=str(oracle.get("probable_cause") or ""),
    )


def _mismatch(
    label: str,
    case_name: str,
    *,
    happy: bool,
    status_code: int,
    expected: int,
) -> FindingDraft:
    title = f"{label} returned {status_code} where {expected} is documented ({case_name})"
    description = (
        f"The specification documents {expected} for this request, and the API "
        f"answered {status_code}."
    )
    if happy and 400 <= status_code < 500:
        return FindingDraft(
            title=title,
            description=description
            + " The request was built from the schema's own constraints, so either the "
            "API enforces a rule the specification does not describe, or the sample "
            "values conflict with data in the target.",
            severity=FindingSeverity.LOW,
            category=FindingCategory.SPEC_GAP,
            confidence=FindingConfidence.LOW,
            probable_cause="The API enforces a rule the specification does not document.",
        )
    return FindingDraft(
        title=title,
        description=description,
        severity=FindingSeverity.LOW,
        category=FindingCategory.SPEC_GAP,
        confidence=FindingConfidence.HIGH,
        probable_cause=(
            f"The API answers {status_code}; the specification says {expected}. One of them "
            f"is out of date."
        ),
    )


def _missing_created(label: str, case_name: str) -> FindingDraft:
    """A 404 for a resource the case created moments before."""
    return FindingDraft(
        title=f"{label} returned 404 for a resource created a moment earlier ({case_name})",
        description=(
            "A create on the collection succeeded and returned an ID, and this request "
            "used that ID straight away. The API answered 404, so the resource the "
            "create reported is not reachable where the document says it is."
        ),
        severity=FindingSeverity.MEDIUM,
        category=FindingCategory.STATE_BUG,
        confidence=FindingConfidence.MEDIUM,
        probable_cause=(
            "The create reports success without persisting, or the ID it returns is not "
            "the one the item route reads."
        ),
    )


def _rejection_code_note(label: str, case_name: str, status_code: int, expected: int) -> FindingDraft:
    """Invalid input was refused, with a code the document does not list.

    A documentation note, made once per operation: the API enforces the rule,
    so the case passes, but a client reading the document would expect
    ``expected`` and get ``status_code``.
    """
    return FindingDraft(
        title=f"{label}: rejects invalid input with {status_code}, not the documented {expected}",
        description=(
            f"Invalid input ({case_name}) was refused with {status_code}. The "
            f"specification documents {expected} as the rejection for this operation. "
            f"The API validates correctly; the document and the code disagree on the code."
        ),
        severity=FindingSeverity.LOW,
        category=FindingCategory.SPEC_GAP,
        confidence=FindingConfidence.MEDIUM,
        probable_cause=(
            f"The {status_code} response is missing from the OpenAPI document, or the "
            f"documented {expected} is out of date."
        ),
        fails_case=False,
    )


# --- Conflicts and sequences ----------------------------------------------------------


def undocumented_conflict(
    label: str, status_code: int, documented_codes: list[str]
) -> FindingDraft | None:
    """A documentation note when a conflict code is missing from the document."""
    if str(status_code) in documented_codes:
        return None
    listed = ", ".join(sorted(documented_codes)) or "none"
    return FindingDraft(
        title=f"{label}: {status_code} for existing data is not documented",
        description=(
            f"The API answered {status_code} because the values sent already exist. That is "
            f"the right behaviour, but the specification does not list {status_code} for "
            f"this operation (it documents: {listed}), so clients cannot expect it."
        ),
        severity=FindingSeverity.LOW,
        category=FindingCategory.SPEC_GAP,
        confidence=FindingConfidence.HIGH,
        probable_cause=f"The {status_code} response is missing from the OpenAPI document.",
        fails_case=False,
    )


def collision_crash(label: str, status_code: int, retry_status: int) -> FindingDraft:
    """A 5xx that went away when the same request was sent with fresh values."""
    return FindingDraft(
        title=(
            f"{label} returns {status_code} when the values already exist "
            f"(unhandled unique constraint)"
        ),
        description=(
            f"The request was valid, but its values already existed in the target and the "
            f"API answered {status_code}. Sent again with fresh values it answered "
            f"{retry_status}. A duplicate should be refused with 409 (or 400), not crash."
        ),
        severity=FindingSeverity.HIGH,
        category=FindingCategory.CRASH,
        confidence=FindingConfidence.MEDIUM,
        probable_cause=(
            "A unique constraint in the database rejects the insert, and the error is not "
            "caught and turned into a 409."
        ),
    )


def duplicate_create(label: str, first_status: int, second_status: int, body: Any) -> Verdict:
    """Judge the second of two identical creates."""
    if second_status >= 500:
        return Verdict(
            status=TestCaseStatus.FAILED,
            detail=(
                f"The first create returned {first_status}; the identical second one "
                f"returned {second_status}."
            ),
            findings=(
                FindingDraft(
                    title=(
                        f"{label}: duplicate create returns {second_status} "
                        f"(unhandled unique constraint)"
                    ),
                    description=(
                        f"Creating a resource succeeded ({first_status}). Sending exactly "
                        f"the same request again crashed with {second_status}. A duplicate "
                        f"should be refused with 409 Conflict (or 400), or accepted; a 5xx "
                        f"means the conflict reached the database and the error escaped."
                    ),
                    severity=FindingSeverity.HIGH,
                    category=FindingCategory.CRASH,
                    confidence=FindingConfidence.HIGH,
                    probable_cause=(
                        "The duplicate check before insert is missing, and the database's "
                        "unique-constraint error is not caught and turned into a 409."
                    ),
                ),
            ),
        )
    if 200 <= second_status < 300:
        return Verdict(
            status=TestCaseStatus.PASSED,
            detail=(
                f"Both creates were accepted ({first_status}, {second_status}): the API "
                f"enforces no uniqueness on these values."
            ),
        )
    if second_status in _ACCESS_REFUSED:
        return Verdict(
            status=TestCaseStatus.ERROR,
            detail=f"The second create was {_ACCESS_REFUSED[second_status]} ({second_status}).",
        )
    return Verdict(
        status=TestCaseStatus.PASSED,
        detail=f"The duplicate was refused cleanly with {second_status} ({_short(body)}).",
    )


def read_back(
    label: str,
    *,
    how: str,
    status_code: int,
    found: bool,
    mismatches: list[oracles.Mismatch],
    identifier: str | None,
    uncertain: bool = False,
) -> Verdict:
    """Judge a created resource read back with ``how`` (e.g. "GET /units/{id}")."""
    if status_code >= 500:
        return Verdict(
            status=TestCaseStatus.FAILED,
            detail=f"Reading the created resource back crashed with {status_code}.",
            findings=(
                FindingDraft(
                    title=f"{label}: reading a just-created resource crashes ({how} -> {status_code})",
                    description=(
                        f"A create succeeded, and reading the new resource back with {how} "
                        f"failed with {status_code}."
                    ),
                    severity=FindingSeverity.HIGH,
                    category=FindingCategory.CRASH,
                    confidence=FindingConfidence.HIGH,
                    probable_cause="The stored record cannot be loaded or serialised back.",
                ),
            ),
        )
    if status_code in _ACCESS_REFUSED:
        return Verdict(
            status=TestCaseStatus.ERROR,
            detail=f"The read-back was {_ACCESS_REFUSED[status_code]} ({status_code}).",
        )
    if not found:
        subject = f"id {identifier}" if identifier else "the values sent"
        return Verdict(
            status=TestCaseStatus.FAILED,
            detail=f"The created resource ({subject}) was not found with {how}.",
            findings=(
                FindingDraft(
                    title=f"{label}: created resource not found by {how}",
                    description=(
                        f"The create succeeded, but {how} answered {status_code} and did not "
                        f"contain the new resource ({subject})."
                        + (
                            " The list may be paginated and the resource on a later page."
                            if uncertain
                            else ""
                        )
                    ),
                    severity=FindingSeverity.MEDIUM,
                    category=FindingCategory.STATE_BUG,
                    confidence=FindingConfidence.LOW if uncertain else FindingConfidence.MEDIUM,
                    probable_cause=(
                        "The create reports success without persisting, or persists it "
                        "somewhere the read does not look."
                    ),
                ),
            ),
        )
    if mismatches:
        listed = "; ".join(f"{m.field}: sent {m.sent!r}, stored {m.received!r}" for m in mismatches)
        return Verdict(
            status=TestCaseStatus.FAILED,
            detail=f"Read back with different values: {listed}.",
            findings=(
                FindingDraft(
                    title=f"{label}: created resource reads back with different values",
                    description=f"Read back with {how}, the new resource differs from what was sent: {listed}.",
                    severity=FindingSeverity.MEDIUM,
                    category=FindingCategory.STATE_BUG,
                    confidence=FindingConfidence.MEDIUM,
                    probable_cause="A field is dropped, transformed or mapped wrongly when stored.",
                ),
            ),
        )
    return Verdict(
        status=TestCaseStatus.PASSED,
        detail=f"Read back with {how}; it holds the values sent.",
    )


def deleted_read_back(
    label: str, *, how: str, status_code: int, identifier: str
) -> Verdict:
    """Judge a read of a resource the case has just deleted with ``how``."""
    if status_code >= 500:
        return Verdict(
            status=TestCaseStatus.FAILED,
            detail=f"Reading the deleted resource back crashed with {status_code}.",
            findings=(
                FindingDraft(
                    title=f"{label}: reading a deleted resource crashes ({how} -> {status_code})",
                    description=(
                        f"The delete succeeded, and reading id {identifier} back with {how} "
                        f"failed with {status_code} instead of a 404."
                    ),
                    severity=FindingSeverity.HIGH,
                    category=FindingCategory.CRASH,
                    confidence=FindingConfidence.HIGH,
                    probable_cause="The read assumes the record exists and does not handle its absence.",
                ),
            ),
        )
    if status_code in _ACCESS_REFUSED:
        return Verdict(
            status=TestCaseStatus.ERROR,
            detail=f"The read-back after the delete was {_ACCESS_REFUSED[status_code]} ({status_code}).",
        )
    if 200 <= status_code < 300:
        return Verdict(
            status=TestCaseStatus.FAILED,
            detail=f"Read back with {how} after the delete: still there ({status_code}).",
            findings=(
                FindingDraft(
                    title=f"{label}: deleted resource is still readable by {how}",
                    description=(
                        f"The delete answered success, and {how} then still returned id "
                        f"{identifier} with {status_code}."
                    ),
                    severity=FindingSeverity.MEDIUM,
                    category=FindingCategory.STATE_BUG,
                    confidence=FindingConfidence.HIGH,
                    probable_cause=(
                        "The delete reports success without removing the record, or "
                        "soft-deletes it without hiding it from reads."
                    ),
                ),
            ),
        )
    return Verdict(
        status=TestCaseStatus.PASSED,
        detail=f"Read back with {how} after the delete: {status_code}, it is gone.",
    )


# --- Looking inside a successful response -------------------------------------------


def response_findings(
    label: str,
    *,
    sent: Any,
    received: Any,
    response_schema: Any,
    check_echo: bool,
) -> list[FindingDraft]:
    """The oracles run on every 2xx: echo, derived fields, response schema."""
    findings: list[FindingDraft] = []

    if check_echo:
        mismatches = oracles.echo_mismatches(sent, received)
        if mismatches:
            listed = "; ".join(
                f"{m.field}: sent {m.sent!r}, got {m.received!r}" for m in mismatches
            )
            findings.append(
                FindingDraft(
                    title=f"{label}: response does not echo the values sent ({mismatches[0].field})",
                    description=(
                        f"The request succeeded, but the resource it returned differs from "
                        f"the request: {listed}."
                    ),
                    severity=FindingSeverity.MEDIUM,
                    category=FindingCategory.STATE_BUG,
                    confidence=FindingConfidence.MEDIUM,
                    probable_cause="A field is transformed, truncated or mapped to the wrong column.",
                )
            )
        for problem in oracles.derived_field_problems(sent, received):
            findings.append(
                FindingDraft(
                    title=f"{label}: derived field '{problem.field}' looks inconsistent ('{problem.value}')",
                    description=(
                        f"'{problem.field}' was not sent; the API worked it out from the input. "
                        f"{problem.explanation[0].upper()}{problem.explanation[1:]}. This is a "
                        f"heuristic reading of the value, so it may be intended."
                    ),
                    severity=FindingSeverity.LOW,
                    category=FindingCategory.STATE_BUG,
                    confidence=FindingConfidence.LOW,
                    probable_cause=(
                        "The value is built from inputs that were never checked against each "
                        "other, or from the wrong input."
                    ),
                )
            )

    if isinstance(response_schema, dict) and received is not None:
        problems = oracles.schema_problems(received, response_schema)
        if problems:
            findings.append(
                FindingDraft(
                    title=f"{label}: response does not match the documented schema",
                    description="The body departs from the documented response schema: "
                    + "; ".join(problems)
                    + ".",
                    severity=FindingSeverity.LOW,
                    category=FindingCategory.SPEC_GAP,
                    confidence=FindingConfidence.MEDIUM,
                    probable_cause="The documented response schema is out of date with the code.",
                )
            )
    return findings


def _short(body: Any) -> str:
    text = oracles.message_text(body) if not isinstance(body, str) else body
    text = " ".join(str(text).split())
    return (text[:120] + "...") if len(text) > 120 else (text or "no message")
