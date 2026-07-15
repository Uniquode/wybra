from __future__ import annotations

import logging
from pathlib import Path

import pytest
from tests_support.database_containers import (
    ContainerDatabaseConfig,
    assert_database_secrets_absent,
    postgresql_fetch_value,
)

from wybra.db import migrate as data_migrate
from wybra.tools import migrate as tools_migrate


async def _initialise_migrations(config_path: Path) -> int:
    return await tools_migrate.run_migration(
        None,
        config_source=config_path.as_posix(),
        operation=lambda backend, context: data_migrate.initialise_migration_lifecycle(
            backend,
            context,
            app_labels=(),
        ),
        include_provisioning_connection=True,
    )


async def _apply_migrations(config_path: Path) -> int:
    return await tools_migrate.run_migration(
        None,
        config_source=config_path.as_posix(),
        operation=lambda backend, context: backend.migrate(
            context,
            data_migrate.MigrationTargetRequest(
                app_label=None,
                migration=None,
                fake=False,
                dry_run=False,
            ),
        ),
        database_credential_purpose="service_account",
    )


async def _list_maintenance_tasks(config_path: Path) -> int:
    return await tools_migrate.run_migration(
        None,
        config_source=config_path.as_posix(),
        operation=data_migrate.list_database_maintenance_tasks_lifecycle,
        resolve_database_credentials=False,
    )


async def _run_maintenance_task(config_path: Path, task: str) -> int:
    return await tools_migrate.run_migration(
        None,
        config_source=config_path.as_posix(),
        operation=lambda _backend, context: (
            data_migrate.run_database_maintenance_lifecycle(
                context,
                data_migrate.DatabaseMaintenanceRequest(task=task, confirm=None),
            )
        ),
        include_provisioning_connection=True,
    )


async def _destroy_database(config_path: Path, confirm: str) -> int:
    return await tools_migrate.run_migration(
        None,
        config_source=config_path.as_posix(),
        operation=lambda _backend, context: data_migrate.destroy_database_lifecycle(
            context,
            data_migrate.DestroyDatabaseRequest(confirm=confirm),
        ),
        include_provisioning_connection=True,
    )


@pytest.mark.anyio
async def test_postgresql_init_provisions_database_and_role(
    postgresql_database_config: ContainerDatabaseConfig,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path = postgresql_database_config.write_app_config(
        tmp_path / "wybra-it.toml"
    )

    caplog.set_level(logging.INFO, logger="wybra.db.migrate")
    exit_code = await _initialise_migrations(config_path)

    captured = capsys.readouterr()
    assert exit_code == 0
    assert_database_secrets_absent(caplog.text, postgresql_database_config)
    assert_database_secrets_absent(
        captured.out + captured.err,
        postgresql_database_config,
    )
    assert await postgresql_fetch_value(
        postgresql_database_config,
        "SELECT EXISTS (SELECT 1 FROM pg_database WHERE datname = $1)",
        postgresql_database_config.database,
        database=postgresql_database_config.service_database,
    )
    assert await postgresql_fetch_value(
        postgresql_database_config,
        "SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = $1)",
        postgresql_database_config.runtime_user,
        database=postgresql_database_config.service_database,
    )


@pytest.mark.anyio
async def test_postgresql_migrate_applies_tortoise_migrations(
    postgresql_database_config: ContainerDatabaseConfig,
    tmp_path: Path,
) -> None:
    config_path = postgresql_database_config.write_app_config(
        tmp_path / "wybra-it.toml"
    )

    assert await _initialise_migrations(config_path) == 0
    assert await _apply_migrations(config_path) == 0

    migration_count = await postgresql_fetch_value(
        postgresql_database_config,
        "SELECT COUNT(*) FROM tortoise_migrations",
    )
    assert isinstance(migration_count, int)
    assert migration_count > 0


@pytest.mark.anyio
async def test_postgresql_migrate_applies_auth_migrations(
    postgresql_database_config: ContainerDatabaseConfig,
    tmp_path: Path,
) -> None:
    config_path = postgresql_database_config.write_app_config(
        tmp_path / "wybra-auth-it.toml",
        modules=("wybra.sessions", "wybra.auth"),
    )

    assert await _initialise_migrations(config_path) == 0
    assert await _apply_migrations(config_path) == 0

    auth_table = await postgresql_fetch_value(
        postgresql_database_config,
        "SELECT to_regclass('identity_external_identity_link')",
    )
    assert auth_table == "identity_external_identity_link"
    external_identity_link_constraint = await postgresql_fetch_value(
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


@pytest.mark.anyio
async def test_postgresql_tasks_list_safe_maintenance_metadata(
    postgresql_database_config: ContainerDatabaseConfig,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path = postgresql_database_config.write_app_config(
        tmp_path / "wybra-it.toml"
    )

    exit_code = await _list_maintenance_tasks(config_path)

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "repair-privs: Reapply runtime role grants" in captured.out
    assert "migrations: Report Tortoise migration recorder state" in captured.out
    assert_database_secrets_absent(
        captured.out + captured.err, postgresql_database_config
    )


@pytest.mark.anyio
async def test_postgresql_run_migrations_maintenance_task(
    postgresql_database_config: ContainerDatabaseConfig,
    tmp_path: Path,
) -> None:
    config_path = postgresql_database_config.write_app_config(
        tmp_path / "wybra-it.toml"
    )

    assert await _initialise_migrations(config_path) == 0
    assert await _apply_migrations(config_path) == 0
    assert await _run_maintenance_task(config_path, "migrations") == 0


@pytest.mark.anyio
async def test_postgresql_destroy_removes_disposable_database(
    postgresql_database_config: ContainerDatabaseConfig,
    tmp_path: Path,
) -> None:
    config_path = postgresql_database_config.write_app_config(
        tmp_path / "wybra-it.toml"
    )

    assert await _initialise_migrations(config_path) == 0
    assert (
        await _destroy_database(config_path, postgresql_database_config.database) == 0
    )
    assert (
        await _destroy_database(config_path, postgresql_database_config.database) == 0
    )

    database_exists = await postgresql_fetch_value(
        postgresql_database_config,
        "SELECT EXISTS (SELECT 1 FROM pg_database WHERE datname = $1)",
        postgresql_database_config.database,
        database=postgresql_database_config.service_database,
    )
    assert database_exists is False
