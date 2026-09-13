"""Model-level tests: persistence, relationships, constraints and validation."""

from __future__ import annotations

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from apps.testing.models import (
    Endpoint,
    Finding,
    FindingCategory,
    FindingSeverity,
    FindingStatus,
    HTTPMethod,
    RequestRecord,
    ResponseRecord,
    Specification,
    SpecificationSourceType,
    TestCase,
    TestCaseStatus,
    TestCaseType,
    TestRun,
    TestRunStatus,
)

pytestmark = pytest.mark.django_db


# --- Creation -----------------------------------------------------------------


def test_specification_is_created_with_sensible_defaults(specification: Specification) -> None:
    specification.refresh_from_db()

    assert specification.pk is not None
    assert specification.name == "Petstore"
    assert specification.source_type == SpecificationSourceType.OPENAPI
    assert specification.raw_specification == {"openapi": "3.0.0", "paths": {}}
    assert specification.created_at is not None
    assert specification.updated_at is not None


def test_specification_name_is_required() -> None:
    with pytest.raises(ValidationError) as excinfo:
        Specification(name="").full_clean()

    assert "name" in excinfo.value.message_dict


def test_endpoint_is_created_and_stores_json_structures(endpoint: Endpoint) -> None:
    endpoint.refresh_from_db()

    assert endpoint.method == HTTPMethod.GET
    assert endpoint.path == "/pets/{petId}"
    assert endpoint.parameters == [{"name": "petId", "in": "path", "required": True}]
    # A GET has no request body at all, which is not the same as an empty one.
    assert endpoint.request_schema is None


def test_test_run_is_created_pending_and_unfinished(test_run: TestRun) -> None:
    test_run.refresh_from_db()

    assert test_run.status == TestRunStatus.PENDING
    assert test_run.started_at is None
    assert test_run.completed_at is None
    assert test_run.is_finished is False


def test_test_case_is_created_pending(test_case: TestCase) -> None:
    test_case.refresh_from_db()

    assert test_case.status == TestCaseStatus.PENDING
    assert test_case.test_type == TestCaseType.HAPPY_PATH
    assert test_case.expected_status_code == 200
    assert test_case.input_data == {"path_params": {"petId": 1}}


def test_request_and_response_records_are_created_as_a_pair(
    request_record: RequestRecord, response_record: ResponseRecord
) -> None:
    request_record.refresh_from_db()

    assert request_record.body is None
    assert request_record.path_params == {"petId": 1}
    # The reverse accessor of the one-to-one is what the executor will read.
    assert request_record.response == response_record
    assert response_record.status_code == 500
    assert response_record.response_time_ms == 143
    # Evidence rows are immutable, so they carry no modification timestamp.
    assert not hasattr(response_record, "updated_at")


def test_finding_is_created_with_its_evidence(
    test_case: TestCase, response_record: ResponseRecord
) -> None:
    finding = Finding.objects.create(
        test_case=test_case,
        response_record=response_record,
        title="Unhandled server error on a valid request",
        description="A documented 200 request returned 500.",
        severity=FindingSeverity.HIGH,
        category=FindingCategory.BUG,
        evidence={"expected_status_code": 200, "actual_status_code": 500},
    )

    finding.refresh_from_db()
    assert finding.status == FindingStatus.OPEN
    assert finding.evidence["actual_status_code"] == 500
    assert finding.severity == FindingSeverity.HIGH


def test_finding_may_exist_without_a_response(test_case: TestCase) -> None:
    # A request that never came back still produces a finding.
    finding = Finding.objects.create(
        test_case=test_case,
        title="Endpoint did not respond",
        severity=FindingSeverity.CRITICAL,
        category=FindingCategory.PERFORMANCE,
    )

    assert finding.response_record is None


# --- Relationship integrity ---------------------------------------------------


def test_related_names_traverse_the_whole_graph(
    specification: Specification,
    endpoint: Endpoint,
    test_run: TestRun,
    test_case: TestCase,
    request_record: RequestRecord,
    response_record: ResponseRecord,
) -> None:
    finding = Finding.objects.create(
        test_case=test_case, response_record=response_record, title="Something is off"
    )

    assert list(specification.endpoints.all()) == [endpoint]
    assert list(specification.test_runs.all()) == [test_run]
    assert list(test_run.test_cases.all()) == [test_case]
    assert list(endpoint.test_cases.all()) == [test_case]
    assert list(test_case.request_records.all()) == [request_record]
    assert request_record.response == response_record
    assert list(test_case.findings.all()) == [finding]
    assert list(response_record.findings.all()) == [finding]


def test_deleting_a_specification_removes_its_entire_tree(
    specification: Specification, test_case: TestCase, response_record: ResponseRecord
) -> None:
    Finding.objects.create(test_case=test_case, title="Doomed")

    specification.delete()

    assert Endpoint.objects.count() == 0
    assert TestRun.objects.count() == 0
    assert TestCase.objects.count() == 0
    assert RequestRecord.objects.count() == 0
    assert ResponseRecord.objects.count() == 0
    assert Finding.objects.count() == 0


def test_deleting_a_response_record_keeps_the_finding(
    test_case: TestCase, response_record: ResponseRecord
) -> None:
    # Pruning evidence must not erase the conclusion drawn from it.
    finding = Finding.objects.create(test_case=test_case, response_record=response_record)

    response_record.delete()

    finding.refresh_from_db()
    assert finding.response_record is None


# --- Constraints and validation -----------------------------------------------


def test_specification_name_and_version_are_unique_together(
    specification: Specification,
) -> None:
    with pytest.raises(IntegrityError), transaction.atomic():
        Specification.objects.create(name="Petstore", version="1.0.0")


def test_the_same_name_at_a_different_version_is_allowed(
    specification: Specification,
) -> None:
    other = Specification.objects.create(name="Petstore", version="2.0.0")

    assert other.pk != specification.pk


def test_a_path_and_method_pair_is_unique_within_a_specification(
    specification: Specification, endpoint: Endpoint
) -> None:
    with pytest.raises(IntegrityError), transaction.atomic():
        Endpoint.objects.create(
            specification=specification, path="/pets/{petId}", method=HTTPMethod.GET
        )


def test_the_same_path_may_be_reused_with_another_method(
    specification: Specification, endpoint: Endpoint
) -> None:
    other = Endpoint.objects.create(
        specification=specification, path="/pets/{petId}", method=HTTPMethod.DELETE
    )

    assert other.pk != endpoint.pk


def test_endpoint_path_is_required(specification: Specification) -> None:
    with pytest.raises(ValidationError) as excinfo:
        Endpoint(specification=specification, path="", method=HTTPMethod.GET).full_clean()

    assert "path" in excinfo.value.message_dict


@pytest.mark.parametrize("method", ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"])
def test_every_documented_http_method_is_accepted(
    specification: Specification, method: str
) -> None:
    endpoint = Endpoint(specification=specification, path="/pets", method=method)

    endpoint.full_clean()  # must not raise


@pytest.mark.parametrize("method", ["TRACE", "CONNECT", "get", "FETCH", ""])
def test_unsupported_http_methods_are_rejected(
    specification: Specification, method: str
) -> None:
    with pytest.raises(ValidationError) as excinfo:
        Endpoint(specification=specification, path="/pets", method=method).full_clean()

    assert "method" in excinfo.value.message_dict


def test_test_case_endpoint_must_belong_to_the_runs_specification(test_run: TestRun) -> None:
    other_specification = Specification.objects.create(name="Other API", version="1.0.0")
    foreign_endpoint = Endpoint.objects.create(
        specification=other_specification, path="/orders", method=HTTPMethod.GET
    )

    with pytest.raises(ValidationError) as excinfo:
        TestCase(
            test_run=test_run,
            endpoint=foreign_endpoint,
            name="Cross-specification case",
            expected_status_code=200,
        ).full_clean()

    assert "endpoint" in excinfo.value.message_dict


@pytest.mark.parametrize("code", [99, 600, 0, 1000])
def test_expected_status_code_outside_the_http_range_is_rejected(
    test_run: TestRun, endpoint: Endpoint, code: int
) -> None:
    with pytest.raises(ValidationError) as excinfo:
        TestCase(
            test_run=test_run, endpoint=endpoint, name="Bad code", expected_status_code=code
        ).full_clean()

    assert "expected_status_code" in excinfo.value.message_dict


@pytest.mark.parametrize("code", [100, 200, 404, 500, 599])
def test_valid_http_status_codes_are_accepted(
    test_run: TestRun, endpoint: Endpoint, code: int
) -> None:
    TestCase(
        test_run=test_run, endpoint=endpoint, name="Good code", expected_status_code=code
    ).full_clean()  # must not raise


def test_the_database_rejects_an_out_of_range_status_code(
    test_run: TestRun, endpoint: Endpoint
) -> None:
    # full_clean is skippable; the check constraint is not.
    with pytest.raises(IntegrityError), transaction.atomic():
        TestCase.objects.create(
            test_run=test_run, endpoint=endpoint, name="Bypass", expected_status_code=900
        )


def test_a_run_cannot_complete_without_having_started(test_run: TestRun) -> None:
    from django.utils import timezone

    test_run.completed_at = timezone.now()

    with pytest.raises(IntegrityError), transaction.atomic():
        test_run.save()


def test_a_finding_cannot_cite_another_test_cases_response(
    test_run: TestRun, endpoint: Endpoint, response_record: ResponseRecord
) -> None:
    unrelated_case = TestCase.objects.create(
        test_run=test_run, endpoint=endpoint, name="Unrelated", expected_status_code=404
    )

    with pytest.raises(ValidationError) as excinfo:
        Finding(
            test_case=unrelated_case, response_record=response_record, title="Mismatched evidence"
        ).full_clean()

    assert "response_record" in excinfo.value.message_dict


def test_a_request_record_has_at_most_one_response(
    request_record: RequestRecord, response_record: ResponseRecord
) -> None:
    with pytest.raises(IntegrityError), transaction.atomic():
        ResponseRecord.objects.create(
            request_record=request_record, status_code=200, response_time_ms=10
        )
