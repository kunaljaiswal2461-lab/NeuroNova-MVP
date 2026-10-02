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
from datetime import datetime, timezone

from sqlalchemy import select
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
from app.exceptions.custom_exceptions import AppError, NotFoundError
from app.schemas.source_schemas import (
    CreateSourceRequest,
    InventoryTableResponse,
    PostgresTestRequest,
    PostgresTestResponse,
    SourceDetailResponse,
    SourceListResponse,
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
    try:
        async with _outbound_operation_slot(settings.connector_max_concurrent_operations):
            # Resolve the default only at invocation. A same-process
            # disposable integration fixture can explicitly monkeypatch this
            # factory to inject its test-only destination policy; no request
            # field or deployment setting can select that policy.
            factory = adapter_factory or _build_adapter
            return await factory(settings).test_and_list_tables(
                host=request.host,
                port=request.port,
                database_name=request.database_name,
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
        scope_count=len(source.table_scopes),
        last_tested_at=source.last_tested_at,
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
