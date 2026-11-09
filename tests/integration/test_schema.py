import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import Engine, create_engine, inspect, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from agentcrash.storage.schema import metadata

from .helpers import alembic_config, migrate

EXPECTED_TABLES = {
    "scenario_versions",
    "agent_versions",
    "experiments",
    "runs",
    "run_attempts",
    "jobs",
    "api_idempotency",
}


def test_empty_database_migrates_to_head(engine: Engine) -> None:
    tables = set(inspect(engine).get_table_names())
    assert tables >= EXPECTED_TABLES
    with engine.connect() as conn:
        assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar() == "0001"


def test_migration_matches_table_definitions(engine: Engine) -> None:
    """The hand-written DDL and storage.schema must not drift apart."""
    with engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), metadata)
    assert diff == []


def test_downgrade_and_upgrade_round_trip(database_url: object, engine: Engine) -> None:
    # Own throwaway engine on the same database; leaves it at head for other tests.
    cfg = alembic_config()
    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        command.downgrade(cfg, "base")
    assert not EXPECTED_TABLES & set(inspect(engine).get_table_names())
    migrate(engine)
    assert set(inspect(engine).get_table_names()) >= EXPECTED_TABLES


def insert_scenario(conn: object, sid: str = "s-one") -> None:
    conn.execute(  # type: ignore[attr-defined]
        text(
            "INSERT INTO scenario_versions (id, version, content_hash, schema_version, "
            "normalized_json) VALUES (:id, 1, :h, '1.0', '{}')"
        ),
        {"id": sid, "h": sid * 8},
    )


def test_scenario_versions_are_immutable_in_the_database(clean_engine: Engine) -> None:
    with clean_engine.begin() as conn:
        insert_scenario(conn)
    for statement in (
        "UPDATE scenario_versions SET schema_version = '2.0'",
        "DELETE FROM scenario_versions",
    ):
        with pytest.raises((IntegrityError, DBAPIError)), clean_engine.begin() as conn:
            conn.execute(text(statement))


def test_only_one_active_execute_job_per_run(clean_engine: Engine) -> None:
    with clean_engine.begin() as conn:
        insert_scenario(conn)
        conn.execute(text("INSERT INTO agent_versions VALUES ('a','1.0.0','a','h',NULL,now())"))
        conn.execute(
            text(
                "INSERT INTO runs (id, scenario_id, scenario_version, agent_id, agent_version, "
                "seed, manifest, manifest_hash) VALUES "
                "('00000000-0000-4000-8000-000000000001','s-one',1,'a','1.0.0',1,'{}','h')"
            )
        )
        job = (
            "INSERT INTO jobs (id, kind, run_id, dedupe_key, status) VALUES "
            "(gen_random_uuid(), 'execute', '00000000-0000-4000-8000-000000000001', :k, :s)"
        )
        conn.execute(text(job), {"k": "k1", "s": "queued"})
        conn.execute(text(job), {"k": "k2", "s": "done"})  # finished jobs do not count
    with pytest.raises(IntegrityError), clean_engine.begin() as conn:
        conn.execute(text(job), {"k": "k3", "s": "leased"})


def test_constraints_reject_bad_values(clean_engine: Engine) -> None:
    with clean_engine.begin() as conn:
        insert_scenario(conn)
        conn.execute(text("INSERT INTO agent_versions VALUES ('a','1.0.0','a','h',NULL,now())"))
    run = (
        "INSERT INTO runs (id, scenario_id, scenario_version, agent_id, agent_version, seed, "
        "manifest, manifest_hash{extra}) VALUES (gen_random_uuid(),'s-one',1,'a','1.0.0',{seed},"
        "'{{}}','h'{values})"
    )
    for seed, extra, values in (
        ("18446744073709551616", "", ""),  # seed beyond u64
        ("-1", "", ""),
        ("1", ", status", ", 'bogus'"),
        ("1", ", verdict", ", 'maybe'"),
    ):
        with pytest.raises(IntegrityError), clean_engine.begin() as conn:
            conn.execute(text(run.format(seed=seed, extra=extra, values=values)))
    with clean_engine.begin() as conn:  # the maximum u64 is accepted
        conn.execute(text(run.format(seed="18446744073709551615", extra="", values="")))


def test_unmigrated_database_reports_no_revision(database_url: object, settings: object) -> None:
    # A fresh empty database must report "not migrated" instead of erroring.
    import secrets

    from agentcrash.storage.store import PostgresStore

    name = f"agentcrash_empty_{secrets.token_hex(4)}"
    admin = create_engine(settings.database_url, isolation_level="AUTOCOMMIT")  # type: ignore[attr-defined]
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    try:
        eng = create_engine(settings.database_url.set(database=name))  # type: ignore[attr-defined]
        assert PostgresStore(eng).schema_revision() is None
        eng.dispose()
    finally:
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()
