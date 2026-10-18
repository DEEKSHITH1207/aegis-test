"""A whole run against a NestJS-like backend with two planted bugs.

What it must show (see status_report_api.py for the API):

* Bug A is reported as a crash on a *duplicate create*, with both requests
  as the steps to reproduce — not as "500 on happy path".
* Bug B is reported as a *suspicious acceptance*: endYear before startYear.
* The existing financial year's 409 is not reported against the happy path.
* Nothing else is reported above low severity, and correct behaviour is never
  called a crash or a validation gap.
* GET and DELETE of one record use a record the run created, never ``id: 1``,
  which is somebody's data.
"""

from __future__ import annotations

import json
from copy import deepcopy

import pytest
from django.urls import reverse

import httpx
from django.db import models

from apps.testing import services
from apps.testing.models import (
    Finding,
    FindingCategory,
    FindingConfidence,
    FindingSeverity,
    ResourceStatus,
    Specification,
    TestCaseStatus,
    TestCaseType,
    TestRun,
)
from apps.testing.tests.status_report_api import (
    BASE_URL,
    STATUS_REPORT_SPECIFICATION,
    StatusReportApi,
    _bad_request,
)

pytestmark = pytest.mark.django_db

UNITS = "/status-report/units"
YEARS = "/status-report/financial-years"


@pytest.fixture
def specification(db) -> Specification:
    specification = Specification.objects.create(
        name="Status report",
        version="1.0.0",
        raw_specification=deepcopy(STATUS_REPORT_SPECIFICATION),
    )
    services.import_endpoints(specification=specification)
    return specification


@pytest.fixture
def api() -> StatusReportApi:
    return StatusReportApi()


def _run(specification: Specification, **fields) -> TestRun:
    test_run = services.create_test_run(
        specification=specification, name="Status report run", base_url=BASE_URL, **fields
    )
    services.generate_test_cases_for_run(test_run=test_run)
    return test_run


def _execute(test_run: TestRun, api: StatusReportApi) -> TestRun:
    services.start_execution(test_run=test_run, enqueue=False)
    test_run.refresh_from_db()
    services.execute_test_run(test_run=test_run, client=api.client())
    test_run.refresh_from_db()
    return test_run


def _case(test_run: TestRun, path: str, name: str, method: str = "POST"):
    return test_run.test_cases.get(endpoint__path=path, endpoint__method=method, name=name)


def _findings(test_run: TestRun):
    return Finding.objects.filter(test_case__test_run=test_run).select_related(
        "test_case__endpoint"
    )


def _describe(findings) -> str:
    """Findings as a reader of a failed assertion needs to see them."""
    lines = [
        f"  - [{f.category}/{f.severity}/{f.confidence}] "
        f"{f.test_case.endpoint.method} {f.test_case.endpoint.path} ({f.test_case.name}): {f.title}"
        for f in findings
    ]
    return "\n".join(lines) or "  (no findings)"


def _setup_and_request(case, method: str):
    """The successful setup create of an item case, and the request it set up."""
    records = list(case.request_records.order_by("id"))
    index = next(i for i, r in enumerate(records) if r.method == method)
    created = records[index - 1]
    assert created.method == "POST" and created.response.status_code == 201, (
        [(r.method, r.response.status_code if hasattr(r, "response") else None) for r in records]
    )
    return created, records[index]


def _cases_text(cases) -> str:
    return "\n".join(
        f"  - {c.endpoint.method} {c.endpoint.path} {c.name}: {c.status_detail}" for c in cases
    ) or "  (none)"


def _expect_finding(
    test_run: TestRun,
    *,
    path: str,
    name: str,
    title: str,
    category: str,
    severity: str,
    confidence: str,
    method: str = "POST",
) -> Finding:
    """The finding a planted bug must produce, classified as the evaluator
    promises — or a failure that says what the run reported instead."""
    case = _case(test_run, path, name, method)
    matches = [f for f in case.findings.all() if title in f.title]
    assert matches, (
        f"Expected a finding titled '...{title}...' on {method} {path} / {name}, which is "
        f"'{case.status}' ({case.status_detail}). Its findings:\n{_describe(case.findings.all())}"
    )
    assert len(matches) == 1, f"More than one finding matches '{title}':\n{_describe(matches)}"
    (finding,) = matches
    actual = (finding.category, finding.severity, finding.confidence)
    expected = (category, severity, confidence)
    assert actual == expected, (
        f"'{finding.title}' is classified (category, severity, confidence) = {actual}; "
        f"expected {expected}."
    )
    return finding


BUG_A = dict(
    path=UNITS,
    name=TestCaseType.DUPLICATE_CREATE,
    title="duplicate create returns 500 (unhandled unique constraint)",
    category=FindingCategory.CRASH,
    severity=FindingSeverity.HIGH,
    confidence=FindingConfidence.HIGH,
)
END_BEFORE_START = "cross_field:startYear/endYear:end_before_start"
BUG_B = dict(
    path=YEARS,
    name=END_BEFORE_START,
    title="suspicious acceptance - endYear",
    category=FindingCategory.VALIDATION_GAP,
    severity=FindingSeverity.MEDIUM,
    confidence=FindingConfidence.HIGH,
)


# --- Bug A: duplicate create ----------------------------------------------------


def test_bug_a_is_reported_as_a_duplicate_create_crash(
    specification: Specification, api: StatusReportApi
) -> None:
    test_run = _execute(_run(specification), api)

    case = _case(test_run, UNITS, TestCaseType.DUPLICATE_CREATE)
    assert case.status == TestCaseStatus.FAILED, case.status_detail
    finding = _expect_finding(test_run, **BUG_A)
    assert case.findings.count() == 1, _describe(case.findings.all())
    assert "unique" in finding.probable_cause
    # The setup create is part of the reproduction.
    first, second = finding.reproduction
    assert (first["method"], first["status_code"]) == ("POST", 201)
    assert (second["method"], second["status_code"]) == ("POST", 500)
    assert first["body"] == second["body"]
    assert first["note"].startswith("Setup")

def test_the_unit_happy_path_uses_a_fresh_name_and_passes(
    specification: Specification, api: StatusReportApi
) -> None:
    test_run = _execute(_run(specification), api)

    case = _case(test_run, UNITS, "happy_path")
    sent = case.request_records.get().body
    assert sent["name"] != "MT"
    assert sent["name"].startswith("MT-")
    assert case.status == TestCaseStatus.PASSED
    assert not case.findings.exists()


def test_a_500_for_existing_values_is_explained_as_a_collision(
    specification: Specification, api: StatusReportApi
) -> None:
    # As the tool used to: the document's example, which already exists.
    test_run = _run(specification)
    case = _case(test_run, UNITS, "happy_path")
    case.input_data = {**case.input_data, "body": {"name": "MT"}}
    case.save()

    _execute(test_run, api)

    case.refresh_from_db()
    assert case.request_records.count() == 2
    finding = case.findings.get()
    assert "500 when the values already exist (unhandled unique constraint)" in finding.title
    assert finding.category == FindingCategory.CRASH
    assert finding.reproduction[0]["body"] == {"name": "MT"}


# --- Bug B: cross-field ----------------------------------------------------------


def test_bug_b_is_reported_as_suspicious_acceptance(
    specification: Specification, api: StatusReportApi
) -> None:
    test_run = _execute(_run(specification), api)

    case = _case(test_run, YEARS, END_BEFORE_START)
    sent = case.request_records.order_by("id").last().body
    assert sent["endYear"] < sent["startYear"]
    assert case.status == TestCaseStatus.FAILED, case.status_detail

    finding = _expect_finding(test_run, **BUG_B)
    assert (
        f"endYear ({sent['endYear']}) is before startYear ({sent['startYear']})" in finding.title
    )
    assert "the record was created" in finding.description
    assert finding.probable_cause

def test_bug_b_also_flags_the_derived_name_with_low_confidence(
    specification: Specification, api: StatusReportApi
) -> None:
    test_run = _execute(_run(specification), api)

    case = _case(test_run, YEARS, END_BEFORE_START)
    derived = case.findings.get(category=FindingCategory.STATE_BUG)
    assert "derived field 'name'" in derived.title
    assert "runs backwards" in derived.description
    assert derived.confidence == FindingConfidence.LOW


def test_a_rule_the_api_still_enforces_passes(
    specification: Specification, api: StatusReportApi
) -> None:
    test_run = _execute(_run(specification), api)

    # The loosened check still refuses endYear == startYear.
    case = _case(test_run, YEARS, "cross_field:startYear/endYear:end_equals_start")
    assert case.status == TestCaseStatus.PASSED
    assert not case.findings.exists()


# --- The existing financial year ---------------------------------------------------


def test_the_existing_year_409_is_not_an_inconsistency(
    specification: Specification, api: StatusReportApi
) -> None:
    test_run = _execute(_run(specification), api)

    happy = _case(test_run, YEARS, "happy_path")
    sent = happy.request_records.get().body
    assert (sent["startYear"], sent["endYear"]) != (2025, 2026)
    assert sent["endYear"] == sent["startYear"] + 1  # The width is kept.
    assert happy.status == TestCaseStatus.PASSED

    year_findings = Finding.objects.filter(
        test_case__test_run=test_run, test_case__endpoint__path=YEARS
    )
    assert not year_findings.filter(category=FindingCategory.INCONSISTENCY).exists()
    assert not year_findings.filter(title__contains="where 201 is documented").exists()


def test_a_colliding_happy_path_is_retried_and_noted_not_failed(
    specification: Specification, api: StatusReportApi
) -> None:
    test_run = _run(specification)
    case = _case(test_run, YEARS, "happy_path")
    case.input_data = {**case.input_data, "body": {"startYear": 2025, "endYear": 2026}}
    case.save()

    _execute(test_run, api)

    case.refresh_from_db()
    first, second = case.request_records.order_by("id")
    assert first.response.status_code == 409
    assert second.response.status_code == 201
    assert second.body["startYear"] != 2025
    assert case.status == TestCaseStatus.PASSED
    assert "collided with existing data (409)" in case.status_detail

    # Only a documentation note: the 409 is right, the document omits it.
    note = case.findings.get()
    assert note.category == FindingCategory.SPEC_GAP
    assert note.severity == FindingSeverity.LOW
    assert "409 for existing data is not documented" in note.title


def test_the_undocumented_409_is_noted_once_per_operation(
    specification: Specification, api: StatusReportApi
) -> None:
    test_run = _execute(_run(specification), api)

    notes = Finding.objects.filter(
        test_case__test_run=test_run,
        test_case__endpoint__path=YEARS,
        title__contains="not documented",
    )
    # The duplicate create's second request returns it.
    assert notes.count() == 1
    # Units document their 409, so nothing to note there.
    assert not Finding.objects.filter(
        test_case__test_run=test_run,
        test_case__endpoint__path=UNITS,
        title__contains="not documented",
    ).exists()


# --- Read-back ------------------------------------------------------------------------


def test_a_created_unit_reads_back_with_its_values(
    specification: Specification, api: StatusReportApi
) -> None:
    test_run = _execute(_run(specification), api)

    case = _case(test_run, UNITS, TestCaseType.CREATE_READ_BACK)
    assert case.status == TestCaseStatus.PASSED, case.status_detail
    create, read = case.request_records.order_by("id")
    assert read.method == "GET"
    assert read.url.endswith(f"/status-report/units/{create.response.body['id']}")


class _ForgetfulApi(StatusReportApi):
    """Stores units with their name upper-cased and padded."""

    def _create_unit(self, body):
        response = super()._create_unit(body)
        if response.status_code == 201:
            unit = self.units[response.json()["id"]]
            unit["name"] = unit["name"][:4]
        return response


def test_a_read_back_that_differs_is_a_state_bug(specification: Specification) -> None:
    test_run = _execute(_run(specification), _ForgetfulApi())

    case = _case(test_run, UNITS, TestCaseType.CREATE_READ_BACK)
    assert case.status == TestCaseStatus.FAILED
    finding = case.findings.get()
    assert finding.category == FindingCategory.STATE_BUG
    assert "reads back with different values" in finding.title
    assert [step["method"] for step in finding.reproduction] == ["POST", "GET"]


# --- Finding quality -------------------------------------------------------------------


def test_every_finding_states_category_confidence_cause_and_steps(
    specification: Specification, api: StatusReportApi
) -> None:
    test_run = _execute(_run(specification), api)

    findings = Finding.objects.filter(test_case__test_run=test_run)
    assert findings.exists()
    for finding in findings:
        assert finding.category in {
            FindingCategory.CRASH,
            FindingCategory.VALIDATION_GAP,
            FindingCategory.STATE_BUG,
            FindingCategory.SPEC_GAP,
        }, finding.title
        assert finding.confidence in FindingConfidence.values, finding.title
        assert finding.probable_cause, finding.title
        assert finding.reproduction, finding.title


# --- Cleanup ------------------------------------------------------------------------------


def test_everything_created_is_tracked_and_left_without_cleanup(
    specification: Specification, api: StatusReportApi
) -> None:
    test_run = _execute(_run(specification), api)

    resources = test_run.created_resources.all()
    assert resources.exists()
    # Only what the run's own DELETE happy paths removed is not left behind.
    deleted = resources.filter(status=ResourceStatus.DELETED)
    assert set(resources.values_list("status", flat=True)) == {
        ResourceStatus.LEFT,
        ResourceStatus.DELETED,
    }
    assert deleted.count() == 2
    assert all("run's own 'happy_path' case" in r.detail for r in deleted)
    created_units = {str(k) for k, v in api.units.items() if v["name"] != "MT"}
    assert created_units <= set(
        resources.filter(endpoint__path=UNITS).values_list("resource_id", flat=True)
    )
    assert all(r.delete_url.startswith(BASE_URL) for r in resources)

    report = services.test_run_report(test_run=test_run)
    assert len(report.resources) == resources.count()

def test_a_run_with_cleanup_deletes_what_it_created(
    specification: Specification, api: StatusReportApi
) -> None:
    test_run = _execute(_run(specification, cleanup=True), api)

    statuses = set(test_run.created_resources.values_list("status", flat=True))
    assert statuses == {ResourceStatus.DELETED}
    # Exactly what was there before the run remains.
    assert [unit["name"] for unit in api.units.values()] == ["MT"]
    assert [year["startYear"] for year in api.years.values()] == [2025]

def test_cleanup_can_be_asked_for_after_the_run(
    specification: Specification, api: StatusReportApi, api_client, monkeypatch
) -> None:
    from apps.testing import executor

    test_run = _execute(_run(specification), api)
    monkeypatch.setattr(executor, "make_client", lambda **kwargs: api.client())

    response = api_client.post(reverse("v1:test-run-cleanup", args=[test_run.pk]))

    assert response.status_code == 200
    assert {item["status"] for item in response.json()} == {ResourceStatus.DELETED}
    listed = api_client.get(reverse("v1:test-run-resource-list", args=[test_run.pk])).json()
    assert listed["count"] == len(response.json())
    run = api_client.get(reverse("v1:test-run-detail", args=[test_run.pk])).json()
    assert run["resources"]["left"] == 0


# --- Only the planted bugs -----------------------------------------------------------


def test_the_planted_bugs_are_the_only_findings_above_low_severity(
    specification: Specification, api: StatusReportApi
) -> None:
    test_run = _execute(_run(specification), api)

    _expect_finding(test_run, **BUG_A)
    _expect_finding(test_run, **BUG_B)
    serious = [
        f
        for f in _findings(test_run)
        if f.severity in {FindingSeverity.CRITICAL, FindingSeverity.HIGH, FindingSeverity.MEDIUM}
    ]
    identities = {
        (f.test_case.endpoint.method, f.test_case.endpoint.path, f.test_case.name, f.category)
        for f in serious
    }
    assert identities == {
        ("POST", UNITS, TestCaseType.DUPLICATE_CREATE, FindingCategory.CRASH),
        ("POST", YEARS, END_BEFORE_START, FindingCategory.VALIDATION_GAP),
    }, "Findings above low severity:\n" + _describe(serious)


def test_correct_behaviour_is_never_called_a_crash_or_a_validation_gap(
    specification: Specification, api: StatusReportApi
) -> None:
    """The known noise — a wide range, a derived name, documentation notes —
    stays low, and no finding claims a defect in behaviour the fixture
    implements correctly."""
    test_run = _execute(_run(specification), api)

    planted = {(UNITS, TestCaseType.DUPLICATE_CREATE), (YEARS, END_BEFORE_START)}
    for finding in _findings(test_run):
        if (finding.test_case.endpoint.path, finding.test_case.name) in planted:
            continue
        assert finding.severity == FindingSeverity.LOW, _describe([finding])
        assert finding.category in {
            FindingCategory.SPEC_GAP,
            FindingCategory.STATE_BUG,
            FindingCategory.VALIDATION_GAP,
        }, _describe([finding])
        if finding.category == FindingCategory.VALIDATION_GAP:
            # The wide-range heuristic is the only low validation gap allowed.
            assert "far wider range" in finding.title, _describe([finding])
            assert finding.confidence == FindingConfidence.LOW
    assert not _findings(test_run).filter(
        category__in=[
            FindingCategory.CRASH,
            FindingCategory.FALSE_POSITIVE_RISK,
            FindingCategory.INCONSISTENCY,
            FindingCategory.BUG,
        ]
    ).exclude(test_case__name=TestCaseType.DUPLICATE_CREATE).exists(), _describe(
        _findings(test_run)
    )


def test_no_case_fails_or_errors_for_a_reason_that_is_not_a_planted_bug(
    specification: Specification, api: StatusReportApi
) -> None:
    test_run = _execute(_run(specification), api)

    failed = test_run.test_cases.filter(status=TestCaseStatus.FAILED).select_related("endpoint")
    assert {(c.endpoint.path, c.name) for c in failed} <= {
        (UNITS, TestCaseType.DUPLICATE_CREATE),
        (YEARS, END_BEFORE_START),
        (YEARS, "cross_field:startYear/endYear:end_far_after_start"),  # known low noise
    }, _cases_text(failed)
    errors = test_run.test_cases.filter(status=TestCaseStatus.ERROR).select_related("endpoint")
    assert not errors.exists(), _cases_text(errors)


# --- Real resources for GET and DELETE ------------------------------------------------


def test_happy_paths_on_item_routes_use_a_resource_the_run_created(
    specification: Specification, api: StatusReportApi
) -> None:
    test_run = _execute(_run(specification), api)

    for method in ("GET", "DELETE"):
        for path in (f"{UNITS}/{{id}}", f"{YEARS}/{{id}}"):
            case = _case(test_run, path, "happy_path", method)
            assert case.status == TestCaseStatus.PASSED, f"{method} {path}: {case.status_detail}"
            created, request = _setup_and_request(case, method)
            created_id = str(created.response.body["id"])
            assert created_id != "1"
            assert request.url.endswith(f"/{created_id}")
            assert request.path_params["id"] == created_id
            assert request.response.status_code == 200


def test_the_records_that_existed_before_the_run_survive_it(
    specification: Specification, api: StatusReportApi
) -> None:
    _execute(_run(specification), api)

    assert api.units[1] == {"id": 1, "name": "MT"}
    assert api.years[1]["name"] == "2025-26"
    assert "units/1" not in api.deleted
    assert "financial-years/1" not in api.deleted


def test_a_delete_happy_path_checks_the_resource_is_gone_and_marks_it_deleted(
    specification: Specification, api: StatusReportApi
) -> None:
    test_run = _execute(_run(specification), api)

    case = _case(test_run, f"{UNITS}/{{id}}", "happy_path", "DELETE")
    created, delete = _setup_and_request(case, "DELETE")
    read_after = case.request_records.order_by("id").last()
    assert (delete.response.status_code, read_after.method, read_after.response.status_code) == (
        200, "GET", 404,
    )
    assert "it is gone" in case.status_detail
    resource = test_run.created_resources.get(test_case=case)
    assert resource.status == ResourceStatus.DELETED
    assert resource.resource_id == str(created.response.body["id"])


class _StickyApi(StatusReportApi):
    """DELETE says 200 and removes nothing."""

    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.method == "DELETE":
            return httpx.Response(200, json={"deleted": True})
        return super().handle(request)


def test_a_delete_that_leaves_the_resource_readable_is_a_state_bug(
    specification: Specification,
) -> None:
    test_run = _execute(_run(specification), _StickyApi())

    case = _case(test_run, f"{UNITS}/{{id}}", "happy_path", "DELETE")
    assert case.status == TestCaseStatus.FAILED, case.status_detail
    finding = _expect_finding(
        test_run,
        path=f"{UNITS}/{{id}}",
        method="DELETE",
        name="happy_path",
        title="deleted resource is still readable",
        category=FindingCategory.STATE_BUG,
        severity=FindingSeverity.MEDIUM,
        confidence=FindingConfidence.HIGH,
    )
    assert [step["method"] for step in finding.reproduction] == ["POST", "DELETE", "GET"]


class _BrokenCreateApi(StatusReportApi):
    """Every create fails."""

    def _create_unit(self, body):
        return httpx.Response(500, json={"statusCode": 500, "message": "Internal server error"})


def test_a_failed_setup_create_leaves_the_item_case_unjudged_not_passed(
    specification: Specification,
) -> None:
    test_run = _execute(_run(specification), _BrokenCreateApi())

    for method in ("GET", "DELETE"):
        case = _case(test_run, f"{UNITS}/{{id}}", "happy_path", method)
        assert case.status == TestCaseStatus.ERROR, case.status_detail
        assert "setup create (POST /status-report/units) answered 500" in case.status_detail
        assert not case.findings.exists(), _describe(case.findings.all())
        # Nothing was sent to a guessed ID.
        assert [r.method for r in case.request_records.all()] == ["POST"]
    # The crash itself is reported by the create's own happy path.
    _expect_finding(
        test_run,
        path=UNITS,
        name="happy_path",
        title="500",
        category=FindingCategory.CRASH,
        severity=FindingSeverity.HIGH,
        confidence=FindingConfidence.HIGH,
    )


class _AnonymousCreateApi(StatusReportApi):
    """Creates succeed but answer without an ID."""

    def _create_unit(self, body):
        response = super()._create_unit(body)
        if response.status_code != 201:
            return response
        return httpx.Response(201, json={"name": response.json()["name"]})


def test_a_create_that_returns_no_id_leaves_the_item_case_unjudged(
    specification: Specification,
) -> None:
    test_run = _execute(_run(specification), _AnonymousCreateApi())

    case = _case(test_run, f"{UNITS}/{{id}}", "happy_path", "GET")
    assert case.status == TestCaseStatus.ERROR, case.status_detail
    assert "carries no ID" in case.status_detail
    assert [r.method for r in case.request_records.all()] == ["POST"]
    assert not case.findings.exists(), _describe(case.findings.all())


def test_without_a_create_operation_a_404_for_a_sample_id_is_inconclusive(
    api: StatusReportApi,
) -> None:
    document = deepcopy(STATUS_REPORT_SPECIFICATION)
    del document["paths"][UNITS]["post"]
    specification = Specification.objects.create(
        name="Status report, read-only units", version="1.0.0", raw_specification=document
    )
    services.import_endpoints(specification=specification)
    api.units.clear()  # The sample ID 1 names nothing.

    test_run = _execute(_run(specification), api)

    case = _case(test_run, f"{UNITS}/{{id}}", "happy_path", "GET")
    assert case.status == TestCaseStatus.ERROR, case.status_detail
    assert "Inconclusive" in case.status_detail
    assert [r.method for r in case.request_records.all()] == ["GET"]
    assert not case.findings.exists(), _describe(case.findings.all())


class _UpdatableApi(StatusReportApi):
    """Adds PUT /status-report/units/{id}, which renames a unit."""

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api")
        if request.method == "PUT" and path.startswith(f"{UNITS}/"):
            key = path.rsplit("/", 1)[1]
            if not key.isdigit() or int(key) not in self.units:
                return httpx.Response(404, json={"statusCode": 404, "message": "Not Found"})
            body = json.loads(request.content) if request.content else None
            name = body.get("name") if isinstance(body, dict) else None
            if not isinstance(name, str) or not name.strip() or len(name) > 100:
                return _bad_request("name must be a string of 1 to 100 characters")
            self.units[int(key)]["name"] = name
            return httpx.Response(200, json=self.units[int(key)])
        return super().handle(request)


def test_an_update_happy_path_also_uses_a_created_resource() -> None:
    document = deepcopy(STATUS_REPORT_SPECIFICATION)
    document["paths"][f"{UNITS}/{{id}}"]["put"] = {
        "tags": ["Units"],
        "requestBody": document["paths"][UNITS]["post"]["requestBody"],
        "responses": {
            "200": document["paths"][f"{UNITS}/{{id}}"]["get"]["responses"]["200"],
            "404": {"description": "Not found"},
        },
    }
    specification = Specification.objects.create(
        name="Status report with updates", version="1.0.0", raw_specification=document
    )
    services.import_endpoints(specification=specification)
    api = _UpdatableApi()

    test_run = _execute(_run(specification), api)

    case = _case(test_run, f"{UNITS}/{{id}}", "happy_path", "PUT")
    assert case.status == TestCaseStatus.PASSED, case.status_detail
    create, update = _setup_and_request(case, "PUT")
    assert update.path_params["id"] == str(create.response.body["id"])
    assert update.response.body["name"] == update.body["name"]
    assert update.path_params["id"] != "1"


# --- Collisions on semantic cases ---------------------------------------------------


def test_a_semantic_case_that_collides_is_retried_and_still_judged(
    specification: Specification, api: StatusReportApi
) -> None:
    """The cause of an intermittent failure: a cross-field case whose start
    year happened to exist already was answered 409 and left unjudged, so
    Bug B went unreported. It is now sent again with the relation intact."""
    test_run = _run(specification)
    case = _case(test_run, YEARS, END_BEFORE_START)
    case.input_data = {**case.input_data, "body": {"startYear": 2025, "endYear": 2015}}
    case.save()

    _execute(test_run, api)

    case.refresh_from_db()
    records = list(case.request_records.order_by("id"))
    assert records[0].response.status_code == 409
    # Fresh values can collide again (the span is a hundred years); the retry
    # goes on, bounded, until they do not.
    assert 2 <= len(records) <= 4
    final = records[-1]
    assert final.response.status_code == 201, [r.response.status_code for r in records]
    assert final.body["startYear"] != 2025
    assert final.body["endYear"] < final.body["startYear"]
    assert len({r.body["startYear"] for r in records}) == len(records)
    assert case.status == TestCaseStatus.FAILED, case.status_detail
    assert "collided with existing data (409)" in case.status_detail
    _expect_finding(test_run, **BUG_B)


# --- Undocumented rejection codes --------------------------------------------------


def test_an_invalid_path_value_refused_with_an_undocumented_code_passes_with_one_note(
    specification: Specification, api: StatusReportApi
) -> None:
    test_run = _execute(_run(specification), api)

    for method in ("GET", "DELETE"):
        case = _case(test_run, f"{UNITS}/{{id}}", "invalid_type:path.id", method)
        assert case.status == TestCaseStatus.PASSED, case.status_detail
        note = case.findings.get()
        assert note.title == (
            f"{method} {UNITS}/{{id}}: rejects invalid input with 400, not the documented 404"
        )
        assert note.category == FindingCategory.SPEC_GAP
        assert note.severity == FindingSeverity.LOW
        assert note.confidence == FindingConfidence.MEDIUM


# --- The PDF report of this run ---------------------------------------------------


def _pdf_text(pdf: bytes) -> str:
    import io

    from pypdf import PdfReader

    return "\n".join(page.extract_text() for page in PdfReader(io.BytesIO(pdf)).pages)


def test_the_report_tells_the_planted_bugs_from_documentation_gaps(
    specification: Specification, api: StatusReportApi
) -> None:
    test_run = _execute(_run(specification, cleanup=True), api)

    data = services.test_run_report(test_run=test_run)
    groups = data.findings_by_group()
    text = _pdf_text(services.test_run_report_pdf(test_run=test_run))

    assert {f.title.split(":")[-1].strip()[:20] for f in groups["bug"]} == {
        "duplicate create ret",
        "suspicious acceptanc",
    }
    assert all(f.category == FindingCategory.SPEC_GAP for f in groups["documentation"])
    assert not groups["inconclusive"] and not groups["dismissed"]
    assert "Bugs (2)" in text
    assert text.index("Bugs (2)") < text.index("Possible issues (") < text.index("Documentation gaps (")
    # The undocumented 400 is filed as a documentation gap, after the bugs.
    assert text.index("Documentation gaps (") < text.index("rejects invalid input with 400")

    # Stateful sequences and cleanup are shown from the records.
    assert "Stateful testing and cleanup" in text
    assert "-> 500 - Send the same request again" in text or "-> 500" in text
    created = test_run.created_resources.count()
    assert f"The run created {created} resources in the target: {created} deleted" in text
    assert data.sequences_total == test_run.test_cases.annotate(
        n=models.Count("request_records")
    ).filter(n__gt=1).count()
    delete_sequence = next(
        s for s in data.sequences if s.endpoint == f"DELETE {UNITS}/{{id}}" and s.case == "happy_path"
    )
    assert [step["method"] for step in delete_sequence.steps][-3:] == ["POST", "DELETE", "GET"]
    assert delete_sequence.steps[-1]["status_code"] == 404


# --- Mutation safety -----------------------------------------------------------------


def _with_put(document: dict) -> dict:
    document = deepcopy(document)
    document["paths"][f"{UNITS}/{{id}}"]["put"] = {
        "tags": ["Units"],
        "requestBody": document["paths"][UNITS]["post"]["requestBody"],
        "responses": {
            "200": document["paths"][f"{UNITS}/{{id}}"]["get"]["responses"]["200"],
            "404": {"description": "Not found"},
        },
    }
    return document


def _specification(document: dict, name: str) -> Specification:
    specification = Specification.objects.create(
        name=name, version="1.0.0", raw_specification=document
    )
    services.import_endpoints(specification=specification)
    return specification


def _records(case) -> list:
    return list(case.request_records.order_by("id"))


def test_invalid_and_semantic_put_cases_never_touch_a_pre_existing_record() -> None:
    specification = _specification(_with_put(STATUS_REPORT_SPECIFICATION), "Updatable units")
    api = _UpdatableApi()

    test_run = _execute(_run(specification), api)

    assert api.units[1] == {"id": 1, "name": "MT"}
    puts = test_run.test_cases.filter(endpoint__method="PUT")
    assert puts.count() > 3
    for case in puts:
        sent = [r for r in _records(case) if r.method == "PUT"]
        if case.status == TestCaseStatus.SKIPPED:
            assert not sent, case.name
            continue
        assert sent, f"{case.name}: {case.status} {case.status_detail}"
        for record in sent:
            sent_id = record.path_params["id"]
            assert sent_id != "1", case.name
            # Either a record this run created, or text that cannot name one.
            assert not sent_id.isdigit() or int(sent_id) > 100, case.name
    # The cases kept what they deliberately break.
    missing = _case(test_run, f"{UNITS}/{{id}}", "missing_required:name", "PUT")
    assert [r for r in _records(missing) if r.method == "PUT"][0].body == {}
    unicode = _case(test_run, f"{UNITS}/{{id}}", "suspicious_value:name:unicode", "PUT")
    assert unicode.status == TestCaseStatus.PASSED, unicode.status_detail
    # A PUT accepted onto a disposable record is harmless, and it is tracked.
    assert all(v["name"] == "MT" for k, v in api.units.items() if k == 1)


def test_delete_cases_that_break_another_parameter_use_a_disposable_record() -> None:
    document = deepcopy(STATUS_REPORT_SPECIFICATION)
    document["paths"][f"{UNITS}/{{id}}"]["delete"]["parameters"] = [
        {"name": "force", "in": "query", "required": False, "schema": {"type": "boolean"}}
    ]
    specification = _specification(document, "Units with forced delete")
    api = StatusReportApi()  # Ignores the query parameter, so the delete goes through.

    test_run = _execute(_run(specification), api)

    assert "units/1" not in api.deleted
    assert api.units[1] == {"id": 1, "name": "MT"}
    case = _case(test_run, f"{UNITS}/{{id}}", "invalid_type:query.force", "DELETE")
    created, delete = _setup_and_request(case, "DELETE")
    assert delete.query_params == {"force": "not-a-boolean"}
    assert delete.path_params["id"] == str(created.response.body["id"])
    # Accepted invalid input is still reported, on the disposable record.
    assert case.status == TestCaseStatus.FAILED
    assert case.findings.get().category == FindingCategory.VALIDATION_GAP


def test_a_mutating_case_that_breaks_the_id_with_a_number_is_skipped_not_sent() -> None:
    document = _with_put(STATUS_REPORT_SPECIFICATION)
    document["paths"][f"{UNITS}/{{id}}"]["parameters"][0]["schema"] = {"type": "integer", "minimum": 1}
    specification = _specification(document, "Units with bounded ids")
    api = _UpdatableApi()

    test_run = _execute(_run(specification), api)

    for method in ("PUT", "DELETE"):
        numeric = _case(test_run, f"{UNITS}/{{id}}", "below_minimum:path.id", method)
        assert numeric.status == TestCaseStatus.SKIPPED
        assert "may name a real record" in numeric.status_detail
        assert not numeric.request_records.exists()
        text = _case(test_run, f"{UNITS}/{{id}}", "invalid_type:path.id", method)
        assert text.status == TestCaseStatus.PASSED, text.status_detail
        assert _records(text)[0].path_params["id"] == "not-an-integer"
    # A GET is read-only, so its boundary case is still sent and judged.
    assert _case(test_run, f"{UNITS}/{{id}}", "below_minimum:path.id", "GET").request_records.exists()
    assert api.units[1] == {"id": 1, "name": "MT"}
    assert "units/1" not in api.deleted


def test_mutating_cases_without_a_create_operation_are_skipped_with_the_reason() -> None:
    document = _with_put(STATUS_REPORT_SPECIFICATION)
    del document["paths"][UNITS]["post"]
    specification = _specification(document, "Units without create")
    api = _UpdatableApi()

    test_run = _run(specification)
    services.start_execution(test_run=test_run, enqueue=False)
    test_run.refresh_from_db()
    summary = services.execute_test_run(test_run=test_run, client=api.client())

    mutating = test_run.test_cases.filter(
        endpoint__path=f"{UNITS}/{{id}}", endpoint__method__in=["PUT", "DELETE"]
    ).exclude(name="invalid_type:path.id")
    assert mutating.exists()
    for case in mutating:
        assert case.status == TestCaseStatus.SKIPPED, f"{case.name}: {case.status}"
        assert "no POST /status-report/units" in case.status_detail
        assert not case.request_records.exists()
    assert summary.skipped == mutating.count()
    assert summary.passed + summary.failed + summary.errors + summary.skipped == summary.executed
    # Reads still happen; the seeded unit is untouched.
    assert _case(test_run, f"{UNITS}/{{id}}", "happy_path", "GET").request_records.exists()
    assert api.units[1] == {"id": 1, "name": "MT"}
    assert not [d for d in api.deleted if d.startswith("units/")]
    assert "financial-years/1" not in api.deleted  # Years still have a POST; theirs are disposable.
    report = services.test_run_report(test_run=test_run)
    assert report.not_run == mutating.count()
    assert any("no POST" in reason for reason, _ in report.not_run_reasons)


class _UpsertApi(StatusReportApi):
    """POST /units answers 200 with the unit of that name, creating it if new."""

    def _create_unit(self, body):
        if isinstance(body, dict) and isinstance(body.get("name"), str) and body["name"].strip():
            existing = next((u for u in self.units.values() if u["name"] == body["name"]), None)
            if existing is not None:
                return httpx.Response(200, json=existing)
            response = super()._create_unit(body)
            return httpx.Response(200, json=response.json()) if response.status_code == 201 else response
        return super()._create_unit(body)


def test_an_upsert_answer_never_makes_a_record_eligible_for_cleanup() -> None:
    # The document says 201, the API answers 200: ownership is uncertain.
    specification = _specification(_with_put(STATUS_REPORT_SPECIFICATION), "Upserting units")
    api = _UpsertApi()
    before = {k: dict(v) for k, v in api.units.items()}

    test_run = _execute(_run(specification, cleanup=True), api)

    assert api.units[1] == before[1]
    assert "units/1" not in api.deleted
    uncertain = test_run.created_resources.filter(endpoint__path=UNITS)
    assert uncertain.exists()
    assert all(r.delete_url == "" and r.status == ResourceStatus.LEFT for r in uncertain)
    assert all("rather than 201" in r.detail for r in uncertain)
    for method in ("PUT", "DELETE"):
        case = _case(test_run, f"{UNITS}/{{id}}", "happy_path", method)
        assert case.status == TestCaseStatus.SKIPPED, case.status_detail
        assert "answered 200 rather than 201" in case.status_detail
        assert [r.method for r in _records(case)] == ["POST"]


def test_a_search_style_post_is_not_tracked_at_all() -> None:
    document = deepcopy(STATUS_REPORT_SPECIFICATION)
    document["paths"][UNITS]["post"]["responses"] = {"200": {"description": "Found or made"}}
    specification = _specification(document, "Units found by post")
    api = _UpsertApi()

    test_run = _execute(_run(specification, cleanup=True), api)

    assert not test_run.created_resources.filter(endpoint__path=UNITS).exists()
    assert api.units[1] == {"id": 1, "name": "MT"}
    assert "units/1" not in api.deleted


class _ConflictingPutApi(_UpdatableApi):
    """PUT refuses each unit's first update as a duplicate name, then accepts."""

    def __init__(self) -> None:
        super().__init__()
        self.refused: set[str] = set()

    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.method == "PUT" and request.url.path not in self.refused:
            self.refused.add(request.url.path)
            return httpx.Response(
                409, json={"statusCode": 409, "message": "A unit with this name already exists"}
            )
        return super().handle(request)


def test_a_put_whose_values_collide_is_retried_on_the_same_record() -> None:
    specification = _specification(_with_put(STATUS_REPORT_SPECIFICATION), "Units with conflicts")
    api = _ConflictingPutApi()

    test_run = _execute(_run(specification), api)

    case = _case(test_run, f"{UNITS}/{{id}}", "happy_path", "PUT")
    records = _records(case)
    assert [r.method for r in records] == ["POST", "PUT", "PUT"]
    assert records[1].response.status_code == 409
    assert records[2].response.status_code == 200
    assert records[1].path_params["id"] == records[2].path_params["id"]
    assert records[1].body["name"] != records[2].body["name"]
    assert case.status == TestCaseStatus.PASSED, case.status_detail
    assert "collided with existing data (409)" in case.status_detail
    # An invalid case is not retried: its rejection is the answer.
    invalid = _case(test_run, f"{UNITS}/{{id}}", "invalid_type:name", "PUT")
    assert [r.method for r in _records(invalid)] == ["POST", "PUT"]
    assert api.units[1] == {"id": 1, "name": "MT"}
