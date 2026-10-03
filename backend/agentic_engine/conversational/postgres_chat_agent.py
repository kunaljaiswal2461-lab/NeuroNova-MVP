"""Stage 4C source-bound PostgreSQL chat turn orchestration.

It reuses the conversation session/message model and the existing SSE wire
format, but deliberately never enters the uploaded-file RAG/Polars paths.
Planning is structured and bounded; all database execution goes through the
Stage 4B gateway.
"""
from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Callable
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.connectors.postgres_query_executor import PostgresExecutionCancellation
from app.core.config import Settings, get_settings
from app.db.session import get_sessionmaker
from app.services import metric_service, relationship_service
from app.services.postgres_query_executor import (
    PostgresQueryExecutionResult,
    PostgresRelationshipProvenance,
    execute_postgres_query,
)
from agentic_engine.conversational import conversation_manager as manager
from agentic_engine.conversational.models import (
    ChatMessage,
    ChatRole,
    ChatStreamEvent,
    ChatStreamEventType,
    ConversationMode,
    ConversationSession,
)
from agentic_engine.conversational.postgres_planner import (
    PostgresPlanningResult,
    plan_postgres_question,
    prior_postgres_asset_id,
    postgres_history_for_prompt,
)
from agentic_engine.llm_engine.base_llm import LLMClient
from agentic_engine.llm_engine.openai_client import build_client


def _event(event: ChatStreamEventType, data: dict[str, Any]) -> ChatStreamEvent:
    return ChatStreamEvent(event=event, data=data)


def _summary_for_result(result: PostgresQueryExecutionResult) -> str:
    if result.status == "cancelled":
        return "The database calculation was cancelled. No result rows were used."
    if result.status != "succeeded":
        return (
            "The database calculation did not complete, so I cannot provide an "
            "answer from database rows."
        )
    # A byte cap can reject the very first serialized row.  That is a
    # successful, deliberately bounded calculation, not evidence that no
    # database rows matched the plan.
    if not result.complete and result.returned_row_count == 0:
        return (
            "The read-only calculation completed, but no result rows could be safely "
            "displayed before a safety limit was reached. The result is incomplete."
        )
    if result.returned_row_count == 0:
        return "The read-only calculation completed successfully; no rows matched."
    completeness = ""
    if not result.complete:
        completeness = " The displayed result is bounded and may be truncated."
    return (
        f"The read-only calculation completed with {result.returned_row_count} returned row(s)."
        f"{completeness}"
    )


async def _persist_assistant(
    *,
    session_db: AsyncSession,
    session_id: uuid.UUID,
    content: str,
    postgres_result: PostgresQueryExecutionResult | None = None,
    unsupported: bool = False,
    degraded: bool = False,
    degraded_reason: str | None = None,
    token_usage: Any | None = None,
) -> ChatMessage:
    return await manager.append_message(
        session_db=session_db,
        session_id=session_id,
        role=ChatRole.ASSISTANT,
        content=content,
        mode_used=ConversationMode.QUERY,
        postgres_result=postgres_result,
        unsupported=unsupported,
        degraded=degraded,
        degraded_reason=degraded_reason,
        token_usage=token_usage,
        commit=True,
    )


def _done(message: ChatMessage, *, token_usage: Any | None = None) -> ChatStreamEvent:
    payload: dict[str, Any] = {
        "message_id": str(message.message_id),
        "session_id": str(message.session_id),
        "degraded": message.degraded,
        "degraded_reason": message.degraded_reason,
        "unsupported": message.unsupported,
    }
    if token_usage is not None:
        payload["token_usage"] = token_usage.model_dump(mode="json")
    return _event(ChatStreamEventType.DONE, payload)


async def _authorize_before_disclosure(
    *,
    owner_user_id: uuid.UUID,
    session_id: uuid.UUID,
    session_factory: Callable[[], Any] | None = None,
) -> bool:
    """Do not disclose a completed result after its source/session vanished."""

    factory = session_factory or get_sessionmaker()
    try:
        async with factory() as fresh_session:
            conversation = await manager.get_session_for_access(
                fresh_session,
                session_id,
                access=manager.ConversationAccess.for_user(owner_user_id),
            )
            return (
                conversation.session.source_id is not None
                and conversation.source is not None
                and conversation.source.id == conversation.session.source_id
            )
    except Exception:
        # A missing/deleted source is intentionally indistinguishable here;
        # any result rows must remain undisclosed.
        return False


async def _verify_metric_pin(
    *,
    metric_definition: Any,
    owner_user_id: uuid.UUID,
    source_id: uuid.UUID,
    session_factory: Callable[[], Any] | None = None,
) -> bool:
    """Fail closed if a confirmed metric changed around execution or disclosure.

    The metric resolver pins an immutable version plus the current catalog
    snapshot during planning.  This fresh-session check prevents a definition
    edit, retirement, scope change, or catalog change from turning an already
    prepared generic plan into an executable authority or disclosable result.
    """

    factory = session_factory or get_sessionmaker()
    try:
        async with factory() as fresh_session:
            return bool(
                await metric_service.verify_metric_pin_for_execution(
                    fresh_session,
                    owner_user_id=owner_user_id,
                    source_id=source_id,
                    metric_id=metric_definition.metric_id,
                    version_number=metric_definition.version_number,
                    snapshot_id=metric_definition.snapshot_id,
                    metric_version_id=getattr(metric_definition, "metric_version_id", None),
                )
            )
    except Exception:
        # A lifecycle/persistence/access race is never a reason to execute a
        # stale definition.  The terminal response below intentionally omits
        # backend detail and does not expose rows.
        return False


async def _verify_relationship_pin(
    *,
    relationship_definition: Any,
    owner_user_id: uuid.UUID,
    source_id: uuid.UUID,
    session_factory: Callable[[], Any] | None = None,
) -> bool:
    """Fail closed if an approved lookup changed around result delivery."""

    factory = session_factory or get_sessionmaker()
    try:
        async with factory() as fresh_session:
            return bool(
                await relationship_service.verify_relationship_pin_for_execution(
                    fresh_session,
                    owner_user_id=owner_user_id,
                    source_id=source_id,
                    relationship_id=relationship_definition.relationship_id,
                    version_number=relationship_definition.version_number,
                    snapshot_id=relationship_definition.snapshot_id,
                    relationship_version_id=getattr(
                        relationship_definition, "relationship_version_id", None
                    ),
                )
            )
    except Exception:
        # The resolver hides lifecycle, catalog, and persistence details. A
        # failed recheck is never a reason to execute or disclose rows.
        return False


async def _persist_cancelled_after_disconnect(
    *,
    owner_user_id: uuid.UUID,
    session_id: uuid.UUID,
    session_factory: Callable[[], Any] | None = None,
) -> None:
    """Best-effort terminal persistence using a fresh app DB session.

    The caller's request session may itself be cancelled.  This does not
    resurrect a deleted source: manager authorization is repeated first.
    """

    factory = session_factory or get_sessionmaker()
    try:
        async with factory() as fresh_session:
            await manager.get_session_for_access(
                fresh_session,
                session_id,
                access=manager.ConversationAccess.for_user(owner_user_id),
            )
            await _persist_assistant(
                session_db=fresh_session,
                session_id=session_id,
                content="The database calculation was cancelled. No result rows were used.",
                degraded=True,
                degraded_reason="execution_cancelled",
            )
    except Exception:
        # Disconnect cleanup must never leak a driver/detail string or keep a
        # cancelled ASGI task alive.  The user turn already records intent.
        return


async def stream_postgres_chat_turn(
    *,
    session_id: uuid.UUID,
    user_message: str,
    session_db: AsyncSession,
    owner_user_id: uuid.UUID,
    settings: Settings | None = None,
    llm: LLMClient | None = None,
    cancellation: PostgresExecutionCancellation | None = None,
    authorized_session: ConversationSession | None = None,
    authorized_source: Any | None = None,
    executor: Callable[..., Any] | None = None,
    session_factory: Callable[[], Any] | None = None,
    turn_id: uuid.UUID | None = None,
) -> AsyncIterator[ChatStreamEvent]:
    """Run one source-bound chat turn without ever accepting raw SQL.

    User messages are committed before planning because the remote gateway uses
    its own short-lived session factory.  Every normal terminal path writes
    exactly one assistant message; cancellation is separately best-effort so
    an HTTP disconnect cannot strand a mutable request transaction.
    """

    settings = settings or get_settings()
    app_session_factory = session_factory or get_sessionmaker()
    cancellation = cancellation or PostgresExecutionCancellation()
    turn_id = turn_id or uuid.uuid4()
    # A user turn is committed before planning.  Track the terminal assistant
    # separately so an SSE disconnect/``aclose`` can add one safe cancellation
    # record without duplicating a completed normal turn.
    terminal_persisted = False
    terminal_emitted = False
    cleanup_started = False
    if (authorized_session is None) != (authorized_source is None):
        raise ValueError("authorized source conversation context must be supplied together")
    if authorized_session is None:
        conversation = await manager.get_session_for_access(
            session_db,
            session_id,
            access=manager.ConversationAccess.for_user(owner_user_id),
        )
        session = conversation.session
        source = conversation.source
    else:
        session = authorized_session
        source = authorized_source
    if (
        session.session_id != session_id
        or session.source_id is None
        or source is None
        or getattr(source, "id", None) != session.source_id
        or getattr(source, "owner_user_id", None) != owner_user_id
    ):
        raise ValueError("authorized database conversation context does not match session")

    user_message_row = await manager.append_message(
        session_db=session_db,
        session_id=session_id,
        role=ChatRole.USER,
        content=user_message,
        commit=True,
    )
    async def _cancelled_events() -> tuple[ChatStreamEvent, ...]:
        """Persist a cancellation outcome without ever retaining result rows."""

        nonlocal terminal_emitted, terminal_persisted
        try:
            # The user message was committed above.  Discard any planner read
            # transaction before adding its one matching terminal assistant.
            await session_db.rollback()
            assistant = await _persist_assistant(
                session_db=session_db,
                session_id=session_id,
                content="The database calculation was cancelled. No result rows were used.",
                degraded=True,
                degraded_reason="execution_cancelled",
            )
            terminal_persisted = True
            # The cancellation record is safely committed before an SSE frame
            # is exposed, so direct generator close cannot add a second one.
            terminal_emitted = True
            return (
                _event(
                    ChatStreamEventType.TOKEN,
                    {"text": "The database calculation was cancelled. No result rows were used."},
                ),
                _done(assistant),
            )
        except Exception:
            # A deleted source/session cannot be recreated just to write a
            # cancellation record. Repeat it once through a fresh app session
            # so an interrupted request transaction does not strand an owned
            # user turn; deleted source/session access remains a no-op.
            await _persist_cancelled_after_disconnect(
                owner_user_id=owner_user_id,
                session_id=session_id,
                session_factory=app_session_factory,
            )
            # The fresh-session repair has completed (or safely no-op'd for a
            # deleted source/session). Only now can cleanup stop retrying it.
            terminal_emitted = True
            return (_event(ChatStreamEventType.DONE, {"session_id": str(session_id)}),)

    async def _persist_safe_delivery_failure() -> ChatMessage | None:
        """Record a failed result handoff without converting it into cancel.

        A Stage 4B query can finish before persisting the bounded transcript
        artefact fails.  That failure must withhold rows and remain visibly a
        persistence/delivery failure, rather than making the next generator
        finalizer write a misleading cancellation outcome.
        """

        nonlocal terminal_persisted
        try:
            await session_db.rollback()
            assistant = await _persist_assistant(
                session_db=session_db,
                session_id=session_id,
                content=(
                    "The database result could not be saved safely, so no result rows "
                    "were delivered."
                ),
                degraded=True,
                degraded_reason="result_persistence_failed",
            )
            terminal_persisted = True
            return assistant
        except Exception:
            # Source/session deletion and a failed persistence backend cannot
            # be repaired by inventing a transcript or exposing a result.
            return None

    async def _cleanup_interrupted_turn() -> None:
        """Stop remote work and repair a user turn closed at an SSE yield."""

        nonlocal cleanup_started
        if terminal_persisted or terminal_emitted or cleanup_started:
            return
        cleanup_started = True
        try:
            await asyncio.shield(cancellation.cancel())
            await asyncio.shield(
                _persist_cancelled_after_disconnect(
                    owner_user_id=owner_user_id,
                    session_id=session_id,
                    session_factory=app_session_factory,
                )
            )
        except Exception:
            # Cancellation cleanup cannot disclose a driver or persistence
            # detail, and source deletion makes this a harmless no-op.
            return

    try:
        yield _event(
            ChatStreamEventType.START,
            {
                "session_id": str(session_id),
                "user_message_id": str(user_message_row.message_id),
                "turn_id": str(turn_id),
                "binding": "postgres_source",
            },
        )
        if cancellation.cancelled:
            for event in await _cancelled_events():
                yield event
            return

        yield _event(ChatStreamEventType.PLANNING, {"state": "planning"})
        if cancellation.cancelled:
            for event in await _cancelled_events():
                yield event
            return

        # The planner needs only safe transcript prose and catalog metadata.
        # It cannot receive retained typed rows from a prior database result.
        messages = await manager.get_recent_messages(session_db, session_id)
        history = postgres_history_for_prompt(messages[:-1])
        prior_asset_id = prior_postgres_asset_id(messages[:-1])
        if cancellation.cancelled:
            for event in await _cancelled_events():
                yield event
            return
        active_llm = llm if llm is not None else build_client(settings.openai_api_key)
        planning = await plan_postgres_question(
            session=session_db,
            owner_user_id=owner_user_id,
            source_id=session.source_id,
            question=user_message,
            history=history,
            settings=settings,
            llm=active_llm,
            prior_asset_id=prior_asset_id,
        )
        if cancellation.cancelled:
            for event in await _cancelled_events():
                yield event
            return

        if not planning.executable:
            event_type = {
                "clarification": ChatStreamEventType.CLARIFICATION,
                "unsupported": ChatStreamEventType.UNSUPPORTED,
                "catalog": ChatStreamEventType.CATALOG,
            }.get(planning.kind, ChatStreamEventType.ERROR)
            assistant = await _persist_assistant(
                session_db=session_db,
                session_id=session_id,
                content=planning.message,
                unsupported=planning.kind == "unsupported",
                degraded=planning.kind == "failure",
                degraded_reason="planning_failure" if planning.kind == "failure" else None,
                token_usage=planning.token_usage,
            )
            terminal_persisted = True
            # Persist before exposing the terminal outcome. A disconnect after
            # an SSE frame must not leave an accepted user turn unmatched.
            yield _event(
                event_type,
                {
                    "message": planning.message,
                    "outcome": planning.kind,
                    "snapshot_id": str(planning.snapshot_id) if planning.snapshot_id else None,
                },
            )
            yield _event(ChatStreamEventType.TOKEN, {"text": planning.message})
            yield _done(assistant, token_usage=planning.token_usage)
            return

        # The request session was used only for transcript/catalog reads. End
        # its read transaction before remote I/O; Stage 4B opens its own
        # bounded app sessions and revalidates all authorization itself.
        await session_db.rollback()
        if cancellation.cancelled:
            for event in await _cancelled_events():
                yield event
            return
        if planning.metric_definition is not None:
            if not await _verify_metric_pin(
                metric_definition=planning.metric_definition,
                owner_user_id=owner_user_id,
                source_id=session.source_id,
                session_factory=app_session_factory,
            ):
                message = (
                    "The confirmed metric changed or needs review before execution. "
                    "No calculation was run."
                )
                assistant = await _persist_assistant(
                    session_db=session_db,
                    session_id=session_id,
                    content=message,
                    degraded=True,
                    degraded_reason="metric_pin_invalid",
                    token_usage=planning.token_usage,
                )
                terminal_persisted = True
                yield _event(ChatStreamEventType.ERROR, {"message": message})
                yield _event(ChatStreamEventType.TOKEN, {"text": message})
                yield _done(assistant, token_usage=planning.token_usage)
                return
        if planning.relationship_definition is not None:
            if not await _verify_relationship_pin(
                relationship_definition=planning.relationship_definition,
                owner_user_id=owner_user_id,
                source_id=session.source_id,
                session_factory=app_session_factory,
            ):
                message = (
                    "The approved relationship changed or needs review before execution. "
                    "No calculation was run."
                )
                assistant = await _persist_assistant(
                    session_db=session_db,
                    session_id=session_id,
                    content=message,
                    degraded=True,
                    degraded_reason="relationship_pin_invalid",
                    token_usage=planning.token_usage,
                )
                terminal_persisted = True
                yield _event(ChatStreamEventType.ERROR, {"message": message})
                yield _event(ChatStreamEventType.TOKEN, {"text": message})
                yield _done(assistant, token_usage=planning.token_usage)
                return
        yield _event(
            ChatStreamEventType.EXECUTING,
            {
                "state": "executing",
                "source_id": str(session.source_id),
                "snapshot_id": str(planning.snapshot_id),
            },
        )
        if cancellation.cancelled:
            for event in await _cancelled_events():
                yield event
            return

        execute = executor or execute_postgres_query
        try:
            execution_kwargs: dict[str, Any] = {
                "owner_user_id": owner_user_id,
                "source_id": session.source_id,
                "snapshot_id": planning.snapshot_id,
                "plan": planning.plan,
                "settings": settings,
                "cancellation": cancellation,
            }
            if planning.metric_definition is not None and not cancellation.cancelled:
                # The executor accepts this optional immutable version-row
                # reference only together with a fresh trusted pin verifier.
                # It is not model output and is absent for ordinary Stage 4C
                # plans. The gateway invokes this closure immediately before
                # remote work, closing the edit/retire race after this
                # orchestration layer's initial precheck.
                metric_version_id = getattr(planning.metric_definition, "metric_version_id", None)
                if isinstance(metric_version_id, uuid.UUID):
                    execution_kwargs["metric_definition_version_id"] = metric_version_id

                    async def metric_pin_verifier() -> bool:
                        return await _verify_metric_pin(
                            metric_definition=planning.metric_definition,
                            owner_user_id=owner_user_id,
                            source_id=session.source_id,
                            session_factory=app_session_factory,
                        )

                    execution_kwargs["metric_pin_verifier"] = metric_pin_verifier
            if planning.relationship_definition is not None and not cancellation.cancelled:
                # The join plan contains an ID-only relationship pin. Bind it
                # to the independently resolved immutable version plus a
                # fresh verifier so the executor can reject a retirement,
                # review state, scope change, or cross-source provenance race.
                relationship_version_id = getattr(
                    planning.relationship_definition,
                    "relationship_version_id",
                    None,
                )
                if isinstance(relationship_version_id, uuid.UUID):
                    execution_kwargs[
                        "relationship_definition_version_id"
                    ] = relationship_version_id

                    async def relationship_pin_verifier() -> bool:
                        return await _verify_relationship_pin(
                            relationship_definition=planning.relationship_definition,
                            owner_user_id=owner_user_id,
                            source_id=session.source_id,
                            session_factory=app_session_factory,
                        )

                    execution_kwargs["relationship_pin_verifier"] = relationship_pin_verifier
            result = await execute(
                app_session_factory,
                **execution_kwargs,
            )
            if planning.metric_definition is not None and not cancellation.cancelled:
                # A metric edit, retirement, catalog refresh, or scope change
                # may race the remote read.  The run remains historical
                # provenance, but its rows are not persisted to chat or sent
                # to the browser once the pinned definition is no longer
                # current.
                if not await _verify_metric_pin(
                    metric_definition=planning.metric_definition,
                    owner_user_id=owner_user_id,
                    source_id=session.source_id,
                    session_factory=app_session_factory,
                ):
                    message = (
                        "The confirmed metric changed or needs review before its result "
                        "could be delivered. No result rows were shown."
                    )
                    assistant = await _persist_assistant(
                        session_db=session_db,
                        session_id=session_id,
                        content=message,
                        degraded=True,
                        degraded_reason="metric_pin_invalid",
                        token_usage=planning.token_usage,
                    )
                    terminal_persisted = True
                    yield _event(ChatStreamEventType.ERROR, {"message": message})
                    yield _event(ChatStreamEventType.TOKEN, {"text": message})
                    yield _done(assistant, token_usage=planning.token_usage)
                    return
                # This is safe definition provenance only: the service omits
                # fixed-filter values and does not duplicate rows.  It is
                # attached before transcript persistence/SSE serialization.
                result = result.model_copy(
                    update={
                        "metric_provenance": metric_service.metric_provenance(
                            planning.metric_definition
                        )
                    }
                )
            if planning.relationship_definition is not None and not cancellation.cancelled:
                # The gateway also performs this check, but chat is the final
                # row-bearing delivery boundary. Do not persist or stream a
                # result if the owner's approved edge changed after remote
                # execution completed.
                if not await _verify_relationship_pin(
                    relationship_definition=planning.relationship_definition,
                    owner_user_id=owner_user_id,
                    source_id=session.source_id,
                    session_factory=app_session_factory,
                ):
                    message = (
                        "The approved relationship changed or needs review before its "
                        "result could be delivered. No result rows were shown."
                    )
                    assistant = await _persist_assistant(
                        session_db=session_db,
                        session_id=session_id,
                        content=message,
                        degraded=True,
                        degraded_reason="relationship_pin_invalid",
                        token_usage=planning.token_usage,
                    )
                    terminal_persisted = True
                    yield _event(ChatStreamEventType.ERROR, {"message": message})
                    yield _event(ChatStreamEventType.TOKEN, {"text": message})
                    yield _done(assistant, token_usage=planning.token_usage)
                    return
                expected_relationship_id = getattr(
                    planning.relationship_definition, "relationship_id", None
                )
                expected_relationship_version_id = getattr(
                    planning.relationship_definition, "relationship_version_id", None
                )
                expected_version_number = getattr(
                    planning.relationship_definition, "version_number", None
                )
                expected_join_type = getattr(
                    planning.relationship_definition, "join_type", None
                )
                provenance = result.relationship_provenance
                if provenance is not None and (
                    provenance.relationship_id != expected_relationship_id
                    or provenance.relationship_version_id != expected_relationship_version_id
                    or provenance.version_number != expected_version_number
                    or provenance.join_type != expected_join_type
                ):
                    message = (
                        "The approved relationship changed before its result could be "
                        "delivered. No result rows were shown."
                    )
                    assistant = await _persist_assistant(
                        session_db=session_db,
                        session_id=session_id,
                        content=message,
                        degraded=True,
                        degraded_reason="relationship_pin_invalid",
                        token_usage=planning.token_usage,
                    )
                    terminal_persisted = True
                    yield _event(ChatStreamEventType.ERROR, {"message": message})
                    yield _event(ChatStreamEventType.TOKEN, {"text": message})
                    yield _done(assistant, token_usage=planning.token_usage)
                    return
                if provenance is None and (
                    isinstance(expected_relationship_id, uuid.UUID)
                    and isinstance(expected_relationship_version_id, uuid.UUID)
                    and isinstance(expected_version_number, int)
                    and expected_join_type in {"inner", "left"}
                ):
                    # Test/delivery adapters can return a typed success before
                    # adding compiler provenance. The planner's independently
                    # pinned relationship is safe to attach only after the
                    # recheck above; production gateway results already carry
                    # the same compiler-originated record.
                    result = result.model_copy(
                        update={
                            "relationship_provenance": PostgresRelationshipProvenance(
                                relationship_id=expected_relationship_id,
                                relationship_version_id=expected_relationship_version_id,
                                version_number=expected_version_number,
                                join_type=expected_join_type,
                            )
                        }
                    )
        except Exception:
            # Stage 4B returns safe typed failures for expected problems. This
            # guard covers only unexpected orchestration failures and must
            # never invent a result or echo an exception.
            if cancellation.cancelled:
                for event in await _cancelled_events():
                    yield event
                return
            message = "The database calculation could not be started safely, so no answer was generated."
            assistant = await _persist_assistant(
                session_db=session_db,
                session_id=session_id,
                content=message,
                degraded=True,
                degraded_reason="execution_failed",
                token_usage=planning.token_usage,
            )
            terminal_persisted = True
            yield _event(ChatStreamEventType.ERROR, {"message": message})
            yield _event(ChatStreamEventType.TOKEN, {"text": message})
            yield _done(assistant, token_usage=planning.token_usage)
            return

        # Explicit cancellation can race the executor's successful return.
        # Treat a winning cancellation as terminal before any authorization,
        # persistence, or SSE path can inspect the returned rows.
        if cancellation.cancelled:
            for event in await _cancelled_events():
                yield event
            return

        if not await _authorize_before_disclosure(
            owner_user_id=owner_user_id,
            session_id=session_id,
            session_factory=app_session_factory,
        ):
            # A source deletion/revocation after execution is never a licence
            # to release a buffered result. Do not recreate that session.
            terminal_emitted = True
            yield _event(
                ChatStreamEventType.ERROR,
                {"message": "Access changed before the database result could be delivered."},
            )
            yield _event(ChatStreamEventType.DONE, {"session_id": str(session_id)})
            return
        if cancellation.cancelled:
            for event in await _cancelled_events():
                yield event
            return

        # Revalidate semantic pins immediately before the one row-bearing
        # transcript write. The gateway checks them too, but a metric or
        # relationship can still be retired/revised between its return and
        # this final delivery decision.
        if planning.metric_definition is not None and not await _verify_metric_pin(
            metric_definition=planning.metric_definition,
            owner_user_id=owner_user_id,
            source_id=session.source_id,
            session_factory=app_session_factory,
        ):
            message = (
                "The confirmed metric changed or needs review before its result could "
                "be delivered. No result rows were shown."
            )
            assistant = await _persist_assistant(
                session_db=session_db,
                session_id=session_id,
                content=message,
                degraded=True,
                degraded_reason="metric_pin_invalid",
                token_usage=planning.token_usage,
            )
            terminal_persisted = True
            yield _event(ChatStreamEventType.ERROR, {"message": message})
            yield _event(ChatStreamEventType.TOKEN, {"text": message})
            yield _done(assistant, token_usage=planning.token_usage)
            return
        if planning.relationship_definition is not None and not await _verify_relationship_pin(
            relationship_definition=planning.relationship_definition,
            owner_user_id=owner_user_id,
            source_id=session.source_id,
            session_factory=app_session_factory,
        ):
            message = (
                "The approved relationship changed or needs review before its result "
                "could be delivered. No result rows were shown."
            )
            assistant = await _persist_assistant(
                session_db=session_db,
                session_id=session_id,
                content=message,
                degraded=True,
                degraded_reason="relationship_pin_invalid",
                token_usage=planning.token_usage,
            )
            terminal_persisted = True
            yield _event(ChatStreamEventType.ERROR, {"message": message})
            yield _event(ChatStreamEventType.TOKEN, {"text": message})
            yield _done(assistant, token_usage=planning.token_usage)
            return

        summary = _summary_for_result(result)
        # Atomically order a cancel request and the final row-bearing write.
        # A cancellation already accepted means we never retain the result;
        # one accepted later is still checked before SSE disclosure below.
        delivery_allowed = False
        async with cancellation.delivery_guard() as delivery_allowed:
            if delivery_allowed:
                assistant = await _persist_assistant(
                    session_db=session_db,
                    session_id=session_id,
                    content=summary,
                    postgres_result=result,
                    degraded=not result.is_ok,
                    degraded_reason=result.failure_code,
                    token_usage=planning.token_usage,
                )
                terminal_persisted = True
        if not delivery_allowed:
            for event in await _cancelled_events():
                yield event
            return

        # Store the bounded typed result before sending it, then repeat owner
        # resolution after the write. These checks close executor-to-write and
        # write-to-disclosure gaps without treating a session ID as authority.
        if cancellation.cancelled:
            yield _event(
                ChatStreamEventType.ERROR,
                {"message": "The database calculation was cancelled before its result could be delivered."},
            )
            yield _done(assistant, token_usage=planning.token_usage)
            return
        if not await _authorize_before_disclosure(
            owner_user_id=owner_user_id,
            session_id=session_id,
            session_factory=app_session_factory,
        ):
            terminal_emitted = True
            yield _event(
                ChatStreamEventType.ERROR,
                {"message": "Access changed before the database result could be delivered."},
            )
            yield _event(ChatStreamEventType.DONE, {"session_id": str(session_id)})
            return
        if cancellation.cancelled:
            yield _event(
                ChatStreamEventType.ERROR,
                {"message": "The database calculation was cancelled before its result could be delivered."},
            )
            yield _done(assistant, token_usage=planning.token_usage)
            return
        safe_result = result.model_dump(mode="json")
        terminal_emitted = True
        yield _event(ChatStreamEventType.POSTGRES_RESULT, {"postgres_result": safe_result})
        yield _event(ChatStreamEventType.TOKEN, {"text": summary})
        yield _done(assistant, token_usage=planning.token_usage)
    except asyncio.CancelledError:
        await _cleanup_interrupted_turn()
        raise
    except Exception:
        # All expected planner/executor failures normally return typed
        # outcomes above.  This catches only an unexpected orchestration or
        # result-persistence fault after the committed user message.  It must
        # never disclose a buffered result or label the failure as a user
        # cancellation.
        if cancellation.cancelled:
            for event in await _cancelled_events():
                yield event
        else:
            message = "The database result could not be delivered safely, so no answer was generated."
            assistant = await _persist_safe_delivery_failure()
            # If persistence was interrupted, leave this false so the
            # cancellation/finalizer path can still repair the committed user
            # turn. A completed persistence or a completed no-op may safely
            # suppress that duplicate cleanup.
            terminal_emitted = True
            yield _event(ChatStreamEventType.ERROR, {"message": message})
            yield _event(ChatStreamEventType.TOKEN, {"text": message})
            if assistant is not None:
                yield _done(assistant)
            else:
                yield _event(ChatStreamEventType.DONE, {"session_id": str(session_id)})
    finally:
        # Routes may close an async generator directly after an SSE frame,
        # which runs ``finally`` without raising CancelledError at the inner
        # executor await. This repairs a committed user turn in that path too.
        await _cleanup_interrupted_turn()


__all__ = ["stream_postgres_chat_turn"]
