"""Running a rules file's scenarios against an API.

For each scenario, in order: send each step, check its status, save values
from its response for later steps, update any timeline it changes, then check
every rule that applies after it. Cleanup steps run last, whatever happened.

The runner records everything and persists nothing. It returns a RunResult:
every exchange in order, every rule outcome with the chain of exchanges that
led to it, and why a scenario stopped if it did. Turning that into database
rows is the service's job, so the runner can be driven — and tested — with a
mock transport and no database.

What it keeps out of the record: the Authorization header (as the executor
already does for test cases), the token the login returned, and every setup
value marked ``secret`` — wherever they appear, including request bodies.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from typing import Any
from urllib.parse import quote, quote_plus
from zoneinfo import ZoneInfo

import httpx

from apps.testing import executor
from apps.testing.rules import timeline
from apps.testing.rules.document import (
    COMPARISONS,
    RequestSpec,
    Rule,
    RulesDocument,
    Scenario,
    Step,
)
from apps.testing.rules.expressions import Evaluator, ExpressionError, is_json_path, query, to_json

_PLACEHOLDER = re.compile(r"\{([^{}]+)\}")


class RunnerError(ValueError):
    """The run cannot start: something it needs was not supplied."""


# --- What a run produces ------------------------------------------------------


@dataclass(frozen=True)
class Exchange:
    """One request and what came back, as it will be recorded (scrubbed)."""

    phase: str  # "auth", "step", "fetch" or "cleanup"
    name: str  # the step's name, or the rule's for a fetch
    request: executor.PreparedRequest
    result: executor.ExecutionResult

    @property
    def label(self) -> str:
        return f"{self.phase} {self.name}".strip()


@dataclass(frozen=True)
class RuleOutcome:
    """One check of one rule after one step."""

    rule: Rule
    scenario: str
    after_step: str
    # True: the rule holds. False: it is broken. None: it could not be
    # checked — the field was missing, the fetch failed, an expression failed.
    passed: bool | None
    expected: Any
    expected_working: str
    actual: Any
    detail: str
    chain: tuple[Exchange, ...]


@dataclass(frozen=True)
class StepFailure:
    """Why a scenario stopped before its last step."""

    scenario: str
    step: str
    detail: str
    expected_status: tuple[int, ...]
    status_code: int | None
    chain: tuple[Exchange, ...]


@dataclass
class ScenarioResult:
    name: str
    exchanges: list[Exchange] = field(default_factory=list)
    outcomes: list[RuleOutcome] = field(default_factory=list)
    failure: StepFailure | None = None
    steps_run: int = 0
    # Cleanup that could not run or did not succeed; never stops the others.
    cleanup_problems: list[str] = field(default_factory=list)

    @property
    def status(self) -> str:
        if self.failure is not None:
            return "stopped"
        if any(outcome.passed is False for outcome in self.outcomes):
            return "failed"
        if any(outcome.passed is None for outcome in self.outcomes):
            return "error"
        return "passed"


@dataclass
class RunResult:
    today: date
    # True when --today set the date. It changes the dates the requests carry;
    # it cannot move the server's clock.
    today_overridden: bool
    scenarios: list[ScenarioResult] = field(default_factory=list)
    auth_exchange: Exchange | None = None
    auth_error: str = ""

    @property
    def broken(self) -> list[RuleOutcome]:
        return [o for s in self.scenarios for o in s.outcomes if o.passed is False]

    @property
    def unchecked(self) -> list[RuleOutcome]:
        return [o for s in self.scenarios for o in s.outcomes if o.passed is None]

    @property
    def stopped(self) -> list[StepFailure]:
        return [s.failure for s in self.scenarios if s.failure is not None]


# --- Running ------------------------------------------------------------------


def resolve_today(timezone: str, override: date | None = None) -> date:
    """The run's "today": the override, or the date now in the file's time zone."""
    if override is not None:
        return override
    return datetime.now(ZoneInfo(timezone)).date()


def missing_setup(rules: RulesDocument, values: Mapping[str, str]) -> list[str]:
    return [value.name for value in rules.setup if not values.get(value.name)]


def run(
    rules: RulesDocument,
    *,
    base_url: str,
    setup_values: Mapping[str, str],
    client: httpx.Client,
    max_response_bytes: int,
    today: date | None = None,
) -> RunResult:
    """Run every scenario in ``rules`` against ``base_url``.

    Raises RunnerError, before sending anything, when a setup value is
    missing or a file-level value cannot be evaluated. Anything the API does
    is recorded in the result, never raised.
    """
    missing = missing_setup(rules, setup_values)
    if missing:
        raise RunnerError(
            "Missing setup values: "
            + ", ".join(
                f"{value.name} (env {value.env})" for value in rules.setup if value.name in missing
            )
        )

    run_today = resolve_today(rules.timezone, today)
    result = RunResult(today=run_today, today_overridden=today is not None)
    secrets = [setup_values[name] for name in rules.secret_names if setup_values.get(name)]

    base = Evaluator({value.name: setup_values[value.name] for value in rules.setup}, today=run_today)
    try:
        for name, value in rules.variables.items():
            base.set(name, base.resolve(value))
        for name, periods in rules.timelines.items():
            base.set(name, timeline.parse_periods(periods, resolve=lambda raw: _resolve(base, raw)))
    except (ExpressionError, timeline.TimelineError) as exc:
        raise RunnerError(f"Could not evaluate the file's variables: {exc}") from exc

    context = _Context(
        base_url=base_url,
        client=client,
        max_response_bytes=max_response_bytes,
        secrets=secrets,
    )

    if rules.auth is not None:
        if not _authenticate(rules, base, context, result):
            return result

    for scenario in rules.scenarios:
        result.scenarios.append(_run_scenario(scenario, base.child(), context))
    return result


@dataclass
class _Context:
    base_url: str
    client: httpx.Client
    max_response_bytes: int
    secrets: list[str]
    auth_headers: dict[str, str] = field(default_factory=dict)


def _authenticate(
    rules: RulesDocument, evaluator: Evaluator, context: _Context, result: RunResult
) -> bool:
    auth = rules.auth
    try:
        exchange = _send(auth.request, evaluator, context, phase="auth", name="login")
    except ExpressionError as exc:
        result.auth_error = f"The login request could not be built: {exc}"
        return False

    status = exchange.result.status_code
    if status is None or not 200 <= status < 300:
        result.auth_exchange = _scrub_exchange(exchange, context.secrets)
        result.auth_error = (
            f"Login failed: {exchange.result.error or f'status {status}'}. "
            f"No scenario was run."
        )
        return False

    try:
        token = query(auth.token_path, exchange.result.body)
    except ExpressionError as exc:
        result.auth_exchange = _scrub_exchange(exchange, context.secrets)
        result.auth_error = f"Login succeeded but returned no token: {exc}"
        return False

    token = str(token)
    context.secrets.append(token)
    context.auth_headers[auth.header] = f"{auth.scheme} {token}".strip()
    result.auth_exchange = _scrub_exchange(exchange, context.secrets)
    return True


def _run_scenario(scenario: Scenario, evaluator: Evaluator, context: _Context) -> ScenarioResult:
    outcome = ScenarioResult(name=scenario.name)
    try:
        for name, value in scenario.variables.items():
            evaluator.set(name, evaluator.resolve(value))
    except ExpressionError as exc:
        outcome.failure = StepFailure(
            scenario=scenario.name,
            step="",
            detail=f"The scenario's variables could not be evaluated: {exc}",
            expected_status=(),
            status_code=None,
            chain=(),
        )
        return outcome

    for step in scenario.steps:
        if not _run_step(scenario, step, evaluator, context, outcome):
            break
        outcome.steps_run += 1
        for rule in scenario.rules:
            if rule.applies_after(step.name):
                outcome.outcomes.append(_check_rule(scenario, step, rule, evaluator, context, outcome))

    for step in scenario.cleanup:
        _run_cleanup(step, evaluator, context, outcome)
    return outcome


def _run_step(
    scenario: Scenario,
    step: Step,
    evaluator: Evaluator,
    context: _Context,
    outcome: ScenarioResult,
) -> bool:
    """Send one step and apply its effects. False when the scenario must stop."""

    def stop(detail: str, status_code: int | None = None) -> bool:
        outcome.failure = StepFailure(
            scenario=scenario.name,
            step=step.name,
            detail=detail,
            expected_status=step.expect,
            status_code=status_code,
            chain=tuple(outcome.exchanges),
        )
        return False

    try:
        exchange = _send(step.request, evaluator, context, phase="step", name=step.name)
    except ExpressionError as exc:
        return stop(f"The request could not be built: {exc}")
    outcome.exchanges.append(_scrub_exchange(exchange, context.secrets))

    status = exchange.result.status_code
    if status is None:
        return stop(exchange.result.error)
    if step.expect and status not in step.expect:
        return stop(
            f"Expected {_statuses(step.expect)}, received {status}. The scenario stopped here.",
            status,
        )

    body = exchange.result.body
    evaluator.set("response", body)
    for name, path in step.save.items():
        try:
            evaluator.set(name, query(path, body))
        except ExpressionError as exc:
            return stop(f"Could not save '{name}' from the response: {exc}", status)

    for change in step.periods:
        try:
            evaluator.set(
                change.timeline,
                timeline.append_period(
                    evaluator.variables[change.timeline],
                    _resolve(evaluator, change.start),
                    _resolve(evaluator, change.rate),
                ),
            )
        except (ExpressionError, timeline.TimelineError) as exc:
            return stop(f"Could not update the timeline '{change.timeline}': {exc}", status)
    return True


def _check_rule(
    scenario: Scenario,
    step: Step,
    rule: Rule,
    evaluator: Evaluator,
    context: _Context,
    outcome: ScenarioResult,
) -> RuleOutcome:
    """Evaluate one rule now. Never raises: a rule that cannot be checked says why."""
    scope = evaluator.child()
    chain = list(outcome.exchanges)

    def result(passed: bool | None, detail: str, **values: Any) -> RuleOutcome:
        return RuleOutcome(
            rule=rule,
            scenario=scenario.name,
            after_step=step.name,
            passed=passed,
            expected=values.get("expected"),
            expected_working=values.get("working", ""),
            actual=values.get("actual"),
            detail=detail,
            chain=tuple(chain),
        )

    if rule.fetch is not None:
        try:
            fetched = _send(rule.fetch, scope, context, phase="fetch", name=rule.name)
        except ExpressionError as exc:
            return result(None, f"The follow-up request could not be built: {exc}")
        # A follow-up is a real request to the API: it belongs to the
        # scenario's record as well as to this rule's chain.
        recorded = _scrub_exchange(fetched, context.secrets)
        outcome.exchanges.append(recorded)
        chain.append(recorded)
        status = fetched.result.status_code
        if status is None or not 200 <= status < 300:
            return result(
                None,
                f"The follow-up {rule.fetch.method} {rule.fetch.path} failed: "
                f"{fetched.result.error or f'status {status}'}.",
            )
        scope.set("response", fetched.result.body)

    try:
        expected = scope.evaluate(rule.expected)
    except ExpressionError as exc:
        return result(None, f"The expected value could not be evaluated: {exc}")
    try:
        if is_json_path(rule.actual):
            actual = query(rule.actual, scope.variables.get("response"))
        else:
            actual = scope.evaluate(rule.actual).value
    except ExpressionError as exc:
        return result(
            None,
            f"The actual value could not be read: {exc}",
            expected=expected.value,
            working=expected.working,
        )

    passed, detail = compare(actual, expected.value, rule.compare, rule.tolerance)
    return result(passed, detail, expected=expected.value, working=expected.working, actual=actual)


def _run_cleanup(step: Step, evaluator: Evaluator, context: _Context, outcome: ScenarioResult) -> None:
    """Best effort: a cleanup that cannot run is noted, and the rest still run."""
    missing = [
        name for name in _PLACEHOLDER.findall(step.request.path) if name not in evaluator.variables
    ]
    if missing:
        outcome.cleanup_problems.append(
            f"{step.name}: skipped, {', '.join(missing)} was never set "
            f"(the scenario stopped before saving it)."
        )
        return
    try:
        exchange = _send(step.request, evaluator, context, phase="cleanup", name=step.name)
    except ExpressionError as exc:
        outcome.cleanup_problems.append(f"{step.name}: skipped, {exc}")
        return
    outcome.exchanges.append(_scrub_exchange(exchange, context.secrets))
    status = exchange.result.status_code
    if status is None:
        outcome.cleanup_problems.append(f"{step.name}: {exchange.result.error}")
    elif step.expect and status not in step.expect:
        outcome.cleanup_problems.append(
            f"{step.name}: expected {_statuses(step.expect)}, received {status}."
        )


# --- Comparing ----------------------------------------------------------------


def compare(
    actual: Any, expected: Any, how: str, tolerance: float | None = None
) -> tuple[bool | None, str]:
    """Compare two values. Returns (holds, plain-English detail).

    Both sides are normalised to what JSON carries first, so a date the rules
    file computed equals the ISO string an API returns for it.
    """
    left, right = to_json(actual), to_json(expected)
    symbol = COMPARISONS[how]

    if how == "equals" and tolerance is not None and _is_number(left) and _is_number(right):
        holds = abs(left - right) <= tolerance
        expectation = f"{_show(right)} (± {_show(tolerance)})"
        return holds, f"expected {expectation}, got {_show(left)}"

    try:
        holds = {
            "==": lambda: left == right,
            "!=": lambda: left != right,
            "<": lambda: left < right,
            "<=": lambda: left <= right,
            ">": lambda: left > right,
            ">=": lambda: left >= right,
        }[symbol]()
    except TypeError:
        return None, (
            f"expected {_EXPECTATIONS[how]}{_show(right)}, got {_show(left)}, "
            f"which cannot be compared with it"
        )
    return holds, f"expected {_EXPECTATIONS[how]}{_show(right)}, got {_show(left)}"


# How each comparison reads before the expected value: "expected 47, got 55",
# "expected at least 0, got -1".
_EXPECTATIONS = {
    "equals": "",
    "not_equals": "anything but ",
    "less_than": "less than ",
    "less_or_equal": "at most ",
    "greater_than": "more than ",
    "greater_or_equal": "at least ",
}


# --- Sending ------------------------------------------------------------------


def _send(
    spec: RequestSpec, evaluator: Evaluator, context: _Context, *, phase: str, name: str
) -> Exchange:
    path_values = {
        placeholder: _path_value(evaluator, placeholder)
        for placeholder in _PLACEHOLDER.findall(spec.path)
    }
    prepared = executor.prepare(
        method=spec.method,
        path=spec.path,
        input_data={
            "path": path_values,
            "query": evaluator.render(spec.query),
            "headers": evaluator.render(spec.headers),
            "body": evaluator.render(spec.body) if spec.body is not None else None,
        },
        base_url=context.base_url,
        run_headers=context.auth_headers if phase != "auth" else {},
    )
    sent = executor.send(prepared, client=context.client, max_response_bytes=context.max_response_bytes)
    return Exchange(phase=phase, name=name, request=prepared, result=sent)


def _path_value(evaluator: Evaluator, name: str) -> Any:
    if name not in evaluator.variables:
        raise ExpressionError(f"'{name}' is not set.")
    value = to_json(evaluator.variables[name])
    if isinstance(value, (dict, list)):
        raise ExpressionError(f"'{name}' is not a single value and cannot go in a path.")
    return value


def _resolve(evaluator: Evaluator, raw: Any) -> Any:
    """A period's value: a bare string is an expression, anything else a template."""
    if isinstance(raw, str) and "${" not in raw:
        return evaluator.evaluate(raw).value
    return evaluator.resolve(raw)


# --- Scrubbing ----------------------------------------------------------------


def _scrub_exchange(exchange: Exchange, secrets: list[str]) -> Exchange:
    # A secret sent in a query string or a path is percent-encoded there.
    forms = {
        form
        for secret in secrets
        if secret
        for form in (secret, quote(secret, safe=""), quote_plus(secret))
    }
    # Longest first, so a secret is not half-replaced by a shorter one inside it.
    secrets = sorted(forms, key=len, reverse=True)
    request = exchange.request
    scrubbed_request = replace(
        request,
        url=_scrub(request.url, secrets),
        headers=executor.redact_headers(_scrub(request.headers, secrets)),
        query_params=_scrub(request.query_params, secrets),
        path_params=_scrub(request.path_params, secrets),
        body=_scrub(request.body, secrets),
    )
    scrubbed_result = replace(
        exchange.result,
        headers=executor.redact_headers(_scrub(exchange.result.headers, secrets)),
        body=_scrub(exchange.result.body, secrets),
        error=_scrub(exchange.result.error, secrets),
    )
    return replace(exchange, request=scrubbed_request, result=scrubbed_result)


def _scrub(value: Any, secrets: list[str]) -> Any:
    """Replace every occurrence of a secret, however deep, with a placeholder."""
    if isinstance(value, str):
        for secret in secrets:
            if secret:
                value = value.replace(secret, executor.REDACTED)
        return value
    if isinstance(value, dict):
        return {key: _scrub(item, secrets) for key, item in value.items()}
    if isinstance(value, list):
        return [_scrub(item, secrets) for item in value]
    return value


def _statuses(codes: tuple[int, ...]) -> str:
    return " or ".join(str(code) for code in codes)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _show(value: Any) -> str:
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, str):
        return repr(value)
    return str(value)
