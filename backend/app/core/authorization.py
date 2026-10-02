"""Trusted actor scopes used by public resource authorization boundaries."""
from __future__ import annotations

import uuid
from dataclasses import dataclass


@dataclass(frozen=True)
class DatasetAccess:
    """Identity scope for public dataset and artifact requests.

    A JWT identifies one user. A legacy global API key identifies no user, so
    it is restricted to records that are genuinely unowned. ``None`` is never
    interpreted as unrestricted access at this boundary.
    """

    user_id: uuid.UUID | None
    legacy_api_key: bool

    def __post_init__(self) -> None:
        if self.legacy_api_key and self.user_id is not None:
            raise ValueError("legacy API-key access cannot carry a user id")
        if not self.legacy_api_key and self.user_id is None:
            raise ValueError("user dataset access requires a user id")

    @classmethod
    def for_user(cls, user_id: uuid.UUID) -> "DatasetAccess":
        return cls(user_id=user_id, legacy_api_key=False)

    @classmethod
    def for_legacy_api_key(cls) -> "DatasetAccess":
        return cls(user_id=None, legacy_api_key=True)
