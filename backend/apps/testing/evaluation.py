"""Judging a response: did the API do what its specification promises?

Pure, like the generator: it takes a case's expectation and the status code
that came back, and returns a verdict and any findings. No database, no
network, so every rule below is testable as a plain function call.

The rules, in the order they are applied:

1. **5xx** — always a failure and a finding. Whatever the input, a server
   error means the API did not handle it.
2. **An invalid case answered with 2xx** — the API accepted input its own
   schema forbids. A validation gap, whether or not a 4xx is documented.
3. **401, 403 or 429** where something else was expected — no verdict. The
   request was turned away before the API looked at what the case is about,
   so the result says nothing about it. Usually the run needs credentials.
4. **A documented code that was not returned** — a failure. The API behaves,
   but not as documented.
5. Otherwise the case passes. A case with no documented expectation passes
   on anything that is not ruled out above: asserting a code the document
   never promised would manufacture failures.
"""

from __future__ import annotations

from dataclasses import dataclass

from apps.testing.models import (
    FindingCategory,
    FindingSeverity,
    TestCaseStatus,
    TestCaseType,
)

# Responses that mean "not now" or "not you" rather than "not this input".
_ACCESS_REFUSED = {401: "unauthorised", 403: "forbidden", 429: "rate limited"}


@dataclass(frozen=True)
class FindingDraft:
    title: str
    description: str
    severity: str
    category: str


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
) -> Verdict:
    """Decide what one response means for the case that produced it."""
    happy = test_type == TestCaseType.HAPPY_PATH
    expected = f"{expected_status_code}" if expected_status_code else "no documented code"

    if status_code >= 500:
        return Verdict(
            status=TestCaseStatus.FAILED,
            detail=f"Server error {status_code}; expected {expected}.",
            findings=(
                FindingDraft(
                    title=f"{label} returned {status_code} for {case_name}",
                    description=(
                        f"The server failed with {status_code} instead of handling the "
                        f"request. {_input_phrase(happy)} A 5xx means the error was not "
                        f"handled, and often points to an unguarded exception."
                    ),
                    severity=FindingSeverity.HIGH,
                    category=FindingCategory.BUG,
                ),
            ),
        )

    if not happy and 200 <= status_code < 300:
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
        return Verdict(
            status=TestCaseStatus.FAILED,
            detail=f"Expected {expected_status_code}, received {status_code}.",
            findings=(
                FindingDraft(
                    title=(
                        f"{label} returned {status_code} where {expected_status_code} "
                        f"is documented ({case_name})"
                    ),
                    description=_mismatch_description(
                        happy=happy, status_code=status_code, expected=expected_status_code
                    ),
                    severity=FindingSeverity.LOW,
                    category=FindingCategory.INCONSISTENCY,
                ),
            ),
        )

    if expected_status_code is None:
        return Verdict(
            status=TestCaseStatus.PASSED,
            detail=f"Received {status_code}; the specification documents no code to assert.",
        )
    return Verdict(status=TestCaseStatus.PASSED, detail=f"Received {status_code} as documented.")


def _input_phrase(happy: bool) -> str:
    if happy:
        return "The request was valid according to the specification."
    return "The request was invalid, and the API should have rejected it with a 4xx."


def _mismatch_description(*, happy: bool, status_code: int, expected: int) -> str:
    description = (
        f"The specification documents {expected} for this request, and the API "
        f"answered {status_code}."
    )
    if happy and status_code == 404:
        description += (
            " The request used generated sample values; if a path parameter names a "
            "resource that does not exist in the target, a 404 is the right answer and "
            "this finding is a false positive."
        )
    elif happy and 400 <= status_code < 500:
        description += (
            " The request was built from the schema's own constraints, so either the "
            "API enforces a rule the specification does not describe, or the sample "
            "values conflict with data in the target."
        )
    return description
