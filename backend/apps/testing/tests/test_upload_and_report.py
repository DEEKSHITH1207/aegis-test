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

from apps.testing import openapi, report, services
from apps.testing.models import Endpoint, Finding, Specification, TestRun
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
    assert "At a glance" in text
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


def test_the_report_of_a_missing_run_is_404(api_client) -> None:
    assert api_client.get("/api/v1/test-runs/999/report.pdf").status_code == 404


def test_features_say_whether_analysis_is_on(api_client, settings) -> None:
    settings.LLM_ANALYSIS_ENABLED = False
    assert api_client.get("/api/v1/features/").data == {"llm_analysis": False, "llm_model": ""}

    settings.LLM_ANALYSIS_ENABLED = True
    settings.LLM_MODEL = "claude-opus-5"
    assert api_client.get("/api/v1/features/").data["llm_analysis"] is True
