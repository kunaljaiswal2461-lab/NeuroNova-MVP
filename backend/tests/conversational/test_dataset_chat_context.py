"""Regression tests for persisted-profile-backed dataset chat context."""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.db.models.dataset import DatasetStatus, FileType
from agentic_engine.conversational import chat_agent
from agentic_engine.conversational.chat_agent import (
    _SCHEMA_CONTEXT_COLUMN_LIMIT,
    _load_dataset_context,
    _schema_snapshot,
    stream_chat_turn,
)
from agentic_engine.conversational.models import (
    ChatMessage,
    ChatRole,
    ChatStreamEventType,
    ConversationMode,
    ConversationSession,
)
from agentic_engine.findings.finding_types import FindingType, Severity
from agentic_engine.profiler.engine import load_profile_report, profile_path, save_report
from agentic_engine.profiler.report import (
    CategoricalStats,
    ColumnQuality,
    ColumnStats,
    HealthSection,
    ProfileReport,
    QualitySection,
    RelationshipSection,
    SchemaColumn,
    SchemaSection,
    SemanticSection,
    SemanticTag,
    StatsSection,
)
from tests.conversational.conftest import (
    FakeRetrieverConfig,
    FakeStreamingLLM,
    install_fake_retriever,
    make_hit,
)


def _profile(dataset_id: uuid.UUID) -> ProfileReport:
    return ProfileReport(
        dataset_id=dataset_id,
        generated_at=datetime.now(timezone.utc),
        schema=SchemaSection(
            row_count=3,
            col_count=2,
            columns=[
                SchemaColumn(
                    name="uploaded_revenue",
                    dtype="float",
                    polars_dtype="Float64",
                    nullable=False,
                ),
                SchemaColumn(
                    name="uploaded_region",
                    dtype="string",
                    polars_dtype="String",
                    nullable=True,
                ),
            ],
        ),
        stats=StatsSection(
            columns=[
                ColumnStats(name="uploaded_revenue", inferred_kind="numeric"),
                ColumnStats(
                    name="uploaded_region",
                    inferred_kind="categorical",
                    categorical=CategoricalStats(count=3, cardinality=2, mode="North"),
                ),
            ]
        ),
        quality=QualitySection(
            duplicate_row_pct=0.0,
            columns=[
                ColumnQuality(name="uploaded_revenue", null_pct=0.0, is_constant=False),
                ColumnQuality(name="uploaded_region", null_pct=33.3, is_constant=False),
            ],
        ),
        relationships=RelationshipSection(correlations=[]),
        semantic=SemanticSection(
            columns=[
                SemanticTag(
                    name="uploaded_revenue", semantic_type="FINANCIAL", confidence=0.98
                ),
                SemanticTag(
                    name="uploaded_region", semantic_type="GEOGRAPHIC", confidence=0.94
                ),
            ]
        ),
        health=HealthSection(score=88.0, grade="B", components={"quality": 90.0}),
    )


def _record(dataset_id: uuid.UUID) -> SimpleNamespace:
    return SimpleNamespace(
        id=dataset_id,
        status=DatasetStatus.COMPLETE,
        filename="uploaded.csv",
        file_type=FileType.CSV,
    )


async def _install_chat_fakes(monkeypatch, *, dataset_id, record, mode):
    session = ConversationSession(
        session_id=uuid.uuid4(), dataset_id=dataset_id, mode=mode
    )
    saved: list[ChatMessage] = []
    commits: list[bool] = []

    async def get_session(_db, _session_id):
        return session

    async def get_dataset(_db, _dataset_id):
        return record

    async def append_message(*, session_db, session_id, role, content, commit, **kwargs):
        message = ChatMessage(
            message_id=uuid.uuid4(),
            session_id=session_id,
            role=role,
            content=content,
            created_at=datetime.now(timezone.utc),
            **kwargs,
        )
        saved.append(message)
        commits.append(commit)
        return message

    async def recent_messages(*_args, **_kwargs):
        return saved

    monkeypatch.setattr(chat_agent.manager, "get_session", get_session)
    monkeypatch.setattr(chat_agent.manager, "append_message", append_message)
    monkeypatch.setattr(chat_agent.manager, "get_recent_messages", recent_messages)
    monkeypatch.setattr(chat_agent.dataset_service, "get_dataset", get_dataset)
    return session, saved, commits


@pytest.mark.asyncio
async def test_mismatched_authorized_context_is_rejected_before_clients_or_persistence(
    monkeypatch, settings_with_key
):
    session = ConversationSession(
        session_id=uuid.uuid4(),
        dataset_id=uuid.uuid4(),
        mode=ConversationMode.AUTO,
    )
    mismatched_dataset = SimpleNamespace(
        id=uuid.uuid4(), status=DatasetStatus.COMPLETE
    )

    def should_not_build_client(*_args, **_kwargs):
        pytest.fail("a mismatched authorized context must not construct an LLM client")

    def should_not_build_embedder(*_args, **_kwargs):
        pytest.fail("a mismatched authorized context must not construct an embedder")

    async def should_not_persist(*_args, **_kwargs):
        pytest.fail("a mismatched authorized context must not persist messages")

    monkeypatch.setattr(chat_agent, "build_client", should_not_build_client)
    monkeypatch.setattr(chat_agent, "build_embedder", should_not_build_embedder)
    monkeypatch.setattr(chat_agent.manager, "append_message", should_not_persist)

    with pytest.raises(ValueError, match="authorized conversation context"):
        async for _event in stream_chat_turn(
            session_id=session.session_id,
            user_message="must not run",
            session_db=object(),
            settings=settings_with_key,
            authorized_session=session,
            authorized_dataset=mismatched_dataset,
        ):
            pass


def test_load_profile_report_returns_typed_model_and_handles_missing_invalid_files(settings_with_key):
    dataset_id = uuid.uuid4()
    assert load_profile_report(dataset_id, settings_with_key) is None

    report = _profile(dataset_id)
    save_report(report, settings_with_key)
    loaded = load_profile_report(dataset_id, settings_with_key)
    assert isinstance(loaded, ProfileReport)
    assert loaded.dataset_id == dataset_id
    assert loaded.schema_.columns[0].polars_dtype == "Float64"

    profile_path(dataset_id, settings_with_key).write_text('{"dataset_id": "bad"}', encoding="utf-8")
    with pytest.raises(ValidationError):
        load_profile_report(dataset_id, settings_with_key)


def test_schema_context_is_bounded_and_signals_truncation():
    profile = _profile(uuid.uuid4())
    extra_columns = [
        SchemaColumn(
            name=f"extra_{index}",
            dtype="integer",
            polars_dtype="Int64",
            nullable=True,
        )
        for index in range(_SCHEMA_CONTEXT_COLUMN_LIMIT)
    ]
    profile.schema_.columns.extend(extra_columns)

    snapshot = _schema_snapshot(profile)

    assert len(snapshot) == _SCHEMA_CONTEXT_COLUMN_LIMIT + 1
    assert snapshot[-1] == {
        "schema_truncated": True,
        "included_column_count": _SCHEMA_CONTEXT_COLUMN_LIMIT,
        "total_column_count": _SCHEMA_CONTEXT_COLUMN_LIMIT + 2,
    }


def test_context_uses_canonical_profile_model_and_safe_aggregate_snapshot(settings_with_key):
    dataset_id = uuid.uuid4()
    record = _record(dataset_id)
    save_report(_profile(dataset_id), settings_with_key)

    columns, overview, loaded_profile = _load_dataset_context(record, settings_with_key)

    assert loaded_profile.dataset_id == dataset_id

    assert columns == [
        {
            "name": "uploaded_revenue",
            "dtype": "Float64",
            "logical_dtype": "float",
            "polars_dtype": "Float64",
            "nullable": False,
            "semantic_type": "FINANCIAL",
            "inferred_kind": "numeric",
            "null_pct": 0.0,
            "is_constant": False,
        },
        {
            "name": "uploaded_region",
            "dtype": "String",
            "logical_dtype": "string",
            "polars_dtype": "String",
            "nullable": True,
            "semantic_type": "GEOGRAPHIC",
            "inferred_kind": "categorical",
            "cardinality": 2,
            "null_pct": 33.3,
            "is_constant": False,
        },
    ]
    assert overview == {
        "row_count": 3,
        "column_count": 2,
        "health": {"score": 88.0, "grade": "B", "components": {"quality": 90.0}},
        "duplicate_row_pct": 0.0,
        "columns_with_nulls": 1,
        "constant_columns": 0,
        "semantic_type_counts": {"FINANCIAL": 1, "GEOGRAPHIC": 1},
        "inferred_kind_counts": {"numeric": 1, "categorical": 1},
    }


@pytest.mark.asyncio
async def test_query_chat_uses_uploaded_profile_columns_for_codegen(
    monkeypatch, settings_with_key
):
    dataset_id = uuid.uuid4()
    record = _record(dataset_id)
    save_report(_profile(dataset_id), settings_with_key)
    settings_with_key.raw_dir.mkdir(parents=True, exist_ok=True)
    (settings_with_key.raw_dir / record.filename).write_text(
        "uploaded_revenue,uploaded_region\n10,North\n20,South\n30,North\n",
        encoding="utf-8",
    )
    session, _, _ = await _install_chat_fakes(
        monkeypatch,
        dataset_id=dataset_id,
        record=record,
        mode=ConversationMode.QUERY,
    )
    llm = FakeStreamingLLM(stream_chunks=[[json.dumps({
        "metrics": [{
            "type": "sum",
            "column": "uploaded_revenue",
            "name": "total",
        }],
    })]])

    events = [
        event
        async for event in stream_chat_turn(
            session_id=session.session_id,
            user_message="What is the total uploaded revenue?",
            session_db=object(),
            settings=settings_with_key,
            llm=llm,
            embedder=None,
        )
    ]

    result = next(event.data["query_result"] for event in events if event.event is ChatStreamEventType.QUERY_RESULT)
    assert result["error"] is None
    assert result["rows"] == [[60]]
    assert '"name": "uploaded_revenue"' in llm.stream_calls[0].user
    assert '"dtype": "Float64"' in llm.stream_calls[0].user
    assert '"semantic_type": "FINANCIAL"' in llm.stream_calls[0].user
    assert "Never return Polars" in llm.stream_calls[0].system
    assert events[-1].event is ChatStreamEventType.DONE
    assert events[-1].data["degraded"] is False


@pytest.mark.asyncio
async def test_query_mode_response_includes_generated_code(monkeypatch, settings_with_key):
    dataset_id = uuid.uuid4()
    record = _record(dataset_id)
    save_report(_profile(dataset_id), settings_with_key)
    settings_with_key.raw_dir.mkdir(parents=True, exist_ok=True)
    (settings_with_key.raw_dir / record.filename).write_text(
        "uploaded_revenue,uploaded_region\n10,North\n20,South\n",
        encoding="utf-8",
    )
    session, saved, _ = await _install_chat_fakes(
        monkeypatch,
        dataset_id=dataset_id,
        record=record,
        mode=ConversationMode.QUERY,
    )
    llm = FakeStreamingLLM(stream_chunks=[[json.dumps({
        "metrics": [{
            "type": "sum",
            "column": "uploaded_revenue",
            "name": "total",
        }],
    })]])

    events = [
        event
        async for event in stream_chat_turn(
            session_id=session.session_id,
            user_message="What is the total uploaded revenue?",
            session_db=object(),
            settings=settings_with_key,
            llm=llm,
            embedder=None,
        )
    ]

    query_event = next(
        event for event in events if event.event is ChatStreamEventType.QUERY_RESULT
    )
    assert isinstance(query_event.data["generated_code"], str)
    assert query_event.data["generated_code"]
    assert "df" in query_event.data["generated_code"]
    assert saved[-1].generated_code == query_event.data["generated_code"]


@pytest.mark.asyncio
async def test_query_mode_unsupported_response_has_no_code(monkeypatch, settings_with_key):
    dataset_id = uuid.uuid4()
    record = _record(dataset_id)
    save_report(_profile(dataset_id), settings_with_key)
    session, saved, _ = await _install_chat_fakes(
        monkeypatch,
        dataset_id=dataset_id,
        record=record,
        mode=ConversationMode.QUERY,
    )
    llm = FakeStreamingLLM(stream_chunks=[["UNSUPPORTED"]])

    events = [
        event
        async for event in stream_chat_turn(
            session_id=session.session_id,
            user_message="What is the meaning of life?",
            session_db=object(),
            settings=settings_with_key,
            llm=llm,
            embedder=None,
        )
    ]

    assert not any(event.event is ChatStreamEventType.QUERY_RESULT for event in events)
    assert "filters, group by, count" in events[-2].data["text"]
    assert events[-1].data["unsupported"] is True
    assert events[-1].data["generated_code"] is None
    assert saved[-1].unsupported is True


@pytest.mark.asyncio
async def test_rag_chat_receives_profile_overview(monkeypatch, settings_with_key):
    dataset_id = uuid.uuid4()
    record = _record(dataset_id)
    save_report(_profile(dataset_id), settings_with_key)
    session, _, _ = await _install_chat_fakes(
        monkeypatch,
        dataset_id=dataset_id,
        record=record,
        mode=ConversationMode.RAG,
    )
    install_fake_retriever(
        monkeypatch, FakeRetrieverConfig(hits=[make_hit(column="uploaded_revenue")])
    )
    llm = FakeStreamingLLM(stream_chunks=[["Revenue looks healthy."]])

    events = [
        event
        async for event in stream_chat_turn(
            session_id=session.session_id,
            user_message="Explain the dataset health.",
            session_db=object(),
            settings=settings_with_key,
            llm=llm,
            embedder=None,
        )
    ]

    prompt = llm.stream_calls[0].user
    assert '"row_count": 3' in prompt
    assert '"grade": "B"' in prompt
    assert "Revenue looks healthy." in [
        event.data["text"] for event in events if event.event is ChatStreamEventType.TOKEN
    ]
    assert any(event.event is ChatStreamEventType.CITATIONS for event in events)
    assert events[-1].event is ChatStreamEventType.DONE
    assert events[-1].data["degraded"] is False


@pytest.mark.asyncio
async def test_rag_mode_response_has_no_generated_code(monkeypatch, settings_with_key):
    dataset_id = uuid.uuid4()
    record = _record(dataset_id)
    save_report(_profile(dataset_id), settings_with_key)
    session, saved, _ = await _install_chat_fakes(
        monkeypatch,
        dataset_id=dataset_id,
        record=record,
        mode=ConversationMode.RAG,
    )
    install_fake_retriever(monkeypatch, FakeRetrieverConfig(hits=[]))
    llm = FakeStreamingLLM(stream_chunks=[["The dataset is healthy."]])

    events = [
        event
        async for event in stream_chat_turn(
            session_id=session.session_id,
            user_message="Explain the dataset health.",
            session_db=object(),
            settings=settings_with_key,
            llm=llm,
            embedder=None,
        )
    ]

    assert saved[-1].generated_code is None
    assert events[-1].data["generated_code"] is None


@pytest.mark.asyncio
async def test_keyless_rag_turn_is_marked_degraded(monkeypatch, settings_no_key):
    dataset_id = uuid.uuid4()
    record = _record(dataset_id)
    save_report(_profile(dataset_id), settings_no_key)
    session, saved, _ = await _install_chat_fakes(
        monkeypatch,
        dataset_id=dataset_id,
        record=record,
        mode=ConversationMode.RAG,
    )
    install_fake_retriever(monkeypatch, FakeRetrieverConfig())

    events = [
        event
        async for event in stream_chat_turn(
            session_id=session.session_id,
            user_message="Explain the quality issues.",
            session_db=object(),
            settings=settings_no_key,
            llm=None,
            embedder=None,
        )
    ]

    assert events[-1].event is ChatStreamEventType.DONE
    assert events[-1].data["degraded"] is True
    assert events[-1].data["degraded_reason"] == "LLM client is not configured"
    assert saved[-1].degraded is True


@pytest.mark.asyncio
async def test_complete_dataset_without_profile_returns_clear_degraded_response(
    monkeypatch, settings_with_key
):
    dataset_id = uuid.uuid4()
    record = _record(dataset_id)
    session, saved, commits = await _install_chat_fakes(
        monkeypatch,
        dataset_id=dataset_id,
        record=record,
        mode=ConversationMode.QUERY,
    )

    events = [
        event
        async for event in stream_chat_turn(
            session_id=session.session_id,
            user_message="Show the total.",
            session_db=object(),
            settings=settings_with_key,
            llm=FakeStreamingLLM(),
            embedder=None,
        )
    ]

    assert [event.event for event in events] == [
        ChatStreamEventType.START,
        ChatStreamEventType.TOKEN,
        ChatStreamEventType.DONE,
    ]
    assert "profile is unavailable" in events[1].data["text"]
    assert events[-1].data["degraded"] is True
    assert [message.role for message in saved] == [ChatRole.USER, ChatRole.ASSISTANT]
    assert commits == [False, True]
    assert saved[-1].content == events[1].data["text"]
    assert saved[-1].degraded is True
    assert saved[-1].degraded_reason == "dataset profile unavailable"


@pytest.mark.asyncio
async def test_complete_dataset_with_invalid_profile_returns_persisted_degraded_response(
    monkeypatch, settings_with_key
):
    dataset_id = uuid.uuid4()
    record = _record(dataset_id)
    target = profile_path(dataset_id, settings_with_key)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text('{"dataset_id": "not-a-uuid"}', encoding="utf-8")
    session, saved, commits = await _install_chat_fakes(
        monkeypatch,
        dataset_id=dataset_id,
        record=record,
        mode=ConversationMode.RAG,
    )

    events = [
        event
        async for event in stream_chat_turn(
            session_id=session.session_id,
            user_message="Explain the health.",
            session_db=object(),
            settings=settings_with_key,
            llm=FakeStreamingLLM(),
            embedder=None,
        )
    ]

    assert [event.event for event in events] == [
        ChatStreamEventType.START,
        ChatStreamEventType.TOKEN,
        ChatStreamEventType.DONE,
    ]
    assert "profile is invalid" in events[1].data["text"]
    assert events[-1].data["degraded"] is True
    assert saved[-1].content == events[1].data["text"]
    assert saved[-1].degraded is True
    assert saved[-1].degraded_reason == "dataset profile unavailable"
    assert commits == [False, True]
