from __future__ import annotations

import uuid

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import event, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.connectors.postgres_adapter import InventoryTable
from app.core.config import Settings
from app.core.connector_credentials import CredentialProtector
from app.db.models.source import DataSource, SourceCredential, SourceStatus, SourceTableScope
from app.db.models.user import User, UserRole
from app.exceptions.custom_exceptions import NotFoundError
from app.schemas.source_schemas import CreateSourceRequest, PostgresTestRequest, SourceScopeUpdateRequest
from app.services import source_service


class _Adapter:
    def __init__(self, inventory: list[InventoryTable]):
        self.inventory = inventory
        self.calls = 0

    async def test_and_list_tables(self, **_kwargs):
        self.calls += 1
        return self.inventory


def _settings(*, enabled: bool = True) -> Settings:
    return Settings(
        _env_file=None,
        api_key="stage2b-test-api-key",
        database_url="sqlite+aiosqlite://",
        connectors_enabled=enabled,
        connector_egress_policy_configured=True,
        connector_credentials_active_key_id="test-key",
        connector_credentials_active_key=Fernet.generate_key().decode(),
    )


def _request(*, selected_tables=None) -> CreateSourceRequest:
    return CreateSourceRequest(
        display_name="Customer warehouse",
        host="warehouse.example.com",
        database_name="analytics",
        credentials={"username": "reporter", "password": "not-in-response"},
        tls={"mode": "verify-full", "custom_ca_pem": "CUSTOM CA"},
        selected_tables=selected_tables or [{"schema_name": "public", "table_name": "orders"}],
    )


@pytest.fixture
async def source_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")

    @event.listens_for(engine.sync_engine, "connect")
    def _enable_foreign_keys(dbapi_connection, _connection_record):
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync_connection: DataSource.metadata.create_all(
                sync_connection,
                tables=[User.__table__, DataSource.__table__, SourceCredential.__table__, SourceTableScope.__table__],
            )
        )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


async def _make_user(session, email: str) -> User:
    user = User(
        id=uuid.uuid4(),
        email=email,
        name=email.split("@", 1)[0],
        role=UserRole.USER,
        is_active=True,
        is_verified=True,
    )
    session.add(user)
    await session.commit()
    return user


@pytest.mark.asyncio
async def test_create_revalidates_scope_encrypts_credentials_and_scopes_owner(source_session):
    owner = await _make_user(source_session, "owner@example.test")
    adapter = _Adapter([
        InventoryTable("public", "orders"),
        InventoryTable("public", "customers"),
    ])
    settings = _settings()

    created = await source_service.create_postgresql_source(
        source_session,
        owner_user_id=owner.id,
        request=_request(),
        settings=settings,
        adapter_factory=lambda _settings: adapter,
    )

    assert created.source_id
    assert created.selected_tables[0].table_name == "orders"
    assert created.profiling_enabled is False
    assert "not-in-response" not in created.model_dump_json()
    assert adapter.calls == 1

    credential = (await source_session.execute(select(SourceCredential))).scalar_one()
    assert b"not-in-response" not in credential.ciphertext
    bundle = CredentialProtector.from_settings(settings).decrypt(
        source_id=created.source_id,
        ciphertext=credential.ciphertext,
        key_id=credential.key_id,
        encryption_format=credential.encryption_format,
    )
    assert bundle.username == "reporter"
    assert bundle.custom_ca_pem == "CUSTOM CA"


@pytest.mark.asyncio
async def test_create_rejects_scope_not_in_fresh_inventory_without_persisting(source_session):
    owner = await _make_user(source_session, "owner@example.test")
    adapter = _Adapter([InventoryTable("public", "orders")])

    with pytest.raises(source_service.InvalidSourceScopeError):
        await source_service.create_postgresql_source(
            source_session,
            owner_user_id=owner.id,
            request=_request(selected_tables=[{"schema_name": "public", "table_name": "removed"}]),
            settings=_settings(),
            adapter_factory=lambda _settings: adapter,
        )
    assert (await source_session.execute(select(DataSource))).scalars().all() == []
    assert (await source_session.execute(select(SourceCredential))).scalars().all() == []


@pytest.mark.asyncio
async def test_owner_scope_and_delete_do_not_decrypt_credentials(source_session):
    owner = await _make_user(source_session, "owner@example.test")
    other = await _make_user(source_session, "other@example.test")
    adapter = _Adapter([InventoryTable("public", "orders")])
    created = await source_service.create_postgresql_source(
        source_session,
        owner_user_id=owner.id,
        request=_request(),
        settings=_settings(),
        adapter_factory=lambda _settings: adapter,
    )

    assert (await source_service.list_sources_for_owner(source_session, owner_user_id=owner.id)).count == 1
    assert (await source_service.list_sources_for_owner(source_session, owner_user_id=other.id)).count == 0
    with pytest.raises(NotFoundError):
        await source_service.read_source_for_owner(
            source_session, source_id=created.source_id, owner_user_id=other.id
        )

    # No settings/protector are accepted by this call. It is deliberately safe
    # when a historical encryption key has been removed.
    await source_service.delete_source_for_owner(
        source_session, source_id=created.source_id, owner_user_id=owner.id
    )
    assert (await source_session.execute(select(DataSource))).scalars().all() == []
    assert (await source_session.execute(select(SourceCredential))).scalars().all() == []
    assert (await source_session.execute(select(SourceTableScope))).scalars().all() == []


@pytest.mark.asyncio
async def test_saved_source_retest_reuses_protected_credential_and_records_safe_state(source_session):
    owner = await _make_user(source_session, "owner@example.test")
    adapter = _Adapter([InventoryTable("public", "orders")])
    settings = _settings()
    created = await source_service.create_postgresql_source(
        source_session,
        owner_user_id=owner.id,
        request=_request(),
        settings=settings,
        adapter_factory=lambda _settings: adapter,
    )

    result = await source_service.retest_saved_postgresql_source_for_owner(
        source_session,
        source_id=created.source_id,
        owner_user_id=owner.id,
        settings=settings,
        adapter_factory=lambda _settings: adapter,
    )

    assert result.count == 1
    assert result.source.status is SourceStatus.TESTED
    assert result.source.configuration_generation == 0
    assert "not-in-response" not in result.model_dump_json()
    assert adapter.calls == 2


@pytest.mark.asyncio
async def test_saved_scope_update_revalidates_inventory_and_returns_reloaded_scope(
    source_session, monkeypatch
):
    owner = await _make_user(source_session, "owner@example.test")
    settings = _settings()
    created = await source_service.create_postgresql_source(
        source_session,
        owner_user_id=owner.id,
        request=_request(),
        settings=settings,
        adapter_factory=lambda _settings: _Adapter([InventoryTable("public", "orders")]),
    )
    adapter = _Adapter([InventoryTable("public", "customers")])

    async def no_active_work(*_args, **_kwargs):
        # The focused source fixture does not create the separate Stage 3/6
        # durable tables; their cancellation fence is covered at that layer.
        return None

    monkeypatch.setattr(source_service, "_cancel_source_work", no_active_work)
    monkeypatch.setattr(source_service, "_clear_source_transcript_results", no_active_work)
    updated = await source_service.update_saved_postgresql_scope_for_owner(
        source_session,
        source_id=created.source_id,
        owner_user_id=owner.id,
        request=SourceScopeUpdateRequest(
            selected_tables=[{"schema_name": "public", "table_name": "customers"}]
        ),
        settings=settings,
        adapter_factory=lambda _settings: adapter,
    )

    assert updated.configuration_generation == 1
    assert [(table.schema_name, table.table_name) for table in updated.selected_tables] == [
        ("public", "customers")
    ]


@pytest.mark.asyncio
async def test_feature_disabled_fails_before_contacting_adapter():
    request = PostgresTestRequest(
        host="warehouse.example.com",
        database_name="analytics",
        credentials={"username": "reporter", "password": "secret"},
    )
    with pytest.raises(source_service.ConnectorDisabledError):
        await source_service.test_postgresql_source(
            request,
            settings=_settings(enabled=False),
            adapter_factory=lambda _settings: (_ for _ in ()).throw(AssertionError("adapter should not run")),
        )


@pytest.mark.asyncio
async def test_missing_credential_key_fails_before_contacting_adapter():
    request = PostgresTestRequest(
        host="warehouse.example.com",
        database_name="analytics",
        credentials={"username": "reporter", "password": "secret"},
    )
    settings = Settings(
        _env_file=None,
        api_key="stage2b-test-api-key",
        database_url="sqlite+aiosqlite://",
        connectors_enabled=True,
        connector_egress_policy_configured=True,
    )
    with pytest.raises(source_service.CredentialKeyUnavailableError):
        await source_service.test_postgresql_source(
            request,
            settings=settings,
            adapter_factory=lambda _settings: (_ for _ in ()).throw(AssertionError("adapter should not run")),
        )


@pytest.mark.asyncio
async def test_default_adapter_factory_is_resolved_at_request_time_for_disposable_fixtures(monkeypatch):
    adapter = _Adapter([InventoryTable("fixture", "allowed")])
    monkeypatch.setattr(source_service, "_build_adapter", lambda _settings: adapter)
    request = PostgresTestRequest(
        host="fixture.example.test",
        database_name="analytics",
        credentials={"username": "reader", "password": "test-password"},
    )

    response = await source_service.test_postgresql_source(request, settings=_settings())

    assert response.count == 1
    assert response.eligible_tables[0].schema_name == "fixture"
    assert adapter.calls == 1


class _FailingCommitSession:
    def __init__(self):
        self.added = None
        self.rolled_back = False

    def add(self, value):
        self.added = value

    async def commit(self):
        raise IntegrityError(
            "INSERT INTO source_credentials",
            {"ciphertext": "synthetic-ciphertext-marker"},
            RuntimeError("synthetic-driver-marker"),
        )

    async def rollback(self):
        self.rolled_back = True


@pytest.mark.asyncio
async def test_create_masks_persistence_errors_after_rolling_back():
    session = _FailingCommitSession()
    adapter = _Adapter([InventoryTable("public", "orders")])

    with pytest.raises(source_service.SourcePersistenceError) as error:
        await source_service.create_postgresql_source(
            session,
            owner_user_id=uuid.uuid4(),
            request=_request(),
            settings=_settings(),
            adapter_factory=lambda _settings: adapter,
        )

    assert session.added is not None
    assert session.rolled_back is True
    assert "synthetic-ciphertext-marker" not in str(error.value)
    assert "synthetic-driver-marker" not in str(error.value)


def test_stage_2_source_statuses_do_not_claim_catalog_readiness():
    assert "CATALOG_READY" not in {status.value for status in SourceStatus}
