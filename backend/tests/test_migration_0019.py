"""SPEC-09 L1-15: migration 0019 goes up, down and up again on SQLite (Postgres is covered by the live layer)."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import inspect

PATH = Path(__file__).resolve().parents[1] / "app/db/migrations/versions/0019_nl_sql_turns.py"


def _load():
    spec = importlib.util.spec_from_file_location("m0019", PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _columns(connection, table):
    return {c["name"] for c in inspect(connection).get_columns(table)}


def test_0019_goes_up_down_and_up_again():
    migration = _load()
    assert migration.revision == "0019_nl_sql_turns" and migration.down_revision == "0018_catalog_column_samples"
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        # The 0018 shape of the two tables that 0019 alters, plus the tables its foreign keys name.
        connection.execute(sa.text("CREATE TABLE users (id VARCHAR(36) PRIMARY KEY)"))
        connection.execute(sa.text(
            "CREATE TABLE data_sources (id VARCHAR(36) PRIMARY KEY, display_name VARCHAR(128) NOT NULL, "
            "sample_values_generation INTEGER NOT NULL DEFAULT 0)"
        ))
        connection.execute(sa.text("CREATE TABLE chat_messages (id VARCHAR(36) PRIMARY KEY, content TEXT NOT NULL)"))
        connection.execute(sa.text("INSERT INTO data_sources (id, display_name) VALUES ('s1', 'shop')"))
        connection.execute(sa.text("INSERT INTO chat_messages (id, content) VALUES ('m1', 'hello')"))

        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
        assert "nl_sql" in _columns(connection, "chat_messages")
        assert {"result_rows_to_model_enabled", "result_rows_to_model_generation"} <= _columns(connection, "data_sources")
        assert {"source_id", "schema_name", "table_name", "column_name", "state"} <= _columns(connection, "source_column_privacy")
        # Existing rows survive with consent off and generation 0 (owner decision 2).
        row = connection.execute(sa.text(
            "SELECT result_rows_to_model_enabled, result_rows_to_model_generation FROM data_sources"
        )).one()
        assert tuple(row) == (0, 0)
        assert connection.execute(sa.text("SELECT nl_sql FROM chat_messages")).scalar() is None
        assert connection.execute(sa.text("SELECT content FROM chat_messages")).scalar() == "hello"

        with Operations.context(MigrationContext.configure(connection)):
            migration.downgrade()
        assert "nl_sql" not in _columns(connection, "chat_messages")
        assert not {"result_rows_to_model_enabled", "result_rows_to_model_generation"} & _columns(connection, "data_sources")
        assert "source_column_privacy" not in inspect(connection).get_table_names()
        assert connection.execute(sa.text("SELECT display_name FROM data_sources")).scalar() == "shop"

        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
        assert "source_column_privacy" in inspect(connection).get_table_names()
    engine.dispose()
