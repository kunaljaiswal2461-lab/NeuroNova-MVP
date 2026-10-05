"""Regression checks for Alembic revision bookkeeping compatibility."""

from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory


BACKEND_ROOT = Path(__file__).resolve().parents[1]


def test_long_revision_identifier_is_preceded_by_version_column_widening() -> None:
    """A fresh PostgreSQL upgrade must widen Alembic's default VARCHAR(32) first."""

    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    script = ScriptDirectory.from_config(config)

    long_revision = script.get_revision("0012_confirmed_metric_definitions")
    widening_revision = script.get_revision("0011b_widen_alembic_version")

    assert long_revision is not None
    assert widening_revision is not None
    assert len(long_revision.revision) > 32
    assert long_revision.down_revision == widening_revision.revision
    assert widening_revision.down_revision == "0011_postgres_chat_bindings"
    assert script.get_heads() == ["0016_password_provenance"]
