from __future__ import annotations

import json
import uuid
from base64 import urlsafe_b64decode, urlsafe_b64encode

import pytest
from cryptography.fernet import Fernet

from app.core.config import Settings
from app.core.connector_credentials import (
    CredentialBundle,
    CredentialConfigurationError,
    CredentialProtector,
    CredentialUnavailableError,
    InvalidCredentialBundleError,
)


def _settings(*, active_key: str, active_id: str = "current", previous: dict[str, str] | None = None) -> Settings:
    return Settings(
        _env_file=None,
        api_key="stage2b-test-api-key",
        database_url="sqlite+aiosqlite://",
        connectors_enabled=True,
        connector_egress_policy_configured=True,
        connector_credentials_active_key_id=active_id,
        connector_credentials_active_key=active_key,
        connector_credentials_previous_keyring=json.dumps(previous or {}),
    )


def test_credentials_are_authenticated_and_bound_to_the_source_identity():
    protector = CredentialProtector.from_settings(_settings(active_key=Fernet.generate_key().decode()))
    source_id = uuid.uuid4()
    ciphertext, key_id, encryption_format = protector.encrypt(
        CredentialBundle(source_id, "database-user", "database-password", "CA PEM")
    )

    assert b"database-password" not in ciphertext
    assert key_id == "current"
    decrypted = protector.decrypt(
        source_id=source_id,
        ciphertext=ciphertext,
        key_id=key_id,
        encryption_format=encryption_format,
    )
    assert decrypted.username == "database-user"
    assert decrypted.password == "database-password"
    with pytest.raises(InvalidCredentialBundleError):
        protector.decrypt(
            source_id=uuid.uuid4(),
            ciphertext=ciphertext,
            key_id=key_id,
            encryption_format=encryption_format,
        )


def test_previous_key_decrypts_and_reencrypts_to_active_key():
    old_key = Fernet.generate_key().decode()
    old_protector = CredentialProtector.from_settings(_settings(active_key=old_key, active_id="old"))
    source_id = uuid.uuid4()
    ciphertext, key_id, encryption_format = old_protector.encrypt(
        CredentialBundle(source_id, "user", "password")
    )

    new_protector = CredentialProtector.from_settings(
        _settings(
            active_key=Fernet.generate_key().decode(),
            active_id="new",
            previous={"old": old_key},
        )
    )
    rewritten, rewritten_id, rewritten_format = new_protector.reencrypt(
        source_id=source_id,
        ciphertext=ciphertext,
        key_id=key_id,
        encryption_format=encryption_format,
    )
    assert rewritten_id == "new"
    assert rewritten_format == encryption_format
    assert new_protector.decrypt(
        source_id=source_id,
        ciphertext=rewritten,
        key_id=rewritten_id,
        encryption_format=rewritten_format,
    ).password == "password"


@pytest.mark.parametrize("previous", [{"current": Fernet.generate_key().decode()}, {str(index): Fernet.generate_key().decode() for index in range(4)}])
def test_invalid_keyring_configuration_is_rejected(previous):
    with pytest.raises(CredentialConfigurationError):
        CredentialProtector.from_settings(
            _settings(active_key=Fernet.generate_key().decode(), previous=previous)
        )


def test_unknown_key_id_and_tampered_ciphertext_fail_closed():
    protector = CredentialProtector.from_settings(_settings(active_key=Fernet.generate_key().decode()))
    source_id = uuid.uuid4()
    ciphertext, _key_id, encryption_format = protector.encrypt(
        CredentialBundle(source_id, "user", "password")
    )
    with pytest.raises(CredentialUnavailableError):
        protector.decrypt(
            source_id=source_id,
            ciphertext=ciphertext,
            key_id="removed-key",
            encryption_format=encryption_format,
        )
    # Fernet tokens are URL-safe base64; alter decoded authenticated bytes,
    # rather than a padding character that may decode to the same token.
    tampered_raw = bytearray(urlsafe_b64decode(ciphertext))
    tampered_raw[-10] ^= 1
    tampered = urlsafe_b64encode(bytes(tampered_raw))
    with pytest.raises(InvalidCredentialBundleError):
        protector.decrypt(
            source_id=source_id,
            ciphertext=tampered,
            key_id="current",
            encryption_format=encryption_format,
        )
