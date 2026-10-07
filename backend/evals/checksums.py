"""Table checksums and the committed manifest (SPEC-03 §9.4, INV-P0-04).

    python -m evals.checksums --write   # recompute md5s from the loaded fixture
    python -m evals.checksums --check   # compare the loaded fixture with manifest.json

Checksums come from Postgres (``t::text`` rendering is part of the contract, run with
``TimeZone=UTC``); row counts and anchors come from the Python generator.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys

from evals import pgrun
from evals.generator import anchors, generate
from evals.models import MANIFEST_PATH

TABLE_PK = {
    "shop.regions": "id",
    "shop.customers": "id",
    "shop.products": "id",
    "shop.orders": "id",
    "shop.order_items": "order_id, line_no",
    "shop.refunds": "id",
    "shop.\"Order Events\"": "id",
    "shop.page_views": "id",
}
MANIFEST_NAMES = {"shop.\"Order Events\"": "shop.Order Events"}


def checksum_sql(table: str, pk: str) -> str:
    return f"SELECT md5(string_agg(t::text, '|' ORDER BY {pk})) FROM {table} AS t"


async def live_checksums(conn) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for table, pk in TABLE_PK.items():
        rows = (await conn.fetchval(f"SELECT count(*) FROM {table}"))
        md5 = await conn.fetchval(checksum_sql(table, pk))
        out[MANIFEST_NAMES.get(table, table)] = {"rows": rows, "md5": md5}
    return out


def load_manifest() -> dict:
    return json.loads(MANIFEST_PATH.read_text())


def build_manifest(live: dict[str, dict], version: int | None = None) -> dict:
    previous = load_manifest() if MANIFEST_PATH.exists() else {}
    ds = generate.generate()
    return {
        "fixture_version": version if version is not None else previous.get("fixture_version", 1),
        "tables": dict(sorted(live.items())),
        "anchors": anchors.compute_from_dataset(ds),
    }


async def _main(write: bool) -> int:
    conn = await pgrun.connect_grader()
    try:
        live = await live_checksums(conn)
    finally:
        await conn.close()
    if write:
        MANIFEST_PATH.write_text(json.dumps(build_manifest(live), indent=2, sort_keys=False) + "\n")
        print(f"wrote {MANIFEST_PATH}")
        return 0
    expected = load_manifest()["tables"]
    bad = [t for t, v in live.items() if expected.get(t) != v]
    for t in bad:
        print(f"MISMATCH {t}: live={live[t]} manifest={expected.get(t)}", file=sys.stderr)
    print("fixture matches manifest" if not bad else f"{len(bad)} table(s) differ")
    return 1 if bad else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m evals.checksums")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--write", action="store_true")
    g.add_argument("--check", action="store_true")
    args = ap.parse_args(argv)
    return asyncio.run(_main(args.write))


if __name__ == "__main__":
    raise SystemExit(main())
