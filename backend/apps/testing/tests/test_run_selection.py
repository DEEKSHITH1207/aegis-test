"""Testing part of an API: tags, endpoint selection, and running again.

The Directory API document is given two sections: "Users" (list and create)
and "User details" (fetch and delete), with fetch also filed under "Users".
"""

from __future__ import annotations

import io
from copy import deepcopy

import pytest
from django.core.exceptions import ValidationError
from pypdf import PdfReader
from rest_framework import status

from apps.testing import openapi, services
from apps.testing.models import Endpoint, Specification, TestRun, TestRunStatus
from apps.testing.tasks import execute_test_run_task
from apps.testing.tests.fixtures import REALISTIC_SPECIFICATION
from apps.testing.tests.test_execution_service import BASE_URL

pytestmark = pytest.mark.django_db


def _tagged_document() -> dict:
    document = deepcopy(REALISTIC_SPECIFICATION)
    paths = document["paths"]
    paths["/users"]["get"]["tags"] = ["Users"]
    paths["/users"]["post"]["tags"] = ["Users", "Users", " "]
    paths["/users/{userId}"]["get"]["tags"] = ["User details", "Users"]
    paths["/users/{userId}"]["delete"]["tags"] = ["User details"]
    return document


@pytest.fixture
def directory(db) -> Specification:
    specification = Specification.objects.create(
        name="Directory API", version="1.0.0", raw_specification=_tagged_document()
    )
    services.import_endpoints(specification=specification)
    return specification


def _endpoint(specification: Specification, method: str, path: str) -> Endpoint:
    return specification.endpoints.get(method=method, path=path)


# --- Tags ------------------------------------------------------------------------


def test_tags_are_trimmed_deduplicated_and_kept_in_order() -> None:
    assert openapi.operation_tags({"tags": [" Users ", "Users", "", 3, "Admin"]}) == ["Users", "Admin"]
    assert openapi.operation_tags({"tags": "Users"}) == []
    assert openapi.operation_tags({}) == []


def test_import_stores_each_endpoints_tags(directory: Specification) -> None:
    assert _endpoint(directory, "POST", "/users").tags == ["Users"]
    assert _endpoint(directory, "GET", "/users/{userId}").tags == ["User details", "Users"]


def test_a_retagged_operation_is_updated_on_reimport(directory: Specification) -> None:
    document = _tagged_document()
    document["paths"]["/users"]["post"]["tags"] = ["Admin"]
    directory.raw_specification = document
    directory.save()

    result = services.import_endpoints(specification=directory)

    assert [(e.method, e.path) for e in result.updated] == [("POST", "/users")]
    assert _endpoint(directory, "POST", "/users").tags == ["Admin"]


def test_endpoints_list_their_tags_over_the_api(api_client, directory: Specification) -> None:
    rows = api_client.get(f"/api/v1/specifications/{directory.pk}/endpoints/").data["results"]

    assert {(r["method"], r["path"]): r["tags"] for r in rows}[("DELETE", "/users/{userId}")] == [
        "User details"
    ]


# --- Selecting ---------------------------------------------------------------------


def test_a_run_without_a_selection_tests_everything(directory: Specification) -> None:
    run = services.create_test_run(specification=directory)

    result = services.generate_test_cases_for_run(test_run=run)

    assert result.endpoints == 4
    assert services.describe_scope(run) == {"all": True, "endpoints": 4, "total": 4, "tags": []}


def test_a_run_can_be_limited_to_one_section(directory: Specification) -> None:
    run = services.create_test_run(specification=directory, tags=["User details"])

    result = services.generate_test_cases_for_run(test_run=run)

    tested = set(run.test_cases.values_list("endpoint__method", "endpoint__path"))
    assert result.endpoints == 2
    assert tested == {("GET", "/users/{userId}"), ("DELETE", "/users/{userId}")}
    assert services.describe_scope(run)["tags"] == ["User details", "Users"]


def test_sections_and_single_endpoints_combine(directory: Specification) -> None:
    post = _endpoint(directory, "POST", "/users")

    run = services.create_test_run(specification=directory, tags=["User details"], endpoints=[post])

    assert run.endpoints.count() == 3


def test_an_unknown_section_is_refused_and_the_real_ones_are_named(directory: Specification) -> None:
    with pytest.raises(ValidationError) as excinfo:
        services.create_test_run(specification=directory, tags=["Orders"])

    message = excinfo.value.message_dict["tags"][0]
    assert "'Orders'" in message
    assert "User details, Users" in message
    assert not TestRun.objects.exists()


def test_an_endpoint_of_another_api_is_refused(directory: Specification, endpoint: Endpoint) -> None:
    with pytest.raises(ValidationError) as excinfo:
        services.create_test_run(specification=directory, endpoints=[endpoint])

    assert "another specification" in excinfo.value.message_dict["endpoints"][0]


def test_a_run_is_created_with_a_selection_over_the_api(api_client, directory: Specification) -> None:
    post = _endpoint(directory, "POST", "/users")

    response = api_client.post(
        "/api/v1/test-runs/",
        {"specification": directory.pk, "base_url": BASE_URL, "endpoints": [post.pk], "tags": []},
        format="json",
    )

    assert response.status_code == status.HTTP_201_CREATED
    assert response.data["endpoints"] == [post.pk]
    assert response.data["scope"] == {"all": False, "endpoints": 1, "total": 4, "tags": ["Users"]}
    assert "tags" not in response.data


def test_selecting_by_tag_over_the_api(api_client, directory: Specification) -> None:
    response = api_client.post(
        "/api/v1/test-runs/",
        {"specification": directory.pk, "tags": ["Users"]},
        format="json",
    )

    assert response.status_code == status.HTTP_201_CREATED
    assert response.data["scope"]["endpoints"] == 3


def test_an_unknown_tag_over_the_api_is_a_400(api_client, directory: Specification) -> None:
    response = api_client.post(
        "/api/v1/test-runs/", {"specification": directory.pk, "tags": ["Orders"]}, format="json"
    )

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert "Orders" in str(response.data["tags"])


def test_the_run_list_reports_each_runs_scope(api_client, directory: Specification) -> None:
    services.create_test_run(specification=directory, tags=["User details"])
    services.create_test_run(specification=directory)

    rows = api_client.get("/api/v1/test-runs/").data["results"]

    assert sorted(row["scope"]["endpoints"] for row in rows) == [2, 4]


# --- Running again -----------------------------------------------------------------


@pytest.fixture
def finished_section_run(directory: Specification) -> TestRun:
    run = services.create_test_run(
        specification=directory,
        name="Details only",
        base_url=BASE_URL,
        headers={"Authorization": "Bearer secret"},
        tags=["User details"],
    )
    services.generate_test_cases_for_run(test_run=run)
    services.start_test_run(run)
    services.complete_test_run(run)
    return run


def test_a_rerun_copies_the_target_headers_and_selection(finished_section_run: TestRun) -> None:
    rerun = services.rerun_test_run(test_run=finished_section_run, enqueue=False)

    assert rerun.pk != finished_section_run.pk
    assert rerun.status == TestRunStatus.RUNNING
    assert rerun.base_url == BASE_URL
    assert rerun.headers == {"Authorization": "Bearer secret"}
    assert set(rerun.endpoints.all()) == set(finished_section_run.endpoints.all())
    assert rerun.test_cases.count() == finished_section_run.test_cases.count()


def test_a_rerun_over_the_api_is_queued(
    api_client, finished_section_run: TestRun, monkeypatch, django_capture_on_commit_callbacks
) -> None:
    queued = []
    monkeypatch.setattr(execute_test_run_task, "delay", lambda *args: queued.append(args))

    with django_capture_on_commit_callbacks(execute=True):
        response = api_client.post(f"/api/v1/test-runs/{finished_section_run.pk}/rerun/")

    assert response.status_code == status.HTTP_202_ACCEPTED
    assert response.data["status"] == "running"
    assert response.data["scope"]["endpoints"] == 2
    assert "headers" not in response.data
    assert queued == [(response.data["id"],)]


def test_a_rerun_of_a_run_without_a_target_is_refused(directory: Specification) -> None:
    run = services.create_test_run(specification=directory)

    with pytest.raises(ValidationError):
        services.rerun_test_run(test_run=run, enqueue=False)
    assert TestRun.objects.count() == 1


def test_the_report_states_the_scope(finished_section_run: TestRun) -> None:
    pdf = services.test_run_report_pdf(test_run=finished_section_run)
    text = "\n".join(p.extract_text() for p in PdfReader(io.BytesIO(pdf)).pages)

    assert "2 of 4 endpoints - sections: User details, Users" in text
