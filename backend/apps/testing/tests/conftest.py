"""Shared fixtures for the testing-domain suite.

Built with plain model calls rather than a factory library: the object graph is
small, and the explicit wiring is itself part of what these tests verify.
"""

from __future__ import annotations

import pytest
from rest_framework.test import APIClient

from apps.testing.models import (
    Endpoint,
    HTTPMethod,
    RequestRecord,
    ResponseRecord,
    Specification,
    TestCase,
    TestRun,
)


@pytest.fixture
def api_client() -> APIClient:
    return APIClient()


@pytest.fixture
def specification(db) -> Specification:
    return Specification.objects.create(
        name="Petstore",
        description="Sample API used across the suite.",
        version="1.0.0",
        raw_specification={"openapi": "3.0.0", "paths": {}},
    )


@pytest.fixture
def endpoint(specification: Specification) -> Endpoint:
    return Endpoint.objects.create(
        specification=specification,
        path="/pets/{petId}",
        method=HTTPMethod.GET,
        operation_id="getPetById",
        summary="Fetch one pet",
        parameters=[{"name": "petId", "in": "path", "required": True}],
        response_schema={"type": "object", "properties": {"id": {"type": "integer"}}},
    )


@pytest.fixture
def test_run(specification: Specification) -> TestRun:
    return TestRun.objects.create(specification=specification, name="Nightly run")


@pytest.fixture
def test_case(test_run: TestRun, endpoint: Endpoint) -> TestCase:
    return TestCase.objects.create(
        test_run=test_run,
        endpoint=endpoint,
        name="GET /pets/{petId} returns 200 for a known pet",
        input_data={"path_params": {"petId": 1}},
        expected_status_code=200,
    )


@pytest.fixture
def request_record(test_case: TestCase) -> RequestRecord:
    return RequestRecord.objects.create(
        test_case=test_case,
        method=HTTPMethod.GET,
        url="https://api.example.com/pets/1",
        headers={"Accept": "application/json"},
        path_params={"petId": 1},
    )


@pytest.fixture
def response_record(request_record: RequestRecord) -> ResponseRecord:
    return ResponseRecord.objects.create(
        request_record=request_record,
        status_code=500,
        headers={"Content-Type": "application/json"},
        body={"error": "boom"},
        response_time_ms=143,
    )
