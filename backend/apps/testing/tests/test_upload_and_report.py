"""Uploading a specification file, and the PDF report of a run.

The report tests read the PDF back with pypdf and assert on its text, so
they check what a reader would see rather than how it was drawn.
"""

from __future__ import annotations

import io
import json
from copy import deepcopy

import pytest
import yaml
from django.core.files.uploadedfile import SimpleUploadedFile
from pypdf import PdfReader
from rest_framework import status

from apps.testing import executor, openapi, report, services
from apps.testing.models import (
    Endpoint,
    Finding,
    RequestRecord,
    ResponseRecord,
    Specification,
    TestCaseStatus,
    TestRun,
)
from apps.testing.tests.fixtures import REALISTIC_SPECIFICATION
from apps.testing.tests.test_analysis_service import FakeAnalyst, finished_run  # noqa: F401

pytestmark = pytest.mark.django_db

UPLOAD_URL = "/api/v1/specifications/upload/"


def _upload(api_client, content: bytes, filename: str = "api.json", **fields):
    return api_client.post(
        UPLOAD_URL,
        {"file": SimpleUploadedFile(filename, content), **fields},
        format="multipart",
    )


def _pdf_text(pdf: bytes) -> str:
    reader = PdfReader(io.BytesIO(pdf))
    return "\n".join(page.extract_text() for page in reader.pages)


# --- Upload --------------------------------------------------------------------


def test_uploading_json_creates_and_imports_the_specification(api_client) -> None:
    response = _upload(api_client, json.dumps(REALISTIC_SPECIFICATION).encode())

    assert response.status_code == status.HTTP_201_CREATED
    spec = response.data["specification"]
    assert (spec["name"], spec["version"]) == ("Directory API", "1.0.0")
    assert spec["endpoint_count"] == response.data["import"]["imported_count"] > 0
    assert Endpoint.objects.filter(specification_id=spec["id"]).count() == spec["endpoint_count"]


def test_uploading_yaml_works_and_the_name_can_be_overridden(api_client) -> None:
    content = yaml.safe_dump(REALISTIC_SPECIFICATION).encode()

    response = _upload(api_client, content, "api.yaml", name="Staging directory", version="2")

    assert response.status_code == status.HTTP_201_CREATED
    assert response.data["specification"]["name"] == "Staging directory"
    assert response.data["specification"]["version"] == "2"


def test_yaml_dates_are_stored_as_text(api_client) -> None:
    document = deepcopy(REALISTIC_SPECIFICATION)
    document["info"]["x-released"] = "2026-01-01"
    content = yaml.safe_dump(document).replace("'2026-01-01'", "2026-01-01").encode()

    response = _upload(api_client, content, "api.yaml")

    stored = Specification.objects.get(pk=response.data["specification"]["id"])
    assert stored.raw_specification["info"]["x-released"] == "2026-01-01"


def test_a_duplicate_upload_is_a_400_naming_the_clash(api_client) -> None:
    content = json.dumps(REALISTIC_SPECIFICATION).encode()
    _upload(api_client, content)

    response = _upload(api_client, content)

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert "already exists" in response.data["name"][0]
    assert Specification.objects.count() == 1


@pytest.mark.parametrize(
    ("content", "message"),
    [
        (b"\xff\xfe\x00bad", "UTF-8"),
        (b"paths: [unclosed", "neither valid JSON nor valid YAML"),
        (b"[1, 2]", "does not hold"),
        (b"base: &a [1]\ncopy: *a\n", "aliases"),
    ],
)
def test_an_unreadable_file_is_a_400(api_client, content: bytes, message: str) -> None:
    response = _upload(api_client, content, "api.yaml")

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert message in str(response.data["file"])


def test_a_document_that_cannot_be_imported_leaves_nothing_behind(api_client) -> None:
    response = _upload(api_client, json.dumps({"info": {"title": "Nope"}}).encode())

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert "file" in response.data
    assert not Specification.objects.exists()


def test_an_oversized_file_is_refused(api_client, settings) -> None:
    settings.SPEC_UPLOAD_MAX_BYTES = 10

    response = _upload(api_client, json.dumps(REALISTIC_SPECIFICATION).encode())

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert "larger than 10 bytes" in str(response.data["file"])


@pytest.mark.parametrize(
    ("document", "expected"),
    [
        ({"servers": [{"url": "https://api.example.com/v1/"}]}, "https://api.example.com/v1"),
        ({"servers": [{"url": "/v1"}]}, ""),
        ({"servers": [{"url": "https://{region}.example.com"}]}, ""),
        ({"host": "api.example.com", "basePath": "/v2", "schemes": ["http"]}, "http://api.example.com/v2"),
        ({"host": "api.example.com", "schemes": ["http", "https"]}, "https://api.example.com"),
        ({}, ""),
        ("not a document", ""),
    ],
)
def test_the_suggested_base_url(document, expected: str) -> None:
    assert openapi.suggested_base_url(document) == expected


def test_specifications_report_their_endpoint_count_and_base_url(api_client) -> None:
    document = deepcopy(REALISTIC_SPECIFICATION)
    document["servers"] = [{"url": "http://directory.test/api"}]
    _upload(api_client, json.dumps(document).encode())

    listed = api_client.get("/api/v1/specifications/").data["results"][0]

    assert listed["endpoint_count"] > 0
    assert listed["suggested_base_url"] == "http://directory.test/api"


# --- Report --------------------------------------------------------------------


def test_the_report_downloads_as_a_pdf(api_client, finished_run: TestRun) -> None:  # noqa: F811
    response = api_client.get(f"/api/v1/test-runs/{finished_run.pk}/report.pdf")

    assert response.status_code == status.HTTP_200_OK
    assert response["Content-Type"] == "application/pdf"
    assert response["Content-Disposition"].startswith("attachment;")
    assert f"run-{finished_run.pk}.pdf" in response["Content-Disposition"]
    assert response.content.startswith(b"%PDF")


def test_the_report_can_be_shown_inline(api_client, finished_run: TestRun) -> None:  # noqa: F811
    response = api_client.get(f"/api/v1/test-runs/{finished_run.pk}/report.pdf?inline=1")

    assert response["Content-Disposition"].startswith("inline;")


def test_the_report_holds_the_results_endpoints_and_every_finding(finished_run: TestRun) -> None:  # noqa: F811
    text = _pdf_text(services.test_run_report_pdf(test_run=finished_run))
    findings = list(services.list_findings(test_run=finished_run))

    assert "AegisTest API test report" in text
    assert "Directory API" in text
    assert "Executive summary" in text
    assert "POST /users" in text
    for finding in findings:
        assert f"#{finding.pk}" in text
    assert "AI analysis" not in text
    assert "Bearer secret" not in text


def test_the_report_includes_the_latest_completed_analysis(finished_run: TestRun, settings) -> None:  # noqa: F811
    settings.LLM_ANALYSIS_ENABLED = True
    settings.LLM_FINDINGS_PER_REQUEST = 100
    analysis = services.start_analysis(test_run=finished_run, enqueue=False)
    services.execute_analysis(analysis=analysis, analyst=FakeAnalyst())

    text = _pdf_text(services.test_run_report_pdf(test_run=finished_run))

    assert "AI analysis" in text
    assert "POST /users validates almost nothing." in text
    assert "Likely root causes" in text
    assert "AI assessment: Likely real" in text


def test_a_run_with_no_findings_still_reports(specification) -> None:
    run = TestRun.objects.create(specification=specification, name="Clean run")

    text = _pdf_text(services.test_run_report_pdf(test_run=run))

    assert "The run produced no findings." in text


def test_markup_and_unusual_characters_in_evidence_do_not_break_the_pdf() -> None:
    finding = report.ReportFinding(
        id=1,
        title="<script>alert(1)</script> & friends",
        severity="high",
        category="bug",
        description="Line one\nLine two",
        endpoint="GET /x",
        request_method="GET",
        request_url="http://x.test/" + "a" * 400,
        response_status=500,
        response_body={"trace": "తెలుగు ✓ " + "b" * 3000},
    )
    data = report.RunReport(
        specification="API",
        run_name="Run",
        run_id=1,
        base_url="http://x.test",
        status="completed",
        status_detail="",
        started_at=None,
        completed_at=None,
        generated_at=__import__("datetime").datetime(2026, 9, 28),
        results={"total": 1, "failed": 1},
        findings=[finding],
        findings_total=1,
    )

    text = _pdf_text(report.render(data))

    assert "<script>alert(1)</script> & friends" in text
    assert "[truncated]" in text


# --- Report content ----------------------------------------------------------------


def test_the_executive_summary_counts_come_from_the_stored_cases(finished_run: TestRun) -> None:  # noqa: F811
    data = services.test_run_report(test_run=finished_run)
    cases = finished_run.test_cases

    assert data.results["total"] == cases.count() == data.cases_total
    assert data.results["passed"] == cases.filter(status=TestCaseStatus.PASSED).count()
    assert data.results["failed"] == cases.filter(status=TestCaseStatus.FAILED).count()
    assert data.results["error"] == cases.filter(status=TestCaseStatus.ERROR).count()
    assert data.executed == data.results["passed"] + data.results["failed"] + data.results["error"]
    assert data.not_run == cases.filter(status=TestCaseStatus.SKIPPED).count() == 1
    assert data.findings_total == Finding.objects.filter(test_case__test_run=finished_run).count()
    assert sum(e.cases for e in data.endpoints) == data.cases_total
    assert sum(e.executed for e in data.endpoints) == data.executed
    assert all(sum(e.scenarios.values()) == e.cases for e in data.endpoints)

    text = _pdf_text(report.render(data))
    assert "Executive summary" in text
    assert "operations executed at least one test case" in text
    assert "Findings count problems, not cases" in text


def test_every_executed_case_is_detailed_with_its_scenario_and_verdict(finished_run: TestRun) -> None:  # noqa: F811
    data = services.test_run_report(test_run=finished_run)

    stored = {
        (c.pk, c.status, c.expected_status_code)
        for c in finished_run.test_cases.all()
    }
    assert {(c.id, c.status, c.expected_status) for c in data.cases} == stored
    for case in data.cases:
        assert case.scenario in set(report.SCENARIO_LABELS.values()) | {"Validation"}, case.test_type
        assert case.request.startswith(case.endpoint.split(" ")[0] + " ")
        assert case.detail
        if case.status == "skipped":
            assert case.actual_status is None and case.requests_sent == 0
            continue
        assert case.actual_status is not None, case.name
    happy = next(c for c in data.cases if c.endpoint == "POST /users" and c.name == "happy_path")
    assert happy.scenario == "Happy path"
    assert happy.expected_status == 201 and happy.actual_status == 201
    assert happy.status == "passed"
    assert "body:" in happy.request and "Bearer" not in happy.request

    text = _pdf_text(report.render(data))
    assert "Test case details" in text
    assert f"#{happy.id}" in text
    assert "Required-field validation" in text


def test_an_empty_run_reports_that_nothing_was_tested() -> None:
    specification = Specification.objects.create(
        name="Directory API", version="1.0.0", raw_specification=deepcopy(REALISTIC_SPECIFICATION)
    )
    services.import_endpoints(specification=specification)
    run = TestRun.objects.create(specification=specification, name="Empty run")

    data = services.test_run_report(test_run=run)
    text = _pdf_text(report.render(data))

    assert data.cases_total == 0 and data.executed == 0 and data.findings_total == 0
    assert data.not_tested_total == specification.endpoints.count()
    assert "No test cases were generated for this run." in text
    assert "nothing about the API was tested" in text
    assert "The run produced no findings." in text
    assert "operations of the specification executed no case in this run" in text


def test_secrets_in_payloads_never_reach_the_report(finished_run: TestRun) -> None:  # noqa: F811
    case = finished_run.test_cases.filter(endpoint__method="POST").first()
    record = RequestRecord.objects.create(
        test_case=case,
        method="POST",
        url="http://directory.test/api/login",
        query_params={"api_key": "key-in-query", "page": 2},
        body={"username": "ann", "password": "hunter2", "profile": {"token": "tok-1", "author": "ann"}},
    )
    response = ResponseRecord.objects.create(
        request_record=record, status_code=200, body={"access_token": "tok-2"}, response_time_ms=3
    )
    Finding.objects.create(
        test_case=case,
        response_record=response,
        title="Login leaks",
        severity="high",
        category="security",
        confidence="high",
        probable_cause="x",
        evidence={
            "request": {"method": "POST", "url": record.url, "body": record.body},
            "response": {"status_code": 200, "body": response.body},
        },
        reproduction=[{"step": 1, "method": "POST", "url": record.url, "body": record.body, "status_code": 200}],
    )

    data = services.test_run_report(test_run=finished_run)
    text = _pdf_text(report.render(data))

    for secret in ("hunter2", "tok-1", "tok-2", "key-in-query"):
        assert secret not in text, secret
    assert executor.REDACTED in text
    assert "ann" in text  # Ordinary values stay; "author" is not a credential.
    assert "Bearer secret" not in text


def test_body_redaction_masks_credentials_at_any_depth() -> None:
    body = {"password": "p", "items": [{"apiKey": "k", "name": "n"}], "author": "a", "count": 1}

    redacted = executor.redact_body(body)

    assert redacted == {
        "password": executor.REDACTED,
        "items": [{"apiKey": executor.REDACTED, "name": "n"}],
        "author": "a",
        "count": 1,
    }
    assert executor.redact_body("plain") == "plain"
    assert executor.redact_body(None) is None


@pytest.mark.parametrize(
    "name",
    [
        "password", "Password", "user_password", "passwd", "accessToken", "refresh_token",
        "tokens", "api_key", "apiKey", "x-api-key", "client_secret", "clientSecret",
        "secrets", "private_key", "authorization", "cookie", "credentials", "otp", "jwt",
    ],
)
def test_secret_field_names_are_recognised_by_their_words(name: str) -> None:
    assert executor.redact_body({name: "v"}) == {name: executor.REDACTED}


@pytest.mark.parametrize(
    "name",
    ["footprint", "secretary", "tokenizer", "author", "pinned", "keyboard", "apiVersion",
     "key", "name", "hotpot", "passwordless_login_enabled" if False else "login_enabled"],
)
def test_ordinary_field_names_are_not_masked(name: str) -> None:
    assert executor.redact_body({name: "v"}) == {name: "v"}


@pytest.mark.parametrize(
    ("category", "severity", "confidence", "status", "group"),
    [
        ("crash", "high", "high", "open", "bug"),
        ("validation_gap", "medium", "high", "open", "bug"),
        ("state_bug", "medium", "medium", "open", "bug"),
        ("validation_gap", "low", "low", "open", "possible"),
        ("state_bug", "medium", "low", "open", "possible"),
        ("spec_gap", "low", "high", "open", "documentation"),
        ("spec_gap", "high", "high", "open", "documentation"),
        ("schema_mismatch", "medium", "medium", "open", "documentation"),
        ("false_positive_risk", "low", "low", "open", "inconclusive"),
        ("crash", "high", "high", "false_positive", "dismissed"),
        ("spec_gap", "low", "low", "confirmed", "bug"),
    ],
)
def test_findings_are_grouped_by_what_their_evidence_supports(
    category, severity, confidence, status, group
) -> None:
    assert report.classify_finding(category, severity, confidence, status) == group


def test_the_findings_section_separates_bugs_from_documentation_gaps() -> None:
    def finding(id, category, severity, confidence, status="open"):
        return report.ReportFinding(
            id=id, title=f"Finding {id}", severity=severity, category=category,
            confidence=confidence, description="", endpoint="GET /x", status=status,
        )
    data = report.RunReport(
        specification="API", run_name="Run", run_id=1, base_url="", status="completed",
        status_detail="", started_at=None, completed_at=None,
        generated_at=__import__("datetime").datetime(2026, 10, 10),
        results={"total": 3, "passed": 2, "failed": 1},
        findings=[
            finding(1, "crash", "high", "high"),
            finding(2, "spec_gap", "low", "high"),
            finding(3, "validation_gap", "low", "low"),
            finding(4, "crash", "high", "high", status="false_positive"),
        ],
        findings_total=4,
    )

    text = _pdf_text(report.render(data))

    assert text.index("Bugs (1)") < text.index("Finding 1") < text.index("Possible issues (1)")
    assert text.index("Possible issues (1)") < text.index("Finding 3") < text.index("Documentation gaps (1)")
    assert text.index("Documentation gaps (1)") < text.index("Finding 2") < text.index("Dismissed as false positives (1)")
    assert "Why it matters" in text
    assert "not a defect" in text


def test_a_large_suite_is_capped_and_still_renders() -> None:
    cases = [
        report.ReportCase(
            id=i, name=f"case_{i}", endpoint=f"GET /things/{i % 7}", test_type="happy_path",
            purpose="p" * 400, request="GET /things\nbody: " + json.dumps({"k": "v" * 2000}),
            expected_status=200, actual_status=200, status="passed", detail="d" * 900,
        )
        for i in range(120)
    ]
    data = report.RunReport(
        specification="API", run_name="Run", run_id=1, base_url="", status="completed",
        status_detail="", started_at=None, completed_at=None,
        generated_at=__import__("datetime").datetime(2026, 10, 10),
        results={"total": 1200, "passed": 1200}, cases=cases, cases_total=1200,
        sequences=[], sequences_total=0,
    )

    text = _pdf_text(report.render(data))

    assert "The first 120 of 1200 cases are shown" in text
    assert "[truncated]" in text
    assert "case_119" in text


def test_the_limitations_name_cases_never_sent_and_operations_never_tested(finished_run: TestRun) -> None:  # noqa: F811
    skipped = finished_run.test_cases.filter(endpoint__method="DELETE")
    skipped_count = skipped.update(
        status=TestCaseStatus.SKIPPED, status_detail="Not sent: the target could not be reached."
    )
    assert skipped_count

    data = services.test_run_report(test_run=finished_run)
    text = _pdf_text(report.render(data))

    assert data.not_run == skipped_count
    assert data.not_run_reasons == [("Not sent: the target could not be reached", skipped_count)]
    assert data.not_tested == ["DELETE /users/{userId}"]
    assert f"{skipped_count} generated cases were never sent" in text
    assert "DELETE /users/{userId}" in text.replace("\n", "")
    assert "do not cover authentication" in text


def test_untested_operations_are_judged_from_this_run_only(finished_run: TestRun) -> None:  # noqa: F811
    """A second run on the same specification whose cases were all skipped must
    list every operation as untested, although the first run executed them."""
    second = services.create_test_run(
        specification=finished_run.specification, name="Second run", base_url=finished_run.base_url
    )
    services.generate_test_cases_for_run(test_run=second)
    second.test_cases.update(
        status=TestCaseStatus.SKIPPED, status_detail="Not sent: the target could not be reached."
    )

    data = services.test_run_report(test_run=second)

    operations = {f"{e.method} {e.path}" for e in finished_run.specification.endpoints.all()}
    assert set(data.not_tested) == operations
    assert data.not_tested_total == len(operations)
    assert data.executed == 0 and data.not_run == second.test_cases.count()
    # The first run is unaffected.
    assert services.test_run_report(test_run=finished_run).not_tested == []


def test_the_inconclusive_reasons_are_listed_in_the_limitations(finished_run: TestRun) -> None:  # noqa: F811
    cases = finished_run.test_cases.filter(endpoint__method="GET")
    count = cases.update(
        status=TestCaseStatus.ERROR,
        status_detail="The request was unauthorised (401), so the case could not be judged. Set credentials.",
    )
    assert count

    data = services.test_run_report(test_run=finished_run)
    text = _pdf_text(report.render(data))

    assert data.results["error"] == count
    assert data.inconclusive_reasons == [
        ("The request was unauthorised (401), so the case could not be judged", count)
    ]
    assert f"{count} cases got no verdict: {count} - The request was unauthorised (401)" in text
    assert "Errors" in text and "Error" in text  # Count tiles say Errors; a case says Error.


def test_a_sequence_step_without_a_response_shows_its_error(finished_run: TestRun) -> None:  # noqa: F811
    case = finished_run.test_cases.filter(endpoint__method="POST").first()
    case.request_records.all().delete()
    first = RequestRecord.objects.create(
        test_case=case, method="POST", url="http://directory.test/api/users", body={"name": "a"}
    )
    ResponseRecord.objects.create(request_record=first, status_code=201, body={"id": 1}, response_time_ms=1)
    RequestRecord.objects.create(
        test_case=case, method="POST", url="http://directory.test/api/users", body={"name": "a"},
        error="No response within the timeout (ReadTimeout).",
    )

    data = services.test_run_report(test_run=finished_run)
    sequence = next(s for s in data.sequences if s.case == case.name and s.endpoint == "POST /users")
    text = _pdf_text(report.render(data))

    assert [step["status_code"] for step in sequence.steps] == [201, None]
    assert sequence.steps[1]["note"] == "No response within the timeout (ReadTimeout)."
    assert "2. POST /users -> no response - No response within the timeout" in text
    shown = next(c for c in data.cases if c.id == case.pk)
    assert shown.actual_status is None  # The request under test got no answer.


def test_finding_counts_describe_the_findings_shown_when_capped() -> None:
    findings = [
        report.ReportFinding(
            id=i, title=f"F{i}", severity="low", category="spec_gap", confidence="high",
            description="", endpoint="GET /x", status="false_positive" if i == 0 else "open",
        )
        for i in range(3)
    ]
    data = report.RunReport(
        specification="API", run_name="Run", run_id=1, base_url="", status="completed",
        status_detail="", started_at=None, completed_at=None,
        generated_at=__import__("datetime").datetime(2026, 10, 10),
        results={"total": 1}, findings=findings, findings_total=500,
    )

    text = _pdf_text(report.render(data))

    assert "Findings shown" in text
    assert "The run produced 500 findings; the 3 most severe are in this report" in text
    assert "Findings by severity (shown)" in text
    assert "Dismissed as false positives" in text
    assert "The 3 most severe of 500 findings are shown" in text


def test_the_case_table_shows_how_many_findings_a_case_produced(finished_run: TestRun) -> None:  # noqa: F811
    data = services.test_run_report(test_run=finished_run)
    with_findings = [c for c in data.cases if c.findings]

    assert with_findings
    assert {c.id for c in with_findings} == set(
        Finding.objects.filter(test_case__test_run=finished_run).values_list("test_case_id", flat=True)
    )
    text = _pdf_text(report.render(data))
    assert "1 finding" in text


def test_the_report_of_a_missing_run_is_404(api_client) -> None:
    assert api_client.get("/api/v1/test-runs/999/report.pdf").status_code == 404


def test_features_say_whether_analysis_is_on(api_client, settings) -> None:
    settings.LLM_ANALYSIS_ENABLED = False
    assert api_client.get("/api/v1/features/").data == {"llm_analysis": False, "llm_model": ""}

    settings.LLM_ANALYSIS_ENABLED = True
    settings.LLM_MODEL = "claude-opus-5"
    assert api_client.get("/api/v1/features/").data["llm_analysis"] is True
