"""Raw file → Polars DataFrame loader.

Polars is the primary engine; pandas is only the bridge for Excel because
polars does not have a native xlsx reader without xlsx2csv installed.
"""
from __future__ import annotations

from pathlib import Path

import polars as pl

from app.db.models.dataset import FileType


class UnreadableDatasetError(Exception):
    """Raised when a file cannot be parsed into a DataFrame."""


def load_dataset(path: Path, file_type: FileType) -> pl.DataFrame:
    if not path.exists():
        raise UnreadableDatasetError(f"file not found: {path}")

    try:
        if file_type is FileType.CSV:
            # Try comma first; fall back to tab on parse failure (matches .tsv intake).
            try:
                return pl.read_csv(path, infer_schema_length=10_000, try_parse_dates=True)
            except pl.exceptions.ComputeError:
                return pl.read_csv(
                    path, separator="\t", infer_schema_length=10_000, try_parse_dates=True
                )

        if file_type is FileType.PARQUET:
            return pl.read_parquet(path)

        if file_type is FileType.JSON:
            # ndjson vs single-document json — peek at the first non-whitespace char.
            with path.open("rb") as f:
                head = f.read(1024).lstrip()
            if head.startswith(b"["):
                return pl.read_json(path)
            return pl.read_ndjson(path)

        if file_type is FileType.XLSX:
            import pandas as pd

            pdf = pd.read_excel(path, engine="openpyxl")
            return pl.from_pandas(pdf)

    except UnreadableDatasetError:
        raise
    except Exception as exc:  # narrow exceptions vary across polars/pandas
        raise UnreadableDatasetError(
            f"could not parse {file_type.value} file: {exc}"
        ) from exc

    raise UnreadableDatasetError(f"unsupported file type: {file_type}")
