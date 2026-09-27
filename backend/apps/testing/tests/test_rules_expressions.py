"""The sandboxed expression evaluator, templates and JSON paths."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from apps.testing.rules.expressions import (
    Evaluator,
    ExpressionError,
    check_syntax,
    query,
    to_json,
)
from apps.testing.rules.timeline import Period

TODAY = date(2026, 9, 27)


def _evaluator(**variables) -> Evaluator:
    return Evaluator(variables, today=TODAY)


# --- Dates --------------------------------------------------------------------


def test_today_is_the_fixed_run_date() -> None:
    assert _evaluator().evaluate("today").value == TODAY


def test_day_literals_do_date_arithmetic() -> None:
    evaluator = _evaluator()

    assert evaluator.evaluate("today - 4d").value == date(2026, 9, 23)
    assert evaluator.evaluate("today + 10d").value == date(2026, 10, 7)


def test_day_literals_are_not_rewritten_inside_strings_or_names() -> None:
    evaluator = _evaluator(d4=1)

    assert evaluator.evaluate("'4d'").value == "4d"
    assert evaluator.evaluate("d4 + 1").value == 2


def test_days_between_two_expressions() -> None:
    assert _evaluator().evaluate("days(today - 4d, today)").value == 5


def test_date_parses_an_iso_string() -> None:
    assert _evaluator().evaluate("date('2026-01-31') + 1d").value == date(2026, 2, 1)


# --- Arithmetic and variables -------------------------------------------------


def test_variables_and_arithmetic() -> None:
    evaluation = _evaluator(opening=10, taken=3).evaluate("opening - taken * 2")

    assert evaluation.value == 4
    assert evaluation.working == "opening = 10; taken = 3; opening - taken * 2 = 4"


def test_a_literal_is_returned_as_is() -> None:
    assert _evaluator().evaluate(42).value == 42


def test_response_data_can_be_indexed() -> None:
    evaluator = _evaluator(response={"items": [{"days": 3}, {"days": 2}]})

    assert evaluator.evaluate("response['items'][1]['days']").value == 2
    assert evaluator.evaluate("sum([item['days'] for item in response['items']])").value == 5


def test_sum_over_periods_explains_the_expected_value() -> None:
    accrual = [
        Period(start=TODAY - timedelta(days=14), end=TODAY - timedelta(days=11), rate=2),
        Period(start=TODAY - timedelta(days=10), end=None, rate=4),
    ]

    evaluation = _evaluator(accrual=accrual, taken=5).evaluate("sum_over_periods(accrual) - taken")

    assert evaluation.value == 47
    assert evaluation.working == (
        "4 days × 2 + 11 days × 4 = 52; taken = 5; sum_over_periods(accrual) - taken = 47"
    )


def test_sum_over_periods_needs_a_timeline() -> None:
    with pytest.raises(ExpressionError, match="timeline"):
        _evaluator(x=[1, 2]).evaluate("sum_over_periods(x)")


# --- The sandbox --------------------------------------------------------------


@pytest.mark.parametrize(
    "expression",
    [
        "__import__('os').system('true')",
        "open('/etc/passwd')",
        "today.__class__",
        "().__class__.__bases__[0].__subclasses__()",
        "(lambda: 1)()",
        "exec('1')",
        "globals()",
    ],
)
def test_the_sandbox_refuses_escape_attempts(expression: str) -> None:
    with pytest.raises(ExpressionError):
        _evaluator().evaluate(expression)


def test_an_unknown_name_is_an_error() -> None:
    with pytest.raises(ExpressionError, match="missing"):
        _evaluator().evaluate("missing + 1")


def test_a_runtime_failure_is_an_expression_error() -> None:
    with pytest.raises(ExpressionError):
        _evaluator(x=1).evaluate("x / 0")
    with pytest.raises(ExpressionError, match="no such item"):
        _evaluator(response={}).evaluate("response['absent']")


def test_check_syntax() -> None:
    assert check_syntax("today - 4d") == ""
    assert "not a valid expression" in check_syntax("today -")


# --- Templates ----------------------------------------------------------------


def test_a_whole_placeholder_keeps_its_type() -> None:
    evaluator = _evaluator(count=3)

    assert evaluator.render("${count}") == 3
    assert evaluator.render("${today - 1d}") == "2026-09-26"


def test_placeholders_inside_text_are_interpolated() -> None:
    assert _evaluator(name="Asha").render("Hello ${name}, today is ${today}") == (
        "Hello Asha, today is 2026-09-27"
    )


def test_rendering_is_recursive_and_json_safe() -> None:
    rendered = _evaluator(n=2).render(
        {"start": "${today - 4d}", "days": "${n + 1}", "tags": ["${n}", "x"], "on": date(2026, 1, 1)}
    )

    assert rendered == {"start": "2026-09-23", "days": 3, "tags": [2, "x"], "on": "2026-01-01"}


def test_a_doubled_dollar_is_a_literal() -> None:
    assert _evaluator().render("cost: $${price}") == "cost: ${price}"


def test_braces_inside_a_placeholder_are_balanced() -> None:
    assert _evaluator().render("${ {'a': 1}['a'] }") == 1


def test_an_unclosed_placeholder_is_an_error() -> None:
    with pytest.raises(ExpressionError, match="Unclosed"):
        _evaluator().render("${today")


def test_a_timeline_cannot_be_sent() -> None:
    with pytest.raises(ExpressionError, match="timeline"):
        to_json([Period(start=TODAY, end=None, rate=1)])


# --- JSON paths ---------------------------------------------------------------


def test_query_returns_one_match_as_itself_and_several_as_a_list() -> None:
    body = {"balance": 12, "leaves": [{"days": 3}, {"days": 2}]}

    assert query("$.balance", body) == 12
    assert query("$.leaves[*].days", body) == [3, 2]
    assert query("$.leaves[?(@.days > 2)].days", body) == 3


def test_query_with_no_match_is_an_error() -> None:
    with pytest.raises(ExpressionError, match="Nothing in the response matches"):
        query("$.missing", {"balance": 1})


def test_query_with_a_malformed_path_is_an_error() -> None:
    with pytest.raises(ExpressionError, match="not a valid JSON path"):
        query("$.[[", {})


def test_resolve_keeps_native_values_for_later_arithmetic() -> None:
    evaluator = _evaluator()

    assert evaluator.resolve("${today - 4d}") == date(2026, 9, 23)
    assert evaluator.resolve({"n": "${1 + 1}", "label": "day ${1 + 1}"}) == {"n": 2, "label": "day 2"}


def test_the_working_lists_variables_in_the_order_they_appear() -> None:
    # Set order would vary with the hash seed; the working must not.
    evaluation = _evaluator(zeta=1, alpha=2, mid=3).evaluate("zeta + alpha + mid + zeta")

    assert evaluation.working == "zeta = 1; alpha = 2; mid = 3; zeta + alpha + mid + zeta = 7"
