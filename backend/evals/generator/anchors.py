"""Anchors: aggregates computed in Python, independently of SQL (SPEC-03 §9.4).

``compute_from_dataset`` works on the generator's rows; ``compute_from_files``
re-parses the emitted COPY files with its own small parser (GEN3). The live
test F6 then asserts that SQL over the *loaded* database returns the same
values, so any disagreement points to a loading bug: a time-zone shift, a
truncated COPY, an encoding issue.
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path

from evals.generator.generate import Dataset, fmt_money

UTC = dt.timezone.utc
PAGE_VIEWS_ROWS = 2_000_000                     # fixed by 30_page_views.sql

# SQL equivalent of every anchor, run as nn_grader in test F6. Scalars only;
# the by-status map is built with json_object_agg.
ANCHOR_SQL: dict[str, str] = {
    "customers_total": "SELECT count(*) FROM shop.customers",
    "orders_total": "SELECT count(*) FROM shop.orders",
    "orders_paid_count": "SELECT count(*) FROM shop.orders WHERE status = 'PAID'",
    "orders_paid_revenue": "SELECT coalesce(sum(total_amount), 0) FROM shop.orders WHERE status = 'PAID'",
    "orders_by_status": (
        "SELECT json_object_agg(status, n) FROM "
        "(SELECT status, count(*) AS n FROM shop.orders GROUP BY status) s"
    ),
    "guest_orders": "SELECT count(*) FROM shop.orders WHERE customer_id IS NULL",
    "undelivered_orders": "SELECT count(*) FROM shop.orders WHERE delivered_on IS NULL",
    "march_2026_orders_ist": (
        "SELECT count(*) FROM shop.orders WHERE ordered_at >= TIMESTAMPTZ '2026-03-01 00:00:00+05:30' "
        "AND ordered_at < TIMESTAMPTZ '2026-04-01 00:00:00+05:30'"
    ),
    "march_2026_orders_utc": (
        "SELECT count(*) FROM shop.orders WHERE ordered_at >= TIMESTAMPTZ '2026-03-01 00:00:00+00' "
        "AND ordered_at < TIMESTAMPTZ '2026-04-01 00:00:00+00'"
    ),
    "order_items_total": "SELECT count(*) FROM shop.order_items",
    "refunds_total": "SELECT count(*) FROM shop.refunds",
    "refunds_amount": "SELECT coalesce(sum(amount), 0) FROM shop.refunds",
    "padded_enterprise_customers": "SELECT count(*) FROM shop.customers WHERE segment = 'enterprise '",
    "page_views_total": "SELECT count(*) FROM shop.page_views",
    "customers_without_phone": "SELECT count(*) FROM shop.customers WHERE phone IS NULL",
    "refunds_without_reason": "SELECT count(*) FROM shop.refunds WHERE reason IS NULL",
}


def _compute(
    *,
    customers_total: int,
    orders: list[tuple[str, int, dt.datetime, bool, bool]],   # status, total paise, ordered_at, guest, undelivered
    order_items_total: int,
    refunds: list[int],
    padded_enterprise: int,
    no_phone: int,
    no_reason: int,
) -> dict:
    by_status: dict[str, int] = {}
    paid = [o for o in orders if o[0] == "PAID"]
    for o in orders:
        by_status[o[0]] = by_status.get(o[0], 0) + 1

    def in_month(ts: dt.datetime, tz: dt.timezone, year: int, month: int) -> bool:
        local = ts.astimezone(tz)
        return (local.year, local.month) == (year, month)

    ist = dt.timezone(dt.timedelta(hours=5, minutes=30))
    return {
        "customers_total": customers_total,
        "orders_total": len(orders),
        "orders_paid_count": len(paid),
        "orders_paid_revenue": fmt_money(sum(o[1] for o in paid)),
        "orders_by_status": dict(sorted(by_status.items())),
        "guest_orders": sum(o[3] for o in orders),
        "undelivered_orders": sum(o[4] for o in orders),
        "march_2026_orders_ist": sum(in_month(o[2], ist, 2026, 3) for o in orders),
        "march_2026_orders_utc": sum(in_month(o[2], UTC, 2026, 3) for o in orders),
        "order_items_total": order_items_total,
        "refunds_total": len(refunds),
        "refunds_amount": fmt_money(sum(refunds)),
        "padded_enterprise_customers": padded_enterprise,
        "page_views_total": PAGE_VIEWS_ROWS,
        "customers_without_phone": no_phone,
        "refunds_without_reason": no_reason,
    }


def compute_from_dataset(ds: Dataset) -> dict:
    return _compute(
        customers_total=len(ds.customers),
        orders=[(o[2], o[6], o[4], o[1] is None, o[5] is None) for o in ds.orders],
        order_items_total=len(ds.order_items),
        refunds=[r[2] for r in ds.refunds],
        padded_enterprise=sum(c[6] == "enterprise " for c in ds.customers),
        no_phone=sum(c[3] is None for c in ds.customers),
        no_reason=sum(r[3] is None for r in ds.refunds),
    )


def _paise(text: str) -> int:
    whole, _, frac = text.partition(".")
    return int(whole) * 100 + int(frac.ljust(2, "0")[:2])


def _rows(path: Path):
    for line in path.read_bytes().decode("utf-8").split("\n"):
        if line:
            yield line.split("\t")


def compute_from_files(directory: Path) -> dict:
    """Parse the emitted COPY files from scratch (no shared code with ``render_copy``)."""
    orders = []
    for f in _rows(directory / "orders.copy"):
        orders.append((f[2], _paise(f[6]), dt.datetime.fromisoformat(f[4]), f[1] == r"\N", f[5] == r"\N"))
    customers = list(_rows(directory / "customers.copy"))
    return _compute(
        customers_total=len(customers),
        orders=orders,
        order_items_total=sum(1 for _ in _rows(directory / "order_items.copy")),
        refunds=[_paise(f[2]) for f in _rows(directory / "refunds.copy")],
        padded_enterprise=sum(1 for f in customers if f[6] == "enterprise "),
        no_phone=sum(1 for f in customers if f[3] == r"\N"),
        no_reason=sum(1 for f in _rows(directory / "refunds.copy") if f[3] == r"\N"),
    )
