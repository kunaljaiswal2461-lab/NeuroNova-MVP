"""PDF summary report renderer.

Pure helper — no FastAPI or DB imports. Takes the already-loaded artifact
dicts (profile / findings / insights) plus dataset metadata and renders a
one-shot PDF summary via reportlab platypus.
"""
from __future__ import annotations

import io
from datetime import datetime, timezone
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

_DEGRADED_HEADLINE = "Insights unavailable"


def _esc(value: object) -> str:
    """Escape free text so reportlab does not treat ``<...>`` as markup."""
    if value is None:
        return ""
    return escape(str(value))


def _insights_degraded(insights: dict | None) -> bool:
    """True when insights are absent or the degraded placeholder payload."""
    if not insights:
        return True
    summary = insights.get("executive_summary") or {}
    if not summary:
        return True
    if summary.get("headline") == _DEGRADED_HEADLINE:
        return True
    return not any(
        summary.get(key)
        for key in ("key_strengths", "key_concerns", "recommended_next_steps")
    )


def build_summary_pdf(
    *,
    metadata: dict,
    profile: dict,
    findings: list[dict] | None,
    insights: dict | None,
) -> bytes:
    """Render a PDF summary report and return its raw bytes.

    Parameters
    ----------
    metadata:
        ``{original_name, file_type, row_count, col_count, dataset_id, status}``.
    profile:
        ``load_report`` output (required, non-None).
    findings:
        The raw ``findings`` list — may be ``[]`` or ``None``.
    insights:
        ``load_insights_raw`` output — may be ``None`` or a degraded payload.
    """
    styles = getSampleStyleSheet()
    title_style = styles["Title"]
    h2 = styles["Heading2"]
    body = styles["BodyText"]
    italic = styles["Italic"]
    bullet = ParagraphStyle("SummaryBullet", parent=body, leftIndent=6 * mm)
    cell = ParagraphStyle("TableCell", parent=body, fontSize=8, leading=10)

    story: list = []

    # ── Title / metadata header ───────────────────────────────────────────
    story.append(Paragraph(_esc(metadata.get("original_name") or "Dataset"), title_style))
    story.append(Paragraph("Dataset Summary Report", h2))
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    meta_lines = [
        f"<b>File type:</b> {_esc(metadata.get('file_type'))}",
        f"<b>Rows:</b> {_esc(metadata.get('row_count'))} &nbsp;&nbsp; "
        f"<b>Columns:</b> {_esc(metadata.get('col_count'))}",
        f"<b>Dataset ID:</b> {_esc(metadata.get('dataset_id'))}",
        f"<b>Status:</b> {_esc(metadata.get('status'))}",
        f"<b>Generated:</b> {generated_at}",
    ]
    for line in meta_lines:
        story.append(Paragraph(line, body))
    story.append(Spacer(1, 6 * mm))

    # ── Health & Quality ─────────────────────────────────────────────────
    story.append(Paragraph("Health &amp; Quality", h2))
    health = profile.get("health") or {}
    schema = profile.get("schema") or {}
    story.append(
        Paragraph(
            f"<b>Health score:</b> {_esc(health.get('score'))} "
            f"(grade {_esc(health.get('grade'))})",
            body,
        )
    )
    story.append(
        Paragraph(
            f"<b>Schema:</b> {_esc(schema.get('row_count'))} rows × "
            f"{_esc(schema.get('col_count'))} columns",
            body,
        )
    )
    components = health.get("components") or {}
    if components:
        rows = [["Component", "Score"]] + [
            [Paragraph(_esc(name).title(), cell), Paragraph(f"{value:.1f}", cell)]
            for name, value in components.items()
        ]
        table = Table(rows, colWidths=[60 * mm, 30 * mm], hAlign="LEFT")
        table.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2c3e50")),
                    ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                    ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                    ("FONTSIZE", (0, 0), (-1, -1), 8),
                    ("GRID", (0, 0), (-1, -1), 0.25, colors.grey),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f4f6f7")]),
                ]
            )
        )
        story.append(Spacer(1, 2 * mm))
        story.append(table)
    story.append(Spacer(1, 6 * mm))

    # ── Executive Summary ─────────────────────────────────────────────────
    story.append(Paragraph("Executive Summary", h2))
    if _insights_degraded(insights):
        story.append(Paragraph("LLM insights unavailable for this dataset.", italic))
    else:
        summary = insights.get("executive_summary") or {}
        if summary.get("headline"):
            story.append(Paragraph(f"<b>{_esc(summary['headline'])}</b>", body))
        if summary.get("overview"):
            story.append(Paragraph(_esc(summary["overview"]), body))
        for label, key in (
            ("Key strengths", "key_strengths"),
            ("Key concerns", "key_concerns"),
            ("Recommended next steps", "recommended_next_steps"),
        ):
            items = summary.get(key) or []
            if not items:
                continue
            story.append(Spacer(1, 2 * mm))
            story.append(Paragraph(f"<b>{label}</b>", body))
            for item in items:
                story.append(Paragraph(f"• {_esc(item)}", bullet))
    story.append(Spacer(1, 6 * mm))

    # ── Findings ──────────────────────────────────────────────────────────
    story.append(Paragraph("Findings", h2))
    findings = findings or []
    if not findings:
        story.append(Paragraph("No findings recorded.", italic))
    else:
        header = ["Severity", "Type", "Column", "Title"]
        rows = [header]
        for finding in findings:
            rows.append(
                [
                    Paragraph(_esc(finding.get("severity")), cell),
                    Paragraph(_esc(finding.get("type")), cell),
                    Paragraph(_esc(finding.get("column") or "—"), cell),
                    Paragraph(_esc(finding.get("title")), cell),
                ]
            )
        table = Table(rows, colWidths=[20 * mm, 42 * mm, 30 * mm, 78 * mm], hAlign="LEFT")
        table.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2c3e50")),
                    ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                    ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                    ("FONTSIZE", (0, 0), (-1, -1), 8),
                    ("GRID", (0, 0), (-1, -1), 0.25, colors.grey),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f4f6f7")]),
                ]
            )
        )
        story.append(table)

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        title=f"{metadata.get('original_name') or 'Dataset'} — Summary",
        leftMargin=18 * mm,
        rightMargin=18 * mm,
        topMargin=16 * mm,
        bottomMargin=16 * mm,
    )
    doc.build(story)
    return buf.getvalue()
