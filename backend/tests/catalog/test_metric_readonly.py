"""SPEC-08 B7 / L1-7: listing metrics for a prompt must not write or commit."""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app.db.models.metric_definition import MetricDefinition, MetricLifecycleState
from app.exceptions.custom_exceptions import NotFoundError
from app.services.metric_service import (
    create_metric_definition_for_owner,
    list_active_metric_definitions_readonly,
    list_metric_definitions_for_owner,
)
from tests.catalog.test_metric_service import (  # noqa: F401  (fixtures + helpers)
    _publish,
    _request,
    _settings,
    _snapshot_input,
    metric_catalog,
    metric_session,
)


async def _lifecycle(session) -> list[MetricLifecycleState]:
    return list((await session.execute(select(MetricDefinition.lifecycle_state))).scalars())


async def test_lists_active_resolved_metrics_without_a_write(metric_catalog, monkeypatch):
    s = metric_catalog.session
    created = await create_metric_definition_for_owner(
        s, source_id=metric_catalog.source_id, owner_user_id=metric_catalog.owner_id,
        request=_request(metric_catalog), settings=_settings(),
    )
    commits = []
    real_commit = s.commit

    async def counting_commit():
        commits.append(1)
        await real_commit()

    monkeypatch.setattr(s, "commit", counting_commit)
    flushes = []
    monkeypatch.setattr(s, "flush", lambda *a, **k: flushes.append(1))      # any write path would flush first

    resolved = await list_active_metric_definitions_readonly(
        s, owner_user_id=metric_catalog.owner_id, source_id=metric_catalog.source_id, settings=_settings()
    )
    assert [r.metric_id for r in resolved] == [created.metric_id]
    assert resolved[0].aggregation == "sum" and resolved[0].aggregation_column_name == "amount"
    assert commits == [] and flushes == []
    assert not (s.new or s.dirty or s.deleted)


async def test_a_stale_definition_is_left_out_but_not_marked(metric_catalog):
    s = metric_catalog.session
    await create_metric_definition_for_owner(
        s, source_id=metric_catalog.source_id, owner_user_id=metric_catalog.owner_id,
        request=_request(metric_catalog), settings=_settings(),
    )
    await _publish(
        s, source_id=metric_catalog.source_id, owner_user_id=metric_catalog.owner_id,
        snapshot_input=_snapshot_input(metric_catalog.identity, amount_type="text", amount_oid=25, amount_formatted="text"),
    )
    resolved = await list_active_metric_definitions_readonly(
        s, owner_user_id=metric_catalog.owner_id, source_id=metric_catalog.source_id, settings=_settings()
    )
    assert resolved == ()
    assert await _lifecycle(s) == [MetricLifecycleState.ACTIVE]           # the read left it alone
    assert not (s.new or s.dirty or s.deleted)

    # The existing listing endpoint is still the one that marks it.
    listed = await list_metric_definitions_for_owner(
        s, source_id=metric_catalog.source_id, owner_user_id=metric_catalog.owner_id, settings=_settings()
    )
    assert [v.state for v in listed] == ["needs_review"]
    assert await _lifecycle(s) == [MetricLifecycleState.NEEDS_REVIEW]
    assert await list_active_metric_definitions_readonly(
        s, owner_user_id=metric_catalog.owner_id, source_id=metric_catalog.source_id, settings=_settings()
    ) == ()


async def test_another_owner_cannot_list(metric_catalog):
    with pytest.raises(NotFoundError):
        await list_active_metric_definitions_readonly(
            metric_catalog.session, owner_user_id=metric_catalog.other_owner_id, source_id=metric_catalog.source_id,
            settings=_settings(),
        )
