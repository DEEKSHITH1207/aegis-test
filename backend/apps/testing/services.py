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
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any
from urllib.parse import urlsplit

import httpx
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.utils import timezone

from apps.core.exceptions import DomainError
from apps.testing import evaluation, executor, generator, openapi
from apps.testing.analysis import prompts as analysis_prompts
from apps.testing.analysis.client import Analyst, AnalystError
from apps.testing.models import (
    Analysis,
    AssessmentVerdict,
    Endpoint,
    Finding,
    FindingAssessment,
    FindingCategory,
    FindingSeverity,
    RequestRecord,
    ResponseRecord,
    RuleExchange,
    RulesRun,
    ScenarioResult,
    ScenarioStatus,
    Specification,
    TestCase,
    TestCaseStatus,
    TestRun,
    TestRunStatus,
)
from apps.testing.rules import document as rules_document
from apps.testing.rules import runner as rules_runner
from apps.testing.rules.expressions import to_json

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
    body = _excerpt(result.body)

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


def _excerpt(body: Any) -> Any:
    """A body as it appears in a finding: whole when small, cut when large."""
    if body is None:
        return None
    rendered = body if isinstance(body, str) else json.dumps(body)
    if len(rendered) > _EVIDENCE_BODY_MAX_CHARS:
        return rendered[:_EVIDENCE_BODY_MAX_CHARS] + " [truncated]"
    return body


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


# --- Business-rules runs -------------------------------------------------------
#
# A rules run executes a business-rules file (see apps.testing.rules) against
# an API. Setup values — IDs, credentials — are passed into these functions
# and never stored: not on the run, not in the evidence (secret ones are
# scrubbed by the runner), not in a finding.

# Finding severities, most severe first, for ordering a report.
_SEVERITY_RANK = {severity: rank for rank, severity in enumerate(FindingSeverity.values)}


def load_rules(rules_text: str) -> rules_document.RulesDocument:
    """Parse a rules file, reporting every problem against the ``rules`` field."""
    try:
        return rules_document.load(rules_text)
    except rules_document.RulesFileError as exc:
        raise ValidationError({"rules": exc.errors}) from exc


@transaction.atomic
def submit_rules_run(
    *,
    specification: Specification,
    rules_text: str,
    base_url: str,
    setup_values: dict[str, str],
    name: str = "",
    today: date | None = None,
    enqueue: bool = True,
) -> RulesRun:
    """Check a rules run can execute, create it, and start it.

    Everything that can be refused is checked before anything is written —
    the file, the base URL, the setup values — so a refused request leaves no
    half-made run behind. ``enqueue=False`` starts the run without queueing
    it, for a caller that executes it itself.
    """
    rules = load_rules(rules_text)

    problem = executor.base_url_problem(base_url, settings.EXECUTOR_ALLOWED_HOSTS)
    if problem:
        raise ValidationError({"base_url": problem})
    missing = rules_runner.missing_setup(rules, setup_values)
    if missing:
        raise ValidationError({"setup": _missing_setup_message(rules, missing)})

    now = timezone.now()
    rules_run = RulesRun(
        specification=specification,
        name=name.strip() or f"{specification.name} rules - {now:%Y-%m-%d %H:%M:%S}"[:255],
        rules_text=rules_text,
        base_url=base_url,
        status=TestRunStatus.RUNNING,
        today=today,
        today_overridden=today is not None,
        warnings=_rules_warnings(specification, rules, base_url),
        started_at=now,
    )
    rules_run.full_clean()
    rules_run.save()

    if enqueue:
        # Setup values travel to the worker as task arguments, through the
        # broker, and are never written to the database.
        values = dict(setup_values)
        transaction.on_commit(lambda: _enqueue_rules_run(rules_run.pk, values))
    return rules_run


def _missing_setup_message(rules: rules_document.RulesDocument, missing: list[str]) -> str:
    wanted = [value for value in rules.setup if value.name in missing]
    return "Missing setup values: " + ", ".join(
        f"{value.name} (env {value.env})" for value in wanted
    )


def _rules_warnings(
    specification: Specification, rules: rules_document.RulesDocument, base_url: str
) -> list[str]:
    """Steps whose request is not an operation of the specification.

    Only a warning: the file may exercise an endpoint the document forgot, and
    placeholder names may differ ({id} against {userId}), so they are ignored.
    A document whose paths include the base URL's own prefix (/sklc/users with
    a base URL ending in /sklc) is matched without it.
    """
    prefix = urlsplit(base_url).path.rstrip("/")
    known: set[tuple[str, str]] = set()
    for endpoint in specification.endpoints.all():
        known.add((endpoint.method, _path_shape(endpoint.path)))
        if prefix and endpoint.path.startswith(prefix + "/"):
            known.add((endpoint.method, _path_shape(endpoint.path[len(prefix) :])))
    if not known:
        return []
    warnings: list[str] = []
    for scenario in rules.scenarios:
        for step in (*scenario.steps, *scenario.cleanup):
            request = step.request
            if (request.method, _path_shape(request.path)) not in known:
                warnings.append(
                    f"Scenario '{scenario.name}', step '{step.name}': {request.method} "
                    f"{request.path} is not an operation of the specification."
                )
    return warnings


def _path_shape(path: str) -> str:
    return re.sub(r"\{[^{}]*\}", "{}", path.rstrip("/") or "/")


def _enqueue_rules_run(rules_run_id: int, setup_values: dict[str, str]) -> None:
    from apps.testing.tasks import run_rules_task

    try:
        run_rules_task.delay(rules_run_id, setup_values)
    except Exception as exc:
        logger.exception("Could not queue rules run %s", rules_run_id)
        _fail_rules_run(
            RulesRun.objects.get(pk=rules_run_id),
            f"The run could not be queued for execution: {exc}",
        )


@dataclass
class RulesRunSummary:
    rules_run_id: int
    scenarios: int = 0
    checks: int = 0
    broken: int = 0
    unchecked: int = 0
    stopped: int = 0
    findings: int = 0


def execute_rules_run(
    *,
    rules_run: RulesRun,
    setup_values: dict[str, str],
    client: httpx.Client | None = None,
) -> RulesRunSummary:
    """Run a started rules run's scenarios and record everything they did."""
    if rules_run.status != TestRunStatus.RUNNING:
        raise DomainError(
            f"Only a running rules run can be executed; this one is '{rules_run.status}'."
        )

    summary = RulesRunSummary(rules_run_id=rules_run.pk)
    owns_client = client is None
    if client is None:
        client = executor.make_client(
            timeout=settings.EXECUTOR_TIMEOUT_SECONDS, verify_tls=settings.EXECUTOR_VERIFY_TLS
        )
    try:
        problem = executor.base_url_problem(rules_run.base_url, settings.EXECUTOR_ALLOWED_HOSTS)
        if problem:
            _fail_rules_run(rules_run, problem)
            return summary
        try:
            result = rules_runner.run(
                rules_document.load(rules_run.rules_text),
                base_url=rules_run.base_url,
                setup_values=setup_values,
                client=client,
                max_response_bytes=settings.EXECUTOR_MAX_RESPONSE_BYTES,
                today=rules_run.today if rules_run.today_overridden else None,
            )
        except (rules_document.RulesFileError, rules_runner.RunnerError) as exc:
            _fail_rules_run(rules_run, str(exc))
            return summary

        with transaction.atomic():
            _record_rules_result(rules_run, result, summary)
            rules_run.today = result.today
            if result.auth_error:
                rules_run.status = TestRunStatus.FAILED
                rules_run.status_detail = result.auth_error
            else:
                rules_run.status = TestRunStatus.COMPLETED
            rules_run.completed_at = timezone.now()
            rules_run.save(
                update_fields=["today", "status", "status_detail", "completed_at", "updated_at"]
            )
    except Exception as exc:
        _fail_rules_run(rules_run, f"Execution stopped unexpectedly: {exc}")
        raise
    finally:
        if owns_client:
            client.close()
    return summary


def _record_rules_result(
    rules_run: RulesRun, result: rules_runner.RunResult, summary: RulesRunSummary
) -> None:
    """Write a finished run's scenarios, exchanges and findings."""
    position = 0
    stored: dict[int, RuleExchange] = {}

    def store(exchange: rules_runner.Exchange, scenario: ScenarioResult | None) -> RuleExchange:
        nonlocal position
        if id(exchange) in stored:
            return stored[id(exchange)]
        position += 1
        request, response = exchange.request, exchange.result
        row = RuleExchange.objects.create(
            rules_run=rules_run,
            scenario_result=scenario,
            position=position,
            phase=exchange.phase,
            name=exchange.name[:255],
            method=request.method,
            url=request.url[:2000],
            request_headers=request.headers,
            query_params=request.query_params,
            request_body=request.body,
            status_code=response.status_code,
            response_headers=response.headers,
            response_body=response.body,
            response_time_ms=response.response_time_ms,
            error=response.error,
        )
        stored[id(exchange)] = row
        return row

    if result.auth_exchange is not None:
        store(result.auth_exchange, None)

    for index, scenario in enumerate(result.scenarios):
        row = ScenarioResult.objects.create(
            rules_run=rules_run,
            position=index,
            name=scenario.name[:255],
            status=scenario.status,
            steps_run=scenario.steps_run,
            detail=scenario.failure.detail if scenario.failure else "",
            checks=[_check_record(outcome) for outcome in scenario.outcomes],
            cleanup_problems=scenario.cleanup_problems,
        )
        for exchange in scenario.exchanges:
            store(exchange, row)

        summary.scenarios += 1
        summary.checks += len(scenario.outcomes)
        for outcome in scenario.outcomes:
            if outcome.passed is None:
                summary.unchecked += 1
            elif outcome.passed is False:
                summary.broken += 1
                _rule_finding(row, outcome, result, [store(e, row) for e in outcome.chain])
                summary.findings += 1
        if scenario.failure is not None:
            summary.stopped += 1
            chain = [store(e, row) for e in scenario.failure.chain]
            if _stop_finding(row, scenario.failure, chain):
                summary.findings += 1


def _check_record(outcome: rules_runner.RuleOutcome) -> dict[str, Any]:
    return {
        "rule": outcome.rule.name,
        "severity": outcome.rule.severity,
        "after_step": outcome.after_step,
        "outcome": {True: "passed", False: "broken", None: "unchecked"}[outcome.passed],
        "expected": to_json(outcome.expected),
        "expected_working": outcome.expected_working,
        "actual": to_json(outcome.actual),
        "detail": outcome.detail,
    }


def _rule_finding(
    scenario: ScenarioResult,
    outcome: rules_runner.RuleOutcome,
    result: rules_runner.RunResult,
    chain: list[RuleExchange],
) -> Finding:
    rule = outcome.rule
    return Finding.objects.create(
        scenario_result=scenario,
        title=f"{rule.name}: {outcome.detail}"[:255],
        description=(
            f"{rule.why}\n\nChecked after step '{outcome.after_step}' of scenario "
            f"'{outcome.scenario}': {outcome.detail}. The expected value was worked out as: "
            f"{outcome.expected_working}."
        ),
        severity=rule.severity,
        category=FindingCategory.BUSINESS_RULE_VIOLATION,
        evidence={
            "rule": rule.name,
            "why": rule.why,
            "scenario": outcome.scenario,
            "after_step": outcome.after_step,
            "comparison": rule.compare,
            "tolerance": rule.tolerance,
            "expected": to_json(outcome.expected),
            "expected_working": outcome.expected_working,
            "actual": to_json(outcome.actual),
            "detail": outcome.detail,
            "today": result.today.isoformat(),
            "today_overridden": result.today_overridden,
            "chain": [_chain_entry(row) for row in chain],
        },
    )


def _stop_finding(
    scenario: ScenarioResult, failure: rules_runner.StepFailure, chain: list[RuleExchange]
) -> Finding | None:
    """A scenario that stopped on an unexpected status is a finding.

    One that stopped because nothing answered — a refused connection, a
    timeout — or because the file itself could not be evaluated is not a
    claim about the API, and is reported on the scenario instead.
    """
    status = failure.status_code
    if status is None or not failure.step:
        return None
    server_error = status >= 500
    expected = " or ".join(str(code) for code in failure.expected_status) or "success"
    return Finding.objects.create(
        scenario_result=scenario,
        title=(
            f"Scenario '{failure.scenario}' stopped at step '{failure.step}': "
            f"expected {expected}, got {status}"
        )[:255],
        description=(
            f"{failure.detail} The steps after '{failure.step}', and the rules they "
            f"would have checked, did not run."
        ),
        severity=FindingSeverity.HIGH if server_error else FindingSeverity.MEDIUM,
        category=FindingCategory.BUG if server_error else FindingCategory.INCONSISTENCY,
        evidence={
            "scenario": failure.scenario,
            "step": failure.step,
            "expected_status": list(failure.expected_status),
            "status_code": status,
            "detail": failure.detail,
            "chain": [_chain_entry(row) for row in chain],
        },
    )


def _chain_entry(exchange: RuleExchange) -> dict[str, Any]:
    return {
        "exchange_id": exchange.pk,
        "step": f"{exchange.phase} {exchange.name}",
        "method": exchange.method,
        "url": exchange.url,
        "request_body": _excerpt(exchange.request_body),
        "status_code": exchange.status_code,
        "response_body": _excerpt(exchange.response_body),
        "error": exchange.error,
    }


def _fail_rules_run(rules_run: RulesRun, detail: str) -> None:
    rules_run.refresh_from_db()
    if rules_run.is_finished:
        return
    now = timezone.now()
    rules_run.status = TestRunStatus.FAILED
    rules_run.status_detail = detail
    rules_run.started_at = rules_run.started_at or now
    rules_run.completed_at = now
    rules_run.save(
        update_fields=["status", "status_detail", "started_at", "completed_at", "updated_at"]
    )


def list_rules_runs(
    *, specification: Specification | None = None
) -> models.QuerySet[RulesRun]:
    queryset = RulesRun.objects.select_related("specification").prefetch_related("scenarios")
    if specification is not None:
        queryset = queryset.filter(specification=specification)
    return queryset.annotate(
        findings_count=models.Count("scenarios__findings", distinct=True)
    ).order_by(*RulesRun._meta.ordering)


def summarise_rules_run(rules_run: RulesRun) -> dict[str, int]:
    """Scenario and rule counts for a run, plus how many findings it produced."""
    if not hasattr(rules_run, "findings_count"):
        rules_run = list_rules_runs().get(pk=rules_run.pk)
    scenarios = list(rules_run.scenarios.all())
    summary = {"scenarios": len(scenarios)}
    summary.update(
        {status: sum(s.status == status for s in scenarios) for status in ScenarioStatus.values}
    )
    checks = [check for scenario in scenarios for check in scenario.checks]
    summary["rules_checked"] = len(checks)
    for outcome in ("passed", "broken", "unchecked"):
        summary[f"rules_{outcome}"] = sum(check["outcome"] == outcome for check in checks)
    summary["findings"] = rules_run.findings_count
    return summary


def list_scenario_results(*, rules_run: RulesRun) -> models.QuerySet[ScenarioResult]:
    return rules_run.scenarios.all()


def list_rule_exchanges(*, rules_run: RulesRun) -> models.QuerySet[RuleExchange]:
    return rules_run.exchanges.all()


def list_rules_findings(*, rules_run: RulesRun) -> models.QuerySet[Finding]:
    """A run's findings, most severe first."""
    return (
        Finding.objects.filter(scenario_result__rules_run=rules_run)
        .select_related("scenario_result")
        .order_by(_severity_rank("severity"), "id")
    )


# --- LLM analysis ---------------------------------------------------------------
#
# An analysis asks a language model for a second opinion on each of a finished
# run's findings, and for a summary that groups findings sharing one cause.
# It writes Analysis and FindingAssessment rows and nothing else: a finding's
# severity, category and triage status are never changed by a model.


@dataclass
class AnalysisSummary:
    analysis_id: int
    findings: int = 0
    assessed: int = 0
    requests: int = 0
    warnings: int = 0


def start_analysis(
    *,
    test_run: TestRun | None = None,
    rules_run: RulesRun | None = None,
    enqueue: bool = True,
) -> Analysis:
    """Check a run can be analysed, create a RUNNING analysis, queue it.

    The run is locked while it is checked, so two simultaneous requests cannot
    both start an analysis of it. ``enqueue=False`` is for a caller that
    executes the analysis itself.
    """
    if not settings.LLM_ANALYSIS_ENABLED:
        raise DomainError(
            "LLM analysis is turned off. Set LLM_ANALYSIS_ENABLED=True and provide "
            "ANTHROPIC_API_KEY to use it."
        )
    if (test_run is None) == (rules_run is None):
        raise DomainError("Analyse either a test run or a rules run.")

    with transaction.atomic():
        if test_run is not None:
            run = TestRun.objects.select_for_update().get(pk=test_run.pk)
        else:
            run = RulesRun.objects.select_for_update().get(pk=rules_run.pk)
        if not run.is_finished:
            raise DomainError(
                f"Only a finished run can be analysed; this one is '{run.status}'."
            )
        if run.analyses.filter(
            status__in=[TestRunStatus.PENDING, TestRunStatus.RUNNING]
        ).exists():
            raise DomainError("This run is already being analysed.")

        analysis = Analysis(
            test_run=run if test_run is not None else None,
            rules_run=run if rules_run is not None else None,
            status=TestRunStatus.RUNNING,
            model=settings.LLM_MODEL,
            effort=settings.LLM_EFFORT,
            started_at=timezone.now(),
        )
        analysis.full_clean()
        analysis.save()
        if enqueue:
            transaction.on_commit(lambda: _enqueue_analysis(analysis.pk))
    return analysis


def _enqueue_analysis(analysis_id: int) -> None:
    from apps.testing.tasks import run_analysis_task

    try:
        run_analysis_task.delay(analysis_id)
    except Exception as exc:
        logger.exception("Could not queue analysis %s", analysis_id)
        _fail_analysis(
            Analysis.objects.get(pk=analysis_id),
            f"The analysis could not be queued: {exc}",
        )


def execute_analysis(*, analysis: Analysis, analyst: Analyst | None = None) -> AnalysisSummary:
    """Send a RUNNING analysis's findings to the model and record its answers.

    Findings go most severe first, in batches of LLM_FINDINGS_PER_REQUEST, and
    each batch's assessments commit on their own: a failure part-way keeps
    what was already assessed. A run that fits in one batch is assessed and
    summarised in a single request; a larger one gets a summary request over
    the assessments once every batch is done.
    """
    if analysis.status != TestRunStatus.RUNNING:
        raise DomainError(
            f"Only a running analysis can be executed; this one is '{analysis.status}'."
        )

    summary = AnalysisSummary(analysis_id=analysis.pk)
    usage: dict[str, Any] = {"requests": 0, "models": []}
    warnings: list[str] = []
    try:
        findings = list(_findings_to_analyse(analysis))
        selected = findings[: settings.LLM_MAX_FINDINGS]
        summary.findings = len(selected)
        if len(findings) > len(selected):
            warnings.append(
                f"The run has {len(findings)} findings; only the {len(selected)} most "
                f"severe were analysed (LLM_MAX_FINDINGS)."
            )
        analysis.findings_total = len(findings)
        analysis.findings_selected = len(selected)
        analysis.save(update_fields=["findings_total", "findings_selected", "updated_at"])

        if not selected:
            _complete_analysis(
                analysis,
                summary="The run produced no findings, so there was nothing to analyse.",
                groups=[],
                warnings=warnings,
                usage=usage,
            )
            return summary

        if analyst is None:
            analyst = Analyst.from_settings()
        context = _analysis_context(analysis)
        size = max(1, settings.LLM_FINDINGS_PER_REQUEST)
        batches = [selected[i : i + size] for i in range(0, len(selected), size)]
        single = len(batches) == 1

        assessed: list[tuple[Finding, analysis_prompts.Assessment]] = []
        run_summary, groups = "", []
        for batch in batches:
            reply = analyst.ask(
                prompt=analysis_prompts.assessment_prompt(
                    context, [analysis_prompts.brief(f) for f in batch], include_summary=single
                ),
                schema=(
                    analysis_prompts.ASSESSMENTS_AND_SUMMARY_SCHEMA
                    if single
                    else analysis_prompts.ASSESSMENTS_SCHEMA
                ),
            )
            _add_usage(usage, reply)
            summary.requests += 1
            parsed = analysis_prompts.parse_assessments(
                reply.text, sent_ids=[f.pk for f in batch], include_summary=single
            )
            warnings.extend(parsed.warnings)
            by_id = {f.pk: f for f in batch}
            batch_assessed = [(by_id[a.finding_id], a) for a in parsed.assessments]
            with transaction.atomic():
                FindingAssessment.objects.bulk_create(
                    _assessment_row(analysis, finding, assessment)
                    for finding, assessment in batch_assessed
                )
                analysis.usage = usage
                analysis.warnings = warnings
                analysis.save(update_fields=["usage", "warnings", "updated_at"])
            assessed.extend(batch_assessed)
            if single:
                run_summary, groups = parsed.summary or "", parsed.groups

        summary.assessed = len(assessed)
        if not assessed:
            _fail_analysis(
                analysis,
                "The model's replies did not assess any of the findings.",
                warnings=warnings,
                usage=usage,
            )
            return summary

        if not single:
            reply = analyst.ask(
                prompt=analysis_prompts.summary_prompt(
                    context, [analysis_prompts.digest(f, a) for f, a in assessed]
                ),
                schema=analysis_prompts.SUMMARY_SCHEMA,
            )
            _add_usage(usage, reply)
            summary.requests += 1
            parsed_summary = analysis_prompts.parse_summary(
                reply.text, assessed_ids=[f.pk for f, _ in assessed]
            )
            warnings.extend(parsed_summary.warnings)
            run_summary, groups = parsed_summary.summary, parsed_summary.groups

        _complete_analysis(
            analysis,
            summary=run_summary,
            groups=[group.as_json() for group in groups],
            warnings=warnings,
            usage=usage,
        )
    except (AnalystError, analysis_prompts.ReplyError) as exc:
        # Expected failures: the provider, the credentials, a refusal, an
        # unusable reply. Recorded on the analysis rather than raised.
        _fail_analysis(analysis, str(exc), warnings=warnings, usage=usage)
    except Exception as exc:
        _fail_analysis(
            analysis, f"The analysis stopped unexpectedly: {exc}", warnings=warnings, usage=usage
        )
        raise
    finally:
        summary.warnings = len(warnings)
    return summary


def _findings_to_analyse(analysis: Analysis) -> models.QuerySet[Finding]:
    """The run's findings, most severe first, loaded with what a brief reads."""
    if analysis.test_run_id:
        queryset = Finding.objects.filter(test_case__test_run_id=analysis.test_run_id)
    else:
        queryset = Finding.objects.filter(scenario_result__rules_run_id=analysis.rules_run_id)
    return queryset.select_related("test_case__endpoint", "scenario_result").order_by(
        _severity_rank("severity"), "id"
    )


def _analysis_context(analysis: Analysis) -> dict[str, Any]:
    """What the model is told about the run itself. Never its headers or setup."""
    if analysis.test_run_id:
        run = analysis.test_run
        context: dict[str, Any] = {
            "kind": "generated test cases",
            "results": summarise_test_run(run),
        }
    else:
        run = analysis.rules_run
        context = {
            "kind": "business rules",
            "today": run.today.isoformat() if run.today else None,
            "rules_file": analysis_prompts.clip(run.rules_text),
            "results": summarise_rules_run(run),
        }
    context.update(
        {
            "name": run.name,
            "specification": str(run.specification),
            "base_url": run.base_url,
            "status": run.status,
            "status_detail": run.status_detail,
        }
    )
    return context


def _assessment_row(
    analysis: Analysis, finding: Finding, assessment: analysis_prompts.Assessment
) -> FindingAssessment:
    return FindingAssessment(
        analysis=analysis,
        finding=finding,
        verdict=assessment.verdict,
        confidence=assessment.confidence,
        suggested_severity=assessment.suggested_severity,
        reasoning=assessment.reasoning,
        likely_cause=assessment.likely_cause,
        recommendation=assessment.recommendation,
    )


def _add_usage(usage: dict[str, Any], reply: Any) -> None:
    usage["requests"] = usage.get("requests", 0) + 1
    for name, count in reply.usage.items():
        usage[name] = usage.get(name, 0) + count
    if reply.model and reply.model not in usage["models"]:
        usage["models"].append(reply.model)


def _complete_analysis(
    analysis: Analysis,
    *,
    summary: str,
    groups: list[dict[str, Any]],
    warnings: list[str],
    usage: dict[str, Any],
) -> None:
    analysis.status = TestRunStatus.COMPLETED
    analysis.summary = summary
    analysis.groups = groups
    analysis.warnings = warnings
    analysis.usage = usage
    analysis.completed_at = timezone.now()
    analysis.save(
        update_fields=[
            "status",
            "summary",
            "groups",
            "warnings",
            "usage",
            "completed_at",
            "updated_at",
        ]
    )


def _fail_analysis(
    analysis: Analysis,
    detail: str,
    *,
    warnings: list[str] | None = None,
    usage: dict[str, Any] | None = None,
) -> None:
    analysis.refresh_from_db()
    if analysis.is_finished:
        return
    now = timezone.now()
    analysis.status = TestRunStatus.FAILED
    analysis.status_detail = detail
    analysis.started_at = analysis.started_at or now
    analysis.completed_at = now
    fields = ["status", "status_detail", "started_at", "completed_at", "updated_at"]
    if warnings is not None:
        analysis.warnings = warnings
        fields.append("warnings")
    if usage is not None:
        analysis.usage = usage
        fields.append("usage")
    analysis.save(update_fields=fields)


_VERDICT_COUNTS = {
    f"{verdict}_count": models.Count(
        "assessments", filter=models.Q(assessments__verdict=verdict), distinct=True
    )
    for verdict in AssessmentVerdict.values
}


def list_analyses(
    *, test_run: TestRun | None = None, rules_run: RulesRun | None = None
) -> models.QuerySet[Analysis]:
    queryset = Analysis.objects.annotate(
        assessed_count=models.Count("assessments", distinct=True), **_VERDICT_COUNTS
    ).order_by(*Analysis._meta.ordering)
    if test_run is not None:
        queryset = queryset.filter(test_run=test_run)
    if rules_run is not None:
        queryset = queryset.filter(rules_run=rules_run)
    return queryset


def summarise_analysis(analysis: Analysis) -> dict[str, int]:
    """How many findings were assessed, and how many got each verdict."""
    if not hasattr(analysis, "assessed_count"):
        analysis = list_analyses().get(pk=analysis.pk)
    summary = {"assessed": analysis.assessed_count}
    summary.update(
        {verdict: getattr(analysis, f"{verdict}_count") for verdict in AssessmentVerdict.values}
    )
    return summary


def list_assessments(*, analysis: Analysis) -> models.QuerySet[FindingAssessment]:
    """An analysis's assessments, in the order of their findings' severity."""
    return (
        analysis.assessments.select_related("finding")
        .order_by(_severity_rank("finding__severity"), "finding_id")
    )


def _severity_rank(field_name: str) -> models.Case:
    return models.Case(
        *(
            models.When(**{field_name: severity}, then=position)
            for severity, position in _SEVERITY_RANK.items()
        ),
        output_field=models.IntegerField(),
    )
