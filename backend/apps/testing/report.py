"""The PDF report of a test run.

Pure, like the generator: ``render`` takes a ``RunReport`` the service has
assembled and returns the PDF's bytes. No database, no network, so the layout
is testable from plain dataclasses.

Everything that reaches the page passes through ``_text``, which escapes it
for ReportLab's paragraph markup — response bodies are arbitrary text, and an
unescaped ``<`` would be read as a tag — and replaces characters the built-in
fonts cannot draw, which would otherwise print as black boxes.
"""

from __future__ import annotations

import io
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    CondPageBreak,
    KeepTogether,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

SEVERITY_ORDER = ("critical", "high", "medium", "low", "info")

SEVERITY_COLOURS = {
    "critical": colors.HexColor("#7f1d1d"),
    "high": colors.HexColor("#dc2626"),
    "medium": colors.HexColor("#d97706"),
    "low": colors.HexColor("#2563eb"),
    "info": colors.HexColor("#6b7280"),
}

VERDICT_LABELS = {
    "likely_real": "Likely real",
    "likely_false_positive": "Likely false positive",
    "uncertain": "Uncertain",
}

# The words the dashboard's tiles use for a case's status. "Error" is a case
# that got no verdict - the request was refused, timed out, or collided with
# existing data - and is inconclusive rather than a failure.
STATUS_LABELS = {
    "passed": "Passed",
    "failed": "Failed",
    "error": "Error",
    "skipped": "Skipped",
    "pending": "Not run",
    "running": "Running",
}
# The same statuses as counts, worded as the dashboard's tiles word them.
COUNT_LABELS = {**STATUS_LABELS, "error": "Errors"}

# What a generated case is for, by its test_type, as a developer would name it.
SCENARIO_LABELS = {
    "happy_path": "Happy path",
    "missing_required": "Required-field validation",
    "empty_value": "Required-field validation",
    "invalid_type": "Wrong type",
    "invalid_format": "Malformed format",
    "invalid_enum": "Value outside the enum",
    "below_minimum": "Boundary value",
    "above_maximum": "Boundary value",
    "below_min_length": "Boundary value",
    "above_max_length": "Boundary value",
    "cross_field": "Semantic rule",
    "suspicious_value": "Semantic rule",
    "duplicate_create": "Duplicate create",
    "create_read_back": "Create and read back",
}

# How findings are grouped on the page, in order. A spec gap is never a bug;
# a low-confidence or low-severity defect is a possible issue; the triage
# status a person set overrides the automatic grouping.
FINDING_GROUPS = ("bug", "possible", "documentation", "inconclusive", "dismissed")
GROUP_TITLES = {
    "bug": "Bugs",
    "possible": "Possible issues",
    "documentation": "Documentation gaps",
    "inconclusive": "Inconclusive",
    "dismissed": "Dismissed as false positives",
}
GROUP_INTROS = {
    "bug": (
        "The API misbehaved and the evidence is strong: a crash, forbidden or "
        "suspicious input accepted, or data that does not read back as written."
    ),
    "possible": (
        "Worth a look, but the evidence is weaker or the impact is low: a heuristic "
        "reading of a value, or a low-severity acceptance."
    ),
    "documentation": (
        "The API and its OpenAPI document disagree. The behaviour may well be right; "
        "the document (or the code) needs updating so clients know what to expect."
    ),
    "inconclusive": (
        "The request may not have reached the operation, so these say nothing certain "
        "about it."
    ),
    "dismissed": "Marked as false positives during triage.",
}
_DOCUMENTATION_CATEGORIES = {"spec_gap", "schema_mismatch"}

# Why a finding of each category is a problem, in one sentence.
WHY_IT_MATTERS = {
    "crash": (
        "A 5xx means the API did not handle the input: whatever it was, the server "
        "should answer with a 4xx it chose, not an error it did not."
    ),
    "validation_gap": (
        "Input the specification forbids, or that no sensible API should accept, "
        "reached the application and was stored or acted on."
    ),
    "state_bug": (
        "What the API stored or returned differs from what it accepted, so clients "
        "cannot trust a success response."
    ),
    "spec_gap": (
        "Clients built from the document will expect something the API does not do. "
        "This is a documentation defect unless the behaviour itself is wrong."
    ),
    "schema_mismatch": "The response does not have the shape the document promises.",
    "inconsistency": "The API answered differently from what the same scenario led to expect.",
    "business_rule_violation": "A business rule the rules file states did not hold.",
    "bug": "The API did not behave as its contract requires.",
    "security": "The behaviour exposes data or actions it should protect.",
    "performance": "The response took longer than the scenario allows.",
    "false_positive_risk": (
        "The request used generated sample values, so the answer may be about the "
        "values rather than the operation."
    ),
}


def classify_finding(
    category: str, severity: str, confidence: str, status: str = "open"
) -> str:
    """Which group of the report a finding belongs to."""
    if status == "false_positive":
        return "dismissed"
    if status == "confirmed":
        return "bug"
    if category == "false_positive_risk":
        return "inconclusive"
    if category in _DOCUMENTATION_CATEGORIES:
        return "documentation"
    if confidence == "low" or severity in {"low", "info"}:
        return "possible"
    return "bug"


def scenario_label(test_type: str) -> str:
    return SCENARIO_LABELS.get(test_type, test_type.replace("_", " ").capitalize())

_INK = colors.HexColor("#111827")
_MUTED = colors.HexColor("#6b7280")
_RULE = colors.HexColor("#e5e7eb")
_PANEL = colors.HexColor("#f3f4f6")
_ACCENT = colors.HexColor("#1e3a8a")

# A body longer than this is cut on the page; the full text stays in the API.
_MAX_BODY_CHARS = 1_500
# A request or a verdict inside the test-case table is shorter still.
_MAX_CELL_CHARS = 320


@dataclass
class ReportAssessment:
    verdict: str
    confidence: str
    suggested_severity: str
    reasoning: str
    likely_cause: str
    recommendation: str


@dataclass
class ReportFinding:
    id: int
    title: str
    severity: str
    category: str
    description: str
    endpoint: str
    test_case: str = ""
    test_type: str = ""
    expected_status: int | None = None
    request_method: str = ""
    request_url: str = ""
    request_body: Any = None
    response_status: int | None = None
    response_body: Any = None
    response_time_ms: int | None = None
    assessment: ReportAssessment | None = None
    confidence: str = ""
    probable_cause: str = ""
    # Each step: method, url, body, status_code, note.
    reproduction: list[dict[str, Any]] = field(default_factory=list)
    # Triage status: open, confirmed, false_positive, resolved.
    status: str = "open"

    @property
    def group(self) -> str:
        return classify_finding(self.category, self.severity, self.confidence, self.status)


@dataclass
class ReportResource:
    endpoint: str
    resource_id: str
    status: str
    detail: str


@dataclass
class ReportEndpoint:
    label: str
    cases: int
    passed: int
    failed: int
    errors: int
    findings: int
    # Cases still pending or skipped: generated, never sent.
    not_run: int = 0
    # Scenario label -> number of cases, e.g. {"Boundary value": 4}.
    scenarios: dict[str, int] = field(default_factory=dict)

    @property
    def executed(self) -> int:
        return self.passed + self.failed + self.errors


@dataclass
class ReportCase:
    """One generated test case as the details table shows it."""

    id: int
    name: str
    endpoint: str
    test_type: str
    purpose: str
    # The request as sent (or as generated, when it was never sent), with
    # secret-named values already masked.
    request: str
    expected_status: int | None
    actual_status: int | None
    status: str
    detail: str
    requests_sent: int = 0
    findings: int = 0

    @property
    def scenario(self) -> str:
        return scenario_label(self.test_type)


@dataclass
class ReportSequence:
    """A case that sent several requests: a setup create, then the request
    under test, then a read-back."""

    case: str
    endpoint: str
    status: str
    # Each step: method, url, status_code (None when no response), note.
    steps: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class ReportAnalysis:
    model: str
    completed_at: datetime | None
    summary: str
    groups: list[dict[str, Any]]
    verdicts: dict[str, int]
    findings_total: int
    findings_selected: int


@dataclass
class RunReport:
    specification: str
    run_name: str
    run_id: int
    base_url: str
    status: str
    status_detail: str
    started_at: datetime | None
    completed_at: datetime | None
    generated_at: datetime
    results: dict[str, int]
    endpoints: list[ReportEndpoint] = field(default_factory=list)
    findings: list[ReportFinding] = field(default_factory=list)
    findings_total: int = 0
    analysis: ReportAnalysis | None = None
    # What part of the API the run tested, e.g. "All 40 endpoints".
    scope: str = ""
    # Everything the run created in the target, deleted or not.
    resources: list[ReportResource] = field(default_factory=list)
    resources_total: int = 0
    cases: list[ReportCase] = field(default_factory=list)
    cases_total: int = 0
    sequences: list[ReportSequence] = field(default_factory=list)
    sequences_total: int = 0
    # Operations of the specification that executed no case in this run.
    not_tested: list[str] = field(default_factory=list)
    not_tested_total: int = 0
    # (reason, how many cases) for cases never sent, and for inconclusive ones.
    not_run_reasons: list[tuple[str, int]] = field(default_factory=list)
    inconclusive_reasons: list[tuple[str, int]] = field(default_factory=list)

    @property
    def executed(self) -> int:
        return sum(self.results.get(s, 0) for s in ("passed", "failed", "error"))

    @property
    def not_run(self) -> int:
        return sum(self.results.get(s, 0) for s in ("pending", "skipped", "running"))

    def findings_by_group(self) -> dict[str, list[ReportFinding]]:
        groups: dict[str, list[ReportFinding]] = {group: [] for group in FINDING_GROUPS}
        for finding in self.findings:
            groups[finding.group].append(finding)
        return groups


def render(report: RunReport) -> bytes:
    buffer = io.BytesIO()
    document = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=18 * mm,
        rightMargin=18 * mm,
        topMargin=18 * mm,
        bottomMargin=18 * mm,
        title=_plain(f"AegisTest report - {report.specification} - {report.run_name}"),
        author="AegisTest",
        subject="API test report",
    )
    styles = _styles()
    story: list[Any] = []
    story += _cover(report, styles)
    story += _executive_summary(report, styles)
    if report.analysis is not None:
        story += _analysis_section(report.analysis, styles)
    story += _coverage_section(report, styles)
    story += _findings_section(report, styles)
    story += _cases_section(report, styles)
    if report.sequences or report.resources:
        story += _stateful_section(report, styles)
    story += _limitations_section(report, styles)

    def footer(canvas, doc) -> None:
        canvas.saveState()
        canvas.setFont("Helvetica", 8)
        canvas.setFillColor(_MUTED)
        canvas.drawString(
            doc.leftMargin,
            10 * mm,
            _plain(f"AegisTest - {report.specification} - run #{report.run_id}"),
        )
        canvas.drawRightString(
            A4[0] - doc.rightMargin, 10 * mm, f"Page {canvas.getPageNumber()}"
        )
        canvas.restoreState()

    document.build(story, onFirstPage=footer, onLaterPages=footer)
    return buffer.getvalue()


# --- Sections --------------------------------------------------------------------


def _cover(report: RunReport, styles: dict[str, ParagraphStyle]) -> list[Any]:
    rows = [
        ("Specification", report.specification),
        ("Test run", f"{report.run_name} (#{report.run_id})"),
        ("Target", report.base_url or "-"),
        ("Scope", report.scope or "-"),
        ("Status", report.status.capitalize()),
        ("Started", _when(report.started_at)),
        ("Completed", _when(report.completed_at)),
        ("Report generated", _when(report.generated_at)),
    ]
    if report.status_detail:
        rows.append(("Status detail", report.status_detail))
    return [
        Paragraph("AegisTest API test report", styles["kicker"]),
        Paragraph(_text(report.specification), styles["title"]),
        Spacer(1, 4 * mm),
        _key_values(rows, styles),
        Spacer(1, 8 * mm),
    ]


def _executive_summary(report: RunReport, styles: dict[str, ParagraphStyle]) -> list[Any]:
    results = report.results
    case_tiles = [
        ("Generated", results.get("total", 0)),
        ("Executed", report.executed),
        (COUNT_LABELS["passed"], results.get("passed", 0)),
        (COUNT_LABELS["failed"], results.get("failed", 0)),
        (COUNT_LABELS["error"], results.get("error", 0)),
        (COUNT_LABELS["pending"], report.not_run),
    ]
    # Every finding count below describes the findings the report holds. When
    # the run has more than the report shows, the first tile says so and the
    # groups and severities add up to the shown number, not the total.
    shown = len(report.findings)
    capped = shown < report.findings_total
    groups = report.findings_by_group()
    finding_tiles = [
        ("Findings shown" if capped else "Findings", shown),
        *((GROUP_TITLES[group], len(groups[group])) for group in FINDING_GROUPS),
    ]

    counts = {severity: 0 for severity in SEVERITY_ORDER}
    for finding in report.findings:
        counts[finding.severity] = counts.get(finding.severity, 0) + 1
    severity_table = Table(
        [
            [_chip(severity, styles) for severity in SEVERITY_ORDER],
            [Paragraph(str(counts[severity]), styles["cell_center"]) for severity in SEVERITY_ORDER],
        ],
        colWidths=[34.8 * mm] * len(SEVERITY_ORDER),
    )
    severity_table.setStyle(
        TableStyle([("GRID", (0, 0), (-1, -1), 0.5, _RULE), ("VALIGN", (0, 0), (-1, -1), "MIDDLE")])
    )

    tested = [e for e in report.endpoints if e.executed]
    paths = {e.label.split(" ", 1)[1] if " " in e.label else e.label for e in tested}
    methods = {e.label.split(" ", 1)[0] for e in tested}
    coverage = (
        f"{_count(len(tested), 'operation')} executed at least one test case: "
        f"{_count(len(paths), 'endpoint path')}, {_count(len(methods), 'HTTP method')} "
        f"({', '.join(sorted(methods))})."
        if tested
        else "No test case was executed."
    )

    return [
        Paragraph("Executive summary", styles["h1"]),
        Paragraph("Test results", styles["h3"]),
        _tiles(case_tiles, styles),
        Paragraph(
            _text(
                f"{coverage} A case is one request (or a short sequence) sent to the API and "
                f"judged against the document. \"{COUNT_LABELS['error']}\" are cases that got "
                f"no verdict - refused, timed out, or collided with existing data - and are "
                f"inconclusive, not failures."
            ),
            styles["muted"],
        ),
        Spacer(1, 4 * mm),
        Paragraph("Findings", styles["h3"]),
        _tiles(finding_tiles, styles),
        Paragraph(
            _text(
                (
                    f"The run produced {report.findings_total} findings; the {shown} most "
                    f"severe are in this report, and every count below describes those. "
                    if capped
                    else ""
                )
                + "Findings count problems, not cases: one failed case can produce several "
                "findings, and a documentation note is attached to a case that passed. "
                "Only the \"Bugs\" column claims a defect with strong evidence; "
                "\"Dismissed\" findings were marked false positives during triage and "
                "are counted in the severity table below as well."
            ),
            styles["muted"],
        ),
        Spacer(1, 3 * mm),
        Paragraph("Findings by severity" + (" (shown)" if capped else ""), styles["h3"]),
        severity_table,
        Spacer(1, 6 * mm),
    ]


def _tiles(tiles: list[tuple[str, int]], styles: dict[str, ParagraphStyle]) -> Table:
    table = Table(
        [
            [Paragraph(str(value), styles["tile_value"]) for _, value in tiles],
            [Paragraph(_text(label), styles["tile_label"]) for label, _ in tiles],
        ],
        colWidths=[174 * mm / len(tiles)] * len(tiles),
    )
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), _PANEL),
                ("LINEAFTER", (0, 0), (-2, -1), 2, colors.white),
                ("TOPPADDING", (0, 0), (-1, 0), 8),
                ("BOTTOMPADDING", (0, -1), (-1, -1), 8),
            ]
        )
    )
    return table


def _analysis_section(analysis: ReportAnalysis, styles: dict[str, ParagraphStyle]) -> list[Any]:
    story: list[Any] = [
        CondPageBreak(60 * mm),
        Paragraph("AI analysis", styles["h1"]),
        Paragraph(
            _text(
                f"A second opinion from {analysis.model}"
                + (f", {_when(analysis.completed_at)}" if analysis.completed_at else "")
                + ". It is advisory: the findings below are the deterministic results, "
                "unchanged, and each carries the model's assessment beside it."
            ),
            styles["muted"],
        ),
        Spacer(1, 3 * mm),
    ]
    verdicts = ", ".join(
        f"{analysis.verdicts.get(key, 0)} {label.lower()}" for key, label in VERDICT_LABELS.items()
    )
    story.append(Paragraph(_text(f"Verdicts: {verdicts}."), styles["body"]))
    if analysis.findings_selected < analysis.findings_total:
        story.append(
            Paragraph(
                _text(
                    f"Only the {analysis.findings_selected} most severe of "
                    f"{analysis.findings_total} findings were analysed."
                ),
                styles["muted"],
            )
        )
    if analysis.summary:
        story += [Spacer(1, 2 * mm), Paragraph(_text(analysis.summary), styles["body"])]
    if analysis.groups:
        story += [Spacer(1, 3 * mm), Paragraph("Likely root causes", styles["h3"])]
        rows = [
            [
                Paragraph("Severity", styles["th"]),
                Paragraph("Problem", styles["th"]),
                Paragraph("Findings", styles["th"]),
            ]
        ]
        for group in analysis.groups:
            ids = ", ".join(f"#{i}" for i in group.get("finding_ids", []))
            rows.append(
                [
                    _chip(group.get("severity", "info"), styles),
                    [
                        Paragraph(f"<b>{_text(group.get('title', ''))}</b>", styles["cell"]),
                        Paragraph(_text(group.get("explanation", "")), styles["cell"]),
                        Paragraph(
                            "<b>Next:</b> " + _text(group.get("recommendation", "")),
                            styles["cell"],
                        ),
                    ],
                    Paragraph(_text(ids), styles["cell"]),
                ]
            )
        table = Table(rows, colWidths=[24 * mm, 118 * mm, 32 * mm], repeatRows=1)
        table.setStyle(_grid_style())
        story.append(table)
    story.append(Spacer(1, 6 * mm))
    return story


def _coverage_section(report: RunReport, styles: dict[str, ParagraphStyle]) -> list[Any]:
    story: list[Any] = [CondPageBreak(40 * mm), Paragraph("Test coverage and scenarios", styles["h1"])]
    if not report.endpoints:
        story += [Paragraph("No test cases were generated for this run.", styles["body"]), Spacer(1, 6 * mm)]
        return story
    story.append(
        Paragraph(
            _text(
                "Every operation the run holds cases for, with how many were sent and how "
                "they ended. The line under each one names the scenarios its cases cover."
            ),
            styles["muted"],
        )
    )
    header = ["Operation", "Cases", "Sent", COUNT_LABELS["passed"], COUNT_LABELS["failed"],
              COUNT_LABELS["error"], COUNT_LABELS["pending"], "Findings"]
    rows: list[list[Any]] = [[Paragraph(h, styles["th"]) for h in header]]
    style = _grid_style()
    for endpoint in report.endpoints:
        rows.append(
            [
                Paragraph(_text(endpoint.label), styles["mono"]),
                *(
                    Paragraph(str(value), styles["cell_center"])
                    for value in (
                        endpoint.cases,
                        endpoint.executed,
                        endpoint.passed,
                        endpoint.failed,
                        endpoint.errors,
                        endpoint.not_run,
                        endpoint.findings,
                    )
                ),
            ]
        )
        scenarios = " - ".join(f"{label} {count}" for label, count in endpoint.scenarios.items())
        rows.append([Paragraph(_text(scenarios or "-"), styles["muted"])] + [""] * 7)
        row = len(rows) - 1
        style.add("SPAN", (0, row), (-1, row))
        style.add("TOPPADDING", (0, row), (-1, row), 0)
        style.add("LINEBELOW", (0, row - 1), (-1, row - 1), 0, colors.white)
    table = Table(rows, colWidths=[55 * mm] + [17 * mm] * 7, repeatRows=1)
    table.setStyle(style)
    return [*story, Spacer(1, 2 * mm), table, Spacer(1, 6 * mm)]


def _cases_section(report: RunReport, styles: dict[str, ParagraphStyle]) -> list[Any]:
    story: list[Any] = [PageBreak(), Paragraph("Test case details", styles["h1"])]
    if not report.cases:
        story.append(Paragraph("No test cases were generated for this run.", styles["body"]))
        return story
    if len(report.cases) < report.cases_total:
        story.append(
            Paragraph(
                _text(
                    f"The first {len(report.cases)} of {report.cases_total} cases are shown, in "
                    f"endpoint order. Every case is available from the API."
                ),
                styles["muted"],
            )
        )
    story.append(
        Paragraph(
            _text(
                "Request values are as sent; secret-named fields are masked. \"Expected\" is "
                "the status the document promises for the case, or none when it promises "
                "nothing. The last column is the recorded verdict."
            ),
            styles["muted"],
        )
    )
    header = ["Case", "Scenario", "Request", "Expected / actual", "Result", "Verdict"]
    widths = [34 * mm, 24 * mm, 48 * mm, 20 * mm, 18 * mm, 30 * mm]
    by_endpoint: dict[str, list[ReportCase]] = {}
    for case in report.cases:
        by_endpoint.setdefault(case.endpoint, []).append(case)
    for endpoint, cases in by_endpoint.items():
        rows: list[list[Any]] = [[Paragraph(h, styles["th"]) for h in header]]
        for case in cases:
            expected = str(case.expected_status) if case.expected_status is not None else "none"
            actual = str(case.actual_status) if case.actual_status is not None else "-"
            rows.append(
                [
                    [
                        Paragraph(f"<b>#{case.id}</b> {_text(case.name)}", styles["cell"]),
                        Paragraph(_text(_clip(case.purpose, 160)), styles["muted"]),
                    ],
                    Paragraph(_text(case.scenario), styles["cell"]),
                    Paragraph(_mono(_clip(case.request, _MAX_CELL_CHARS)), styles["mono"]),
                    Paragraph(_text(f"{expected} / {actual}"), styles["cell_center"]),
                    Paragraph(_text(STATUS_LABELS.get(case.status, case.status)), styles["cell_center"]),
                    [
                        Paragraph(_text(_clip(case.detail, _MAX_CELL_CHARS) or "-"), styles["muted"]),
                        *(
                            [Paragraph(_text(_count(case.findings, "finding")), styles["cell"])]
                            if case.findings
                            else []
                        ),
                    ],
                ]
            )
        table = Table(rows, colWidths=widths, repeatRows=1)
        table.setStyle(_grid_style())
        story += [
            CondPageBreak(30 * mm),
            Paragraph(_text(endpoint), styles["h3"]),
            table,
            Spacer(1, 4 * mm),
        ]
    return story


def _stateful_section(report: RunReport, styles: dict[str, ParagraphStyle]) -> list[Any]:
    story: list[Any] = [CondPageBreak(50 * mm), Paragraph("Stateful testing and cleanup", styles["h1"])]
    if report.sequences:
        story.append(Paragraph("Sequences", styles["h3"]))
        story.append(
            Paragraph(
                _text(
                    "Cases that sent more than one request: a create followed by the request "
                    "under test, the same create twice, or a create read back. Each step is "
                    "the request as sent and the status it received."
                ),
                styles["muted"],
            )
        )
        if len(report.sequences) < report.sequences_total:
            story.append(
                Paragraph(
                    _text(f"The first {len(report.sequences)} of {report.sequences_total} are shown."),
                    styles["muted"],
                )
            )
        for sequence in report.sequences:
            lines: list[Any] = [
                Paragraph(
                    f"<b>{_text(sequence.endpoint)}</b> - {_text(sequence.case)} - "
                    f"{_text(STATUS_LABELS.get(sequence.status, sequence.status))}",
                    styles["cell"],
                )
            ]
            for number, step in enumerate(sequence.steps, start=1):
                status = step.get("status_code")
                shown = str(status) if status is not None else "no response"
                note = f" - {step['note']}" if step.get("note") else ""
                lines.append(
                    Paragraph(
                        _text(f"{number}. {step.get('method')} {step.get('url')} -> {shown}{note}"),
                        styles["mono"],
                    )
                )
            story.append(KeepTogether(lines))
            story.append(Spacer(1, 2 * mm))
    if report.resources:
        story += _resources_section(report, styles)
    story.append(Spacer(1, 4 * mm))
    return story


def _resources_section(report: RunReport, styles: dict[str, ParagraphStyle]) -> list[Any]:
    resources = report.resources
    total = report.resources_total or len(resources)
    deleted = sum(1 for r in resources if r.status == "deleted")
    failed = sum(1 for r in resources if r.status == "delete_failed")
    left = len(resources) - deleted - failed
    story: list[Any] = [
        Paragraph("Created data and cleanup", styles["h3"]),
        Paragraph(
            _text(
                f"The run created {total} resources in the target: {deleted} deleted, "
                f"{failed} could not be deleted, {left} left in place."
                + (" Cleanup was not requested for this run." if not deleted and not failed else "")
            ),
            styles["body"],
        ),
    ]
    if len(resources) < total:
        story.append(Paragraph(_text(f"The first {len(resources)} are listed."), styles["muted"]))
    labels = {"deleted": "Deleted", "delete_failed": "Delete failed", "left": "Left in place"}
    rows = [[Paragraph(h, styles["th"]) for h in ("Created by", "ID", "Cleanup", "Detail")]]
    for resource in resources:
        rows.append(
            [
                Paragraph(_text(resource.endpoint), styles["mono"]),
                Paragraph(_text(resource.resource_id or "-"), styles["mono"]),
                Paragraph(_text(labels.get(resource.status, resource.status)), styles["cell"]),
                Paragraph(_text(resource.detail or "-"), styles["muted"]),
            ]
        )
    table = Table(rows, colWidths=[58 * mm, 28 * mm, 22 * mm, 66 * mm], repeatRows=1)
    table.setStyle(_grid_style())
    story += [Spacer(1, 2 * mm), table]
    return story


def _limitations_section(report: RunReport, styles: dict[str, ParagraphStyle]) -> list[Any]:
    story: list[Any] = [CondPageBreak(50 * mm), Paragraph("Limitations", styles["h1"])]
    bullets: list[str] = []
    if report.not_run:
        reasons = "; ".join(f"{count} - {reason}" for reason, count in report.not_run_reasons)
        bullets.append(
            f"{report.not_run} generated cases were never sent"
            + (f": {reasons}." if reasons else ".")
            + " They are not evidence about the API either way."
        )
    if report.results.get("error", 0):
        reasons = "; ".join(f"{count} - {reason}" for reason, count in report.inconclusive_reasons)
        bullets.append(
            f"{report.results['error']} cases got no verdict"
            + (f": {reasons}." if reasons else ".")
        )
    if report.not_tested_total:
        listed = ", ".join(report.not_tested)
        more = (
            f" and {report.not_tested_total - len(report.not_tested)} more"
            if report.not_tested_total > len(report.not_tested)
            else ""
        )
        bullets.append(
            f"{report.not_tested_total} operations of the specification executed no case in "
            f"this run (outside its scope, or no case could be generated): {listed}{more}."
        )
    if not report.cases_total:
        bullets.append("No test cases were generated, so nothing about the API was tested.")
    bullets.append(
        "Generated cases do not cover authentication or authorisation, regex 'pattern' "
        "constraints, or request bodies the generator could not build from the document. "
        "Only the happy path of an operation on one resource creates its own resource "
        "first; its other cases use sample path values."
    )
    bullets.append(
        "A documentation gap records a difference between the API and its document, "
        "not a defect. A finding's confidence says how strong its evidence is; a person "
        "still decides each one."
    )
    for bullet in bullets:
        story.append(Paragraph("- " + _text(bullet), styles["body"]))
    return story


def _findings_section(report: RunReport, styles: dict[str, ParagraphStyle]) -> list[Any]:
    story: list[Any] = [PageBreak(), Paragraph("Findings", styles["h1"])]
    if not report.findings:
        story.append(Paragraph("The run produced no findings.", styles["body"]))
        return story
    if len(report.findings) < report.findings_total:
        story.append(
            Paragraph(
                _text(
                    f"The {len(report.findings)} most severe of {report.findings_total} findings "
                    f"are shown. Every finding is available from the API."
                ),
                styles["muted"],
            )
        )
    groups = report.findings_by_group()
    for group in FINDING_GROUPS:
        findings = groups[group]
        if not findings:
            continue
        story += [
            CondPageBreak(40 * mm),
            Spacer(1, 3 * mm),
            Paragraph(f"{GROUP_TITLES[group]} ({len(findings)})", styles["h2"]),
            Paragraph(_text(GROUP_INTROS[group]), styles["muted"]),
            Spacer(1, 3 * mm),
        ]
        if group == "dismissed":
            for finding in findings:
                story.append(Paragraph(_text(f"#{finding.id} {finding.title}"), styles["cell"]))
            continue
        for finding in findings:
            story += _finding(finding, styles)
    return story


def _finding(finding: ReportFinding, styles: dict[str, ParagraphStyle]) -> list[Any]:
    heading = Table(
        [
            [
                _chip(finding.severity, styles),
                Paragraph(f"<b>#{finding.id}</b> {_text(finding.title)}", styles["h3_inline"]),
            ]
        ],
        colWidths=[22 * mm, 152 * mm],
    )
    heading.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("LEFTPADDING", (0, 0), (0, 0), 0),
                ("LINEBELOW", (0, 0), (-1, 0), 0.75, _RULE),
            ]
        )
    )

    category = finding.category.replace("_", " ")
    if finding.confidence:
        category += f" - {finding.confidence} confidence"
    rows: list[tuple[str, str]] = [
        ("Category", category),
        ("Endpoint", finding.endpoint or "-"),
    ]
    if finding.probable_cause:
        rows.append(("Probable cause", finding.probable_cause))
    why = WHY_IT_MATTERS.get(finding.category)
    if why:
        rows.append(("Why it matters", why))
    if finding.status and finding.status != "open":
        rows.append(("Triage", finding.status.replace("_", " ")))
    if finding.test_case:
        rows.append(("Test case", f"{finding.test_case} ({finding.test_type.replace('_', ' ')})"))
    if finding.expected_status is not None or finding.response_status is not None:
        expected = str(finding.expected_status) if finding.expected_status is not None else "none documented"
        got = str(finding.response_status) if finding.response_status is not None else "no response"
        timing = f" in {finding.response_time_ms} ms" if finding.response_time_ms is not None else ""
        rows.append(("Status", f"expected {expected}, got {got}{timing}"))

    block: list[Any] = [heading, Spacer(1, 2 * mm), _key_values(rows, styles)]
    if finding.description:
        block += [Spacer(1, 2 * mm), Paragraph(_text(finding.description), styles["body"])]

    evidence: list[Any] = []
    if len(finding.reproduction) > 1:
        evidence.append(Paragraph("Steps to reproduce", styles["label"]))
        for step in finding.reproduction:
            note = f" - {step['note']}" if step.get("note") else ""
            evidence.append(
                Paragraph(
                    _text(
                        f"{step.get('step')}. {step.get('method')} {step.get('url')} -> "
                        f"{step.get('status_code') if step.get('status_code') is not None else 'no response'}"
                        f"{note}"
                    ),
                    styles["mono"],
                )
            )
            if step.get("body") is not None:
                evidence.append(Paragraph(_mono(_body(step["body"])), styles["mono"]))
    elif finding.request_url:
        evidence.append(Paragraph("Request", styles["label"]))
        evidence.append(
            Paragraph(_text(f"{finding.request_method} {finding.request_url}"), styles["mono"])
        )
        if finding.request_body is not None:
            evidence.append(Paragraph(_mono(_body(finding.request_body)), styles["mono"]))
    if finding.response_body is not None:
        evidence.append(Paragraph("Response body", styles["label"]))
        evidence.append(Paragraph(_mono(_body(finding.response_body)), styles["mono"]))
    if evidence:
        panel = Table([[evidence]], colWidths=[174 * mm])
        panel.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, -1), _PANEL),
                    ("LEFTPADDING", (0, 0), (-1, -1), 6),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                ]
            )
        )
        block += [Spacer(1, 2 * mm), panel]

    if finding.assessment is not None:
        block += [Spacer(1, 2 * mm), _assessment(finding, styles)]

    # The heading and its first facts stay together; long evidence may split.
    return [KeepTogether(block[:3]), *block[3:], Spacer(1, 7 * mm)]


def _assessment(finding: ReportFinding, styles: dict[str, ParagraphStyle]) -> Table:
    assessment = finding.assessment
    assert assessment is not None
    verdict = VERDICT_LABELS.get(assessment.verdict, assessment.verdict)
    severity_note = ""
    if assessment.suggested_severity != finding.severity:
        severity_note = f"; suggests {assessment.suggested_severity} severity"
    lines = [
        Paragraph(
            f"<b>AI assessment: {_text(verdict)}</b> "
            f"({_text(assessment.confidence)} confidence{_text(severity_note)})",
            styles["cell"],
        ),
        Paragraph(_text(assessment.reasoning), styles["cell"]),
    ]
    if assessment.likely_cause:
        lines.append(Paragraph("<b>Likely cause:</b> " + _text(assessment.likely_cause), styles["cell"]))
    if assessment.recommendation:
        lines.append(Paragraph("<b>Next step:</b> " + _text(assessment.recommendation), styles["cell"]))
    table = Table([[lines]], colWidths=[174 * mm])
    table.setStyle(
        TableStyle(
            [
                ("LINEBEFORE", (0, 0), (0, -1), 2.5, _ACCENT),
                ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#eef2ff")),
                ("LEFTPADDING", (0, 0), (-1, -1), 8),
            ]
        )
    )
    return table


# --- Building blocks ---------------------------------------------------------------


def _styles() -> dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()["Normal"]

    def style(name: str, **overrides: Any) -> ParagraphStyle:
        return ParagraphStyle(name, parent=base, **{"textColor": _INK, **overrides})

    return {
        "kicker": style("kicker", fontSize=9, textColor=_MUTED, leading=12),
        "title": style("title", fontName="Helvetica-Bold", fontSize=22, leading=27),
        "h1": style("h1", fontName="Helvetica-Bold", fontSize=15, leading=19, spaceBefore=4, spaceAfter=6),
        "h2": style("h2", fontName="Helvetica-Bold", fontSize=12.5, leading=16, spaceAfter=3),
        "h3": style("h3", fontName="Helvetica-Bold", fontSize=10.5, leading=14, spaceAfter=3),
        "h3_inline": style("h3_inline", fontSize=10.5, leading=14),
        "body": style("body", fontSize=9.5, leading=13.5),
        "muted": style("muted", fontSize=8.5, leading=12, textColor=_MUTED),
        "label": style("label", fontName="Helvetica-Bold", fontSize=8, leading=11, textColor=_MUTED, spaceBefore=3),
        "cell": style("cell", fontSize=8.5, leading=11.5, spaceAfter=2),
        "cell_center": style("cell_center", fontSize=9, leading=12, alignment=TA_CENTER),
        "th": style("th", fontName="Helvetica-Bold", fontSize=8.5, leading=11),
        # CJK word wrap breaks anywhere, so a long URL or a JSON body without
        # spaces wraps instead of running off the page.
        "mono": style("mono", fontName="Courier", fontSize=7.5, leading=9.5, wordWrap="CJK"),
        "tile_value": style("tile_value", fontName="Helvetica-Bold", fontSize=18, leading=22, alignment=TA_CENTER),
        "tile_label": style("tile_label", fontSize=8, leading=10, alignment=TA_CENTER, textColor=_MUTED),
        "chip": style(
            "chip", fontName="Helvetica-Bold", fontSize=7.5, leading=10, alignment=TA_CENTER, textColor=colors.white
        ),
    }


def _chip(severity: str, styles: dict[str, ParagraphStyle]) -> Table:
    chip = Table([[Paragraph(_text(severity.upper()), styles["chip"])]], colWidths=[20 * mm])
    chip.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), SEVERITY_COLOURS.get(severity, _MUTED)),
                ("TOPPADDING", (0, 0), (-1, -1), 2),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
            ]
        )
    )
    return chip


def _key_values(rows: list[tuple[str, str]], styles: dict[str, ParagraphStyle]) -> Table:
    table = Table(
        [[Paragraph(_text(k), styles["label"]), Paragraph(_text(v), styles["cell"])] for k, v in rows],
        colWidths=[34 * mm, 140 * mm],
    )
    table.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 1),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 1),
            ]
        )
    )
    return table


def _grid_style() -> TableStyle:
    return TableStyle(
        [
            ("BACKGROUND", (0, 0), (-1, 0), _PANEL),
            ("LINEBELOW", (0, 0), (-1, -1), 0.5, _RULE),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ]
    )


def _body(body: Any) -> str:
    rendered = body if isinstance(body, str) else json.dumps(body, indent=2, ensure_ascii=False)
    if len(rendered) > _MAX_BODY_CHARS:
        rendered = rendered[:_MAX_BODY_CHARS] + " [truncated]"
    return rendered


def _count(number: int, noun: str) -> str:
    return f"{number} {noun}" if number == 1 else f"{number} {noun}s"


def _clip(value: str, limit: int) -> str:
    value = str(value or "")
    return value if len(value) <= limit else value[:limit] + " [truncated]"


def _when(moment: datetime | None) -> str:
    return moment.strftime("%Y-%m-%d %H:%M %Z").strip() if moment else "-"


def _plain(value: str) -> str:
    """Text the built-in fonts can draw: characters outside Windows-1252 become '?'."""
    return str(value).encode("cp1252", "replace").decode("cp1252")


def _text(value: Any) -> str:
    """Escaped for paragraph markup, drawable, with line breaks kept."""
    return escape(_plain(value)).replace("\n", "<br/>")


def _mono(value: str) -> str:
    """As _text, keeping each line's indentation, which paragraphs collapse."""
    lines = []
    for line in escape(_plain(value)).split("\n"):
        stripped = line.lstrip(" ")
        lines.append("&nbsp;" * (len(line) - len(stripped)) + stripped)
    return "<br/>".join(lines)
