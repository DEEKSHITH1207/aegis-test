"""Business operations for the testing domain.

Views parse input and serialise output; everything between those two steps
lives here. Keeping it in plain functions means the Celery task and the
management command call exactly the same code as an HTTP request, with no view
layer involved.

Read helpers return querysets rather than lists so the caller can still filter,
paginate or count without loading rows it will not use.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

import httpx
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.utils import timezone

from apps.core.exceptions import DomainError
from apps.testing import evaluation, executor, generator, openapi
from apps.testing.models import (
    Endpoint,
    Finding,
    RequestRecord,
    ResponseRecord,
    Specification,
    TestCase,
    TestCaseStatus,
    TestRun,
    TestRunStatus,
)

logger = logging.getLogger(__name__)

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
    _validate_target(test_run)
    test_run.save()
    return test_run


def _validate_target(test_run: TestRun) -> None:
    """Check where a run will send its requests, and what it will send along.

    An empty base URL is allowed while a run is only being prepared; executing
    it is what requires one.
    """
    errors: dict[str, str] = {}
    if test_run.base_url:
        problem = executor.base_url_problem(test_run.base_url, settings.EXECUTOR_ALLOWED_HOSTS)
        if problem:
            errors["base_url"] = problem

    headers = test_run.headers
    if not isinstance(headers, dict) or not all(
        isinstance(name, str) and name and isinstance(value, str)
        for name, value in headers.items()
    ):
        errors["headers"] = "Headers must be an object mapping header names to string values."

    if errors:
        raise ValidationError(errors)


def _default_test_run_name(specification: Specification) -> str:
    stamp = timezone.now().strftime("%Y-%m-%d %H:%M:%S")
    return f"{specification.name} - {stamp}"[:255]


# The per-status counts reported on a run. Annotated in the list query rather
# than computed per row, so a page of runs costs one query, not fifty.
_RESULT_COUNTS = {
    f"{status}_count": models.Count(
        "test_cases", filter=models.Q(test_cases__status=status), distinct=True
    )
    for status in TestCaseStatus.values
}


def list_test_runs(*, specification: Specification | None = None) -> models.QuerySet[TestRun]:
    # Django drops Meta.ordering from an aggregating query, so it is restated.
    queryset = (
        TestRun.objects.select_related("specification")
        .annotate(
            total_count=models.Count("test_cases", distinct=True),
            findings_count=models.Count("test_cases__findings", distinct=True),
            **_RESULT_COUNTS,
        )
        .order_by(*TestRun._meta.ordering)
    )
    if specification is not None:
        queryset = queryset.filter(specification=specification)
    return queryset


def summarise_test_run(test_run: TestRun) -> dict[str, int]:
    """How a run's cases stand: a count per status, a total, and findings.

    Uses the annotations from :func:`list_test_runs` when the run came from
    there, and queries for them otherwise.
    """
    if not hasattr(test_run, "total_count"):
        test_run = list_test_runs().get(pk=test_run.pk)
    summary = {"total": test_run.total_count}
    summary.update({status: getattr(test_run, f"{status}_count") for status in TestCaseStatus.values})
    summary["findings"] = test_run.findings_count
    return summary


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


def list_test_cases(*, test_run: TestRun, status: str | None = None) -> models.QuerySet[TestCase]:
    queryset = test_run.test_cases.select_related("endpoint")
    if status:
        if status not in TestCaseStatus.values:
            raise ValidationError(
                {"status": f"Must be one of: {', '.join(TestCaseStatus.values)}."}
            )
        queryset = queryset.filter(status=status)
    return queryset


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


# --- Executing a run ----------------------------------------------------------

# A run whose first requests cannot even connect is pointed at the wrong place.
# Stopping after a few saves waiting out a timeout for every remaining case.
_UNREACHABLE_AFTER = 3


@dataclass
class ExecutionSummary:
    """What one execution pass did."""

    test_run_id: int
    executed: int = 0
    passed: int = 0
    failed: int = 0
    errors: int = 0
    skipped: int = 0
    findings: int = 0


def start_execution(*, test_run: TestRun, enqueue: bool = True) -> TestRun:
    """Check a run can execute, mark it RUNNING, and hand it to the worker.

    The run is locked while it is checked and started, so two simultaneous
    requests cannot both start it. The task is queued only once that commits:
    a worker picking it up earlier would find the run still pending.

    ``enqueue=False`` starts the run without queueing it, for a caller that
    executes it itself, such as the management command.
    """
    with transaction.atomic():
        test_run = TestRun.objects.select_for_update().get(pk=test_run.pk)
        if test_run.status != TestRunStatus.PENDING:
            raise DomainError(
                f"Only a pending run can be executed; this one is '{test_run.status}'. "
                f"Create a new run to test again."
            )
        problem = executor.base_url_problem(test_run.base_url, settings.EXECUTOR_ALLOWED_HOSTS)
        if problem:
            raise ValidationError({"base_url": problem})
        if not test_run.test_cases.exists():
            raise DomainError("The run has no test cases; generate them before executing it.")

        start_test_run(test_run)
        if enqueue:
            transaction.on_commit(lambda: _enqueue_execution(test_run.pk))

    test_run.refresh_from_db()
    return test_run


def _enqueue_execution(test_run_id: int) -> None:
    # Imported here: tasks imports this module.
    from apps.testing.tasks import execute_test_run_task

    try:
        execute_test_run_task.delay(test_run_id)
    except Exception as exc:
        # Without this the run would sit in RUNNING forever with nothing
        # working on it.
        logger.exception("Could not queue test run %s", test_run_id)
        test_run = TestRun.objects.get(pk=test_run_id)
        _fail_run(test_run, f"The run could not be queued for execution: {exc}")


def execute_test_run(
    *, test_run: TestRun, client: httpx.Client | None = None
) -> ExecutionSummary:
    """Send every pending case of a RUNNING run and record what happened.

    Each case is committed as soon as it is judged, so progress is visible
    while the run is under way and a crash loses only the case in flight. A
    case whose request fails does not stop the run; a target that cannot be
    reached at all does.
    """
    if test_run.status != TestRunStatus.RUNNING:
        raise DomainError(f"Only a running run can be executed; this one is '{test_run.status}'.")

    summary = ExecutionSummary(test_run_id=test_run.pk)
    owns_client = client is None
    if client is None:
        client = executor.make_client(
            timeout=settings.EXECUTOR_TIMEOUT_SECONDS, verify_tls=settings.EXECUTOR_VERIFY_TLS
        )

    try:
        problem = executor.base_url_problem(test_run.base_url, settings.EXECUTOR_ALLOWED_HOSTS)
        if problem:
            # The allow-list may have changed since the run was started.
            _fail_run(test_run, problem)
            return summary

        cases = test_run.test_cases.filter(status=TestCaseStatus.PENDING).select_related(
            "endpoint"
        )
        unreachable = 0
        reached = False
        for test_case in cases.order_by("id").iterator():
            result, findings = _execute_case(test_run, test_case, client)
            _tally(summary, test_case)
            summary.findings += findings

            if result.status_code is not None:
                reached = True
            elif result.connection_failed and not reached:
                unreachable += 1
                if unreachable >= _UNREACHABLE_AFTER:
                    summary.skipped += _skip_remaining(
                        test_run, "Not sent: the target could not be reached."
                    )
                    _fail_run(
                        test_run,
                        f"Stopped after {unreachable} requests could not connect to "
                        f"{test_run.base_url}: {result.error}",
                    )
                    return summary

        complete_test_run(test_run)
    except Exception as exc:
        _fail_run(test_run, f"Execution stopped unexpectedly: {exc}")
        raise
    finally:
        if owns_client:
            client.close()

    return summary


def _execute_case(
    test_run: TestRun, test_case: TestCase, client: httpx.Client
) -> tuple[executor.ExecutionResult, int]:
    """Send one case, record the evidence, judge it. Returns the result and
    the number of findings it produced."""
    endpoint = test_case.endpoint
    prepared = executor.prepare(
        method=endpoint.method,
        path=endpoint.path,
        input_data=test_case.input_data if isinstance(test_case.input_data, dict) else {},
        base_url=test_run.base_url,
        run_headers=test_run.headers,
    )
    result = executor.send(
        prepared, client=client, max_response_bytes=settings.EXECUTOR_MAX_RESPONSE_BYTES
    )

    findings = 0
    with transaction.atomic():
        request_record = RequestRecord.objects.create(
            test_case=test_case,
            method=prepared.method,
            url=prepared.url[: RequestRecord._meta.get_field("url").max_length],
            headers=executor.redact_headers(prepared.headers),
            query_params=prepared.query_params,
            path_params=prepared.path_params,
            body=prepared.body,
            error=result.error,
        )

        if result.status_code is None:
            test_case.status = TestCaseStatus.ERROR
            test_case.status_detail = result.error
        else:
            response_record = ResponseRecord.objects.create(
                request_record=request_record,
                status_code=result.status_code,
                headers=executor.redact_headers(result.headers),
                body=result.body,
                response_time_ms=result.response_time_ms,
            )
            verdict = evaluation.evaluate(
                label=f"{endpoint.method} {endpoint.path}",
                case_name=test_case.name,
                test_type=test_case.test_type,
                expected_status_code=test_case.expected_status_code,
                status_code=result.status_code,
            )
            test_case.status = verdict.status
            test_case.status_detail = verdict.detail
            for draft in verdict.findings:
                Finding.objects.create(
                    test_case=test_case,
                    response_record=response_record,
                    title=draft.title[: Finding._meta.get_field("title").max_length],
                    description=draft.description,
                    severity=draft.severity,
                    category=draft.category,
                    evidence=_evidence(test_case, prepared, result),
                )
                findings += 1

        test_case.save(update_fields=["status", "status_detail", "updated_at"])

    return result, findings


# Enough of a response body to see what the API said, without copying a large
# payload into every finding.
_EVIDENCE_BODY_MAX_CHARS = 2_000


def _evidence(
    test_case: TestCase,
    prepared: executor.PreparedRequest,
    result: executor.ExecutionResult,
) -> dict[str, Any]:
    """A finding's self-contained support: what was sent, and what came back."""
    body = result.body
    if body is not None:
        rendered = body if isinstance(body, str) else json.dumps(body)
        if len(rendered) > _EVIDENCE_BODY_MAX_CHARS:
            body = rendered[:_EVIDENCE_BODY_MAX_CHARS] + " [truncated]"

    return {
        "test_type": test_case.test_type,
        "expected_status_code": test_case.expected_status_code,
        "request": {
            "method": prepared.method,
            "url": prepared.url,
            "query": prepared.query_params,
            "headers": executor.redact_headers(prepared.headers),
            "body": prepared.body,
        },
        "response": {
            "status_code": result.status_code,
            "response_time_ms": result.response_time_ms,
            "body": body,
        },
    }


def _tally(summary: ExecutionSummary, test_case: TestCase) -> None:
    summary.executed += 1
    if test_case.status == TestCaseStatus.PASSED:
        summary.passed += 1
    elif test_case.status == TestCaseStatus.FAILED:
        summary.failed += 1
    else:
        summary.errors += 1


def _skip_remaining(test_run: TestRun, detail: str) -> int:
    return test_run.test_cases.filter(status=TestCaseStatus.PENDING).update(
        status=TestCaseStatus.SKIPPED, status_detail=detail, updated_at=timezone.now()
    )


def _fail_run(test_run: TestRun, detail: str) -> None:
    """End a run as FAILED, from whichever state it was left in."""
    test_run.refresh_from_db()
    if test_run.is_finished:
        return
    now = timezone.now()
    test_run.status = TestRunStatus.FAILED
    test_run.status_detail = detail
    test_run.started_at = test_run.started_at or now
    test_run.completed_at = now
    test_run.save(
        update_fields=["status", "status_detail", "started_at", "completed_at", "updated_at"]
    )


# --- Findings -----------------------------------------------------------------


def list_findings(*, test_run: TestRun) -> models.QuerySet[Finding]:
    """Every finding produced by a run, across all of its cases."""
    return Finding.objects.filter(test_case__test_run=test_run).select_related("test_case")
