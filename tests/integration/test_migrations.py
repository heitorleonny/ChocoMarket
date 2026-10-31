"""The Alembic migrations build exactly the schema declared by the models."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext

from app.database import Base, create_database_engine

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _config(database_url: str) -> Config:
    # No ini file: fileConfig would reconfigure logging for the whole test run.
    config = Config()
    config.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    config.attributes["database_url"] = database_url
    return config


def test_upgrade_head_matches_models_and_is_repeatable(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'migrated.db'}"
    config = _config(database_url)

    command.upgrade(config, "head")
    command.upgrade(config, "head")  # already at head: must be a no-op

    engine = create_database_engine(database_url)
    try:
        with engine.connect() as connection:
            context = MigrationContext.configure(connection, opts={"compare_type": True})
            assert compare_metadata(context, Base.metadata) == []
            assert context.get_current_revision() is not None
    finally:
        engine.dispose()
