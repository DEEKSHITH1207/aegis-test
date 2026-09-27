"""Loading and checking a business-rules file."""

from __future__ import annotations

import pytest

from apps.testing.rules import document
from apps.testing.rules.document import EVERY_STEP, RulesFileError, load

# A leave-management API: leave accrues at 2 days a month, then 4 from a rate
# change; taking leave reduces the balance. Kept here, not in fixtures.py, so
# the loader's tests read top to bottom.
LEAVE_RULES = """
version: 1
timezone: Asia/Kolkata

setup:
  employee_id: {env: AEGIS_EMPLOYEE_ID, description: An employee in the test tenant}
  username: {}
  password: {secret: true}

variables:
  leave_start: ${today - 4d}
  leave_days: ${days(leave_start, today)}

timelines:
  accrual:
    - {from: today - 14d, to: today - 11d, rate: 2}
    - {from: today - 10d, rate: 4}

auth:
  method: POST
  path: /auth/login
  body: {username: "${username}", password: "${password}"}
  token: $.access_token

scenarios:
  - name: leave_balance
    description: Taking leave reduces the balance by exactly the days taken.
    steps:
      - name: apply
        method: POST
        path: /employees/{employee_id}/leaves
        body: {start: "${leave_start}", end: "${today}", reason: sick}
        expect: 201
        save: {leave_id: $.id}
      - name: approve
        method: POST
        path: /leaves/{leave_id}/approve
        expect: [200, 204]
      - name: raise_rate
        method: PUT
        path: /employees/{employee_id}/accrual
        body: {rate: 4}
        expect: 200
        timeline:
          accrual: {from: today, rate: 5}
    rules:
      - name: leave_counts_inclusive_days
        severity: medium
        why: A leave from the 23rd to the 27th is five days, both ends included.
        after: [apply]
        actual: $.days
        expected: leave_days
      - name: balance_is_accrual_minus_taken
        severity: high
        why: The balance must equal what accrued minus the leave taken.
        after: [approve, raise_rate]
        fetch: {method: GET, path: "/employees/{employee_id}/balance"}
        actual: $.balance
        expected: sum_over_periods(accrual) - leave_days
        tolerance: 0.01
      - name: never_negative
        severity: low
        why: A balance below zero is not allowed.
        actual: response['balance'] if 'balance' in response else 0
        expected: 0
        compare: greater_or_equal
    cleanup:
      - name: delete_leave
        method: DELETE
        path: /leaves/{leave_id}
"""


def _errors(text: str) -> list[str]:
    with pytest.raises(RulesFileError) as excinfo:
        load(text)
    return excinfo.value.errors


def _minimal(**replace: str) -> str:
    parts = {
        "head": "version: 1",
        "steps": """
    steps:
      - name: get
        method: GET
        path: /health
        expect: 200""",
        "rules": "",
    }
    parts.update(replace)
    return f"""{parts['head']}
scenarios:
  - name: s{parts['steps']}{parts['rules']}
"""


# --- A complete file ----------------------------------------------------------


def test_a_complete_file_loads() -> None:
    rules = load(LEAVE_RULES)

    assert rules.version == 1
    assert rules.timezone == "Asia/Kolkata"
    assert [value.name for value in rules.setup] == ["employee_id", "username", "password"]
    assert rules.setup[0].env == "AEGIS_EMPLOYEE_ID"
    assert rules.setup[1].env == "AEGIS_USERNAME"
    assert rules.secret_names == {"password"}
    assert list(rules.variables) == ["leave_start", "leave_days"]
    assert rules.auth.token_path == "$.access_token"
    assert rules.auth.header == "Authorization" and rules.auth.scheme == "Bearer"

    (scenario,) = rules.scenarios
    assert [step.name for step in scenario.steps] == ["apply", "approve", "raise_rate"]
    apply, approve, raise_rate = scenario.steps
    assert apply.expect == (201,)
    assert approve.expect == (200, 204)
    assert apply.save == {"leave_id": "$.id"}
    assert raise_rate.periods[0].timeline == "accrual"
    assert [cleanup.name for cleanup in scenario.cleanup] == ["delete_leave"]


def test_rules_know_when_they_apply() -> None:
    (scenario,) = load(LEAVE_RULES).scenarios
    inclusive, balance, never_negative = scenario.rules

    assert inclusive.applies_after("apply") and not inclusive.applies_after("approve")
    assert balance.fetch.path == "/employees/{employee_id}/balance"
    assert balance.tolerance == 0.01
    assert never_negative.after == (EVERY_STEP,)
    assert all(never_negative.applies_after(step.name) for step in scenario.steps)
    assert never_negative.compare == "greater_or_equal"


# --- Problems are reported, all at once ---------------------------------------


def test_invalid_yaml() -> None:
    assert "not valid YAML" in _errors("version: [1")[0]


def test_the_top_level_must_be_a_mapping() -> None:
    assert "mapping" in _errors("- 1\n- 2")[0]


def test_an_unsupported_version() -> None:
    assert any("version" in error for error in _errors(_minimal(head="version: 2")))


def test_an_unknown_timezone() -> None:
    errors = _errors(_minimal(head="version: 1\ntimezone: Mars/Olympus"))

    assert any("Mars/Olympus" in error for error in errors)


def test_every_problem_is_reported_together() -> None:
    errors = _errors(
        """
version: 3
timezone: Nowhere/Nothing
scenarios:
  - name: s
    steps:
      - name: bad
        method: FETCH
        path: no-slash
        expect: 999
    rules:
      - name: r
        severity: catastrophic
        after: [ghost]
        actual: $.[[
        expected: 1
"""
    )

    joined = "\n".join(errors)
    for fragment in ("version", "Nowhere", "method", "path", "999", "severity", "why", "ghost", "JSON path"):
        assert fragment in joined, fragment


def test_unknown_keys_are_reported() -> None:
    errors = _errors(
        _minimal(
            steps="""
    steps:
      - name: get
        method: GET
        path: /health
        expeted: 200"""
        )
    )

    assert any("unknown key 'expeted'" in error for error in errors)


def test_a_path_placeholder_must_be_defined() -> None:
    errors = _errors(
        _minimal(
            steps="""
    steps:
      - name: get
        method: GET
        path: /users/{user_id}"""
        )
    )

    assert any("'{user_id}' is not a defined variable" in error for error in errors)


def test_a_saved_value_is_only_available_to_later_steps() -> None:
    errors = _errors(
        _minimal(
            steps="""
    steps:
      - name: first
        method: GET
        path: /things/{thing_id}
      - name: second
        method: POST
        path: /things
        save: {thing_id: $.id}"""
        )
    )

    assert any("first" in error and "thing_id" in error for error in errors)


def test_an_undefined_name_in_an_expression_is_reported() -> None:
    errors = _errors(
        _minimal(
            rules="""
    rules:
      - name: r
        severity: low
        why: because
        actual: $.total
        expected: subtotal + tax"""
        )
    )

    assert any("'subtotal' is not a defined variable" in error for error in errors)
    assert any("'tax' is not a defined variable" in error for error in errors)


def test_a_malformed_expression_is_reported() -> None:
    errors = _errors(
        _minimal(
            rules="""
    rules:
      - name: r
        severity: low
        why: because
        actual: $.total
        expected: 1 +"""
        )
    )

    assert any("not a valid expression" in error for error in errors)


def test_a_reserved_name_cannot_be_defined() -> None:
    errors = _errors(_minimal(head="version: 1\nvariables:\n  today: 1"))

    assert any("reserved" in error for error in errors)


def test_a_name_defined_twice_is_reported() -> None:
    errors = _errors(_minimal(head="version: 1\nvariables:\n  x: 1\nsetup:\n  x: {}"))

    assert any("more than once" in error for error in errors)


def test_duplicate_step_names_are_reported() -> None:
    errors = _errors(
        _minimal(
            steps="""
    steps:
      - {name: get, method: GET, path: /a}
      - {name: get, method: GET, path: /b}"""
        )
    )

    assert any("more than one step is named 'get'" in error for error in errors)


def test_a_step_can_only_change_a_defined_timeline() -> None:
    errors = _errors(
        _minimal(
            steps="""
    steps:
      - name: get
        method: GET
        path: /a
        timeline:
          accrual: {from: today, rate: 4}"""
        )
    )

    assert any("'accrual' is not a timeline" in error for error in errors)


def test_tolerance_only_applies_to_equals() -> None:
    errors = _errors(
        _minimal(
            rules="""
    rules:
      - name: r
        severity: low
        why: because
        actual: $.total
        expected: 1
        compare: less_than
        tolerance: 0.5"""
        )
    )

    assert any("tolerance" in error for error in errors)


def test_auth_needs_a_token_path() -> None:
    errors = _errors(_minimal(head="version: 1\nauth:\n  method: POST\n  path: /login"))

    assert any("auth.token" in error for error in errors)


def test_an_oversized_file_is_refused() -> None:
    errors = _errors("#" * (document.MAX_DOCUMENT_BYTES + 1))

    assert "larger than" in errors[0]
