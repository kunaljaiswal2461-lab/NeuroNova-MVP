# NeuroNova MVP — Full Project Memory & Onboarding Doc

> **Purpose:** Self-contained context dump for any Claude Code session (especially a
> fresh one on a new account). Read this top-to-bottom before answering anything
> non-trivial. Merges the project `CLAUDE.md` with the full Notion deep-spec
> ("NeuroNova — Deep Product Spec, Research-Validated") so nothing lives only in
> Notion.
>
> **Last updated:** 2026-05-29
> **Owner:** vs2005005@gmail.com
> **Repo root:** `C:\Users\hp\.gemini\antigravity\scratch\NeuroNova-MVP-NEW-`
>
> **Convention used throughout this doc:** every feature/section is marked
> **✅ MVP** (build now) or **⏳ Post-MVP** (design-aware, build later). If those
> tags conflict with anything elsewhere, this doc wins for *intent*; `CLAUDE.md`
> wins for *current code-checked-in truth*.

---

## 0. How to use this file (Claude, read this first)

1. This file is the canonical onboarding doc. `CLAUDE.md` is the short-form spec.
   If they conflict on what's *built*, `CLAUDE.md` wins. If they conflict on what
   *should* be built, this doc wins.
2. The auto-memory dir at
   `C:\Users\hp\.claude\projects\C--Users-hp--gemini-antigravity-scratch-NeuroNova-MVP-NEW-\memory\`
   is the *runtime* memory store (per-account, per-machine). It will be **empty** on
   a fresh account. Rebuild it lazily — write a memory only when something genuinely
   surprising or non-derivable appears.
3. Do not mirror sections 4–18 into auto-memory. They're derivable from this file +
   the codebase; duplicating is wasted bytes.
4. If the user says "switch context" / "new account" — point them at this file plus
   `CLAUDE.md`. No other transfer needed; both are checked into git.

---

## 1. Product (what NeuroNova actually is)

**One-line pitch:** An AI-powered data analysis platform that combines automated
EDA, semantic column intelligence, and conversational analytics in one product.

**Slightly longer:** A user uploads a tabular dataset (CSV/Excel/JSON/Parquet).
The system profiles it across six dimensions (schema, stats, semantic, quality,
relationships, health), extracts confidence-scored *Findings* (typed observations
like `HIGH_NULLABILITY`, `STRONG_CORRELATION`), embeds those findings into pgvector,
and exposes a dual-mode chat: RAG over findings for "what's interesting?" questions,
and NL→Pandas for "compute X for me" questions. The LLM never touches raw data —
only structured Finding JSON — so it can't hallucinate stats.

**Who it's for:** analysts, data scientists, PMs who want fast, trustworthy EDA
without writing notebooks.

**Stage:** MVP, 10-day build window. Today is day ~7 of that window.

---

## 2. The Moat — the *actual* differentiators

1. **Semantic column intelligence.** AutoEDA tools say a column is `int64`.
   NeuroNova says it's `FINANCIAL`, flags `15% negative values` as suspicious, and
   explains *why* that matters. Generic tools can't do this.
2. **Confidence-scored Findings.** Each finding carries a 0.0–1.0 confidence so the
   UI and LLM can rank/filter. Confidence 0.98 = near certain; 0.62 = soft signal.
   Makes the intelligence auditable.
3. **Retrieval-grounded answers.** The conversational layer never hallucinates
   statistics because it only reasons over pre-computed, deterministic findings.
   Every answer is traceable to a Finding with specific evidence numbers.
4. **Dual-mode chat in one interface.** RAG (insights) + NL→Pandas (direct compute),
   auto-routed. Most "chat with your data" products do one or the other.
5. **Dataset memory.** Findings + summaries + embeddings persist; user can revisit a
   dataset weeks later and pick up the conversation.

Differentiates from: ydata-profiling / pandas-profiling (no semantics, no chat) and
generic "chat with CSV" wrappers (no profiling, hallucinated stats).

---

## 3. The 7 Layers (architecture spine)

| # | Layer | Status | Notes |
|---|-------|--------|-------|
| 1 | Dataset Upload System | ✅ Complete | FastAPI upload, validation, `DatasetRecord` lifecycle |
| 2 | Profiling Engine | ✅ Complete | schema + stats + semantic + quality + relationship + health |
| 3 | Semantic Findings Layer | ✅ Complete | typed `Finding` objects, extractors, JSON persistence |
| 4 | Visualization Metadata Layer | ✅ Complete | `VizReport` JSON — chart specs derived from findings |
| 5 | LLM Insight Layer | ✅ Complete | exec summary + per-finding explanations + suggested questions, 3 parallel calls |
| 6 | Vector Retrieval Layer | ✅ Complete | pgvector index over finding embeddings, atomic re-indexing, pre-filter + IVFFlat cosine |
| 7 | Conversational Analytics Layer | ⏳ Next | dual-mode chat (RAG + NL→Pandas), streaming |

Status as of 2026-05-29: Layers 1–6 merged on `main`. Layer 7 is the
immediate next milestone. 108/108 unit tests passing.

---

## 4. 🔴 What was explicitly cut from MVP (do not re-add without asking)

The original ChatGPT spec mixed MVP-ready decisions with Post-MVP complexity. These
are the things deferred — each is design-ready but unbuilt.

| Feature | Why Post-MVP | MVP substitute |
|---|---|---|
| AWS S3 + presigned URLs | Adds infra/IAM/cost before any users | Local filesystem behind `StorageBackend` Protocol — swap to S3 in one config line |
| Redis + Celery async queue | Heavy infra; Celery is sync-first, awkward with FastAPI async | FastAPI `BackgroundTasks` + job-status polling (`GET /jobs/{job_id}`) — same API contract as Celery |
| Next.js + TypeScript frontend | Toolchain scaffolding eats days | Streamlit for MVP, Next.js for v1.0 |
| JWT auth | No users yet | Static API key via `X-API-Key` header from env var |
| OpenTelemetry + Grafana + Prometheus | Zero users to monitor | `structlog` structured JSON logs only |
| LangGraph agents | Agents are v2; pipeline is sequential, not agentic | Keep `agentic_engine/agents/` as a stub directory |
| Multicollinearity / causal inference | Complex stats, rarely needed at MVP | Pearson + Spearman is enough |
| Monitoring dashboard (6th UI page) | No ops team yet | Fold into pipeline-status endpoint later |
| Checksum validation + encrypted-at-rest blobs | Pre-product security theater | MIME type + 100MB size cap is sufficient |

---

## 5. Tech Stack — Research-Validated

### MVP (locked)

```
Backend:        FastAPI + Python 3.11
Database:       PostgreSQL (already built)
Vector Search:  pgvector (extension on existing Postgres — zero new infra)
Embeddings:     OpenAI text-embedding-3-small ($0.02/1M tokens, same SDK)
Task Queue:     FastAPI BackgroundTasks
Data Layer:     Polars (primary — 5–50× faster than Pandas for large files)
                Pandas (Excel fallback only)
Stats:          SciPy (skewness, kurtosis, statistical tests)
LLM:            OpenAI gpt-4o + gpt-4o-mini (one SDK, two tiers)
UI:             Streamlit
Logging:        structlog (structured JSON, zero config)
Storage:        Local FS behind StorageBackend Protocol
Auth:           Static API key via X-API-Key header
```

### Post-MVP / v1.0

```
Frontend:       Next.js + TypeScript + TailwindCSS
Task Queue:     ARQ (async-native, Redis-backed, FastAPI-native) — preferred over Celery
Storage:        AWS S3 (S3Storage implementing StorageBackend)
Auth:           JWT (Supabase Auth or custom)
Observability:  OpenTelemetry → Grafana + Prometheus
Agents:         LangGraph
```

**Why ARQ over Celery for Post-MVP:** Celery was built for sync Python. FastAPI is
async. ARQ is async-native — uses Redis, supports retries, job status, timeouts,
and integrates with FastAPI without sync/async bridging complexity.

---

## 6. Key architecture decisions (the *why*)

- **pgvector over ChromaDB** — PostgreSQL is already in the stack. `CREATE EXTENSION
  vector` is zero new infrastructure. Allows SQL joins:
  `SELECT findings.* FROM findings JOIN vectors ON ... WHERE vectors.dataset_id = $1
  ORDER BY embedding <=> $2` — filter + rank in one query, no two-step dance.
  Managed Postgres (Supabase, Neon, RDS) all support pgvector out of the box.
  pgvector latency (~3.59s) ≈ ChromaDB (~4.04s) at MVP scale. ChromaDB is fine for
  pure prototypes, but since Postgres is already committed, pgvector is obvious.
- **OpenAI `text-embedding-3-small` over local `all-MiniLM-L6-v2`** — already calling
  OpenAI SDK, so consolidate. MTEB ~62 vs ~56, $0.02/1M tokens. At MVP scale total
  embedding cost is <$0.01. One less local model to manage.
- **BackgroundTasks API contract == Celery's** — upgrade is a pure backend swap
  (`background_tasks.add_task(...)` → `celery_task.delay(...)`, add `REDIS_URL`).
  No API change.
- **LLM never sees raw data** — only structured Findings as JSON. This is the
  hallucination firewall; do not break it.
- **StorageBackend Protocol** — LocalStorage today, S3Storage Post-MVP, swappable
  in one config line.
- **Disk for large JSON blobs** (profiles, findings, viz, llm_cache); PostgreSQL
  for anything relational or queryable. Don't put 5MB JSON in a Postgres column.
- **Single dataset per chat session** for MVP — cross-dataset reasoning is Post-MVP.

---

## 7. Layer 1 — Dataset Upload System ✅ MVP

Entry point. Validates, stores, and registers a dataset so every downstream layer
can reference it by a stable `dataset_id`.

**Supported formats:** CSV (any delimiter — auto-detect), XLSX/XLS, JSON (flat array
or newline-delimited), Parquet.

**Architecture:**

```
Client → POST /api/v1/upload (multipart)
       → MIME validation + size check (≤100MB for MVP)
       → Save to data/raw/<dataset_id>_<filename>
       → Register DatasetRecord in PostgreSQL (metadata only)
       → Enqueue profiling job via BackgroundTasks
       → Return {dataset_id, status: "queued"} immediately
```

**Why background immediately, not sync:** a 50MB CSV with 1M rows takes 8–25s to
profile fully. Blocking the HTTP response for that is wrong from day one. Fix is
**not** Celery — it's FastAPI `BackgroundTasks`, which runs in the same process
*after* the response is sent. Contract identical to Celery's.

**`DatasetRecord` (PostgreSQL, `app/db/`):**

```
id:            UUID (PK)
filename:      str
original_name: str
file_type:     enum (CSV | XLSX | JSON | PARQUET)
size_bytes:    int
row_count:     int | None       ← filled after profiling
col_count:     int | None       ← filled after profiling
status:        enum (UPLOADED | QUEUED | PROFILING | COMPLETE | FAILED)
uploaded_at:   timestamp
profiled_at:   timestamp | None
```

(In practice the lifecycle has grown intermediate states as layers landed:
`UPLOADED → QUEUED → PROFILING → FINDINGS → VIZ → COMPLETE | FAILED`. The PG enum
should be expanded as each layer ships.)

**Storage abstraction (critical for S3 migration):**

```python
# agentic_engine/tools/uploader/storage.py
class StorageBackend(Protocol):
    def save(self, file_id: str, filename: str, data: bytes) -> Path: ...
    def get_path(self, file_id: str, filename: str) -> Path: ...

class LocalStorage(StorageBackend):  # MVP
    ...

class S3Storage(StorageBackend):     # Post-MVP, same interface
    ...
```

Move to S3 = swap `LocalStorage` → `S3Storage` in `app/core/config.py`. Nothing
else changes.

---

## 8. Layer 2 — Profiling Engine ✅ MVP

The most important layer. Everything downstream depends on output quality. All
six sub-profilers run inside `agentic_engine/profiler/engine.py` and produce a
single `ProfileReport`.

### 8a. Schema Intelligence

Detects the *true* type of each column — not just pandas dtype, but semantic type.

**Multi-pass type inference:**
1. Raw dtype detection (pandas/Polars native)
2. Content sampling — sample 1,000 rows, apply pattern tests
3. Semantic override — if column name + value pattern match a semantic type

**Detected dtypes:**

```
NUMERIC      — integers, floats, currencies
CATEGORICAL  — low-cardinality strings
DATETIME     — any parseable date/time format
BOOLEAN      — True/False, 1/0, yes/no, Y/N
TEXT         — high-cardinality free-form strings (NLP candidates)
MIXED        — column has inconsistent types (quality flag)
```

**Per-column schema stats:** cardinality, cardinality_ratio (unique/total),
null_count, null_pct, uniqueness_ratio, detected_format (DATETIME),
schema_snapshot `{dtype_raw, dtype_inferred, sample_values: list[5]}`.

### 8b. Statistical Profiling

All stats computed deterministically. LLMs never touch raw data.

**NUMERIC:** mean, median, std, min, max; p5, p25, p50, p75, p95; skewness,
kurtosis (`scipy.stats`); outlier_count + outlier_pct (IQR: `Q1 - 1.5·IQR`,
`Q3 + 1.5·IQR`); zero_count, negative_count (useful for financial data).

**CATEGORICAL:** mode, mode_frequency; top_15_values `[{value, count, pct}]`;
bottom_5_values (rare-noise detection); entropy (log2 scale, distribution balance).

**DATETIME:** min_date, max_date, date_range_days; detected_format (e.g.
`%Y-%m-%d`); gap_count (missing periods given inferred frequency);
inferred_frequency (daily / weekly / monthly / irregular).

**Dataset-level:** row_count, col_count, duplicate_row_count, duplicate_row_pct,
memory_usage_bytes, overall_null_rate, mixed_type_column_count.

### 8c. Semantic Intelligence ✅ MVP — the moat

What separates NeuroNova from generic AutoEDA. Most profilers stop at data type;
NeuroNova identifies *what the column represents*.

**Semantic types:**

```
IDENTIFIER   — pure IDs (UUID, sequential int, high cardinality, no analytical value)
FINANCIAL    — price, revenue, cost, salary, amount (name match + numeric)
TEMPORAL     — date, time, year, quarter, week (beyond raw DATETIME)
GEOGRAPHIC   — lat/lon pairs, country, city, zip, region
EMAIL        — >80% of values match email regex
PHONE        — phone number patterns
URL          — http/https patterns
NAME         — person names (first/last/full name patterns)
FREE_TEXT    — long text, likely NLP candidate (avg token count > 10)
BINARY_FLAG  — exactly 2 unique values after null removal
```

**Detection — two-signal approach:**

```python
# Signal 1 — column-name matching (fast, pattern-based)
NAME_PATTERNS = {
    'FINANCIAL':  ['price', 'revenue', 'cost', 'salary', 'amount', 'fee', 'pay'],
    'GEOGRAPHIC': ['lat', 'lon', 'latitude', 'longitude', 'country', 'city', 'zip'],
    'EMAIL':      ['email', 'mail'],
    ...
}
# Signal 2 — value sampling (sample 200 rows, apply validators)
# Final semantic_type = name_signal OR value_signal (value takes priority)
```

Semantic type becomes the grounding context for LLM prompts and conversational
responses.

### 8d. Quality Intelligence ✅ MVP

Detects data-quality problems; each becomes a Finding (Layer 3).

**Missingness:** null_pct per column; missing pattern (MCAR random vs MAR
structured — nulls clustering in specific rows); null correlation (do nulls in
column A correlate with nulls in column B?).

**Anomaly detection:**

```
CONSTANT_COLUMN         — zero variance after null removal
NEAR_DUPLICATE_COLUMN   — >95% overlap with another column
ID_COLUMN               — cardinality == row_count, no analytical value
ENTIRELY_NULL           — 100% null
IMBALANCED_CATEGORICAL  — single value > 80% of rows
MIXED_TYPES             — multiple detected dtypes in one column
SUSPICIOUS_DISTRIBUTION — extreme skewness + outliers together
INVALID_RANGE           — values outside logical bounds (age > 150, negative price)
POTENTIAL_LEAKAGE       — IDENTIFIER semantic type present (risky in ML features)
```

### 8e. Relationship Intelligence ✅ MVP

- Pearson correlation matrix (all numeric columns)
- Spearman correlation matrix (rank-based, handles non-linear)
- Flag strong pairs: `|r| > 0.75` → HIGH, `0.50–0.75` → MEDIUM
- Flag multicollinear groups (Post-MVP: VIF scores)
- Categorical associations: Cramér's V for categorical × categorical pairs

### 8f. Health Intelligence ✅ MVP

Unified dataset health score — the single most communicable output of the profiler.

```
Dataset Health Score: 74/100   GRADE: B

Dimension Breakdown:
  Completeness Score:   85/100   (null rate across all columns)
  Uniqueness Score:     90/100   (duplicate row rate)
  Validity Score:       70/100   (type consistency, invalid ranges)
  Consistency Score:    65/100   (mixed types, suspicious distributions)
  Outlier Score:        60/100   (outlier density across numeric columns)
  Leakage Risk Score:   80/100   (presence of ID/leakage columns)
```

**Grade thresholds:** A ≥ 90 | B 75–89 | C 60–74 | D 45–59 | F < 45. All thresholds
configurable in `app/core/config.py`.

---

## 9. Layer 3 — Semantic Findings Layer ✅ MVP

The bridge between raw stats and human-readable intelligence. "Semantic Findings"
is the canonical name (replaces earlier generic "InsightObject").

### `Finding` model

```python
class Finding(BaseModel):
    finding_id: UUID
    type: FindingType            # enum, extensible
    severity: Severity           # HIGH | MEDIUM | LOW
    confidence: float            # 0.0–1.0  ← KEY ADDITION
    column: str | None           # None for dataset-level findings
    title: str                   # short, human-readable
    description: str             # 1–2 sentence plain English
    evidence: dict[str, Any]     # exact triggering numbers
    semantic_context: str | None # semantic type if relevant
    generated_at: datetime
```

### Why confidence matters

A 42% null rate on a column with 1M rows is HIGH confidence. The same null rate on
a 50-row test dataset is MEDIUM confidence — sample too small. Confidence is a
function of sample_size, effect_size, and statistical threshold. This makes
findings more honest and more useful as LLM grounding context.

### `FindingType` enum

```
HIGH_NULLABILITY
CONSTANT_COLUMN
ID_COLUMN_DETECTED
INVALID_RANGE_VALUES
IMBALANCED_DISTRIBUTION
HIGH_OUTLIER_DENSITY
STRONG_CORRELATION
SKEWED_DISTRIBUTION
MIXED_TYPE_COLUMN
DUPLICATE_ROWS
POTENTIAL_LEAKAGE
SEMANTIC_TAG          ← e.g., "this column appears to be financial"
DATA_RECOMMENDATION   ← actionable suggestion
```

### Findings as the RAG substrate

Every Finding becomes an indexed vector chunk. The LLM never sees raw data — it
sees Findings. The conversational layer retrieves relevant Findings per question.

Persistence: `data/findings/{dataset_id}.json`. Endpoint:
`GET /datasets/{id}/findings`. Wired into `profiling_pipeline.py` as the
post-profile step.

---

## 10. Layer 4 — Visualization Metadata Layer ✅ MVP

### Design: metadata, not images

Backend never renders charts. It generates chart-ready JSON payloads; the frontend
renders them. Decouples backend from frontend framework choice.

### Chart payloads

**Per NUMERIC column:**
- Histogram: `{bins: [{start, end, count}], recommended_bin_count: int}`
- Box plot: `{min, q1, median, q3, max, outliers: list[float]}`
- KDE approximation: `{x_values: list[float], y_values: list[float]}`

**Per CATEGORICAL column:**
- Bar chart: `{values: [{label, count, pct}], has_other_bucket: bool}`
- Pie chart data: same structure, top 8 + other

**Per DATETIME column:**
- Time series: `{period: 'day'|'week'|'month', points: [{date, count}]}`

**Dataset-level:**
- Pearson correlation heatmap: `{columns: list[str], matrix: list[list[float]]}`
- Null matrix: `{columns: list[str], null_flags: list[list[int]]}` — sample 500 rows
- Cardinality bar chart: `{columns: list[str], cardinalities: list[int]}`
- Health score radar: `{dimensions: list[str], scores: list[float]}`

### Chart recommendation engine

```python
def recommend_chart(col: ColumnProfile) -> list[str]:
    if col.dtype == 'NUMERIC':
        return ['histogram', 'box_plot']
    if col.dtype == 'CATEGORICAL' and col.cardinality < 10:
        return ['pie_chart', 'bar_chart']
    if col.dtype == 'DATETIME':
        return ['time_series']
    ...
```

---

## 11. Layer 5 — LLM Insight Layer ✅ Complete (shipped 2026-05-29)

### Design principle (critical)

The LLM receives **Findings + Health Score + Semantic Types**. It never receives
raw data. It never calculates statistics. Its job is *translation* — turning
structured analytical intelligence into human language.

### Model routing (OpenAI only)

```
gpt-4o       → executive summary, insight explanations, recommendations
gpt-4o-mini  → suggested questions, short labels, tag generation
```

### LLM outputs

**Executive Summary** (`gpt-4o`, ~150 words)
- What the dataset is about (inferred from semantic types)
- Overall data-quality verdict with evidence
- Top 3 most important findings
- Whether data is ready for analysis/ML

**Per-Finding Explanations** (`gpt-4o`, HIGH severity only)
- What the finding means in plain English
- Why it matters (business/analytical impact)
- Specific recommended action

**Suggested Analytical Questions** (`gpt-4o-mini`, 5 questions)
- Generated from column names + semantic types + finding types
- Become clickable chips in the UI
- Examples: "Which customers have the highest revenue concentration?"
  "What percentage of records are missing address data?"

**Dataset Tags** (`gpt-4o-mini`, `list[str]`)
- Short categorical tags: `["financial", "customer-data", "time-series",
  "high-null-risk"]`
- Used for future dataset search and organization

### Prompt architecture

All prompts in `agentic_engine/prompts/`. Context passed to LLM is structured:

```
SYSTEM: Role definition + strict instruction (never invent statistics)
USER: {
  dataset_summary: {rows, cols, health_score, file_type},
  semantic_types:  {column: semantic_type},
  findings:        [list of Finding objects as JSON],
  task:            "generate executive summary"
}
```

### Response caching

Cache all LLM outputs to `data/llm_cache/<dataset_id>.json`. On re-request, serve
from cache. Invalidate only if dataset is re-profiled. Key by
`(dataset_id, finding_id, prompt_version)`.

### Endpoint & pipeline wiring

- `GET /datasets/{id}/insights` returns the full insight bundle.
- Pipeline status flow becomes: `VIZ → INSIGHTS → COMPLETE`.

### As-built resolutions

- **Batching:** all HIGH/MEDIUM findings go in **one** batched LLM call (not
  per-finding). Cheaper, faster, single-shot cache. LOW findings are skipped.
- **Streaming:** **not** implemented for the exec summary in Layer 5. The
  pipeline is async-batch (BackgroundTasks), so streaming would be wasted —
  the result is read from disk by the UI. Layer 7's chat agent will stream.
- **Three parallel calls** via `asyncio.gather(return_exceptions=True)`:
  exec summary (gpt-4o), batched explanations (gpt-4o-mini), suggested
  questions (gpt-4o-mini). One slow/failed call cannot block the others.
- **LLMClient Protocol seam** in `llm_engine/base_llm.py` — tests inject a
  `FakeLLM` recording calls and returning canned JSON. No network in tests.
- **Hard "no raw rows" invariant** lives in `llm_engine/prompt_builder.py`.
  Any future prompt must route through `build_dataset_context`.
- **Hallucinated `finding_id` guard:** if the model returns an `id` that
  wasn't in the input batch, the explanation is dropped (logged warn).
- **Graceful degradation:** if `OPENAI_API_KEY` is missing OR any single
  call fails, the orchestrator returns an `InsightReport(degraded=True)`
  with placeholders for failed sections. The pipeline still reaches
  `COMPLETE` — never `FAILED` on LLM trouble.
- **Token caps** in the context: ≤40 columns, ≤10 correlations, ≤25
  findings explained. Anything above hits the cap and is dropped.
- **Output schema:** JSON-mode via `response_format={"type": "json_object"}`,
  client-side validated with Pydantic (`InsightReport` + sub-models).
- **Persistence:** `data/llm_cache/{dataset_id}.json` (full report
  including `TokenUsage` and `degraded` flag).
- **Endpoint:** `GET /api/v1/datasets/{id}/insights?section=summary|explanations|questions`.

### File layout that landed

```
backend/agentic_engine/llm_engine/
├── __init__.py
├── base_llm.py            # LLMClient Protocol + ChatResult + LLMUnavailable
├── openai_client.py       # async wrapper, JSON mode, retries
├── models.py              # InsightReport + ExecutiveSummary + FindingExplanation
│                          # + SuggestedQuestion + TokenUsage + QuestionIntent
├── prompt_builder.py      # context compression, no raw rows
├── prompts/
│   ├── executive_summary.py
│   ├── finding_explanation.py
│   └── suggested_questions.py
├── insight_builder.py     # async orchestrator
└── persistence.py         # save/load InsightReport
```

Migration `0002_pipeline_status_values.py` backfills `FINDINGS`, `VIZ`,
`INSIGHTS` into `dataset_status_enum` (uses `ALTER TYPE ... ADD VALUE`
inside `autocommit_block()`).

### Open questions deferred to Layer 6/7

- Per-finding cache granularity (skipped — we batch instead). Revisit if
  cost telemetry shows whole-batch re-runs are expensive on re-profile.
- Streaming for *exec summary* viewing in UI: not a Layer 5 concern;
  Layer 7 will stream chat responses.

---

## 12. Layer 6 — Vector Retrieval Layer ✅ Complete (shipped 2026-05-29)

### Vector store: pgvector (not ChromaDB)

PostgreSQL is already in the stack; pgvector is `CREATE EXTENSION vector`
— zero new infra. Migration `0001_initial.py` already issues the
`CREATE EXTENSION IF NOT EXISTS vector`.

### Embedding model: `text-embedding-3-small`

| Model | MTEB | Cost | Dims | Verdict |
|---|---|---|---|---|
| `all-MiniLM-L6-v2` (local) | ~56 | $0 | 384 | Free but lower quality, local model mgmt |
| `text-embedding-3-small` | ~62 | $0.02/1M tok | 1536 | **Shipped** |
| `text-embedding-3-large` | ~64 | $0.13/1M tok | 3072 | Overkill for MVP |

Dimensionality is declared in
`app.db.models.finding_embedding.EMBEDDING_DIMENSION` (=1536) AND in
migration `0003`. Switching to `large` requires updating both
constants and a follow-up migration to widen the `vector(N)` column.

### As-built resolutions

- **Single chunk per Finding** (not per-column / per-summary). Findings
  are atomic units of analytical signal; splitting would spread meaning
  thin across multiple low-similarity hits. Earlier spec mentioned
  COLUMN_PROFILE / EXECUTIVE_SUMMARY / SUGGESTED_QUESTION chunks — we
  may add those in Post-MVP if retrieval recall proves insufficient.
- **Embedded text** = `title + description + Column: X + Semantic type: Y`.
  Evidence dict is **excluded** — raw numbers like `{"null_pct": 60.0}`
  do not carry semantic signal for NL queries and only inflate length.
- **Atomic re-indexing**: indexer wraps `delete + insert + commit` in a
  single transaction so a re-profile cannot leave stale vectors.
- **Pre-filter then sort**: `severity`, `finding_type`, `column_name`,
  `confidence` are denormalised onto the embeddings row and btree-indexed.
  The retriever narrows the candidate set on those columns *before* the
  cosine sort runs.
- **IVFFlat over HNSW**: `lists=100`, cosine ops. IVFFlat builds faster
  and is cheaper at MVP scale (<1k vectors per dataset, scoped queries).
- **Embedder Protocol seam** in `vector_store/embedder.py` mirrors
  Layer 5's `LLMClient` — tests inject a deterministic `FakeEmbedder`.
- **Dimensionality guard**: `build_index` raises `RuntimeError` if the
  embedder's `.dimension` does not match the SQL column. Loud fail rather
  than silent corruption when someone misconfigures the model.
- **Graceful degradation** (mirrors Layer 5): missing `OPENAI_API_KEY`,
  missing session, or embedder failure → `IndexReport(degraded=True)`.
  Pipeline still reaches `COMPLETE`. Retriever similarly returns an
  empty `RetrievalResult(degraded=True)`, so Layer 7 can fall back to
  non-RAG mode.
- **Hard cap**: ≤1000 findings indexed per dataset (`_MAX_FINDINGS_TO_INDEX`).
- **Cosine similarity normalisation**: pgvector's `<=>` returns distance
  in `[0, 2]`; we project to similarity in `[0, 1]` as `1 - distance`
  (clamped).

### Storage shape

```sql
finding_embeddings (
  id            uuid PK,
  dataset_id    uuid FK ON DELETE CASCADE,    -- scope every query
  finding_id    uuid,                         -- references on-disk Finding
  embedding     vector(1536),
  model_name    varchar(128),
  embedded_text text,                         -- traceability + UI citation
  severity      varchar(16),                  -- pre-filter, btree
  finding_type  varchar(64),                  -- pre-filter, btree
  column_name   varchar(256) NULL,            -- pre-filter, btree
  confidence    float,
  created_at    timestamptz,
  UNIQUE (dataset_id, finding_id)
)
```

Plus `ivfflat (embedding vector_cosine_ops) WITH (lists = 100)`.

### File layout that landed

```
backend/agentic_engine/vector_store/
├── __init__.py
├── models.py        # FindingChunk, IndexReport,
│                    # RetrievalQuery / RetrievalHit / RetrievalResult
├── chunking.py      # Finding → embeddable text (title+desc+col+semantic)
├── embedder.py      # Embedder Protocol + OpenAIEmbedder (batched)
├── store.py         # async SQLAlchemy CRUD (delete, insert, cosine_search)
├── indexer.py       # async orchestrator — idempotent delete-then-insert
└── retriever.py     # async orchestrator — embed query, search, format
```

Plus:
- `app/db/models/finding_embedding.py` — SQLAlchemy ORM + `EMBEDDING_DIMENSION`
- `app/db/migrations/versions/0003_finding_embeddings.py` — table + indexes

### Pipeline + endpoint wiring

- Pipeline status flow: `VIZ → INSIGHTS → INDEXING → COMPLETE`.
- Endpoint: `POST /api/v1/datasets/{id}/retrieve` with JSON body
  `RetrievalQuery(query, top_k, min_similarity, severity?, finding_type?, column?)`.
- Endpoint requires the dataset to be in `COMPLETE` status; otherwise 404.

### Embedding cost analysis (rebuilt for MVP scale)

| Scale | Findings/dataset | Avg tok/finding | Total tokens | Cost |
|---|---|---|---|---|
| 10 datasets | ~25 | ~50 | 12.5K | $0.0003 |
| 100 datasets | ~25 | ~50 | 125K | $0.0025 |
| 1,000 datasets | ~25 | ~50 | 1.25M | $0.025 |

Effectively free at MVP scale.

### Open questions deferred to Layer 7 / Post-MVP

- Add COLUMN_PROFILE / EXECUTIVE_SUMMARY chunks for richer recall? Defer
  until Layer 7 telemetry shows recall gaps.
- Hybrid retrieval (BM25 + dense)? Out of scope for MVP.
- Re-embedding policy on partial re-profile (currently full atomic
  replace; could be incremental). Fine until cost or latency makes it
  worth optimising.

---

## 13. Layer 7 — Conversational Analytics Layer ✅ MVP

### Two distinct modes

Earlier specs covered only RAG. The Notion deep-spec correctly identifies that
queries like *"Show rows where revenue > 10000"* are **not** RAG — they're direct
dataframe queries. Both modes required.

#### Mode A — Insight Retrieval (RAG)
For questions about analysis, patterns, findings, recommendations:
- "Why are outliers high in customer spending?"
- "What are the data quality issues?"
- "Which features might cause leakage?"

Flow: user query → retrieve chunks → `gpt-4o` with context → grounded response.

#### Mode B — Direct Data Query (NL → Pandas)
For questions about the raw data:
- "Show rows where revenue > 10000"
- "How many customers are in California?"
- "What is the average order value by region?"

Flow: user query → `gpt-4o` generates Pandas code → execute safely → return result
as a table.

**Safety for Mode B (mandatory):**

```python
# Whitelist-based execution — never eval() arbitrary code
ALLOWED_OPERATIONS = ['filter', 'groupby', 'agg', 'sort_values',
                      'head', 'describe', 'value_counts']
# Generated code is parsed to AST before execution
# Only allow read operations — no df.to_csv(), no file writes, no imports
```

### `ConversationSession`

```python
class ConversationSession:
    session_id:   UUID
    dataset_id:   str
    mode:         Literal['auto', 'rag', 'query']  # auto detects from question
    history:      list[ChatMessage]                # 30-message cap
    created_at:   datetime
    last_active:  datetime
```

### Query-mode detection

```python
QUERY_SIGNALS = ['show', 'filter', 'rows where', 'list', 'count', 'how many', 'select']
RAG_SIGNALS   = ['why', 'explain', 'what does', 'analyze', 'recommend', 'insight']
# If ambiguous → gpt-4o-mini classifies the intent
```

---

## 14. Async architecture — the right MVP pattern

### Problem
Profiling a 100MB CSV with 2M rows takes 15–40s. Cannot block the HTTP response.

### MVP pattern — BackgroundTasks + job polling

```
POST /api/v1/upload
  → validates file
  → saves to disk
  → registers DatasetRecord in DB (status: QUEUED)
  → background_tasks.add_task(run_profiling_pipeline, dataset_id)
  → returns {dataset_id, status: "queued"} in <200ms

GET /api/v1/datasets/{dataset_id}/status
  → reads DatasetRecord.status from DB
  → returns {status: "profiling" | "complete" | "failed", progress_pct: int}

# Client polls every 2s until status == "complete"
```

### Why this prepares for Celery / ARQ

API contract (return immediately, poll for status) is identical to Celery's.
Upgrade:
1. Replace `background_tasks.add_task(...)` with `celery_task.delay(...)` (or ARQ
   equivalent).
2. Add `REDIS_URL` to config.
3. Nothing else changes.

---

## 15. Persistence Layer ✅ MVP

### PostgreSQL

```
dataset_records      — dataset metadata, status, timestamps
chat_sessions        — session metadata
chat_messages        — conversation history
dataset_embeddings   — pgvector table (chunk content + embedding + metadata)
```

### Disk (`data/`)

```
data/raw/         — original uploaded files
data/profiles/    — ProfileReport JSON (full profiling output)
data/findings/    — FindingReport JSON
data/viz/         — VizReport JSON
data/llm_cache/   — LLM interpretation cache
data/pipeline/    — PipelineResult per run
```

**Why split:** PostgreSQL for queryable, relational, structured data. Disk for
large JSON blobs — cheaper, faster to write, easily moved to S3 later.

---

## 16. Folder & file conventions

```
backend/
├── app/                          # FastAPI app (HTTP layer, DB, config)
│   ├── api/routes/               # dataset_routes.py, etc.
│   ├── core/                     # config, dependencies, auth
│   ├── db/                       # SQLAlchemy session, models, migrations
│   ├── exceptions/               # custom_exceptions.py + handlers.py
│   ├── schemas/                  # Pydantic request/response/error
│   └── services/                 # dataset_service.py, etc.
├── agentic_engine/               # all "intelligence" — profiler, findings, llm, etc.
│   ├── profiler/                 # Layer 2 (✅)
│   ├── findings/                 # Layer 3 (✅)
│   │   └── extractors/           # one file per finding type
│   ├── viz/                      # Layer 4 (✅)
│   │   └── chart_builders/
│   ├── llm_engine/               # Layer 5 (✅)
│   ├── vector_store/             # Layer 6 (✅)
│   ├── conversational/           # Layer 7
│   ├── memory/                   # dataset memory (findings + embeddings recall)
│   ├── prompts/                  # ALL LLM prompts live here, nowhere else
│   ├── tools/uploader/storage.py # StorageBackend Protocol
│   ├── agents/                   # LangGraph stub (Post-MVP)
│   └── workflows/                # profiling_pipeline.py, etc.
├── data/                         # disk blobs — NOT checked into git
└── tests/
    ├── findings/
    └── viz/
```

**Conventions:**
- All LLM prompts go in `agentic_engine/prompts/`. Don't inline them in service code.
- Anything that consumes/produces `ProfileReport` lives under `agentic_engine/`.
  Anything that's pure HTTP/CRUD lives under `app/`.
- Data blobs are disk, not DB. The dataset row in PG points at file paths.

---

## 17. Dashboard Structure (5 Streamlit pages — 6th was cut)

**1. Upload Center**
- File uploader
- Pipeline progress (step-by-step status badges with real-time polling)
- Dataset list with health-score badges

**2. Dataset Explorer**
- Schema overview table (column, dtype, semantic_type, null_pct, cardinality)
- Health Score card with dimension-breakdown radar chart
- Findings grouped by severity
- Anomaly flags with evidence

**3. Visualization Center**
- Column selector → renders chart from `VizReport` JSON
- Correlation heatmap
- Null matrix
- Distribution comparisons

**4. AI Insight Center**
- Executive summary
- High-severity finding explanations with recommended actions
- Dataset tags
- Suggested questions (clickable → pre-fill chat)

**5. Conversational Analyst**
- Auto-detects mode (RAG vs Data Query)
- Chat history with source citations
- Data-query results rendered as tables
- Export conversation as PDF (Post-MVP)

---

## 18. Resolved open questions (don't re-litigate)

1. **File size limit:** 100MB cap for MVP (configurable in `config.py`). PDF
   suggested 500MB+; deferred to Post-MVP.
2. **Multi-dataset conversations:** one dataset per session for MVP; cross-dataset
   in v2.
3. **User accounts:** none. Single-user, API-key-protected.
4. **Re-profiling:** allowed on explicit user request; invalidates all downstream
   caches for that dataset.
5. **Streaming chat:** yes — use OpenAI streaming API. Significantly improves
   perceived latency for conversational turns.

---

## 19. Timeline — what's been built

### 2026-05-19 — Layer 1 + Layer 2
**Layer 1 (Dataset Upload System, ~02:09–02:12):**
- `backend/app/exceptions/custom_exceptions.py` + `handlers.py`
- `backend/app/schemas/{request,response,error}_schemas.py`
- `backend/app/core/dependencies.py` — API-key auth + DI
- `backend/app/db/__init__.py` — SQLAlchemy session/engine
- `backend/app/services/dataset_service.py` — upload validation + lifecycle
- `backend/app/api/routes/{__init__,dataset_routes}.py`
- `backend/main.py`
- `README.md`

**Layer 2 (Profiling Engine, ~03:54–04:01):**
- `backend/agentic_engine/profiler/` — `report.py`, `loader.py`,
  `schema_profiler.py`, `quality_profiler.py`, `relationship_profiler.py`,
  `semantic_profiler.py`, `health_scorer.py`, `stats_profiler.py`, `engine.py`
- `backend/agentic_engine/workflows/profiling_pipeline.py`
- `dataset_routes.py` wired to enqueue profiling on upload

### 2026-05-20 — Layer 3
**Semantic Findings Layer:**
- `Finding` Pydantic model
- `agentic_engine/findings/` — `finding_types.py`, `confidence.py`,
  `findings_builder.py`, plus per-type extractors under `extractors/`
- Persistence to `data/findings/{dataset_id}.json`
- `GET /datasets/{id}/findings` endpoint
- Wired into `profiling_pipeline.py` as the post-profile step
- Unit tests for ≥2–3 extractors

### Layer 4 (commit `f7e8be4`)
**Visualization Metadata Layer:** `VizReport` JSON, chart builders under
`agentic_engine/viz/chart_builders/`, tests under `tests/viz/`.

### 2026-05-29 — Layer 5 (commit `788ceca`)
**LLM Insight Layer:**
- `agentic_engine/llm_engine/` module (see §11 for layout)
- 3 parallel LLM calls via `asyncio.gather`
- `LLMClient` Protocol seam + `FakeLLM` test stub
- Graceful degradation when no API key or any call fails
- `GET /datasets/{id}/insights` endpoint
- Pipeline: `VIZ → INSIGHTS → COMPLETE`
- Migration `0002` backfills FINDINGS/VIZ/INSIGHTS to enum
- pytest-asyncio added; `pytest.ini` sets `asyncio_mode=auto`
- 16 new tests (74/74 total passing)

### 2026-05-29 — Layer 6
**Vector Retrieval Layer (pgvector):**
- `agentic_engine/vector_store/` module (see §12 for layout)
- `app/db/models/finding_embedding.py` — SQLAlchemy ORM + EMBEDDING_DIMENSION
- Migration `0003` adds INDEXING enum value + `finding_embeddings` table
  with IVFFlat cosine index + btree indexes on filter columns
- `Embedder` Protocol seam + `FakeEmbedder` test stub
- Atomic delete-then-insert reindexing in a single transaction
- Graceful degradation when no API key, no session, or embed failure
- `POST /datasets/{id}/retrieve` endpoint with severity / finding_type /
  column filters and `min_similarity` post-filter
- Pipeline: `VIZ → INSIGHTS → INDEXING → COMPLETE`
- pgvector==0.3.6 added to environment (already in requirements.txt)
- 34 new tests (108/108 total passing)

### Next: Layer 7 (Conversational Analytics) — see §13

---

## 20. Collaboration preferences (how the user works with Claude)

- **Working dir** is `C:\Users\hp\.gemini\antigravity\scratch\NeuroNova-MVP-NEW-`.
  (Note: the path inside `CLAUDE.md` is currently missing the `-NEW-` suffix — that
  field is stale; the `-NEW-` path is correct.)
- **Shell:** PowerShell on Windows 11. Use PowerShell syntax, not bash.
- **Tone:** terse, technical, no fluff. Skip end-of-turn summaries unless asked.
- **Scope discipline:** don't add features, error handling, or refactors beyond
  what was asked. Bug fix = bug fix.
- **Comments:** default to none. Only write a comment when the *why* is non-obvious.
- **Don't break the moat:** LLM never sees raw data. If a future change would route
  a dataframe slice into a prompt, push back.

---

## 21. Useful pointers

- `CLAUDE.md` — short-form project spec (source of truth, checked in).
- `MEMORY.md` — this file (deep spec + onboarding).
- `README.md` — user-facing repo readme.
- `progress` — one-line free-form progress log (currently: "layer 1 and layer 2
  completed along with smoke test"; stale, don't rely on it).
- `docker-compose.yml` — Postgres + pgvector for local dev.
- `sample_clean.csv` / `sample_messy.csv` — fixture datasets for manual smoke tests.
- `.env.example` — env var template (OpenAI key, DB URL, API key, storage path).

---

## 22. For a fresh Claude on a new account — quick start

1. Read `CLAUDE.md` (short-form spec).
2. Read this file (`MEMORY.md`) for depth.
3. Skim `backend/agentic_engine/profiler/engine.py` for the data flow into findings.
4. Skim `backend/agentic_engine/findings/findings_builder.py` for the extractor
   orchestration pattern — Layer 5 will follow the same shape.
5. `git log --oneline -20` for current state. Layer 6 is the most recent
   layer; the last commit may be a docs-update on top of it.
6. Ask the user what they want to work on — don't assume Layer 7 just because this
   doc says so. Plans drift.

You do **not** need to mirror this file into auto-memory. Auto-memory is for things
this file *doesn't* already say — surprising user preferences, mid-flight decisions,
etc.
