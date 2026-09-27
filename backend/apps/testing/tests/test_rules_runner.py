"""Running scenarios: auth, steps, saved variables, timelines, rules, cleanup.

Every request goes to FakeLeaveApi through an httpx.MockTransport; no network
and no database.
"""

from __future__ import annotations

import json
from datetime import date

import httpx
import pytest

from apps.testing import executor
from apps.testing.rules import document
from apps.testing.rules.runner import RunnerError, compare, run
from apps.testing.tests.rules_fixtures import (
    LEAVE_RULES,
    PASSWORD,
    SETUP,
    TODAY,
    TOKEN,
    FakeLeaveApi,
)

BASE_URL = "http://leave.test/api"


def _run(api: FakeLeaveApi, rules_text: str = LEAVE_RULES, **overrides):
    arguments = {
        "base_url": BASE_URL,
        "setup_values": SETUP,
        "client": api.client(),
        "max_response_bytes": 100_000,
        "today": TODAY,
    }
    arguments.update(overrides)
    return run(document.load(rules_text), **arguments)


def _outcomes(result, rule_name: str):
    return [o for s in result.scenarios for o in s.outcomes if o.rule.name == rule_name]


# --- A correct API ------------------------------------------------------------


def test_a_correct_api_passes_every_rule() -> None:
    result = _run(FakeLeaveApi())

    (scenario,) = result.scenarios
    assert scenario.status == "passed"
    assert scenario.steps_run == 3
    assert [o.passed for o in scenario.outcomes] == [True, True, True]
    assert scenario.cleanup_problems == []
    assert result.broken == [] and result.unchecked == [] and result.stopped == []


def test_the_exchanges_are_recorded_in_order() -> None:
    result = _run(FakeLeaveApi())

    assert result.auth_exchange.label == "auth login"
    labels = [exchange.label for exchange in result.scenarios[0].exchanges]
    assert labels == [
        "step apply",
        "step approve",
        "fetch balance_is_accrual_minus_taken",
        "step raise_rate",
        "fetch balance_is_accrual_minus_taken",
        "cleanup delete_leave",
    ]


# --- Variables between steps --------------------------------------------------


def test_a_saved_value_is_used_by_later_steps() -> None:
    api = FakeLeaveApi()

    result = _run(api)

    exchanges = result.scenarios[0].exchanges
    assert exchanges[0].result.body["id"] == 101
    assert exchanges[1].request.url == f"{BASE_URL}/leaves/101/approve"
    assert exchanges[-1].request.url == f"{BASE_URL}/leaves/101"
    assert api.leaves == {}  # cleanup deleted what the scenario created


def test_setup_values_and_dates_are_rendered_into_requests() -> None:
    result = _run(FakeLeaveApi())

    apply = result.scenarios[0].exchanges[0]
    assert apply.request.url == f"{BASE_URL}/employees/42/leaves"
    assert apply.request.body == {"start": "2026-09-23", "end": "2026-09-27"}


def test_the_login_token_is_sent_on_every_request() -> None:
    api = FakeLeaveApi()

    _run(api)

    login, *rest = api.requests
    assert "Authorization" not in login.headers
    assert rest and all(r.headers["Authorization"] == f"Bearer {TOKEN}" for r in rest)


# --- Timelines ----------------------------------------------------------------


def test_a_step_changes_the_timeline_for_later_rules() -> None:
    result = _run(FakeLeaveApi())

    after_approve, after_raise = _outcomes(result, "balance_is_accrual_minus_taken")
    assert after_approve.after_step == "approve"
    assert after_approve.expected == 47
    assert after_approve.expected_working == (
        "4 days × 2 + 11 days × 4 = 52; leave_start = 2026-09-23; "
        "sum_over_periods(accrual) - days(leave_start, today) = 47"
    )
    # From today the rate is 5: the open period closes yesterday.
    assert after_raise.expected == 48
    assert after_raise.expected_working.startswith("4 days × 2 + 10 days × 4 + 1 day × 5 = 53")


# --- A wrong API --------------------------------------------------------------


def test_a_wrong_total_breaks_the_rule_with_both_values() -> None:
    result = _run(FakeLeaveApi(retroactive_rate=True))

    after_approve, after_raise = _outcomes(result, "balance_is_accrual_minus_taken")
    assert after_approve.passed is False
    assert after_approve.actual == 55  # 15 days × 4 - 5
    assert after_approve.expected == 47
    assert after_approve.detail == "actual 55 should be equals expected 47"
    assert after_raise.passed is False and after_raise.actual == 70
    assert result.scenarios[0].status == "failed"
    # The chain ends with the follow-up GET whose answer was wrong.
    assert after_approve.chain[-1].label == "fetch balance_is_accrual_minus_taken"
    assert after_approve.chain[-1].result.body == {"balance": 55}


def test_an_off_by_one_breaks_only_the_rule_it_affects() -> None:
    result = _run(FakeLeaveApi(exclusive_days=True))

    (inclusive,) = _outcomes(result, "leave_counts_both_ends")
    assert inclusive.passed is False
    assert (inclusive.actual, inclusive.expected) == (4, 5)
    # The balance follows the API's own leave count, so it is off by one too.
    assert all(o.passed is False for o in _outcomes(result, "balance_is_accrual_minus_taken"))


# --- Stopping and cleanup -----------------------------------------------------


def test_an_unexpected_status_stops_the_scenario_and_cleanup_still_runs() -> None:
    api = FakeLeaveApi(approve_status=409)

    result = _run(api)

    (scenario,) = result.scenarios
    assert scenario.status == "stopped"
    assert scenario.steps_run == 1
    failure = scenario.failure
    assert (failure.step, failure.status_code, failure.expected_status) == ("approve", 409, (200,))
    assert "Expected 200, received 409" in failure.detail
    assert [e.label for e in failure.chain] == ["step apply", "step approve"]
    # raise_rate never ran; its rules were never checked.
    assert [o.after_step for o in scenario.outcomes] == ["apply"]
    assert scenario.exchanges[-1].label == "cleanup delete_leave"
    assert api.leaves == {}


def test_cleanup_that_needs_an_unsaved_value_is_skipped() -> None:
    def refuse_leaves(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/leaves") and request.method == "POST":
            return httpx.Response(500, text="boom")
        return api.handle(request)

    api = FakeLeaveApi()
    client = httpx.Client(transport=httpx.MockTransport(refuse_leaves))

    result = _run(api, client=client)

    (scenario,) = result.scenarios
    assert scenario.failure.step == "apply" and scenario.failure.status_code == 500
    assert scenario.cleanup_problems == [
        "delete_leave: skipped, leave_id was never set (the scenario stopped before saving it)."
    ]


def test_a_connection_failure_stops_the_scenario() -> None:
    def refused(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/auth/login"):
            return FakeLeaveApi().handle(request)
        raise httpx.ConnectError("Connection refused", request=request)

    result = _run(FakeLeaveApi(), client=httpx.Client(transport=httpx.MockTransport(refused)))

    failure = result.scenarios[0].failure
    assert failure.status_code is None
    assert "Connection refused" in failure.detail


def test_a_failed_login_runs_no_scenario() -> None:
    api = FakeLeaveApi()

    result = _run(api, setup_values={**SETUP, "password": "wrong"})

    assert result.scenarios == []
    assert "Login failed" in result.auth_error
    assert len(api.requests) == 1


def test_missing_setup_values_stop_the_run_before_any_request() -> None:
    api = FakeLeaveApi()

    with pytest.raises(RunnerError, match="password") as excinfo:
        _run(api, setup_values={"employee_id": "42", "username": "asha"})

    assert "AEGIS_PASSWORD" in str(excinfo.value)
    assert api.requests == []


# --- Rules that cannot be checked ---------------------------------------------


def test_a_missing_field_leaves_the_rule_unchecked_not_passed() -> None:
    rules = LEAVE_RULES.replace("actual: $.days", "actual: $.day_count")

    result = _run(FakeLeaveApi(), rules)

    (outcome,) = _outcomes(result, "leave_counts_both_ends")
    assert outcome.passed is None
    assert "Nothing in the response matches $.day_count" in outcome.detail
    assert outcome.expected == 5
    assert result.scenarios[0].status == "error"


def test_a_failed_follow_up_leaves_the_rule_unchecked() -> None:
    rules = LEAVE_RULES.replace("/employees/{employee_id}/balance", "/employees/{employee_id}/nothing")

    result = _run(FakeLeaveApi(), rules)

    for outcome in _outcomes(result, "balance_is_accrual_minus_taken"):
        assert outcome.passed is None
        assert "failed: status 404" in outcome.detail


# --- Secrets ------------------------------------------------------------------


def test_secrets_and_the_token_never_appear_in_the_record() -> None:
    result = _run(FakeLeaveApi())

    exchanges = [result.auth_exchange, *result.scenarios[0].exchanges]
    for outcome in result.scenarios[0].outcomes:
        exchanges.extend(outcome.chain)
    recorded = json.dumps(
        [
            [e.request.url, e.request.headers, e.request.body, e.result.headers, e.result.body]
            for e in exchanges
        ]
    )
    assert PASSWORD not in recorded
    assert TOKEN not in recorded
    assert result.auth_exchange.request.body["password"] == executor.REDACTED
    assert result.auth_exchange.result.body["access_token"] == executor.REDACTED
    assert result.scenarios[0].exchanges[0].request.headers["Authorization"] == executor.REDACTED
    # A non-secret setup value is part of the evidence, as the request used it.
    assert result.auth_exchange.request.body["username"] == "asha"


def test_a_secret_is_scrubbed_in_its_url_encoded_form_too() -> None:
    rules = LEAVE_RULES.replace(
        "path: /leaves/{leave_id}/approve", "path: /leaves/{leave_id}/approve\n        query: {pin: \"${password}\"}"
    )
    setup = {**SETUP, "password": "p@ss word"}

    def accept_any_login(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/auth/login"):
            return httpx.Response(200, json={"access_token": TOKEN})
        return FakeLeaveApi.handle(api, request)

    api = FakeLeaveApi()
    result = _run(api, rules, setup_values=setup, client=httpx.Client(transport=httpx.MockTransport(accept_any_login)))

    approve = result.scenarios[0].exchanges[1]
    assert approve.request.query_params == {"pin": executor.REDACTED}
    assert "p%40ss" not in approve.request.url and "p@ss" not in approve.request.url


# --- Determinism and --today --------------------------------------------------


def test_the_same_file_and_today_send_the_same_requests() -> None:
    first, second = FakeLeaveApi(), FakeLeaveApi()

    _run(first)
    _run(second)

    def signature(api):
        return [(r.method, str(r.url), r.content) for r in api.requests]

    assert signature(first) == signature(second)


def test_today_can_be_overridden_for_the_dates_sent() -> None:
    api = FakeLeaveApi()

    result = _run(api, today=date(2026, 1, 10))

    assert result.today == date(2026, 1, 10) and result.today_overridden
    apply = result.scenarios[0].exchanges[0]
    assert apply.request.body == {"start": "2026-01-06", "end": "2026-01-10"}


def test_without_an_override_today_comes_from_the_files_time_zone() -> None:
    result = _run(FakeLeaveApi(), today=None)

    assert result.today_overridden is False
    assert isinstance(result.today, date)


# --- compare() ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("actual", "expected", "how", "holds"),
    [
        (5, 5, "equals", True),
        (5, 5.0, "equals", True),
        ("2026-09-23", date(2026, 9, 23), "equals", True),
        (4, 5, "equals", False),
        (4, 5, "not_equals", True),
        (4, 5, "less_than", True),
        (5, 5, "less_or_equal", True),
        (6, 5, "greater_than", True),
        (5, 5, "greater_or_equal", True),
        (-1, 0, "greater_or_equal", False),
    ],
)
def test_compare(actual, expected, how, holds) -> None:
    assert compare(actual, expected, how)[0] is holds


def test_compare_with_tolerance() -> None:
    assert compare(47.004, 47, "equals", 0.01) == (True, "actual 47.004 is within 0.01 of expected 47")
    holds, detail = compare(47.5, 47, "equals", 0.01)
    assert holds is False
    assert "by 0.5, more than the tolerance 0.01" in detail


def test_values_that_cannot_be_ordered_are_unchecked() -> None:
    holds, detail = compare("abc", 5, "less_than")

    assert holds is None
    assert "cannot be compared" in detail
