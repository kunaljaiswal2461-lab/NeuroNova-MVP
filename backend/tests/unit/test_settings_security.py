"""SPEC-02: JWT secret and APP_ENV must fail closed."""
from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone

import pytest
from jose import JWTError, jwt
from pydantic import ValidationError

from app.core.config import Settings
from app.core.security import decode_access_token

PUBLIC_DEFAULT = "change-me-in-production"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in ("JWT_SECRET_KEY", "APP_ENV", "RENDER", "AUTH_DEV_AUTO_VERIFY"):
        monkeypatch.delenv(name, raising=False)


def _settings(**overrides) -> Settings:
    base = dict(_env_file=None, database_url="sqlite://", api_key="test-key-123")
    base.update(overrides)
    return Settings(**base)


def _forged(secret: str) -> str:
    return jwt.encode(
        {"sub": "victim", "type": "access",
         "exp": datetime.now(timezone.utc) + timedelta(days=1)},
        secret, algorithm="HS256",
    )


# --- exploit tests (must fail on pre-fix code) -------------------------------

def test_default_secret_cannot_mint_valid_tokens():
    """E1. On unfixed code Settings(app_env="prod") builds with the public default, and
    decode_access_token(jwt.encode({..., "type": "access"}, "change-me-in-production"),
    settings.jwt_signing_key) succeeds: any token can be forged."""
    with pytest.raises(ValidationError):
        _settings(app_env="prod")


def test_dev_on_render_refused(monkeypatch):
    """E2. On unfixed code there is no tripwire."""
    monkeypatch.setenv("RENDER", "true")
    with pytest.raises(ValidationError):
        _settings(app_env="dev")


# --- matrix ------------------------------------------------------------------

def test_t1_prod_unset():
    with pytest.raises(ValidationError):
        _settings(app_env="prod")


def test_t2_prod_placeholder():
    with pytest.raises(ValidationError):
        _settings(app_env="prod", jwt_secret_key=PUBLIC_DEFAULT)


def test_t3_prod_31_bytes():
    with pytest.raises(ValidationError):
        _settings(app_env="prod", jwt_secret_key="a" * 31)


def test_t4_prod_valid():
    key = secrets.token_urlsafe(48)
    s = _settings(app_env="prod", jwt_secret_key=key)
    assert s.jwt_signing_key == key
    assert s.jwt_secret_ephemeral is False


def test_t5_staging_unset():
    with pytest.raises(ValidationError):
        _settings(app_env="staging")


def test_t6_dev_unset_gets_random_per_process_key():
    a, b = _settings(app_env="dev"), _settings(app_env="dev")
    assert len(a.jwt_signing_key) >= 64
    assert a.jwt_secret_ephemeral is True
    assert a.jwt_signing_key != b.jwt_signing_key


def test_t7_dev_placeholder():
    with pytest.raises(ValidationError):
        _settings(app_env="dev", jwt_secret_key=PUBLIC_DEFAULT)


def test_t8_dev_existing_test_convention():
    s = _settings(app_env="dev", jwt_secret_key="artifact-auth-test-secret")
    assert s.jwt_signing_key == "artifact-auth-test-secret"
    assert s.jwt_secret_ephemeral is False


def test_t9_error_never_contains_secret():
    with pytest.raises(ValidationError) as exc:
        _settings(app_env="prod", jwt_secret_key="leakcheck-short")
    assert "leakcheck-short" not in str(exc.value)


def test_t10_app_env_has_no_default():
    with pytest.raises(ValidationError):
        _settings(jwt_secret_key=secrets.token_urlsafe(48))


def test_t11_dev_on_render(monkeypatch):
    monkeypatch.setenv("RENDER", "true")
    with pytest.raises(ValidationError):
        _settings(app_env="dev", jwt_secret_key=secrets.token_urlsafe(48))


def test_t12_prod_on_render(monkeypatch):
    monkeypatch.setenv("RENDER", "true")
    _settings(app_env="prod", jwt_secret_key=secrets.token_urlsafe(48))


def test_t13_repr_hides_secret():
    key = secrets.token_urlsafe(48)
    s = _settings(app_env="prod", jwt_secret_key=key)
    assert key not in repr(s)
    assert key not in str(s)


def test_t14_default_secret_token_rejected():
    s = _settings(app_env="prod", jwt_secret_key=secrets.token_urlsafe(48))
    with pytest.raises(JWTError):
        decode_access_token(_forged(PUBLIC_DEFAULT), s.jwt_signing_key)
