"""Evaluating the expressions a rules file is written in.

Expressions are small Python-like formulas — ``today - 4d``,
``sum_over_periods(accrual) - taken``, ``days(start, end) * 2`` — evaluated by
simpleeval, never by eval(). The rules file is user input, and the evaluator
only offers names the file defined and the functions listed in ``_FUNCTIONS``:
no imports, no attribute that starts with an underscore, no lambdas.

Every evaluation returns the value *and* its working, so a broken rule can say
how the expected value was reached, not just what it was.

Two syntaxes appear in a rules file:

* An **expression** is a whole value: ``expected: sum_over_periods(accrual)``.
* A **template** is any other value — a body, a header, a query string — in
  which ``${...}`` is replaced by an expression's value. A string that is
  exactly one ``${...}`` keeps the value's type, so ``"${count}"`` sends the
  number 3, not the string "3". ``$${`` writes a literal ``${``.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from jsonpath_ng.exceptions import JsonPathLexerError, JsonPathParserError
from jsonpath_ng.ext import parse as parse_json_path
from simpleeval import EvalWithCompoundTypes, InvalidExpression

from apps.testing.rules import timeline


class ExpressionError(ValueError):
    """An expression that is malformed, or that failed when evaluated."""


@dataclass(frozen=True)
class Evaluation:
    value: Any
    # How the value was reached, e.g. "4 days × 2 + 11 days × 4 = 52".
    working: str


# `4d` means four days. Matched only outside string literals, and only as a
# whole token, so "4days" or a variable named d4 are untouched.
_DAY_LITERAL = re.compile(
    r"""(?P<string>"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')|(?<![\w.])(?P<count>\d+)d\b"""
)

# The functions an expression may call. Anything else is "not defined".
_FUNCTIONS: dict[str, Any] = {
    "days": timeline.days,
    "days_delta": timeline.days_delta,
    "date": timeline.as_date,
    "abs": abs,
    "min": min,
    "max": max,
    "round": round,
    "sum": sum,
    "len": len,
    "int": int,
    "float": float,
    "str": str,
}

# Names a rules file may not define, because an expression would then be
# unable to reach the built-in of the same name.
RESERVED_NAMES = frozenset({"today", "sum_over_periods", "response", *_FUNCTIONS})


def check_syntax(expression: str) -> str:
    """Why ``expression`` cannot be parsed, or "" when it can."""
    try:
        ast.parse(_expand_day_literals(expression).strip(), mode="eval")
    except SyntaxError as exc:
        return f"'{expression}' is not a valid expression ({exc.msg})."
    return ""


def is_json_path(value: Any) -> bool:
    return isinstance(value, str) and value.strip().startswith("$")


def check_json_path(path: str) -> str:
    """Why ``path`` is not a usable JSONPath, or "" when it is."""
    try:
        parse_json_path(path)
    except (JsonPathLexerError, JsonPathParserError, Exception) as exc:
        return f"'{path}' is not a valid JSON path ({exc})."
    return ""


def query(path: str, document: Any) -> Any:
    """The value at a JSONPath in a response body.

    One match is returned as itself, several as a list. No match is an error:
    a rule comparing against a field the response does not have is a finding
    in its own right, not a silent None.
    """
    problem = check_json_path(path)
    if problem:
        raise ExpressionError(problem)
    matches = [match.value for match in parse_json_path(path).find(document)]
    if not matches:
        raise ExpressionError(f"Nothing in the response matches {path}.")
    return matches[0] if len(matches) == 1 else matches


class Evaluator:
    """Evaluates expressions against one scope of variables.

    ``today`` is fixed when the evaluator is built, so every expression in a
    run sees the same date however long the run takes.
    """

    def __init__(self, variables: Mapping[str, Any] | None = None, *, today: date) -> None:
        self.today = today
        self.variables: dict[str, Any] = dict(variables or {})

    def set(self, name: str, value: Any) -> None:
        self.variables[name] = value

    def child(self) -> Evaluator:
        """A copy whose later changes do not leak back into this one."""
        return Evaluator(self.variables, today=self.today)

    # --- Expressions ------------------------------------------------------------

    def evaluate(self, expression: Any) -> Evaluation:
        """Evaluate one expression. A non-string is a literal and is returned as is."""
        if not isinstance(expression, str):
            return Evaluation(value=expression, working=_describe(expression))

        source = _expand_day_literals(expression).strip()
        trace: list[str] = []

        def sum_periods(periods: Any, until: Any = None) -> Any:
            if not isinstance(periods, list) or not all(
                isinstance(item, timeline.Period) for item in periods
            ):
                raise ExpressionError("sum_over_periods() needs a timeline defined in the file.")
            total, working = timeline.sum_over_periods(
                periods, until if until is not None else self.today
            )
            trace.append(working)
            return total

        engine = EvalWithCompoundTypes(
            names={**self.variables, "today": self.today},
            functions={**_FUNCTIONS, "sum_over_periods": sum_periods},
        )
        try:
            value = engine.eval(source)
        except timeline.TimelineError as exc:
            raise ExpressionError(f"{expression}: {exc}") from exc
        except ExpressionError:
            raise
        except (InvalidExpression, SyntaxError, ArithmeticError, TypeError, ValueError) as exc:
            raise ExpressionError(f"Could not evaluate '{expression}': {exc}") from exc
        except (KeyError, IndexError, AttributeError) as exc:
            raise ExpressionError(f"Could not evaluate '{expression}': no such item {exc}") from exc

        return Evaluation(value=value, working=self._working(source, value, trace))

    def _working(self, source: str, value: Any, trace: list[str]) -> str:
        parts = list(trace)
        for name in _referenced_names(source):
            if name in self.variables and not isinstance(self.variables[name], list):
                parts.append(f"{name} = {_describe(self.variables[name])}")
        result = f"{_describe(value)}"
        # "52 = 52" says nothing; show the expression only when it adds something.
        if source != result:
            result = f"{source} = {result}"
        parts.append(result)
        return "; ".join(parts)

    # --- Templates --------------------------------------------------------------

    def resolve(self, template: Any) -> Any:
        """Evaluate a template, keeping native values: a date stays a date.

        For values the file defines — a variable, a period's start — where a
        later expression may still do arithmetic on the result. ``render`` is
        for what goes on the wire.
        """
        if isinstance(template, str):
            pieces = _split_template(template)
            if len(pieces) == 1 and pieces[0][0] == "expression":
                return self.evaluate(pieces[0][1]).value
            return self._render_string(template)
        if isinstance(template, dict):
            return {str(key): self.resolve(value) for key, value in template.items()}
        if isinstance(template, list):
            return [self.resolve(item) for item in template]
        return template

    def render(self, template: Any) -> Any:
        """Replace every ``${...}`` in a value, recursively, with JSON-safe results."""
        if isinstance(template, str):
            return self._render_string(template)
        if isinstance(template, dict):
            return {str(key): self.render(value) for key, value in template.items()}
        if isinstance(template, list):
            return [self.render(item) for item in template]
        return to_json(template)

    def _render_string(self, template: str) -> Any:
        pieces = _split_template(template)
        if len(pieces) == 1 and pieces[0][0] == "expression":
            return to_json(self.evaluate(pieces[0][1]).value)
        rendered: list[str] = []
        for kind, text in pieces:
            if kind == "text":
                rendered.append(text)
            else:
                rendered.append(_as_text(to_json(self.evaluate(text).value)))
        return "".join(rendered)


def to_json(value: Any) -> Any:
    """A value an HTTP body can carry: dates become ISO strings."""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dict):
        return {str(key): to_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        if any(isinstance(item, timeline.Period) for item in value):
            raise ExpressionError("A timeline cannot be sent in a request; sum it first.")
        return [to_json(item) for item in value]
    return value


def template_problems(template: Any) -> list[str]:
    """Syntax problems in every ``${...}`` of a template, for up-front checking."""
    problems: list[str] = []
    if isinstance(template, str):
        try:
            pieces = _split_template(template)
        except ExpressionError as exc:
            return [str(exc)]
        for kind, text in pieces:
            if kind == "expression":
                problem = check_syntax(text)
                if problem:
                    problems.append(problem)
    elif isinstance(template, dict):
        for value in template.values():
            problems.extend(template_problems(value))
    elif isinstance(template, list):
        for item in template:
            problems.extend(template_problems(item))
    return problems


def template_names(template: Any) -> set[str]:
    """Every variable a template's expressions refer to."""
    names: set[str] = set()
    if isinstance(template, str):
        for kind, text in _split_template(template):
            if kind == "expression":
                names |= set(_referenced_names(_expand_day_literals(text).strip()))
    elif isinstance(template, dict):
        for value in template.values():
            names |= template_names(value)
    elif isinstance(template, list):
        for item in template:
            names |= template_names(item)
    return names


def expression_names(expression: Any) -> set[str]:
    if not isinstance(expression, str):
        return set()
    return set(_referenced_names(_expand_day_literals(expression).strip()))


# --- Internals ----------------------------------------------------------------


def _expand_day_literals(expression: str) -> str:
    def replace(match: re.Match[str]) -> str:
        if match.group("string") is not None:
            return match.group("string")
        return f"days_delta({match.group('count')})"

    return _DAY_LITERAL.sub(replace, expression)


def _split_template(template: str) -> list[tuple[str, str]]:
    """Split a string into ("text", ...) and ("expression", ...) pieces.

    Braces inside an expression are balanced, so ``${ {'a': 1}['a'] }`` works.
    """
    pieces: list[tuple[str, str]] = []
    text: list[str] = []
    index = 0
    while index < len(template):
        if template.startswith("$${", index):
            text.append("${")
            index += 3
            continue
        if template.startswith("${", index):
            depth, cursor = 1, index + 2
            while cursor < len(template) and depth:
                if template[cursor] == "{":
                    depth += 1
                elif template[cursor] == "}":
                    depth -= 1
                cursor += 1
            if depth:
                raise ExpressionError(f"Unclosed '${{' in '{template}'.")
            if text:
                pieces.append(("text", "".join(text)))
                text = []
            pieces.append(("expression", template[index + 2 : cursor - 1].strip()))
            index = cursor
            continue
        text.append(template[index])
        index += 1
    if text or not pieces:
        pieces.append(("text", "".join(text)))
    return pieces


def _referenced_names(source: str) -> list[str]:
    """Variables an expression reads, in the order they appear.

    Ordered, not a set: the working shown in a finding must read the same on
    every run, and set order changes with Python's hash seed.
    """
    try:
        tree = ast.parse(source, mode="eval")
    except SyntaxError:
        return []
    called = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    names = sorted(
        (
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Name) and node.id not in called and node.id != "today"
        ),
        key=lambda node: (node.lineno, node.col_offset),
    )
    return list(dict.fromkeys(node.id for node in names))


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _describe(value: Any) -> str:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, str):
        return repr(value)
    return str(value)
