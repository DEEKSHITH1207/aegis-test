"""Analyse a finished run's findings in the foreground, without a worker.

    python manage.py analyse_run --test-run 12
    python manage.py analyse_run --rules-run 3

Sends the run's findings to the language model configured by LLM_MODEL and
prints what it concluded. It performs exactly the work the worker would.
"""

from __future__ import annotations

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError

from apps.core.exceptions import DomainError
from apps.testing import services
from apps.testing.models import RulesRun, TestRun


class Command(BaseCommand):
    help = "Ask the language model for a second opinion on a finished run's findings."

    def add_arguments(self, parser) -> None:
        source = parser.add_mutually_exclusive_group(required=True)
        source.add_argument("--test-run", type=int, dest="test_run_id")
        source.add_argument("--rules-run", type=int, dest="rules_run_id")

    def handle(self, *args, test_run_id: int | None, rules_run_id: int | None, **options) -> None:
        if test_run_id is not None:
            run = TestRun.objects.filter(pk=test_run_id).first()
            if run is None:
                raise CommandError(f"Test run {test_run_id} does not exist.")
            source = {"test_run": run}
        else:
            run = RulesRun.objects.filter(pk=rules_run_id).first()
            if run is None:
                raise CommandError(f"Rules run {rules_run_id} does not exist.")
            source = {"rules_run": run}

        try:
            analysis = services.start_analysis(**source, enqueue=False)
            summary = services.execute_analysis(analysis=analysis)
        except (DomainError, ValidationError) as exc:
            raise CommandError(str(exc)) from exc

        analysis.refresh_from_db()
        results = services.summarise_analysis(analysis)
        self.stdout.write(
            f"Analysis {analysis.pk} {analysis.status}: {results['assessed']} of "
            f"{summary.findings} findings assessed in {summary.requests} requests — "
            f"{results['likely_real']} likely real, {results['likely_false_positive']} "
            f"likely false positives, {results['uncertain']} uncertain."
        )
        if analysis.status_detail:
            self.stdout.write(analysis.status_detail)
        if analysis.summary:
            self.stdout.write("")
            self.stdout.write(analysis.summary)
        for group in analysis.groups:
            ids = ", ".join(str(i) for i in group["finding_ids"])
            self.stdout.write("")
            self.stdout.write(f"[{group['severity']}] {group['title']} (findings {ids})")
            self.stdout.write(f"  {group['explanation']}")
            self.stdout.write(f"  Next: {group['recommendation']}")
        for warning in analysis.warnings:
            self.stdout.write(f"Warning: {warning}")
