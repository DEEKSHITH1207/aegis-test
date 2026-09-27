"""Dates and quantities that change over time.

A business rule often depends on a rate that held for a while and then
changed: leave that accrues at 2 days a month, then at 4. The rules file
describes such a quantity as a list of periods, and the expected value is the
sum of each period's length times its rate.

Every function returns its result *and* the working that produced it, because
a finding that says "expected 52" is far less useful than one that says
"4 days × 2 + 11 days × 4 = 52".
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any


class TimelineError(ValueError):
    """A period, or a list of them, that cannot be summed as written."""


@dataclass(frozen=True)
class Period:
    """A stretch of days at one rate. ``end`` None means still open."""

    start: date
    end: date | None
    rate: int | float | Decimal

    def closed_at(self, until: date) -> date:
        return self.end if self.end is not None else until


def as_date(value: Any) -> date:
    """Accept a date, a datetime, or an ISO string such as '2026-01-31'."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value.strip()[:10])
        except ValueError:
            pass
    raise TimelineError(f"Not a date: {value!r}. Use YYYY-MM-DD.")


def days(start: Any, end: Any) -> int:
    """Days from ``start`` to ``end``, counting both: days(d, d) is 1.

    A range that ends before it starts is an error rather than zero or a
    negative count, because a rule built on it is certainly miswritten.
    """
    first, last = as_date(start), as_date(end)
    if last < first:
        raise TimelineError(f"A period cannot end ({last}) before it starts ({first}).")
    return (last - first).days + 1


def days_delta(count: int) -> timedelta:
    """What ``4d`` in an expression means: a span of that many days."""
    return timedelta(days=count)


def append_period(periods: list[Period], start: Any, rate: Any) -> list[Period]:
    """Return ``periods`` with a new open period from ``start``.

    The period still open, if any, is closed the day before ``start``, which is
    how a rate change reads in a rules file: "from today, the rate is 4".
    """
    begin = as_date(start)
    result = list(periods)
    if result and result[-1].end is None:
        previous = result[-1]
        close = begin - timedelta(days=1)
        if close < previous.start:
            raise TimelineError(
                f"A new period from {begin} would close the open one, which started "
                f"on {previous.start}, before it began."
            )
        result[-1] = Period(start=previous.start, end=close, rate=previous.rate)
    elif result and result[-1].end is not None and begin <= result[-1].end:
        raise TimelineError(
            f"A new period from {begin} overlaps the last one, which ends {result[-1].end}."
        )
    result.append(Period(start=begin, end=None, rate=_rate(rate)))
    return result


def sum_over_periods(periods: list[Period], until: Any) -> tuple[int | float | Decimal, str]:
    """Sum of days(start, end) × rate over every period, with the working.

    An open period runs to ``until`` — the run's "today" — inclusive. A period
    that starts after ``until`` has not begun and contributes nothing.
    """
    limit = as_date(until)
    total: int | float | Decimal = 0
    terms: list[str] = []
    for period in periods:
        if period.start > limit:
            continue
        end = min(period.closed_at(limit), limit)
        count = days(period.start, end)
        total += count * period.rate
        terms.append(f"{count} {'day' if count == 1 else 'days'} × {_number(period.rate)}")

    if not terms:
        return 0, "no period has started = 0"
    return total, f"{' + '.join(terms)} = {_number(total)}"


def parse_periods(raw: Any, *, resolve) -> list[Period]:
    """Build periods from the rules file's list of {from, to, rate} mappings.

    ``resolve`` turns a written value (which may be an expression such as
    "${today - 4d}") into a concrete one; the caller supplies it so this module
    does not depend on the expression engine.
    """
    if not isinstance(raw, list):
        raise TimelineError("A timeline must be a list of periods.")
    periods: list[Period] = []
    for index, entry in enumerate(raw):
        if not isinstance(entry, dict) or "from" not in entry or "rate" not in entry:
            raise TimelineError(f"Period {index + 1} needs 'from' and 'rate'.")
        start = as_date(resolve(entry["from"]))
        end = as_date(resolve(entry["to"])) if entry.get("to") is not None else None
        if end is not None and end < start:
            raise TimelineError(f"Period {index + 1} ends ({end}) before it starts ({start}).")
        if periods and (periods[-1].end is None or start <= periods[-1].end):
            raise TimelineError(
                f"Period {index + 1} starts on {start}, before the previous one has ended."
            )
        periods.append(Period(start=start, end=end, rate=_rate(resolve(entry["rate"]))))
    return periods


def _rate(value: Any) -> int | float | Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise TimelineError(f"A rate must be a number, not {value!r}.")
    return value


def _number(value: Any) -> str:
    """12.0 reads as 12 in an explanation; 12.5 stays 12.5."""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)
