"""Schema profiling — column names, dtypes, nullability."""
from __future__ import annotations

import polars as pl

from agentic_engine.profiler.report import SchemaColumn, SchemaSection


_POLARS_TO_LOGICAL = {
    "Int8": "integer", "Int16": "integer", "Int32": "integer", "Int64": "integer",
    "UInt8": "integer", "UInt16": "integer", "UInt32": "integer", "UInt64": "integer",
    "Float32": "float", "Float64": "float",
    "Boolean": "boolean",
    "Utf8": "string", "String": "string",
    "Date": "date", "Datetime": "datetime", "Time": "time", "Duration": "duration",
    "List": "list", "Struct": "struct", "Object": "object",
}


def _logical_dtype(pl_dtype: pl.DataType) -> str:
    name = str(pl_dtype)
    # polars formats parameterised dtypes as "Datetime(time_unit='us', time_zone=None)"
    head = name.split("(", 1)[0]
    return _POLARS_TO_LOGICAL.get(head, head.lower())


def profile_schema(df: pl.DataFrame) -> SchemaSection:
    columns: list[SchemaColumn] = []
    for col in df.columns:
        series = df.get_column(col)
        columns.append(
            SchemaColumn(
                name=col,
                dtype=_logical_dtype(series.dtype),
                polars_dtype=str(series.dtype),
                nullable=series.null_count() > 0,
            )
        )
    return SchemaSection(
        row_count=df.height,
        col_count=df.width,
        columns=columns,
    )
