"""A small stand-in for a NestJS status-report backend, with two planted bugs.

It behaves like the real service, including the data it already holds:

* Unit "MT" exists, and financial year 2025-26 exists.
* **Bug A** — POST /status-report/units no longer checks for a duplicate name
  before inserting, so the database's unique constraint throws and the API
  answers 500 instead of 409.
* **Bug B** — POST /status-report/financial-years should require
  endYear == startYear + 1. The check was loosened to endYear != startYear,
  so {"startYear": 2030, "endYear": 2020} is saved, named "2030-20".
* A financial year whose startYear exists is refused with 409 "Financial year
  already exists" — correct behaviour that the document does not list.

Everything else is implemented correctly, so any other finding against it is
noise the tool should not produce.
"""

from __future__ import annotations

import json
import re
from typing import Any

import httpx

BASE_URL = "http://status.test/api"

STATUS_REPORT_SPECIFICATION: dict[str, Any] = {
    "openapi": "3.0.3",
    "info": {"title": "Status report API", "version": "1.0.0"},
    "paths": {
        "/status-report/units": {
            "get": {
                "tags": ["Units"],
                "responses": {
                    "200": {
                        "description": "OK",
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "array",
                                    "items": {"$ref": "#/components/schemas/Unit"},
                                }
                            }
                        },
                    }
                },
            },
            "post": {
                "tags": ["Units"],
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {"$ref": "#/components/schemas/CreateUnitDto"}
                        }
                    },
                },
                "responses": {
                    "201": {
                        "description": "Created",
                        "content": {
                            "application/json": {"schema": {"$ref": "#/components/schemas/Unit"}}
                        },
                    },
                    "400": {"description": "Validation failed"},
                    "409": {"description": "A unit with this name exists"},
                },
            },
        },
        "/status-report/units/{id}": {
            "parameters": [
                {"name": "id", "in": "path", "required": True, "schema": {"type": "integer"}}
            ],
            "get": {
                "tags": ["Units"],
                "responses": {
                    "200": {
                        "description": "OK",
                        "content": {
                            "application/json": {"schema": {"$ref": "#/components/schemas/Unit"}}
                        },
                    },
                    "404": {"description": "Not found"},
                },
            },
            "delete": {
                "tags": ["Units"],
                "responses": {"200": {"description": "Deleted"}, "404": {"description": "Not found"}},
            },
        },
        "/status-report/financial-years": {
            "get": {
                "tags": ["Financial years"],
                "responses": {"200": {"description": "OK"}},
            },
            "post": {
                "tags": ["Financial years"],
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {"$ref": "#/components/schemas/CreateFinancialYearDto"}
                        }
                    },
                },
                # 409 is returned for an existing year, but not documented.
                "responses": {
                    "201": {
                        "description": "Created",
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/FinancialYear"}
                            }
                        },
                    },
                    "400": {"description": "Validation failed"},
                },
            },
        },
        "/status-report/financial-years/{id}": {
            "parameters": [
                {"name": "id", "in": "path", "required": True, "schema": {"type": "integer"}}
            ],
            "get": {
                "tags": ["Financial years"],
                "responses": {"200": {"description": "OK"}, "404": {"description": "Not found"}},
            },
            "delete": {
                "tags": ["Financial years"],
                "responses": {"200": {"description": "Deleted"}, "404": {"description": "Not found"}},
            },
        },
    },
    "components": {
        "schemas": {
            "CreateUnitDto": {
                "type": "object",
                "required": ["name"],
                "properties": {"name": {"type": "string", "maxLength": 100, "example": "MT"}},
            },
            "Unit": {
                "type": "object",
                "required": ["id", "name"],
                "properties": {"id": {"type": "integer"}, "name": {"type": "string"}},
            },
            "CreateFinancialYearDto": {
                "type": "object",
                "required": ["startYear", "endYear"],
                "properties": {
                    "startYear": {
                        "type": "integer",
                        "minimum": 2000,
                        "maximum": 2100,
                        "example": 2025,
                    },
                    "endYear": {
                        "type": "integer",
                        "minimum": 2000,
                        "maximum": 2100,
                        "example": 2026,
                    },
                },
            },
            "FinancialYear": {
                "type": "object",
                "required": ["id", "startYear", "endYear", "name"],
                "properties": {
                    "id": {"type": "integer"},
                    "startYear": {"type": "integer"},
                    "endYear": {"type": "integer"},
                    "name": {"type": "string"},
                },
            },
        }
    },
}


def _bad_request(*messages: str) -> httpx.Response:
    return httpx.Response(
        400, json={"statusCode": 400, "message": list(messages), "error": "Bad Request"}
    )


class StatusReportApi:
    """The service's state and request handling. Pass ``handle`` to MockTransport."""

    def __init__(self) -> None:
        self.units: dict[int, dict[str, Any]] = {1: {"id": 1, "name": "MT"}}
        self.years: dict[int, dict[str, Any]] = {
            1: {"id": 1, "startYear": 2025, "endYear": 2026, "name": "2025-26"}
        }
        self.next_id = 100
        self.deleted: list[str] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api")
        body = json.loads(request.content) if request.content else None

        if path == "/status-report/units":
            if request.method == "GET":
                return httpx.Response(200, json=list(self.units.values()))
            if request.method == "POST":
                return self._create_unit(body)
        if path == "/status-report/financial-years":
            if request.method == "GET":
                return httpx.Response(200, json=list(self.years.values()))
            if request.method == "POST":
                return self._create_year(body)

        match = re.fullmatch(r"/status-report/(units|financial-years)/([^/]+)", path)
        if match:
            table = self.units if match.group(1) == "units" else self.years
            if not match.group(2).isdigit():
                return _bad_request("Validation failed (numeric string is expected)")
            key = int(match.group(2))
            if key not in table:
                return httpx.Response(404, json={"statusCode": 404, "message": "Not Found"})
            if request.method == "GET":
                return httpx.Response(200, json=table[key])
            if request.method == "DELETE":
                del table[key]
                self.deleted.append(f"{match.group(1)}/{key}")
                return httpx.Response(200, json={"deleted": True})

        return httpx.Response(404, json={"statusCode": 404, "message": "Cannot route"})

    def _create_unit(self, body: Any) -> httpx.Response:
        if not isinstance(body, dict):
            return _bad_request("body must be an object")
        name = body.get("name")
        if not isinstance(name, str) or not name.strip():
            return _bad_request("name should not be empty", "name must be a string")
        if len(name) > 100:
            return _bad_request("name must be shorter than or equal to 100 characters")
        # Bug A: the duplicate check that used to be here was removed, so the
        # insert hits the unique index and the exception escapes.
        if any(unit["name"] == name for unit in self.units.values()):
            return httpx.Response(
                500, json={"statusCode": 500, "message": "Internal server error"}
            )
        unit = {"id": self._id(), "name": name}
        self.units[unit["id"]] = unit
        return httpx.Response(201, json=unit)

    def _create_year(self, body: Any) -> httpx.Response:
        if not isinstance(body, dict):
            return _bad_request("body must be an object")
        problems = []
        for field in ("startYear", "endYear"):
            value = body.get(field)
            if not isinstance(value, int) or isinstance(value, bool):
                problems.append(f"{field} must be an integer number")
            elif not 2000 <= value <= 2100:
                problems.append(f"{field} must be between 2000 and 2100")
        if problems:
            return _bad_request(*problems)
        start, end = body["startYear"], body["endYear"]
        # Bug B: this was `end != start + 1`.
        if end == start:
            return _bad_request("endYear must be after startYear")
        if any(year["startYear"] == start for year in self.years.values()):
            return httpx.Response(
                409,
                json={
                    "statusCode": 409,
                    "message": "Financial year already exists",
                    "error": "Conflict",
                },
            )
        year = {"id": self._id(), "startYear": start, "endYear": end, "name": f"{start}-{str(end)[-2:]}"}
        self.years[year["id"]] = year
        return httpx.Response(201, json=year)

    def _id(self) -> int:
        self.next_id += 1
        return self.next_id

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handle), follow_redirects=False)
