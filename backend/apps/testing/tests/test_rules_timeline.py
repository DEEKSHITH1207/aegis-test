"""Date helpers and quantities that change over time."""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from apps.testing.rules.timeline import (
    Period,
    TimelineError,
    append_period,
    as_date,
    days,
    parse_periods,
    sum_over_periods,
)

TODAY = date(2026, 9, 27)


def test_days_counts_both_ends() -> None:
    assert days(TODAY, TODAY) == 1
    assert days(TODAY - timedelta(days=4), TODAY) == 5
    assert days("2026-02-27", "2026-03-01") == 3  # across a month end, non-leap year


def test_days_accepts_dates_datetimes_and_iso_strings() -> None:
    assert days(datetime(2026, 9, 1, 23, 59), "2026-09-02T00:00:00Z") == 2


def test_a_range_that_ends_before_it_starts_is_an_error() -> None:
    with pytest.raises(TimelineError, match="before it starts"):
        days(TODAY, TODAY - timedelta(days=1))


def test_a_value_that_is_not_a_date_is_an_error() -> None:
    with pytest.raises(TimelineError, match="Not a date"):
        as_date("next tuesday")


def test_sum_over_periods_shows_its_working() -> None:
    # The example from the specification: 4 days at 2, then 11 days at 4.
    periods = [
        Period(start=TODAY - timedelta(days=14), end=TODAY - timedelta(days=11), rate=2),
        Period(start=TODAY - timedelta(days=10), end=None, rate=4),
    ]

    total, working = sum_over_periods(periods, TODAY)

    assert total == 52
    assert working == "4 days × 2 + 11 days × 4 = 52"


def test_an_open_period_runs_to_until_inclusive() -> None:
    total, _ = sum_over_periods([Period(start=TODAY, end=None, rate=3)], TODAY)

    assert total == 3


def test_a_period_that_has_not_started_counts_nothing() -> None:
    periods = [Period(start=TODAY + timedelta(days=1), end=None, rate=5)]

    assert sum_over_periods(periods, TODAY) == (0, "no period has started = 0")


def test_a_period_with_an_end_counts_in_full_even_past_until() -> None:
    # The specification's definition: days(from, to) × rate. A schedule's
    # total covers its future days too.
    periods = [Period(start=TODAY - timedelta(days=4), end=TODAY + timedelta(days=3), rate=2)]

    total, working = sum_over_periods(periods, TODAY)

    assert total == 16
    assert working == "8 days × 2 = 16"


def test_a_closed_period_entirely_in_the_future_counts_in_full() -> None:
    periods = [Period(start=TODAY + timedelta(days=1), end=TODAY + timedelta(days=2), rate=3)]

    assert sum_over_periods(periods, TODAY) == (6, "2 days × 3 = 6")


def test_fractional_rates() -> None:
    total, working = sum_over_periods([Period(start=TODAY, end=TODAY, rate=1.5)], TODAY)

    assert total == 1.5
    assert working == "1 day × 1.5 = 1.5"


def test_appending_a_period_closes_the_open_one_the_day_before() -> None:
    periods = [Period(start=TODAY - timedelta(days=10), end=None, rate=2)]

    result = append_period(periods, TODAY, 4)

    assert result == [
        Period(start=TODAY - timedelta(days=10), end=TODAY - timedelta(days=1), rate=2),
        Period(start=TODAY, end=None, rate=4),
    ]
    # The original list is not modified.
    assert periods[0].end is None


def test_a_change_inside_a_closed_period_splits_it_and_keeps_its_end() -> None:
    schedule = [Period(start=TODAY - timedelta(days=4), end=TODAY + timedelta(days=3), rate=2)]

    result = append_period(schedule, TODAY, 4)

    assert result == [
        Period(start=TODAY - timedelta(days=4), end=TODAY - timedelta(days=1), rate=2),
        Period(start=TODAY, end=TODAY + timedelta(days=3), rate=4),
    ]
    assert sum_over_periods(result, TODAY) == (24, "4 days × 2 + 4 days × 4 = 24")


def test_a_change_on_a_periods_first_day_re_rates_the_whole_period() -> None:
    result = append_period([Period(start=TODAY, end=TODAY + timedelta(days=3), rate=2)], TODAY, 5)

    assert result == [Period(start=TODAY, end=TODAY + timedelta(days=3), rate=5)]


def test_a_change_after_the_last_period_ended_opens_a_new_one() -> None:
    result = append_period([Period(start=TODAY - timedelta(days=9), end=TODAY - timedelta(days=5), rate=2)], TODAY, 4)

    assert result[-1] == Period(start=TODAY, end=None, rate=4)


def test_appending_before_the_last_period_began_is_an_error() -> None:
    with pytest.raises(TimelineError, match="before it began"):
        append_period([Period(start=TODAY, end=None, rate=2)], TODAY - timedelta(days=5), 4)


def test_a_rate_must_be_a_number() -> None:
    with pytest.raises(TimelineError, match="rate"):
        append_period([], TODAY, "four")


def test_parse_periods_resolves_each_value() -> None:
    resolved = {"start": TODAY - timedelta(days=3), "end": TODAY - timedelta(days=2)}

    periods = parse_periods(
        [{"from": "start", "to": "end", "rate": 2}, {"from": "2026-09-26", "rate": 1}],
        resolve=lambda value: resolved.get(value, value),
    )

    assert periods == [
        Period(start=TODAY - timedelta(days=3), end=TODAY - timedelta(days=2), rate=2),
        Period(start=date(2026, 9, 26), end=None, rate=1),
    ]


def test_parse_periods_rejects_overlaps() -> None:
    with pytest.raises(TimelineError, match="before the previous one has ended"):
        parse_periods(
            [{"from": "2026-09-01", "to": "2026-09-10", "rate": 1}, {"from": "2026-09-10", "rate": 2}],
            resolve=lambda value: value,
        )
