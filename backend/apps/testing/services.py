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
from copy import deepcopy
import logging
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any
from urllib.parse import urlsplit

import httpx
import yaml
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.utils import timezone

from apps.core.exceptions import DomainError
from apps.testing import evaluation, executor, generator, openapi, oracles, report, semantic
from apps.testing.analysis import prompts as analysis_prompts
from apps.testing.analysis.client import Analyst, AnalystError
from apps.testing.models import (
    Analysis,
    AssessmentVerdict,
    CreatedResource,
    Endpoint,
    Finding,
    FindingAssessment,
    FindingCategory,
    FindingSeverity,
    RequestRecord,
    ResourceStatus,
    ResponseRecord,
    RuleExchange,
    RulesRun,
    ScenarioResult,
    ScenarioStatus,
    Specification,
    TestCase,
    TestCaseStatus,
    TestCaseType,
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
    return Specification.objects.annotate(
        endpoint_count=models.Count("endpoints", distinct=True)
    ).order_by(*Specification._meta.ordering)


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
    "tags",
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


# --- Uploading a specification file -------------------------------------------


class _NoAliasLoader(yaml.SafeLoader):
    """SafeLoader that refuses anchors and aliases.

    A few kilobytes of nested aliases expand into gigabytes once the document
    is copied into a JSON column. No real API description needs them.
    """

    def compose_node(self, parent, index):
        if self.check_event(yaml.AliasEvent):
            event = self.peek_event()
            raise yaml.composer.ComposerError(
                None, None, "YAML aliases (*name) are not supported", event.start_mark
            )
        return super().compose_node(parent, index)


def decode_specification_file(content: bytes) -> dict[str, Any]:
    """Turn an uploaded JSON or YAML file into a document, or explain why not."""
    if len(content) > settings.SPEC_UPLOAD_MAX_BYTES:
        raise ValidationError(
            {"file": f"The file is larger than {settings.SPEC_UPLOAD_MAX_BYTES} bytes."}
        )
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValidationError({"file": "The file is not UTF-8 text."}) from exc

    try:
        document = json.loads(text)
    except ValueError:
        try:
            document = yaml.load(text, Loader=_NoAliasLoader)  # noqa: S506 - safe loader
        except yaml.YAMLError as exc:
            raise ValidationError(
                {"file": f"The file is neither valid JSON nor valid YAML: {exc}"}
            ) from exc

    if not isinstance(document, dict):
        raise ValidationError({"file": "The file does not hold an OpenAPI or Swagger document."})
    # YAML can hold values JSON cannot, such as dates; store them as text.
    return json.loads(json.dumps(document, default=str))


@transaction.atomic
def upload_specification(
    *, content: bytes, filename: str = "", name: str = "", version: str = ""
) -> tuple[Specification, ImportResult]:
    """Create a specification from an uploaded file and import its endpoints.

    One transaction: a document that cannot be imported leaves no
    specification behind. Name and version default to the document's
    ``info.title`` and ``info.version``.
    """
    document = decode_specification_file(content)
    info = document.get("info") if isinstance(document.get("info"), dict) else {}
    stem = filename.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    name = (name.strip() or str(info.get("title") or "").strip() or stem or "Uploaded API")[:255]
    version = (version.strip() or str(info.get("version") or "").strip())[:64]
    description = info.get("description")

    if Specification.objects.filter(name=name, version=version).exists():
        raise ValidationError(
            {
                "name": f"A specification named '{name}' at version '{version or '(none)'}' "
                f"already exists. Give this upload another name or version."
            }
        )
    specification = create_specification(
        name=name,
        version=version,
        description=description if isinstance(description, str) else "",
        raw_specification=document,
    )
    try:
        result = import_endpoints(specification=specification)
    except ValidationError as exc:
        raise ValidationError({"file": exc.message_dict.get("raw_specification", exc.messages)}) from exc
    return specification, result


# --- Test runs ----------------------------------------------------------------


@transaction.atomic
def create_test_run(
    *,
    specification: Specification,
    name: str = "",
    endpoints: list[Endpoint] | None = None,
    tags: list[str] | None = None,
    **fields: Any,
) -> TestRun:
    """Open a new run in the PENDING state.

    A run is always created pending: nothing has executed yet at the moment the
    caller asks for one. Naming it is a convenience, not information the caller
    must supply, so an omitted name is derived from the specification.

    ``endpoints`` and ``tags`` limit the run to part of the API: the endpoints
    named, plus every endpoint filed under one of the tags. With neither, the
    run tests every endpoint.
    """
    selected = _select_endpoints(specification, endpoints or [], tags or [])
    test_run = TestRun(
        specification=specification,
        name=name.strip() or _default_test_run_name(specification),
        status=TestRunStatus.PENDING,
        **fields,
    )
    test_run.full_clean()
    _validate_target(test_run)
    test_run.save()
    if selected:
        test_run.endpoints.set(selected)
    return test_run


def _select_endpoints(
    specification: Specification, endpoints: list[Endpoint], tags: list[str]
) -> list[Endpoint]:
    """The endpoints a selection names, checked against the specification."""
    foreign = [e for e in endpoints if e.specification_id != specification.pk]
    if foreign:
        raise ValidationError(
            {
                "endpoints": "These endpoints belong to another specification: "
                + ", ".join(str(e.pk) for e in foreign)
                + "."
            }
        )
    selected = {e.pk: e for e in endpoints}
    unknown: list[str] = []
    for tag in dict.fromkeys(t.strip() for t in tags if t.strip()):
        tagged = list(specification.endpoints.filter(tags__contains=[tag]))
        if not tagged:
            unknown.append(tag)
        selected.update((e.pk, e) for e in tagged)
    if unknown:
        known = sorted({t for e in specification.endpoints.all() for t in e.tags})
        raise ValidationError(
            {
                "tags": f"No endpoint is tagged {', '.join(repr(t) for t in unknown)}. "
                + (f"Tags in this API: {', '.join(known)}." if known else "This API uses no tags.")
            }
        )
    return list(selected.values())


def run_endpoints(test_run: TestRun) -> models.QuerySet[Endpoint]:
    """The endpoints a run tests: its selection, or every one of its API's."""
    if test_run.endpoints.exists():
        return test_run.endpoints.all()
    return test_run.specification.endpoints.all()


def describe_scope(test_run: TestRun) -> dict[str, Any]:
    """What part of the API a run covers, for display."""
    selected = list(test_run.endpoints.all())
    total = getattr(test_run, "specification_endpoint_count", None)
    if total is None:
        total = test_run.specification.endpoints.count()
    if not selected:
        return {"all": True, "endpoints": total, "total": total, "tags": []}
    tags = sorted({tag for endpoint in selected for tag in endpoint.tags})
    return {"all": False, "endpoints": len(selected), "total": total, "tags": tags}


@transaction.atomic
def rerun_test_run(*, test_run: TestRun, enqueue: bool = True) -> TestRun:
    """Test the same part of the same API again, with the same headers.

    A new run — a run executes once — copying the target, the headers and the
    endpoint selection, with its cases generated and its execution started.
    The headers are copied on the server, so credentials never have to be
    typed in again.
    """
    rerun = create_test_run(
        specification=test_run.specification,
        base_url=test_run.base_url,
        headers=dict(test_run.headers) if isinstance(test_run.headers, dict) else {},
        endpoints=list(test_run.endpoints.all()),
        cleanup=test_run.cleanup,
    )
    generate_test_cases_for_run(test_run=rerun)
    return start_execution(test_run=rerun, enqueue=enqueue)


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
        .prefetch_related("endpoints")
        .annotate(
            specification_endpoint_count=models.Subquery(
                Endpoint.objects.filter(specification=models.OuterRef("specification"))
                .order_by()
                .values("specification")
                .annotate(count=models.Count("id"))
                .values("count")
            ),
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
    "oracle",
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
    routes = _routes(test_run)
    seed = run_seed(test_run)

    for endpoint in run_endpoints(test_run):
        result.endpoints += 1

        expected = generator.ExpectedStatus.from_codes(
            openapi.documented_status_codes(document, endpoint.path, endpoint.method)
        )
        generated = semantic.build_test_cases(
            endpoint,
            expected=expected,
            seed=seed,
            readable=bool(
                _item_route(routes, endpoint.path, "GET") or routes.get(("GET", endpoint.path))
            ),
        )
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
        oracle=definition.oracle,
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


def run_seed(test_run: TestRun) -> str:
    """What a run's fresh values are derived from.

    The creation time is part of it: run IDs restart when a database is
    reset, and the target may still hold what the earlier run 1 created.
    """
    created = test_run.created_at.isoformat() if test_run.created_at else ""
    return f"run:{test_run.pk}:{created}"


def _routes(test_run: TestRun) -> dict[tuple[str, str], Endpoint]:
    """Every operation of the run's API, by method and path.

    The whole document, not just the run's selection: reading back or
    deleting what a create made may use an operation the run does not test.
    """
    return {(e.method, e.path): e for e in test_run.specification.endpoints.all()}


def _item_route(
    routes: dict[tuple[str, str], Endpoint], collection: str, method: str
) -> tuple[Endpoint, str] | None:
    """The ``{collection}/{id}`` operation for a method, and its ID parameter."""
    prefix = collection.rstrip("/") + "/"
    for (route_method, path), endpoint in sorted(routes.items()):
        if route_method != method or not path.startswith(prefix):
            continue
        match = re.fullmatch(r"\{([^{}/]+)\}", path[len(prefix) :])
        if match:
            return endpoint, match.group(1)
    return None


_ITEM_PATH = re.compile(r"^(?P<collection>.+)/\{(?P<parameter>[^{}/]+)\}$")


def _collection_route(
    routes: dict[tuple[str, str], Endpoint], path: str
) -> tuple[Endpoint, str] | None:
    """The POST that creates what ``{collection}/{id}`` addresses, and the ID
    parameter's name. The inverse of :func:`_item_route`."""
    match = _ITEM_PATH.fullmatch(path)
    if match is None:
        return None
    creator = routes.get(("POST", match.group("collection")))
    if creator is None:
        return None
    return creator, match.group("parameter")


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
    reached at all does. A run marked for cleanup then deletes what it created.
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

        context = _RunContext(
            test_run=test_run,
            client=client,
            seed=run_seed(test_run),
            routes=_routes(test_run),
            document=test_run.specification.raw_specification,
        )
        cases = test_run.test_cases.filter(status=TestCaseStatus.PENDING).select_related(
            "endpoint"
        )
        unreachable = 0
        reached = False
        for test_case in cases.order_by("id").iterator():
            result, findings = _execute_case(context, test_case)
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

        if test_run.cleanup:
            cleanup_test_run(test_run=test_run, client=client)
        complete_test_run(test_run)
    except Exception as exc:
        _fail_run(test_run, f"Execution stopped unexpectedly: {exc}")
        raise
    finally:
        if owns_client:
            client.close()

    return summary


@dataclass
class _RunContext:
    """What every case of one execution pass shares."""

    test_run: TestRun
    client: httpx.Client
    seed: str
    routes: dict[tuple[str, str], Endpoint]
    document: Any
    _codes: dict[int, list[str]] = field(default_factory=dict)

    def documented(self, endpoint: Endpoint) -> list[str]:
        if endpoint.pk not in self._codes:
            self._codes[endpoint.pk] = openapi.documented_status_codes(
                self.document, endpoint.path, endpoint.method
            )
        return self._codes[endpoint.pk]


@dataclass
class _Exchange:
    """One request a case sent, and what came back."""

    endpoint: Endpoint
    step: str
    prepared: executor.PreparedRequest
    result: executor.ExecutionResult

    @property
    def status(self) -> int | None:
        return self.result.status_code

    @property
    def succeeded(self) -> bool:
        return self.status is not None and 200 <= self.status < 300


@dataclass
class _Judged:
    """A finding, the exchange it is about, and the exchanges that reproduce it."""

    draft: evaluation.FindingDraft
    exchange: int
    steps: list[tuple[int, str]]


@dataclass
class _Outcome:
    exchanges: list[_Exchange]
    status: str
    detail: str
    findings: list[_Judged] = field(default_factory=list)


def _send(
    context: _RunContext, endpoint: Endpoint, input_data: dict[str, Any], step: str
) -> _Exchange:
    prepared = executor.prepare(
        method=endpoint.method,
        path=endpoint.path,
        input_data=input_data,
        base_url=context.test_run.base_url,
        run_headers=context.test_run.headers,
    )
    result = executor.send(
        prepared, client=context.client, max_response_bytes=settings.EXECUTOR_MAX_RESPONSE_BYTES
    )
    return _Exchange(endpoint=endpoint, step=step, prepared=prepared, result=result)


def _execute_case(
    context: _RunContext, test_case: TestCase
) -> tuple[executor.ExecutionResult, int]:
    """Send one case's requests, record the evidence, judge it. Returns the
    last result and the number of findings it produced."""
    if test_case.test_type == TestCaseType.DUPLICATE_CREATE:
        outcome = _duplicate_create(context, test_case)
    elif test_case.test_type == TestCaseType.CREATE_READ_BACK:
        outcome = _create_read_back(context, test_case)
    elif _needs_own_resource(test_case):
        outcome = _item_case(context, test_case)
    elif (unsafe := _unsafe_id_case(test_case)) is not None:
        outcome = _skipped(unsafe)
    else:
        outcome = _single_request(context, test_case)
    findings = _record(context, test_case, outcome)
    last = outcome.exchanges[-1].result if outcome.exchanges else executor.ExecutionResult()
    return last, findings


def _input(test_case: TestCase) -> dict[str, Any]:
    return test_case.input_data if isinstance(test_case.input_data, dict) else {}


def _label(endpoint: Endpoint) -> str:
    return f"{endpoint.method} {endpoint.path}"


def _no_response(exchanges: list[_Exchange]) -> _Outcome:
    return _Outcome(exchanges, TestCaseStatus.ERROR, exchanges[-1].result.error)


_SEMANTIC_TYPES = {TestCaseType.CROSS_FIELD, TestCaseType.SUSPICIOUS_VALUE}
# Methods that change the record their path names. Never sent to a guessed ID.
_MUTATING_METHODS = {"PUT", "PATCH", "DELETE"}
# How many more times a request that collided with existing data is sent with
# fresh values before the collision is accepted as the answer. Fresh values are
# drawn from a span the schema bounds (a year range allows about a hundred), so
# one retry can collide again; three make that vanishingly rare.
_COLLISION_RETRIES = 3
# Methods whose ``{collection}/{id}`` happy path is sent to a resource the case
# creates first, instead of to a sample ID. A mutating method's other cases
# go the same way; a GET's other cases are read-only and keep the sample.
_ITEM_METHODS = {"GET", "DELETE", "PUT", "PATCH"}


def _guessed_path(test_case: TestCase, input_data: dict[str, Any]) -> bool:
    """Whether the request's path values are generated samples that may name
    nothing. A case that deliberately breaks a path parameter is not guessing."""
    if not input_data.get("path"):
        return False
    target = test_case.name.split(":", 2)[1] if ":" in test_case.name else ""
    return not target.startswith("path.")


def _retry_input(
    context: _RunContext, test_case: TestCase, attempt: int
) -> dict[str, Any] | None:
    """The same case on values the target has not seen, for one more attempt
    after a collision. A happy path has every free-text field freshened; a
    semantic case is rebuilt, so the relation it tests survives the retry."""
    endpoint = test_case.endpoint
    if test_case.test_type == TestCaseType.HAPPY_PATH:
        return semantic.refreshed_input(
            _input(test_case),
            endpoint.request_schema,
            _retry_token(context, test_case, attempt),
        )
    if test_case.test_type in _SEMANTIC_TYPES:
        expected = generator.ExpectedStatus.from_codes(context.documented(endpoint))
        return semantic.retry_input(
            endpoint,
            expected=expected,
            seed=context.seed,
            case_name=test_case.name,
            attempt=attempt,
        )
    return None


def _retry_token(context: _RunContext, test_case: TestCase, attempt: int) -> str:
    return semantic.token_for(
        context.seed, _label(test_case.endpoint), test_case.name, f"retry{attempt}"
    )


def _single_request(context: _RunContext, test_case: TestCase) -> _Outcome:
    """A case that is one request — unless it collides with data already in
    the target, in which case it is sent once more with fresh values."""
    endpoint = test_case.endpoint
    label = _label(endpoint)
    input_data = _input(test_case)
    happy = test_case.test_type == TestCaseType.HAPPY_PATH

    exchanges = [_send(context, endpoint, input_data, "Send the request")]
    first = exchanges[0]
    if first.status is None:
        return _no_response(exchanges)

    judged: list[_Judged] = []
    decisive = 0
    conflict = oracles.is_conflict(first.status, first.result.body)
    if conflict:
        note = evaluation.undocumented_conflict(label, first.status, context.documented(endpoint))
        if note is not None:
            judged.append(_Judged(note, 0, [(0, "")]))

    if conflict:
        for attempt in range(1, _COLLISION_RETRIES + 1):
            retry = _retry_input(context, test_case, attempt)
            if retry is None:
                break
            exchanges.append(_send(context, endpoint, retry, "Send again with fresh values"))
            latest = exchanges[-1]
            if latest.status is None:
                return _no_response(exchanges)
            decisive = len(exchanges) - 1
            if not oracles.is_conflict(latest.status, latest.result.body):
                break
    elif happy and first.status >= 500:
        retry = _retry_input(context, test_case, 1)
        if retry is not None:
            exchanges.append(_send(context, endpoint, retry, "Send again with fresh values"))
            second = exchanges[1]
            if second.status is None:
                return _no_response(exchanges)
            if second.succeeded:
                # The crash was about the values, not the request: they exist.
                crash = evaluation.collision_crash(label, first.status, second.status)
                return _Outcome(
                    exchanges,
                    TestCaseStatus.FAILED,
                    f"{first.status} with the example values; {second.status} with fresh ones.",
                    [_Judged(crash, 0, [(0, "These values already exist in the target.")])],
                )

    current = exchanges[decisive]
    verdict = evaluation.evaluate(
        label=label,
        case_name=test_case.name,
        test_type=test_case.test_type,
        expected_status_code=test_case.expected_status_code,
        status_code=current.status,
        body=current.result.body,
        oracle=test_case.oracle if isinstance(test_case.oracle, dict) else None,
        guessed_path=_guessed_path(test_case, input_data),
    )
    status, detail = verdict.status, verdict.detail
    if decisive:
        attempts = "sent again" if decisive == 1 else f"sent {decisive} more times"
        detail = (
            f"The first attempt collided with existing data ({first.status}); "
            f"{attempts} with fresh values: {detail}"
        )
    judged += [_Judged(draft, decisive, [(decisive, "")]) for draft in verdict.findings]

    checked = {TestCaseType.HAPPY_PATH, TestCaseType.CROSS_FIELD, TestCaseType.SUSPICIOUS_VALUE}
    if current.succeeded and test_case.test_type in checked:
        drafts = _response_findings(context, endpoint, current)
        judged += [_Judged(draft, decisive, [(decisive, "")]) for draft in drafts]
        if drafts and status == TestCaseStatus.PASSED:
            status = TestCaseStatus.FAILED
            detail += " " + " ".join(draft.title + "." for draft in drafts)

    return _Outcome(exchanges, status, detail, judged)


def _response_findings(
    context: _RunContext, endpoint: Endpoint, exchange: _Exchange
) -> list[evaluation.FindingDraft]:
    success = generator.ExpectedStatus.from_codes(context.documented(endpoint)).success
    return evaluation.response_findings(
        _label(endpoint),
        sent=exchange.prepared.body,
        received=exchange.result.body,
        # The stored schema describes the lowest documented 2xx only.
        response_schema=endpoint.response_schema if exchange.status == success else None,
        check_echo=endpoint.method in {"POST", "PUT", "PATCH"},
    )


def _create(
    context: _RunContext,
    endpoint: Endpoint,
    input_data: dict[str, Any],
    exchanges: list[_Exchange],
    test_case: TestCase,
) -> _Exchange | None:
    """The setup create of a sequence, sent again with fresh values while it
    collides with existing data. None when it did not succeed."""
    exchanges.append(_send(context, endpoint, input_data, "Create the resource"))
    created = exchanges[-1]
    for attempt in range(1, _COLLISION_RETRIES + 1):
        if not oracles.is_conflict(created.status, created.result.body):
            break
        retry = semantic.refreshed_input(
            input_data, endpoint.request_schema, _retry_token(context, test_case, attempt)
        )
        if retry is None:
            break
        exchanges.append(_send(context, endpoint, retry, "Create the resource"))
        created = exchanges[-1]
    return created if created.succeeded else None


def _setup_failed(exchanges: list[_Exchange], purpose: str = "the sequence") -> _Outcome:
    last = exchanges[-1]
    if last.status is None:
        return _no_response(exchanges)
    return _Outcome(
        exchanges,
        TestCaseStatus.ERROR,
        f"The setup create ({_label(last.endpoint)}) answered {last.status}, so {purpose} "
        f"could not continue. That operation's own happy path reports on the create.",
    )


def _duplicate_create(context: _RunContext, test_case: TestCase) -> _Outcome:
    """Create a resource, then send exactly the same create again."""
    endpoint = test_case.endpoint
    label = _label(endpoint)
    exchanges: list[_Exchange] = []
    created = _create(context, endpoint, _input(test_case), exchanges, test_case)
    if created is None:
        return _setup_failed(exchanges)
    first = len(exchanges) - 1

    body = created.prepared.body
    input_data = {**_input(test_case), "body": body}
    exchanges.append(_send(context, endpoint, input_data, "Send the identical create again"))
    second = exchanges[-1]
    if second.status is None:
        return _no_response(exchanges)

    verdict = evaluation.duplicate_create(label, created.status, second.status, second.result.body)
    steps = [(first, "Setup: create the resource."), (len(exchanges) - 1, "Send the same request again.")]
    judged = [_Judged(draft, len(exchanges) - 1, steps) for draft in verdict.findings]
    if oracles.is_conflict(second.status, second.result.body):
        note = evaluation.undocumented_conflict(label, second.status, context.documented(endpoint))
        if note is not None:
            judged.append(_Judged(note, len(exchanges) - 1, steps))
    return _Outcome(exchanges, verdict.status, verdict.detail, judged)


def _create_read_back(context: _RunContext, test_case: TestCase) -> _Outcome:
    """Create a resource, then find it again by ID, or else in the list."""
    endpoint = test_case.endpoint
    label = _label(endpoint)
    exchanges: list[_Exchange] = []
    created = _create(context, endpoint, _input(test_case), exchanges, test_case)
    if created is None:
        return _setup_failed(exchanges)
    create_index = len(exchanges) - 1

    sent = created.prepared.body
    path_values = dict(created.prepared.path_params)
    item = _item_route(context.routes, endpoint.path, "GET")
    identifier = oracles.resource_id(created.result.body, item[1] if item else None)
    collection = context.routes.get(("GET", endpoint.path))

    if item is not None and identifier is not None:
        reader, parameter = item
        read_input = _read_input(reader, {**path_values, parameter: identifier})
        exchanges.append(_send(context, reader, read_input, "Read it back by ID"))
        read = exchanges[-1]
        if read.status is None:
            return _no_response(exchanges)
        found = read.succeeded
        verdict = evaluation.read_back(
            label,
            how=_label(reader),
            status_code=read.status,
            found=found,
            mismatches=oracles.echo_mismatches(sent, read.result.body) if found else [],
            identifier=identifier,
        )
    elif collection is not None:
        exchanges.append(
            _send(context, collection, _read_input(collection, path_values), "List the resources")
        )
        read = exchanges[-1]
        if read.status is None:
            return _no_response(exchanges)
        items = oracles.list_items(read.result.body) if read.succeeded else None
        if read.status < 500 and items is None:
            return _Outcome(
                exchanges,
                TestCaseStatus.ERROR,
                f"The list answered {read.status} with a body AegisTest could not read as a "
                f"list, so the created resource could not be looked for.",
            )
        match = oracles.find_created(items or [], identifier, sent)
        verdict = evaluation.read_back(
            label,
            how=_label(collection),
            status_code=read.status,
            found=match is not None,
            mismatches=oracles.echo_mismatches(sent, match) if match is not None else [],
            identifier=identifier,
            uncertain=len(items or []) >= 10 or not isinstance(read.result.body, list),
        )
    else:
        return _Outcome(
            exchanges,
            TestCaseStatus.ERROR,
            "The create response carries no ID, and the API has no list to look in.",
        )

    steps = [(create_index, "Setup: create the resource."), (len(exchanges) - 1, "Read it back.")]
    judged = [_Judged(draft, len(exchanges) - 1, steps) for draft in verdict.findings]
    return _Outcome(exchanges, verdict.status, verdict.detail, judged)


def _case_target(test_case: TestCase) -> str:
    """The parameter or field a generated case breaks: ``path.id``, ``query.limit``,
    ``name``; empty for a happy path or a sequence."""
    return test_case.name.split(":", 2)[1] if ":" in test_case.name else ""


def _needs_own_resource(test_case: TestCase) -> bool:
    """Whether a case on ``{collection}/{id}`` must address a resource of its own.

    Every case of a PUT, PATCH or DELETE does: sent to the sample ID it would
    change or remove a record the run did not create. A GET only needs one for
    its happy path; its other cases are read-only. The exception is a case
    that breaks the ID parameter itself: that value is the test.
    """
    endpoint = test_case.endpoint
    if endpoint.method not in _ITEM_METHODS:
        return False
    match = _ITEM_PATH.fullmatch(endpoint.path)
    if match is None:
        return False
    if _case_target(test_case) == f"path.{match.group('parameter')}":
        return False
    if endpoint.method in _MUTATING_METHODS:
        return True
    return test_case.test_type == TestCaseType.HAPPY_PATH


def _names_a_record(value: Any) -> bool:
    """Whether a path value could address an existing record: a number, or
    text made of digits. ``"not-an-integer"`` cannot."""
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return True
    return isinstance(value, str) and value.strip().isdigit()


def _unsafe_id_case(test_case: TestCase) -> str | None:
    """Why a mutating case that breaks the ID parameter must not be sent, or None.

    ``invalid_type:path.id`` sends text where a number is required and cannot
    reach a record; ``below_minimum:path.id`` sends a number that may well be
    one, and a PUT or DELETE to it would change data the run does not own.
    """
    endpoint = test_case.endpoint
    if endpoint.method not in _MUTATING_METHODS:
        return None
    match = _ITEM_PATH.fullmatch(endpoint.path)
    if match is None or _case_target(test_case) != f"path.{match.group('parameter')}":
        return None
    value = (_input(test_case).get("path") or {}).get(match.group("parameter"))
    if not _names_a_record(value):
        return None
    return (
        f"Not sent: the case breaks the '{match.group('parameter')}' path parameter with "
        f"{value!r}, which may name a real record, and a {endpoint.method} to it would change "
        f"data the run did not create."
    )


def _skipped(reason: str) -> _Outcome:
    return _Outcome([], TestCaseStatus.SKIPPED, reason)


def _owned(exchange: _Exchange) -> bool:
    """Whether a create's answer proves the record is the run's own to change
    or delete. Only a 201 does: a 200 may be an existing record returned by an
    upsert or a find-or-create."""
    return exchange.status == 201


def _setup_input(context: _RunContext, creator: Endpoint, test_case: TestCase) -> dict[str, Any] | None:
    """A valid create request for ``creator``, with values of this case's own."""
    expected = generator.ExpectedStatus.from_codes(context.documented(creator))
    generated = semantic.build_test_cases(
        creator,
        expected=expected,
        seed=semantic.token_for(context.seed, _label(test_case.endpoint), test_case.name, "setup"),
        readable=False,
    )
    for definition in generated.definitions:
        if definition.test_type == TestCaseType.HAPPY_PATH:
            return deepcopy(definition.input_data)
    return None


def _item_case(context: _RunContext, test_case: TestCase) -> _Outcome:
    """A case on one resource, sent to a resource the case creates first.

    The generated path value is a sample (``id: 1``) that may name nothing —
    or somebody's data, which a DELETE would remove and a PUT would overwrite.
    Creating a resource first makes the request about something that exists
    and belongs to the run, so its answer is a verdict rather than a guess,
    and the case keeps whatever it deliberately breaks: an invalid body, a
    bad query parameter, a wrong-typed parent ID.

    A mutating case with nothing disposable to address is skipped, never sent
    to the sample. A create that fails, or answers without an ID, leaves the
    case unjudged: an ID is never guessed.
    """
    endpoint = test_case.endpoint
    label = _label(endpoint)
    input_data = _input(test_case)
    mutating = endpoint.method in _MUTATING_METHODS
    parameter = _ITEM_PATH.fullmatch(endpoint.path).group("parameter")
    found = _collection_route(context.routes, endpoint.path)
    creator = found[0] if found is not None else None

    if creator is None:
        if mutating:
            return _skipped(
                f"Not sent: the document has no POST {endpoint.path.rsplit('/', 1)[0]} to "
                f"create a disposable record, and a {endpoint.method} to the sample ID would "
                f"change a record the run did not create."
            )
        return _single_request(context, test_case)
    setup = _setup_input(context, creator, test_case)
    if setup is None:
        if mutating:
            return _skipped(
                f"Not sent: no valid request to {_label(creator)} can be built from the "
                f"document, so there is no disposable record for this {endpoint.method}."
            )
        return _single_request(context, test_case)

    exchanges: list[_Exchange] = []
    created = _create(context, creator, setup, exchanges, test_case)
    if created is None:
        return _setup_failed(exchanges, f"the {endpoint.method} of a created resource")
    create_index = len(exchanges) - 1
    if mutating and not _owned(created):
        return _Outcome(
            exchanges,
            TestCaseStatus.SKIPPED,
            f"Not sent: the create ({_label(creator)}) answered {created.status} rather than "
            f"201, so the record it returned may have existed already and is not the run's "
            f"to {endpoint.method.lower()}.",
        )

    identifier = oracles.resource_id(created.result.body, parameter)
    if identifier is None:
        return _Outcome(
            exchanges,
            TestCaseStatus.ERROR,
            f"The create ({_label(creator)}) answered {created.status}, but its response "
            f"carries no ID (looked for '{parameter}', 'id', '_id' and 'uuid'), so the new "
            f"resource cannot be addressed and the case could not be judged.",
        )

    # The case's own path values win over the create's, so a case that breaks a
    # parent parameter still breaks it; only the ID is the created record's.
    path_values = {
        **created.prepared.path_params,
        **dict(input_data.get("path") or {}),
        parameter: identifier,
    }
    request = {**input_data, "path": path_values}
    exchanges.append(_send(context, endpoint, request, f"{endpoint.method} it"))
    current = exchanges[-1]
    if current.status is None:
        return _no_response(exchanges)

    # Fresh body values while the request under test collides with other data.
    retryable = test_case.test_type == TestCaseType.HAPPY_PATH or test_case.test_type in _SEMANTIC_TYPES
    attempts = 0
    while retryable and oracles.is_conflict(current.status, current.result.body) and attempts < _COLLISION_RETRIES:
        attempts += 1
        retry = _retry_input(context, test_case, attempts)
        if retry is None:
            break
        exchanges.append(
            _send(context, endpoint, {**retry, "path": path_values}, "Send again with fresh values")
        )
        current = exchanges[-1]
        if current.status is None:
            return _no_response(exchanges)
    index = len(exchanges) - 1
    steps = [(create_index, "Setup: create the resource."), (index, "")]

    verdict = evaluation.evaluate(
        label=label,
        case_name=test_case.name,
        test_type=test_case.test_type,
        expected_status_code=test_case.expected_status_code,
        status_code=current.status,
        body=current.result.body,
        oracle=test_case.oracle if isinstance(test_case.oracle, dict) else None,
        created_resource=True,
    )
    status, detail = verdict.status, verdict.detail
    if attempts:
        detail = (
            f"The first attempt collided with existing data ({exchanges[create_index + 1].status}); "
            f"sent again with fresh values: {detail}"
        )
    judged = [_Judged(draft, index, steps) for draft in verdict.findings]

    checked = {TestCaseType.HAPPY_PATH, *_SEMANTIC_TYPES}
    if current.succeeded and test_case.test_type in checked:
        drafts = _response_findings(context, endpoint, current)
        judged += [_Judged(draft, index, steps) for draft in drafts]
        if drafts and status == TestCaseStatus.PASSED:
            status = TestCaseStatus.FAILED
            detail += " " + " ".join(draft.title + "." for draft in drafts)

    reader = _item_route(context.routes, creator.path, "GET")
    if endpoint.method == "DELETE" and current.succeeded and reader is not None:
        reader_endpoint, reader_parameter = reader
        exchanges.append(
            _send(
                context,
                reader_endpoint,
                _read_input(reader_endpoint, {**path_values, reader_parameter: identifier}),
                "Read it back after the delete",
            )
        )
        after = exchanges[-1]
        if after.status is None:
            return _no_response(exchanges)
        gone = evaluation.deleted_read_back(
            label, how=_label(reader_endpoint), status_code=after.status, identifier=identifier
        )
        after_steps = steps + [(len(exchanges) - 1, "Read it back after the delete.")]
        judged += [_Judged(draft, len(exchanges) - 1, after_steps) for draft in gone.findings]
        if gone.status != TestCaseStatus.PASSED and status == TestCaseStatus.PASSED:
            status = gone.status
        detail += " " + gone.detail

    return _Outcome(exchanges, status, detail, judged)


def _read_input(reader: Endpoint, path_values: dict[str, Any]) -> dict[str, Any]:
    """A valid request to a GET, with the given path values."""
    generated = generator.generate_test_cases(reader)
    base = generated.definitions[0].input_data if not generated.is_empty else {}
    names = {
        p.get("name") for p in reader.parameters or [] if isinstance(p, dict) and p.get("in") == "path"
    }
    return {
        "path": {name: value for name, value in path_values.items() if name in names},
        "query": dict(base.get("query") or {}),
        "headers": dict(base.get("headers") or {}),
        "body": None,
    }


def _record(context: _RunContext, test_case: TestCase, outcome: _Outcome) -> int:
    """Store a case's requests, responses, findings and created resources."""
    findings = 0
    with transaction.atomic():
        responses: list[ResponseRecord | None] = []
        for exchange in outcome.exchanges:
            request_record = RequestRecord.objects.create(
                test_case=test_case,
                method=exchange.prepared.method,
                url=exchange.prepared.url[: RequestRecord._meta.get_field("url").max_length],
                headers=executor.redact_headers(exchange.prepared.headers),
                query_params=exchange.prepared.query_params,
                path_params=exchange.prepared.path_params,
                body=exchange.prepared.body,
                error=exchange.result.error,
            )
            response_record = None
            if exchange.status is not None:
                response_record = ResponseRecord.objects.create(
                    request_record=request_record,
                    status_code=exchange.status,
                    headers=executor.redact_headers(exchange.result.headers),
                    body=exchange.result.body,
                    response_time_ms=exchange.result.response_time_ms,
                )
                _track_created(context, test_case, exchange)
                _mark_deleted(context, test_case, exchange)
            responses.append(response_record)

        for judged in outcome.findings:
            draft = judged.draft
            if not draft.fails_case and _already_noted(test_case, draft):
                continue
            exchange = outcome.exchanges[judged.exchange]
            Finding.objects.create(
                test_case=test_case,
                response_record=responses[judged.exchange],
                title=draft.title[: Finding._meta.get_field("title").max_length],
                description=draft.description,
                severity=draft.severity,
                category=draft.category,
                confidence=draft.confidence,
                probable_cause=draft.probable_cause[
                    : Finding._meta.get_field("probable_cause").max_length
                ],
                reproduction=[
                    _reproduction_step(number, outcome.exchanges[index], note)
                    for number, (index, note) in enumerate(judged.steps, start=1)
                ],
                evidence=_evidence(test_case, exchange.prepared, exchange.result),
            )
            findings += 1

        test_case.status = outcome.status
        test_case.status_detail = outcome.detail
        test_case.save(update_fields=["status", "status_detail", "updated_at"])

    return findings


def _already_noted(test_case: TestCase, draft: evaluation.FindingDraft) -> bool:
    """A documentation note is made once per operation, not once per case."""
    return Finding.objects.filter(
        test_case__test_run_id=test_case.test_run_id,
        test_case__endpoint_id=test_case.endpoint_id,
        title=draft.title[: Finding._meta.get_field("title").max_length],
    ).exists()


def _reproduction_step(number: int, exchange: _Exchange, note: str) -> dict[str, Any]:
    prepared = exchange.prepared
    return {
        "step": number,
        "note": note,
        "method": prepared.method,
        "url": prepared.url,
        "query": prepared.query_params,
        "body": prepared.body,
        "status_code": exchange.status,
    }


def _track_created(context: _RunContext, test_case: TestCase, exchange: _Exchange) -> None:
    """Remember a resource a successful create made, and how to delete it.

    Only a 201 proves the record is new. A POST documented as a create that
    answers 200 is recorded without a delete URL: it may be an upsert or a
    find-or-create handing back a record that existed, which cleanup must not
    remove. Any other 2xx is not a create at all - a search, an action.
    """
    if exchange.prepared.method != "POST" or not exchange.succeeded:
        return
    route = _item_route(context.routes, exchange.endpoint.path, "DELETE")
    identifier = oracles.resource_id(exchange.result.body, route[1] if route else None)
    documented_create = (
        generator.ExpectedStatus.from_codes(context.documented(exchange.endpoint)).success == 201
    )
    if not _owned(exchange) and not (documented_create and identifier is not None):
        return

    delete_url = ""
    detail = ""
    if not _owned(exchange):
        detail = (
            f"The create answered {exchange.status} rather than 201, so this may be a record "
            f"that existed already; it is not deleted automatically."
        )
    elif identifier is None:
        detail = "The create response carried no ID, so it cannot be deleted automatically."
    elif route is None:
        detail = "The API documents no DELETE for this resource."
    else:
        remover, parameter = route
        delete_url = executor.prepare(
            method="DELETE",
            path=remover.path,
            input_data={"path": {**exchange.prepared.path_params, parameter: identifier}},
            base_url=context.test_run.base_url,
        ).url
    CreatedResource.objects.create(
        test_run=context.test_run,
        test_case=test_case,
        endpoint=exchange.endpoint,
        resource_id=identifier or "",
        delete_url=delete_url[: CreatedResource._meta.get_field("delete_url").max_length],
        detail=detail,
    )


def _mark_deleted(context: _RunContext, test_case: TestCase, exchange: _Exchange) -> None:
    """A DELETE the run sent itself takes the resource off the cleanup list."""
    if exchange.prepared.method != "DELETE" or not exchange.succeeded:
        return
    CreatedResource.objects.filter(
        test_run=context.test_run,
        delete_url=exchange.prepared.url[: CreatedResource._meta.get_field("delete_url").max_length],
    ).exclude(status=ResourceStatus.DELETED).update(
        status=ResourceStatus.DELETED,
        detail=f"Deleted by the run's own '{test_case.name}' case ({exchange.status}).",
        updated_at=timezone.now(),
    )


# --- Cleaning up ----------------------------------------------------------------


@dataclass
class CleanupSummary:
    deleted: int = 0
    failed: int = 0
    left: int = 0


def cleanup_test_run(*, test_run: TestRun, client: httpx.Client | None = None) -> CleanupSummary:
    """DELETE every resource the run created that can be deleted.

    A 404 or 410 counts as deleted: the resource is gone either way. What
    cannot be deleted — no ID, no DELETE operation, a refused DELETE — stays
    listed as left behind.
    """
    problem = executor.base_url_problem(test_run.base_url, settings.EXECUTOR_ALLOWED_HOSTS)
    if problem:
        raise ValidationError({"base_url": problem})

    owns_client = client is None
    if client is None:
        client = executor.make_client(
            timeout=settings.EXECUTOR_TIMEOUT_SECONDS, verify_tls=settings.EXECUTOR_VERIFY_TLS
        )
    headers = {"Accept": "application/json"}
    headers.update({str(k): str(v) for k, v in (test_run.headers or {}).items()})
    try:
        pending = test_run.created_resources.exclude(status=ResourceStatus.DELETED).exclude(
            delete_url=""
        )
        for resource in pending.order_by("-id"):
            result = executor.send(
                executor.PreparedRequest(
                    method="DELETE",
                    url=resource.delete_url,
                    headers=headers,
                    query_params={},
                    path_params={},
                ),
                client=client,
                max_response_bytes=settings.EXECUTOR_MAX_RESPONSE_BYTES,
            )
            if result.status_code is not None and (
                200 <= result.status_code < 300 or result.status_code in {404, 410}
            ):
                resource.status = ResourceStatus.DELETED
                resource.detail = f"DELETE answered {result.status_code}."
            else:
                resource.status = ResourceStatus.DELETE_FAILED
                resource.detail = (
                    f"DELETE answered {result.status_code}."
                    if result.status_code is not None
                    else f"DELETE failed: {result.error}"
                )
            resource.save(update_fields=["status", "detail", "updated_at"])
    finally:
        if owns_client:
            client.close()
    return summarise_resources(test_run)


def summarise_resources(test_run: TestRun) -> CleanupSummary:
    counts = dict(
        test_run.created_resources.order_by().values_list("status").annotate(n=models.Count("id"))
    )
    return CleanupSummary(
        deleted=counts.get(ResourceStatus.DELETED, 0),
        failed=counts.get(ResourceStatus.DELETE_FAILED, 0),
        left=counts.get(ResourceStatus.LEFT, 0),
    )


def list_created_resources(*, test_run: TestRun) -> models.QuerySet[CreatedResource]:
    return test_run.created_resources.select_related("endpoint")


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
    elif test_case.status == TestCaseStatus.SKIPPED:
        summary.skipped += 1
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


# --- PDF report -----------------------------------------------------------------

# Findings printed in a report, most severe first. Every finding stays
# available from the API; a PDF of thousands of pages helps nobody.
_REPORT_MAX_FINDINGS = 300
_REPORT_MAX_CASES = 500
_REPORT_MAX_SEQUENCES = 60
_REPORT_MAX_RESOURCES = 100
_REPORT_MAX_NOT_TESTED = 40
_REPORT_MAX_REASONS = 12


def test_run_report_pdf(*, test_run: TestRun) -> bytes:
    """A test run's findings, endpoint results and latest AI analysis, as a PDF."""
    return report.render(test_run_report(test_run=test_run))


def test_run_report(*, test_run: TestRun) -> report.RunReport:
    test_run = list_test_runs().get(pk=test_run.pk)
    all_findings = (
        Finding.objects.filter(test_case__test_run=test_run)
        .select_related("test_case__endpoint", "response_record")
        .order_by(_severity_rank("severity"), "id")
    )
    findings_total = all_findings.count()
    findings = list(all_findings[:_REPORT_MAX_FINDINGS])

    analysis = (
        list_analyses(test_run=test_run).filter(status=TestRunStatus.COMPLETED).first()
    )
    assessments: dict[int, FindingAssessment] = {}
    if analysis is not None:
        assessments = {a.finding_id: a for a in analysis.assessments.all()}

    test_cases = list(
        test_run.test_cases.select_related("endpoint")
        .prefetch_related("request_records__response")
        .order_by("endpoint__path", "endpoint__method", "id")
    )
    cases, sequences = _report_cases(test_run, test_cases)
    not_run = [
        c for c in test_cases
        if c.status in {TestCaseStatus.PENDING, TestCaseStatus.SKIPPED, TestCaseStatus.RUNNING}
    ]
    resources = list(list_created_resources(test_run=test_run))
    # Operations this run executed nothing against, judged from this run's own
    # cases: a case executed in an earlier run is no evidence for this one.
    executed_endpoints = {
        c.endpoint_id
        for c in test_cases
        if c.status in {TestCaseStatus.PASSED, TestCaseStatus.FAILED, TestCaseStatus.ERROR}
    }
    not_tested = list(
        test_run.specification.endpoints.exclude(pk__in=executed_endpoints).order_by(
            "path", "method"
        )
    )

    return report.RunReport(
        specification=str(test_run.specification),
        run_name=test_run.name,
        run_id=test_run.pk,
        base_url=test_run.base_url,
        status=test_run.status,
        status_detail=test_run.status_detail,
        started_at=_local(test_run.started_at),
        completed_at=_local(test_run.completed_at),
        generated_at=_local(timezone.now()),
        results=summarise_test_run(test_run),
        endpoints=_report_endpoints(test_run),
        findings=[_report_finding(f, assessments.get(f.pk)) for f in findings],
        findings_total=findings_total,
        analysis=_report_analysis(analysis) if analysis is not None else None,
        scope=_scope_text(describe_scope(test_run)),
        resources=[
            report.ReportResource(
                endpoint=f"{r.endpoint.method} {r.endpoint.path}",
                resource_id=r.resource_id,
                status=r.status,
                detail=r.detail,
            )
            for r in resources[:_REPORT_MAX_RESOURCES]
        ],
        resources_total=len(resources),
        cases=cases[:_REPORT_MAX_CASES],
        cases_total=len(cases),
        sequences=sequences[:_REPORT_MAX_SEQUENCES],
        sequences_total=len(sequences),
        not_tested=[f"{e.method} {e.path}" for e in not_tested[:_REPORT_MAX_NOT_TESTED]],
        not_tested_total=len(not_tested),
        not_run_reasons=_reasons(not_run),
        inconclusive_reasons=_reasons(
            [c for c in test_cases if c.status == TestCaseStatus.ERROR]
        ),
    )


def _report_cases(
    test_run: TestRun, test_cases: list[TestCase]
) -> tuple[list[report.ReportCase], list[report.ReportSequence]]:
    """Every case of the run as the report shows it, and the multi-request ones
    again as sequences. Request values are the ones sent, with secrets masked."""
    finding_counts = dict(
        Finding.objects.filter(test_case__test_run=test_run)
        .values_list("test_case_id")
        .annotate(n=models.Count("id"))
    )
    cases: list[report.ReportCase] = []
    sequences: list[report.ReportSequence] = []
    for test_case in test_cases:
        label = f"{test_case.endpoint.method} {test_case.endpoint.path}"
        records = list(test_case.request_records.all())
        responses = [_response_of(r) for r in records]
        # The request under test: the last one with the operation's own method
        # (a sequence's setup create or read-back uses another), else the last.
        decisive = next(
            (i for i in range(len(records) - 1, -1, -1)
             if records[i].method == test_case.endpoint.method),
            len(records) - 1,
        )
        actual = responses[decisive].status_code if records and responses[decisive] else None
        if records:
            request = _request_text(
                records[decisive].method, records[decisive].url, test_run.base_url,
                records[decisive].query_params, records[decisive].body,
            )
        else:
            input_data = _input(test_case)
            request = _request_text(
                test_case.endpoint.method, test_case.endpoint.path, "",
                input_data.get("query"), input_data.get("body"),
            )
        cases.append(
            report.ReportCase(
                id=test_case.pk,
                name=test_case.name,
                endpoint=label,
                test_type=test_case.test_type,
                purpose=test_case.description,
                request=request,
                expected_status=test_case.expected_status_code,
                actual_status=actual,
                status=test_case.status,
                detail=test_case.status_detail,
                requests_sent=len(records),
                findings=finding_counts.get(test_case.pk, 0),
            )
        )
        if len(records) > 1:
            sequences.append(
                report.ReportSequence(
                    case=test_case.name,
                    endpoint=label,
                    status=test_case.status,
                    steps=[
                        {
                            "method": record.method,
                            "url": _relative_url(record.url, test_run.base_url),
                            "status_code": response.status_code if response else None,
                            "note": record.error if response is None else "",
                        }
                        for record, response in zip(records, responses)
                    ],
                )
            )
    return cases, sequences


def _response_of(record: RequestRecord) -> ResponseRecord | None:
    try:
        return record.response
    except ResponseRecord.DoesNotExist:
        return None


def _relative_url(url: str, base_url: str) -> str:
    return url[len(base_url):] if base_url and url.startswith(base_url) else url


def _request_text(method: str, url: str, base_url: str, query: Any, body: Any) -> str:
    """One line naming the request, then its query and body, secrets masked."""
    text = f"{method} {_relative_url(url, base_url)}"
    if isinstance(query, dict) and query:
        text += "\nquery: " + json.dumps(executor.redact_body(query), ensure_ascii=False)
    if body is not None:
        rendered = (
            body if isinstance(body, str)
            else json.dumps(executor.redact_body(body), ensure_ascii=False)
        )
        text += "\nbody: " + rendered
    return text


def _reasons(test_cases: list[TestCase]) -> list[tuple[str, int]]:
    """Cases grouped by the first sentence of their status detail, most common first."""
    counts: dict[str, int] = {}
    for test_case in test_cases:
        reason = (test_case.status_detail or "no reason recorded").split(". ")[0].strip().rstrip(".")
        reason = reason if len(reason) <= 140 else reason[:140] + "..."
        counts[reason] = counts.get(reason, 0) + 1
    ordered = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    return ordered[:_REPORT_MAX_REASONS]


def _scope_text(scope: dict[str, Any]) -> str:
    if scope["all"]:
        return f"All {scope['total']} endpoints"
    text = f"{scope['endpoints']} of {scope['total']} endpoints"
    if scope["tags"]:
        text += " - sections: " + ", ".join(scope["tags"])
    return text


def _local(moment):
    return timezone.localtime(moment) if moment else None


def _report_endpoints(test_run: TestRun) -> list[report.ReportEndpoint]:
    def count(status: str) -> models.Count:
        return models.Count(
            "test_cases",
            filter=models.Q(test_cases__test_run=test_run, test_cases__status=status),
            distinct=True,
        )

    endpoints = (
        test_run.specification.endpoints.filter(test_cases__test_run=test_run)
        .annotate(
            cases=models.Count(
                "test_cases", filter=models.Q(test_cases__test_run=test_run), distinct=True
            ),
            passed=count(TestCaseStatus.PASSED),
            failed=count(TestCaseStatus.FAILED),
            errors=count(TestCaseStatus.ERROR),
            pending=count(TestCaseStatus.PENDING),
            skipped=count(TestCaseStatus.SKIPPED),
            findings=models.Count(
                "test_cases__findings",
                filter=models.Q(test_cases__test_run=test_run),
                distinct=True,
            ),
        )
        .distinct()
        .order_by("path", "method")
    )
    scenarios: dict[int, dict[str, int]] = {}
    for endpoint_id, test_type in (
        test_run.test_cases.order_by("test_type").values_list("endpoint_id", "test_type")
    ):
        label = report.scenario_label(test_type)
        counts = scenarios.setdefault(endpoint_id, {})
        counts[label] = counts.get(label, 0) + 1
    return [
        report.ReportEndpoint(
            label=f"{e.method} {e.path}",
            cases=e.cases,
            passed=e.passed,
            failed=e.failed,
            errors=e.errors,
            findings=e.findings,
            not_run=e.pending + e.skipped,
            scenarios=scenarios.get(e.pk, {}),
        )
        for e in endpoints
    ]


def _report_finding(
    finding: Finding, assessment: FindingAssessment | None
) -> report.ReportFinding:
    test_case = finding.test_case
    endpoint = test_case.endpoint
    evidence = finding.evidence if isinstance(finding.evidence, dict) else {}
    request = evidence.get("request") if isinstance(evidence.get("request"), dict) else {}
    response = evidence.get("response") if isinstance(evidence.get("response"), dict) else {}
    return report.ReportFinding(
        id=finding.pk,
        title=finding.title,
        severity=finding.severity,
        category=finding.category,
        description=finding.description,
        endpoint=f"{endpoint.method} {endpoint.path}",
        test_case=test_case.name,
        test_type=test_case.test_type,
        expected_status=test_case.expected_status_code,
        request_method=str(request.get("method") or ""),
        request_url=str(request.get("url") or ""),
        request_body=executor.redact_body(request.get("body")),
        response_status=response.get("status_code"),
        response_body=executor.redact_body(response.get("body")),
        response_time_ms=response.get("response_time_ms"),
        confidence=finding.confidence,
        probable_cause=finding.probable_cause,
        reproduction=[
            {**step, "body": executor.redact_body(step.get("body"))}
            for step in finding.reproduction
            if isinstance(step, dict)
        ]
        if isinstance(finding.reproduction, list)
        else [],
        status=finding.status,
        assessment=(
            report.ReportAssessment(
                verdict=assessment.verdict,
                confidence=assessment.confidence,
                suggested_severity=assessment.suggested_severity,
                reasoning=assessment.reasoning,
                likely_cause=assessment.likely_cause,
                recommendation=assessment.recommendation,
            )
            if assessment is not None
            else None
        ),
    )


def _report_analysis(analysis: Analysis) -> report.ReportAnalysis:
    counts = summarise_analysis(analysis)
    return report.ReportAnalysis(
        model=", ".join(analysis.usage.get("models") or []) or analysis.model,
        completed_at=_local(analysis.completed_at),
        summary=analysis.summary,
        groups=analysis.groups,
        verdicts={verdict: counts[verdict] for verdict in AssessmentVerdict.values},
        findings_total=analysis.findings_total,
        findings_selected=analysis.findings_selected,
    )
