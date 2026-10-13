"""The stage4c candidate's typed-result decoder (the rest needs a model key and is not unit tested)."""
from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal

from evals.candidates.stage4c import decode_value


def test_decode_value_covers_every_executor_kind():
    u = "12345678-1234-5678-1234-567812345678"
    assert decode_value("null", None) is None
    assert decode_value("boolean", True) is True
    assert decode_value("integer", "42") == 42
    assert decode_value("decimal", "1.50") == Decimal("1.50")
    assert decode_value("number", "0.5") == 0.5
    assert decode_value("non_finite_number", "inf") == float("inf")
    assert decode_value("text", "PAID") == "PAID"
    assert decode_value("uuid", u) == uuid.UUID(u)
    assert decode_value("date", "2026-03-01") == dt.date(2026, 3, 1)
    assert decode_value("timestamp", "2026-03-01T00:00:00") == dt.datetime(2026, 3, 1)
    aware = decode_value("timestamptz", "2026-03-01T00:00:00+05:30")
    assert aware.tzinfo is not None and aware.utcoffset() == dt.timedelta(hours=5, minutes=30)
