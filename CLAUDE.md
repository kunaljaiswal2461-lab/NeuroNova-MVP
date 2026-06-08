# NeuroNova MVP — Project Memory

NeuroNova is an AI-powered data analysis platform that combines automated EDA, semantic column intelligence, and conversational analytics in one product.

**Working directory:** `C:\Users\kunal\NeuroNova-MVP-NEW-`
**Stage:** MVP (10-day build window)
**GitHub:** https://github.com/VAIBHAVtheCODER-2028/NeuroNova-MVP-NEW-

## What It Is

An intelligent dataset profiler and conversational analyst. Users upload a dataset → system profiles it with statistical + semantic intelligence → generates findings → LLM translates findings to human language → user can chat with the data in two modes (RAG insight retrieval or NL→Pandas direct query).

## The Moat (Key Differentiators)

1. **Semantic column intelligence** — not just `int64`, but `FINANCIAL`, `GEOGRAPHIC`, etc.
2. **Confidence-scored Findings** — each finding has a 0.0–1.0 confidence score
3. **Retrieval-grounded answers** — LLM only sees Findings, never raw data (no hallucinated stats)
4. **Dual-mode chat** — RAG (insights) + NL→Pandas (direct queries) auto-detected in one interface
5. **Dataset memory** — findings + embeddings persist; revisit datasets weeks later

Differentiates from generic AutoEDA tools (ydata-profiling, etc.) and generic "chat with CSV" products.

## 7 Layers

| Layer | Name | Status |
|-------|------|--------|
| 1 | Dataset Upload System | ✅ Complete |
| 2 | Profiling Engine (schema, stats, semantic, quality, relationship, health) | ✅ Complete |
| 3 | Semantic Findings Layer | ✅ Complete |
| 4 | Visualization Metadata Layer | ✅ Complete |
| 5 | LLM Insight Layer | ✅ Complete |
| 6 | Vector Retrieval Layer (pgvector) | ✅ Complete |
| 7 | Conversational Analytics Layer | ✅ Complete |

## Pipeline Flow (end-to-end, single BackgroundTask)

```
POST /upload
  → PROFILING 5%  → load Polars
  → PROFILING 30% → run all sub-profilers
  → PROFILING 65% → save ProfileReport → data/profiles/{id}.json
  → FINDINGS  68% → run 9 extractors
  → FINDINGS  82% → save FindingsReport → data/findings/{id}.json
  → VIZ       85% → FindingsIndex built, 6 chart builders run
  → VIZ       95% → save VizReport → data/viz/{id}.json
  → INSIGHTS  96% → 3 parallel LLM calls (summary, explanations, questions)
  → INSIGHTS  97% → save InsightReport → data/llm_cache/{id}.json
  → INDEXING  98% → chunk findings, embed (text-embedding-3-small), persist
  → INDEXING  99% → atomic replace into finding_embeddings (pgvector)
  → COMPLETE 100% → row_count + col_count written to DB
```

Once `COMPLETE`, the dataset is chat-ready. Layer 7 runs per-turn (not in the upload pipeline) and is invoked through the chat endpoints.

`DatasetStatus`: UPLOADED → QUEUED → PROFILING → FINDINGS → VIZ → INSIGHTS → INDEXING → COMPLETE | FAILED

## Layer Summaries

- **Layer 1 — Dataset Upload:** Validates, stores, registers datasets in PostgreSQL. POST /upload, GET /datasets, GET /datasets/{id}/status. LocalStorage backend with `StorageBackend` Protocol for future S3 swap.
- **Layer 2 — Profiling Engine:** 6 sub-profilers (schema, stats, quality, relationship, semantic, health) produce a single `ProfileReport`. Polars-first loader, Pandas/Excel fallback. GET /datasets/{id}/profile.
- **Layer 3 — Findings:** 9 extractors turn `ProfileReport` into `Finding` objects with confidence scores. 13 `FindingType`s, 3 severities. GET /datasets/{id}/findings.
- **Layer 4 — Viz Metadata:** 6 chart builders produce a `VizReport` of chart specs (no raw data). L3 `FindingsIndex` skips constant/ID columns and boosts priority for flagged columns. 7 `ChartType`s. GET /datasets/{id}/viz.
- **Layer 5 — LLM Insight:** 3 parallel LLM calls (gpt-4o for summary, gpt-4o-mini for explanations and questions). Protocol-based `LLMClient` seam for testability. Graceful degradation when no `OPENAI_API_KEY`. GET /datasets/{id}/insights.
- **Layer 6 — Vector Retrieval (pgvector):** One embedding row per Finding (`text-embedding-3-small`, 1536-d). `finding_embeddings` table with denormalised `severity` / `finding_type` / `column_name` for pre-filtering, IVFFlat cosine index (`lists=100`) for the similarity sort. `Embedder` Protocol seam mirrors `LLMClient`. Indexer is atomic (delete-then-insert in one transaction). Retriever supports filters + min_similarity. POST /datasets/{id}/retrieve.
- **Layer 7 — Conversational Analytics:** Dual-mode chat (RAG + NL→Polars) over a profiled dataset, streamed via SSE. Intent classifier is a cheapest-first cascade — session pin → keyword heuristic → gpt-4o-mini disambiguation → safe RAG default. RAG path uses Layer 6 retriever, grounds gpt-4o on findings, streams tokens with inline citations. QUERY path generates a single Polars expression with gpt-4o, runs it through an AST whitelist (default-deny), sandbox-evals against the on-disk DataFrame, and caps results at 200 rows / 5s. Conversation persistence in PostgreSQL (`conversation_sessions` + `chat_messages` tables) with a 30-msg rolling window. LLMClient Protocol now also defines `chat_stream` (yields typed `StreamDelta`s). Chat agent never raises — every failure mode persists as a degraded assistant turn and ends with a clean DONE event.

## Test Status

- **171/171 unit tests passing** (24 findings + 34 viz + 16 llm + 34 vector_store + 63 conversational)
- pytest config in `backend/pytest.ini` (asyncio_mode=auto)
- End-to-end pipeline smoke test: `backend/e2e_test.py` (runs Layers 2–6 without DB; Layer 7 is per-turn, not in the pipeline)
- Run all tests: `cd backend && python -m pytest`

## Next Up — Streamlit dashboard wiring

All seven backend layers are shipped. Outstanding work for the MVP cut:

1. Build out the 5-page Streamlit dashboard (Upload Center, Dataset Explorer, Viz Center, AI Insight Center, Conversational Analyst).
2. Wire the Conversational Analyst page to the SSE chat endpoint (`POST /chat/sessions/{sid}/message`) — incremental token rendering, citation chips, query-result tables.
3. Decision shipped: intent routing is **auto-detect by default** with an explicit mode toggle. Session is created with `mode=AUTO|RAG|QUERY`; AUTO routes per-message via classifier; the other two pin every turn.

## Technology Stack (MVP)

- **Backend:** FastAPI + Python 3.13
- **Database:** PostgreSQL + pgvector extension (single infra, no ChromaDB)
- **Embeddings:** OpenAI `text-embedding-3-small` ($0.02/1M tokens)
- **LLM:** OpenAI `gpt-4o` (executive summary) + `gpt-4o-mini` (explanations, questions)
- **Task Queue:** FastAPI `BackgroundTasks` (not Celery — Post-MVP)
- **Data Processing:** Polars (primary) + Pandas (Excel fallback)
- **Stats:** SciPy
- **UI:** Streamlit (Next.js Post-MVP)
- **Logging:** structlog
- **Storage:** Local filesystem with `StorageBackend` Protocol abstraction (S3 Post-MVP)
- **Auth:** Static API key via `X-API-Key` header (JWT Post-MVP)
- **Testing:** pytest + pytest-asyncio (asyncio_mode=auto)

## What Was Explicitly Cut from MVP

- AWS S3 + presigned URLs → use LocalStorage with abstraction interface
- Redis + Celery → use FastAPI BackgroundTasks
- Next.js frontend → use Streamlit
- JWT auth → use static API key
- OpenTelemetry + Grafana → use structlog only
- LangGraph agents → stub directory only
- Multicollinearity/causal inference → Pearson + Spearman is enough
- Monitoring dashboard (6th UI page) → cut entirely
- Checksum validation + encrypted S3 → MIME type + file size cap is enough

## Post-MVP Stack (v1.0)

- Frontend: Next.js + TypeScript + TailwindCSS
- Task queue: ARQ (async-native) or Celery
- Storage: AWS S3
- Auth: JWT (Supabase Auth or custom)
- Observability: OpenTelemetry → Grafana + Prometheus
- Agents: LangGraph

## Key Architecture Decisions

- **pgvector over ChromaDB** — PostgreSQL already in stack; `CREATE EXTENSION vector` is zero new infra
- **BackgroundTasks API contract** identical to Celery — upgrade is pure backend swap, no API changes
- **LLM never sees raw data** — hard invariant enforced in `llm_engine/prompt_builder.py`. Only schema, aggregated stats, and Findings flow into prompts.
- **StorageBackend Protocol** — swap LocalStorage → S3Storage in one config line
- **Disk for large JSON blobs** (profiles, findings, viz, llm_cache); PostgreSQL for relational/queryable
- **LLMClient and Embedder Protocols** — `FakeLLM` and `FakeEmbedder` stubs in tests mean Layers 5 and 6 are fully unit-testable without an API key
- **Graceful degradation in Layers 5/6** — missing API key OR any failed call → degraded report, pipeline still reaches COMPLETE
- **Atomic re-indexing** — Layer 6 deletes then inserts in a single transaction, so re-profiling never leaves stale vectors
- **Pre-filter then sort** — embeddings store denormalised `severity` / `finding_type` / `column_name` (btree-indexed) so the cosine search runs on a narrowed candidate set

## Open Questions (resolved during spec)

1. File size limit: 100MB cap (configurable in `config.py`)
2. Multi-dataset conversations: single dataset per session for MVP
3. User accounts: no, API-key protected
4. Re-profiling: allowed on explicit user request, invalidates all caches
5. Streaming chat: yes, use OpenAI streaming API

## Dashboard Structure (5 pages)

1. Upload Center
2. Dataset Explorer (schema, health score, findings, anomalies)
3. Visualization Center (charts from VizReport JSON)
4. AI Insight Center (executive summary, finding explanations, suggested questions)
5. Conversational Analyst (dual-mode RAG + data query)

## Key Data Models

- `DatasetRecord` — PostgreSQL, tracks status: UPLOADED → … → COMPLETE | FAILED
- `ProfileReport` — Layer 2 output, disk JSON at `data/profiles/{id}.json`
- `Finding` / `FindingsReport` — Layer 3, disk JSON at `data/findings/{id}.json`
- `VizChart` / `VizReport` — Layer 4, disk JSON at `data/viz/{id}.json`
- `InsightReport` (ExecutiveSummary + FindingExplanation[] + SuggestedQuestion[] + TokenUsage) — Layer 5, disk JSON at `data/llm_cache/{id}.json`
- `FindingEmbedding` — Layer 6, PostgreSQL row in `finding_embeddings(dataset_id, finding_id, embedding vector(1536), severity, finding_type, column_name, confidence, ...)`. Atomic delete-then-insert per dataset.
- `RetrievalQuery` / `RetrievalHit` / `RetrievalResult` — Layer 6 API contracts. `RetrievalHit` carries `similarity` (0–1) + a denormalised slice of the finding metadata.
- `ConversationSession` / `ChatMessage` — Layer 7, persisted to `conversation_sessions` + `chat_messages` (PostgreSQL). Session pins routing mode; messages carry per-turn intent / citations / query_result / token usage as JSONB.
- `IntentDecision` — Layer 7, `{mode: RAG|QUERY, confidence, rationale, routed_by: session_override|heuristic|llm}`.
- `RagCitation` — Layer 7, `{finding_id, similarity, title, severity, finding_type, column}` — inline citation chip data for the UI.
- `QueryResult` — Layer 7, `{expression, columns, rows, row_count, truncated, elapsed_ms, error}` — outcome of a NL→Polars turn. `error` is non-null on AST rejection / runtime failure / timeout.
- `ChatStreamEvent` — Layer 7 SSE wire format, `{event: start|intent|citations|query_result|token|done|error, data: dict}`.

## API Endpoints (so far)

```
POST   /api/v1/upload
GET    /api/v1/datasets
GET    /api/v1/datasets/{id}
GET    /api/v1/datasets/{id}/status
GET    /api/v1/datasets/{id}/profile
GET    /api/v1/datasets/{id}/findings?severity=HIGH&type=…&column=…
GET    /api/v1/datasets/{id}/viz?type=SCATTER&column=…
GET    /api/v1/datasets/{id}/insights?section=summary|explanations|questions
POST   /api/v1/datasets/{id}/retrieve              body: RetrievalQuery
POST   /api/v1/datasets/{id}/chat/sessions         body: { mode: AUTO|RAG|QUERY }
GET    /api/v1/datasets/{id}/chat/sessions
GET    /api/v1/chat/sessions/{sid}
DELETE /api/v1/chat/sessions/{sid}
GET    /api/v1/chat/sessions/{sid}/messages
POST   /api/v1/chat/sessions/{sid}/message         body: { message }     (SSE stream)
```

All routes require `X-API-Key` header.

## File/Folder Conventions

- `backend/app/` — FastAPI app, db models, core config, services, schemas
- `backend/agentic_engine/` — profiler, findings, viz, llm_engine, vector_store, conversational
- `backend/agentic_engine/llm_engine/prompts/` — Layer 5 prompt templates
- `backend/agentic_engine/conversational/prompts/` — Layer 7 prompts (intent disambiguation, RAG answer, query codegen)
- `backend/agentic_engine/tools/uploader/storage.py` — StorageBackend protocol
- `data/raw/`, `data/profiles/`, `data/findings/`, `data/viz/`, `data/llm_cache/`, `data/pipeline/`
- `backend/tests/{findings,viz,llm,vector_store,conversational}/` — pytest suites per layer

## DB Setup (one-time)

PostgreSQL is **not** yet set up locally — the API cannot run without it, but all layer logic works without it (see `backend/e2e_test.py`).

```
createdb neuronova
cp .env.example .env       # fill in DATABASE_URL, API_KEY, OPENAI_API_KEY
cd backend && alembic upgrade head
python main.py
```

**Migrations 0002, 0003, and 0004 are required.** 0002 backfills FINDINGS, VIZ, INSIGHTS into `dataset_status_enum`. 0003 adds INDEXING to the enum and creates `finding_embeddings` with the pgvector cosine index. 0004 creates `conversation_sessions` and `chat_messages` (with a composite `(session_id, created_at)` index for the rolling-window read). Without them the pipeline crashes when writing the first non-legacy status or when a chat session is opened.

**pgvector extension** must be installed in the PostgreSQL instance (`CREATE EXTENSION vector;` — already issued by migration 0001 via `IF NOT EXISTS`).

## Operational Notes

- **Commit style**: use PowerShell here-string (`@'…'@`) for git commits — bash heredoc breaks on PowerShell shells. See `feedback_commit_style.md` in auto-memory.
- **No raw data in LLM prompts** — enforced by `llm_engine/prompt_builder.py` (Layer 5) and by the Layer 7 RAG answerer (only Finding metadata flows into RAG prompts). The QUERY branch is the *only* path that touches the dataframe; it does so inside an AST-validated sandbox in `conversational/query_executor.py`.
- **Adding a new pipeline status**: update both `app/db/models/dataset.py` AND add a migration with `ALTER TYPE … ADD VALUE` inside `autocommit_block()`.
- **Layer 7 AST whitelist** lives at `conversational/query_executor.py`. New Polars surface area (a new method, a new dtype) must be added to `_ALLOWED_METHODS` / `_ALLOWED_PL_ATTRIBUTES` *and* covered by a test in `tests/conversational/test_query_executor.py`. Default-deny is the security boundary — never widen by removing the check.
