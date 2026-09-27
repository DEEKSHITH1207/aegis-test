"""Domain models for AegisTest.

The object graph mirrors how a test run actually happens:

    Specification --< Endpoint
          |               |
          +--< TestRun --< TestCase --< RequestRecord --- ResponseRecord
                              |                                |
                              +------------< Finding >---------+

A ``Specification`` is the root of every tree: deleting one removes its
endpoints, its runs, and everything those runs produced. ``RequestRecord`` and
``ResponseRecord`` are immutable evidence — they are written once when a request
is executed and never edited, so they carry ``created_at`` but no ``updated_at``.
"""

from __future__ import annotations

from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models

# The lowest and highest values the HTTP specification assigns meaning to.
# Used for both expected and observed status codes.
MIN_HTTP_STATUS_CODE = 100
MAX_HTTP_STATUS_CODE = 599


class HTTPMethod(models.TextChoices):
    """The HTTP methods AegisTest is able to exercise."""

    GET = "GET", "GET"
    POST = "POST", "POST"
    PUT = "PUT", "PUT"
    PATCH = "PATCH", "PATCH"
    DELETE = "DELETE", "DELETE"
    HEAD = "HEAD", "HEAD"
    OPTIONS = "OPTIONS", "OPTIONS"


class SpecificationSourceType(models.TextChoices):
    """Where a specification's contents came from."""

    OPENAPI = "openapi", "OpenAPI 3"
    SWAGGER = "swagger", "Swagger 2"
    MANUAL = "manual", "Manually defined"


class TestRunStatus(models.TextChoices):
    """Lifecycle of a run. Terminal states are COMPLETED, FAILED and CANCELLED."""

    PENDING = "pending", "Pending"
    RUNNING = "running", "Running"
    COMPLETED = "completed", "Completed"
    FAILED = "failed", "Failed"
    CANCELLED = "cancelled", "Cancelled"


class TestCaseType(models.TextChoices):
    """What a test case is trying to prove.

    The validation types name the single constraint a case violates, because a
    test that says only "invalid input" cannot explain a failure. Each generated
    case breaks exactly one rule, so its type is the diagnosis.
    """

    HAPPY_PATH = "happy_path", "Happy path"

    # One violated constraint each, produced by the generator.
    MISSING_REQUIRED = "missing_required", "Missing required field"
    EMPTY_VALUE = "empty_value", "Empty value for a required field"
    INVALID_TYPE = "invalid_type", "Wrong JSON type"
    INVALID_FORMAT = "invalid_format", "Malformed string format"
    INVALID_ENUM = "invalid_enum", "Value outside the enum"
    BELOW_MINIMUM = "below_minimum", "Below the minimum"
    ABOVE_MAXIMUM = "above_maximum", "Above the maximum"
    BELOW_MIN_LENGTH = "below_min_length", "Shorter than the minimum length"
    ABOVE_MAX_LENGTH = "above_max_length", "Longer than the maximum length"

    # Coarser kinds, for cases written by hand or by later phases.
    VALIDATION = "validation", "Input validation"
    AUTHENTICATION = "authentication", "Authentication"
    AUTHORIZATION = "authorization", "Authorization"
    ERROR_HANDLING = "error_handling", "Error handling"
    SCHEMA = "schema", "Schema conformance"
    SECURITY = "security", "Security"


class TestCaseStatus(models.TextChoices):
    """Outcome of a single test case.

    FAILED means the target API behaved incorrectly; ERROR means AegisTest could
    not reach a verdict (timeout, connection refused, malformed response).
    """

    PENDING = "pending", "Pending"
    RUNNING = "running", "Running"
    PASSED = "passed", "Passed"
    FAILED = "failed", "Failed"
    ERROR = "error", "Error"
    SKIPPED = "skipped", "Skipped"


class FindingSeverity(models.TextChoices):
    CRITICAL = "critical", "Critical"
    HIGH = "high", "High"
    MEDIUM = "medium", "Medium"
    LOW = "low", "Low"
    INFO = "info", "Informational"


class FindingCategory(models.TextChoices):
    BUG = "bug", "Bug"
    VALIDATION_GAP = "validation_gap", "Validation gap"
    SECURITY = "security", "Security"
    SCHEMA_MISMATCH = "schema_mismatch", "Schema mismatch"
    INCONSISTENCY = "inconsistency", "Inconsistency"
    PERFORMANCE = "performance", "Performance"


class FindingStatus(models.TextChoices):
    """Triage state. Findings are evidence-backed claims, not facts."""

    OPEN = "open", "Open"
    CONFIRMED = "confirmed", "Confirmed"
    FALSE_POSITIVE = "false_positive", "False positive"
    RESOLVED = "resolved", "Resolved"


class TimeStampedModel(models.Model):
    """Adds creation and modification timestamps to a mutable record."""

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class Specification(TimeStampedModel):
    """An API description that AegisTest tests against.

    ``raw_specification`` holds the document exactly as it was supplied. Parsing
    it into ``Endpoint`` rows happens in a later phase; storing the original
    means a re-parse never requires the user to upload it again.
    """

    name = models.CharField(max_length=255)
    description = models.TextField(blank=True, default="")
    source_type = models.CharField(
        max_length=16,
        choices=SpecificationSourceType.choices,
        default=SpecificationSourceType.OPENAPI,
    )
    raw_specification = models.JSONField(
        blank=True,
        default=dict,
        help_text="The specification document as supplied, unmodified.",
    )
    version = models.CharField(
        max_length=64,
        blank=True,
        default="",
        help_text="Version of the described API, e.g. '1.0.0'. Not a row version.",
    )

    class Meta:
        ordering = ["-created_at", "-id"]
        constraints = [
            # Re-uploading the same API at the same version is a duplicate, not
            # a new subject to test. An empty version is part of the key, so a
            # single unversioned specification per name is also enforced.
            models.UniqueConstraint(
                fields=["name", "version"], name="uniq_specification_name_version"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.name} {self.version}".strip()


class Endpoint(TimeStampedModel):
    """A single operation (path + method) offered by a specification."""

    specification = models.ForeignKey(
        Specification, on_delete=models.CASCADE, related_name="endpoints"
    )
    path = models.CharField(
        max_length=500, help_text="Templated path as written in the spec, e.g. /users/{id}."
    )
    method = models.CharField(max_length=7, choices=HTTPMethod.choices)
    operation_id = models.CharField(max_length=255, blank=True, default="")
    summary = models.CharField(max_length=500, blank=True, default="")
    description = models.TextField(blank=True, default="")
    parameters = models.JSONField(
        blank=True, default=list, help_text="OpenAPI parameter objects; a list."
    )
    # Absent rather than empty: a GET has no request body at all, and that is
    # different from having one with no properties.
    request_schema = models.JSONField(null=True, blank=True, default=None)
    response_schema = models.JSONField(null=True, blank=True, default=None)

    class Meta:
        ordering = ["path", "method"]
        constraints = [
            # OpenAPI itself allows a path+method pair to appear only once per
            # document, so the database should not allow it either.
            models.UniqueConstraint(
                fields=["specification", "path", "method"],
                name="uniq_endpoint_per_specification",
            ),
        ]
        indexes = [
            models.Index(fields=["specification", "method"], name="idx_endpoint_spec_method"),
        ]

    def __str__(self) -> str:
        return f"{self.method} {self.path}"


class TestRun(TimeStampedModel):
    """One execution of a generated suite against a specification."""

    specification = models.ForeignKey(
        Specification, on_delete=models.CASCADE, related_name="test_runs"
    )
    name = models.CharField(max_length=255)
    status = models.CharField(
        max_length=16, choices=TestRunStatus.choices, default=TestRunStatus.PENDING
    )
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        constraints = [
            # A run cannot finish before it starts.
            models.CheckConstraint(
                condition=models.Q(completed_at__isnull=True)
                | models.Q(started_at__isnull=False),
                name="ck_testrun_completed_implies_started",
            ),
        ]
        indexes = [
            models.Index(fields=["specification", "-created_at"], name="idx_testrun_spec_created"),
            models.Index(fields=["status"], name="idx_testrun_status"),
        ]

    def __str__(self) -> str:
        return self.name

    @property
    def is_finished(self) -> bool:
        return self.status in {
            TestRunStatus.COMPLETED,
            TestRunStatus.FAILED,
            TestRunStatus.CANCELLED,
        }


class TestCase(TimeStampedModel):
    """A single check to perform against one endpoint during one run.

    The endpoint must belong to the run's specification. There is no database
    constraint that can express this without denormalising the specification
    onto the row, so it is enforced in :meth:`clean` and by the service layer.
    """

    test_run = models.ForeignKey(TestRun, on_delete=models.CASCADE, related_name="test_cases")
    # A test case is meaningless without the endpoint it describes, and both
    # sides of this relation hang off the same specification, so they are
    # removed together.
    endpoint = models.ForeignKey(Endpoint, on_delete=models.CASCADE, related_name="test_cases")
    name = models.CharField(max_length=255)
    description = models.TextField(blank=True, default="")
    test_type = models.CharField(
        max_length=24, choices=TestCaseType.choices, default=TestCaseType.HAPPY_PATH
    )
    input_data = models.JSONField(
        blank=True,
        default=dict,
        help_text="Inputs for the request: body, query, path and header values.",
    )
    # Null means the specification documents no status code for this case, so
    # AegisTest has no expectation to assert. Inventing one — assuming every API
    # answers an invalid body with 400 — would manufacture failures against APIs
    # that simply chose 422, or that document nothing at all.
    expected_status_code = models.PositiveSmallIntegerField(
        null=True,
        blank=True,
        validators=[
            MinValueValidator(MIN_HTTP_STATUS_CODE),
            MaxValueValidator(MAX_HTTP_STATUS_CODE),
        ],
    )
    status = models.CharField(
        max_length=16, choices=TestCaseStatus.choices, default=TestCaseStatus.PENDING
    )

    class Meta:
        ordering = ["id"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(
                    expected_status_code__gte=MIN_HTTP_STATUS_CODE,
                    expected_status_code__lte=MAX_HTTP_STATUS_CODE,
                ),
                name="ck_testcase_expected_status_code_range",
            ),
        ]
        indexes = [
            models.Index(fields=["test_run", "status"], name="idx_testcase_run_status"),
            models.Index(fields=["endpoint"], name="idx_testcase_endpoint"),
        ]

    def __str__(self) -> str:
        return self.name

    def clean(self) -> None:
        super().clean()
        if self.test_run_id and self.endpoint_id:
            if self.test_run.specification_id != self.endpoint.specification_id:
                raise ValidationError(
                    {
                        "endpoint": "Endpoint must belong to the same specification "
                        "as the test run."
                    }
                )


class RequestRecord(models.Model):
    """The request that was actually sent for a test case.

    Immutable evidence, written once. A test case may have more than one — a
    retry, or a setup call — so this is a foreign key rather than a one-to-one.
    """

    test_case = models.ForeignKey(
        TestCase, on_delete=models.CASCADE, related_name="request_records"
    )
    method = models.CharField(max_length=7, choices=HTTPMethod.choices)
    url = models.URLField(max_length=2000, help_text="Fully resolved URL that was called.")
    headers = models.JSONField(blank=True, default=dict)
    query_params = models.JSONField(blank=True, default=dict)
    path_params = models.JSONField(blank=True, default=dict)
    # None means no body was sent at all, which is distinct from an empty one.
    body = models.JSONField(null=True, blank=True, default=None)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["id"]
        indexes = [
            models.Index(fields=["test_case"], name="idx_requestrecord_testcase"),
        ]

    def __str__(self) -> str:
        return f"{self.method} {self.url}"


class ResponseRecord(models.Model):
    """The response received for a request. Exactly one per request."""

    request_record = models.OneToOneField(
        RequestRecord, on_delete=models.CASCADE, related_name="response"
    )
    status_code = models.PositiveSmallIntegerField(
        validators=[
            MinValueValidator(MIN_HTTP_STATUS_CODE),
            MaxValueValidator(MAX_HTTP_STATUS_CODE),
        ]
    )
    headers = models.JSONField(blank=True, default=dict)
    # None covers an empty body and a body that was not valid JSON.
    body = models.JSONField(null=True, blank=True, default=None)
    response_time_ms = models.PositiveIntegerField(help_text="Wall-clock latency in milliseconds.")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["id"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(
                    status_code__gte=MIN_HTTP_STATUS_CODE,
                    status_code__lte=MAX_HTTP_STATUS_CODE,
                ),
                name="ck_responserecord_status_code_range",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.status_code} in {self.response_time_ms}ms"


class Finding(TimeStampedModel):
    """A problem AegisTest believes it has observed, with the evidence for it.

    ``response_record`` is optional and nulled rather than cascaded: a finding
    such as "the endpoint never responded" has no response to point at, and
    pruning old evidence should not erase the conclusion drawn from it.
    """

    test_case = models.ForeignKey(TestCase, on_delete=models.CASCADE, related_name="findings")
    response_record = models.ForeignKey(
        ResponseRecord,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="findings",
    )
    title = models.CharField(max_length=255)
    description = models.TextField(blank=True, default="")
    severity = models.CharField(
        max_length=16, choices=FindingSeverity.choices, default=FindingSeverity.MEDIUM
    )
    category = models.CharField(
        max_length=24, choices=FindingCategory.choices, default=FindingCategory.BUG
    )
    evidence = models.JSONField(
        blank=True,
        default=dict,
        help_text="Structured support for the claim: excerpts, diffs, comparisons.",
    )
    status = models.CharField(
        max_length=16, choices=FindingStatus.choices, default=FindingStatus.OPEN
    )

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["test_case"], name="idx_finding_testcase"),
            models.Index(fields=["severity", "status"], name="idx_finding_severity_status"),
        ]

    def __str__(self) -> str:
        return f"[{self.severity}] {self.title}"

    def clean(self) -> None:
        super().clean()
        if self.test_case_id and self.response_record_id:
            evidence_test_case_id = self.response_record.request_record.test_case_id
            if evidence_test_case_id != self.test_case_id:
                raise ValidationError(
                    {"response_record": "Response record belongs to a different test case."}
                )
