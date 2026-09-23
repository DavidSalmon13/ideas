from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text

from opp.db.session import create_db_engine, create_session_factory

ROOT = Path(__file__).resolve().parents[1]


def test_sqlite_uses_wal_and_foreign_keys(tmp_path: Path) -> None:
    engine = create_db_engine(f"sqlite:///{tmp_path / 'nested' / 'opp.db'}")

    with engine.connect() as conn:
        assert conn.execute(text("PRAGMA journal_mode")).scalar() == "wal"
        assert conn.execute(text("PRAGMA foreign_keys")).scalar() == 1


def test_session_factory_runs_queries(tmp_path: Path) -> None:
    session_factory = create_session_factory(create_db_engine(f"sqlite:///{tmp_path / 'opp.db'}"))

    with session_factory() as session:
        assert session.execute(text("SELECT 1")).scalar() == 1


def test_alembic_upgrade_and_downgrade(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'opp.db'}"
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", url)
    config.attributes["configure_logger"] = False

    command.upgrade(config, "head")
    tables = inspect(create_db_engine(url)).get_table_names()
    assert "alembic_version" in tables

    command.downgrade(config, "base")
