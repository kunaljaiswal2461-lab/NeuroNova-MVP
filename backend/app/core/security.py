from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

import bcrypt
from jose import JWTError, jwt

# bcrypt has a hard limit of 72 bytes; truncate before hashing
_BCRYPT_MAX = 72


def hash_password(password: str) -> str:
    salt = bcrypt.gensalt()
    return bcrypt.hashpw(password.encode()[:_BCRYPT_MAX], salt).decode()


def verify_password(plain: str, hashed: str) -> bool:
    return bcrypt.checkpw(plain.encode()[:_BCRYPT_MAX], hashed.encode())


def create_access_token(
    user_id: str, email: str, role: str, secret: str, expire_minutes: int
) -> str:
    expire = datetime.now(timezone.utc) + timedelta(minutes=expire_minutes)
    return jwt.encode(
        {"sub": user_id, "email": email, "role": role, "exp": expire, "type": "access"},
        secret,
        algorithm="HS256",
    )


def create_refresh_token_pair() -> tuple[str, str]:
    """Returns (raw_token, sha256_hash). Only the hash is stored in DB."""
    raw = secrets.token_urlsafe(48)
    token_hash = hashlib.sha256(raw.encode()).hexdigest()
    return raw, token_hash


def decode_access_token(token: str, secret: str) -> dict[str, Any]:
    return jwt.decode(token, secret, algorithms=["HS256"])


def create_one_time_token(sub: str, purpose: str, secret: str, expire_hours: int) -> str:
    """Signed JWT for email verification or password reset."""
    expire = datetime.now(timezone.utc) + timedelta(hours=expire_hours)
    return jwt.encode(
        {"sub": sub, "purpose": purpose, "exp": expire},
        secret,
        algorithm="HS256",
    )


def decode_one_time_token(token: str, expected_purpose: str, secret: str) -> str:
    """Returns the `sub` (user_id string) if valid, raises JWTError otherwise."""
    payload = jwt.decode(token, secret, algorithms=["HS256"])
    if payload.get("purpose") != expected_purpose:
        raise JWTError("token purpose mismatch")
    return payload["sub"]
