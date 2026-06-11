from __future__ import annotations

import hmac
import uuid
from typing import Annotated

from fastapi import Depends, Header
from jose import JWTError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.security import decode_access_token
from app.db.session import get_session
from app.exceptions.custom_exceptions import UnauthorizedError
from agentic_engine.tools.uploader.storage import StorageBackend, build_storage


# ── legacy API-key auth (kept for backward compatibility) ────────────────────

async def require_api_key(
    x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
    settings: Settings = Depends(get_settings),
) -> None:
    if x_api_key is None or not hmac.compare_digest(x_api_key, settings.api_key):
        raise UnauthorizedError("invalid or missing X-API-Key")


# ── JWT user resolution ───────────────────────────────────────────────────────

async def _resolve_user_from_jwt(
    authorization: Annotated[str | None, Header()] = None,
    settings: Settings = Depends(get_settings),
    db: AsyncSession = Depends(get_session),
):
    """Extract and validate a Bearer JWT; return the User row or raise 401."""
    from app.db.models.user import User  # local import avoids circular at module load

    if not authorization or not authorization.startswith("Bearer "):
        raise UnauthorizedError("missing or malformed Authorization header")
    token = authorization[len("Bearer "):]
    try:
        payload = decode_access_token(token, settings.jwt_secret_key)
    except JWTError:
        raise UnauthorizedError("invalid or expired access token")
    if payload.get("type") != "access":
        raise UnauthorizedError("token type mismatch")

    user = await db.get(User, uuid.UUID(payload["sub"]))
    if not user or not user.is_active:
        raise UnauthorizedError("user not found or disabled")
    return user


async def get_current_user(
    user=Depends(_resolve_user_from_jwt),
):
    """Require a valid JWT. Returns the authenticated User."""
    return user


async def get_auth_context(
    authorization: Annotated[str | None, Header()] = None,
    x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
    settings: Settings = Depends(get_settings),
    db: AsyncSession = Depends(get_session),
):
    """Accept either a JWT Bearer token (returns User) or legacy X-API-Key (returns None).

    Raises 401 if neither is present or valid.
    """
    from app.db.models.user import User

    if authorization and authorization.startswith("Bearer "):
        token = authorization[len("Bearer "):]
        try:
            payload = decode_access_token(token, settings.jwt_secret_key)
        except JWTError:
            raise UnauthorizedError("invalid or expired access token")
        if payload.get("type") != "access":
            raise UnauthorizedError("token type mismatch")
        user = await db.get(User, uuid.UUID(payload["sub"]))
        if not user or not user.is_active:
            raise UnauthorizedError("user not found or disabled")
        return user  # authenticated as a real user

    if x_api_key is not None:
        if hmac.compare_digest(x_api_key, settings.api_key):
            return None  # authorized via legacy API key, no user identity
        raise UnauthorizedError("invalid X-API-Key")

    raise UnauthorizedError("authentication required — provide Bearer token or X-API-Key")


# ── typed aliases ─────────────────────────────────────────────────────────────

SettingsDep = Annotated[Settings, Depends(get_settings)]
DBSession = Annotated[AsyncSession, Depends(get_session)]
AuthRequired = Depends(require_api_key)           # legacy — kept so existing usages compile
AuthContext = Depends(get_auth_context)           # new combined auth


# ── storage ───────────────────────────────────────────────────────────────────

_storage: StorageBackend | None = None


def get_storage(settings: Settings = Depends(get_settings)) -> StorageBackend:
    global _storage
    if _storage is None:
        _storage = build_storage(settings.storage_backend, settings.raw_dir)
    return _storage


StorageDep = Annotated[StorageBackend, Depends(get_storage)]
