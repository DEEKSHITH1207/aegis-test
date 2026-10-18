"""Run a business-rules file against an API, in the foreground.

    python manage.py run_rules --specification 1 --rules rules.yaml \\
        --base-url http://localhost:3000 --env-file .env.rules [--today 2026-09-27]

Setup values are read from the environment variables the rules file names
(AEGIS_<NAME> unless it says otherwise); --env-file adds a dotenv file whose
values win over the environment. They are passed to the run and never stored.

Exits with status 1 when a rule is broken or a scenario stopped, so the
command can gate a CI pipeline.
"""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from dotenv import dotenv_values

from apps.core.exceptions import DomainError
from apps.testing import services
from apps.testing.models import Specification
from apps.testing.rules import document

CLOCK_NOTE = (
    "Note: --today sets the dates AegisTest puts in its requests. It does not move "
    "the server's clock: anything the API works out from its own date still uses "
    "its real today."
)


class Command(BaseCommand):
    help = "Run a business-rules file's scenarios against an API and report broken rules."

    def add_arguments(self, parser) -> None:
        parser.add_argument("--specification", type=int, required=True, help="Specification id.")
        parser.add_argument("--rules", type=Path, required=True, help="Rules file (YAML).")
        parser.add_argument("--base-url", required=True, help="Root URL of the API under test.")
        parser.add_argument("--env-file", type=Path, help="Dotenv file with setup values.")
        parser.add_argument(
            "--today",
            type=date.fromisoformat,
            help="Use this date (YYYY-MM-DD) as today in the requests.",
        )
        parser.add_argument("--name", default="", help="A name for the run.")

    def handle(self, *args, **options) -> None:
        specification = Specification.objects.filter(pk=options["specification"]).first()
        if specification is None:
            raise CommandError(f"Specification {options['specification']} does not exist.")
        try:
            rules_text = options["rules"].read_text(encoding="utf-8")
        except OSError as exc:
            raise CommandError(f"Cannot read the rules file: {exc}") from exc

        try:
            rules = document.load(rules_text)
        except document.RulesFileError as exc:
            raise CommandError(
                "The rules file has problems:\n" + "\n".join(f"  - {e}" for e in exc.errors)
            ) from exc
        setup_values = self._setup_values(rules, options["env_file"])

        try:
            rules_run = services.submit_rules_run(
                specification=specification,
                rules_text=rules_text,
                base_url=options["base_url"],
                setup_values=setup_values,
                name=options["name"],
                today=options["today"],
                enqueue=False,
            )
            summary = services.execute_rules_run(rules_run=rules_run, setup_values=setup_values)
        except ValidationError as exc:
            raise CommandError("; ".join(_messages(exc))) from exc
        except DomainError as exc:
            raise CommandError(str(exc)) from exc

        rules_run.refresh_from_db()
        self._report(rules_run, rules.timezone)
        if summary.broken or summary.stopped or rules_run.status != "completed":
            raise SystemExit(1)

    def _setup_values(self, rules: document.RulesDocument, env_file: Path | None) -> dict[str, str]:
        environment = dict(os.environ)
        if env_file is not None:
            if not env_file.is_file():
                raise CommandError(f"The env file {env_file} does not exist.")
            environment.update({k: v for k, v in dotenv_values(env_file).items() if v is not None})
        return {
            value.name: environment[value.env]
            for value in rules.setup
            if environment.get(value.env)
        }

    def _report(self, rules_run, timezone: str) -> None:
        write = self.stdout.write
        write(
            f"Rules run {rules_run.pk} {rules_run.status} against {rules_run.base_url} "
            f"(today = {rules_run.today}, {timezone})"
        )
        if rules_run.today_overridden:
            write(CLOCK_NOTE)
        if rules_run.status_detail:
            write(rules_run.status_detail)
        for warning in rules_run.warnings:
            write(f"Warning: {warning}")

        for scenario in services.list_scenario_results(rules_run=rules_run):
            write("")
            write(f"Scenario {scenario.name}: {scenario.status.upper()} ({scenario.steps_run} steps run)")
            if scenario.detail:
                write(f"  stopped: {scenario.detail}")
            for check in scenario.checks:
                label = {"passed": "passed", "broken": "BROKEN", "unchecked": "UNCHECKED"}[
                    check["outcome"]
                ]
                write(f"  {label} [{check['severity']}] {check['rule']} after {check['after_step']}")
                if check["outcome"] != "passed":
                    write(f"    {check['detail']}")
                    if check["expected_working"]:
                        write(f"    expected worked out as: {check['expected_working']}")
            for problem in scenario.cleanup_problems:
                write(f"  cleanup: {problem}")

        results = services.summarise_rules_run(rules_run)
        write("")
        write(
            f"{results['rules_checked']} rule checks: {results['rules_passed']} passed, "
            f"{results['rules_broken']} broken, {results['rules_unchecked']} unchecked. "
            f"{results['findings']} findings."
        )


def _messages(exc: ValidationError) -> list[str]:
    if hasattr(exc, "message_dict"):
        return [
            f"{field}: {message}"
            for field, messages in exc.message_dict.items()
            for message in messages
        ]
    return exc.messages
