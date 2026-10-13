"""A browser-ready backend over the eval fixture with a scripted model (SPEC-09 acceptance, QA scenarios).

    docker compose -f backend/evals/fixture/compose.yml run --rm --service-ports -p 8000:8000 runner python -m evals.dev_server
    # then, on the host: cd neuronova-frontend && VITE_API_BASE_URL=http://localhost:8000/api/v1 npm run dev

It builds the fixture owner, source and catalog, writes a stub script with one question per display type plus the repair,
clarification, privacy and hostile-value demos, and serves the real application on 0.0.0.0:8000 with
``NL_SQL_STUB_LLM_PATH`` pointing at that script. It prints the source id and a 12-hour access token to put in
``localStorage`` (``access_token``). The script exists only here: ``NL_SQL_STUB_LLM_PATH`` is refused when APP_ENV=prod.
Type the quoted question text (``dev:kpi``, ``dev:pie``, ...) into the chat.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

from evals import guards

NO = {"x": None, "y": [], "series": None, "label": None, "value": None}
MONTH = "date_trunc('month', ordered_at AT TIME ZONE 'Asia/Kolkata')::date"


def enc(**kw):
    return {**NO, **kw}


def envelope(sql, display, encodings, assumptions=("Scripted demo query",)):
    return {
        "kind": "sql", "sql": sql, "assumptions": list(assumptions), "tables_used": [],
        "output_intent": {"type": display, "encodings": encodings},
        "clarification": None, "reason_code": None, "reason": None,
    }


def spec(display, encodings, answer, takeaways=(), caveats=(), table_columns=()):
    return {"display": display, "encodings": encodings, "answer": answer, "takeaways": list(takeaways),
            "caveats": list(caveats), "table_columns": list(table_columns)}


# (trigger, display, sql, encodings, answer, takeaways)
DEMOS = [
    ("dev:kpi", "kpi", "SELECT count(*) AS orders FROM shop.orders", enc(value="orders"),
     "We have {{stat:orders.sum}} orders in total.", ["One row: {{rowcount}}"]),
    ("dev:compare", "kpi_comparison",
     "SELECT sum(CASE WHEN ordered_at >= TIMESTAMPTZ '2026-01-01 00:00:00+05:30' THEN total_amount ELSE 0 END) AS this_year, "
     "sum(CASE WHEN ordered_at >= TIMESTAMPTZ '2025-01-01 00:00:00+05:30' AND ordered_at < TIMESTAMPTZ '2026-01-01 00:00:00+05:30' "
     "THEN total_amount ELSE 0 END) AS last_year FROM shop.orders WHERE status = 'PAID'",
     enc(y=["this_year", "last_year"]), "Paid order value changed by {{delta:this_year,last_year}} ({{delta_pct:this_year,last_year}}).", []),
    ("dev:table", "table", "SELECT id, name, category, unit_price FROM shop.products ORDER BY id LIMIT 20", enc(),
     "Here are the first {{rowcount}} products.", []),
    ("dev:ranked", "ranked_list",
     "SELECT p.name, sum(i.quantity) AS units FROM shop.order_items i JOIN shop.products p ON p.id = i.product_id "
     "GROUP BY p.name ORDER BY units DESC LIMIT 10", enc(label="name", value="units"), "The ten best-selling products.", []),
    ("dev:bar", "bar", "SELECT channel, count(*) AS orders FROM shop.orders GROUP BY channel ORDER BY orders DESC",
     enc(x="channel", y=["orders"]), "Orders by channel.", []),
    ("dev:grouped", "grouped_bar", "SELECT channel, status, count(*) AS orders FROM shop.orders GROUP BY channel, status",
     enc(x="channel", series="status", y=["orders"]), "Orders by channel and status.", []),
    ("dev:line", "line", f"SELECT {MONTH} AS month, count(*) AS orders FROM shop.orders GROUP BY 1 ORDER BY 1",
     enc(x="month", y=["orders"]), "Orders per month.", []),
    ("dev:multiline", "multi_line", f"SELECT {MONTH} AS month, channel, count(*) AS orders FROM shop.orders GROUP BY 1, 2",
     enc(x="month", series="channel", y=["orders"]), "Monthly orders per channel.", []),
    ("dev:area", "area", f"SELECT {MONTH} AS month, sum(total_amount) AS revenue FROM shop.orders WHERE status = 'PAID' GROUP BY 1 ORDER BY 1",
     enc(x="month", y=["revenue"]), "Paid order value per month.", []),
    ("dev:pie", "pie", "SELECT channel, count(*) AS orders FROM shop.orders GROUP BY channel",
     enc(label="channel", value="orders"), "Share of orders by channel.", []),
    ("dev:sevenpie", "pie", "SELECT reason, count(*) AS refunds FROM shop.refunds GROUP BY reason",
     enc(label="reason", value="refunds"), "Refunds by reason, as a pie (it has more than six slices).", []),
    ("dev:scatter", "scatter",
     "SELECT p.unit_price, sum(i.quantity) AS units FROM shop.products p JOIN shop.order_items i ON i.product_id = p.id GROUP BY p.id, p.unit_price",
     enc(x="unit_price", y=["units"]), "Price against units sold.", []),
    ("dev:histogram", "histogram",
     "SELECT (floor(unit_price / 10000) * 10000)::int AS band, count(*) AS products FROM shop.products GROUP BY 1 ORDER BY 1",
     enc(x="band", y=["products"]), "Products per price band.", []),
    ("dev:heatmap", "heatmap", "SELECT channel, status, count(*) AS orders FROM shop.orders GROUP BY channel, status",
     enc(x="channel", series="status", value="orders"), "Orders by channel and status.", []),
    ("dev:pivot", "pivot", "SELECT channel, status, count(*) AS orders FROM shop.orders GROUP BY channel, status",
     enc(x="channel", series="status", value="orders"), "Orders by channel and status, pivoted.", []),
    ("dev:narrative", "narrative", "SELECT status, count(*) AS orders FROM shop.orders GROUP BY status", enc(),
     "There are {{stat:status.distinct}} different order statuses across {{stat:orders.sum}} orders.", []),
    ("dev:takeaways", "takeaways", "SELECT status, count(*) AS orders FROM shop.orders GROUP BY status", enc(),
     "Orders by status.", ["Most orders are paid.", "{{stat:orders.sum}} orders in all."]),
    ("dev:partial", "table", "SELECT id, ordered_at FROM shop.page_views LIMIT 5", enc(), "Page views sample.", []),
    ("dev:people", "table", "SELECT full_name, email, city FROM shop.customers LIMIT 25", enc(),
     "Here are {{rowcount}} customers.", []),
    ("dev:formula", "table", "SELECT '=SUM(1+1)' AS formula_cell, '-5' AS negative_text, 'ok' AS plain", enc(),
     "A row whose cells start with spreadsheet formula characters.", []),
    ("dev:longlabel", "bar",
     "SELECT category || ' — a deliberately very long label that must be truncated in the chart' AS category, count(*) AS products "
     "FROM shop.products GROUP BY category", enc(x="category", y=["products"]), "Long labels.", []),
]


def build_script() -> list[dict]:
    lines: list[dict] = []
    for trigger, display, sql, encodings, answer, takeaways in DEMOS:
        lines.append({"schema": "pass1_envelope", "match": trigger, "response": envelope(sql, display, encodings)})
        lines.append({"schema": "pass2_spec", "match": trigger, "response": spec(display, encodings, answer, takeaways)})
    good = "SELECT count(*) AS orders FROM shop.orders"
    lines += [
        {"schema": "pass1_envelope", "match": "dev:repair", "brief": False,
         "response": envelope("SELECT count_of_orders FROM shop.orders", "kpi", enc(value="orders"))},
        {"schema": "pass1_envelope", "match": "dev:repair", "brief": True, "response": envelope(good, "kpi", enc(value="orders"))},
        {"schema": "pass2_spec", "match": "dev:repair", "response": spec("kpi", enc(value="orders"), "Corrected: {{stat:orders.sum}} orders.")},
        {"schema": "pass1_envelope", "match": "dev:ask", "response": {
            "kind": "clarification", "sql": None, "assumptions": [], "tables_used": [], "output_intent": None,
            "clarification": "Do you mean gross or net revenue?", "reason_code": None, "reason": None}},
        {"schema": "pass1_envelope", "match": "dev:fail", "response": envelope(
            "SELECT no_such_column_zz FROM shop.orders", "table", enc())},
        {"schema": "pass1_envelope", "match": "dev:unavailable", "error": "unavailable"},
    ]
    return lines


async def prepare():
    from app.core.security import create_access_token
    from app.db.models.user import User
    from evals.bootstrap import bootstrap

    fixture = await bootstrap()
    async with fixture.session_factory() as db:
        user = await db.get(User, fixture.user_id)
    token = create_access_token(str(user.id), user.email, user.role.value, fixture.settings.jwt_signing_key, 12 * 60)
    return fixture, token


def main() -> int:
    guards.assert_fixture_environment()
    script = Path(tempfile.mkdtemp(prefix="nn-dev-stub-")) / "script.jsonl"
    script.write_text("\n".join(json.dumps(line) for line in build_script()) + "\n", encoding="utf-8")
    os.environ["NL_SQL_STUB_LLM_PATH"] = str(script)
    fixture, token = asyncio.run(prepare())
    print("source id :", fixture.source_id)
    print("token     :", token)
    print("chat URL  : http://localhost:5173/sources/%s/chat" % fixture.source_id)
    print("triggers  :", ", ".join(d[0] for d in DEMOS), "dev:repair dev:ask dev:fail dev:unavailable", flush=True)
    import uvicorn

    # The production connection policy refuses Docker's private addresses; use the fixture's disposable-test policy.
    from app.services import sql_execution

    sql_execution._build_adapter = lambda _settings: fixture.query_adapter_factory()

    sys.path.insert(0, "/app")
    from main import app

    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="warning")      # noqa: S104 - the disposable fixture network
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
