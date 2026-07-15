from __future__ import annotations

import logging
from functools import partial
from pathlib import Path

import anyio
import pytest
from tests_support.database_containers import (
    ContainerDatabaseConfig,
    assert_database_secrets_absent,
    postgresql_fetch_value,
)

from wybra.tools import migrate as tools_migrate


def test_postgresql_init_provisions_database_and_role(
    postgresql_database_config: ContainerDatabaseConfig,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path = postgresql_database_config.write_app_config(
        tmp_path / "wybra-it.toml"
    )

    caplog.set_level(logging.INFO, logger="wybra.db.migrate")
    exit_code = tools_migrate.main(["--config", config_path.as_posix(), "init"])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert_database_secrets_absent(caplog.text, postgresql_database_config)
    assert_database_secrets_absent(
        captured.out + captured.err,
        postgresql_database_config,
    )
    assert anyio.run(
        partial(
            postgresql_fetch_value,
            postgresql_database_config,
            "SELECT EXISTS (SELECT 1 FROM pg_database WHERE datname = $1)",
            postgresql_database_config.database,
            database=postgresql_database_config.service_database,
        )
    )
    assert anyio.run(
        partial(
            postgresql_fetch_value,
            postgresql_database_config,
            "SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = $1)",
            postgresql_database_config.runtime_user,
            database=postgresql_database_config.service_database,
        )
    )


def test_postgresql_migrate_applies_tortoise_migrations(
    postgresql_database_config: ContainerDatabaseConfig,
    tmp_path: Path,
) -> None:
    config_path = postgresql_database_config.write_app_config(
        tmp_path / "wybra-it.toml"
    )

    assert tools_migrate.main(["--config", config_path.as_posix(), "init"]) == 0
    assert tools_migrate.main(["--config", config_path.as_posix(), "migrate"]) == 0

    migration_count = anyio.run(
        postgresql_fetch_value,
        postgresql_database_config,
        "SELECT COUNT(*) FROM tortoise_migrations",
    )
    assert isinstance(migration_count, int)
    assert migration_count > 0


def test_postgresql_migrate_applies_auth_migrations(
    postgresql_database_config: ContainerDatabaseConfig,
    tmp_path: Path,
) -> None:
    config_path = postgresql_database_config.write_app_config(
        tmp_path / "wybra-auth-it.toml",
        modules=("wybra.sessions", "wybra.auth"),
    )

    assert tools_migrate.main(["--config", config_path.as_posix(), "init"]) == 0
    assert tools_migrate.main(["--config", config_path.as_posix(), "migrate"]) == 0

    auth_table = anyio.run(
        postgresql_fetch_value,
        postgresql_database_config,
        "SELECT to_regclass('identity_external_identity_link')",
    )
    assert auth_table == "identity_external_identity_link"
    external_identity_link_constraint = anyio.run(
        postgresql_fetch_value,
        postgresql_database_config,
        """
        SELECT EXISTS (
            SELECT 1
            FROM pg_constraint
            WHERE conrelid = 'identity_external_identity_link'::regclass
              AND contype = 'u'
              AND pg_get_constraintdef(oid) = 'UNIQUE (user_id, provider_id)'
        )
        """,
    )
    assert external_identity_link_constraint is True


def test_postgresql_tasks_list_safe_maintenance_metadata(
    postgresql_database_config: ContainerDatabaseConfig,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path = postgresql_database_config.write_app_config(
        tmp_path / "wybra-it.toml"
    )

    exit_code = tools_migrate.main(["--config", config_path.as_posix(), "tasks"])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "repair-privs: Reapply runtime role grants" in captured.out
    assert "migrations: Report Tortoise migration recorder state" in captured.out
    assert_database_secrets_absent(
        captured.out + captured.err, postgresql_database_config
    )


def test_postgresql_run_migrations_maintenance_task(
    postgresql_database_config: ContainerDatabaseConfig,
    tmp_path: Path,
) -> None:
    config_path = postgresql_database_config.write_app_config(
        tmp_path / "wybra-it.toml"
    )

    assert tools_migrate.main(["--config", config_path.as_posix(), "init"]) == 0
    assert tools_migrate.main(["--config", config_path.as_posix(), "migrate"]) == 0
    assert (
        tools_migrate.main(["--config", config_path.as_posix(), "run", "migrations"])
        == 0
    )


def test_postgresql_destroy_removes_disposable_database(
    postgresql_database_config: ContainerDatabaseConfig,
    tmp_path: Path,
) -> None:
    config_path = postgresql_database_config.write_app_config(
        tmp_path / "wybra-it.toml"
    )

    assert tools_migrate.main(["--config", config_path.as_posix(), "init"]) == 0
    assert (
        tools_migrate.main(
            [
                "--config",
                config_path.as_posix(),
                "destroy",
                "--confirm",
                postgresql_database_config.database,
            ]
        )
        == 0
    )
    assert (
        tools_migrate.main(
            [
                "--config",
                config_path.as_posix(),
                "destroy",
                "--confirm",
                postgresql_database_config.database,
            ]
        )
        == 0
    )

    database_exists = anyio.run(
        partial(
            postgresql_fetch_value,
            postgresql_database_config,
            "SELECT EXISTS (SELECT 1 FROM pg_database WHERE datname = $1)",
            postgresql_database_config.database,
            database=postgresql_database_config.service_database,
        )
    )
    assert database_exists is False
