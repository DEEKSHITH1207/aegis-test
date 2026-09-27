"""Serializers for the testing domain.

These handle shape and field-level validation only. Rules that involve more
than one object, or that decide what the system does, belong in services.py.
Relations are exposed as primary keys: the API stays predictable and a list
response never fans out into queries the caller did not ask for.
"""

from __future__ import annotations

from typing import Any

from rest_framework import serializers

from apps.testing import services
from apps.testing.models import (
    Endpoint,
    Finding,
    Specification,
    TestCase,
    TestRun,
)


class SpecificationSerializer(serializers.ModelSerializer):
    class Meta:
        model = Specification
        fields = [
            "id",
            "name",
            "description",
            "source_type",
            "raw_specification",
            "version",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]

    def validate_name(self, value: str) -> str:
        # A name of spaces passes CharField's blank check but is not a name.
        name = value.strip()
        if not name:
            raise serializers.ValidationError("This field may not be blank.")
        return name


class EndpointSerializer(serializers.ModelSerializer):
    class Meta:
        model = Endpoint
        fields = [
            "id",
            "specification",
            "path",
            "method",
            "operation_id",
            "summary",
            "description",
            "parameters",
            "request_schema",
            "response_schema",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "specification", "created_at", "updated_at"]


class TestRunSerializer(serializers.ModelSerializer):
    # Omitting the name is normal; the service derives one from the
    # specification. Status and the timestamps are driven by the run's
    # lifecycle, never set by the client.
    name = serializers.CharField(max_length=255, required=False, allow_blank=True, default="")
    # Headers usually carry credentials, so they are accepted but never
    # returned. Their names are, so a client can see what is configured.
    headers = serializers.DictField(
        child=serializers.CharField(allow_blank=True), required=False, write_only=True
    )
    header_names = serializers.SerializerMethodField()
    results = serializers.SerializerMethodField()

    class Meta:
        model = TestRun
        fields = [
            "id",
            "specification",
            "name",
            "base_url",
            "headers",
            "header_names",
            "status",
            "status_detail",
            "results",
            "started_at",
            "completed_at",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "id",
            "status",
            "status_detail",
            "started_at",
            "completed_at",
            "created_at",
            "updated_at",
        ]

    def get_header_names(self, test_run: TestRun) -> list[str]:
        return sorted(test_run.headers) if isinstance(test_run.headers, dict) else []

    def get_results(self, test_run: TestRun) -> dict[str, int]:
        return services.summarise_test_run(test_run)


class TestCaseSerializer(serializers.ModelSerializer):
    class Meta:
        model = TestCase
        fields = [
            "id",
            "test_run",
            "endpoint",
            "name",
            "description",
            "test_type",
            "input_data",
            "expected_status_code",
            "status",
            "status_detail",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "status", "status_detail", "created_at", "updated_at"]

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        test_run = attrs.get("test_run")
        endpoint = attrs.get("endpoint")
        if test_run and endpoint and test_run.specification_id != endpoint.specification_id:
            raise serializers.ValidationError(
                {"endpoint": "Endpoint must belong to the same specification as the test run."}
            )
        return attrs


class FindingSerializer(serializers.ModelSerializer):
    class Meta:
        model = Finding
        fields = [
            "id",
            "test_case",
            "response_record",
            "title",
            "description",
            "severity",
            "category",
            "evidence",
            "status",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]


class SkippedOperationSerializer(serializers.Serializer):
    """An operation the parser found but could not import, and why."""

    path = serializers.CharField()
    method = serializers.CharField(allow_blank=True)
    reason = serializers.CharField()


class StaleEndpointSerializer(serializers.Serializer):
    """An endpoint the database still holds that the document no longer lists."""

    id = serializers.IntegerField()
    path = serializers.CharField()
    method = serializers.CharField()


class ImportResultSerializer(serializers.Serializer):
    """The outcome of importing a specification document.

    Endpoints that were created, updated or left untouched are reported as
    counts; things needing a human decision — skipped operations, unresolved
    references, endpoints no longer in the document — are listed in full.
    """

    source_type = serializers.CharField()
    title = serializers.CharField()
    api_version = serializers.CharField()
    created = serializers.SerializerMethodField()
    updated = serializers.SerializerMethodField()
    unchanged = serializers.SerializerMethodField()
    imported_count = serializers.IntegerField()
    stale = StaleEndpointSerializer(many=True)
    skipped = SkippedOperationSerializer(many=True)
    warnings = serializers.ListField(child=serializers.CharField())

    def get_created(self, result) -> int:
        return len(result.created)

    def get_updated(self, result) -> int:
        return len(result.updated)

    def get_unchanged(self, result) -> int:
        return len(result.unchanged)


class GenerationResultSerializer(serializers.Serializer):
    """The outcome of generating a run's test cases.

    Counts rather than the cases themselves: a specification of any size
    produces hundreds, and they are already listable on the run.
    """

    test_run_id = serializers.IntegerField()
    endpoints = serializers.IntegerField()
    created = serializers.IntegerField()
    updated = serializers.IntegerField()
    unchanged = serializers.IntegerField()
    skipped = serializers.IntegerField()
    warnings = serializers.ListField(child=serializers.CharField())
