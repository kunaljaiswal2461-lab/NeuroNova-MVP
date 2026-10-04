"""Owner-scoped PostgreSQL source onboarding service.

The remote test/inventory operation completes before this service opens an
application-database write transaction. Only the successful observation and
explicit selected table scope are persisted.
"""
from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.connectors.connection_policy import ConnectionPolicyError
from app.connectors.postgres_adapter import (
    InaccessibleInventoryError,
    InventoryOverflowError,
    InventoryTable,
    PostgresAdapterError,
    PostgresOnboardingAdapter,
    PostgresTLSFailure,
    PostgresTimeoutError,
)
from app.core.config import Settings
from app.core.connector_credentials import (
    CredentialBundle,
    CredentialConfigurationError,
    CredentialProtector,
)
from app.db.models.source import (
    DataSource,
    SourceCredential,
    SourceStatus,
    SourceTableScope,
    SourceType,
    TLSMode,
)
from app.db.models.catalog import CatalogSyncRun, CatalogSyncRunStatus
from app.db.models.conversation import ChatMessageRow, ConversationSessionRow
from app.db.models.source_profile import SourceProfileRun, SourceProfileRunStatus
from app.exceptions.custom_exceptions import AppError, NotFoundError
from app.schemas.source_schemas import (
    CreateSourceRequest,
    InventoryTableResponse,
    PostgresTestRequest,
    PostgresTestResponse,
    SavedSourceCredentialReplacementRequest,
    SavedSourceTestResponse,
    SourceCatalogRefreshPolicyRequest,
    SourceCatalogRefreshPolicyResponse,
    SourceDetailResponse,
    SourceListResponse,
    SourceScopeUpdateRequest,
    SourceSummaryResponse,
)


class SourceServiceError(AppError):
    pass


class ConnectorDisabledError(SourceServiceError):
    status_code = 503
    code = "connectors_disabled"

    def __init__(self):
        super().__init__("database source onboarding is not enabled")


class ConnectorActivationError(SourceServiceError):
    status_code = 503
    code = "connector_egress_not_configured"

    def __init__(self):
        super().__init__("database source onboarding is not activated")


class CredentialKeyUnavailableError(SourceServiceError):
    status_code = 503
    code = "credential_key_unavailable"

    def __init__(self):
        super().__init__("database credential protection is unavailable")


class InvalidConnectionSettingsError(SourceServiceError):
    status_code = 422
    code = "unsupported_connection_settings"

    def __init__(self):
        super().__init__("only public PostgreSQL DNS hosts on port 5432 with verified TLS are supported")


class SourceConnectionFailedError(SourceServiceError):
    status_code = 503
    code = "connection_failed"

    def __init__(self):
        super().__init__("could not connect to the PostgreSQL source; verify its network, TLS, and credentials")


class SourceConnectionTimeoutError(SourceServiceError):
    status_code = 504
    code = "connection_timeout"

    def __init__(self):
        super().__init__("the PostgreSQL source did not respond before the connection timeout")


class SourceTLSFailureError(SourceServiceError):
    status_code = 422
    code = "tls_verification_failed"

    def __init__(self):
        super().__init__("the PostgreSQL source TLS certificate could not be verified")


class SourceInventoryOverflowError(SourceServiceError):
    status_code = 422
    code = "inventory_limit_exceeded"

    def __init__(self):
        super().__init__("the PostgreSQL role exposes more than 1,000 eligible tables; restrict the role before onboarding")


class SourceInventoryEmptyError(SourceServiceError):
    status_code = 422
    code = "no_eligible_tables"

    def __init__(self):
        super().__init__("the PostgreSQL role has no eligible base tables with complete read access")


class InvalidSourceScopeError(SourceServiceError):
    status_code = 422
    code = "invalid_source_scope"

    def __init__(self):
        super().__init__("one or more selected tables are no longer eligible for this source")


class SourcePersistenceError(SourceServiceError):
    """Safe boundary for application-database failures on source routes."""

    status_code = 503
    code = "source_persistence_failed"

    def __init__(self):
        super().__init__("could not complete the source configuration request")


_operation_gates: dict[int, asyncio.Semaphore] = {}


@asynccontextmanager
async def _outbound_operation_slot(limit: int) -> AsyncIterator[None]:
    """Bound outbound handshakes per application process.

    This complements route rate limits. It intentionally does not attempt to
    coordinate multiple replicas; production activation uses one worker today
    and must add an external limiter before scaling connector traffic.
    """
    gate = _operation_gates.setdefault(limit, asyncio.Semaphore(limit))
    await gate.acquire()
    try:
        yield
    finally:
        gate.release()


AdapterFactory = Callable[[Settings], PostgresOnboardingAdapter]


def _build_adapter(settings: Settings) -> PostgresOnboardingAdapter:
    return PostgresOnboardingAdapter(settings=settings)


def require_credential_operations(settings: Settings) -> CredentialProtector:
    """Fail closed unless connector work can safely be requested or run.

    Both onboarding and the later durable-catalog request path use this one
    activation/credential boundary. It intentionally returns a protector so
    callers that need it do not reimplement configuration validation.
    """

    if not settings.connectors_enabled:
        raise ConnectorDisabledError()
    # This acknowledgement cannot replace an egress firewall/proxy. It makes
    # production activation explicit rather than silently enabling arbitrary
    # outbound connections when only a feature flag is set.
    if not settings.connector_egress_policy_configured:
        raise ConnectorActivationError()
    try:
        return CredentialProtector.from_settings(settings)
    except CredentialConfigurationError:
        raise CredentialKeyUnavailableError() from None


def _request_secrets(request: PostgresTestRequest | CreateSourceRequest) -> tuple[str, str, str | None]:
    custom_ca = request.tls.custom_ca_pem
    return (
        request.credentials.username.get_secret_value(),
        request.credentials.password.get_secret_value(),
        custom_ca.get_secret_value() if custom_ca is not None else None,
    )


async def _test_and_inventory(
    request: PostgresTestRequest | CreateSourceRequest,
    *,
    settings: Settings,
    adapter_factory: AdapterFactory | None = None,
) -> list[InventoryTable]:
    require_credential_operations(settings)
    username, password, custom_ca_pem = _request_secrets(request)
    return await _test_and_inventory_values(
        host=request.host,
        port=request.port,
        database_name=request.database_name,
        username=username,
        password=password,
        custom_ca_pem=custom_ca_pem,
        settings=settings,
        adapter_factory=adapter_factory,
    )


async def _test_and_inventory_values(
    *,
    host: str,
    port: int,
    database_name: str,
    username: str,
    password: str,
    custom_ca_pem: str | None,
    settings: Settings,
    adapter_factory: AdapterFactory | None = None,
) -> list[InventoryTable]:
    """Run the bounded names-only inventory without retaining caller secrets."""

    require_credential_operations(settings)
    try:
        async with _outbound_operation_slot(settings.connector_max_concurrent_operations):
            # Resolve the default only at invocation. A same-process
            # disposable integration fixture can explicitly monkeypatch this
            # factory to inject its test-only destination policy; no request
            # field or deployment setting can select that policy.
            factory = adapter_factory or _build_adapter
            return await factory(settings).test_and_list_tables(
                host=host,
                port=port,
                database_name=database_name,
                username=username,
                password=password,
                custom_ca_pem=custom_ca_pem,
            )
    except ConnectionPolicyError:
        raise InvalidConnectionSettingsError() from None
    except PostgresTLSFailure:
        raise SourceTLSFailureError() from None
    except PostgresTimeoutError:
        raise SourceConnectionTimeoutError() from None
    except InventoryOverflowError:
        raise SourceInventoryOverflowError() from None
    except InaccessibleInventoryError:
        raise SourceInventoryEmptyError() from None
    except PostgresAdapterError:
        raise SourceConnectionFailedError() from None


async def test_postgresql_source(
    request: PostgresTestRequest,
    *,
    settings: Settings,
    adapter_factory: AdapterFactory | None = None,
) -> PostgresTestResponse:
    inventory = await _test_and_inventory(request, settings=settings, adapter_factory=adapter_factory)
    tables = [
        InventoryTableResponse(schema_name=item.schema_name, table_name=item.table_name)
        for item in inventory
    ]
    return PostgresTestResponse(eligible_tables=tables, count=len(tables))


async def create_postgresql_source(
    session: AsyncSession,
    *,
    owner_user_id: uuid.UUID,
    request: CreateSourceRequest,
    settings: Settings,
    adapter_factory: AdapterFactory | None = None,
) -> SourceDetailResponse:
    # Network work is intentionally complete before the first application DB
    # write; no transaction stays open while a customer host is contacted.
    inventory = await _test_and_inventory(request, settings=settings, adapter_factory=adapter_factory)
    inventory_set = {(item.schema_name, item.table_name) for item in inventory}
    selected = {(item.schema_name, item.table_name) for item in request.selected_tables}
    if not selected or not selected.issubset(inventory_set):
        raise InvalidSourceScopeError()

    protector = require_credential_operations(settings)
    source_id = uuid.uuid4()
    username, password, custom_ca_pem = _request_secrets(request)
    ciphertext, key_id, encryption_format = protector.encrypt(
        CredentialBundle(
            source_id=source_id,
            username=username,
            password=password,
            custom_ca_pem=custom_ca_pem,
        )
    )
    now = datetime.now(timezone.utc)
    source = DataSource(
        id=source_id,
        owner_user_id=owner_user_id,
        source_type=SourceType.POSTGRESQL,
        display_name=request.display_name,
        host=request.host,
        port=request.port,
        database_name=request.database_name,
        tls_mode=TLSMode.VERIFY_FULL,
        has_custom_ca=custom_ca_pem is not None,
        status=SourceStatus.TESTED,
        last_tested_at=now,
        profiling_enabled=False,
        credential=SourceCredential(
            ciphertext=ciphertext,
            key_id=key_id,
            encryption_format=encryption_format,
        ),
        table_scopes=[
            SourceTableScope(schema_name=schema_name, table_name=table_name)
            for schema_name, table_name in sorted(selected)
        ],
    )
    try:
        session.add(source)
        await session.commit()
    except SQLAlchemyError:
        await _rollback_quietly(session)
        # Do not re-raise driver/SQL strings: the global fallback logs them
        # verbatim and some dialects include bound persistence parameters.
        raise SourcePersistenceError() from None
    # Re-read with explicit scope loading. ``AsyncSession.refresh`` expires
    # relationships, and allowing a response serializer to lazy-load them
    # would fail outside SQLAlchemy's async greenlet context.
    return _source_detail(
        await get_source_for_owner(session, source_id=source_id, owner_user_id=owner_user_id)
    )


async def list_sources_for_owner(
    session: AsyncSession,
    *,
    owner_user_id: uuid.UUID,
) -> SourceListResponse:
    try:
        result = await session.execute(
            select(DataSource)
            .where(DataSource.owner_user_id == owner_user_id)
            .options(selectinload(DataSource.table_scopes))
            .order_by(DataSource.created_at.desc())
        )
    except SQLAlchemyError:
        raise SourcePersistenceError() from None
    sources = list(result.scalars().unique().all())
    return SourceListResponse(items=[_source_summary(source) for source in sources], count=len(sources))


async def get_source_for_owner(
    session: AsyncSession,
    *,
    source_id: uuid.UUID,
    owner_user_id: uuid.UUID,
) -> DataSource:
    try:
        result = await session.execute(
            select(DataSource)
            .where(DataSource.id == source_id, DataSource.owner_user_id == owner_user_id)
            .options(selectinload(DataSource.table_scopes))
        )
    except SQLAlchemyError:
        raise SourcePersistenceError() from None
    source = result.scalar_one_or_none()
    if source is None:
        raise NotFoundError("source not found")
    return source


async def read_source_for_owner(
    session: AsyncSession,
    *,
    source_id: uuid.UUID,
    owner_user_id: uuid.UUID,
) -> SourceDetailResponse:
    return _source_detail(
        await get_source_for_owner(session, source_id=source_id, owner_user_id=owner_user_id)
    )


async def delete_source_for_owner(
    session: AsyncSession,
    *,
    source_id: uuid.UUID,
    owner_user_id: uuid.UUID,
) -> None:
    # This path intentionally never constructs a CredentialProtector or opens
    # a client database connection, so cleanup works after a key rotation or
    # a remote database outage.
    source = await get_source_for_owner(
        session, source_id=source_id, owner_user_id=owner_user_id
    )
    try:
        await session.delete(source)
        await session.commit()
    except SQLAlchemyError:
        await _rollback_quietly(session)
        raise SourcePersistenceError() from None


async def _load_saved_connection_for_operation(
    session: AsyncSession,
    *,
    source_id: uuid.UUID,
    owner_user_id: uuid.UUID,
    settings: Settings,
) -> tuple[DataSource, CredentialBundle]:
    """Decrypt one owner-scoped saved connection, then end the read txn.

    Callers must perform remote work only after this helper returns.  It keeps
    source configuration and application-database locks out of the network
    window, while the generation fence below catches a concurrent change.
    """

    protector = require_credential_operations(settings)
    try:
        result = await session.execute(
            select(DataSource)
            .where(DataSource.id == source_id, DataSource.owner_user_id == owner_user_id)
            .options(selectinload(DataSource.credential), selectinload(DataSource.table_scopes))
        )
        source = result.scalar_one_or_none()
        if source is None:
            raise NotFoundError("source not found")
        if source.source_type is not SourceType.POSTGRESQL or source.credential is None:
            raise SourceConnectionFailedError()
        try:
            bundle = protector.decrypt(
                source_id=source.id,
                ciphertext=source.credential.ciphertext,
                key_id=source.credential.key_id,
                encryption_format=source.credential.encryption_format,
            )
        except Exception as exc:
            # Credential protector errors deliberately never escape this
            # service boundary because some providers include key metadata.
            raise CredentialKeyUnavailableError() from exc
        # Detach the small scalar snapshot before ending the app DB read.
        captured = DataSource(
            id=source.id,
            owner_user_id=source.owner_user_id,
            source_type=source.source_type,
            display_name=source.display_name,
            host=source.host,
            port=source.port,
            database_name=source.database_name,
            tls_mode=source.tls_mode,
            has_custom_ca=source.has_custom_ca,
            status=source.status,
            configuration_generation=source.configuration_generation,
        )
        captured.table_scopes = [
            SourceTableScope(schema_name=row.schema_name, table_name=row.table_name)
            for row in source.table_scopes
        ]
        return captured, bundle
    except SQLAlchemyError:
        raise SourcePersistenceError() from None
    finally:
        await _rollback_quietly(session)


async def _locked_saved_source(
    session: AsyncSession,
    *,
    source_id: uuid.UUID,
    owner_user_id: uuid.UUID,
) -> DataSource:
    result = await session.execute(
        select(DataSource)
        .where(DataSource.id == source_id, DataSource.owner_user_id == owner_user_id)
        .options(selectinload(DataSource.credential), selectinload(DataSource.table_scopes))
        .with_for_update()
    )
    source = result.scalar_one_or_none()
    if source is None:
        raise NotFoundError("source not found")
    return source


async def _cancel_source_work(
    session: AsyncSession,
    *,
    source_id: uuid.UUID,
    failure_code: str,
    scheduled_only: bool = False,
) -> None:
    """Fence queued/leased work; a worker observes cancellation on renewal.

    This does not try to interrupt an arbitrary client socket from a web
    process.  The durable lease/renewal boundary is the safe cross-process
    cancellation mechanism already used by catalog and profile workers.
    """

    now = datetime.now(timezone.utc)
    catalog_conditions = [
        CatalogSyncRun.source_id == source_id,
        CatalogSyncRun.status.in_((CatalogSyncRunStatus.QUEUED, CatalogSyncRunStatus.RUNNING)),
    ]
    if scheduled_only:
        catalog_conditions.append(CatalogSyncRun.scheduled.is_(True))
    await session.execute(
        update(CatalogSyncRun)
        .where(*catalog_conditions)
        .values(
            status=CatalogSyncRunStatus.CANCELLED,
            completed_at=now,
            failure_code=failure_code,
            next_attempt_at=None,
            cancel_requested_at=now,
            lease_owner=None,
            lease_token=None,
            lease_expires_at=None,
        )
    )
    if not scheduled_only:
        await session.execute(
            update(SourceProfileRun)
            .where(
                SourceProfileRun.source_id == source_id,
                SourceProfileRun.status.in_(
                    (SourceProfileRunStatus.QUEUED, SourceProfileRunStatus.RUNNING)
                ),
            )
            .values(
                status=SourceProfileRunStatus.CANCELLED,
                completed_at=now,
                failure_code=failure_code,
                next_attempt_at=None,
                cancel_requested_at=now,
                lease_owner=None,
                lease_token=None,
                lease_expires_at=None,
            )
        )


async def _clear_source_transcript_results(
    session: AsyncSession, *, source_id: uuid.UUID
) -> None:
    """Retain chat text but redact typed rows after a selected-table removal."""

    source_sessions = select(ConversationSessionRow.id).where(
        ConversationSessionRow.source_id == source_id
    )
    await session.execute(
        update(ChatMessageRow)
        .where(
            ChatMessageRow.session_id.in_(source_sessions),
            ChatMessageRow.postgres_result.is_not(None),
        )
        .values(postgres_result=None)
    )


async def _write_source_test_state(
    session: AsyncSession,
    *,
    source_id: uuid.UUID,
    owner_user_id: uuid.UUID,
    expected_generation: int,
    status: SourceStatus,
    error_code: str | None,
) -> DataSource | None:
    source = await _locked_saved_source(
        session, source_id=source_id, owner_user_id=owner_user_id
    )
    if source.configuration_generation != expected_generation:
        await _rollback_quietly(session)
        return None
    source.status = status
    source.last_error_code = error_code
    source.last_tested_at = datetime.now(timezone.utc)
    await session.commit()
    return source


async def retest_saved_postgresql_source_for_owner(
    session: AsyncSession,
    *,
    source_id: uuid.UUID,
    owner_user_id: uuid.UUID,
    settings: Settings,
    adapter_factory: AdapterFactory | None = None,
) -> SavedSourceTestResponse:
    """Retest only the immutable saved endpoint with its protected secret."""

    captured, bundle = await _load_saved_connection_for_operation(
        session, source_id=source_id, owner_user_id=owner_user_id, settings=settings
    )
    try:
        inventory = await _test_and_inventory_values(
            host=captured.host,
            port=captured.port,
            database_name=captured.database_name,
            username=bundle.username,
            password=bundle.password,
            custom_ca_pem=bundle.custom_ca_pem,
            settings=settings,
            adapter_factory=adapter_factory,
        )
    except SourceServiceError as exc:
        # A test result is safe operational state; persist only its bounded
        # classifier, and only if another maintenance write did not win.
        try:
            await _write_source_test_state(
                session,
                source_id=source_id,
                owner_user_id=owner_user_id,
                expected_generation=captured.configuration_generation,
                status=SourceStatus.CONNECTION_FAILED,
                error_code=exc.code,
            )
        except (SQLAlchemyError, SourceServiceError):
            await _rollback_quietly(session)
        raise
    source = await _write_source_test_state(
        session,
        source_id=source_id,
        owner_user_id=owner_user_id,
        expected_generation=captured.configuration_generation,
        status=SourceStatus.TESTED,
        error_code=None,
    )
    if source is None:
        raise SourcePersistenceError()
    # The mutation's relationship was loaded before commit. Re-read rather
    # than allowing response serialization to lazy-load in async context.
    detail = await read_source_for_owner(session, source_id=source_id, owner_user_id=owner_user_id)
    return SavedSourceTestResponse(
        eligible_tables=[
            InventoryTableResponse(schema_name=row.schema_name, table_name=row.table_name)
            for row in inventory
        ],
        count=len(inventory),
        source=detail,
    )


async def replace_saved_postgresql_credentials_for_owner(
    session: AsyncSession,
    *,
    source_id: uuid.UUID,
    owner_user_id: uuid.UUID,
    request: SavedSourceCredentialReplacementRequest,
    settings: Settings,
    adapter_factory: AdapterFactory | None = None,
) -> SourceDetailResponse:
    """Validate and atomically rotate only a saved source's credential bundle."""

    # Read endpoint/generation without decrypting the old credential. A
    # replacement must be validated with the proposed bundle first.
    source = await get_source_for_owner(session, source_id=source_id, owner_user_id=owner_user_id)
    captured_generation = source.configuration_generation
    host, port, database_name = source.host, source.port, source.database_name
    await _rollback_quietly(session)
    username = request.credentials.username.get_secret_value()
    password = request.credentials.password.get_secret_value()
    custom_ca_pem = (
        request.tls.custom_ca_pem.get_secret_value() if request.tls.custom_ca_pem is not None else None
    )
    await _test_and_inventory_values(
        host=host,
        port=port,
        database_name=database_name,
        username=username,
        password=password,
        custom_ca_pem=custom_ca_pem,
        settings=settings,
        adapter_factory=adapter_factory,
    )
    protector = require_credential_operations(settings)
    ciphertext, key_id, encryption_format = protector.encrypt(
        CredentialBundle(
            source_id=source_id,
            username=username,
            password=password,
            custom_ca_pem=custom_ca_pem,
        )
    )
    try:
        locked = await _locked_saved_source(
            session, source_id=source_id, owner_user_id=owner_user_id
        )
        if locked.configuration_generation != captured_generation or locked.credential is None:
            await _rollback_quietly(session)
            raise SourcePersistenceError()
        locked.credential.ciphertext = ciphertext
        locked.credential.key_id = key_id
        locked.credential.encryption_format = encryption_format
        locked.has_custom_ca = custom_ca_pem is not None
        locked.status = SourceStatus.TESTED
        locked.last_error_code = None
        locked.last_tested_at = datetime.now(timezone.utc)
        locked.configuration_generation += 1
        await _cancel_source_work(
            session, source_id=source_id, failure_code="configuration_changed"
        )
        await session.commit()
        session.expire_all()
    except SQLAlchemyError:
        await _rollback_quietly(session)
        raise SourcePersistenceError() from None
    return await read_source_for_owner(session, source_id=source_id, owner_user_id=owner_user_id)


async def update_saved_postgresql_scope_for_owner(
    session: AsyncSession,
    *,
    source_id: uuid.UUID,
    owner_user_id: uuid.UUID,
    request: SourceScopeUpdateRequest,
    settings: Settings,
    adapter_factory: AdapterFactory | None = None,
) -> SourceDetailResponse:
    """Retest the saved connection and persist a validated explicit scope."""

    captured, bundle = await _load_saved_connection_for_operation(
        session, source_id=source_id, owner_user_id=owner_user_id, settings=settings
    )
    inventory = await _test_and_inventory_values(
        host=captured.host,
        port=captured.port,
        database_name=captured.database_name,
        username=bundle.username,
        password=bundle.password,
        custom_ca_pem=bundle.custom_ca_pem,
        settings=settings,
        adapter_factory=adapter_factory,
    )
    selected = {(item.schema_name, item.table_name) for item in request.selected_tables}
    inventory_set = {(item.schema_name, item.table_name) for item in inventory}
    if not selected.issubset(inventory_set):
        raise InvalidSourceScopeError()
    try:
        locked = await _locked_saved_source(
            session, source_id=source_id, owner_user_id=owner_user_id
        )
        if locked.configuration_generation != captured.configuration_generation:
            await _rollback_quietly(session)
            raise SourcePersistenceError()
        previous = {(item.schema_name, item.table_name) for item in locked.table_scopes}
        locked.status = SourceStatus.TESTED
        locked.last_error_code = None
        locked.last_tested_at = datetime.now(timezone.utc)
        if previous != selected:
            removed = previous - selected
            await session.execute(
                delete(SourceTableScope).where(SourceTableScope.source_id == locked.id)
            )
            for schema_name, table_name in sorted(selected):
                session.add(
                    SourceTableScope(
                        source_id=locked.id, schema_name=schema_name, table_name=table_name
                    )
                )
            locked.configuration_generation += 1
            await _cancel_source_work(session, source_id=source_id, failure_code="scope_changed")
            if removed:
                await _clear_source_transcript_results(session, source_id=source_id)
        await session.commit()
        # Bulk scope deletion intentionally bypasses relationship bookkeeping;
        # reload the response so it cannot report the old in-memory scope.
        session.expire_all()
    except SQLAlchemyError:
        await _rollback_quietly(session)
        raise SourcePersistenceError() from None
    return await read_source_for_owner(session, source_id=source_id, owner_user_id=owner_user_id)


async def configure_saved_source_catalog_refresh_for_owner(
    session: AsyncSession,
    *,
    source_id: uuid.UUID,
    owner_user_id: uuid.UUID,
    request: SourceCatalogRefreshPolicyRequest,
) -> SourceCatalogRefreshPolicyResponse:
    """Set an opt-in structural refresh schedule; it never runs inline."""

    try:
        source = await _locked_saved_source(
            session, source_id=source_id, owner_user_id=owner_user_id
        )
        source.catalog_refresh_enabled = request.enabled
        source.catalog_refresh_interval_minutes = request.interval_minutes
        source.catalog_refresh_next_due_at = (
            datetime.now(timezone.utc) + timedelta(minutes=request.interval_minutes)
            if request.enabled and request.interval_minutes is not None
            else None
        )
        if not request.enabled:
            await _cancel_source_work(
                session,
                source_id=source_id,
                failure_code="schedule_disabled",
                scheduled_only=True,
            )
        await session.commit()
        return SourceCatalogRefreshPolicyResponse(
            source_id=source.id,
            enabled=source.catalog_refresh_enabled,
            interval_minutes=source.catalog_refresh_interval_minutes,
            next_due_at=source.catalog_refresh_next_due_at,
        )
    except SQLAlchemyError:
        await _rollback_quietly(session)
        raise SourcePersistenceError() from None


async def _rollback_quietly(session: AsyncSession) -> None:
    """Leave source persistence failures safe even when rollback also fails."""
    try:
        await session.rollback()
    except Exception:
        pass


def _source_summary(source: DataSource) -> SourceSummaryResponse:
    return SourceSummaryResponse(
        source_id=source.id,
        display_name=source.display_name,
        source_type=source.source_type,
        status=source.status,
        host=source.host,
        port=source.port,
        database_name=source.database_name,
        tls_mode=source.tls_mode,
        has_custom_ca=source.has_custom_ca,
        profiling_enabled=source.profiling_enabled,
        configuration_generation=source.configuration_generation,
        scope_count=len(source.table_scopes),
        last_tested_at=source.last_tested_at,
        catalog_refresh_enabled=source.catalog_refresh_enabled,
        catalog_refresh_interval_minutes=source.catalog_refresh_interval_minutes,
        catalog_refresh_next_due_at=source.catalog_refresh_next_due_at,
        catalog_last_success_at=source.catalog_last_success_at,
        catalog_last_failure_code=source.catalog_last_failure_code,
        created_at=source.created_at,
        updated_at=source.updated_at,
    )


def _source_detail(source: DataSource) -> SourceDetailResponse:
    return SourceDetailResponse(
        **_source_summary(source).model_dump(),
        selected_tables=[
            InventoryTableResponse(schema_name=scope.schema_name, table_name=scope.table_name)
            for scope in sorted(source.table_scopes, key=lambda item: (item.schema_name, item.table_name))
        ],
    )
