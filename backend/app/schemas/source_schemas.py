"""Wire contracts for PostgreSQL source onboarding.

Request credentials use ``SecretStr`` so accidental repr/logging never emits
the underlying value. Public response models intentionally have no credential
or ciphertext fields.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import StrictBool, BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

from app.db.models.source import SourceStatus, SourceType, TLSMode
from app.db.models.catalog import (
    CatalogForeignKeyTargetStatus,
    CatalogKeyKind,
    CatalogSyncRunStatus,
)
from app.db.models.source_profile import (
    SourceProfileFindingSeverity,
    SourceProfileFindingType,
    SourceProfileObservationStatus,
    SourceProfilePopulation,
    SourceProfileRunStatus,
    SourceProfileSamplingMethod,
)
from app.services.postgres_query_plan import PostgresFilter


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


class SavedSourceCredentialReplacementRequest(BaseModel):
    """Credentials may rotate, but a saved source endpoint is immutable.

    The endpoint/database identity is intentionally absent.  Moving a source
    is a new onboarding decision, rather than a way to silently redirect an
    approved connector to a different network destination.
    """

    model_config = ConfigDict(extra="forbid")

    credentials: PostgresCredentialsRequest
    tls: PostgresTLSRequest = Field(default_factory=PostgresTLSRequest)


class SourceScopeUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    selected_tables: list[TableScopeRequest] = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def reject_duplicate_scopes(self) -> "SourceScopeUpdateRequest":
        pairs = {(scope.schema_name, scope.table_name) for scope in self.selected_tables}
        if len(pairs) != len(self.selected_tables):
            raise ValueError("selected tables must be unique")
        return self


class SourceCatalogRefreshPolicyRequest(BaseModel):
    """A bounded, opt-in schedule for structural metadata only."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool
    interval_minutes: int | None = Field(default=None, ge=15, le=10_080)

    @model_validator(mode="after")
    def validate_schedule_shape(self) -> "SourceCatalogRefreshPolicyRequest":
        if self.enabled and self.interval_minutes is None:
            raise ValueError("an interval is required when scheduled refresh is enabled")
        if not self.enabled and self.interval_minutes is not None:
            raise ValueError("an interval is only valid when scheduled refresh is enabled")
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
    configuration_generation: int = 0
    scope_count: int
    last_tested_at: datetime | None
    catalog_refresh_enabled: bool = False
    catalog_refresh_interval_minutes: int | None = None
    catalog_refresh_next_due_at: datetime | None = None
    catalog_last_success_at: datetime | None = None
    catalog_last_failure_code: str | None = None
    created_at: datetime
    updated_at: datetime


class SourceDetailResponse(SourceSummaryResponse):
    selected_tables: list[InventoryTableResponse]


class SavedSourceTestResponse(PostgresTestResponse):
    source: SourceDetailResponse


class SourceCatalogRefreshPolicyResponse(BaseModel):
    source_id: uuid.UUID
    enabled: bool
    interval_minutes: int | None
    next_due_at: datetime | None


class SourceListResponse(BaseModel):
    items: list[SourceSummaryResponse]
    count: int


class CatalogSyncRunResponse(BaseModel):
    """Safe public subset of durable worker state; never expose lease tokens."""

    run_id: uuid.UUID
    state: CatalogSyncRunStatus
    requested_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    attempt_count: int
    next_attempt_at: datetime | None
    failure_code: str | None


class CatalogAvailabilityResponse(BaseModel):
    source_id: uuid.UUID
    latest_run: CatalogSyncRunResponse | None
    current_snapshot_id: uuid.UUID | None
    current_snapshot_published_at: datetime | None
    older_snapshot_available: bool


class CatalogAssetReferenceResponse(BaseModel):
    """Structured identity; clients must not parse a dotted display name."""

    asset_id: uuid.UUID
    schema_name: str
    table_name: str


class CatalogAssetSummaryResponse(CatalogAssetReferenceResponse):
    # Raw database comments are descriptive source text. Clients must render
    # them as text, never as trusted markup or a business definition.
    table_comment: str | None


class CatalogAssetListResponse(BaseModel):
    source_id: uuid.UUID
    snapshot_id: uuid.UUID
    snapshot_published_at: datetime
    scope_matches_current_selection: bool
    page: int
    page_size: int
    total: int
    items: list[CatalogAssetSummaryResponse]


class CatalogColumnResponse(BaseModel):
    # Stable only within the pinned snapshot; callers must not treat a column
    # UUID as an authority outside the source/current-catalog boundary.
    column_id: uuid.UUID | None = None
    column_name: str
    ordinal_position: int
    is_nullable: bool
    formatted_native_type: str
    native_type_schema: str | None
    native_type_name: str | None
    native_type_oid: int | None
    native_type_modifier: int | None
    column_comment: str | None


class CatalogDeclaredKeyResponse(BaseModel):
    constraint_name: str
    key_kind: CatalogKeyKind
    column_names: list[str]


class CatalogForeignKeyColumnMappingResponse(BaseModel):
    ordinal_position: int
    referencing_column_name: str
    referenced_column_name: str


class CatalogForeignKeyResponse(BaseModel):
    # Available only for an in-scope target. It lets an owner explicitly
    # confirm one declared relationship without inferring an identifier from
    # a rendered constraint name.
    foreign_key_id: uuid.UUID | None = None
    constraint_name: str
    # An old in-scope target that is no longer selected is deliberately
    # rendered as TARGET_OUT_OF_SCOPE, with no target or mapping fields.
    target_status: CatalogForeignKeyTargetStatus
    referenced_asset: CatalogAssetReferenceResponse | None
    column_mappings: list[CatalogForeignKeyColumnMappingResponse]


class CatalogAssetDetailResponse(BaseModel):
    source_id: uuid.UUID
    snapshot_id: uuid.UUID
    snapshot_published_at: datetime
    scope_matches_current_selection: bool
    asset: CatalogAssetSummaryResponse
    columns: list[CatalogColumnResponse]
    declared_keys: list[CatalogDeclaredKeyResponse]
    foreign_keys: list[CatalogForeignKeyResponse]


class CatalogTableChangeResponse(BaseModel):
    schema_name: str
    table_name: str


class CatalogColumnIdentityChangeResponse(CatalogTableChangeResponse):
    column_name: str


class CatalogColumnChangeResponse(CatalogColumnIdentityChangeResponse):
    previous_formatted_native_type: str
    current_formatted_native_type: str
    previous_is_nullable: bool
    current_is_nullable: bool


class CatalogKeyChangeResponse(BaseModel):
    change: Literal["added", "removed"]
    schema_name: str
    table_name: str
    constraint_name: str
    key_kind: CatalogKeyKind
    column_names: list[str]


class CatalogForeignKeyChangeResponse(BaseModel):
    change: Literal["added", "removed"]
    schema_name: str
    table_name: str
    constraint_name: str
    target_status: CatalogForeignKeyTargetStatus
    referenced_asset: CatalogAssetReferenceResponse | None
    column_mappings: list[CatalogForeignKeyColumnMappingResponse]


class CatalogCommentChangeResponse(BaseModel):
    subject: Literal["table", "column"]
    schema_name: str
    table_name: str
    column_name: str | None
    previous_comment: str | None
    current_comment: str | None


class CatalogStructuralDiffResponse(BaseModel):
    source_id: uuid.UUID
    snapshot_id: uuid.UUID
    snapshot_published_at: datetime
    state: Literal["baseline", "compared", "scope_changed"]
    previous_snapshot_id: uuid.UUID | None
    previous_snapshot_published_at: datetime | None
    truncated: bool
    change_limit: int
    tables_added: list[CatalogTableChangeResponse]
    tables_removed: list[CatalogTableChangeResponse]
    columns_added: list[CatalogColumnIdentityChangeResponse]
    columns_removed: list[CatalogColumnIdentityChangeResponse]
    columns_changed: list[CatalogColumnChangeResponse]
    keys_changed: list[CatalogKeyChangeResponse]
    foreign_keys_changed: list[CatalogForeignKeyChangeResponse]
    comments_changed: list[CatalogCommentChangeResponse]


# ── confirmed single-table metric definitions ──────────────────────────────

_METRIC_OPERATIONS = Literal["count", "sum", "average", "minimum", "maximum"]


def _metric_text(value: str, *, maximum: int, field_name: str) -> str:
    if "\x00" in value or len(value) > maximum:
        raise ValueError(f"{field_name} is invalid")
    result = value.strip()
    if not result:
        raise ValueError(f"{field_name} is invalid")
    return result


class MetricAggregationRequest(BaseModel):
    """One confirmed aggregate; count accepts COUNT(*) or COUNT(column)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    operation: _METRIC_OPERATIONS
    column_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def validate_column_shape(self) -> "MetricAggregationRequest":
        if self.operation != "count" and self.column_id is None:
            raise ValueError(f"{self.operation} requires a column_id")
        return self


class MetricDefinitionWriteRequest(BaseModel):
    """Full explicit confirmation input for create or immutable new version."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=128)
    owner_confirmed: Literal[True]
    description: str | None = Field(default=None, max_length=2_000)
    snapshot_id: uuid.UUID
    asset_id: uuid.UUID
    aggregation: MetricAggregationRequest
    fixed_filters: list[PostgresFilter] = Field(default_factory=list, max_length=50)
    unit_label: str | None = Field(default=None, max_length=64)
    default_date_column_id: uuid.UUID | None = None
    default_time_zone: str | None = Field(default=None, max_length=128)

    @field_validator("name")
    @classmethod
    def validate_metric_name(cls, value: str) -> str:
        return _metric_text(value, maximum=128, field_name="metric name")

    @field_validator("description")
    @classmethod
    def validate_metric_description(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _metric_text(value, maximum=2_000, field_name="metric description")

    @field_validator("unit_label")
    @classmethod
    def validate_metric_unit(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _metric_text(value, maximum=64, field_name="metric unit label")

    @field_validator("default_time_zone")
    @classmethod
    def validate_metric_timezone(cls, value: str | None) -> str | None:
        if value is None:
            return None
        result = _metric_text(value, maximum=128, field_name="metric timezone")
        try:
            ZoneInfo(result)
        except (ValueError, ZoneInfoNotFoundError) as exc:
            raise ValueError("metric timezone must be a valid IANA timezone") from exc
        return result

    @model_validator(mode="after")
    def validate_metric_time_defaults(self) -> "MetricDefinitionWriteRequest":
        if self.default_time_zone is not None and self.default_date_column_id is None:
            raise ValueError("default_time_zone requires default_date_column_id")
        return self


class MetricDefinitionCreateRequest(MetricDefinitionWriteRequest):
    pass


class MetricDefinitionVersionCreateRequest(MetricDefinitionWriteRequest):
    pass


class MetricAggregationResponse(BaseModel):
    operation: _METRIC_OPERATIONS
    column_id: uuid.UUID | None
    column_name: str | None = None


class MetricDefinitionVersionResponse(BaseModel):
    metric_id: uuid.UUID
    version_number: int
    name: str
    description: str | None
    snapshot_id: uuid.UUID
    asset_id: uuid.UUID
    aggregation: MetricAggregationResponse
    fixed_filters: list[PostgresFilter]
    unit_label: str | None
    default_date_column_id: uuid.UUID | None
    default_date_column_name: str | None = None
    default_time_zone: str | None
    confirmed_by_user_id: uuid.UUID
    confirmed_at: datetime


class MetricCurrentCatalogResponse(BaseModel):
    """Live IDs after compatible catalog rematerialization.

    These are intentionally separate from the immutable version's historical
    snapshot/asset values.  They let a UI prepare a fresh confirmation against
    the current selected catalog without treating a stale version UUID as a
    current catalog reference.
    """

    snapshot_id: uuid.UUID
    asset_id: uuid.UUID
    schema_name: str
    table_name: str
    aggregation_column_id: uuid.UUID | None
    aggregation_column_name: str | None = None
    fixed_filters: list[PostgresFilter]
    default_date_column_id: uuid.UUID | None
    default_date_column_name: str | None = None
    default_time_zone: str | None = None


class MetricDefinitionResponse(MetricDefinitionVersionResponse):
    """Current definition plus a public lifecycle result."""

    state: Literal["valid", "needs_review", "retired"]
    needs_review_reason: str | None = None
    retired_at: datetime | None = None
    current_catalog: MetricCurrentCatalogResponse | None = None


class MetricDefinitionListResponse(BaseModel):
    items: list[MetricDefinitionResponse]
    count: int


# ── confirmed same-source relationship definitions ────────────────────────

_RELATIONSHIP_JOIN_TYPES = Literal["inner", "left"]
_RELATIONSHIP_KEY_KINDS = Literal["primary_key", "unique"]


class RelationshipDefinitionWriteRequest(BaseModel):
    """Explicit confirmation of one eligible declared FK direction.

    Table names, key columns, and ON expressions are intentionally absent:
    the service derives all of them from the current owner-authorized catalog
    record identified by ``foreign_key_id``.
    """

    model_config = ConfigDict(extra="forbid")

    owner_confirmed: Literal[True]
    snapshot_id: uuid.UUID
    foreign_key_id: uuid.UUID
    join_type: _RELATIONSHIP_JOIN_TYPES


class RelationshipDefinitionCreateRequest(RelationshipDefinitionWriteRequest):
    pass


class RelationshipDefinitionVersionCreateRequest(RelationshipDefinitionWriteRequest):
    pass


class RelationshipColumnPairResponse(BaseModel):
    ordinal_position: int
    base_column_id: uuid.UUID
    base_column_name: str
    lookup_column_id: uuid.UUID
    lookup_column_name: str


class RelationshipTargetKeyResponse(BaseModel):
    key_id: uuid.UUID
    constraint_name: str
    key_kind: _RELATIONSHIP_KEY_KINDS
    lookup_column_ids: list[uuid.UUID]
    lookup_column_names: list[str]


class RelationshipDefinitionVersionResponse(BaseModel):
    relationship_id: uuid.UUID
    version_number: int
    snapshot_id: uuid.UUID
    foreign_key_id: uuid.UUID
    base_asset_id: uuid.UUID
    base_schema_name: str | None = None
    base_table_name: str | None = None
    lookup_asset_id: uuid.UUID
    lookup_schema_name: str | None = None
    lookup_table_name: str | None = None
    join_type: _RELATIONSHIP_JOIN_TYPES
    column_pairs: list[RelationshipColumnPairResponse]
    target_key_id: uuid.UUID
    target_key_constraint_name: str | None = None
    target_key_kind: _RELATIONSHIP_KEY_KINDS | None = None
    confirmed_by_user_id: uuid.UUID
    confirmed_at: datetime


class RelationshipCurrentCatalogResponse(BaseModel):
    """Fresh structural IDs for a compatible current catalog snapshot."""

    snapshot_id: uuid.UUID
    foreign_key_id: uuid.UUID
    base_asset_id: uuid.UUID
    base_schema_name: str
    base_table_name: str
    lookup_asset_id: uuid.UUID
    lookup_schema_name: str
    lookup_table_name: str
    join_type: _RELATIONSHIP_JOIN_TYPES
    column_pairs: list[RelationshipColumnPairResponse]
    target_key: RelationshipTargetKeyResponse


class RelationshipDefinitionResponse(RelationshipDefinitionVersionResponse):
    state: Literal["valid", "needs_review", "retired"]
    needs_review_reason: str | None = None
    retired_at: datetime | None = None
    current_catalog: RelationshipCurrentCatalogResponse | None = None


class RelationshipDefinitionListResponse(BaseModel):
    items: list[RelationshipDefinitionResponse]
    count: int


class EligibleDeclaredRelationshipResponse(BaseModel):
    snapshot_id: uuid.UUID
    foreign_key_id: uuid.UUID
    constraint_name: str
    base_asset_id: uuid.UUID
    base_schema_name: str
    base_table_name: str
    lookup_asset_id: uuid.UUID
    lookup_schema_name: str
    lookup_table_name: str
    column_pairs: list[RelationshipColumnPairResponse]
    target_key: RelationshipTargetKeyResponse


class EligibleDeclaredRelationshipListResponse(BaseModel):
    items: list[EligibleDeclaredRelationshipResponse]
    count: int


# ── optional bounded PostgreSQL profiling (Stage 6A) ──────────────────────


class SourceProfilingConfigurationRequest(BaseModel):
    """Owner-only opt-in. Enabling does not enqueue or schedule a profile."""

    model_config = ConfigDict(extra="forbid")

    profiling_enabled: bool


class SourceProfilingConfigurationResponse(BaseModel):
    source_id: uuid.UUID
    profiling_enabled: bool
    profiling_generation: int
    # Completed aggregate-only profiles are retained; every read rechecks the
    # current source owner and selected-table scope.
    historical_profiles_retained: Literal[True] = True


class SampleColumnName(BaseModel):
    """A column named by schema, table and column. Names only; never a value."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    schema_name: str = Field(min_length=1, max_length=63, alias="schema")
    table: str = Field(min_length=1, max_length=63)
    column: str = Field(min_length=1, max_length=63)


class SampleValuesConfigurationRequest(BaseModel):
    """Owner-only. ``excluded_columns`` REPLACES the exclusion list; omit a field to leave it unchanged."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool | None = None
    excluded_columns: list[SampleColumnName] | None = Field(default=None, max_length=2000)


class SampleCollectedColumn(SampleColumnName):
    method: Literal["pg_stats", "exact"]
    value_count: int


class SampleSkippedColumn(SampleColumnName):
    reason: str


class SampleValuesResponse(BaseModel):
    """Counts and names only. The values go to the model, not to another API surface (SPEC-07 I7)."""

    model_config = ConfigDict(populate_by_name=True)

    enabled: bool
    sample_values_generation: int
    excluded_columns: list[SampleColumnName]
    collected: list[SampleCollectedColumn]
    skipped: list[SampleSkippedColumn]


class AiSettingsRequest(BaseModel):
    """Owner-only (SPEC-09 W3.1). Omit a field to leave it unchanged. The sample-values switch has its own endpoint."""

    model_config = ConfigDict(extra="forbid")

    result_rows_to_model: StrictBool | None = None


class AiSettingsResponse(BaseModel):
    result_rows_to_model: bool
    sample_values_enabled: bool
    result_rows_to_model_generation: int


class ColumnPrivacyEntry(BaseModel):
    """One text column with the suggestion, the owner's override and the effective flag. Names only."""

    model_config = ConfigDict(populate_by_name=True)

    schema_name: str = Field(alias="schema")
    table: str
    column: str
    type: str
    suggested_personal: bool
    state: Literal["personal", "not_personal"] | None
    effective_personal: bool


class ColumnPrivacyChange(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    schema_name: str = Field(min_length=1, max_length=63, alias="schema")
    table: str = Field(min_length=1, max_length=63)
    column: str = Field(min_length=1, max_length=63)
    state: Literal["personal", "not_personal"] | None


class ColumnPrivacyUpdateRequest(BaseModel):
    """``state: null`` clears the override so the suggestion applies again."""

    model_config = ConfigDict(extra="forbid")

    changes: list[ColumnPrivacyChange] = Field(max_length=2000)


class SourceProfileTableSelectionRequest(BaseModel):
    """A narrower selection using only current catalog IDs, never names/SQL."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    asset_id: uuid.UUID
    # Empty means record the table-level observed-row count only. This is
    # useful for a safely bounded profile without treating all columns as an
    # implicit opt-in.
    column_ids: list[uuid.UUID] = Field(default_factory=list, max_length=50)

    @field_validator("column_ids")
    @classmethod
    def validate_profile_column_ids(cls, value: list[uuid.UUID]) -> list[uuid.UUID]:
        if len(value) != len(set(value)):
            raise ValueError("profile columns must be unique")
        return value


class SourceProfileRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tables: list[SourceProfileTableSelectionRequest] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def validate_profile_assets(self) -> "SourceProfileRunRequest":
        asset_ids = [item.asset_id for item in self.tables]
        if len(asset_ids) != len(set(asset_ids)):
            raise ValueError("profile tables must be unique")
        return self


class SourceProfileRunResponse(BaseModel):
    """Safe public run facts; lease identity and credentials stay private."""

    run_id: uuid.UUID
    snapshot_id: uuid.UUID
    state: SourceProfileRunStatus
    requested_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    attempt_count: int
    next_attempt_at: datetime | None
    failure_code: str | None
    sampling_method: SourceProfileSamplingMethod
    sample_percent: Decimal
    sample_limit: int
    sample_seed: int
    max_operations: int
    selected_asset_count: int
    selected_column_count: int
    population: SourceProfilePopulation = SourceProfilePopulation.OBSERVED_SAMPLE


class SourceProfileRunListResponse(BaseModel):
    source_id: uuid.UUID
    page: int
    page_size: int
    total: int
    items: list[SourceProfileRunResponse]


class SourceProfileTableObservationResponse(BaseModel):
    observation_id: uuid.UUID
    asset_id: uuid.UUID
    schema_name: str
    table_name: str
    collected_at: datetime
    sampling_method: SourceProfileSamplingMethod
    sample_percent: Decimal
    sample_limit: int
    sample_seed: int
    population: SourceProfilePopulation
    observed_row_count: int
    status: SourceProfileObservationStatus
    warning_codes: list[str]


class SourceProfileColumnObservationResponse(BaseModel):
    observation_id: uuid.UUID
    table_observation_id: uuid.UUID
    asset_id: uuid.UUID
    column_id: uuid.UUID
    column_name: str
    formatted_native_type: str
    scalar_kind: str
    collected_at: datetime
    population: SourceProfilePopulation
    observed_row_count: int
    null_count: int
    non_null_count: int
    null_rate: Decimal | None
    distinct_count: int
    numeric_count: int | None
    numeric_sum: Decimal | None
    numeric_minimum: Decimal | None
    numeric_maximum: Decimal | None
    numeric_mean: Decimal | None
    status: SourceProfileObservationStatus
    warning_codes: list[str]


class SourceProfileObservationListResponse(BaseModel):
    source_id: uuid.UUID
    run_id: uuid.UUID
    snapshot_id: uuid.UUID
    page: int
    page_size: int
    total_tables: int
    table_observations: list[SourceProfileTableObservationResponse]
    column_observations: list[SourceProfileColumnObservationResponse]


class SourceProfileFindingResponse(BaseModel):
    finding_id: uuid.UUID
    table_observation_id: uuid.UUID
    column_observation_id: uuid.UUID
    asset_id: uuid.UUID
    schema_name: str
    table_name: str
    column_id: uuid.UUID
    column_name: str
    finding_type: SourceProfileFindingType
    severity: SourceProfileFindingSeverity
    title: str
    description: str
    limitations: str
    evidence: dict
    generated_at: datetime


class SourceProfileFindingListResponse(BaseModel):
    source_id: uuid.UUID
    run_id: uuid.UUID
    page: int
    page_size: int
    total: int
    items: list[SourceProfileFindingResponse]
