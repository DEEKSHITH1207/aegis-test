"""Execute a test run in the foreground, without a Celery worker.

    python manage.py execute_test_run 12

Useful when running Django on the host with no worker, and for watching a run
from a terminal. It performs exactly the work the worker would.
"""

from __future__ import annotations

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError

from apps.core.exceptions import DomainError
from apps.testing import services
from apps.testing.models import TestRun, TestRunStatus


class Command(BaseCommand):
    help = "Send a test run's requests to its base URL and record the results."

    def add_arguments(self, parser) -> None:
        parser.add_argument("test_run_id", type=int)

    def handle(self, *args, test_run_id: int, **options) -> None:
        test_run = TestRun.objects.filter(pk=test_run_id).first()
        if test_run is None:
            raise CommandError(f"Test run {test_run_id} does not exist.")

        try:
            if test_run.status == TestRunStatus.PENDING:
                test_run = services.start_execution(test_run=test_run, enqueue=False)
            summary = services.execute_test_run(test_run=test_run)
        except (DomainError, ValidationError) as exc:
            raise CommandError(str(exc)) from exc

        test_run.refresh_from_db()
        self.stdout.write(
            f"Run {test_run.pk} {test_run.status}: {summary.executed} sent, "
            f"{summary.passed} passed, {summary.failed} failed, {summary.errors} errors, "
            f"{summary.skipped} skipped, {summary.findings} findings."
        )
        if test_run.status_detail:
            self.stdout.write(test_run.status_detail)
