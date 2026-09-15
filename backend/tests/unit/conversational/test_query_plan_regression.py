"""Production query failures become regression tests in this module."""
from __future__ import annotations

import pytest

from agentic_engine.conversational.query_executor import (
    QueryValidationError,
    validate_expression,
)


def test_pl_len_with_dataframe_argument_is_rejected_before_execution():
    """Regression: the LLM generated ``pl.len(df.filter(...))``."""
    with pytest.raises(QueryValidationError, match=r"pl\.len expected 0 argument"):
        validate_expression("pl.len(df)")
