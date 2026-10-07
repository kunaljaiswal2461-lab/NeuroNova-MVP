"""SPEC-03 §16.4 — generator tests GEN1–GEN7."""
from __future__ import annotations

import ast
import datetime as dt
import hashlib
import random
import re
from pathlib import Path

import pytest

from evals.generator import anchors, generate

IST = generate.IST


@pytest.fixture(scope="module")
def ds():
    return generate.generate()


@pytest.fixture(scope="module")
def files(tmp_path_factory):
    out = tmp_path_factory.mktemp("copy")
    generate.write_files(out)
    return out


def test_gen1_same_seed_same_bytes(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    assert generate.write_files(a) == generate.write_files(b)
    for f in a.iterdir():
        assert hashlib.sha256(f.read_bytes()).hexdigest() == hashlib.sha256((b / f.name).read_bytes()).hexdigest()


def test_gen2_different_seed_differs(tmp_path):
    assert generate.write_files(tmp_path / "a", 27) != generate.write_files(tmp_path / "b", 28)


def test_gen3_anchors_from_files_equal_anchors_from_rows(ds, files):
    assert anchors.compute_from_files(files) == anchors.compute_from_dataset(ds)


_OFFSET = re.compile(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d(\.\d+)?[+-]\d\d:\d\d$")
_TS_LIKE = re.compile(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d")


def test_gen4_every_timestamp_has_an_explicit_offset(files):
    checked = 0
    for path in files.glob("*.copy"):
        for line in path.read_text(encoding="utf-8").splitlines():
            for cell in line.split("\t"):
                if _TS_LIKE.match(cell):
                    assert _OFFSET.match(cell), f"{path.name}: naive timestamp {cell!r}"
                    checked += 1
    assert checked > 100_000


def test_gen5_month_boundary_rows_exist_for_every_2026_month(ds):
    stamps = [o[4].astimezone(IST) for o in ds.orders]
    for month in range(1, 7):
        early = [t for t in stamps if (t.year, t.month, t.day) == (2026, month, 1) and t.hour == 0 and t.minute < 30]
        assert len(early) >= 3, f"month {month}: early-on-the-1st rows"
        if month < 6:                                            # data ends 14 June
            last_day = (dt.date(2026, month + 1, 1) - dt.timedelta(days=1)).day
            late = [t for t in stamps if (t.year, t.month, t.day) == (2026, month, last_day) and t.hour == 23 and t.minute >= 30]
            assert len(late) >= 3, f"month {month}: late-on-the-last-day rows"


def test_gen5_the_boundary_trap_bites_for_march(ds):
    a = anchors.compute_from_dataset(ds)
    # Not merely "different": equal planting at both ends of a month cancels out, so demand a real gap.
    assert abs(a["march_2026_orders_ist"] - a["march_2026_orders_utc"]) >= 5


def _assert_distinct(sums: dict) -> None:
    values = list(sums.values())
    assert len(values) == len(set(values))


def test_gen6_no_ties_in_rankings(ds):
    paid = {o[0]: o for o in ds.orders if o[2] == "PAID"}
    cust_region = {c[0]: c[5] for c in ds.customers}
    per_customer: dict[str, int] = {}
    per_region: dict[int, int] = {}
    for o in paid.values():
        if o[1] is None:
            continue
        per_customer[o[1]] = per_customer.get(o[1], 0) + o[6]
        per_region[cust_region[o[1]]] = per_region.get(cust_region[o[1]], 0) + o[6]
    per_product: dict[int, int] = {}
    for oid, _, pid, qty, price in ds.order_items:
        if oid in paid:
            per_product[pid] = per_product.get(pid, 0) + qty * price
    _assert_distinct(per_customer)
    _assert_distinct(per_region)
    _assert_distinct(per_product)


_GENERATE_SRC = Path(generate.__file__)
_ALLOWED_RANDOM_ATTRS = {"Random", "random"}


def test_gen7_only_random_dot_random_is_used():
    tree = ast.parse(_GENERATE_SRC.read_text())
    banned = {name for name in dir(random.Random) if not name.startswith("_")} - _ALLOWED_RANDOM_ATTRS
    offenders = [
        f"line {n.lineno}: .{n.attr}"
        for n in ast.walk(tree)
        if isinstance(n, ast.Attribute) and n.attr in banned
    ]
    assert not offenders, offenders
    # Only the module's ``random.Random`` constructor may be touched on the stdlib module.
    for n in ast.walk(tree):
        if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == "random":
            assert n.attr == "Random", f"line {n.lineno}: random.{n.attr}"
    imports = [n for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module == "random"]
    assert not imports, "use `import random`, not `from random import …`"


def test_shape_matches_spec_table(ds):
    assert len(ds.regions) == 36
    assert len(ds.customers) == 5000
    assert len(ds.products) == 400
    assert len(ds.orders) == 60000
    assert 120_000 <= len(ds.order_items) <= 180_000
    assert 1_500 <= len(ds.refunds) <= 3_500
    assert 30_000 <= len(ds.order_events) <= 50_000
    statuses = {o[2] for o in ds.orders}
    assert statuses == {"PAID", "pending", "Cancelled", "REFUNDED"}
    assert any(o[1] is None for o in ds.orders)                       # guest orders
    assert any(o[5] is None for o in ds.orders)                       # undelivered
    assert any(c[6] == "enterprise " for c in ds.customers)
    assert any("DROP TABLE" in c[1] for c in ds.customers)
    assert any("disregard your rules" in (r[3] or "") for r in ds.refunds)
    first, last = min(o[4] for o in ds.orders), max(o[4] for o in ds.orders)
    assert first >= generate.DATA_START and last < generate.DATA_END


def test_planted_same_name_different_city_exists(ds):
    by_name: dict[str, set[str]] = {}
    for c in ds.customers:
        by_name.setdefault(c[1], set()).add(c[4])
    assert sum(1 for cities in by_name.values() if len(cities) > 1) >= 10


def test_order_ids_follow_time_and_total_references_exist(ds):
    stamps = [o[4] for o in ds.orders]
    assert stamps == sorted(stamps)
    order_ids = {o[0] for o in ds.orders}
    assert all(i[0] in order_ids for i in ds.order_items)
    assert all(r[1] in order_ids for r in ds.refunds)
    assert all(e[1] in order_ids for e in ds.order_events)


def test_exact_boundary_and_threshold_probes_exist(ds):
    """Planted rows that make >= vs > (and < vs <=) flips change answers."""
    midnights = {o[4] for o in ds.orders if o[2] == "PAID"}
    for y, m in [(2024, 1), (2025, 1), (2026, 1), (2026, 3), (2026, 4), (2026, 6)]:
        assert dt.datetime(y, m, 1, tzinfo=IST) in midnights
    assert dt.datetime(2026, 6, 14, tzinfo=IST) in midnights
    assert any(o[2] == "pending" and o[4] == dt.datetime(2024, 1, 10, tzinfo=IST) for o in ds.orders)
    assert any(o[3] == "phone" and o[6] == 60_000_000 for o in ds.orders)
    assert any(o[1] is None and o[2] == "PAID" and o[6] == 50_000_000 for o in ds.orders)
    assert any(p[3] == "Electronics" and p[5] and p[4] == 4_000_000 for p in ds.products)
    assert any(p[4] == 100_000 for p in ds.products)
    assert any(r[2] == 50_000_000 for r in ds.refunds)
    assert any(r[3] == "Late delivery" and r[2] == 10_000_000 for r in ds.refunds)
    for month in (1, 3, 4):
        assert any(r[3] == "Damaged on arrival" and r[4] == dt.datetime(2026, month, 1, tzinfo=IST) for r in ds.refunds)
    assert any(c[7] == dt.datetime(2025, 1, 1, tzinfo=IST) for c in ds.customers)
    assert any(c[4] == "Mumbai" and c[7] == dt.datetime(2026, 1, 1, tzinfo=IST) for c in ds.customers)
    assert any(e[2] == "shipped" and e[3] == dt.datetime(2025, 1, 1, tzinfo=IST) for e in ds.order_events)


def test_nullable_columns_really_contain_nulls(ds):
    assert any(c[3] is None for c in ds.customers)
    assert any(r[3] is None for r in ds.refunds)
    assert any("disregard your rules" in (r[3] or "") for r in ds.refunds)         # injection row survived
