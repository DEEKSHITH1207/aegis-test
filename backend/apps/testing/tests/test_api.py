"""API tests: routing, status codes, payload shape and edge-level validation."""

from __future__ import annotations

import pytest
from django.urls import reverse
from rest_framework import status

from apps.testing.models import (
    Endpoint,
    Finding,
    FindingSeverity,
    HTTPMethod,
    Specification,
    TestCase,
    TestRun,
    TestRunStatus,
)

pytestmark = pytest.mark.django_db


# --- Specifications -----------------------------------------------------------


def test_specification_routes_are_mounted_under_v1() -> None:
    assert reverse("v1:specification-list") == "/api/v1/specifications/"
    assert reverse("v1:specification-detail", args=[7]) == "/api/v1/specifications/7/"


def test_create_specification(api_client) -> None:
    payload = {
        "name": "Orders API",
        "description": "Order management.",
        "source_type": "openapi",
        "version": "2.0.0",
        "raw_specification": {"openapi": "3.0.0", "paths": {"/orders": {}}},
    }

    response = api_client.post("/api/v1/specifications/", payload, format="json")

    assert response.status_code == status.HTTP_201_CREATED
    body = response.json()
    assert body["name"] == "Orders API"
    assert body["raw_specification"] == {"openapi": "3.0.0", "paths": {"/orders": {}}}
    assert Specification.objects.filter(pk=body["id"]).exists()


def test_create_specification_without_a_name_is_rejected(api_client) -> None:
    response = api_client.post("/api/v1/specifications/", {"description": "x"}, format="json")

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert "name" in response.json()


def test_create_specification_with_an_unknown_source_type_is_rejected(api_client) -> None:
    response = api_client.post(
        "/api/v1/specifications/", {"name": "X", "source_type": "raml"}, format="json"
    )

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert "source_type" in response.json()


def test_list_specifications_is_paginated(api_client, specification: Specification) -> None:
    response = api_client.get("/api/v1/specifications/")

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["count"] == 1
    assert body["results"][0]["name"] == "Petstore"


def test_retrieve_specification(api_client, specification: Specification) -> None:
    response = api_client.get(f"/api/v1/specifications/{specification.pk}/")

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["version"] == "1.0.0"


def test_retrieve_a_missing_specification_is_404(api_client) -> None:
    response = api_client.get("/api/v1/specifications/999999/")

    assert response.status_code == status.HTTP_404_NOT_FOUND


# --- Endpoints ----------------------------------------------------------------


def test_list_endpoints_of_a_specification(
    api_client, specification: Specification, endpoint: Endpoint
) -> None:
    response = api_client.get(f"/api/v1/specifications/{specification.pk}/endpoints/")

    assert response.status_code == status.HTTP_200_OK
    results = response.json()["results"]
    assert len(results) == 1
    assert results[0]["path"] == "/pets/{petId}"
    assert results[0]["method"] == "GET"


def test_endpoints_of_another_specification_are_not_listed(
    api_client, specification: Specification, endpoint: Endpoint
) -> None:
    other = Specification.objects.create(name="Other API")
    Endpoint.objects.create(specification=other, path="/orders", method=HTTPMethod.GET)

    response = api_client.get(f"/api/v1/specifications/{specification.pk}/endpoints/")

    assert [item["path"] for item in response.json()["results"]] == ["/pets/{petId}"]


def test_listing_endpoints_of_a_missing_specification_is_404(api_client) -> None:
    response = api_client.get("/api/v1/specifications/999999/endpoints/")

    assert response.status_code == status.HTTP_404_NOT_FOUND


# --- Test runs ----------------------------------------------------------------


def test_create_test_run(api_client, specification: Specification) -> None:
    response = api_client.post(
        "/api/v1/test-runs/",
        {"specification": specification.pk, "name": "Release candidate"},
        format="json",
    )

    assert response.status_code == status.HTTP_201_CREATED
    body = response.json()
    assert body["name"] == "Release candidate"
    # The lifecycle belongs to the system, not the client.
    assert body["status"] == TestRunStatus.PENDING
    assert body["started_at"] is None


def test_create_test_run_without_a_name_gets_a_derived_one(
    api_client, specification: Specification
) -> None:
    response = api_client.post(
        "/api/v1/test-runs/", {"specification": specification.pk}, format="json"
    )

    assert response.status_code == status.HTTP_201_CREATED
    assert response.json()["name"].startswith("Petstore - ")


def test_create_test_run_ignores_a_client_supplied_status(
    api_client, specification: Specification
) -> None:
    response = api_client.post(
        "/api/v1/test-runs/",
        {"specification": specification.pk, "status": TestRunStatus.COMPLETED},
        format="json",
    )

    assert response.json()["status"] == TestRunStatus.PENDING


def test_create_test_run_for_a_missing_specification_is_rejected(api_client) -> None:
    response = api_client.post("/api/v1/test-runs/", {"specification": 999999}, format="json")

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert "specification" in response.json()


def test_create_test_run_without_a_specification_is_rejected(api_client) -> None:
    response = api_client.post("/api/v1/test-runs/", {"name": "Orphan"}, format="json")

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert "specification" in response.json()


def test_list_and_retrieve_test_runs(api_client, test_run: TestRun) -> None:
    listed = api_client.get("/api/v1/test-runs/")
    retrieved = api_client.get(f"/api/v1/test-runs/{test_run.pk}/")

    assert listed.json()["count"] == 1
    assert retrieved.status_code == status.HTTP_200_OK
    assert retrieved.json()["name"] == "Nightly run"


def test_test_runs_can_be_filtered_by_specification(
    api_client, specification: Specification, test_run: TestRun
) -> None:
    other = Specification.objects.create(name="Other API")
    TestRun.objects.create(specification=other, name="Elsewhere")

    response = api_client.get(f"/api/v1/test-runs/?specification={specification.pk}")

    assert [item["name"] for item in response.json()["results"]] == ["Nightly run"]


# --- Test cases and findings --------------------------------------------------


def test_list_test_cases_of_a_run(api_client, test_run: TestRun, test_case: TestCase) -> None:
    response = api_client.get(f"/api/v1/test-runs/{test_run.pk}/test-cases/")

    assert response.status_code == status.HTTP_200_OK
    results = response.json()["results"]
    assert len(results) == 1
    assert results[0]["expected_status_code"] == 200
    assert results[0]["endpoint"] == test_case.endpoint_id


def test_listing_test_cases_of_a_missing_run_is_404(api_client) -> None:
    response = api_client.get("/api/v1/test-runs/999999/test-cases/")

    assert response.status_code == status.HTTP_404_NOT_FOUND


def test_list_findings_of_a_run(
    api_client, test_run: TestRun, test_case: TestCase, response_record
) -> None:
    Finding.objects.create(
        test_case=test_case,
        response_record=response_record,
        title="Unhandled server error",
        severity=FindingSeverity.HIGH,
        evidence={"expected_status_code": 200, "actual_status_code": 500},
    )

    response = api_client.get(f"/api/v1/test-runs/{test_run.pk}/findings/")

    assert response.status_code == status.HTTP_200_OK
    results = response.json()["results"]
    assert len(results) == 1
    assert results[0]["title"] == "Unhandled server error"
    assert results[0]["severity"] == "high"
    assert results[0]["evidence"]["actual_status_code"] == 500


def test_findings_of_another_run_are_not_listed(
    api_client, specification: Specification, test_run: TestRun, endpoint: Endpoint
) -> None:
    other_run = TestRun.objects.create(specification=specification, name="Other")
    other_case = TestCase.objects.create(
        test_run=other_run, endpoint=endpoint, name="Other", expected_status_code=200
    )
    Finding.objects.create(test_case=other_case, title="Belongs elsewhere")

    response = api_client.get(f"/api/v1/test-runs/{test_run.pk}/findings/")

    assert response.json()["count"] == 0


def test_listing_findings_of_a_missing_run_is_404(api_client) -> None:
    response = api_client.get("/api/v1/test-runs/999999/findings/")

    assert response.status_code == status.HTTP_404_NOT_FOUND


# --- Error translation --------------------------------------------------------


def test_a_service_validation_error_is_reported_as_400_not_500(
    api_client, monkeypatch
) -> None:
    """Services validate independently of serializers; that must not leak a 500."""
    from django.core.exceptions import ValidationError as DjangoValidationError

    from apps.testing import views

    def explode(**kwargs):
        raise DjangoValidationError({"name": "Rejected by the service layer."})

    monkeypatch.setattr(views.services, "create_specification", explode)

    response = api_client.post("/api/v1/specifications/", {"name": "Anything"}, format="json")

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert "name" in response.json()


def test_a_domain_error_is_reported_as_400(
    api_client, monkeypatch, specification: Specification
) -> None:
    from apps.core.exceptions import DomainError
    from apps.testing import views

    def explode(**kwargs):
        raise DomainError("Cannot start a run with status 'running'.")

    monkeypatch.setattr(views.services, "create_test_run", explode)

    response = api_client.post(
        "/api/v1/test-runs/", {"specification": specification.pk}, format="json"
    )

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert "running" in str(response.json())
