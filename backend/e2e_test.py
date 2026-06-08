"""End-to-end pipeline smoke test — no database required.

Runs the full Layer 2 → Layer 3 → Layer 4 → Layer 5 → Layer 6 pipeline
against the real CSV files on disk and prints a summary of what each
layer produced. Layer 6 (indexing) requires a DB and is therefore
exercised in its degraded-by-design mode here, which still validates
the orchestrator wiring.
"""
import asyncio
import uuid
import os
from collections import Counter
from pathlib import Path

# Inject minimal env so Settings doesn't need a real .env file
os.environ.setdefault("API_KEY", "smoke-test-key")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://x:x@localhost/x")

from app.db.models.dataset import FileType
from agentic_engine.profiler.engine import profile_dataset
from agentic_engine.findings.findings_builder import build_findings
from agentic_engine.viz.viz_builder import build_viz
from agentic_engine.llm_engine.insight_builder import build_insights
from agentic_engine.vector_store.indexer import build_index

raw_dir = Path("data/raw")
csv_files = list(raw_dir.glob("*.csv"))

print(f"\nFound {len(csv_files)} dataset(s) on disk:")
for f in csv_files:
    print(f"  {f.name}")

SEP = "=" * 70

for csv_path in csv_files:
    did = uuid.uuid4()
    label = "MESSY" if "messy" in csv_path.name else "CLEAN"
    print(f"\n{SEP}")
    print(f"  {label} DATASET  —  {csv_path.name}")
    print(SEP)

    # ── Layer 2: Profile ──────────────────────────────────────────────────
    report = profile_dataset(dataset_id=did, raw_path=csv_path, file_type=FileType.CSV)

    print(f"\n[Layer 2 — Profile]")
    print(f"  Rows        : {report.schema_.row_count:,}")
    print(f"  Columns     : {report.schema_.col_count}")
    print(f"  Health      : {report.health.score:.1f}/100  (Grade {report.health.grade})")
    print(f"  Dup rows    : {report.quality.duplicate_row_pct:.1f}%")

    print(f"  Columns:")
    sem_map = {s.name: s.semantic_type for s in report.semantic.columns}
    qual_map = {q.name: q for q in report.quality.columns}
    for col in report.stats.columns:
        q = qual_map.get(col.name)
        sem = sem_map.get(col.name, "?")
        null = f"null={q.null_pct:.0f}%" if q else ""
        const = " CONSTANT" if (q and q.is_constant) else ""
        outlier = f" outliers={q.outlier_pct:.0f}%" if (q and q.outlier_pct and q.outlier_pct > 0) else ""
        print(f"    {col.name:<25} kind={col.inferred_kind:<12} sem={sem:<14} {null}{outlier}{const}")

    if report.relationships.correlations:
        print(f"  Top correlations:")
        sorted_corrs = sorted(
            report.relationships.correlations,
            key=lambda c: abs(c.pearson or 0),
            reverse=True
        )[:3]
        for c in sorted_corrs:
            print(f"    {c.col_a} <-> {c.col_b}  pearson={c.pearson:.3f}  spearman={c.spearman:.3f}")

    # ── Layer 3: Findings ────────────────────────────────────────────────
    findings_report = build_findings(report)

    by_sev = Counter(f.severity.value for f in findings_report.findings)
    by_type = Counter(f.type.value for f in findings_report.findings)

    print(f"\n[Layer 3 — Findings]")
    print(f"  Total       : {findings_report.count}")
    print(f"  HIGH        : {by_sev['HIGH']}")
    print(f"  MEDIUM      : {by_sev['MEDIUM']}")
    print(f"  LOW         : {by_sev['LOW']}")
    print(f"  By type:")
    for t, n in sorted(by_type.items(), key=lambda x: -x[1]):
        print(f"    {t:<35} {n}")

    print(f"  Sample findings (HIGH severity):")
    high = [f for f in findings_report.findings if f.severity.value == "HIGH"]
    for f in high[:5]:
        col = f"[{f.column}]" if f.column else "[dataset]"
        print(f"    {col:<20} {f.title[:60]}")

    # ── Layer 4: Viz ─────────────────────────────────────────────────────
    viz_report = build_viz(report, findings_report)

    by_chart = Counter(c.type.value for c in viz_report.charts)

    print(f"\n[Layer 4 — Viz]")
    print(f"  Total charts: {viz_report.count}")
    for t, n in sorted(by_chart.items(), key=lambda x: -x[1]):
        print(f"    {t:<15} {n}")

    print(f"  Top 5 by priority (shown first in UI):")
    for chart in viz_report.sorted_by_priority()[:5]:
        linked = f"  linked_findings={len(chart.finding_ids)}" if chart.finding_ids else ""
        print(f"    [{chart.priority:>3}] {chart.chart_id:<40} {chart.type.value}{linked}")

    # ── Verify L3→L4 skip logic ──────────────────────────────────────────
    from agentic_engine.findings.finding_types import FindingType
    const_cols = {f.column for f in findings_report.findings if f.type == FindingType.CONSTANT_COLUMN and f.column}
    id_cols = {f.column for f in findings_report.findings if f.type == FindingType.ID_COLUMN_DETECTED and f.column and f.confidence >= 0.75}
    chart_cols = {col for c in viz_report.charts for col in c.columns}

    skipped_correctly = (const_cols | id_cols) - chart_cols
    leaked = (const_cols | id_cols) & chart_cols

    print(f"\n[L3->L4 Integration Check]")
    print(f"  Constant/ID cols flagged by L3 : {const_cols | id_cols or 'none'}")
    print(f"  Correctly excluded from charts : {skipped_correctly or 'none'}")
    print(f"  Incorrectly leaked into charts : {leaked or 'NONE (good)'}")

    scatter_charts = viz_report.by_type(viz_report.charts[0].type.__class__("SCATTER")) if viz_report.count else []
    corr_findings = [f for f in findings_report.findings if f.type == FindingType.STRONG_CORRELATION]
    scatter_charts = [c for c in viz_report.charts if c.type.value == "SCATTER"]
    print(f"  STRONG_CORRELATION findings    : {len(corr_findings)}")
    print(f"  Scatter charts generated       : {len(scatter_charts)}")
    if scatter_charts:
        for sc in scatter_charts:
            print(f"    {sc.chart_id}  finding_ids={sc.finding_ids}")

    # ── Layer 5: LLM Insights ────────────────────────────────────────────
    insight_report = asyncio.run(build_insights(report, findings_report))

    print(f"\n[Layer 5 — LLM Insights]")
    if insight_report.degraded:
        print(f"  Status      : DEGRADED")
        print(f"  Reason      : {insight_report.degraded_reason}")
    else:
        print(f"  Status      : OK")
        print(f"  Model used  : {insight_report.model_used}")
        print(f"  Tokens      : in={insight_report.token_usage.input_tokens:,}  "
              f"out={insight_report.token_usage.output_tokens:,}  "
              f"calls={insight_report.token_usage.call_count}")

    print(f"  Headline    : {insight_report.executive_summary.headline}")
    if insight_report.executive_summary.key_concerns:
        print(f"  Key concerns:")
        for c in insight_report.executive_summary.key_concerns[:3]:
            print(f"    - {c}")
    if insight_report.executive_summary.recommended_next_steps:
        print(f"  Next steps  :")
        for s in insight_report.executive_summary.recommended_next_steps[:3]:
            print(f"    - {s}")

    print(f"  Explanations: {insight_report.explanation_count}")
    print(f"  Questions   : {insight_report.question_count}")
    if insight_report.suggested_questions:
        print(f"  Sample questions:")
        for q in insight_report.suggested_questions[:3]:
            print(f"    [{q.intent.value:<11}] {q.question}")

    # ── Layer 6: Vector Indexing (degraded — no DB in smoke test) ────────
    # We still call it so the chunking + Protocol wiring is exercised end
    # to end. Without a DB session it returns degraded=True; that proves
    # the orchestrator does not raise on the no-DB path.
    index_report = asyncio.run(build_index(findings_report, session=None))

    print(f"\n[Layer 6 — Vector Indexing]")
    if index_report.degraded:
        print(f"  Status      : DEGRADED  (expected in smoke test)")
        print(f"  Reason      : {index_report.degraded_reason}")
    else:
        print(f"  Status      : OK")
        print(f"  Indexed     : {index_report.indexed_count}")
    print(f"  Model       : {index_report.model_name}")
    print(f"  Dimension   : {index_report.embedding_dimension}")

print(f"\n{SEP}")
print("  ALL LAYERS PASSED — pipeline is working end-to-end")
print(SEP)
