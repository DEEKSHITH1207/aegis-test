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

    # Sequences of requests, judged on how the API's state behaves.
    DUPLICATE_CREATE = "duplicate_create", "The same create sent twice"
    CREATE_READ_BACK = "create_read_back", "Create, then read it back"

    # Schema-valid input that a sensible API should still question: a range
    # whose end is before its start, a negative price, 29 February 2025.
    CROSS_FIELD = "cross_field", "Suspicious combination of fields"
    SUSPICIOUS_VALUE = "suspicious_value", "Valid but suspicious value"

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
    # A well-formed, successful response that holds a wrong value: a rule from
    # a business-rules file did not hold.
    BUSINESS_RULE_VIOLATION = "business_rule_violation", "Business rule violation"
    # The kinds generated test runs report.
    CRASH = "crash", "Crash"
    STATE_BUG = "state_bug", "State bug"
    SPEC_GAP = "spec_gap", "Specification gap"
    # The evidence may point at AegisTest's own test data rather than the API.
    FALSE_POSITIVE_RISK = "false_positive_risk", "Possible false positive"


class FindingConfidence(models.TextChoices):
    """How strongly a finding's evidence supports it."""

    LOW = "low", "Low"
    MEDIUM = "medium", "Medium"
    HIGH = "high", "High"


class ResourceStatus(models.TextChoices):
    """What became of a resource a run created in the API under test."""

    LEFT = "left", "Left behind"
    DELETED = "deleted", "Deleted"
    DELETE_FAILED = "delete_failed", "Delete failed"


class ScenarioStatus(models.TextChoices):
    """How one scenario of a business-rules run ended."""

    PASSED = "passed", "Every rule held"
    FAILED = "failed", "A rule was broken"
    ERROR = "error", "A rule could not be checked"
    STOPPED = "stopped", "A step failed and the scenario stopped"


class FindingStatus(models.TextChoices):
    """Triage state. Findings are evidence-backed claims, not facts."""

    OPEN = "open", "Open"
    CONFIRMED = "confirmed", "Confirmed"
    FALSE_POSITIVE = "false_positive", "False positive"
    RESOLVED = "resolved", "Resolved"


class AssessmentVerdict(models.TextChoices):
    """What the language model made of a finding's evidence. Advisory only."""

    LIKELY_REAL = "likely_real", "Likely a real defect"
    LIKELY_FALSE_POSITIVE = "likely_false_positive", "Likely a false positive"
    UNCERTAIN = "uncertain", "The evidence does not decide it"


class AssessmentConfidence(models.TextChoices):
    LOW = "low", "Low"
    MEDIUM = "medium", "Medium"
    HIGH = "high", "High"


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
    # The document's tags for the operation: the sections a user picks from
    # when a run should test only part of an API.
    tags = models.JSONField(blank=True, default=list)

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
    # Where the run sends its requests. A CharField rather than a URLField:
    # Django's URL validator rejects single-label hosts such as
    # http://api:8080, which is exactly how a service is named inside Docker.
    base_url = models.CharField(
        max_length=2000,
        blank=True,
        default="",
        help_text="Root URL of the API under test, e.g. http://localhost:3000/api.",
    )
    # Sent with every request of the run — typically credentials. Never
    # returned by the API and redacted from the stored request evidence.
    headers = models.JSONField(
        blank=True, default=dict, help_text="Headers added to every request, e.g. Authorization."
    )
    status_detail = models.TextField(
        blank=True, default="", help_text="Why the run ended as it did, when that needs saying."
    )
    # The endpoints this run tests. Empty means every endpoint of the
    # specification, including ones imported after the run was created.
    endpoints = models.ManyToManyField(Endpoint, blank=True, related_name="selected_in_runs")
    # Delete what the run created once it has executed, where the API offers
    # a DELETE for it.
    cleanup = models.BooleanField(default=False)
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
    status_detail = models.TextField(
        blank=True, default="", help_text="Why the case received its status."
    )
    # What a semantic case checks beyond the status code: why accepting it
    # would be suspicious, and how much that would matter.
    oracle = models.JSONField(blank=True, default=dict)

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
    # A CharField for the same reason as TestRun.base_url: the URL that was
    # called is evidence, and must be recorded even when Django would not
    # consider it a valid URL.
    url = models.CharField(max_length=2000, help_text="Fully resolved URL that was called.")
    # Credentials are replaced with a placeholder before this is written.
    headers = models.JSONField(blank=True, default=dict)
    query_params = models.JSONField(blank=True, default=dict)
    path_params = models.JSONField(blank=True, default=dict)
    # None means no body was sent at all, which is distinct from an empty one.
    body = models.JSONField(null=True, blank=True, default=None)
    # Set when no response was received: a timeout, a refused connection. Such
    # a request has no ResponseRecord.
    error = models.TextField(blank=True, default="", help_text="Why no response was received.")
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
    # Parsed JSON when the body was JSON; otherwise its text, as a string, cut
    # to a bounded length. None means the body was empty.
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


class RulesRun(TimeStampedModel):
    """One run of a business-rules file against an API.

    The file's text is copied onto the run, so what a run checked cannot
    change after the fact. Setup values — IDs, credentials — are deliberately
    not stored: they are handed to the run when it starts and live only in
    memory while it executes.
    """

    specification = models.ForeignKey(
        Specification, on_delete=models.CASCADE, related_name="rules_runs"
    )
    name = models.CharField(max_length=255)
    rules_text = models.TextField(help_text="The rules file as supplied (YAML, version 1).")
    base_url = models.CharField(max_length=2000)
    status = models.CharField(
        max_length=16, choices=TestRunStatus.choices, default=TestRunStatus.PENDING
    )
    status_detail = models.TextField(blank=True, default="")
    # The date "today" meant in the requests. Recorded because a rerun on
    # another day sends different dates.
    today = models.DateField(null=True, blank=True)
    # True when the date came from --today. It changes the dates the requests
    # carry, not the server's clock.
    today_overridden = models.BooleanField(default=False)
    warnings = models.JSONField(blank=True, default=list)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(completed_at__isnull=True)
                | models.Q(started_at__isnull=False),
                name="ck_rulesrun_completed_implies_started",
            ),
        ]
        indexes = [
            models.Index(fields=["specification", "-created_at"], name="idx_rulesrun_spec_created"),
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


class ScenarioResult(models.Model):
    """What one scenario of a rules run did. Written once, when it finishes."""

    rules_run = models.ForeignKey(RulesRun, on_delete=models.CASCADE, related_name="scenarios")
    position = models.PositiveIntegerField()
    name = models.CharField(max_length=255)
    status = models.CharField(max_length=16, choices=ScenarioStatus.choices)
    steps_run = models.PositiveIntegerField(default=0)
    # Why the scenario stopped, when it did.
    detail = models.TextField(blank=True, default="")
    # Every rule check, passed or not: rule, after_step, outcome, expected,
    # working, actual, detail. Findings are written for the broken ones.
    checks = models.JSONField(blank=True, default=list)
    cleanup_problems = models.JSONField(blank=True, default=list)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["rules_run", "position"]
        constraints = [
            models.UniqueConstraint(
                fields=["rules_run", "position"], name="uniq_scenarioresult_position"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.name}: {self.status}"


class RuleExchange(models.Model):
    """One request of a rules run and what came back. Immutable evidence.

    Stored already scrubbed: the Authorization header, the login token and
    every secret setup value are replaced before the row is written.
    """

    scenario_result = models.ForeignKey(
        ScenarioResult, on_delete=models.CASCADE, related_name="exchanges", null=True, blank=True
    )
    # The login belongs to the run, not to a scenario.
    rules_run = models.ForeignKey(RulesRun, on_delete=models.CASCADE, related_name="exchanges")
    position = models.PositiveIntegerField()
    phase = models.CharField(max_length=16)
    name = models.CharField(max_length=255)
    method = models.CharField(max_length=7, choices=HTTPMethod.choices)
    url = models.CharField(max_length=2000)
    request_headers = models.JSONField(blank=True, default=dict)
    query_params = models.JSONField(blank=True, default=dict)
    request_body = models.JSONField(null=True, blank=True, default=None)
    # Null when no response arrived; ``error`` then says why.
    status_code = models.PositiveSmallIntegerField(null=True, blank=True)
    response_headers = models.JSONField(blank=True, default=dict)
    response_body = models.JSONField(null=True, blank=True, default=None)
    response_time_ms = models.PositiveIntegerField(default=0)
    error = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["rules_run", "position"]
        indexes = [
            models.Index(fields=["rules_run", "position"], name="idx_ruleexchange_run_position"),
        ]

    def __str__(self) -> str:
        return f"{self.method} {self.url} -> {self.status_code}"


class Finding(TimeStampedModel):
    """A problem AegisTest believes it has observed, with the evidence for it.

    A finding comes either from a generated test case or from a scenario of a
    business-rules run — exactly one of the two, which a constraint enforces.

    ``response_record`` is optional and nulled rather than cascaded: a finding
    such as "the endpoint never responded" has no response to point at, and
    pruning old evidence should not erase the conclusion drawn from it.
    """

    test_case = models.ForeignKey(
        TestCase, on_delete=models.CASCADE, related_name="findings", null=True, blank=True
    )
    scenario_result = models.ForeignKey(
        ScenarioResult, on_delete=models.CASCADE, related_name="findings", null=True, blank=True
    )
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
    confidence = models.CharField(
        max_length=8, choices=FindingConfidence.choices, blank=True, default=""
    )
    probable_cause = models.CharField(max_length=500, blank=True, default="")
    # The fewest requests that show the problem, setup calls included, in order.
    reproduction = models.JSONField(blank=True, default=list)

    class Meta:
        ordering = ["-created_at", "-id"]
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(test_case__isnull=False, scenario_result__isnull=True)
                    | models.Q(test_case__isnull=True, scenario_result__isnull=False)
                ),
                name="ck_finding_exactly_one_source",
            ),
        ]
        indexes = [
            models.Index(fields=["test_case"], name="idx_finding_testcase"),
            models.Index(fields=["scenario_result"], name="idx_finding_scenarioresult"),
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


class Analysis(TimeStampedModel):
    """A language model's reading of one run's findings.

    Advisory by construction: an analysis writes its own rows and never edits
    a finding. The deterministic verdicts stay the record; a person triages
    them, with the assessments as a second opinion. A run may be analysed more
    than once — with another model, or after the prompt changes — and each
    analysis is kept.
    """

    test_run = models.ForeignKey(
        TestRun, on_delete=models.CASCADE, related_name="analyses", null=True, blank=True
    )
    rules_run = models.ForeignKey(
        RulesRun, on_delete=models.CASCADE, related_name="analyses", null=True, blank=True
    )
    status = models.CharField(
        max_length=16, choices=TestRunStatus.choices, default=TestRunStatus.PENDING
    )
    status_detail = models.TextField(blank=True, default="")
    # The model asked for. A refusal fallback may have served some requests
    # from another one; ``usage["models"]`` lists every model that answered.
    model = models.CharField(max_length=100)
    effort = models.CharField(max_length=16, blank=True, default="")
    summary = models.TextField(blank=True, default="")
    # Findings that look like one underlying problem: title, finding_ids,
    # explanation, recommendation, severity.
    groups = models.JSONField(blank=True, default=list)
    warnings = models.JSONField(blank=True, default=list)
    # How many findings the run had, and how many of them were sent. They
    # differ when the run had more than LLM_MAX_FINDINGS.
    findings_total = models.PositiveIntegerField(default=0)
    findings_selected = models.PositiveIntegerField(default=0)
    # Requests made and tokens spent, summed over every request.
    usage = models.JSONField(blank=True, default=dict)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        verbose_name_plural = "analyses"
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(test_run__isnull=False, rules_run__isnull=True)
                    | models.Q(test_run__isnull=True, rules_run__isnull=False)
                ),
                name="ck_analysis_exactly_one_source",
            ),
            models.CheckConstraint(
                condition=models.Q(completed_at__isnull=True)
                | models.Q(started_at__isnull=False),
                name="ck_analysis_completed_implies_started",
            ),
        ]

    def __str__(self) -> str:
        source = f"test run {self.test_run_id}" if self.test_run_id else f"rules run {self.rules_run_id}"
        return f"Analysis of {source}: {self.status}"

    @property
    def is_finished(self) -> bool:
        return self.status in {
            TestRunStatus.COMPLETED,
            TestRunStatus.FAILED,
            TestRunStatus.CANCELLED,
        }


class FindingAssessment(models.Model):
    """What one analysis concluded about one finding. Written once."""

    analysis = models.ForeignKey(Analysis, on_delete=models.CASCADE, related_name="assessments")
    finding = models.ForeignKey(Finding, on_delete=models.CASCADE, related_name="assessments")
    verdict = models.CharField(max_length=24, choices=AssessmentVerdict.choices)
    confidence = models.CharField(max_length=8, choices=AssessmentConfidence.choices)
    # The severity the model would give the finding if it is real. The
    # finding's own severity is not changed.
    suggested_severity = models.CharField(max_length=16, choices=FindingSeverity.choices)
    reasoning = models.TextField()
    likely_cause = models.TextField(blank=True, default="")
    recommendation = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["id"]
        constraints = [
            models.UniqueConstraint(
                fields=["analysis", "finding"], name="uniq_assessment_per_finding"
            ),
        ]
        indexes = [
            models.Index(fields=["finding"], name="idx_assessment_finding"),
        ]

    def __str__(self) -> str:
        return f"Finding {self.finding_id}: {self.verdict}"


class CreatedResource(models.Model):
    """Something a test run created in the API under test.

    Recorded for every create that succeeded, so a run can clean up after
    itself and its report can say what it left behind.
    """

    test_run = models.ForeignKey(
        TestRun, on_delete=models.CASCADE, related_name="created_resources"
    )
    test_case = models.ForeignKey(
        TestCase, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    # The operation that created it.
    endpoint = models.ForeignKey(Endpoint, on_delete=models.CASCADE, related_name="+")
    resource_id = models.CharField(max_length=255, blank=True, default="")
    delete_url = models.CharField(max_length=2000, blank=True, default="")
    status = models.CharField(
        max_length=16, choices=ResourceStatus.choices, default=ResourceStatus.LEFT
    )
    detail = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["id"]
        indexes = [
            models.Index(fields=["test_run", "status"], name="idx_resource_run_status"),
        ]

    def __str__(self) -> str:
        return f"{self.endpoint} -> {self.resource_id or '?'}"
