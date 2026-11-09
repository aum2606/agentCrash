"""Real-PostgreSQL integration harness.

Each session creates a throwaway database (random name, owned by this harness), migrates it
with Alembic, and drops it afterwards. If PostgreSQL is unreachable the tests FAIL: a missing
database must never look like a pass.
"""

import secrets
from collections.abc import Iterator

import pytest
from sqlalchemy import URL, Engine, create_engine, text

from agentcrash.settings import Settings, load_settings
from agentcrash.storage.engine import make_engine
from agentcrash.storage.store import PostgresStore

from .helpers import DATA_TABLES, ROOT, migrate


@pytest.fixture(scope="session")
def settings() -> Settings:
    try:
        return load_settings(ROOT)
    except ValueError as exc:  # no password configured
        pytest.fail(f"integration tests need PostgreSQL settings: {exc}", pytrace=False)


@pytest.fixture(scope="session")
def database_url(settings: Settings) -> Iterator[URL]:
    admin = create_engine(settings.database_url, isolation_level="AUTOCOMMIT")
    name = f"agentcrash_test_{secrets.token_hex(6)}"
    try:
        with admin.connect() as conn:
            conn.execute(text(f'CREATE DATABASE "{name}"'))
    except Exception as exc:
        pytest.fail(
            f"cannot reach PostgreSQL ({type(exc).__name__}); "
            "run `docker compose up -d --wait postgres`",
            pytrace=False,
        )
    try:
        yield settings.database_url.set(database=name)
    finally:
        with admin.connect() as conn:  # only ever the database created above
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


@pytest.fixture(scope="session")
def engine(database_url: URL) -> Iterator[Engine]:
    eng = make_engine(database_url)
    migrate(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def clean_engine(engine: Engine) -> Engine:
    """The migrated engine with all rows removed (TRUNCATE does not fire row triggers)."""
    with engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {DATA_TABLES} CASCADE"))
    return engine


@pytest.fixture
def store(clean_engine: Engine) -> PostgresStore:
    return PostgresStore(clean_engine)
