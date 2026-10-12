"""Write/DDL request recognizer (SPEC-09 W0.1), shared by the Stage 4C planner and the NL->SQL pipeline.

Moved verbatim from ``agentic_engine/conversational/postgres_planner.py`` so that pass 1 no longer imports the old
planner module. No model is involved: a request that matches never reaches any LLM, on either route.
"""
from __future__ import annotations

import re

MUTATION_COMMAND_RE = re.compile(
    r"^\s*(?:(?:please|can\s+you|could\s+you|would\s+you)\s+)?"
    r"(?:delete|remove|update|insert|upsert|drop|truncate|create|alter|rename|"
    r"replace|grant|revoke|vacuum|analy[sz]e|reindex|cluster|copy|call|execute)\b",
    re.IGNORECASE,
)
SQL_MUTATION_RE = re.compile(
    r"\b(?:delete\s+from|update\s+(?:only\s+)?[^\n;]{1,240}?\s+set|"
    r"insert\s+into|upsert\s+into|drop\s+(?:table|schema|database|view)|"
    r"truncate\s+(?:table\s+)?|create\s+(?:table|schema|database|view)|"
    r"alter\s+(?:table|schema|database|view)|grant\s+.+?\s+on|"
    r"revoke\s+.+?\s+on)\b",
    re.IGNORECASE,
)


def is_write_or_ddl_request(question: str) -> bool:
    """Recognize mutation command shapes without rejecting analytical prose.

    Words such as ``updated``, ``removed``, and ``deleted`` commonly describe
    a perfectly valid row filter.  The structured plan/compiler remain the
    actual read-only execution boundary; this early check only produces a
    clearer response for recognizable DDL/DML requests.
    """

    return bool(MUTATION_COMMAND_RE.search(question) or SQL_MUTATION_RE.search(question))
