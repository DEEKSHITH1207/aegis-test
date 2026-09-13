"""Business operations for the testing domain.

Views parse input and serialise output; everything between those two steps
lives here. Keeping it in plain functions means a Celery task in a later phase
can call exactly the same code as an HTTP request, with no view layer involved.

Read helpers return querysets rather than lists so the caller can still filter,
paginate or count without loading rows it will not use.
"""

from __future__ import annotations

from typing import Any

from django.db import models, transaction
from django.utils import timezone

from apps.core.exceptions import DomainError
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


# --- Findings -----------------------------------------------------------------


def list_findings(*, test_run: TestRun) -> models.QuerySet[Finding]:
    """Every finding produced by a run, across all of its cases."""
    return Finding.objects.filter(test_case__test_run=test_run).select_related("test_case")
