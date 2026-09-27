"""A realistic specification used across the Phase 4 tests.

Not named test_*.py on purpose, so pytest imports it rather than collecting it.

It is deliberately broad: a GET with query parameters, a GET with a path
parameter, and a POST whose body exercises required fields, string length,
string format, an enum, numeric bounds, a nested object and an array — with
several documented response codes. One document, many kinds of test case.
"""

from __future__ import annotations

from typing import Any

REALISTIC_SPECIFICATION: dict[str, Any] = {
    "openapi": "3.0.3",
    "info": {"title": "Directory API", "version": "1.0.0"},
    "paths": {
        "/users": {
            "get": {
                "operationId": "listUsers",
                "summary": "List users",
                "parameters": [
                    {
                        "name": "page",
                        "in": "query",
                        "required": False,
                        "schema": {"type": "integer", "minimum": 1, "maximum": 1000},
                    },
                    {
                        "name": "limit",
                        "in": "query",
                        "required": True,
                        "schema": {"type": "integer", "minimum": 1, "maximum": 100},
                    },
                    {
                        "name": "status",
                        "in": "query",
                        "required": False,
                        "schema": {"type": "string", "enum": ["active", "inactive"]},
                    },
                ],
                "responses": {
                    "200": {"description": "OK"},
                    "400": {"description": "Bad request"},
                },
            },
            "post": {
                "operationId": "createUser",
                "summary": "Create a user",
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "required": ["name", "email", "age", "role"],
                                "properties": {
                                    "name": {"type": "string", "minLength": 3, "maxLength": 50},
                                    "email": {"type": "string", "format": "email"},
                                    "age": {"type": "integer", "minimum": 18, "maximum": 120},
                                    "role": {
                                        "type": "string",
                                        "enum": ["admin", "member", "guest"],
                                    },
                                    "score": {
                                        "type": "number",
                                        "exclusiveMinimum": 0,
                                        "exclusiveMaximum": 100,
                                    },
                                    "active": {"type": "boolean"},
                                    "tags": {
                                        "type": "array",
                                        "items": {"type": "string"},
                                        "minItems": 1,
                                        "maxItems": 5,
                                    },
                                    "profile": {
                                        "type": "object",
                                        "required": ["displayName"],
                                        "properties": {
                                            "displayName": {
                                                "type": "string",
                                                "minLength": 2,
                                            },
                                            "website": {"type": "string", "format": "uri"},
                                        },
                                    },
                                },
                            }
                        }
                    },
                },
                "responses": {
                    "201": {"description": "Created"},
                    "400": {"description": "Validation failed"},
                    "500": {"description": "Server error"},
                },
            },
        },
        "/users/{userId}": {
            "parameters": [
                {
                    "name": "userId",
                    "in": "path",
                    "required": True,
                    "schema": {"type": "integer", "minimum": 1},
                }
            ],
            "get": {
                "operationId": "getUser",
                "responses": {"200": {"description": "OK"}, "404": {"description": "Missing"}},
            },
            "delete": {
                "operationId": "deleteUser",
                "responses": {"204": {"description": "Gone"}},
            },
        },
    },
}
