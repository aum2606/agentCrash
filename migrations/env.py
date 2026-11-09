"""Alembic environment. Online migrations only (no SQL script generation)."""

from alembic import context
from sqlalchemy import create_engine

from agentcrash.settings import load_settings

config = context.config


def run_migrations_online() -> None:
    # A caller (tests, `agentcrash db upgrade`) may pass a live connection via
    # config.attributes["connection"]; otherwise connect using local settings.
    connection = config.attributes.get("connection")
    if connection is not None:
        context.configure(connection=connection)
        with context.begin_transaction():
            context.run_migrations()
        return
    engine = create_engine(load_settings().database_url)
    with engine.connect() as conn:
        context.configure(connection=conn)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    raise RuntimeError("Offline SQL generation is not supported; migrate a live database")
run_migrations_online()
