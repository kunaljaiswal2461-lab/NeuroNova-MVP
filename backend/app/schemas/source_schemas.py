"""Wire contracts for PostgreSQL source onboarding.

Request credentials use ``SecretStr`` so accidental repr/logging never emits
the underlying value. Public response models intentionally have no credential
or ciphertext fields.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

from app.db.models.source import SourceStatus, SourceType, TLSMode


def _bounded_text(value: str, *, maximum: int, field_name: str) -> str:
    trimmed = value.strip()
    if not trimmed or len(trimmed) > maximum:
        raise ValueError(f"{field_name} is invalid")
    return trimmed


class PostgresTLSRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["verify-full"] = "verify-full"
    custom_ca_pem: SecretStr | None = None

    @field_validator("custom_ca_pem")
    @classmethod
    def validate_ca_size(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None:
            custom_ca_pem = value.get_secret_value()
            # An empty field must behave as absent at the HTTP boundary. If it
            # were accepted, the adapter would use system trust while the
            # saved source incorrectly claimed a custom CA was configured.
            if not custom_ca_pem.strip():
                raise ValueError("custom CA bundle is invalid")
            if len(custom_ca_pem.encode("utf-8")) > 64 * 1024:
                raise ValueError("custom CA bundle is too large")
        return value


class PostgresCredentialsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: SecretStr
    password: SecretStr

    @field_validator("username", "password")
    @classmethod
    def validate_secret(cls, value: SecretStr) -> SecretStr:
        secret = value.get_secret_value()
        if not secret or len(secret.encode("utf-8")) > 4096:
            raise ValueError("credential value is invalid")
        return value


class PostgresConnectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    host: str = Field(min_length=1, max_length=253)
    port: Literal[5432] = 5432
    database_name: str = Field(min_length=1, max_length=63)
    tls: PostgresTLSRequest = Field(default_factory=PostgresTLSRequest)
    credentials: PostgresCredentialsRequest

    @field_validator("host")
    @classmethod
    def validate_host_text(cls, value: str) -> str:
        return _bounded_text(value, maximum=253, field_name="host")

    @field_validator("database_name")
    @classmethod
    def validate_database_name(cls, value: str) -> str:
        # A plain database name, never a URI, query-string, or connection
        # option carrier. PostgreSQL later performs the definitive lookup.
        result = _bounded_text(value, maximum=63, field_name="database name")
        if any(character in result for character in "/\\@?:#[]%"):
            raise ValueError("database name is invalid")
        return result


class TableScopeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_name: str = Field(min_length=1, max_length=63)
    table_name: str = Field(min_length=1, max_length=63)

    @field_validator("schema_name", "table_name")
    @classmethod
    def validate_identifier(cls, value: str) -> str:
        result = _bounded_text(value, maximum=63, field_name="table scope")
        if "\x00" in result:
            raise ValueError("table scope is invalid")
        return result


class PostgresTestRequest(PostgresConnectionRequest):
    pass


class CreateSourceRequest(PostgresConnectionRequest):
    display_name: str = Field(min_length=1, max_length=128)
    selected_tables: list[TableScopeRequest] = Field(min_length=1, max_length=1000)
    profiling_enabled: Literal[False] = False

    @field_validator("display_name")
    @classmethod
    def validate_display_name(cls, value: str) -> str:
        return _bounded_text(value, maximum=128, field_name="display name")

    @model_validator(mode="after")
    def reject_duplicate_scopes(self) -> "CreateSourceRequest":
        seen = {(scope.schema_name, scope.table_name) for scope in self.selected_tables}
        if len(seen) != len(self.selected_tables):
            raise ValueError("selected tables must be unique")
        return self


class InventoryTableResponse(BaseModel):
    schema_name: str
    table_name: str


class PostgresTestResponse(BaseModel):
    status: Literal["reachable"] = "reachable"
    eligible_tables: list[InventoryTableResponse]
    count: int


class SourceSummaryResponse(BaseModel):
    source_id: uuid.UUID
    display_name: str
    source_type: SourceType
    status: SourceStatus
    host: str
    port: int
    database_name: str
    tls_mode: TLSMode
    has_custom_ca: bool
    profiling_enabled: bool
    scope_count: int
    last_tested_at: datetime | None
    created_at: datetime
    updated_at: datetime


class SourceDetailResponse(SourceSummaryResponse):
    selected_tables: list[InventoryTableResponse]


class SourceListResponse(BaseModel):
    items: list[SourceSummaryResponse]
    count: int
