"""Business-rules runs end to end: service, findings, REST API, task, command.

The API under test is FakeLeaveApi from rules_fixtures. The headline test is
the first one: the fake computes the leave balance wrongly, and the run must
report it as a business-rule violation with the expected value, how it was
worked out, the actual value, and the chain of requests that produced it.
"""

from __future__ import annotations

import json
from io import StringIO

import pytest
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.db import IntegrityError, transaction
from rest_framework import status

from apps.testing import executor, services
from apps.testing.models import (
    Finding,
    FindingCategory,
    FindingSeverity,
    RuleExchange,
    RulesRun,
    ScenarioResult,
    ScenarioStatus,
    Specification,
    TestRunStatus,
)
from apps.testing.rules.runner import resolve_today
from apps.testing.tasks import run_rules_task
from apps.testing.tests.rules_fixtures import (
    LEAVE_RULES,
    PASSWORD,
    SETUP,
    TODAY,
    TOKEN,
    FakeLeaveApi,
)

pytestmark = pytest.mark.django_db

BASE_URL = "http://leave.test/api"


@pytest.fixture
def leave_spec(db) -> Specification:
    return Specification.objects.create(name="Leave API", version="1.0.0")


def _run(spec: Specification, api: FakeLeaveApi, rules: str = LEAVE_RULES, **overrides):
    arguments = {
        "specification": spec,
        "rules_text": rules,
        "base_url": BASE_URL,
        "setup_values": SETUP,
        "today": TODAY,
        "enqueue": False,
    }
    arguments.update(overrides)
    setup = arguments["setup_values"]
    rules_run = services.submit_rules_run(**arguments)
    summary = services.execute_rules_run(rules_run=rules_run, setup_values=setup, client=api.client())
    rules_run.refresh_from_db()
    return rules_run, summary


# --- End to end: a wrong total is reported ------------------------------------


def test_a_wrongly_calculated_total_is_reported_as_a_business_rule_violation(leave_spec) -> None:
    rules_run, summary = _run(leave_spec, FakeLeaveApi(retroactive_rate=True))

    assert rules_run.status == TestRunStatus.COMPLETED
    assert (summary.checks, summary.broken, summary.findings) == (3, 2, 2)

    first = services.list_rules_findings(rules_run=rules_run).first()
    assert first.category == FindingCategory.BUSINESS_RULE_VIOLATION
    assert first.severity == FindingSeverity.HIGH
    assert first.title == "balance_is_accrual_minus_taken: expected 47, got 55"

    evidence = first.evidence
    assert evidence["why"].startswith("The balance must equal")
    assert (evidence["expected"], evidence["actual"]) == (47, 55)
    assert evidence["expected_working"].startswith("4 days × 2 + 11 days × 4 = 52")
    assert evidence["after_step"] == "approve"
    assert [entry["step"] for entry in evidence["chain"]] == [
        "step apply",
        "step approve",
        "fetch balance_is_accrual_minus_taken",
    ]
    assert evidence["chain"][-1]["response_body"] == {"balance": 55}
    # Each chain entry points at the stored exchange it summarises.
    stored = RuleExchange.objects.get(pk=evidence["chain"][-1]["exchange_id"])
    assert stored.url == f"{BASE_URL}/employees/42/balance"

    (scenario,) = ScenarioResult.objects.filter(rules_run=rules_run)
    assert scenario.status == ScenarioStatus.FAILED
    assert [c["outcome"] for c in scenario.checks] == ["passed", "broken", "broken"]


def test_a_correct_api_produces_no_findings(leave_spec) -> None:
    rules_run, summary = _run(leave_spec, FakeLeaveApi())

    assert summary.broken == 0 and summary.findings == 0
    assert services.summarise_rules_run(rules_run) == {
        "scenarios": 1,
        "passed": 1,
        "failed": 0,
        "error": 0,
        "stopped": 0,
        "rules_checked": 3,
        "rules_passed": 3,
        "rules_broken": 0,
        "rules_unchecked": 0,
        "findings": 0,
    }


def test_every_exchange_is_stored_in_order(leave_spec) -> None:
    rules_run, _ = _run(leave_spec, FakeLeaveApi())

    rows = list(services.list_rule_exchanges(rules_run=rules_run))
    assert [(r.position, r.phase, r.name) for r in rows] == [
        (1, "auth", "login"),
        (2, "step", "apply"),
        (3, "step", "approve"),
        (4, "fetch", "balance_is_accrual_minus_taken"),
        (5, "step", "raise_rate"),
        (6, "fetch", "balance_is_accrual_minus_taken"),
        (7, "cleanup", "delete_leave"),
    ]
    assert rows[0].scenario_result is None


# --- Nothing secret is stored -------------------------------------------------


def test_no_secret_reaches_the_database(leave_spec) -> None:
    rules_run, _ = _run(leave_spec, FakeLeaveApi(retroactive_rate=True))

    stored = json.dumps(
        [
            list(RulesRun.objects.values()),
            list(ScenarioResult.objects.values()),
            list(RuleExchange.objects.values()),
            list(Finding.objects.values()),
        ],
        default=str,
    )
    assert PASSWORD not in stored
    assert TOKEN not in stored
    login = RuleExchange.objects.get(rules_run=rules_run, phase="auth")
    assert login.request_body["password"] == executor.REDACTED
    assert login.response_body["access_token"] == executor.REDACTED
    # A non-secret value stays visible where the request used it.
    assert RuleExchange.objects.filter(url__contains="/employees/42/").exists()


# --- Stopped scenarios --------------------------------------------------------


def test_an_unexpected_status_is_a_finding(leave_spec) -> None:
    rules_run, summary = _run(leave_spec, FakeLeaveApi(approve_status=409))

    assert summary.stopped == 1
    finding = Finding.objects.get(scenario_result__rules_run=rules_run)
    assert finding.category == FindingCategory.INCONSISTENCY
    assert finding.severity == FindingSeverity.MEDIUM
    assert finding.title == (
        "Scenario 'leave_balance' stopped at step 'approve': expected 200, got 409"
    )
    assert [entry["step"] for entry in finding.evidence["chain"]] == ["step apply", "step approve"]
    assert ScenarioResult.objects.get(rules_run=rules_run).status == ScenarioStatus.STOPPED


def test_a_server_error_that_stops_a_scenario_is_a_bug(leave_spec) -> None:
    rules_run, _ = _run(leave_spec, FakeLeaveApi(approve_status=500))

    finding = Finding.objects.get(scenario_result__rules_run=rules_run)
    assert (finding.category, finding.severity) == (FindingCategory.BUG, FindingSeverity.HIGH)


def test_a_failed_login_fails_the_run(leave_spec) -> None:
    rules_run, summary = _run(leave_spec, FakeLeaveApi(), setup_values={**SETUP, "password": "no"})

    assert rules_run.status == TestRunStatus.FAILED
    assert "Login failed" in rules_run.status_detail
    assert summary.scenarios == 0
    assert RuleExchange.objects.filter(rules_run=rules_run, phase="auth").count() == 1


# --- Refused before anything is written ---------------------------------------


def test_an_invalid_rules_file_is_refused_with_every_problem(leave_spec) -> None:
    with pytest.raises(ValidationError) as excinfo:
        services.submit_rules_run(
            specification=leave_spec,
            rules_text="version: 2\nscenarios: []",
            base_url=BASE_URL,
            setup_values={},
        )

    assert len(excinfo.value.message_dict["rules"]) == 2
    assert not RulesRun.objects.exists()


def test_missing_setup_values_are_refused(leave_spec) -> None:
    with pytest.raises(ValidationError) as excinfo:
        services.submit_rules_run(
            specification=leave_spec,
            rules_text=LEAVE_RULES,
            base_url=BASE_URL,
            setup_values={"employee_id": "42"},
        )

    assert "password (env AEGIS_PASSWORD)" in excinfo.value.message_dict["setup"][0]
    assert not RulesRun.objects.exists()


def test_steps_outside_the_specification_are_warned_about(leave_spec) -> None:
    leave_spec.endpoints.create(path="/employees/{employeeId}/leaves", method="POST")

    rules_run, _ = _run(leave_spec, FakeLeaveApi())

    # Placeholder names may differ; only the shape of the path matters.
    assert not any("step 'apply'" in warning for warning in rules_run.warnings)
    assert any("step 'approve'" in warning for warning in rules_run.warnings)


# --- Findings from either source ----------------------------------------------


def test_a_finding_needs_exactly_one_source(leave_spec) -> None:
    with pytest.raises(IntegrityError), transaction.atomic():
        Finding.objects.create(title="orphan")


# --- REST API -----------------------------------------------------------------


def _post(api_client, spec, **overrides):
    payload = {
        "specification": spec.pk,
        "base_url": BASE_URL,
        "rules": LEAVE_RULES,
        "setup": SETUP,
        "today": "2026-09-27",
    }
    payload.update(overrides)
    return api_client.post("/api/v1/rules-runs/", payload, format="json")


def test_post_queues_the_run_with_its_setup_values(
    api_client, leave_spec, monkeypatch, django_capture_on_commit_callbacks
) -> None:
    queued: list = []
    monkeypatch.setattr(run_rules_task, "delay", lambda *args: queued.append(args))

    with django_capture_on_commit_callbacks(execute=True):
        response = _post(api_client, leave_spec)

    assert response.status_code == status.HTTP_202_ACCEPTED
    body = response.json()
    assert body["status"] == TestRunStatus.RUNNING
    assert body["today"] == "2026-09-27" and body["today_overridden"] is True
    assert "setup" not in body
    assert PASSWORD not in json.dumps(body)
    assert queued == [(body["id"], SETUP)]


def test_post_with_an_invalid_file_is_a_400_listing_every_problem(api_client, leave_spec) -> None:
    response = _post(api_client, leave_spec, rules="version: 1\nscenarios: nope")

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert response.json()["rules"] == ["scenarios: must be a non-empty list."]


def test_post_without_setup_values_is_a_400(api_client, leave_spec) -> None:
    response = _post(api_client, leave_spec, setup={})

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert "Missing setup values" in response.json()["setup"][0]


def test_a_finished_run_can_be_read_back(api_client, leave_spec) -> None:
    rules_run, _ = _run(leave_spec, FakeLeaveApi(retroactive_rate=True))
    base = f"/api/v1/rules-runs/{rules_run.pk}"

    detail = api_client.get(f"{base}/").json()
    assert detail["status"] == TestRunStatus.COMPLETED
    assert detail["results"]["rules_broken"] == 2 and detail["results"]["findings"] == 2

    scenarios = api_client.get(f"{base}/scenarios/").json()["results"]
    assert scenarios[0]["checks"][1]["detail"] == "expected 47, got 55"

    exchanges = api_client.get(f"{base}/exchanges/").json()
    assert exchanges["count"] == 7

    findings = api_client.get(f"{base}/findings/").json()["results"]
    assert {f["category"] for f in findings} == {"business_rule_violation"}
    assert all(f["scenario_result"] == scenarios[0]["id"] for f in findings)

    listing = api_client.get(f"/api/v1/rules-runs/?specification={leave_spec.pk}").json()
    assert listing["count"] == 1


def test_findings_are_listed_most_severe_first(api_client, leave_spec) -> None:
    rules = LEAVE_RULES.replace("severity: medium", "severity: critical")
    rules_run, _ = _run(leave_spec, FakeLeaveApi(exclusive_days=True), rules)

    findings = api_client.get(f"/api/v1/rules-runs/{rules_run.pk}/findings/").json()["results"]

    assert [f["severity"] for f in findings] == ["critical", "high", "high"]


# --- Worker and command -------------------------------------------------------


def test_the_task_runs_a_started_run(leave_spec, monkeypatch) -> None:
    api = FakeLeaveApi(retroactive_rate=True)
    monkeypatch.setattr(executor, "make_client", lambda **kwargs: api.client())
    rules_run = services.submit_rules_run(
        specification=leave_spec, rules_text=LEAVE_RULES, base_url=BASE_URL,
        setup_values=SETUP, today=TODAY, enqueue=False,
    )

    run_rules_task(rules_run.pk, SETUP)

    rules_run.refresh_from_db()
    assert rules_run.status == TestRunStatus.COMPLETED
    assert services.summarise_rules_run(rules_run)["findings"] == 2


def test_the_command_reads_setup_from_an_env_file_and_reports(
    leave_spec, monkeypatch, tmp_path
) -> None:
    api = FakeLeaveApi(retroactive_rate=True)
    monkeypatch.setattr(executor, "make_client", lambda **kwargs: api.client())
    rules_file = tmp_path / "rules.yaml"
    rules_file.write_text(LEAVE_RULES)
    env_file = tmp_path / ".env.rules"
    env_file.write_text(
        f"AEGIS_EMPLOYEE_ID=42\nAEGIS_USERNAME=asha\nAEGIS_PASSWORD={PASSWORD}\n"
    )
    out = StringIO()

    with pytest.raises(SystemExit) as excinfo:
        call_command(
            "run_rules",
            "--specification", str(leave_spec.pk),
            "--rules", str(rules_file),
            "--base-url", BASE_URL,
            "--env-file", str(env_file),
            "--today", "2026-09-27",
            stdout=out,
        )

    assert excinfo.value.code == 1
    report = out.getvalue()
    assert "does not move the server's clock" in report
    assert "BROKEN [high] balance_is_accrual_minus_taken after approve" in report
    assert "expected 47, got 55" in report
    assert "expected worked out as: 4 days × 2 + 11 days × 4 = 52" in report
    assert "3 rule checks: 1 passed, 2 broken, 0 unchecked. 2 findings." in report
    assert PASSWORD not in report


def test_the_command_exits_cleanly_when_every_rule_holds(leave_spec, monkeypatch, tmp_path) -> None:
    # No --today: the run uses the real date in the file's time zone, so the
    # fake API must keep the same calendar.
    api = FakeLeaveApi(today=resolve_today("Asia/Kolkata"))
    monkeypatch.setattr(executor, "make_client", lambda **kwargs: api.client())
    for name, value in (("AEGIS_EMPLOYEE_ID", "42"), ("AEGIS_USERNAME", "asha"), ("AEGIS_PASSWORD", PASSWORD)):
        monkeypatch.setenv(name, value)
    rules_file = tmp_path / "rules.yaml"
    rules_file.write_text(LEAVE_RULES)
    out = StringIO()

    call_command(
        "run_rules", "--specification", str(leave_spec.pk), "--rules", str(rules_file),
        "--base-url", BASE_URL, stdout=out,
    )

    assert "3 rule checks: 3 passed" in out.getvalue()
    assert "does not move the server's clock" not in out.getvalue()


def test_the_command_lists_every_problem_in_a_bad_file(leave_spec, tmp_path) -> None:
    rules_file = tmp_path / "rules.yaml"
    rules_file.write_text("version: 2\nscenarios: []")

    with pytest.raises(Exception) as excinfo:
        call_command(
            "run_rules", "--specification", str(leave_spec.pk), "--rules", str(rules_file),
            "--base-url", BASE_URL,
        )

    assert "version: must be 1" in str(excinfo.value)
    assert "scenarios: must be a non-empty list" in str(excinfo.value)
