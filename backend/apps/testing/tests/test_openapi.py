"""Parser tests. Pure functions, so none of these need a database."""

from __future__ import annotations

import pytest

from apps.testing import openapi
from apps.testing.models import SpecificationSourceType


def document(**overrides) -> dict:
    """A minimal valid OpenAPI 3 document, with overrides merged in."""
    base = {
        "openapi": "3.0.3",
        "info": {"title": "Example API", "version": "1.0.0"},
        "paths": {},
    }
    base.update(overrides)
    return base


# --- Document validation ------------------------------------------------------


@pytest.mark.parametrize(
    "bad, expected",
    [
        ([], "must be a JSON object"),
        ("openapi", "must be a JSON object"),
        (None, "must be a JSON object"),
        (42, "must be a JSON object"),
        ({}, "empty"),
    ],
)
def test_a_document_that_is_not_an_object_is_rejected(bad, expected: str) -> None:
    with pytest.raises(openapi.SpecificationParseError) as excinfo:
        openapi.parse(bad)

    assert expected in str(excinfo.value)


def test_a_document_without_a_version_key_is_rejected() -> None:
    with pytest.raises(openapi.SpecificationParseError) as excinfo:
        openapi.parse({"info": {"title": "x"}, "paths": {}})

    assert "must declare a version" in str(excinfo.value)


@pytest.mark.parametrize("version", ["2.0", "4.0.0", "1.0"])
def test_an_unsupported_openapi_major_version_is_rejected(version: str) -> None:
    with pytest.raises(openapi.SpecificationParseError) as excinfo:
        openapi.parse({"openapi": version, "paths": {}})

    assert "Unsupported OpenAPI version" in str(excinfo.value)


def test_an_unsupported_swagger_version_is_rejected() -> None:
    with pytest.raises(openapi.SpecificationParseError) as excinfo:
        openapi.parse({"swagger": "1.2", "paths": {}})

    assert "Unsupported Swagger version" in str(excinfo.value)


def test_a_document_without_paths_is_rejected() -> None:
    with pytest.raises(openapi.SpecificationParseError) as excinfo:
        openapi.parse({"openapi": "3.0.3"})

    assert "no 'paths' object" in str(excinfo.value)


def test_paths_that_are_not_an_object_are_rejected() -> None:
    with pytest.raises(openapi.SpecificationParseError) as excinfo:
        openapi.parse({"openapi": "3.0.3", "paths": []})

    assert "'paths' must be a JSON object" in str(excinfo.value)


def test_a_valid_document_with_no_paths_parses_to_nothing() -> None:
    parsed = openapi.parse(document())

    assert parsed.operations == []
    assert parsed.source_type == SpecificationSourceType.OPENAPI
    assert parsed.title == "Example API"
    assert parsed.api_version == "1.0.0"


def test_swagger_2_is_recognised() -> None:
    parsed = openapi.parse({"swagger": "2.0", "info": {"title": "Legacy"}, "paths": {}})

    assert parsed.source_type == SpecificationSourceType.SWAGGER
    assert parsed.title == "Legacy"


# --- Operations ---------------------------------------------------------------


def test_operations_are_extracted_with_their_metadata() -> None:
    parsed = openapi.parse(
        document(
            paths={
                "/users": {
                    "get": {
                        "operationId": "listUsers",
                        "summary": "List users",
                        "description": "Returns every user.",
                    },
                    "post": {"operationId": "createUser"},
                }
            }
        )
    )

    by_method = {operation.method: operation for operation in parsed.operations}
    assert set(by_method) == {"GET", "POST"}
    assert by_method["GET"].path == "/users"
    assert by_method["GET"].operation_id == "listUsers"
    assert by_method["GET"].summary == "List users"
    assert by_method["GET"].description == "Returns every user."
    assert by_method["POST"].summary == ""


@pytest.mark.parametrize("method", ["get", "post", "put", "patch", "delete", "head", "options"])
def test_every_executable_method_is_extracted(method: str) -> None:
    parsed = openapi.parse(document(paths={"/things": {method: {}}}))

    assert [operation.method for operation in parsed.operations] == [method.upper()]


def test_uppercase_method_keys_are_accepted() -> None:
    parsed = openapi.parse(document(paths={"/things": {"GET": {}}}))

    assert [operation.method for operation in parsed.operations] == ["GET"]


def test_trace_is_reported_as_skipped_rather_than_dropped() -> None:
    parsed = openapi.parse(document(paths={"/things": {"trace": {}, "get": {}}}))

    assert [operation.method for operation in parsed.operations] == ["GET"]
    assert [(s.method, s.path) for s in parsed.skipped] == [("TRACE", "/things")]
    assert "cannot execute" in parsed.skipped[0].reason


def test_path_level_metadata_is_not_treated_as_an_operation() -> None:
    parsed = openapi.parse(
        document(
            paths={
                "/users": {
                    "summary": "User collection",
                    "description": "...",
                    "servers": [{"url": "https://example.com"}],
                    "parameters": [],
                    "x-internal": True,
                    "get": {},
                }
            }
        )
    )

    assert len(parsed.operations) == 1
    assert parsed.skipped == []


def test_a_path_not_starting_with_a_slash_is_skipped() -> None:
    parsed = openapi.parse(document(paths={"users": {"get": {}}}))

    assert parsed.operations == []
    assert "must start with '/'" in parsed.skipped[0].reason


def test_an_overlong_path_is_skipped_because_it_cannot_be_truncated() -> None:
    long_path = "/" + "a" * 600

    parsed = openapi.parse(document(paths={long_path: {"get": {}}}))

    assert parsed.operations == []
    assert "exceeds 500 characters" in parsed.skipped[0].reason


def test_a_path_item_that_is_not_an_object_is_skipped() -> None:
    parsed = openapi.parse(document(paths={"/users": "nonsense"}))

    assert parsed.operations == []
    assert "Path item must be an object" in parsed.skipped[0].reason


def test_an_operation_that_is_not_an_object_is_skipped() -> None:
    parsed = openapi.parse(document(paths={"/users": {"get": "nonsense"}}))

    assert parsed.operations == []
    assert "Operation must be an object" in parsed.skipped[0].reason


def test_overlong_text_fields_are_truncated_with_a_warning() -> None:
    parsed = openapi.parse(
        document(paths={"/users": {"get": {"summary": "s" * 600, "operationId": "o" * 300}}})
    )

    operation = parsed.operations[0]
    assert len(operation.summary) == 500
    assert len(operation.operation_id) == 255
    assert any("summary truncated" in warning for warning in parsed.warnings)
    assert any("operationId truncated" in warning for warning in parsed.warnings)


# --- Parameters ---------------------------------------------------------------


def test_path_level_parameters_are_inherited_by_every_operation() -> None:
    parsed = openapi.parse(
        document(
            paths={
                "/users/{id}": {
                    "parameters": [{"name": "id", "in": "path", "required": True}],
                    "get": {"parameters": [{"name": "expand", "in": "query"}]},
                    "delete": {},
                }
            }
        )
    )

    by_method = {operation.method: operation for operation in parsed.operations}
    assert [p["name"] for p in by_method["GET"].parameters] == ["id", "expand"]
    assert [p["name"] for p in by_method["DELETE"].parameters] == ["id"]


def test_an_operation_parameter_overrides_the_inherited_one() -> None:
    parsed = openapi.parse(
        document(
            paths={
                "/users/{id}": {
                    "parameters": [{"name": "id", "in": "path", "required": False}],
                    "get": {"parameters": [{"name": "id", "in": "path", "required": True}]},
                }
            }
        )
    )

    parameters = parsed.operations[0].parameters
    assert len(parameters) == 1
    assert parameters[0]["required"] is True


def test_parameters_differing_only_by_location_are_both_kept() -> None:
    parsed = openapi.parse(
        document(
            paths={
                "/users": {
                    "get": {
                        "parameters": [
                            {"name": "token", "in": "query"},
                            {"name": "token", "in": "header"},
                        ]
                    }
                }
            }
        )
    )

    assert len(parsed.operations[0].parameters) == 2


def test_referenced_parameters_are_inlined() -> None:
    parsed = openapi.parse(
        document(
            paths={"/users/{id}": {"get": {"parameters": [{"$ref": "#/components/parameters/Id"}]}}},
            components={
                "parameters": {
                    "Id": {"name": "id", "in": "path", "required": True, "schema": {"type": "integer"}}
                }
            },
        )
    )

    parameter = parsed.operations[0].parameters[0]
    assert parameter["name"] == "id"
    assert parameter["schema"] == {"type": "integer"}


# --- Schemas ------------------------------------------------------------------


def test_request_and_response_schemas_are_extracted_and_dereferenced() -> None:
    parsed = openapi.parse(
        document(
            paths={
                "/users": {
                    "post": {
                        "requestBody": {
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/NewUser"}
                                }
                            }
                        },
                        "responses": {
                            "201": {
                                "content": {
                                    "application/json": {
                                        "schema": {"$ref": "#/components/schemas/User"}
                                    }
                                }
                            }
                        },
                    }
                }
            },
            components={
                "schemas": {
                    "NewUser": {"type": "object", "properties": {"name": {"type": "string"}}},
                    "User": {
                        "type": "object",
                        "properties": {"id": {"type": "integer"}, "name": {"type": "string"}},
                    },
                }
            },
        )
    )

    operation = parsed.operations[0]
    assert operation.request_schema == {
        "type": "object",
        "properties": {"name": {"type": "string"}},
    }
    assert operation.response_schema["properties"]["id"] == {"type": "integer"}


def test_the_lowest_success_response_is_chosen() -> None:
    parsed = openapi.parse(
        document(
            paths={
                "/users": {
                    "get": {
                        "responses": {
                            "404": {"content": {"application/json": {"schema": {"title": "err"}}}},
                            "201": {"content": {"application/json": {"schema": {"title": "b"}}}},
                            "200": {"content": {"application/json": {"schema": {"title": "a"}}}},
                        }
                    }
                }
            }
        )
    )

    assert parsed.operations[0].response_schema == {"title": "a"}


def test_the_default_response_is_used_when_there_is_no_2xx() -> None:
    parsed = openapi.parse(
        document(
            paths={
                "/users": {
                    "get": {
                        "responses": {
                            "default": {
                                "content": {"application/json": {"schema": {"title": "fallback"}}}
                            }
                        }
                    }
                }
            }
        )
    )

    assert parsed.operations[0].response_schema == {"title": "fallback"}


def test_an_operation_without_schemas_stores_none_rather_than_an_empty_object() -> None:
    parsed = openapi.parse(document(paths={"/users": {"get": {"responses": {"204": {}}}}}))

    operation = parsed.operations[0]
    assert operation.request_schema is None
    assert operation.response_schema is None


def test_a_json_suffixed_media_type_is_preferred_over_a_non_json_one() -> None:
    parsed = openapi.parse(
        document(
            paths={
                "/users": {
                    "get": {
                        "responses": {
                            "200": {
                                "content": {
                                    "text/csv": {"schema": {"title": "csv"}},
                                    "application/hal+json": {"schema": {"title": "hal"}},
                                }
                            }
                        }
                    }
                }
            }
        )
    )

    assert parsed.operations[0].response_schema == {"title": "hal"}


def test_a_parameterised_json_media_type_is_recognised() -> None:
    parsed = openapi.parse(
        document(
            paths={
                "/users": {
                    "get": {
                        "responses": {
                            "200": {
                                "content": {
                                    "application/json; charset=utf-8": {"schema": {"title": "j"}}
                                }
                            }
                        }
                    }
                }
            }
        )
    )

    assert parsed.operations[0].response_schema == {"title": "j"}


def test_swagger_2_body_parameter_becomes_the_request_schema() -> None:
    parsed = openapi.parse(
        {
            "swagger": "2.0",
            "info": {"title": "Legacy", "version": "1.0"},
            "paths": {
                "/users": {
                    "post": {
                        "parameters": [
                            {"name": "body", "in": "body", "schema": {"$ref": "#/definitions/User"}}
                        ],
                        "responses": {"200": {"schema": {"$ref": "#/definitions/User"}}},
                    }
                }
            },
            "definitions": {"User": {"type": "object", "properties": {"id": {"type": "integer"}}}},
        }
    )

    operation = parsed.operations[0]
    assert operation.request_schema == {"type": "object", "properties": {"id": {"type": "integer"}}}
    assert operation.response_schema == {"type": "object", "properties": {"id": {"type": "integer"}}}


# --- Reference resolution -----------------------------------------------------


def test_nested_references_are_resolved_recursively() -> None:
    parsed = openapi.parse(
        document(
            paths={
                "/users": {
                    "get": {
                        "responses": {
                            "200": {
                                "content": {
                                    "application/json": {"schema": {"$ref": "#/components/schemas/List"}}
                                }
                            }
                        }
                    }
                }
            },
            components={
                "schemas": {
                    "List": {"type": "array", "items": {"$ref": "#/components/schemas/User"}},
                    "User": {"type": "object", "properties": {"id": {"type": "integer"}}},
                }
            },
        )
    )

    schema = parsed.operations[0].response_schema
    assert schema["items"]["properties"]["id"] == {"type": "integer"}


def test_a_circular_reference_terminates_and_keeps_the_ref() -> None:
    parsed = openapi.parse(
        document(
            paths={
                "/nodes": {
                    "get": {
                        "responses": {
                            "200": {
                                "content": {
                                    "application/json": {"schema": {"$ref": "#/components/schemas/Node"}}
                                }
                            }
                        }
                    }
                }
            },
            components={
                "schemas": {
                    "Node": {
                        "type": "object",
                        "properties": {
                            "child": {"$ref": "#/components/schemas/Node"},
                            "name": {"type": "string"},
                        },
                    }
                }
            },
        )
    )

    schema = parsed.operations[0].response_schema
    assert schema["properties"]["name"] == {"type": "string"}
    # The cycle stops by leaving the reference in place rather than recursing.
    assert schema["properties"]["child"] == {"$ref": "#/components/schemas/Node"}


def test_a_dangling_reference_is_kept_and_warned_about() -> None:
    parsed = openapi.parse(
        document(
            paths={
                "/users": {
                    "get": {
                        "responses": {
                            "200": {
                                "content": {
                                    "application/json": {"schema": {"$ref": "#/components/schemas/Missing"}}
                                }
                            }
                        }
                    }
                }
            }
        )
    )

    assert parsed.operations[0].response_schema == {"$ref": "#/components/schemas/Missing"}
    assert any("could not be resolved" in warning for warning in parsed.warnings)


def test_an_external_reference_is_never_fetched() -> None:
    parsed = openapi.parse(
        document(
            paths={
                "/users": {
                    "get": {
                        "responses": {
                            "200": {
                                "content": {
                                    "application/json": {
                                        "schema": {"$ref": "https://example.com/schema.json#/User"}
                                    }
                                }
                            }
                        }
                    }
                }
            }
        )
    )

    assert parsed.operations[0].response_schema == {
        "$ref": "https://example.com/schema.json#/User"
    }
    assert any("External reference" in warning for warning in parsed.warnings)


def test_keywords_beside_a_ref_override_the_target() -> None:
    parsed = openapi.parse(
        document(
            paths={
                "/users": {
                    "get": {
                        "responses": {
                            "200": {
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "$ref": "#/components/schemas/User",
                                            "description": "A single user.",
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
            },
            components={"schemas": {"User": {"type": "object", "description": "generic"}}},
        )
    )

    schema = parsed.operations[0].response_schema
    assert schema["type"] == "object"
    assert schema["description"] == "A single user."


def test_a_json_pointer_may_index_into_a_list() -> None:
    parsed = openapi.parse(
        document(
            paths={
                "/users": {
                    "get": {"parameters": [{"$ref": "#/components/x/0"}]},
                }
            },
            components={"x": [{"name": "first", "in": "query"}]},
        )
    )

    assert parsed.operations[0].parameters[0]["name"] == "first"


def test_escaped_tokens_in_a_json_pointer_are_decoded() -> None:
    parsed = openapi.parse(
        document(
            paths={
                "/users": {
                    "get": {"parameters": [{"$ref": "#/components/parameters/a~1b"}]},
                }
            },
            components={"parameters": {"a/b": {"name": "slashy", "in": "query"}}},
        )
    )

    assert parsed.operations[0].parameters[0]["name"] == "slashy"
