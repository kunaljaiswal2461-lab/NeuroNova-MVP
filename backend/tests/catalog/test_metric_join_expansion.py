"""Stage 5B deterministic metric-to-approved-lookup expansion tests.

These unit contracts deliberately avoid a database connection: catalog and
relationship services supply already-resolved, current owner-authorized facts.
The tests prove the planner cannot turn that narrow input into a fan-out-prone
or unqualified generic join plan.
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.core.config import Settings
from app.services.metric_service import (
    MetricValidationError,
    ResolvedMetricDefinition,
    expand_metric_relationship_query_intent,
)
from app.services.postgres_query_plan import PostgresFilter


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        api_key="stage5b-metric-join-test-key",
        database_url="sqlite+aiosqlite://",
    )


def _resolved_metric():
    source_id = uuid4()
    snapshot_id = uuid4()
    asset_id = uuid4()
    amount_id = uuid4()
    status_id = uuid4()
    fixed_filter = PostgresFilter(
        column_id=status_id,
        operator="eq",
        value={"kind": "text", "value": "paid"},
    )
    metric = ResolvedMetricDefinition(
        metric_id=uuid4(),
        metric_version_id=uuid4(),
        version_number=3,
        source_id=source_id,
        snapshot_id=snapshot_id,
        asset_id=asset_id,
        asset_schema_name="sales",
        asset_table_name="orders",
        display_name="Paid order revenue",
        description=None,
        aggregation="sum",
        aggregation_column_id=amount_id,
        aggregation_column_name="amount",
        fixed_filters=(fixed_filter,),
        unit_label="USD",
        default_date_column_id=None,
        default_date_column_name=None,
        default_time_zone=None,
        confirmed_at=datetime.now(timezone.utc),
        confirmed_by_user_id=uuid4(),
        allowed_column_ids=frozenset((amount_id, status_id)),
    )
    return metric, amount_id, status_id


def _relationship(metric, *, join_type: str = "left"):
    lookup_asset_id = uuid4()
    lookup_region_id = uuid4()
    relationship = SimpleNamespace(
        relationship_id=uuid4(),
        relationship_version_id=uuid4(),
        version_number=4,
        source_id=metric.source_id,
        snapshot_id=metric.snapshot_id,
        base_asset_id=metric.asset_id,
        lookup_asset_id=lookup_asset_id,
        join_type=join_type,
        # The service exposes all current scoped columns for modifiers; the
        # ordered FK mappings remain separately constrained in the compiler.
        allowed_base_column_ids=metric.allowed_column_ids,
        allowed_lookup_column_ids=frozenset((lookup_region_id,)),
    )
    return relationship, lookup_region_id


def _intent(metric, relationship, lookup_region_id, *, placement: str | None = "join_on"):
    return {
        "metric_id": metric.metric_id,
        "version_number": metric.version_number,
        "join": {
            "relationship_id": relationship.relationship_id,
            "relationship_version_id": relationship.relationship_version_id,
            "lookup_asset_id": relationship.lookup_asset_id,
            "join_type": relationship.join_type,
        },
        "additional_filters": [
            {
                "column_id": lookup_region_id,
                "asset_id": relationship.lookup_asset_id,
                "operator": "eq",
                "placement": placement,
                "value": {"kind": "text", "value": "West"},
            }
        ],
        "group_by": [
            {
                "column_id": lookup_region_id,
                "asset_id": relationship.lookup_asset_id,
            }
        ],
        "sort": {"output_kind": "grouping", "output_index": 0, "direction": "asc"},
        "limit": 100,
    }


def test_joined_metric_keeps_aggregate_and_fixed_filter_on_base_table():
    metric, amount_id, status_id = _resolved_metric()
    relationship, region_id = _relationship(metric)

    plan = expand_metric_relationship_query_intent(
        metric,
        relationship,
        _intent(metric, relationship, region_id),
        _settings(),
    )

    assert plan.join is not None
    assert plan.join.relationship_id == relationship.relationship_id
    assert plan.join.relationship_version_id == relationship.relationship_version_id
    assert plan.aggregations[0].column_id == amount_id
    assert plan.aggregations[0].asset_id == metric.asset_id
    assert plan.filters[0].column_id == status_id
    assert plan.filters[0].asset_id == metric.asset_id
    assert plan.filters[0].placement is None
    assert plan.filters[1].column_id == region_id
    assert plan.filters[1].asset_id == relationship.lookup_asset_id
    assert plan.filters[1].placement == "join_on"
    assert plan.group_by[0].asset_id == relationship.lookup_asset_id


def test_left_lookup_filter_requires_explicit_where_or_join_on_semantics():
    metric, _amount_id, _status_id = _resolved_metric()
    relationship, region_id = _relationship(metric, join_type="left")

    with pytest.raises(MetricValidationError, match="LEFT join lookup filters require explicit placement"):
        expand_metric_relationship_query_intent(
            metric,
            relationship,
            _intent(metric, relationship, region_id, placement=None),
            _settings(),
        )


def test_joined_metric_rejects_forged_relationship_or_base_join_on_filter():
    metric, _amount_id, status_id = _resolved_metric()
    relationship, region_id = _relationship(metric)
    forged = _intent(metric, relationship, region_id)
    forged["join"] = {**forged["join"], "lookup_asset_id": str(uuid4())}
    with pytest.raises(MetricValidationError, match="approved relationship"):
        expand_metric_relationship_query_intent(metric, relationship, forged, _settings())

    invalid_base_filter = _intent(metric, relationship, region_id)
    invalid_base_filter["additional_filters"] = [
        {
            "column_id": status_id,
            "asset_id": metric.asset_id,
            "operator": "eq",
            "placement": "join_on",
            "value": {"kind": "text", "value": "paid"},
        }
    ]
    with pytest.raises(MetricValidationError, match="base metric filters"):
        expand_metric_relationship_query_intent(metric, relationship, invalid_base_filter, _settings())
