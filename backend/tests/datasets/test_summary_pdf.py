"""HTTP tests for GET /api/v1/datasets/{id}/summary.pdf."""
from __future__ import annotations

from app.db.models.dataset import DatasetStatus

from tests.datasets.conftest import StubDatasetRecord


async def test_summary_pdf_ok(client, fake_session, dataset_id, write_artifacts):
    fake_session.record = StubDatasetRecord(id=dataset_id)
    write_artifacts(profile=True, findings=True, insights="ok")

    resp = await client.get(f"/api/v1/datasets/{dataset_id}/summary.pdf")

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/pdf")
    assert resp.content[:4] == b"%PDF"
    assert "attachment" in resp.headers["content-disposition"]
    assert "sales_data-summary.pdf" in resp.headers["content-disposition"]


async def test_summary_pdf_degraded_insights(client, fake_session, dataset_id, write_artifacts):
    fake_session.record = StubDatasetRecord(id=dataset_id)
    write_artifacts(profile=True, findings=True, insights="degraded")

    resp = await client.get(f"/api/v1/datasets/{dataset_id}/summary.pdf")

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/pdf")
    assert resp.content[:4] == b"%PDF"


async def test_summary_pdf_not_complete(client, fake_session, dataset_id, write_artifacts):
    fake_session.record = StubDatasetRecord(id=dataset_id, status=DatasetStatus.PROFILING)
    write_artifacts(profile=True, findings=True, insights="ok")

    resp = await client.get(f"/api/v1/datasets/{dataset_id}/summary.pdf")

    assert resp.status_code == 404


async def test_summary_pdf_missing_profile(client, fake_session, dataset_id, write_artifacts):
    fake_session.record = StubDatasetRecord(id=dataset_id)
    write_artifacts(profile=False, findings=True, insights="ok")

    resp = await client.get(f"/api/v1/datasets/{dataset_id}/summary.pdf")

    assert resp.status_code == 404


# ── Content-Disposition filename hardening (RFC 6266) ────────────────────────

async def _get_ok(client, fake_session, dataset_id, write_artifacts, original_name):
    fake_session.record = StubDatasetRecord(id=dataset_id, original_name=original_name)
    write_artifacts(profile=True, findings=True, insights="ok")
    resp = await client.get(f"/api/v1/datasets/{dataset_id}/summary.pdf")
    assert resp.status_code == 200
    assert resp.content[:4] == b"%PDF"
    return resp.headers["content-disposition"]


async def test_summary_pdf_filename_with_quotes(client, fake_session, dataset_id, write_artifacts):
    cd = await _get_ok(
        client, fake_session, dataset_id, write_artifacts, 'we"ird"name.csv'
    )
    # The quoted filename= fallback must not contain raw quotes.
    fallback = cd.split('filename="', 1)[1].split('"', 1)[0]
    assert '"' not in fallback
    assert fallback.endswith("-summary.pdf")


async def test_summary_pdf_filename_with_crlf(client, fake_session, dataset_id, write_artifacts):
    cd = await _get_ok(
        client, fake_session, dataset_id, write_artifacts,
        "evil\r\nX-Injected: 1.csv",
    )
    assert "\r" not in cd and "\n" not in cd
    assert "X-Injected" in cd  # text kept, but control chars stripped
    assert "attachment" in cd


async def test_summary_pdf_filename_path_like(client, fake_session, dataset_id, write_artifacts):
    cd = await _get_ok(
        client, fake_session, dataset_id, write_artifacts,
        "../../etc/passwd.csv",
    )
    assert "/" not in cd.split("filename=", 1)[1]
    assert 'filename="passwd-summary.pdf"' in cd


async def test_summary_pdf_filename_non_ascii(client, fake_session, dataset_id, write_artifacts):
    cd = await _get_ok(
        client, fake_session, dataset_id, write_artifacts, "数据.csv"
    )
    # ASCII fallback plus RFC 8187 UTF-8 ext-value with the real name.
    assert cd.isascii()
    assert 'filename="__-summary.pdf"' in cd
    assert "filename*=utf-8''%E6%95%B0%E6%8D%AE-summary.pdf" in cd
