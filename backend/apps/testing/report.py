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

_INK = colors.HexColor("#111827")
_MUTED = colors.HexColor("#6b7280")
_RULE = colors.HexColor("#e5e7eb")
_PANEL = colors.HexColor("#f3f4f6")
_ACCENT = colors.HexColor("#1e3a8a")

# A body longer than this is cut on the page; the full text stays in the API.
_MAX_BODY_CHARS = 1_500


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


@dataclass
class ReportEndpoint:
    label: str
    cases: int
    passed: int
    failed: int
    errors: int
    findings: int


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
    story += _at_a_glance(report, styles)
    if report.analysis is not None:
        story += _analysis_section(report.analysis, styles)
    if report.endpoints:
        story += _endpoints_section(report.endpoints, styles)
    story += _findings_section(report, styles)

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


def _at_a_glance(report: RunReport, styles: dict[str, ParagraphStyle]) -> list[Any]:
    results = report.results
    tiles = [
        ("Test cases", results.get("total", 0)),
        ("Passed", results.get("passed", 0)),
        ("Failed", results.get("failed", 0)),
        ("Errors", results.get("error", 0)),
        ("Findings", report.findings_total),
    ]
    tile_table = Table(
        [
            [Paragraph(str(value), styles["tile_value"]) for _, value in tiles],
            [Paragraph(label, styles["tile_label"]) for label, _ in tiles],
        ],
        colWidths=[34.8 * mm] * len(tiles),
    )
    tile_table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), _PANEL),
                ("LINEAFTER", (0, 0), (-2, -1), 2, colors.white),
                ("TOPPADDING", (0, 0), (-1, 0), 8),
                ("BOTTOMPADDING", (0, -1), (-1, -1), 8),
            ]
        )
    )

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

    return [
        Paragraph("At a glance", styles["h1"]),
        tile_table,
        Spacer(1, 5 * mm),
        Paragraph("Findings by severity", styles["h3"]),
        severity_table,
        Spacer(1, 6 * mm),
    ]


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


def _endpoints_section(endpoints: list[ReportEndpoint], styles: dict[str, ParagraphStyle]) -> list[Any]:
    header = ["Endpoint", "Cases", "Passed", "Failed", "Errors", "Findings"]
    rows = [[Paragraph(h, styles["th"]) for h in header]]
    for endpoint in endpoints:
        rows.append(
            [
                Paragraph(_text(endpoint.label), styles["mono"]),
                *(
                    Paragraph(str(value), styles["cell_center"])
                    for value in (
                        endpoint.cases,
                        endpoint.passed,
                        endpoint.failed,
                        endpoint.errors,
                        endpoint.findings,
                    )
                ),
            ]
        )
    table = Table(rows, colWidths=[84 * mm] + [18 * mm] * 5, repeatRows=1)
    table.setStyle(_grid_style())
    return [CondPageBreak(40 * mm), Paragraph("Endpoints", styles["h1"]), table, Spacer(1, 6 * mm)]


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
    story.append(Spacer(1, 2 * mm))
    for finding in report.findings:
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

    rows: list[tuple[str, str]] = [
        ("Category", finding.category.replace("_", " ")),
        ("Endpoint", finding.endpoint or "-"),
    ]
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
    if finding.request_url:
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
