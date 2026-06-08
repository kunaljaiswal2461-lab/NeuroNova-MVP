from __future__ import annotations

import hmac
from typing import Annotated

from fastapi import Depends, Header
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.db.session import get_session
from app.exceptions.custom_exceptions import UnauthorizedError
from agentic_engine.tools.uploader.storage import StorageBackend, build_storage


async def require_api_key(
    x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
    settings: Settings = Depends(get_settings),
) -> None:
    if x_api_key is None or not hmac.compare_digest(x_api_key, settings.api_key):
        raise UnauthorizedError("invalid or missing X-API-Key")


SettingsDep = Annotated[Settings, Depends(get_settings)]
DBSession = Annotated[AsyncSession, Depends(get_session)]
AuthRequired = Depends(require_api_key)


_storage: StorageBackend | None = None


def get_storage(settings: Settings = Depends(get_settings)) -> StorageBackend:
    global _storage
    if _storage is None:
        _storage = build_storage(settings.storage_backend, settings.raw_dir)
    return _storage


StorageDep = Annotated[StorageBackend, Depends(get_storage)]
