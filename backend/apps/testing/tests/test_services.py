"""Service-layer tests: the business rules, independent of HTTP."""

from __future__ import annotations

import pytest
from django.core.exceptions import ValidationError

from apps.testing import services
from apps.testing.models import (
    Endpoint,
    Finding,
    HTTPMethod,
    Specification,
    TestCase,
    TestRun,
    TestRunStatus,
)

pytestmark = pytest.mark.django_db


def test_create_specification_persists_and_validates() -> None:
    specification = services.create_specification(name="Billing API", version="3.1")

    assert Specification.objects.filter(pk=specification.pk).exists()


def test_create_specification_rejects_an_empty_name() -> None:
    with pytest.raises(ValidationError):
        services.create_specification(name="")


def test_create_test_run_starts_pending(specification: Specification) -> None:
    test_run = services.create_test_run(specification=specification, name="Smoke")

    assert test_run.status == TestRunStatus.PENDING
    assert test_run.name == "Smoke"
    assert test_run.started_at is None


def test_create_test_run_derives_a_name_when_none_is_given(
    specification: Specification,
) -> None:
    test_run = services.create_test_run(specification=specification)

    assert test_run.name.startswith("Petstore - ")


def test_run_lifecycle_stamps_its_timestamps(test_run: TestRun) -> None:
    services.start_test_run(test_run)

    assert test_run.status == TestRunStatus.RUNNING
    assert test_run.started_at is not None

    services.complete_test_run(test_run)

    test_run.refresh_from_db()
    assert test_run.status == TestRunStatus.COMPLETED
    assert test_run.completed_at is not None
    assert test_run.is_finished is True


def test_a_run_cannot_be_started_twice(test_run: TestRun) -> None:
    services.start_test_run(test_run)

    with pytest.raises(services.DomainError):
        services.start_test_run(test_run)


def test_a_pending_run_cannot_be_completed(test_run: TestRun) -> None:
    with pytest.raises(services.DomainError):
        services.complete_test_run(test_run)


def test_a_run_cannot_be_completed_into_a_non_terminal_status(test_run: TestRun) -> None:
    services.start_test_run(test_run)

    with pytest.raises(services.DomainError):
        services.complete_test_run(test_run, status=TestRunStatus.RUNNING)


def test_create_test_case_rejects_an_endpoint_from_another_specification(
    test_run: TestRun,
) -> None:
    other = services.create_specification(name="Other API")
    foreign_endpoint = services.create_endpoint(
        specification=other, path="/orders", method=HTTPMethod.GET
    )

    with pytest.raises(ValidationError):
        services.create_test_case(
            test_run=test_run,
            endpoint=foreign_endpoint,
            name="Should not be allowed",
            expected_status_code=200,
        )

    assert TestCase.objects.count() == 0


def test_create_test_case_accepts_an_endpoint_from_the_same_specification(
    test_run: TestRun, endpoint: Endpoint
) -> None:
    test_case = services.create_test_case(
        test_run=test_run, endpoint=endpoint, name="Happy path", expected_status_code=200
    )

    assert test_case.pk is not None


def test_list_test_runs_can_be_scoped_to_one_specification(
    specification: Specification, test_run: TestRun
) -> None:
    other = services.create_specification(name="Other API")
    services.create_test_run(specification=other, name="Elsewhere")

    scoped = services.list_test_runs(specification=specification)

    assert list(scoped) == [test_run]
    assert services.list_test_runs().count() == 2


def test_list_findings_collects_across_every_case_in_the_run(
    test_run: TestRun, endpoint: Endpoint, test_case: TestCase
) -> None:
    second_case = services.create_test_case(
        test_run=test_run, endpoint=endpoint, name="Second", expected_status_code=404
    )
    Finding.objects.create(test_case=test_case, title="First problem")
    Finding.objects.create(test_case=second_case, title="Second problem")
    # A finding on another run must not leak in.
    other_run = services.create_test_run(specification=test_run.specification, name="Other run")
    other_case = services.create_test_case(
        test_run=other_run, endpoint=endpoint, name="Other", expected_status_code=200
    )
    Finding.objects.create(test_case=other_case, title="Unrelated problem")

    titles = set(services.list_findings(test_run=test_run).values_list("title", flat=True))

    assert titles == {"First problem", "Second problem"}
