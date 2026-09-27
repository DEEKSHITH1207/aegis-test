"""Reading a business-rules file (YAML, version 1).

The generator can only find what a schema forbids. A business rule — "the
leave balance equals what accrued minus what was taken" — lives outside any
schema, so the user writes it down in a rules file: scenarios of requests to
make, and assertions to check after them.

This module turns that file into frozen dataclasses and checks everything that
can be checked without running it: structure, names, step references, and the
syntax of every expression and JSON path. Like ``openapi.parse`` it reports
every problem at once, each with its location, rather than stopping at the
first. It is pure: no database and no network.

A minimal file::

    version: 1
    timezone: Asia/Kolkata
    setup:
      employee_id: {env: AEGIS_EMPLOYEE_ID}
    scenarios:
      - name: leave_balance
        steps:
          - name: apply
            method: POST
            path: /employees/{employee_id}/leaves
            body: {start: "${today - 4d}", end: "${today}"}
            expect: 201
            save: {leave_id: $.id}
        rules:
          - name: five_days_taken
            severity: high
            why: A leave from today-4 to today is five days.
            after: [apply]
            actual: $.days
            expected: days(today - 4d, today)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

from apps.testing.models import (
    MAX_HTTP_STATUS_CODE,
    MIN_HTTP_STATUS_CODE,
    FindingSeverity,
    HTTPMethod,
)
from apps.testing.rules import expressions

SUPPORTED_VERSION = 1

# The largest rules file accepted. Generous for hand-written scenarios, and a
# bound on what one upload can make the parser do.
MAX_DOCUMENT_BYTES = 256 * 1024

_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

COMPARISONS = {
    "equals": "==",
    "not_equals": "!=",
    "less_than": "<",
    "less_or_equal": "<=",
    "greater_than": ">",
    "greater_or_equal": ">=",
}

# The step name a rule's `after` uses for "every step of the scenario".
EVERY_STEP = "*"


class RulesFileError(ValueError):
    """A rules file that cannot be used. ``errors`` lists every problem."""

    def __init__(self, errors: list[str]) -> None:
        super().__init__("; ".join(errors))
        self.errors = errors


@dataclass(frozen=True)
class SetupValue:
    """A value the user supplies at run time, such as an ID or a password."""

    name: str
    # The environment variable the command reads it from.
    env: str
    # Scrubbed from every recorded request and response. Defaults to true for
    # a name containing password, token, secret or key.
    secret: bool = False
    description: str = ""


@dataclass(frozen=True)
class RequestSpec:
    """A request, before its templates are rendered."""

    method: str
    path: str
    query: dict[str, Any] = field(default_factory=dict)
    headers: dict[str, Any] = field(default_factory=dict)
    # None means no body is sent.
    body: Any = None


@dataclass(frozen=True)
class AuthSpec:
    """A login request whose response carries the token for every other request."""

    request: RequestSpec
    token_path: str
    header: str = "Authorization"
    scheme: str = "Bearer"


@dataclass(frozen=True)
class PeriodChange:
    """A step's effect on a timeline: from this date on, this rate."""

    timeline: str
    start: Any
    rate: Any


@dataclass(frozen=True)
class Step:
    name: str
    request: RequestSpec
    # Empty means any status is accepted.
    expect: tuple[int, ...] = ()
    # Variable name -> JSON path into this step's response body.
    save: dict[str, str] = field(default_factory=dict)
    periods: tuple[PeriodChange, ...] = ()


@dataclass(frozen=True)
class Rule:
    name: str
    severity: str
    why: str
    # Step names after which the rule is checked; EVERY_STEP for all of them.
    after: tuple[str, ...]
    # A JSON path into the response, or an expression.
    actual: str
    expected: Any
    compare: str = "equals"
    # For numbers compared with `equals`: how far apart still counts as equal.
    tolerance: float | None = None
    # Requests made just for this rule, each bound to a name its expressions
    # can read. A single unnamed `fetch` is bound to `response`, so a JSON-path
    # `actual` reads it.
    fetches: tuple[tuple[str, RequestSpec], ...] = ()

    def applies_after(self, step_name: str) -> bool:
        return EVERY_STEP in self.after or step_name in self.after


@dataclass(frozen=True)
class Scenario:
    name: str
    steps: tuple[Step, ...]
    rules: tuple[Rule, ...] = ()
    # Run after the steps whatever happened, e.g. to delete what was created.
    cleanup: tuple[Step, ...] = ()
    description: str = ""
    variables: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RulesDocument:
    version: int
    timezone: str
    scenarios: tuple[Scenario, ...]
    variables: dict[str, Any] = field(default_factory=dict)
    timelines: dict[str, list[Any]] = field(default_factory=dict)
    setup: tuple[SetupValue, ...] = ()
    auth: AuthSpec | None = None

    @property
    def secret_names(self) -> frozenset[str]:
        return frozenset(value.name for value in self.setup if value.secret)


# --- Loading ------------------------------------------------------------------


def load(text: str | bytes) -> RulesDocument:
    """Parse and check a rules file, or raise RulesFileError listing every problem."""
    if isinstance(text, bytes):
        if len(text) > MAX_DOCUMENT_BYTES:
            raise RulesFileError([f"The rules file is larger than {MAX_DOCUMENT_BYTES} bytes."])
        text = text.decode("utf-8", errors="replace")
    if len(text.encode("utf-8")) > MAX_DOCUMENT_BYTES:
        raise RulesFileError([f"The rules file is larger than {MAX_DOCUMENT_BYTES} bytes."])

    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise RulesFileError([f"The rules file is not valid YAML: {exc}"]) from exc

    return _Parser().document(raw)


class _Parser:
    """Walks the raw YAML once, collecting every problem it finds."""

    def __init__(self) -> None:
        self.errors: list[str] = []

    def error(self, where: str, message: str) -> None:
        self.errors.append(f"{where}: {message}" if where else message)

    # --- Top level --------------------------------------------------------------

    def document(self, raw: Any) -> RulesDocument:
        if not isinstance(raw, dict):
            raise RulesFileError(["The rules file must be a mapping at the top level."])

        self.unknown_keys(
            raw, {"version", "timezone", "variables", "timelines", "setup", "auth", "scenarios"}, ""
        )

        version = raw.get("version")
        if version != SUPPORTED_VERSION:
            self.error("version", f"must be {SUPPORTED_VERSION}, found {version!r}.")

        timezone = raw.get("timezone", "UTC")
        if not isinstance(timezone, str) or not _is_timezone(timezone):
            self.error("timezone", f"{timezone!r} is not a known time zone, e.g. Asia/Kolkata.")
            timezone = "UTC"

        # Setup values exist before anything else is evaluated, then variables
        # in the order written, then timelines, which may use both.
        setup = self.setup(raw.get("setup"))
        setup_names = {value.name for value in setup}
        variables = self.variables(raw.get("variables"), "variables", setup_names)
        timelines = self.timelines(raw.get("timelines"), {*setup_names, *variables})

        global_names = {*variables, *timelines, *setup_names}
        for name in _duplicates([*variables, *timelines, *(value.name for value in setup)]):
            self.error("", f"'{name}' is defined more than once across variables, timelines and setup.")

        auth = self.auth(raw.get("auth"), global_names)
        scenarios = self.scenarios(raw.get("scenarios"), global_names, set(timelines))

        if self.errors:
            raise RulesFileError(self.errors)

        return RulesDocument(
            version=SUPPORTED_VERSION,
            timezone=timezone,
            scenarios=scenarios,
            variables=variables,
            timelines=timelines,
            setup=setup,
            auth=auth,
        )

    def variables(self, raw: Any, where: str, known: set[str]) -> dict[str, Any]:
        """Named values, each a literal or a template. Later ones may use earlier ones."""
        if raw is None:
            return {}
        if not isinstance(raw, dict):
            self.error(where, "must be a mapping of names to values.")
            return {}
        result: dict[str, Any] = {}
        for name, value in raw.items():
            if self.name(name, f"{where}.{name}"):
                self.templates(value, f"{where}.{name}", {*known, *result})
                result[name] = value
        return result

    def timelines(self, raw: Any, known: set[str]) -> dict[str, list[Any]]:
        if raw is None:
            return {}
        if not isinstance(raw, dict):
            self.error("timelines", "must be a mapping of names to lists of periods.")
            return {}
        result: dict[str, list[Any]] = {}
        for name, periods in raw.items():
            where = f"timelines.{name}"
            if not self.name(name, where):
                continue
            if not isinstance(periods, list):
                self.error(where, "must be a list of periods ({from, to, rate}).")
                continue
            for index, period in enumerate(periods):
                self.period(period, f"{where}[{index}]", known, closing_allowed=True)
            result[name] = periods
        return result

    def period(self, raw: Any, where: str, known: set[str], *, closing_allowed: bool) -> bool:
        """Check a {from, to, rate} mapping. Each value is a literal, an
        expression such as `today - 4d`, or a template."""
        allowed = {"from", "to", "rate"} if closing_allowed else {"from", "rate"}
        if not isinstance(raw, dict):
            self.error(where, f"must be a mapping with {', '.join(sorted(allowed))}.")
            return False
        self.unknown_keys(raw, allowed, where)
        ok = True
        for key in ("from", "rate"):
            if key not in raw:
                self.error(where, f"'{key}' is required.")
                ok = False
        for key in sorted(allowed & raw.keys()):
            value = raw[key]
            if isinstance(value, str) and "${" not in value:
                ok = self.expression(value, f"{where}.{key}", known) and ok
            else:
                problems = len(self.errors)
                self.templates(value, f"{where}.{key}", known)
                ok = ok and len(self.errors) == problems
        return ok

    def setup(self, raw: Any) -> tuple[SetupValue, ...]:
        if raw is None:
            return ()
        if not isinstance(raw, dict):
            self.error("setup", "must be a mapping of names to {env, secret, description}.")
            return ()
        values: list[SetupValue] = []
        for name, options in raw.items():
            where = f"setup.{name}"
            if not self.name(name, where):
                continue
            options = {} if options is None else options
            if not isinstance(options, dict):
                self.error(where, "must be a mapping with optional env, secret and description.")
                continue
            self.unknown_keys(options, {"env", "secret", "description"}, where)
            env = options.get("env", f"AEGIS_{name.upper()}")
            if not isinstance(env, str) or not _NAME.match(env):
                self.error(f"{where}.env", f"{env!r} is not a valid environment variable name.")
                continue
            # A credential whose flag was forgotten must not leak into the
            # evidence, so the name decides when the file does not. An
            # explicit `secret: false` still wins, for a name like `keyboard_id`.
            secret = options.get("secret", _looks_secret(name))
            if not isinstance(secret, bool):
                self.error(f"{where}.secret", "must be true or false.")
                continue
            values.append(
                SetupValue(
                    name=name,
                    env=env,
                    secret=secret,
                    description=str(options.get("description") or ""),
                )
            )
        return tuple(values)

    def auth(self, raw: Any, known: set[str]) -> AuthSpec | None:
        if raw is None:
            return None
        if not isinstance(raw, dict):
            self.error("auth", "must be a mapping.")
            return None
        self.unknown_keys(
            raw, {"method", "path", "query", "headers", "body", "token", "header", "scheme"}, "auth"
        )
        request = self.request(raw, "auth", known)
        token = raw.get("token")
        if not expressions.is_json_path(token):
            self.error("auth.token", "must be a JSON path to the token, e.g. $.access_token.")
        else:
            self.json_path(token, "auth.token")
        header = raw.get("header", "Authorization")
        scheme = raw.get("scheme", "Bearer")
        ok = request is not None and expressions.is_json_path(token)
        if not isinstance(header, str) or not header:
            self.error("auth.header", "must be a header name.")
            ok = False
        if not isinstance(scheme, str):
            self.error("auth.scheme", "must be a string; use \"\" to send the token alone.")
            ok = False
        if not ok:
            return None
        return AuthSpec(request=request, token_path=token, header=header, scheme=scheme)

    # --- Scenarios --------------------------------------------------------------

    def scenarios(
        self, raw: Any, global_names: set[str], timelines: set[str]
    ) -> tuple[Scenario, ...]:
        if not isinstance(raw, list) or not raw:
            self.error("scenarios", "must be a non-empty list.")
            return ()
        result: list[Scenario] = []
        names: list[str] = []
        for index, entry in enumerate(raw):
            scenario = self.scenario(entry, index, global_names, timelines)
            if scenario is not None:
                result.append(scenario)
                names.append(scenario.name)
        for name in _duplicates(names):
            self.error("scenarios", f"more than one scenario is named '{name}'.")
        return tuple(result)

    def scenario(
        self, raw: Any, index: int, global_names: set[str], timelines: set[str]
    ) -> Scenario | None:
        where = f"scenarios[{index}]"
        if not isinstance(raw, dict):
            self.error(where, "must be a mapping.")
            return None
        name = raw.get("name")
        if isinstance(name, str) and name:
            where = f"scenario '{name}'"
        else:
            self.error(where, "'name' is required.")
            name = f"scenario_{index + 1}"
        self.unknown_keys(
            raw, {"name", "description", "variables", "steps", "rules", "cleanup"}, where
        )

        variables = self.variables(raw.get("variables"), f"{where}.variables", global_names)
        for shadowed in set(variables) & global_names:
            self.error(f"{where}.variables", f"'{shadowed}' is already defined for the whole file.")
        known = {*global_names, *variables}

        steps = self.steps(raw.get("steps"), f"{where}.steps", known, timelines, required=True)
        saved = {saved_name for step in steps for saved_name in step.save}
        step_names = {step.name for step in steps}

        rules = self.rules(raw.get("rules"), where, {*known, *saved}, step_names)
        cleanup = self.steps(
            raw.get("cleanup"), f"{where}.cleanup", {*known, *saved}, timelines, required=False
        )

        return Scenario(
            name=name,
            description=str(raw.get("description") or ""),
            variables=variables,
            steps=steps,
            rules=rules,
            cleanup=cleanup,
        )

    def steps(
        self, raw: Any, where: str, known: set[str], timelines: set[str], *, required: bool
    ) -> tuple[Step, ...]:
        if raw is None and not required:
            return ()
        if not isinstance(raw, list) or (required and not raw):
            self.error(where, "must be a non-empty list of steps." if required else "must be a list.")
            return ()

        result: list[Step] = []
        available = set(known)
        for index, entry in enumerate(raw):
            step = self.step(entry, f"{where}[{index}]", available, timelines)
            if step is None:
                continue
            result.append(step)
            # A value saved by a step is usable from the next step on.
            available |= set(step.save)
        for name in _duplicates([step.name for step in result]):
            self.error(where, f"more than one step is named '{name}'.")
        return tuple(result)

    def step(
        self, raw: Any, where: str, known: set[str], timelines: set[str]
    ) -> Step | None:
        if not isinstance(raw, dict):
            self.error(where, "must be a mapping.")
            return None
        name = raw.get("name")
        if not isinstance(name, str) or not _NAME.match(name):
            self.error(where, "'name' is required and must be a simple identifier.")
            return None
        where = f"{where} ('{name}')"
        self.unknown_keys(
            raw,
            {"name", "method", "path", "query", "headers", "body", "expect", "save", "timeline"},
            where,
        )

        request = self.request(raw, where, known)
        expect = self.expect(raw.get("expect"), f"{where}.expect")
        save = self.save(raw.get("save"), f"{where}.save", known)
        periods = self.period_changes(raw.get("timeline"), f"{where}.timeline", known, timelines)
        if request is None:
            return None
        return Step(name=name, request=request, expect=expect, save=save, periods=periods)

    def request(self, raw: dict[str, Any], where: str, known: set[str]) -> RequestSpec | None:
        method = raw.get("method")
        path = raw.get("path")
        ok = True
        if not isinstance(method, str) or method.upper() not in HTTPMethod.values:
            self.error(f"{where}.method", f"must be one of {', '.join(HTTPMethod.values)}.")
            ok = False
        if not isinstance(path, str) or not path.startswith("/"):
            self.error(f"{where}.path", "must be a path starting with '/'.")
            ok = False
        else:
            for placeholder in re.findall(r"\{([^{}]+)\}", path):
                if placeholder not in known:
                    self.error(f"{where}.path", f"'{{{placeholder}}}' is not a defined variable.")

        query = raw.get("query") or {}
        headers = raw.get("headers") or {}
        for key, value in (("query", query), ("headers", headers)):
            if not isinstance(value, dict):
                self.error(f"{where}.{key}", "must be a mapping.")
                ok = False
        body = raw.get("body")
        for part, value in (("query", query), ("headers", headers), ("body", body)):
            self.templates(value, f"{where}.{part}", known)

        if not ok:
            return None
        return RequestSpec(
            method=method.upper(), path=path, query=query, headers=headers, body=body
        )

    def expect(self, raw: Any, where: str) -> tuple[int, ...]:
        if raw is None:
            return ()
        codes = raw if isinstance(raw, list) else [raw]
        result: list[int] = []
        for code in codes:
            if (
                isinstance(code, bool)
                or not isinstance(code, int)
                or not MIN_HTTP_STATUS_CODE <= code <= MAX_HTTP_STATUS_CODE
            ):
                self.error(where, f"{code!r} is not an HTTP status code.")
            else:
                result.append(code)
        return tuple(result)

    def save(self, raw: Any, where: str, known: set[str]) -> dict[str, str]:
        if raw is None:
            return {}
        if not isinstance(raw, dict):
            self.error(where, "must map variable names to JSON paths.")
            return {}
        result: dict[str, str] = {}
        for name, path in raw.items():
            if not self.name(name, f"{where}.{name}"):
                continue
            if not expressions.is_json_path(path):
                self.error(f"{where}.{name}", "must be a JSON path such as $.id.")
                continue
            if self.json_path(path, f"{where}.{name}"):
                result[name] = path
        return result

    def period_changes(
        self, raw: Any, where: str, known: set[str], timelines: set[str]
    ) -> tuple[PeriodChange, ...]:
        if raw is None:
            return ()
        if not isinstance(raw, dict):
            self.error(where, "must map timeline names to {from, rate}.")
            return ()
        changes: list[PeriodChange] = []
        for name, change in raw.items():
            if name not in timelines:
                self.error(where, f"'{name}' is not a timeline defined under timelines.")
                continue
            if self.period(change, f"{where}.{name}", known, closing_allowed=False):
                changes.append(PeriodChange(timeline=name, start=change["from"], rate=change["rate"]))
        return tuple(changes)

    # --- Rules ------------------------------------------------------------------

    def rules(
        self, raw: Any, where: str, known: set[str], step_names: set[str]
    ) -> tuple[Rule, ...]:
        if raw is None:
            return ()
        if not isinstance(raw, list):
            self.error(f"{where}.rules", "must be a list.")
            return ()
        result: list[Rule] = []
        for index, entry in enumerate(raw):
            rule = self.rule(entry, f"{where}.rules[{index}]", known, step_names)
            if rule is not None:
                result.append(rule)
        for name in _duplicates([rule.name for rule in result]):
            self.error(f"{where}.rules", f"more than one rule is named '{name}'.")
        return tuple(result)

    def rule(
        self, raw: Any, where: str, known: set[str], step_names: set[str]
    ) -> Rule | None:
        if not isinstance(raw, dict):
            self.error(where, "must be a mapping.")
            return None
        name = raw.get("name")
        if not isinstance(name, str) or not name:
            self.error(where, "'name' is required.")
            return None
        where = f"rule '{name}'"
        self.unknown_keys(
            raw,
            {"name", "severity", "why", "after", "fetch", "actual", "expected", "compare", "tolerance"},
            where,
        )
        ok = True

        severity = raw.get("severity")
        if severity not in FindingSeverity.values:
            self.error(f"{where}.severity", f"must be one of {', '.join(FindingSeverity.values)}.")
            ok = False
        why = raw.get("why")
        if not isinstance(why, str) or not why.strip():
            self.error(f"{where}.why", "is required: say in plain English why the rule must hold.")
            ok = False

        after = raw.get("after", [EVERY_STEP])
        after = [after] if isinstance(after, str) else after
        if not isinstance(after, list) or not after:
            self.error(f"{where}.after", "must be a list of step names, or omitted for every step.")
            ok = False
            after = []
        for step in after:
            if step != EVERY_STEP and step not in step_names:
                self.error(f"{where}.after", f"'{step}' is not a step of this scenario.")
                ok = False

        fetches = self.fetches(raw.get("fetch"), f"{where}.fetch", known)
        if fetches is None:
            ok = False
            fetches = ()
        readable = {*known, "response", *(fetch_name for fetch_name, _ in fetches)}

        actual = raw.get("actual")
        if not isinstance(actual, str) or not actual.strip():
            self.error(f"{where}.actual", "is required: a JSON path or an expression.")
            ok = False
        elif expressions.is_json_path(actual):
            ok = self.json_path(actual, f"{where}.actual") and ok
        else:
            ok = self.expression(actual, f"{where}.actual", readable) and ok

        if "expected" not in raw:
            self.error(f"{where}.expected", "is required.")
            ok = False
        elif isinstance(raw["expected"], str):
            ok = self.expression(raw["expected"], f"{where}.expected", readable) and ok

        compare = raw.get("compare", "equals")
        if compare not in COMPARISONS:
            self.error(f"{where}.compare", f"must be one of {', '.join(COMPARISONS)}.")
            ok = False

        tolerance = raw.get("tolerance")
        if tolerance is not None:
            if isinstance(tolerance, bool) or not isinstance(tolerance, (int, float)) or tolerance < 0:
                self.error(f"{where}.tolerance", "must be a number of zero or more.")
                ok = False
            elif compare != "equals":
                self.error(f"{where}.tolerance", "only applies to compare: equals.")
                ok = False

        if not ok:
            return None
        return Rule(
            name=name,
            severity=severity,
            why=why.strip(),
            after=tuple(after),
            actual=actual.strip(),
            expected=raw["expected"],
            compare=compare,
            tolerance=float(tolerance) if tolerance is not None else None,
            fetches=fetches,
        )

    def fetches(
        self, raw: Any, where: str, known: set[str]
    ) -> tuple[tuple[str, RequestSpec], ...] | None:
        """A rule's follow-up requests, or None when they are unusable.

        Either one request ({method, path, ...}), read as `response`, or a
        mapping of names to requests, each read by its name:
        ``{history: {method: GET, ...}, schedule: {method: GET, ...}}``.
        """
        if raw is None:
            return ()
        request_keys = {"method", "path", "query", "headers"}
        if not isinstance(raw, dict) or not raw:
            self.error(where, "must be a request ({method, path}) or a mapping of names to requests.")
            return None
        if "method" in raw or "path" in raw:
            self.unknown_keys(raw, request_keys, where)
            spec = self.request(raw, where, known)
            return None if spec is None else (("response", spec),)

        fetches: list[tuple[str, RequestSpec]] = []
        ok = True
        for fetch_name, request in raw.items():
            at = f"{where}.{fetch_name}"
            if not self.name(fetch_name, at):
                ok = False
                continue
            if fetch_name in known:
                self.error(at, f"'{fetch_name}' is already a variable; name the request differently.")
                ok = False
                continue
            if not isinstance(request, dict):
                self.error(at, "must be a request mapping with method and path.")
                ok = False
                continue
            self.unknown_keys(request, request_keys, at)
            spec = self.request(request, at, known)
            if spec is None:
                ok = False
            else:
                fetches.append((fetch_name, spec))
        return tuple(fetches) if ok else None

    # --- Shared checks ----------------------------------------------------------

    def name(self, name: Any, where: str) -> bool:
        if not isinstance(name, str) or not _NAME.match(name):
            self.error(where, "names must be simple identifiers: letters, digits and _.")
            return False
        if name in expressions.RESERVED_NAMES:
            self.error(where, f"'{name}' is reserved for a built-in.")
            return False
        return True

    def templates(self, value: Any, where: str, known: set[str] | None = None) -> None:
        for problem in expressions.template_problems(value):
            self.error(where, problem)
        if known is not None:
            try:
                names = expressions.template_names(value)
            except expressions.ExpressionError:
                return
            for missing in sorted(names - known):
                self.error(where, f"'{missing}' is not a defined variable.")

    def expression(self, value: str, where: str, known: set[str]) -> bool:
        problem = expressions.check_syntax(value)
        if problem:
            self.error(where, problem)
            return False
        missing = sorted(expressions.expression_names(value) - known)
        for name in missing:
            self.error(where, f"'{name}' is not a defined variable.")
        return not missing

    def json_path(self, path: str, where: str) -> bool:
        problem = expressions.check_json_path(path)
        if problem:
            self.error(where, problem)
            return False
        return True

    def unknown_keys(self, raw: dict[str, Any], allowed: set[str], where: str) -> None:
        for key in raw:
            if key not in allowed:
                self.error(where, f"unknown key '{key}'. Expected one of: {', '.join(sorted(allowed))}.")


# Setup names treated as secret unless the file says `secret: false`.
_SECRET_NAME_MARKERS = ("password", "token", "secret", "key")


def _looks_secret(name: str) -> bool:
    lowered = name.lower()
    return any(marker in lowered for marker in _SECRET_NAME_MARKERS)


def _is_timezone(name: str) -> bool:
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return False
    return True


def _duplicates(names: list[str]) -> list[str]:
    seen: set[str] = set()
    repeated: list[str] = []
    for name in names:
        if name in seen and name not in repeated:
            repeated.append(name)
        seen.add(name)
    return repeated
