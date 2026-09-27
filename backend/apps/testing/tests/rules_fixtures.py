"""A leave-management API and a rules file for it, shared by the rules tests.

Not named test_*.py on purpose, so pytest imports it rather than collecting it.

The fake API keeps real state — leaves, approvals, an accrual rate that
changes — and computes a balance from it. Each deliberate defect is switched
on by a flag, so a test can prove the runner catches exactly that defect and
nothing else.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date, timedelta

import httpx

from apps.testing.rules.timeline import Period, append_period, days, sum_over_periods

TODAY = date(2026, 9, 27)
TOKEN = "tok-9f8e7d6c"
PASSWORD = "s3cret-pass"
SETUP = {"employee_id": "42", "username": "asha", "password": PASSWORD}

LEAVE_RULES = """
version: 1
timezone: Asia/Kolkata

setup:
  employee_id: {}
  username: {}
  password: {secret: true}

variables:
  leave_start: ${today - 4d}

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
        body: {start: "${leave_start}", end: "${today}"}
        expect: 201
        save: {leave_id: $.id}
      - name: approve
        method: POST
        path: /leaves/{leave_id}/approve
        expect: 200
      - name: raise_rate
        method: PUT
        path: /employees/{employee_id}/accrual
        body: {rate: 5}
        expect: 200
        timeline:
          accrual: {from: today, rate: 5}
    rules:
      - name: leave_counts_both_ends
        severity: medium
        why: A leave from the 23rd to the 27th is five days; both ends count.
        after: [apply]
        actual: $.days
        expected: days(leave_start, today)
      - name: balance_is_accrual_minus_taken
        severity: high
        why: The balance must equal the leave accrued so far minus the leave taken.
        after: [approve, raise_rate]
        fetch: {method: GET, path: "/employees/{employee_id}/balance"}
        actual: $.balance
        expected: sum_over_periods(accrual) - days(leave_start, today)
    cleanup:
      - name: delete_leave
        method: DELETE
        path: /leaves/{leave_id}
        expect: 204
"""


@dataclass
class FakeLeaveApi:
    """An in-process leave API. Pass ``handle`` to httpx.MockTransport."""

    today: date = TODAY
    # Defects, each off by default.
    retroactive_rate: bool = False  # applies the current rate to every day accrued
    exclusive_days: bool = False  # counts a leave's days without its last day
    approve_status: int = 200
    # Defaults to the same periods LEAVE_RULES describes, relative to `today`.
    periods: list[Period] | None = None
    leaves: dict[int, dict] = field(default_factory=dict)
    requests: list[httpx.Request] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.periods is None:
            self.periods = [
                Period(
                    start=self.today - timedelta(days=14),
                    end=self.today - timedelta(days=11),
                    rate=2,
                ),
                Period(start=self.today - timedelta(days=10), end=None, rate=4),
            ]

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path.removeprefix("/api")
        body = json.loads(request.content) if request.content else None

        if path == "/auth/login" and request.method == "POST":
            if body == {"username": "asha", "password": PASSWORD}:
                return httpx.Response(200, json={"access_token": TOKEN, "expires_in": 3600})
            return httpx.Response(401, json={"error": "bad credentials"})

        if request.headers.get("Authorization") != f"Bearer {TOKEN}":
            return httpx.Response(401, json={"error": "unauthorised"})

        if match := re.fullmatch(r"/employees/(\w+)/leaves", path):
            count = days(body["start"], body["end"]) - (1 if self.exclusive_days else 0)
            leave_id = 100 + len(self.leaves) + 1
            self.leaves[leave_id] = {"id": leave_id, "days": count, "status": "pending"}
            return httpx.Response(201, json=self.leaves[leave_id])

        if match := re.fullmatch(r"/leaves/(\d+)/approve", path):
            leave = self.leaves.get(int(match.group(1)))
            if leave is None:
                return httpx.Response(404)
            if self.approve_status != 200:
                return httpx.Response(self.approve_status, json={"error": "cannot approve"})
            leave["status"] = "approved"
            return httpx.Response(200, json=leave)

        if match := re.fullmatch(r"/employees/(\w+)/accrual", path):
            self.periods = append_period(self.periods, self.today, body["rate"])
            return httpx.Response(200, json={"rate": body["rate"]})

        if match := re.fullmatch(r"/employees/(\w+)/balance", path):
            return httpx.Response(200, json={"balance": self._balance()})

        if match := re.fullmatch(r"/leaves/(\d+)", path):
            if self.leaves.pop(int(match.group(1)), None) is None:
                return httpx.Response(404)
            return httpx.Response(204)

        return httpx.Response(404)

    def _balance(self) -> float:
        if self.retroactive_rate:
            accrued_days = days(self.periods[0].start, self.today)
            accrued = accrued_days * self.periods[-1].rate
        else:
            accrued, _ = sum_over_periods(self.periods, self.today)
        taken = sum(leave["days"] for leave in self.leaves.values() if leave["status"] == "approved")
        return accrued - taken

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handle), follow_redirects=False)
