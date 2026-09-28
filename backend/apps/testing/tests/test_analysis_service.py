"""LLM analysis end to end: the service, the API, the task, the command, the client.

No request reaches a model. ``FakeAnalyst`` stands in for ``Analyst``: it reads
the finding IDs out of the prompt it is given and answers the way a
well-behaved model would, unless a test tells it to misbehave. The client
tests drive the real ``Analyst`` against a stand-in for the SDK client.
"""

from __future__ import annotations

import json
import re
from copy import deepcopy
from io import StringIO
from types import SimpleNamespace

import anthropic
import httpx2
import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from rest_framework import status

from apps.core.exceptions import DomainError
from apps.testing import services
from apps.testing.analysis import client as analysis_client
from apps.testing.analysis.client import Analyst, AnalystError, Reply
from apps.testing.models import (
    Analysis,
    Finding,
    FindingAssessment,
    RulesRun,
    ScenarioResult,
    ScenarioStatus,
    Specification,
    TestRun,
    TestRunStatus,
)
from apps.testing.tasks import run_analysis_task
from apps.testing.tests.fixtures import REALISTIC_SPECIFICATION
from apps.testing.tests.test_execution_service import BASE_URL, _client, directory_api

pytestmark = pytest.mark.django_db


class FakeAnalyst:
    """Answers like a model would. ``tweak`` edits a reply before it is sent;
    ``fail_on`` raises AnalystError on that request (1-based)."""

    def __init__(self, tweak=None, fail_on: int | None = None) -> None:
        self.prompts: list[str] = []
        self.schemas: list[dict] = []
        self.tweak = tweak
        self.fail_on = fail_on

    def ask(self, *, prompt: str, schema: dict) -> Reply:
        self.prompts.append(prompt)
        self.schemas.append(schema)
        if self.fail_on == len(self.prompts):
            raise AnalystError("The model provider answered 529: Overloaded")

        ids = _ids(prompt)
        data: dict = {}
        if "assessments" in schema["properties"]:
            data["assessments"] = [
                {
                    "finding_id": finding_id,
                    "verdict": "likely_real",
                    "confidence": "high",
                    "suggested_severity": "high",
                    "reasoning": f"Finding {finding_id}: the status code shows it.",
                    "likely_cause": "Missing validation.",
                    "recommendation": "Validate the body.",
                }
                for finding_id in ids
            ]
        if "summary" in schema["properties"]:
            data["summary"] = "POST /users validates almost nothing."
            data["groups"] = [
                {
                    "title": "POST /users body validation",
                    "finding_ids": ids,
                    "explanation": "Only presence is checked.",
                    "recommendation": "Validate against the schema.",
                    "severity": "high",
                }
            ]
        if self.tweak:
            data = self.tweak(data)
        return Reply(
            text=json.dumps(data),
            model="claude-opus-5",
            request_id=f"req_{len(self.prompts)}",
            usage={"input_tokens": 1000, "output_tokens": 200},
        )


def _ids(prompt: str) -> list[int]:
    return [int(i) for i in re.findall(r'"finding_id": (\d+)', prompt)]


@pytest.fixture(autouse=True)
def llm_enabled(settings):
    settings.LLM_ANALYSIS_ENABLED = True
    # Large enough for the whole Directory run; batching tests lower it.
    settings.LLM_FINDINGS_PER_REQUEST = 100
    settings.LLM_MAX_FINDINGS = 200
    return settings


@pytest.fixture
def finished_run(db) -> TestRun:
    """A Directory API run that has executed and found real problems."""
    specification = Specification.objects.create(
        name="Directory API", version="1.0.0", raw_specification=deepcopy(REALISTIC_SPECIFICATION)
    )
    services.import_endpoints(specification=specification)
    run = services.create_test_run(
        specification=specification,
        base_url=BASE_URL,
        headers={"Authorization": "Bearer secret"},
    )
    services.generate_test_cases_for_run(test_run=run)
    services.start_execution(test_run=run, enqueue=False)
    run.refresh_from_db()
    services.execute_test_run(test_run=run, client=_client(directory_api))
    run.refresh_from_db()
    assert run.status == TestRunStatus.COMPLETED
    return run


def _findings(run: TestRun) -> list[Finding]:
    return list(services.list_findings(test_run=run))


def _analyse(run=None, analyst=None, **source) -> Analysis:
    source = source or {"test_run": run}
    analysis = services.start_analysis(**source, enqueue=False)
    services.execute_analysis(analysis=analysis, analyst=analyst or FakeAnalyst())
    analysis.refresh_from_db()
    return analysis


# --- Starting ------------------------------------------------------------------


def test_analysis_is_refused_while_it_is_turned_off(finished_run, settings) -> None:
    settings.LLM_ANALYSIS_ENABLED = False

    with pytest.raises(DomainError, match="LLM_ANALYSIS_ENABLED"):
        services.start_analysis(test_run=finished_run, enqueue=False)


def test_an_unfinished_run_cannot_be_analysed(specification) -> None:
    run = TestRun.objects.create(specification=specification, name="Pending")

    with pytest.raises(DomainError, match="finished"):
        services.start_analysis(test_run=run, enqueue=False)


def test_a_run_cannot_be_analysed_twice_at_once(finished_run) -> None:
    services.start_analysis(test_run=finished_run, enqueue=False)

    with pytest.raises(DomainError, match="already being analysed"):
        services.start_analysis(test_run=finished_run, enqueue=False)


def test_a_run_can_be_analysed_again_once_the_first_has_finished(finished_run) -> None:
    _analyse(finished_run)
    second = _analyse(finished_run)

    assert second.status == TestRunStatus.COMPLETED
    assert finished_run.analyses.count() == 2


def test_the_analysis_records_the_model_and_effort(finished_run, settings) -> None:
    settings.LLM_MODEL = "claude-sonnet-5"
    settings.LLM_EFFORT = "medium"

    analysis = services.start_analysis(test_run=finished_run, enqueue=False)

    assert analysis.status == TestRunStatus.RUNNING
    assert (analysis.model, analysis.effort) == ("claude-sonnet-5", "medium")
    assert analysis.started_at is not None


def test_starting_queues_the_task_after_commit(
    finished_run, monkeypatch, django_capture_on_commit_callbacks
) -> None:
    queued = []
    monkeypatch.setattr(run_analysis_task, "delay", lambda *args: queued.append(args))

    with django_capture_on_commit_callbacks(execute=True):
        analysis = services.start_analysis(test_run=finished_run)

    assert queued == [(analysis.pk,)]


def test_a_failure_to_queue_fails_the_analysis(
    finished_run, monkeypatch, django_capture_on_commit_callbacks
) -> None:
    def broken(*args):
        raise ConnectionError("redis is down")

    monkeypatch.setattr(run_analysis_task, "delay", broken)

    with django_capture_on_commit_callbacks(execute=True):
        analysis = services.start_analysis(test_run=finished_run)

    analysis.refresh_from_db()
    assert analysis.status == TestRunStatus.FAILED
    assert "redis is down" in analysis.status_detail


# --- Executing -----------------------------------------------------------------


def test_every_finding_is_assessed_in_one_request_when_they_fit(finished_run) -> None:
    analyst = FakeAnalyst()
    findings = _findings(finished_run)
    assert findings

    analysis = _analyse(finished_run, analyst)

    assert analysis.status == TestRunStatus.COMPLETED
    assert len(analyst.prompts) == 1
    assert "summary" in analyst.schemas[0]["properties"]
    assert analysis.assessments.count() == len(findings)
    assert analysis.summary == "POST /users validates almost nothing."
    assert sorted(analysis.groups[0]["finding_ids"]) == sorted(f.pk for f in findings)
    assert analysis.findings_total == analysis.findings_selected == len(findings)
    assert analysis.usage["requests"] == 1
    assert analysis.usage["input_tokens"] == 1000
    assert analysis.usage["models"] == ["claude-opus-5"]
    assert analysis.warnings == []


def test_a_large_run_is_sent_in_batches_then_summarised(finished_run, settings) -> None:
    settings.LLM_FINDINGS_PER_REQUEST = 2
    findings = _findings(finished_run)
    batches = -(-len(findings) // 2)
    analyst = FakeAnalyst()

    analysis = _analyse(finished_run, analyst)

    assert len(analyst.prompts) == batches + 1
    assert all("summary" not in schema["properties"] for schema in analyst.schemas[:-1])
    assert "assessments" not in analyst.schemas[-1]["properties"]
    assert "<assessed_findings>" in analyst.prompts[-1]
    assert analysis.assessments.count() == len(findings)
    assert analysis.usage["requests"] == batches + 1


def test_the_most_severe_findings_are_sent_first_and_the_cap_is_reported(
    finished_run, settings
) -> None:
    settings.LLM_MAX_FINDINGS = 2
    analyst = FakeAnalyst()

    analysis = _analyse(finished_run, analyst)

    total = len(_findings(finished_run))
    sent = Finding.objects.filter(pk__in=_ids(analyst.prompts[0]))
    assert analysis.findings_total == total
    assert analysis.findings_selected == 2
    # The Directory API's 500s are high; its validation gaps are medium.
    assert {f.severity for f in sent} == {"high"}
    assert any("LLM_MAX_FINDINGS" in warning for warning in analysis.warnings)


def test_a_run_without_findings_completes_without_asking_the_model(specification) -> None:
    run = TestRun.objects.create(specification=specification, name="Clean")
    services.start_test_run(run)
    services.complete_test_run(run)
    analyst = FakeAnalyst()

    analysis = _analyse(run, analyst)

    assert analysis.status == TestRunStatus.COMPLETED
    assert analyst.prompts == []
    assert "no findings" in analysis.summary


def test_the_model_never_changes_a_finding(finished_run) -> None:
    before = {
        f.pk: (f.severity, f.category, f.status, f.title) for f in _findings(finished_run)
    }

    def downgrade(data):
        for item in data["assessments"]:
            item.update(verdict="likely_false_positive", suggested_severity="info")
        return data

    _analyse(finished_run, FakeAnalyst(tweak=downgrade))

    after = {f.pk: (f.severity, f.category, f.status, f.title) for f in _findings(finished_run)}
    assert after == before
    assert set(FindingAssessment.objects.values_list("suggested_severity", flat=True)) == {"info"}


def test_run_credentials_never_reach_the_model(finished_run) -> None:
    analyst = FakeAnalyst()

    _analyse(finished_run, analyst)

    assert "Bearer secret" not in analyst.prompts[0]
    assert "[REDACTED]" in analyst.prompts[0]


def test_invented_finding_ids_are_ignored_and_reported(finished_run, test_case) -> None:
    # A real finding, but from another run: the model was never shown it.
    elsewhere = Finding.objects.create(test_case=test_case, title="From another run")

    def invent(data):
        data["assessments"].append({**data["assessments"][0], "finding_id": elsewhere.pk})
        data["groups"][0]["finding_ids"].append(elsewhere.pk)
        return data

    analysis = _analyse(finished_run, FakeAnalyst(tweak=invent))

    assert analysis.status == TestRunStatus.COMPLETED
    assert not elsewhere.assessments.exists()
    assert analysis.assessments.count() == len(_findings(finished_run))
    assert elsewhere.pk not in analysis.groups[0]["finding_ids"]
    assert any(str(elsewhere.pk) in warning for warning in analysis.warnings)


def test_a_provider_error_fails_the_analysis_and_keeps_earlier_batches(
    finished_run, settings
) -> None:
    settings.LLM_FINDINGS_PER_REQUEST = 2
    assert len(_findings(finished_run)) > 2

    analysis = _analyse(finished_run, FakeAnalyst(fail_on=2))

    assert analysis.status == TestRunStatus.FAILED
    assert "529" in analysis.status_detail
    assert analysis.assessments.count() == 2
    assert analysis.usage["requests"] == 1
    assert analysis.completed_at is not None


def test_an_unusable_reply_fails_the_analysis(finished_run) -> None:
    analysis = _analyse(finished_run, FakeAnalyst(tweak=lambda data: {"nothing": True}))

    assert analysis.status == TestRunStatus.FAILED
    assert "assessments" in analysis.status_detail
    assert analysis.usage["requests"] == 1


def test_a_reply_that_assesses_nothing_fails_the_analysis(finished_run) -> None:
    def empty(data):
        data["assessments"] = []
        return data

    analysis = _analyse(finished_run, FakeAnalyst(tweak=empty))

    assert analysis.status == TestRunStatus.FAILED
    assert "did not assess any" in analysis.status_detail


def test_an_unexpected_error_fails_the_analysis_and_is_raised(finished_run) -> None:
    class Broken:
        def ask(self, **kwargs):
            raise RuntimeError("bug")

    analysis = services.start_analysis(test_run=finished_run, enqueue=False)
    with pytest.raises(RuntimeError):
        services.execute_analysis(analysis=analysis, analyst=Broken())

    analysis.refresh_from_db()
    assert analysis.status == TestRunStatus.FAILED
    assert "bug" in analysis.status_detail


def test_a_finished_analysis_cannot_be_executed_again(finished_run) -> None:
    analysis = _analyse(finished_run)

    with pytest.raises(DomainError):
        services.execute_analysis(analysis=analysis, analyst=FakeAnalyst())


# --- Rules runs ----------------------------------------------------------------


@pytest.fixture
def finished_rules_run(specification) -> RulesRun:
    rules_run = RulesRun.objects.create(
        specification=specification,
        name="Leave rules",
        rules_text="version: 1\nscenarios: []\n",
        base_url="http://hr.test",
        status=TestRunStatus.COMPLETED,
    )
    scenario = ScenarioResult.objects.create(
        rules_run=rules_run, position=0, name="leave_balance", status=ScenarioStatus.FAILED
    )
    Finding.objects.create(
        scenario_result=scenario,
        title="balance: expected 10, got 12",
        severity="high",
        category="business_rule_violation",
        evidence={"expected": 10, "actual": 12, "expected_working": "14 - 4 = 10"},
    )
    return rules_run


def test_a_rules_run_is_analysed_with_its_rules_file(finished_rules_run) -> None:
    analyst = FakeAnalyst()

    analysis = _analyse(analyst=analyst, rules_run=finished_rules_run)

    assert analysis.status == TestRunStatus.COMPLETED
    assert analysis.rules_run == finished_rules_run
    assert analysis.assessments.count() == 1
    assert '"scenario": "leave_balance"' in analyst.prompts[0]
    assert "rules_file" in analyst.prompts[0]


# --- API -----------------------------------------------------------------------


def test_post_analyse_returns_202_and_the_analysis(
    api_client, finished_run, monkeypatch
) -> None:
    monkeypatch.setattr(run_analysis_task, "delay", lambda *args: None)

    response = api_client.post(f"/api/v1/test-runs/{finished_run.pk}/analyse/")

    assert response.status_code == status.HTTP_202_ACCEPTED
    assert response.data["status"] == "running"
    assert response.data["test_run"] == finished_run.pk
    assert response.data["results"] == {
        "assessed": 0,
        "likely_real": 0,
        "likely_false_positive": 0,
        "uncertain": 0,
    }


def test_post_analyse_for_a_rules_run(api_client, finished_rules_run, monkeypatch) -> None:
    monkeypatch.setattr(run_analysis_task, "delay", lambda *args: None)

    response = api_client.post(f"/api/v1/rules-runs/{finished_rules_run.pk}/analyse/")

    assert response.status_code == status.HTTP_202_ACCEPTED
    assert response.data["rules_run"] == finished_rules_run.pk


def test_analyse_while_turned_off_is_a_400(api_client, finished_run, settings) -> None:
    settings.LLM_ANALYSIS_ENABLED = False

    response = api_client.post(f"/api/v1/test-runs/{finished_run.pk}/analyse/")

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert "LLM_ANALYSIS_ENABLED" in response.data["detail"]


def test_analysing_a_missing_run_is_404(api_client) -> None:
    assert api_client.post("/api/v1/test-runs/999/analyse/").status_code == 404
    assert api_client.post("/api/v1/rules-runs/999/analyse/").status_code == 404


def test_analyse_is_not_reachable_by_get(api_client, finished_run) -> None:
    response = api_client.get(f"/api/v1/test-runs/{finished_run.pk}/analyse/")

    assert response.status_code == status.HTTP_405_METHOD_NOT_ALLOWED


def test_a_finished_analysis_and_its_assessments_are_readable(api_client, finished_run) -> None:
    def mixed(data):
        data["assessments"][-1]["verdict"] = "uncertain"
        return data

    analysis = _analyse(finished_run, FakeAnalyst(tweak=mixed))
    count = analysis.assessments.count()

    detail = api_client.get(f"/api/v1/analyses/{analysis.pk}/").data
    assert detail["status"] == "completed"
    assert detail["results"]["assessed"] == count
    assert detail["results"]["uncertain"] == 1
    assert detail["results"]["likely_real"] == count - 1
    assert detail["groups"][0]["title"] == "POST /users body validation"

    page = api_client.get(f"/api/v1/analyses/{analysis.pk}/assessments/").data
    assert page["count"] == count
    first = page["results"][0]
    assert first["finding_severity"] == "high"
    assert {"finding_title", "verdict", "confidence", "reasoning"} <= set(first)


def test_analyses_can_be_listed_by_run(api_client, finished_run, finished_rules_run) -> None:
    _analyse(finished_run)
    _analyse(rules_run=finished_rules_run)

    everything = api_client.get("/api/v1/analyses/").data
    by_test_run = api_client.get(f"/api/v1/analyses/?test_run={finished_run.pk}").data
    by_rules_run = api_client.get(f"/api/v1/analyses/?rules_run={finished_rules_run.pk}").data

    assert everything["count"] == 2
    assert [a["test_run"] for a in by_test_run["results"]] == [finished_run.pk]
    assert [a["rules_run"] for a in by_rules_run["results"]] == [finished_rules_run.pk]


def test_assessments_of_a_missing_analysis_are_404(api_client) -> None:
    assert api_client.get("/api/v1/analyses/999/assessments/").status_code == 404


# --- Task and command ------------------------------------------------------------


def test_the_task_executes_the_analysis(finished_run, monkeypatch) -> None:
    monkeypatch.setattr(Analyst, "from_settings", classmethod(lambda cls: FakeAnalyst()))
    analysis = services.start_analysis(test_run=finished_run, enqueue=False)

    run_analysis_task(analysis.pk)

    analysis.refresh_from_db()
    assert analysis.status == TestRunStatus.COMPLETED


def test_the_task_ignores_a_deleted_analysis() -> None:
    run_analysis_task(999)


def test_the_command_analyses_and_prints_the_result(finished_run, monkeypatch) -> None:
    monkeypatch.setattr(Analyst, "from_settings", classmethod(lambda cls: FakeAnalyst()))
    out = StringIO()

    call_command("analyse_run", "--test-run", str(finished_run.pk), stdout=out)

    text = out.getvalue()
    assert "completed" in text
    assert "likely real" in text
    assert "POST /users body validation" in text


def test_the_command_reports_a_refusal_to_start(finished_run, settings) -> None:
    settings.LLM_ANALYSIS_ENABLED = False

    with pytest.raises(CommandError, match="turned off"):
        call_command("analyse_run", "--test-run", str(finished_run.pk), stdout=StringIO())


def test_the_command_rejects_a_missing_run() -> None:
    with pytest.raises(CommandError, match="does not exist"):
        call_command("analyse_run", "--rules-run", "999", stdout=StringIO())


# --- The client ------------------------------------------------------------------


class _Stream:
    def __init__(self, outcome) -> None:
        self.outcome = outcome

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        return None

    def get_final_message(self):
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


class _SDK:
    """Records the request and returns ``outcome`` from the stream."""

    def __init__(self, outcome) -> None:
        self.requests: list[dict] = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(stream=self._stream))
        self.outcome = outcome

    def _stream(self, **request):
        self.requests.append(request)
        return _Stream(self.outcome)


def _message(stop_reason="end_turn", text='{"ok": true}', **extra):
    return SimpleNamespace(
        stop_reason=stop_reason,
        model="claude-opus-5",
        content=[
            SimpleNamespace(type="thinking", thinking=""),
            SimpleNamespace(type="text", text=text),
        ],
        usage=SimpleNamespace(
            input_tokens=10,
            output_tokens=5,
            cache_creation_input_tokens=None,
            cache_read_input_tokens=0,
        ),
        **extra,
    )


def test_the_client_sends_a_structured_request_with_fallbacks() -> None:
    sdk = _SDK(_message())
    schema = {"type": "object"}

    reply = Analyst(client=sdk, model="claude-opus-5", effort="high").ask(
        prompt="hello", schema=schema
    )

    request = sdk.requests[0]
    assert request["model"] == "claude-opus-5"
    assert request["system"] == analysis_client.SYSTEM_PROMPT
    assert request["messages"] == [{"role": "user", "content": "hello"}]
    assert request["thinking"] == {"type": "adaptive"}
    assert request["output_config"] == {
        "format": {"type": "json_schema", "schema": schema},
        "effort": "high",
    }
    assert request["fallbacks"] == "default"
    assert request["betas"] == ["server-side-fallback-2026-07-01"]
    assert reply.text == '{"ok": true}'
    assert reply.usage == {
        "input_tokens": 10,
        "output_tokens": 5,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
    }


def test_the_client_can_leave_fallbacks_and_effort_out() -> None:
    sdk = _SDK(_message())

    Analyst(client=sdk, model="claude-haiku-4-5", refusal_fallback=False).ask(
        prompt="p", schema={}
    )

    request = sdk.requests[0]
    assert "fallbacks" not in request and "betas" not in request
    assert "effort" not in request["output_config"]


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        (
            _message("refusal", stop_details=SimpleNamespace(category="cyber")),
            "declined to analyse these findings (category: cyber)",
        ),
        (_message("max_tokens"), "cut off at the token limit"),
        (_message(text=""), "held no text"),
    ],
)
def test_an_unusable_message_is_an_analyst_error(message, expected: str) -> None:
    with pytest.raises(AnalystError, match=re.escape(expected)):
        Analyst(client=_SDK(message), model="m").ask(prompt="p", schema={})


def _status_error(cls, code: int):
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx2.Response(code, request=request, json={"error": {"message": "nope"}})
    return cls("nope", response=response, body=None)


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (_status_error(anthropic.AuthenticationError, 401), "ANTHROPIC_API_KEY"),
        (_status_error(anthropic.RateLimitError, 429), "rate limited"),
        (_status_error(anthropic.BadRequestError, 400), "refused the request"),
        (_status_error(anthropic.InternalServerError, 500), "answered 500"),
        (
            anthropic.APIConnectionError(
                request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
            ),
            "Could not reach",
        ),
    ],
)
def test_provider_errors_become_analyst_errors(error, expected: str) -> None:
    with pytest.raises(AnalystError, match=expected):
        Analyst(client=_SDK(error), model="m").ask(prompt="p", schema={})


def test_the_client_is_built_from_settings(settings) -> None:
    settings.LLM_MODEL = "claude-sonnet-5"
    settings.LLM_EFFORT = "low"
    settings.LLM_REFUSAL_FALLBACK = False
    settings.LLM_TIMEOUT_SECONDS = 30
    settings.LLM_MAX_RETRIES = 1

    analyst = Analyst.from_settings()

    assert (analyst.model, analyst.effort, analyst.refusal_fallback) == (
        "claude-sonnet-5",
        "low",
        False,
    )
    assert analyst.client.max_retries == 1
