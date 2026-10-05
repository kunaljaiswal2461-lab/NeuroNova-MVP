from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import DefaultClause, event, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import Settings
from app.core.security import hash_password
from app.db.models.user import RefreshToken, User, UserRole
from app.core.security import create_one_time_token
from app.exceptions.custom_exceptions import (
    EmailDeliveryUnavailableError,
    PasswordResetRequiredError,
    UnauthorizedError,
)
from app.services import auth_service, email_service

EMAIL = "priya@acme-logistics.com"


def _settings(**overrides) -> Settings:
    values = dict(
        api_key="test-api-key-12345",
        database_url="postgresql+asyncpg://x:x@localhost/x",
        openai_api_key="",
        smtp_host="",
        resend_api_key="",
    )
    values.update(overrides)
    return Settings(**values)


@pytest.fixture
def settings() -> Settings:
    return _settings()


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    tables = [User.__table__, RefreshToken.__table__]

    def _create(sync_conn):
        # PostgreSQL-only defaults (uuid_generate_v4(), now()) are not valid SQLite DDL.
        # Ids are set explicitly in tests; timestamps fall back to CURRENT_TIMESTAMP.
        saved = {}
        for t in tables:
            for col in t.columns:
                default = col.server_default
                sql = str(getattr(default, "arg", "")) if default is not None else ""
                if "uuid_generate_v4" in sql:
                    saved[col] = default
                    col.server_default = None
                elif sql == "now()":
                    saved[col] = default
                    col.server_default = DefaultClause(text("CURRENT_TIMESTAMP"))
        try:
            User.metadata.create_all(sync_conn, tables=tables)
        finally:
            for col, default in saved.items():
                col.server_default = default

    async with engine.begin() as conn:
        await conn.run_sync(_create)

    # Production code inserts rows without ids (PostgreSQL generates them).
    def _assign_id(_mapper, _connection, target):
        if target.id is None:
            target.id = uuid.uuid4()

    for model in (User, RefreshToken):
        event.listen(model, "before_insert", _assign_id)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as s:
            yield s
    finally:
        for model in (User, RefreshToken):
            event.remove(model, "before_insert", _assign_id)
        await engine.dispose()


async def _seed(session, *, verified: bool, proven: bool = False,
                password: str | None = "Mall0ry!pass",
                google_id: str | None = None, active: bool = True,
                name: str = "Priya", avatar_url: str | None = None) -> User:
    user = User(
        id=uuid.uuid4(), email=EMAIL, name=name,
        hashed_password=hash_password(password) if password else None,
        is_active=active, is_verified=verified, role=UserRole.USER,
        google_id=google_id, avatar_url=avatar_url,
        password_proven_at=datetime.now(timezone.utc) if proven else None,
    )
    session.add(user)
    await session.commit()
    return user


async def _google(session, settings, **kw):
    args = dict(google_id="G-PRIYA", email=EMAIL, email_verified=True,
                name="Priya Sharma", avatar_url=None)
    args.update(kw)
    return await auth_service.get_or_create_google_user(session, settings, **args)


async def _user_count(session) -> int:
    return len((await session.scalars(select(User))).all())


async def test_preregistered_password_does_not_survive_google_link(session, settings):
    attacker_row = await _seed(session, verified=False)
    session.add(RefreshToken(
        user_id=attacker_row.id, token_hash="a" * 64,
        expires_at=datetime.now(timezone.utc) + timedelta(days=1),
    ))
    await session.commit()

    _, _, user = await _google(session, settings)

    with pytest.raises(UnauthorizedError):
        await auth_service.login_user(session, settings, EMAIL, "Mall0ry!pass")
    assert user.id == attacker_row.id
    assert user.is_verified is True
    assert user.hashed_password is None
    assert user.name == "Priya Sharma"
    assert user.google_id == "G-PRIYA"


async def test_reclaim_revokes_refresh_tokens(session, settings):
    row = await _seed(session, verified=False)
    token = RefreshToken(
        user_id=row.id, token_hash="b" * 64,
        expires_at=datetime.now(timezone.utc) + timedelta(days=1),
    )
    session.add(token)
    await session.commit()

    await _google(session, settings)
    await session.refresh(token)
    assert token.revoked is True


async def test_known_google_id_logs_in_unchanged(session, settings):
    row = await _seed(session, verified=True, google_id="G-PRIYA", name="Original")
    _, _, user = await _google(session, settings)
    assert user.id == row.id
    assert user.name == "Original"
    assert user.hashed_password is not None


async def test_new_google_user_created_verified_without_password(session, settings):
    _, _, user = await _google(session, settings)
    assert user.is_verified is True
    assert user.hashed_password is None
    assert user.google_id == "G-PRIYA"


async def test_verified_password_user_links_and_keeps_password(session, settings):
    await _seed(session, verified=True, proven=True, password="Real!pass1")
    _, _, user = await _google(session, settings, avatar_url="http://a/p.png")
    assert user.google_id == "G-PRIYA"
    assert user.name == "Priya"
    assert user.avatar_url == "http://a/p.png"
    await auth_service.login_user(session, settings, EMAIL, "Real!pass1")


async def test_link_conflict_rejected(session, settings):
    row = await _seed(session, verified=True, google_id="G-OTHER")
    with pytest.raises(UnauthorizedError):
        await _google(session, settings)
    await session.refresh(row)
    assert row.google_id == "G-OTHER"


async def test_unverified_google_email_rejected(session, settings):
    with pytest.raises(UnauthorizedError):
        await _google(session, settings, email_verified=False)
    assert await _user_count(session) == 0


async def test_string_true_email_verified_rejected(session, settings):
    with pytest.raises(UnauthorizedError):
        await _google(session, settings, email_verified="true")
    assert await _user_count(session) == 0


async def test_missing_email_verified_rejected(session, settings):
    with pytest.raises(UnauthorizedError):
        await _google(session, settings, email_verified=None)
    assert await _user_count(session) == 0


async def test_missing_email_rejected(session, settings):
    with pytest.raises(UnauthorizedError):
        await _google(session, settings, email=None)
    assert await _user_count(session) == 0


async def test_inactive_user_rejected(session, settings):
    await _seed(session, verified=True, active=False)
    with pytest.raises(UnauthorizedError, match="account disabled"):
        await _google(session, settings)


def test_dev_auto_verify_refused_outside_dev():
    with pytest.raises(ValueError, match="AUTH_DEV_AUTO_VERIFY"):
        _settings(auth_dev_auto_verify=True, app_env="prod")


@pytest.fixture
def sent(monkeypatch):
    calls = []

    async def _record(*args, **kwargs):
        calls.append(args)

    monkeypatch.setattr(email_service, "send_verification_email", _record)
    monkeypatch.setattr(email_service, "send_password_reset_email", _record)
    return calls


async def test_register_unverified_without_opt_in(session, sent):
    # M12: email provider configured, opt-in off.
    s = _settings(app_env="dev", auth_dev_auto_verify=False, resend_api_key="re_test")
    user = await auth_service.register_user(session, s, EMAIL, "Priya", "Pass!word1")
    assert user.is_verified is False
    assert user.password_proven_at is None
    assert len(sent) == 1


async def test_register_auto_verified_with_opt_in(session, sent):
    # M13: dev opt-in verifies AND proves the password, so local login works.
    s = _settings(app_env="dev", auth_dev_auto_verify=True)
    user = await auth_service.register_user(session, s, EMAIL, "Priya", "Pass!word1")
    assert user.is_verified is True
    assert user.password_proven_at is not None
    await auth_service.login_user(session, s, EMAIL, "Pass!word1")


async def test_register_fails_loudly_in_dev_without_delivery(session, sent):
    # M21
    s = _settings(app_env="dev", auth_dev_auto_verify=False)
    with pytest.raises(EmailDeliveryUnavailableError, match="AUTH_DEV_AUTO_VERIFY"):
        await auth_service.register_user(session, s, EMAIL, "Priya", "Pass!word1")
    assert await _user_count(session) == 0


async def test_register_fails_generically_in_prod_without_delivery(session, sent):
    # M22
    s = _settings(app_env="prod")
    with pytest.raises(EmailDeliveryUnavailableError) as exc:
        await auth_service.register_user(session, s, EMAIL, "Priya", "Pass!word1")
    assert "AUTH_DEV_AUTO_VERIFY" not in str(exc.value)
    assert await _user_count(session) == 0


async def test_forgot_password_fails_without_delivery_for_any_email(session, sent):
    # M23: same error whether or not the email exists.
    await _seed(session, verified=True)
    s = _settings()
    for email in (EMAIL, "nobody@example.com"):
        with pytest.raises(EmailDeliveryUnavailableError):
            await auth_service.forgot_password(session, s, email)


async def test_legacy_verified_row_with_unproven_password_is_reclaimed(session, settings):
    # M14 / A1.2: a row auto-verified before Rule 4 keeps the squatter's password.
    await _seed(session, verified=True)
    _, _, user = await _google(session, settings)
    assert user.hashed_password is None
    assert user.name == "Priya Sharma"
    with pytest.raises(UnauthorizedError):
        await auth_service.login_user(session, settings, EMAIL, "Mall0ry!pass")


async def test_google_only_row_is_linked_not_reclaimed(session, settings):
    # M15
    await _seed(session, verified=True, password=None, name="Original")
    _, _, user = await _google(session, settings)
    assert user.google_id == "G-PRIYA"
    assert user.name == "Original"


async def test_login_with_unproven_password_requires_reset(session, settings):
    # M16
    await _seed(session, verified=True)
    with pytest.raises(PasswordResetRequiredError) as exc:
        await auth_service.login_user(session, settings, EMAIL, "Mall0ry!pass")
    assert exc.value.code == "password_reset_required"


async def test_wrong_password_gives_no_reset_hint(session, settings):
    # M17
    await _seed(session, verified=True)
    with pytest.raises(UnauthorizedError, match="invalid credentials") as exc:
        await auth_service.login_user(session, settings, EMAIL, "wrong")
    assert not isinstance(exc.value, PasswordResetRequiredError)


async def test_reset_password_proves_password(session, settings):
    # M18
    row = await _seed(session, verified=True)
    token = create_one_time_token(str(row.id), "password_reset", settings.jwt_secret_key, expire_hours=1)
    user = await auth_service.reset_password(session, settings, token, "Brand!new1")
    assert user.password_proven_at is not None
    await auth_service.login_user(session, settings, EMAIL, "Brand!new1")
    with pytest.raises(UnauthorizedError):
        await auth_service.login_user(session, settings, EMAIL, "Mall0ry!pass")


async def test_verify_email_transition_proves_password(session, settings):
    # M19
    row = await _seed(session, verified=False)
    token = create_one_time_token(str(row.id), "email_verification", settings.jwt_secret_key, expire_hours=24)
    user = await auth_service.verify_email(session, settings, token)
    assert user.is_verified is True
    assert user.password_proven_at is not None


async def test_verify_email_cannot_bless_legacy_password(session, settings):
    # M20
    row = await _seed(session, verified=True)
    token = create_one_time_token(str(row.id), "email_verification", settings.jwt_secret_key, expire_hours=24)
    user = await auth_service.verify_email(session, settings, token)
    assert user.password_proven_at is None
