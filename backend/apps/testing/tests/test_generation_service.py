"""Persisting generated test cases: the service and the API around it."""

from __future__ import annotations

from copy import deepcopy

import pytest
from django.urls import reverse
from rest_framework import status

from apps.testing import services
from apps.testing.models import (
    Specification,
    TestCase,
    TestCaseStatus,
    TestCaseType,
    TestRun,
    TestRunStatus,
)
from apps.testing.tests.fixtures import REALISTIC_SPECIFICATION

pytestmark = pytest.mark.django_db


@pytest.fixture
def directory(db) -> Specification:
    specification = Specification.objects.create(
        name="Directory API", version="1.0.0", raw_specification=deepcopy(REALISTIC_SPECIFICATION)
    )
    services.import_endpoints(specification=specification)
    return specification


@pytest.fixture
def run(directory: Specification) -> TestRun:
    return services.create_test_run(specification=directory, name="Generation run")


# --- Generation over a realistic specification --------------------------------


def test_generation_covers_every_endpoint(run: TestRun, directory: Specification) -> None:
    result = services.generate_test_cases_for_run(test_run=run)

    assert result.endpoints == directory.endpoints.count() == 4
    assert result.created == run.test_cases.count()
    assert result.created > 30
    assert result.skipped == 0
    # Every endpoint contributed at least its happy path.
    assert run.test_cases.filter(test_type=TestCaseType.HAPPY_PATH).count() == 4


def test_generation_produces_the_expected_kinds_of_case(run: TestRun) -> None:
    services.generate_test_cases_for_run(test_run=run)

    produced = set(run.test_cases.values_list("test_type", flat=True))
    assert produced == {
        TestCaseType.HAPPY_PATH,
        TestCaseType.MISSING_REQUIRED,
        TestCaseType.EMPTY_VALUE,
        TestCaseType.INVALID_TYPE,
        TestCaseType.INVALID_FORMAT,
        TestCaseType.INVALID_ENUM,
        TestCaseType.BELOW_MINIMUM,
        TestCaseType.ABOVE_MAXIMUM,
        TestCaseType.BELOW_MIN_LENGTH,
        TestCaseType.ABOVE_MAX_LENGTH,
    }


def test_generated_cases_are_attached_to_the_right_endpoint(run: TestRun) -> None:
    services.generate_test_cases_for_run(test_run=run)

    for case in run.test_cases.select_related("endpoint"):
        assert case.endpoint.specification_id == run.specification_id


def test_generated_cases_start_pending(run: TestRun) -> None:
    services.generate_test_cases_for_run(test_run=run)

    assert set(run.test_cases.values_list("status", flat=True)) == {TestCaseStatus.PENDING}


def test_a_get_endpoint_with_no_body_still_gets_parameter_cases(run: TestRun) -> None:
    services.generate_test_cases_for_run(test_run=run)

    listing = run.test_cases.filter(endpoint__path="/users", endpoint__method="GET")
    names = set(listing.values_list("name", flat=True))
    assert "happy_path" in names
    assert "missing_required:query.limit" in names
    assert "invalid_enum:query.status" in names
    assert all(case.input_data["body"] is None for case in listing)


def test_status_codes_come_from_the_document(run: TestRun) -> None:
    services.generate_test_cases_for_run(test_run=run)

    post = run.test_cases.filter(endpoint__path="/users", endpoint__method="POST")
    assert post.get(name="happy_path").expected_status_code == 201
    assert post.get(name="missing_required:name").expected_status_code == 400


def test_an_endpoint_documenting_no_client_error_gets_no_expectation(run: TestRun) -> None:
    services.generate_test_cases_for_run(test_run=run)

    # DELETE /users/{userId} documents only 204.
    delete = run.test_cases.filter(endpoint__method="DELETE")
    assert delete.get(name="happy_path").expected_status_code == 204
    assert all(
        case.expected_status_code is None
        for case in delete.exclude(test_type=TestCaseType.HAPPY_PATH)
    )


def test_input_data_separates_the_request_components(run: TestRun) -> None:
    services.generate_test_cases_for_run(test_run=run)

    case = run.test_cases.get(endpoint__path="/users/{userId}", endpoint__method="GET", name="happy_path")
    assert case.input_data == {"path": {"userId": 1}, "query": {}, "headers": {}, "body": None}


# --- Re-generation ------------------------------------------------------------


def test_generating_twice_creates_no_duplicates(run: TestRun) -> None:
    first = services.generate_test_cases_for_run(test_run=run)

    second = services.generate_test_cases_for_run(test_run=run)

    assert second.created == 0
    assert second.updated == 0
    assert second.unchanged == first.created
    assert TestCase.objects.count() == first.created


def test_regeneration_updates_a_case_in_place(run: TestRun) -> None:
    services.generate_test_cases_for_run(test_run=run)
    case = run.test_cases.get(endpoint__path="/users", endpoint__method="POST", name="below_minimum:age")
    original_pk = case.pk

    # Raise the documented minimum; the violating value must follow it.
    schema = run.specification.raw_specification["paths"]["/users"]["post"]["requestBody"][
        "content"
    ]["application/json"]["schema"]
    schema["properties"]["age"]["minimum"] = 21
    run.specification.save()
    services.import_endpoints(specification=run.specification)

    result = services.generate_test_cases_for_run(test_run=run)

    case.refresh_from_db()
    assert case.pk == original_pk
    assert case.input_data["body"]["age"] == 20
    # Nothing is duplicated and nothing is orphaned: the same rows are rewritten.
    assert result.created == 0
    assert result.updated + result.unchanged == run.test_cases.count()

    post_cases = run.test_cases.filter(endpoint__path="/users", endpoint__method="POST")
    # Every case that carries a valid age moved to the new minimum. The ones
    # that deliberately omit, null or override it are untouched, which is why
    # the update count is lower than the number of cases for this endpoint.
    carrying_a_valid_age = [
        case
        for case in post_cases
        if isinstance(case.input_data["body"], dict)
        and isinstance(case.input_data["body"].get("age"), int)
        and case.name not in {"below_minimum:age", "above_maximum:age"}
    ]
    assert carrying_a_valid_age
    assert all(case.input_data["body"]["age"] == 21 for case in carrying_a_valid_age)


def test_a_second_run_gets_its_own_cases(directory: Specification, run: TestRun) -> None:
    services.generate_test_cases_for_run(test_run=run)
    other = services.create_test_run(specification=directory, name="Second run")

    result = services.generate_test_cases_for_run(test_run=other)

    assert result.created == run.test_cases.count()
    assert other.test_cases.count() == run.test_cases.count()


# --- Rules and failure modes --------------------------------------------------


def test_generation_is_refused_for_a_run_that_has_started(run: TestRun) -> None:
    services.start_test_run(run)

    with pytest.raises(services.DomainError):
        services.generate_test_cases_for_run(test_run=run)

    assert run.test_cases.count() == 0


def test_a_specification_with_no_endpoints_generates_nothing(db) -> None:
    specification = Specification.objects.create(name="Bare", raw_specification={})
    test_run = services.create_test_run(specification=specification, name="Bare run")

    result = services.generate_test_cases_for_run(test_run=test_run)

    assert result.endpoints == 0
    assert result.created == 0


def test_an_unsupported_body_schema_is_skipped_with_a_warning(db) -> None:
    specification = Specification.objects.create(
        name="Recursive",
        raw_specification={
            "openapi": "3.0.3",
            "paths": {
                "/nodes": {
                    "post": {
                        "requestBody": {
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/Node"}
                                }
                            }
                        },
                        "responses": {"200": {"description": "OK"}},
                    }
                }
            },
        },
    )
    services.import_endpoints(specification=specification)
    test_run = services.create_test_run(specification=specification, name="Recursive run")

    result = services.generate_test_cases_for_run(test_run=test_run)

    assert result.endpoints == 1
    assert result.skipped == 1
    assert result.created == 0
    assert any("not supported" in warning for warning in result.warnings)


def test_a_failed_generation_leaves_nothing_behind(run: TestRun, monkeypatch) -> None:
    from apps.testing import generator

    real = generator.generate_test_cases
    calls = {"n": 0}

    def explode_on_the_second_endpoint(endpoint, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("generation blew up")
        return real(endpoint, **kwargs)

    monkeypatch.setattr(services.generator, "generate_test_cases", explode_on_the_second_endpoint)

    with pytest.raises(RuntimeError):
        services.generate_test_cases_for_run(test_run=run)

    # The first endpoint's cases were rolled back with the rest.
    assert TestCase.objects.count() == 0


# --- API ----------------------------------------------------------------------


def test_the_generate_route_is_mounted_under_v1() -> None:
    assert (
        reverse("v1:test-run-generate-tests", args=[5])
        == "/api/v1/test-runs/5/generate-tests/"
    )


def test_post_generate_tests_returns_a_summary(api_client, run: TestRun) -> None:
    response = api_client.post(f"/api/v1/test-runs/{run.pk}/generate-tests/")

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["test_run_id"] == run.pk
    assert body["endpoints"] == 4
    assert body["created"] == run.test_cases.count()
    assert body["updated"] == 0
    assert body["unchanged"] == 0
    assert body["skipped"] == 0
    assert body["warnings"] == []


def test_generated_cases_are_then_listable_on_the_run(api_client, run: TestRun) -> None:
    api_client.post(f"/api/v1/test-runs/{run.pk}/generate-tests/")

    response = api_client.get(f"/api/v1/test-runs/{run.pk}/test-cases/")

    assert response.json()["count"] == run.test_cases.count()


def test_a_repeated_generate_call_reports_everything_unchanged(
    api_client, run: TestRun
) -> None:
    first = api_client.post(f"/api/v1/test-runs/{run.pk}/generate-tests/").json()

    second = api_client.post(f"/api/v1/test-runs/{run.pk}/generate-tests/").json()

    assert second["created"] == 0
    assert second["unchanged"] == first["created"]
    assert TestCase.objects.count() == first["created"]


def test_generating_for_a_started_run_is_a_400(api_client, run: TestRun) -> None:
    services.start_test_run(run)

    response = api_client.post(f"/api/v1/test-runs/{run.pk}/generate-tests/")

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert "pending" in str(response.json())


def test_generating_for_a_missing_run_is_404(api_client) -> None:
    response = api_client.post("/api/v1/test-runs/999999/generate-tests/")

    assert response.status_code == status.HTTP_404_NOT_FOUND


def test_generate_is_not_reachable_by_get(api_client, run: TestRun) -> None:
    response = api_client.get(f"/api/v1/test-runs/{run.pk}/generate-tests/")

    assert response.status_code == status.HTTP_405_METHOD_NOT_ALLOWED


def test_the_api_reports_warnings(api_client, db) -> None:
    specification = Specification.objects.create(
        name="Patterned",
        raw_specification={
            "openapi": "3.0.3",
            "paths": {
                "/codes": {
                    "post": {
                        "requestBody": {
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {
                                            "code": {"type": "string", "pattern": "^[A-Z]+$"}
                                        },
                                    }
                                }
                            }
                        },
                        "responses": {"200": {"description": "OK"}},
                    }
                }
            },
        },
    )
    services.import_endpoints(specification=specification)
    test_run = services.create_test_run(specification=specification, name="Pattern run")

    body = api_client.post(f"/api/v1/test-runs/{test_run.pk}/generate-tests/").json()

    assert any("pattern" in warning for warning in body["warnings"])
