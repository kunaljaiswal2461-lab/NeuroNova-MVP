"""Authenticated encryption for external-source credentials.

This module deliberately uses direct authenticated encryption (Fernet), not
envelope encryption: the deployment-provided Fernet key encrypts the small
credential bundle directly. The key is never stored in the application DB.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import Mapping

from cryptography.fernet import Fernet, InvalidToken
from pydantic import SecretStr

from app.core.config import Settings


CREDENTIAL_ENCRYPTION_FORMAT = "fernet-v1"
_MAX_PREVIOUS_KEYS = 3
_MAX_BUNDLE_BYTES = 96 * 1024


class CredentialProtectionError(Exception):
    """A safe internal category; callers must not expose its cause."""


class CredentialConfigurationError(CredentialProtectionError):
    pass


class CredentialUnavailableError(CredentialProtectionError):
    pass


class InvalidCredentialBundleError(CredentialProtectionError):
    pass


@dataclass(frozen=True)
class CredentialBundle:
    """Secret plaintext held only while a request is being processed."""

    source_id: uuid.UUID
    # This value can be carried through exception context by a caller. Keep
    # every secret field out of the default dataclass repr/str boundary.
    username: str = field(repr=False)
    password: str = field(repr=False)
    custom_ca_pem: str | None = field(default=None, repr=False)

    def serialize(self) -> bytes:
        return json.dumps(
            {
                "source_id": str(self.source_id),
                "username": self.username,
                "password": self.password,
                "custom_ca_pem": self.custom_ca_pem,
            },
            separators=(",", ":"),
        ).encode("utf-8")

    @classmethod
    def deserialize(cls, raw: bytes, *, expected_source_id: uuid.UUID) -> "CredentialBundle":
        if not raw or len(raw) > _MAX_BUNDLE_BYTES:
            raise InvalidCredentialBundleError()
        try:
            payload = json.loads(raw.decode("utf-8"))
            source_id = uuid.UUID(payload["source_id"])
            username = payload["username"]
            password = payload["password"]
            custom_ca_pem = payload.get("custom_ca_pem")
        except (UnicodeDecodeError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            raise InvalidCredentialBundleError() from None

        if (
            source_id != expected_source_id
            or not isinstance(username, str)
            or not username
            or not isinstance(password, str)
            or not password
            or (custom_ca_pem is not None and not isinstance(custom_ca_pem, str))
        ):
            raise InvalidCredentialBundleError()
        return cls(
            source_id=source_id,
            username=username,
            password=password,
            custom_ca_pem=custom_ca_pem,
        )


def _secret_value(value: SecretStr | None) -> str | None:
    return value.get_secret_value() if value is not None else None


class CredentialProtector:
    """Encrypt/decrypt source-bound bundles with an active and prior keyring."""

    def __init__(self, *, active_key_id: str, keyring: Mapping[str, Fernet]):
        self.active_key_id = active_key_id
        self._keyring = dict(keyring)

    @classmethod
    def from_settings(cls, settings: Settings) -> "CredentialProtector":
        active_key_id = settings.connector_credentials_active_key_id.strip()
        active_key = _secret_value(settings.connector_credentials_active_key)
        if not active_key_id or not active_key:
            raise CredentialConfigurationError()

        previous_raw = _secret_value(settings.connector_credentials_previous_keyring)
        previous: dict[str, str] = {}
        if previous_raw:
            try:
                parsed = json.loads(previous_raw)
            except json.JSONDecodeError:
                raise CredentialConfigurationError() from None
            if (
                not isinstance(parsed, dict)
                or len(parsed) > _MAX_PREVIOUS_KEYS
                or any(not isinstance(key, str) or not isinstance(value, str) for key, value in parsed.items())
            ):
                raise CredentialConfigurationError()
            previous = parsed
        if active_key_id in previous or any(not key or len(key) > 64 for key in previous):
            raise CredentialConfigurationError()

        raw_keyring = {active_key_id: active_key, **previous}
        try:
            keyring = {key_id: Fernet(key.encode("ascii")) for key_id, key in raw_keyring.items()}
        except (ValueError, UnicodeEncodeError):
            raise CredentialConfigurationError() from None
        return cls(active_key_id=active_key_id, keyring=keyring)

    def encrypt(self, bundle: CredentialBundle) -> tuple[bytes, str, str]:
        return (
            self._keyring[self.active_key_id].encrypt(bundle.serialize()),
            self.active_key_id,
            CREDENTIAL_ENCRYPTION_FORMAT,
        )

    def decrypt(
        self,
        *,
        source_id: uuid.UUID,
        ciphertext: bytes,
        key_id: str,
        encryption_format: str,
    ) -> CredentialBundle:
        if encryption_format != CREDENTIAL_ENCRYPTION_FORMAT:
            raise CredentialUnavailableError()
        fernet = self._keyring.get(key_id)
        if fernet is None:
            raise CredentialUnavailableError()
        try:
            raw = fernet.decrypt(ciphertext)
        except (InvalidToken, TypeError):
            raise InvalidCredentialBundleError() from None
        return CredentialBundle.deserialize(raw, expected_source_id=source_id)

    def reencrypt(
        self,
        *,
        source_id: uuid.UUID,
        ciphertext: bytes,
        key_id: str,
        encryption_format: str,
    ) -> tuple[bytes, str, str]:
        """Return a new active-key ciphertext without mutating persistence.

        An operator-only command can call this idempotently. If a row already
        uses the active key it is returned unchanged after structural
        validation, making interrupted rotations safe to resume.
        """
        bundle = self.decrypt(
            source_id=source_id,
            ciphertext=ciphertext,
            key_id=key_id,
            encryption_format=encryption_format,
        )
        if key_id == self.active_key_id:
            return ciphertext, key_id, encryption_format
        return self.encrypt(bundle)
