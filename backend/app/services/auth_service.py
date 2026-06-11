from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timedelta, timezone

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.security import (
    create_access_token,
    create_one_time_token,
    create_refresh_token_pair,
    decode_one_time_token,
    hash_password,
    verify_password,
)
from app.db.models.user import RefreshToken, User, UserRole
from app.exceptions.custom_exceptions import (
    ConflictError,
    NotFoundError,
    UnauthorizedError,
    ValidationFailure,
)
from app.services import email_service

log = structlog.get_logger()


async def register_user(
    db: AsyncSession, settings: Settings, email: str, name: str, password: str
) -> User:
    existing = await db.scalar(select(User).where(User.email == email.lower()))
    if existing:
        raise ConflictError("email already registered")

    # In dev with no SMTP configured, sending the verification email is a no-op,
    # which would leave the user permanently unable to log in. Auto-verify instead
    # and log the would-be verification URL so the verify flow can still be tested.
    dev_auto_verify = settings.app_env == "dev" and not settings.smtp_host

    user = User(
        email=email.lower(),
        name=name,
        hashed_password=hash_password(password),
        is_active=True,
        is_verified=dev_auto_verify,
        role=UserRole.USER,
    )
    db.add(user)
    await db.flush()  # assign user.id before sending email

    token = create_one_time_token(
        str(user.id), "email_verification", settings.jwt_secret_key, expire_hours=24
    )
    if dev_auto_verify:
        verify_url = f"{settings.frontend_url}/auth/verify-email?token={token}"
        log.warning(
            "auth.register.dev_auto_verified",
            user_id=str(user.id),
            email=user.email,
            verify_url=verify_url,
        )
    else:
        await email_service.send_verification_email(settings, user.email, user.name, token)

    await db.commit()
    await db.refresh(user)
    log.info("auth.register", user_id=str(user.id), email=user.email, auto_verified=dev_auto_verify)
    return user


async def login_user(
    db: AsyncSession, settings: Settings, email: str, password: str
) -> tuple[str, str, User]:
    user = await db.scalar(select(User).where(User.email == email.lower()))
    if not user or not user.hashed_password or not verify_password(password, user.hashed_password):
        raise UnauthorizedError("invalid credentials")
    if not user.is_active:
        raise UnauthorizedError("account disabled")
    if not user.is_verified:
        raise UnauthorizedError("email not verified — check your inbox")

    access_token = create_access_token(
        str(user.id), user.email, user.role.value,
        settings.jwt_secret_key, settings.access_token_expire_minutes,
    )
    raw_refresh, token_hash = create_refresh_token_pair()
    expires_at = datetime.now(timezone.utc) + timedelta(days=settings.refresh_token_expire_days)
    db.add(RefreshToken(user_id=user.id, token_hash=token_hash, expires_at=expires_at))
    await db.commit()

    log.info("auth.login", user_id=str(user.id))
    return access_token, raw_refresh, user


async def logout_user(db: AsyncSession, raw_refresh_token: str) -> None:
    token_hash = hashlib.sha256(raw_refresh_token.encode()).hexdigest()
    rt = await db.scalar(select(RefreshToken).where(RefreshToken.token_hash == token_hash))
    if rt:
        rt.revoked = True
        await db.commit()


async def refresh_access_token(
    db: AsyncSession, settings: Settings, raw_refresh_token: str
) -> str:
    token_hash = hashlib.sha256(raw_refresh_token.encode()).hexdigest()
    rt = await db.scalar(select(RefreshToken).where(RefreshToken.token_hash == token_hash))
    if not rt or rt.revoked or rt.expires_at.replace(tzinfo=timezone.utc) < datetime.now(timezone.utc):
        raise UnauthorizedError("invalid or expired refresh token")

    user = await db.get(User, rt.user_id)
    if not user or not user.is_active:
        raise UnauthorizedError("account not found or disabled")

    return create_access_token(
        str(user.id), user.email, user.role.value,
        settings.jwt_secret_key, settings.access_token_expire_minutes,
    )


async def verify_email(db: AsyncSession, settings: Settings, token: str) -> User:
    from jose import JWTError
    try:
        user_id = decode_one_time_token(token, "email_verification", settings.jwt_secret_key)
    except JWTError:
        raise ValidationFailure("invalid or expired verification token")

    user = await db.get(User, uuid.UUID(user_id))
    if not user:
        raise NotFoundError("user not found")
    if not user.is_verified:
        user.is_verified = True
        await db.commit()
        await db.refresh(user)
    return user


async def forgot_password(db: AsyncSession, settings: Settings, email: str) -> None:
    user = await db.scalar(select(User).where(User.email == email.lower()))
    if not user:
        return  # don't leak whether the email exists
    token = create_one_time_token(
        str(user.id), "password_reset", settings.jwt_secret_key, expire_hours=1
    )
    await email_service.send_password_reset_email(settings, user.email, user.name, token)


async def reset_password(
    db: AsyncSession, settings: Settings, token: str, new_password: str
) -> User:
    from jose import JWTError
    try:
        user_id = decode_one_time_token(token, "password_reset", settings.jwt_secret_key)
    except JWTError:
        raise ValidationFailure("invalid or expired reset token")

    user = await db.get(User, uuid.UUID(user_id))
    if not user:
        raise NotFoundError("user not found")

    user.hashed_password = hash_password(new_password)

    # revoke all existing refresh tokens after password reset
    tokens = await db.scalars(
        select(RefreshToken).where(
            RefreshToken.user_id == user.id, RefreshToken.revoked == False  # noqa: E712
        )
    )
    for rt in tokens:
        rt.revoked = True

    await db.commit()
    await db.refresh(user)
    log.info("auth.password_reset", user_id=str(user.id))
    return user


async def get_or_create_google_user(
    db: AsyncSession,
    settings: Settings,
    google_id: str,
    email: str,
    name: str,
    avatar_url: str | None,
) -> tuple[str, str, User]:
    # 1. Try by google_id
    user = await db.scalar(select(User).where(User.google_id == google_id))
    if not user:
        # 2. Try by email (link existing account)
        user = await db.scalar(select(User).where(User.email == email.lower()))
        if user:
            user.google_id = google_id
            if avatar_url and not user.avatar_url:
                user.avatar_url = avatar_url
            if not user.is_verified:
                user.is_verified = True  # Google confirms the email
        else:
            # 3. New user via Google
            user = User(
                email=email.lower(),
                name=name,
                hashed_password=None,
                is_active=True,
                is_verified=True,
                role=UserRole.USER,
                google_id=google_id,
                avatar_url=avatar_url,
            )
            db.add(user)
            await db.flush()

    if not user.is_active:
        raise UnauthorizedError("account disabled")

    access_token = create_access_token(
        str(user.id), user.email, user.role.value,
        settings.jwt_secret_key, settings.access_token_expire_minutes,
    )
    raw_refresh, token_hash = create_refresh_token_pair()
    expires_at = datetime.now(timezone.utc) + timedelta(days=settings.refresh_token_expire_days)
    db.add(RefreshToken(user_id=user.id, token_hash=token_hash, expires_at=expires_at))

    await db.commit()
    await db.refresh(user)
    log.info("auth.google", user_id=str(user.id), email=user.email)
    return access_token, raw_refresh, user
