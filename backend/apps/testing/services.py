"""Business operations for the testing domain.

Views parse input and serialise output; everything between those two steps
lives here. Keeping it in plain functions means a Celery task in a later phase
can call exactly the same code as an HTTP request, with no view layer involved.

Read helpers return querysets rather than lists so the caller can still filter,
paginate or count without loading rows it will not use.
"""

from __future__ import annotations

from typing import Any

from dataclasses import dataclass, field

from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.utils import timezone

from apps.core.exceptions import DomainError
from apps.testing import generator, openapi
from apps.testing.models import (
    Endpoint,
    Finding,
    Specification,
    TestCase,
    TestRun,
    TestRunStatus,
)

# DomainError is imported rather than defined here: it is raised by this module
# and translated to HTTP 400 by the handler in apps.core.exceptions.


# --- Specifications -----------------------------------------------------------


def create_specification(**fields: Any) -> Specification:
    specification = Specification(**fields)
    specification.full_clean()
    specification.save()
    return specification


def list_specifications() -> models.QuerySet[Specification]:
    return Specification.objects.all()


def list_endpoints(*, specification: Specification) -> models.QuerySet[Endpoint]:
    return specification.endpoints.all()


def create_endpoint(*, specification: Specification, **fields: Any) -> Endpoint:
    endpoint = Endpoint(specification=specification, **fields)
    endpoint.full_clean()
    endpoint.save()
    return endpoint


# --- Importing endpoints from a specification ---------------------------------


@dataclass
class ImportResult:
    """What one import did to a specification's endpoints.

    ``stale`` lists endpoints that exist in the database but are no longer in
    the document. They are reported rather than deleted: removing one would
    cascade to its test cases and every request, response and finding recorded
    against them, destroying history to reflect an edit to a document.
    """

    created: list[Endpoint] = field(default_factory=list)
    updated: list[Endpoint] = field(default_factory=list)
    unchanged: list[Endpoint] = field(default_factory=list)
    stale: list[Endpoint] = field(default_factory=list)
    skipped: list[openapi.SkippedOperation] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    title: str = ""
    api_version: str = ""
    source_type: str = ""

    @property
    def imported_count(self) -> int:
        return len(self.created) + len(self.updated) + len(self.unchanged)


# The Endpoint fields an import owns. Anything outside this list is left alone,
# so a re-import never clobbers data the parser does not produce.
_IMPORTED_ENDPOINT_FIELDS = (
    "operation_id",
    "summary",
    "description",
    "parameters",
    "request_schema",
    "response_schema",
)


@transaction.atomic
def import_endpoints(*, specification: Specification) -> ImportResult:
    """Parse a specification's stored document into Endpoint rows.

    The whole import is one transaction: a document either lands completely or
    not at all, so a failure halfway through cannot leave a specification
    describing a mixture of two versions of an API.

    Re-importing is safe and idempotent. Endpoints are matched on path and
    method — the pair the database already treats as an endpoint's identity —
    so an edited operation is updated in place and keeps its test history.
    """
    try:
        parsed = openapi.parse(specification.raw_specification)
    except openapi.SpecificationParseError as exc:
        # The document is the user's input, so report it against that field.
        raise ValidationError({"raw_specification": exc.errors}) from exc

    result = ImportResult(
        skipped=list(parsed.skipped),
        warnings=list(parsed.warnings),
        title=parsed.title,
        api_version=parsed.api_version,
        source_type=parsed.source_type,
    )

    existing = {
        (endpoint.path, endpoint.method): endpoint
        for endpoint in specification.endpoints.all()
    }

    for operation in parsed.operations:
        endpoint = existing.pop((operation.path, operation.method), None)
        if endpoint is None:
            result.created.append(_create_imported_endpoint(specification, operation))
        elif _apply_operation(endpoint, operation):
            result.updated.append(endpoint)
        else:
            result.unchanged.append(endpoint)

    # Whatever is left was in the database but not in the document.
    result.stale = sorted(existing.values(), key=lambda e: (e.path, e.method))

    _sync_source_type(specification, parsed.source_type)
    return result


def _create_imported_endpoint(
    specification: Specification, operation: openapi.ParsedOperation
) -> Endpoint:
    endpoint = Endpoint(
        specification=specification,
        path=operation.path,
        method=operation.method,
        **{name: getattr(operation, name) for name in _IMPORTED_ENDPOINT_FIELDS},
    )
    endpoint.full_clean()
    endpoint.save()
    return endpoint


def _apply_operation(endpoint: Endpoint, operation: openapi.ParsedOperation) -> bool:
    """Update an endpoint in place. Returns whether anything actually changed."""
    changed = [
        name
        for name in _IMPORTED_ENDPOINT_FIELDS
        if getattr(endpoint, name) != getattr(operation, name)
    ]
    if not changed:
        return False

    for name in changed:
        setattr(endpoint, name, getattr(operation, name))
    endpoint.full_clean()
    endpoint.save(update_fields=[*changed, "updated_at"])
    return True


def _sync_source_type(specification: Specification, source_type: str) -> None:
    """Let the document correct the dialect recorded against the specification.

    Only source_type is taken from the document. Name and version are not: they
    identify the specification and are covered by a unique constraint, so
    overwriting them from a file could collide with another row or silently
    rename something a user is already working with.
    """
    if source_type and specification.source_type != source_type:
        specification.source_type = source_type
        specification.save(update_fields=["source_type", "updated_at"])


# --- Test runs ----------------------------------------------------------------


def create_test_run(*, specification: Specification, name: str = "", **fields: Any) -> TestRun:
    """Open a new run in the PENDING state.

    A run is always created pending: nothing has executed yet at the moment the
    caller asks for one. Naming it is a convenience, not information the caller
    must supply, so an omitted name is derived from the specification.
    """
    test_run = TestRun(
        specification=specification,
        name=name.strip() or _default_test_run_name(specification),
        status=TestRunStatus.PENDING,
        **fields,
    )
    test_run.full_clean()
    test_run.save()
    return test_run


def _default_test_run_name(specification: Specification) -> str:
    stamp = timezone.now().strftime("%Y-%m-%d %H:%M:%S")
    return f"{specification.name} - {stamp}"[:255]


def list_test_runs(*, specification: Specification | None = None) -> models.QuerySet[TestRun]:
    queryset = TestRun.objects.select_related("specification")
    if specification is not None:
        queryset = queryset.filter(specification=specification)
    return queryset


def start_test_run(test_run: TestRun) -> TestRun:
    """Move a pending run into RUNNING and stamp its start time."""
    if test_run.status != TestRunStatus.PENDING:
        raise DomainError(f"Cannot start a run with status '{test_run.status}'.")
    test_run.status = TestRunStatus.RUNNING
    test_run.started_at = timezone.now()
    test_run.save(update_fields=["status", "started_at", "updated_at"])
    return test_run


def complete_test_run(test_run: TestRun, *, status: str = TestRunStatus.COMPLETED) -> TestRun:
    """Close a running run in a terminal state and stamp its completion time."""
    terminal = {TestRunStatus.COMPLETED, TestRunStatus.FAILED, TestRunStatus.CANCELLED}
    if status not in terminal:
        raise DomainError(f"'{status}' is not a terminal run status.")
    if test_run.status != TestRunStatus.RUNNING:
        raise DomainError(f"Cannot complete a run with status '{test_run.status}'.")
    test_run.status = status
    test_run.completed_at = timezone.now()
    test_run.save(update_fields=["status", "completed_at", "updated_at"])
    return test_run


# --- Test cases ---------------------------------------------------------------


@transaction.atomic
def create_test_case(*, test_run: TestRun, endpoint: Endpoint, **fields: Any) -> TestCase:
    """Add a case to a run.

    The endpoint has to come from the run's own specification; testing an
    operation the run is not about would silently produce meaningless results.
    """
    test_case = TestCase(test_run=test_run, endpoint=endpoint, **fields)
    test_case.full_clean()
    test_case.save()
    return test_case


def list_test_cases(*, test_run: TestRun) -> models.QuerySet[TestCase]:
    return test_run.test_cases.select_related("endpoint")


# --- Generating test cases ----------------------------------------------------


@dataclass
class GenerationResult:
    """What one generation pass did to a run's test cases."""

    test_run_id: int
    endpoints: int = 0
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    skipped: int = 0
    warnings: list[str] = field(default_factory=list)


# The fields generation owns. A field outside this list is never touched, so a
# value set by hand survives a re-generation.
_GENERATED_TEST_CASE_FIELDS = (
    "test_type",
    "description",
    "input_data",
    "expected_status_code",
)


@transaction.atomic
def generate_test_cases_for_run(*, test_run: TestRun) -> GenerationResult:
    """Fill a pending run with the test cases its specification implies.

    Generation is deterministic, so running it twice is not an error: a case is
    identified by its run, its endpoint and its generated name, and a second
    pass updates those cases in place rather than duplicating them.

    The whole pass is one transaction. A run half-populated with test cases
    would be executed as though it were complete and would under-report.
    """
    if test_run.status != TestRunStatus.PENDING:
        # Adding cases to a run that has already executed would silently change
        # what its results mean.
        raise DomainError(
            f"Test cases can only be generated for a pending run; this one is "
            f"'{test_run.status}'."
        )

    result = GenerationResult(test_run_id=test_run.pk)
    document = test_run.specification.raw_specification

    existing = {
        (case.endpoint_id, case.name): case
        for case in test_run.test_cases.all()
    }

    for endpoint in test_run.specification.endpoints.all():
        result.endpoints += 1

        expected = generator.ExpectedStatus.from_codes(
            openapi.documented_status_codes(document, endpoint.path, endpoint.method)
        )
        generated = generator.generate_test_cases(endpoint, expected=expected)
        result.warnings.extend(generated.warnings)

        if generated.is_empty:
            result.skipped += 1
            continue

        for definition in generated.definitions:
            case = existing.get((endpoint.pk, definition.name))
            if case is None:
                _create_generated_test_case(test_run, endpoint, definition)
                result.created += 1
            elif _apply_definition(case, definition):
                result.updated += 1
            else:
                result.unchanged += 1

    return result


def _create_generated_test_case(
    test_run: TestRun, endpoint: Endpoint, definition: generator.TestCaseDefinition
) -> TestCase:
    test_case = TestCase(
        test_run=test_run,
        endpoint=endpoint,
        name=definition.name,
        test_type=definition.test_type,
        description=definition.description,
        input_data=definition.input_data,
        expected_status_code=definition.expected_status_code,
    )
    test_case.full_clean()
    test_case.save()
    return test_case


def _apply_definition(
    test_case: TestCase, definition: generator.TestCaseDefinition
) -> bool:
    """Bring an existing case in line with the definition. Did anything change?"""
    changed = [
        name
        for name in _GENERATED_TEST_CASE_FIELDS
        if getattr(test_case, name) != getattr(definition, name)
    ]
    if not changed:
        return False

    for name in changed:
        setattr(test_case, name, getattr(definition, name))
    test_case.full_clean()
    test_case.save(update_fields=[*changed, "updated_at"])
    return True


# --- Findings -----------------------------------------------------------------


def list_findings(*, test_run: TestRun) -> models.QuerySet[Finding]:
    """Every finding produced by a run, across all of its cases."""
    return Finding.objects.filter(test_case__test_run=test_run).select_related("test_case")
