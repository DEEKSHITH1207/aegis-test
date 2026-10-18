"""Executing a run end to end: the service, the API, the task and the command.

The API under test is a MockTransport handler standing in for a real
Directory API with two deliberate defects, so the suite can assert that
AegisTest finds exactly the kinds of problem it exists to find.
"""

from __future__ import annotations

import json
import re
from copy import deepcopy
from io import StringIO

import httpx
import pytest
from django.core.exceptions import ValidationError
from django.core.management import call_command
from rest_framework import status

from apps.core.exceptions import DomainError
from apps.testing import executor, services
from apps.testing.models import (
    Finding,
    FindingCategory,
    RequestRecord,
    Specification,
    TestCaseStatus,
    TestRun,
    TestRunStatus,
)
from apps.testing.tasks import execute_test_run_task
from apps.testing.tests.fixtures import REALISTIC_SPECIFICATION

pytestmark = pytest.mark.django_db

BASE_URL = "http://directory.test/api"


def directory_api(request: httpx.Request) -> httpx.Response:
    """A plausible implementation of the Directory API, with two bugs.

    * POST /users validates presence of name and email, and nothing else, so
      every other invalid body is accepted — validation gaps.
    * POST /users crashes with a 500 when age is not an integer.
    """
    if request.headers.get("Authorization") != "Bearer secret":
        return httpx.Response(401, json={"error": "unauthorised"})

    path = request.url.path.removeprefix("/api")

    if path == "/users" and request.method == "GET":
        limit = request.url.params.get("limit")
        if limit is None or not limit.isdigit() or not 1 <= int(limit) <= 100:
            return httpx.Response(400, json={"error": "limit"})
        return httpx.Response(200, json=[])

    if path == "/users" and request.method == "POST":
        body = json.loads(request.content) if request.content else None
        if not isinstance(body, dict) or not body.get("name") or not body.get("email"):
            return httpx.Response(400, json={"error": "name and email are required"})
        if "age" in body and not isinstance(body["age"], int):
            return httpx.Response(500, text="Traceback: int() argument must be ...")
        return httpx.Response(201, json={"id": 1, **body})

    match = re.fullmatch(r"/users/([^/]+)", path)
    if match:
        if not match.group(1).isdigit() or int(match.group(1)) < 1:
            return httpx.Response(404)
        return httpx.Response(204 if request.method == "DELETE" else 200, json=None)

    return httpx.Response(404)


def _client(handler=directory_api) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)


@pytest.fixture
def directory(db) -> Specification:
    specification = Specification.objects.create(
        name="Directory API", version="1.0.0", raw_specification=deepcopy(REALISTIC_SPECIFICATION)
    )
    services.import_endpoints(specification=specification)
    return specification


@pytest.fixture
def run(directory: Specification) -> TestRun:
    test_run = services.create_test_run(
        specification=directory,
        name="Execution run",
        base_url=BASE_URL,
        headers={"Authorization": "Bearer secret"},
    )
    services.generate_test_cases_for_run(test_run=test_run)
    return test_run


@pytest.fixture
def mock_target(monkeypatch):
    """Route every client the application builds to the fake Directory API."""
    monkeypatch.setattr(executor, "make_client", lambda **kwargs: _client())


def _execute(run: TestRun, handler=directory_api) -> services.ExecutionSummary:
    services.start_execution(test_run=run, enqueue=False)
    run.refresh_from_db()
    return services.execute_test_run(test_run=run, client=_client(handler))


# --- Creating a run with a target ---------------------------------------------


def test_a_run_rejects_an_unusable_base_url(directory: Specification) -> None:
    with pytest.raises(ValidationError) as excinfo:
        services.create_test_run(specification=directory, base_url="ftp://x.test")

    assert "base_url" in excinfo.value.message_dict


def test_a_run_rejects_headers_that_are_not_strings(directory: Specification) -> None:
    with pytest.raises(ValidationError) as excinfo:
        services.create_test_run(specification=directory, headers={"X-Retry": 3})

    assert "headers" in excinfo.value.message_dict


def test_the_allow_list_applies_when_a_run_is_created(directory, settings) -> None:
    settings.EXECUTOR_ALLOWED_HOSTS = ["localhost"]

    with pytest.raises(ValidationError):
        services.create_test_run(specification=directory, base_url="http://169.254.169.254")


# --- Executing ----------------------------------------------------------------


def test_every_case_is_sent_and_judged(run: TestRun) -> None:
    summary = _execute(run)

    run.refresh_from_db()
    assert run.status == TestRunStatus.COMPLETED
    assert run.started_at and run.completed_at
    total = run.test_cases.count()
    assert summary.executed == total
    assert summary.passed + summary.failed + summary.errors + summary.skipped == total
    assert not run.test_cases.filter(status=TestCaseStatus.PENDING).exists()
    # The one skipped case is a DELETE to id 0, which could name a real record.
    skipped = run.test_cases.filter(status=TestCaseStatus.SKIPPED)
    assert [c.name for c in skipped] == ["below_minimum:path.userId"]
    assert summary.skipped == 1
    # Every other case sent at least one request; a sequence sends more.
    sent = RequestRecord.objects.filter(test_case__test_run=run)
    assert sent.values("test_case").distinct().count() == total - 1


def test_the_rules_an_api_enforces_pass(run: TestRun) -> None:
    _execute(run)

    # The fake validates limit and nothing else on GET /users.
    limit = run.test_cases.filter(
        endpoint__path="/users", endpoint__method="GET", name__endswith="query.limit"
    )
    assert limit.count() > 1
    assert set(limit.values_list("status", flat=True)) == {TestCaseStatus.PASSED}
    assert run.test_cases.get(
        endpoint__path="/users", endpoint__method="GET", name="happy_path"
    ).status == TestCaseStatus.PASSED


def test_the_rules_an_api_ignores_are_gaps(run: TestRun) -> None:
    _execute(run)

    case = run.test_cases.get(
        endpoint__path="/users", endpoint__method="GET", name="invalid_enum:query.status"
    )
    assert case.status == TestCaseStatus.FAILED
    assert case.findings.get().category == FindingCategory.VALIDATION_GAP


def test_a_crash_is_reported_as_a_crash(run: TestRun) -> None:
    _execute(run)

    case = run.test_cases.get(name="invalid_type:age")
    assert case.status == TestCaseStatus.FAILED
    finding = case.findings.get()
    assert finding.category == FindingCategory.CRASH
    assert len(finding.reproduction) == 1
    assert finding.reproduction[0]["status_code"] == 500
    assert finding.response_record.status_code == 500
    assert finding.evidence["response"]["body"].startswith("Traceback")
    assert not isinstance(finding.evidence["request"]["body"]["age"], int)


def test_accepted_invalid_input_is_reported_as_a_validation_gap(run: TestRun) -> None:
    _execute(run)

    case = run.test_cases.get(name="below_minimum:age")
    assert case.status == TestCaseStatus.FAILED
    assert case.status_detail == "Invalid input was accepted with 201; expected 400."
    assert case.findings.get().category == FindingCategory.VALIDATION_GAP


def test_enforced_rules_pass(run: TestRun) -> None:
    _execute(run)

    assert run.test_cases.get(name="missing_required:email").status == TestCaseStatus.PASSED
    assert run.test_cases.get(name="missing_required:query.limit").status == (
        TestCaseStatus.PASSED
    )


def test_credentials_never_reach_the_evidence(run: TestRun) -> None:
    _execute(run)

    for record in RequestRecord.objects.filter(test_case__test_run=run):
        assert record.headers["Authorization"] == executor.REDACTED
    for finding in Finding.objects.filter(test_case__test_run=run):
        assert "Bearer secret" not in json.dumps(finding.evidence)


def test_missing_credentials_leave_cases_unjudged(run: TestRun) -> None:
    run.headers = {}
    run.save()

    _execute(run)

    sent = run.test_cases.exclude(status=TestCaseStatus.SKIPPED)
    assert set(sent.values_list("status", flat=True)) == {TestCaseStatus.ERROR}
    assert not Finding.objects.filter(test_case__test_run=run).exists()


def test_a_timeout_is_an_error_and_the_run_continues(run: TestRun) -> None:
    def slow_deletes(request: httpx.Request) -> httpx.Response:
        if request.method == "DELETE":
            raise httpx.ReadTimeout("slow", request=request)
        return directory_api(request)

    _execute(run, slow_deletes)

    run.refresh_from_db()
    assert run.status == TestRunStatus.COMPLETED
    deletes = run.test_cases.filter(endpoint__method="DELETE").exclude(
        status=TestCaseStatus.SKIPPED
    )
    assert set(deletes.values_list("status", flat=True)) == {TestCaseStatus.ERROR}
    # The happy path creates a user first; it is the DELETE itself that timed out.
    record = RequestRecord.objects.filter(test_case__in=deletes, method="DELETE").first()
    assert "timeout" in record.error
    assert not hasattr(record, "response") or record.response is None


def test_an_unreachable_target_stops_the_run_early(run: TestRun) -> None:
    def refused(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Connection refused", request=request)

    summary = _execute(run, refused)

    run.refresh_from_db()
    assert run.status == TestRunStatus.FAILED
    assert "could not connect" in run.status_detail
    assert summary.errors == 3
    assert run.test_cases.filter(status=TestCaseStatus.ERROR).count() == 3
    assert run.test_cases.filter(status=TestCaseStatus.SKIPPED).count() == (
        run.test_cases.count() - 3
    )


# --- Starting a run -----------------------------------------------------------


def test_a_run_without_a_base_url_cannot_start(directory: Specification) -> None:
    test_run = services.create_test_run(specification=directory)
    services.generate_test_cases_for_run(test_run=test_run)

    with pytest.raises(ValidationError):
        services.start_execution(test_run=test_run)


def test_a_run_without_cases_cannot_start(directory: Specification) -> None:
    test_run = services.create_test_run(specification=directory, base_url=BASE_URL)

    with pytest.raises(DomainError, match="generate"):
        services.start_execution(test_run=test_run)


def test_a_run_cannot_be_executed_twice(run: TestRun) -> None:
    _execute(run)

    with pytest.raises(DomainError, match="pending"):
        services.start_execution(test_run=run)


def test_a_run_that_cannot_be_queued_fails_instead_of_hanging(run, monkeypatch) -> None:
    def broker_down(*args, **kwargs):
        raise ConnectionError("Redis is not running")

    monkeypatch.setattr(execute_test_run_task, "delay", broker_down)

    services._enqueue_execution(run.pk)

    run.refresh_from_db()
    assert run.status == TestRunStatus.FAILED
    assert "Redis is not running" in run.status_detail


# --- The API ------------------------------------------------------------------


def test_headers_are_accepted_but_never_returned(api_client, directory) -> None:
    response = api_client.post(
        "/api/v1/test-runs/",
        {
            "specification": directory.pk,
            "base_url": "http://localhost:3000",
            "headers": {"Authorization": "Bearer secret"},
        },
        format="json",
    )

    assert response.status_code == status.HTTP_201_CREATED
    body = response.json()
    assert "headers" not in body
    assert body["header_names"] == ["Authorization"]
    assert body["base_url"] == "http://localhost:3000"
    assert body["results"]["total"] == 0


def test_an_unusable_base_url_is_a_400(api_client, directory) -> None:
    response = api_client.post(
        "/api/v1/test-runs/",
        {"specification": directory.pk, "base_url": "not a url"},
        format="json",
    )

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert "base_url" in response.json()


def test_execute_queues_the_run(
    api_client, run: TestRun, monkeypatch, django_capture_on_commit_callbacks
) -> None:
    queued: list[int] = []
    monkeypatch.setattr(execute_test_run_task, "delay", queued.append)

    with django_capture_on_commit_callbacks(execute=True):
        response = api_client.post(f"/api/v1/test-runs/{run.pk}/execute/")

    assert response.status_code == status.HTTP_202_ACCEPTED
    assert response.json()["status"] == TestRunStatus.RUNNING
    assert queued == [run.pk]


def test_execute_without_a_base_url_is_a_400(api_client, directory) -> None:
    test_run = services.create_test_run(specification=directory)
    services.generate_test_cases_for_run(test_run=test_run)

    response = api_client.post(f"/api/v1/test-runs/{test_run.pk}/execute/")

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert "base_url" in response.json()


def test_a_finished_run_reports_its_results(api_client, run: TestRun) -> None:
    _execute(run)

    body = api_client.get(f"/api/v1/test-runs/{run.pk}/").json()

    results = body["results"]
    assert body["status"] == TestRunStatus.COMPLETED
    assert results["total"] == run.test_cases.count()
    assert results["failed"] > 0 and results["passed"] > 0
    assert results["pending"] == 0
    assert results["findings"] == Finding.objects.filter(test_case__test_run=run).count()


def test_test_cases_can_be_filtered_by_status(api_client, run: TestRun) -> None:
    _execute(run)

    response = api_client.get(f"/api/v1/test-runs/{run.pk}/test-cases/?status=failed")

    assert response.status_code == status.HTTP_200_OK
    results = response.json()["results"]
    assert results
    assert {case["status"] for case in results} == {"failed"}
    assert all(case["status_detail"] for case in results)


def test_an_unknown_status_filter_is_a_400(api_client, run: TestRun) -> None:
    response = api_client.get(f"/api/v1/test-runs/{run.pk}/test-cases/?status=broken")

    assert response.status_code == status.HTTP_400_BAD_REQUEST


# --- The task and the command -------------------------------------------------


def test_the_task_executes_a_started_run(run: TestRun, mock_target) -> None:
    services.start_execution(test_run=run, enqueue=False)

    execute_test_run_task(run.pk)

    run.refresh_from_db()
    assert run.status == TestRunStatus.COMPLETED


def test_the_task_ignores_a_deleted_run() -> None:
    execute_test_run_task(987654)


def test_the_command_executes_a_pending_run(run: TestRun, mock_target) -> None:
    out = StringIO()

    call_command("execute_test_run", str(run.pk), stdout=out)

    run.refresh_from_db()
    assert run.status == TestRunStatus.COMPLETED
    assert f"Run {run.pk} completed" in out.getvalue()
