"""Writes ``display_eligibility.json`` (SPEC-09 C4): hand-written cases that BOTH the Python and the JavaScript
implementations must satisfy. The expected verdicts are written here by hand, not derived from either implementation.

    python tests/fixtures/make_display_eligibility.py
"""
from __future__ import annotations

import json
from pathlib import Path

cases: list[dict] = []


def cell(kind, value=None):
    return {"kind": kind, "value": value}


def t(v):
    return cell("text", v) if v is not None else cell("null")


def i(v):
    return cell("integer", str(v)) if v is not None else cell("null")


def d(v):
    return cell("decimal", v) if v is not None else cell("null")


def f(v):
    return cell("number", v)


def date(v):
    return cell("date", v)


def ts(v):
    return cell("timestamp", v)


def tz(v):
    return cell("timestamptz", v)


def b(v):
    return cell("boolean", v)


def u(v):
    return cell("uuid", v)


NO = {"x": None, "y": [], "series": None, "label": None, "value": None}


def enc(**kw):
    return {**NO, **kw}


def case(name, display, encodings, columns, rows, ok, reason=None):
    cases.append({"name": name, "display": display, "encodings": encodings, "columns": columns, "rows": rows, "ok": ok, "reason": reason})


CAT_NUM = [["region", "text"], ["revenue", "numeric"]]
three = [[t("N"), d("1.5")], [t("S"), d("2")], [t("E"), d("3")]]

# always
for display in ("table", "narrative", "takeaways"):
    case(f"{display} always", display, NO, CAT_NUM, three, True)
    case(f"{display} with no rows", display, NO, CAT_NUM, [], True)
case("unknown display", "sparkline", NO, CAT_NUM, three, False, "unknown_display")

# kpi
case("kpi one numeric", "kpi", enc(value="n"), [["n", "int8"]], [[i(42)]], True)
case("kpi only column, no encoding", "kpi", NO, [["n", "int8"]], [[i(42)]], True)
case("kpi decimal", "kpi", enc(value="n"), [["n", "numeric"]], [[d("12345678901234567890.123456789")]], True)
case("kpi two rows", "kpi", enc(value="n"), [["n", "int8"]], [[i(1)], [i(2)]], False, "needs_one_row")
case("kpi no rows", "kpi", enc(value="n"), [["n", "int8"]], [], False, "needs_one_row")
case("kpi text value", "kpi", enc(value="n"), [["n", "text"]], [[t("x")]], False, "value_not_numeric")
case("kpi null value", "kpi", enc(value="n"), [["n", "int8"]], [[i(None)]], False, "missing_value")
case("kpi two columns no encoding", "kpi", NO, [["a", "int8"], ["b", "int8"]], [[i(1), i(2)]], False, "value_column_missing")
case("kpi unknown label", "kpi", enc(value="zzz"), [["n", "int8"]], [[i(1)]], False, "value_column_missing")

# kpi_comparison
two = [["cur", "numeric"], ["prev", "numeric"]]
case("kpi_comparison ok", "kpi_comparison", enc(y=["cur", "prev"]), two, [[d("12"), d("10")]], True)
case("kpi_comparison one y", "kpi_comparison", enc(y=["cur"]), two, [[d("12"), d("10")]], False, "needs_two_numeric_columns")
case("kpi_comparison three y", "kpi_comparison", enc(y=["cur", "prev", "cur"]), two, [[d("12"), d("10")]], False, "needs_two_numeric_columns")
case("kpi_comparison unknown", "kpi_comparison", enc(y=["cur", "zzz"]), two, [[d("12"), d("10")]], False, "unknown_label")
case("kpi_comparison text", "kpi_comparison", enc(y=["cur", "prev"]), [["cur", "numeric"], ["prev", "text"]], [[d("1"), t("x")]], False, "value_not_numeric")
case("kpi_comparison null previous", "kpi_comparison", enc(y=["cur", "prev"]), two, [[d("12"), d(None)]], False, "missing_value")
case("kpi_comparison two rows", "kpi_comparison", enc(y=["cur", "prev"]), two, [[d("1"), d("2")], [d("3"), d("4")]], False, "needs_one_row")

# ranked_list
case("ranked_list ok", "ranked_list", enc(label="region", value="revenue"), CAT_NUM, three, True)
case("ranked_list bool and uuid labels", "ranked_list", enc(label="k", value="v"), [["k", "bool"], ["v", "int8"]], [[b(True), i(1)], [b(False), i(2)]], True)
case("ranked_list uuid label", "ranked_list", enc(label="k", value="v"), [["k", "uuid"], ["v", "int8"]], [[u("00000000-0000-0000-0000-000000000001"), i(1)]], True)
case("ranked_list 100 rows", "ranked_list", enc(label="region", value="revenue"), CAT_NUM, [[t(f"r{n}"), d("1")] for n in range(100)], True)
case("ranked_list 101 rows", "ranked_list", enc(label="region", value="revenue"), CAT_NUM, [[t(f"r{n}"), d("1")] for n in range(101)], False, "too_many_rows")
case("ranked_list numeric label", "ranked_list", enc(label="revenue", value="revenue"), CAT_NUM, three, False, "label_not_category")
case("ranked_list text value", "ranked_list", enc(label="region", value="region"), CAT_NUM, three, False, "value_not_numeric")
case("ranked_list unknown", "ranked_list", enc(label="zzz", value="revenue"), CAT_NUM, three, False, "unknown_label")
case("ranked_list null value", "ranked_list", enc(label="region", value="revenue"), CAT_NUM, [[t("a"), d(None)]], False, "missing_value")
case("ranked_list null label", "ranked_list", enc(label="region", value="revenue"), CAT_NUM, [[t(None), d("1")]], False, "missing_value")
case("ranked_list no rows", "ranked_list", enc(label="region", value="revenue"), CAT_NUM, [], False, "empty_result")
case("ranked_list exact decimals beyond plotting", "ranked_list", enc(label="region", value="revenue"), CAT_NUM, [[t("a"), d("12345678901234567890.12345")]], True)

# bar
BAR = enc(x="region", y=["revenue"])
case("bar ok", "bar", BAR, CAT_NUM, three, True)
case("bar encodings optional", "bar", NO, CAT_NUM, three, True)
case("bar 100 rows", "bar", BAR, CAT_NUM, [[t(f"r{n}"), d("1")] for n in range(100)], True)
case("bar 101 rows", "bar", BAR, CAT_NUM, [[t(f"r{n}"), d("1")] for n in range(101)], False, "too_many_rows")
case("bar three columns", "bar", BAR, CAT_NUM + [["extra", "int8"]], [[t("a"), d("1"), i(1)]], False, "result_shape")
case("bar numeric x", "bar", NO, [["a", "int8"], ["b", "int8"]], [[i(1), i(2)]], False, "x_not_category")
case("bar text y", "bar", NO, [["a", "text"], ["b", "text"]], [[t("x"), t("y")]], False, "value_not_numeric")
case("bar encodings mismatch", "bar", enc(x="revenue", y=["region"]), CAT_NUM, three, False, "encodings_mismatch")
case("bar duplicate category", "bar", BAR, CAT_NUM, [[t("a"), d("1")], [t("a"), d("2")]], False, "duplicate_category")
case("bar null category", "bar", BAR, CAT_NUM, [[t(None), d("1")]], False, "missing_value")
case("bar null value", "bar", BAR, CAT_NUM, [[t("a"), d(None)]], False, "missing_value")
case("bar 15 significant digits", "bar", BAR, CAT_NUM, [[t("a"), d("123456789012345")]], True)
case("bar 16 significant digits", "bar", BAR, CAT_NUM, [[t("a"), d("1234567890123456")]], False, "numeric_precision_exceeds_plot_policy")
case("bar magnitude too big", "bar", BAR, CAT_NUM, [[t("a"), d("2000000000000000")]], False, "numeric_magnitude_outside_plot_policy")
case("bar magnitude too small", "bar", BAR, CAT_NUM, [[t("a"), d("0.0000000000001")]], False, "numeric_magnitude_outside_plot_policy")
case("bar zero", "bar", BAR, CAT_NUM, [[t("a"), d("0")], [t("b"), d("0.000")]], True)
case("bar float kind", "bar", BAR, [["region", "text"], ["revenue", "float8"]], [[t("a"), f("1.5")]], True)
case("bar boolean category", "bar", NO, [["k", "bool"], ["v", "int8"]], [[b(True), i(1)], [b(False), i(2)]], True)
case("bar equal values are fine", "bar", BAR, CAT_NUM, [[t("a"), d("1.0")], [t("b"), d("1.00")]], True)
case("bar no rows", "bar", BAR, CAT_NUM, [], False, "empty_result")

# line / area
LINE = enc(x="d", y=["v"])
dv = [["d", "date"], ["v", "int8"]]
two_days = [[date("2026-01-01"), i(1)], [date("2026-01-02"), i(2)]]
for display in ("line", "area"):
    case(f"{display} dates", display, LINE, dv, two_days, True)
    case(f"{display} timestamps", display, LINE, [["d", "timestamp"], ["v", "int8"]], [[ts("2026-01-01 10:00:00"), i(1)], [ts("2026-01-01T11:00:00.5"), i(2)]], True)
    case(f"{display} timestamptz", display, LINE, [["d", "timestamptz"], ["v", "int8"]], [[tz("2026-01-01T10:00:00Z"), i(1)], [tz("2026-01-01T11:00:00+05:30"), i(2)]], True)
    case(f"{display} text x", display, NO, CAT_NUM, three, False, "x_not_temporal")
    case(f"{display} duplicate time", display, LINE, dv, [[date("2026-01-01"), i(1)], [date("2026-01-01"), i(2)]], False, "duplicate_time")
    case(f"{display} bad date", display, LINE, dv, [[date("2026-02-30"), i(1)]], False, "invalid_time")
    case(f"{display} bad timestamp", display, LINE, [["d", "timestamp"], ["v", "int8"]], [[ts("2026-01-01 25:00:00"), i(1)]], False, "invalid_time")
    case(f"{display} timestamptz without zone", display, LINE, [["d", "timestamptz"], ["v", "int8"]], [[tz("2026-01-01T10:00:00"), i(1)]], False, "invalid_time")
    case(f"{display} three columns", display, LINE, dv + [["w", "int8"]], [[date("2026-01-01"), i(1), i(2)]], False, "result_shape")
    case(f"{display} text y", display, NO, [["d", "date"], ["v", "text"]], [[date("2026-01-01"), t("x")]], False, "value_not_numeric")
    case(f"{display} encodings mismatch", display, enc(x="v", y=["d"]), dv, two_days, False, "encodings_mismatch")
    case(f"{display} 17 significant digits", display, LINE, [["d", "date"], ["v", "numeric"]], [[date("2026-01-01"), d("0.30000000000000004")]], False, "numeric_precision_exceeds_plot_policy")
    case(f"{display} collapsing values", display, LINE, [["d", "date"], ["v", "numeric"]], [[date("2026-01-01"), d("100000000000000")], [date("2026-01-02"), d("100000000000000.0")]], True)

# pie
PIE = enc(label="region", value="revenue")
case("pie six rows", "pie", PIE, CAT_NUM, [[t(c), d("1")] for c in "abcdef"], True)
case("pie seven rows", "pie", PIE, CAT_NUM, [[t(c), d("1")] for c in "abcdefg"], False, "too_many_rows")
case("pie negative", "pie", PIE, CAT_NUM, [[t("a"), d("-1")], [t("b"), d("2")]], False, "negative_value")
case("pie zero is fine", "pie", PIE, CAT_NUM, [[t("a"), d("0")], [t("b"), d("2")]], True)
case("pie null value", "pie", PIE, CAT_NUM, [[t("a"), d(None)]], False, "missing_value")
case("pie numeric label", "pie", enc(label="revenue", value="revenue"), CAT_NUM, three, False, "label_not_category")
case("pie unknown", "pie", enc(label="zzz", value="revenue"), CAT_NUM, three, False, "unknown_label")
case("pie too precise", "pie", PIE, CAT_NUM, [[t("a"), d("1.2345678901234567")]], False, "numeric_precision_exceeds_plot_policy")

# scatter
SC = enc(x="a", y=["b"])
nn = [["a", "int8"], ["b", "numeric"]]
case("scatter ok", "scatter", SC, nn, [[i(1), d("2.5")], [i(2), d("3")]], True)
case("scatter 500 rows", "scatter", SC, nn, [[i(n), d("1")] for n in range(500)], True)
case("scatter 501 rows", "scatter", SC, nn, [[i(n), d("1")] for n in range(501)], False, "too_many_rows")
case("scatter text x", "scatter", SC, [["a", "text"], ["b", "numeric"]], [[t("x"), d("1")]], False, "value_not_numeric")
case("scatter unknown", "scatter", enc(x="a", y=["zzz"]), nn, [[i(1), d("1")]], False, "unknown_label")
case("scatter null", "scatter", SC, nn, [[i(1), d(None)]], False, "missing_value")

# histogram
HI = enc(x="bin", y=["n"])
case("histogram text bins", "histogram", HI, [["bin", "text"], ["n", "int8"]], [[t("0-10"), i(3)], [t("10-20"), i(5)]], True)
case("histogram numeric bins", "histogram", HI, [["bin", "numeric"], ["n", "int8"]], [[d("0"), i(3)], [d("10"), i(5)]], True)
case("histogram date bins", "histogram", HI, [["bin", "date"], ["n", "int8"]], [[date("2026-01-01"), i(3)]], False, "x_not_category")
case("histogram 101 bins", "histogram", HI, [["bin", "text"], ["n", "int8"]], [[t(f"b{n}"), i(1)] for n in range(101)], False, "too_many_rows")
case("histogram text count", "histogram", HI, [["bin", "text"], ["n", "text"]], [[t("a"), t("b")]], False, "value_not_numeric")
case("histogram null bin", "histogram", HI, [["bin", "text"], ["n", "int8"]], [[t(None), i(1)]], False, "missing_value")

# grouped_bar
GB = enc(x="r", series="s", y=["v"])
rsv = [["r", "text"], ["s", "text"], ["v", "int8"]]
case("grouped_bar ok", "grouped_bar", GB, rsv, [[t("a"), t("x"), i(1)], [t("a"), t("y"), i(2)], [t("b"), t("x"), i(3)]], True)
case("grouped_bar 12 series", "grouped_bar", GB, rsv, [[t("a"), t(f"s{n}"), i(1)] for n in range(12)], True)
case("grouped_bar 13 series", "grouped_bar", GB, rsv, [[t("a"), t(f"s{n}"), i(1)] for n in range(13)], False, "too_many_series")
case("grouped_bar 100 x", "grouped_bar", GB, rsv, [[t(f"r{n}"), t("x"), i(1)] for n in range(100)], True)
case("grouped_bar 101 x", "grouped_bar", GB, rsv, [[t(f"r{n}"), t("x"), i(1)] for n in range(101)], False, "too_many_rows")
case("grouped_bar duplicate pair", "grouped_bar", GB, rsv, [[t("a"), t("x"), i(1)], [t("a"), t("x"), i(2)]], False, "duplicate_pair")
case("grouped_bar numeric series", "grouped_bar", GB, [["r", "text"], ["s", "int8"], ["v", "int8"]], [[t("a"), i(1), i(1)]], False, "series_not_category")
case("grouped_bar numeric x", "grouped_bar", GB, [["r", "int8"], ["s", "text"], ["v", "int8"]], [[i(1), t("x"), i(1)]], False, "x_not_category")
case("grouped_bar null series", "grouped_bar", GB, rsv, [[t("a"), t(None), i(1)]], False, "missing_value")
case("grouped_bar text value", "grouped_bar", GB, [["r", "text"], ["s", "text"], ["v", "text"]], [[t("a"), t("x"), t("1")]], False, "value_not_numeric")
case("grouped_bar unknown", "grouped_bar", enc(x="r", series="zzz", y=["v"]), rsv, [[t("a"), t("x"), i(1)]], False, "unknown_label")

# multi_line
ML = enc(x="d", series="s", y=["v"])
dsv = [["d", "date"], ["s", "text"], ["v", "int8"]]
case("multi_line ok", "multi_line", ML, dsv, [[date("2026-01-01"), t("x"), i(1)], [date("2026-01-01"), t("y"), i(2)], [date("2026-01-02"), t("x"), i(3)]], True)
case("multi_line 8 series", "multi_line", ML, dsv, [[date("2026-01-01"), t(f"s{n}"), i(1)] for n in range(8)], True)
case("multi_line 9 series", "multi_line", ML, dsv, [[date("2026-01-01"), t(f"s{n}"), i(1)] for n in range(9)], False, "too_many_series")
case("multi_line duplicate pair", "multi_line", ML, dsv, [[date("2026-01-01"), t("x"), i(1)], [date("2026-01-01"), t("x"), i(2)]], False, "duplicate_pair")
case("multi_line text x", "multi_line", enc(x="r", series="s", y=["v"]), rsv, [[t("a"), t("x"), i(1)]], False, "x_not_temporal")
case("multi_line bad date", "multi_line", ML, dsv, [[date("2026-13-01"), t("x"), i(1)]], False, "invalid_time")

# heatmap
HM = enc(x="r", series="s", value="v")
case("heatmap ok", "heatmap", HM, rsv, [[t("a"), t("x"), i(1)], [t("b"), t("y"), i(2)]], True)
case("heatmap 50 by 50", "heatmap", HM, rsv, [[t(f"r{a}"), t(f"s{c}"), i(1)] for a in range(50) for c in range(50)], True)
case("heatmap 51 columns", "heatmap", HM, rsv, [[t("r"), t(f"s{c}"), i(1)] for c in range(51)], False, "too_many_series")
case("heatmap 51 rows", "heatmap", HM, rsv, [[t(f"r{a}"), t("s"), i(1)] for a in range(51)], False, "too_many_rows")
case("heatmap duplicate pair", "heatmap", HM, rsv, [[t("a"), t("x"), i(1)], [t("a"), t("x"), i(1)]], False, "duplicate_pair")
case("heatmap null value", "heatmap", HM, rsv, [[t("a"), t("x"), i(None)]], False, "missing_value")

# pivot
PV = enc(x="r", series="s", value="v")
case("pivot ok", "pivot", PV, rsv, [[t("a"), t("x"), i(1)], [t("a"), t("y"), i(2)]], True)
case("pivot numeric x", "pivot", PV, [["r", "int8"], ["s", "text"], ["v", "int8"]], [[i(1), t("x"), i(1)]], True)
case("pivot date x", "pivot", PV, [["r", "date"], ["s", "text"], ["v", "int8"]], [[date("2026-01-01"), t("x"), i(1)]], True)
case("pivot 30 series", "pivot", PV, rsv, [[t("a"), t(f"s{n}"), i(1)] for n in range(30)], True)
case("pivot 31 series", "pivot", PV, rsv, [[t("a"), t(f"s{n}"), i(1)] for n in range(31)], False, "too_many_series")
case("pivot duplicate pair", "pivot", PV, rsv, [[t("a"), t("x"), i(1)], [t("a"), t("x"), i(2)]], False, "duplicate_pair")
case("pivot not subject to the plotting policy", "pivot", PV, [["r", "text"], ["s", "text"], ["v", "numeric"]], [[t("a"), t("x"), d("12345678901234567890.123")]], True)
case("pivot null value is shown blank", "pivot", PV, rsv, [[t("a"), t("x"), i(None)]], True)
case("pivot text value", "pivot", PV, [["r", "text"], ["s", "text"], ["v", "text"]], [[t("a"), t("x"), t("1")]], False, "value_not_numeric")
case("pivot no rows", "pivot", PV, rsv, [], False, "empty_result")

path = Path(__file__).with_name("display_eligibility.json")
path.write_text(json.dumps({"version": 1, "cases": cases}, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
print(f"{len(cases)} cases -> {path}")
