"""Celery tasks for the testing domain.

Tasks are thin in the same way views are: they load their inputs and call a
service. Everything a task does can also be done synchronously by calling the
service directly, which is what the management command and the tests do.
"""

from __future__ import annotations

from celery import shared_task

from apps.testing import services
from apps.testing.models import RulesRun, TestRun


@shared_task(name="testing.execute_test_run", ignore_result=True)
def execute_test_run_task(test_run_id: int) -> None:
    # The run may have been deleted between queueing and pickup; there is
    # nothing left to execute then.
    test_run = TestRun.objects.filter(pk=test_run_id).first()
    if test_run is None:
        return
    services.execute_test_run(test_run=test_run)


@shared_task(name="testing.run_rules", ignore_result=True)
def run_rules_task(rules_run_id: int, setup_values: dict[str, str]) -> None:
    # ignore_result: the setup values are arguments, and must not be kept in
    # the result backend after the run.
    rules_run = RulesRun.objects.filter(pk=rules_run_id).first()
    if rules_run is None:
        return
    services.execute_rules_run(rules_run=rules_run, setup_values=setup_values)
