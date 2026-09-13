"""Importing a parsed document into Endpoint rows, over the service and the API."""

from __future__ import annotations

from copy import deepcopy

import pytest
from django.core.exceptions import ValidationError
from django.urls import reverse
from rest_framework import status

from apps.testing import services
from apps.testing.models import (
    Endpoint,
    Specification,
    SpecificationSourceType,
    TestCase,
    TestRun,
)

pytestmark = pytest.mark.django_db


PETSTORE = {
    "openapi": "3.0.3",
    "info": {"title": "Example API", "version": "1.0.0"},
    "paths": {
        "/users": {
            "get": {
                "operationId": "listUsers",
                "summary": "List users",
                "responses": {
                    "200": {
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "array",
                                    "items": {"$ref": "#/components/schemas/User"},
                                }
                            }
                        }
                    }
                },
            },
            "post": {
                "operationId": "createUser",
                "requestBody": {
                    "content": {
                        "application/json": {"schema": {"$ref": "#/components/schemas/NewUser"}}
                    }
                },
                "responses": {"201": {"description": "Created"}},
            },
        },
        "/users/{userId}": {
            "parameters": [
                {"name": "userId", "in": "path", "required": True, "schema": {"type": "integer"}}
            ],
            "get": {"operationId": "getUser", "responses": {"200": {"description": "OK"}}},
            "delete": {"operationId": "deleteUser", "responses": {"204": {}}},
        },
    },
    "components": {
        "schemas": {
            "User": {
                "type": "object",
                "properties": {"id": {"type": "integer"}, "name": {"type": "string"}},
            },
            "NewUser": {"type": "object", "properties": {"name": {"type": "string"}}},
        }
    },
}


@pytest.fixture
def petstore(db) -> Specification:
    # Deep-copied: several tests edit the document to check re-import behaviour,
    # and they must not mutate the constant every other test relies on.
    return Specification.objects.create(
        name="Example API", version="1.0.0", raw_specification=deepcopy(PETSTORE)
    )


# --- Service ------------------------------------------------------------------


def test_import_creates_an_endpoint_per_operation(petstore: Specification) -> None:
    result = services.import_endpoints(specification=petstore)

    assert len(result.created) == 4
    assert result.updated == []
    assert result.unchanged == []
    assert {(e.path, e.method) for e in petstore.endpoints.all()} == {
        ("/users", "GET"),
        ("/users", "POST"),
        ("/users/{userId}", "GET"),
        ("/users/{userId}", "DELETE"),
    }


def test_imported_endpoints_carry_their_metadata(petstore: Specification) -> None:
    services.import_endpoints(specification=petstore)

    endpoint = petstore.endpoints.get(path="/users", method="GET")
    assert endpoint.operation_id == "listUsers"
    assert endpoint.summary == "List users"
    # The $ref is inlined, so the stored schema stands on its own.
    assert endpoint.response_schema["items"]["properties"]["name"] == {"type": "string"}


def test_path_level_parameters_reach_every_operation_under_the_path(
    petstore: Specification,
) -> None:
    services.import_endpoints(specification=petstore)

    for method in ("GET", "DELETE"):
        endpoint = petstore.endpoints.get(path="/users/{userId}", method=method)
        assert [p["name"] for p in endpoint.parameters] == ["userId"]


def test_importing_twice_changes_nothing_the_second_time(petstore: Specification) -> None:
    services.import_endpoints(specification=petstore)

    second = services.import_endpoints(specification=petstore)

    assert second.created == []
    assert second.updated == []
    assert len(second.unchanged) == 4
    assert Endpoint.objects.count() == 4


def test_a_changed_operation_is_updated_in_place(petstore: Specification) -> None:
    services.import_endpoints(specification=petstore)
    original = petstore.endpoints.get(path="/users", method="GET")

    petstore.raw_specification["paths"]["/users"]["get"]["summary"] = "List all users"
    petstore.save()
    result = services.import_endpoints(specification=petstore)

    assert len(result.updated) == 1
    assert len(result.unchanged) == 3
    original.refresh_from_db()
    assert original.summary == "List all users"
    # Updated in place, so anything referring to this endpoint still does.
    assert original.pk == result.updated[0].pk


def test_a_new_operation_is_added_without_disturbing_the_others(
    petstore: Specification,
) -> None:
    services.import_endpoints(specification=petstore)

    petstore.raw_specification["paths"]["/health"] = {"get": {"operationId": "health"}}
    petstore.save()
    result = services.import_endpoints(specification=petstore)

    assert len(result.created) == 1
    assert len(result.unchanged) == 4
    assert result.created[0].path == "/health"


def test_an_endpoint_dropped_from_the_document_is_reported_not_deleted(
    petstore: Specification,
) -> None:
    services.import_endpoints(specification=petstore)
    del petstore.raw_specification["paths"]["/users/{userId}"]
    petstore.save()

    result = services.import_endpoints(specification=petstore)

    assert [(e.path, e.method) for e in result.stale] == [
        ("/users/{userId}", "DELETE"),
        ("/users/{userId}", "GET"),
    ]
    # Reported, but still present: its test history depends on it.
    assert Endpoint.objects.filter(path="/users/{userId}").count() == 2


def test_a_stale_endpoint_keeps_its_test_history(petstore: Specification) -> None:
    services.import_endpoints(specification=petstore)
    endpoint = petstore.endpoints.get(path="/users/{userId}", method="GET")
    test_run = services.create_test_run(specification=petstore, name="Before the edit")
    test_case = services.create_test_case(
        test_run=test_run, endpoint=endpoint, name="Historic case", expected_status_code=200
    )

    del petstore.raw_specification["paths"]["/users/{userId}"]
    petstore.save()
    services.import_endpoints(specification=petstore)

    test_case.refresh_from_db()
    assert test_case.endpoint_id == endpoint.pk


def test_import_reports_what_it_could_not_use(petstore: Specification) -> None:
    petstore.raw_specification["paths"]["/traced"] = {"trace": {}}
    petstore.raw_specification["paths"]["relative"] = {"get": {}}
    petstore.save()

    result = services.import_endpoints(specification=petstore)

    reasons = {(s.path, s.method): s.reason for s in result.skipped}
    assert ("/traced", "TRACE") in reasons
    assert ("relative", "") in reasons
    assert len(result.created) == 4


def test_import_records_the_documents_title_and_version(petstore: Specification) -> None:
    result = services.import_endpoints(specification=petstore)

    assert result.title == "Example API"
    assert result.api_version == "1.0.0"
    assert result.imported_count == 4


def test_import_corrects_the_recorded_dialect(db) -> None:
    specification = Specification.objects.create(
        name="Legacy",
        source_type=SpecificationSourceType.OPENAPI,
        raw_specification={"swagger": "2.0", "info": {"title": "Legacy"}, "paths": {}},
    )

    result = services.import_endpoints(specification=specification)

    specification.refresh_from_db()
    assert specification.source_type == SpecificationSourceType.SWAGGER
    assert result.source_type == SpecificationSourceType.SWAGGER


def test_import_does_not_overwrite_the_name_or_version(petstore: Specification) -> None:
    petstore.raw_specification["info"] = {"title": "Renamed", "version": "9.9.9"}
    petstore.save()

    services.import_endpoints(specification=petstore)

    petstore.refresh_from_db()
    assert petstore.name == "Example API"
    assert petstore.version == "1.0.0"


def test_an_invalid_document_is_rejected_against_its_own_field(db) -> None:
    specification = Specification.objects.create(name="Broken", raw_specification={"nope": 1})

    with pytest.raises(ValidationError) as excinfo:
        services.import_endpoints(specification=specification)

    assert "raw_specification" in excinfo.value.message_dict


def test_a_specification_created_without_a_document_is_rejected_on_import(db) -> None:
    # raw_specification defaults to {}, which is not an API description.
    specification = Specification.objects.create(name="Empty")

    with pytest.raises(ValidationError):
        services.import_endpoints(specification=specification)


def test_a_failed_import_leaves_no_endpoints_behind(db) -> None:
    specification = Specification.objects.create(
        name="Half broken",
        raw_specification={"openapi": "3.0.3", "paths": {"/ok": {"get": {}}}},
    )
    services.import_endpoints(specification=specification)

    specification.raw_specification = {"openapi": "9.0.0", "paths": {}}
    specification.save()
    with pytest.raises(ValidationError):
        services.import_endpoints(specification=specification)

    # The rejected import changed nothing.
    assert specification.endpoints.count() == 1


def test_endpoints_of_other_specifications_are_untouched(petstore: Specification) -> None:
    other = Specification.objects.create(
        name="Other API",
        raw_specification={"openapi": "3.0.3", "paths": {"/users": {"get": {}}}},
    )
    services.import_endpoints(specification=other)

    result = services.import_endpoints(specification=petstore)

    assert result.stale == []
    assert other.endpoints.count() == 1
    assert petstore.endpoints.count() == 4


# --- API ----------------------------------------------------------------------


def test_the_import_route_is_mounted_under_v1() -> None:
    assert reverse("v1:specification-import", args=[3]) == "/api/v1/specifications/3/import/"


def test_post_import_returns_a_summary(api_client, petstore: Specification) -> None:
    response = api_client.post(f"/api/v1/specifications/{petstore.pk}/import/")

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["created"] == 4
    assert body["updated"] == 0
    assert body["unchanged"] == 0
    assert body["imported_count"] == 4
    assert body["title"] == "Example API"
    assert body["api_version"] == "1.0.0"
    assert body["source_type"] == "openapi"
    assert body["stale"] == []
    assert body["skipped"] == []
    assert body["warnings"] == []


def test_imported_endpoints_are_then_visible_over_the_api(
    api_client, petstore: Specification
) -> None:
    api_client.post(f"/api/v1/specifications/{petstore.pk}/import/")

    response = api_client.get(f"/api/v1/specifications/{petstore.pk}/endpoints/")

    assert response.json()["count"] == 4


def test_a_repeated_import_reports_everything_as_unchanged(
    api_client, petstore: Specification
) -> None:
    api_client.post(f"/api/v1/specifications/{petstore.pk}/import/")

    body = api_client.post(f"/api/v1/specifications/{petstore.pk}/import/").json()

    assert body["created"] == 0
    assert body["unchanged"] == 4


def test_import_reports_stale_endpoints_in_full(api_client, petstore: Specification) -> None:
    api_client.post(f"/api/v1/specifications/{petstore.pk}/import/")
    del petstore.raw_specification["paths"]["/users/{userId}"]
    petstore.save()

    body = api_client.post(f"/api/v1/specifications/{petstore.pk}/import/").json()

    assert [(item["path"], item["method"]) for item in body["stale"]] == [
        ("/users/{userId}", "DELETE"),
        ("/users/{userId}", "GET"),
    ]
    assert all("id" in item for item in body["stale"])


def test_import_reports_warnings(api_client, db) -> None:
    specification = Specification.objects.create(
        name="Dangling",
        raw_specification={
            "openapi": "3.0.3",
            "paths": {
                "/x": {
                    "get": {
                        "responses": {
                            "200": {
                                "content": {
                                    "application/json": {"schema": {"$ref": "#/nope"}}
                                }
                            }
                        }
                    }
                }
            },
        },
    )

    body = api_client.post(f"/api/v1/specifications/{specification.pk}/import/").json()

    assert any("could not be resolved" in warning for warning in body["warnings"])


def test_importing_an_invalid_document_is_a_400_naming_the_field(api_client, db) -> None:
    specification = Specification.objects.create(name="Broken", raw_specification={"nope": 1})

    response = api_client.post(f"/api/v1/specifications/{specification.pk}/import/")

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert "raw_specification" in response.json()


def test_importing_a_missing_specification_is_404(api_client) -> None:
    response = api_client.post("/api/v1/specifications/999999/import/")

    assert response.status_code == status.HTTP_404_NOT_FOUND


def test_import_is_not_reachable_by_get(api_client, petstore: Specification) -> None:
    response = api_client.get(f"/api/v1/specifications/{petstore.pk}/import/")

    assert response.status_code == status.HTTP_405_METHOD_NOT_ALLOWED
