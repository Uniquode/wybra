from __future__ import annotations

import logging
from pathlib import Path

import pytest
from tests_support.database_containers import (
    ContainerDatabaseConfig,
    assert_database_secrets_absent,
    mssql_fetch_value,
)
from tests_support.migration_lifecycle import (
    apply_migrations,
    destroy_database,
    initialise_migrations,
    list_maintenance_tasks,
    run_maintenance_task,
)


@pytest.mark.anyio
async def test_mssql_init_provisions_database_and_login(
    mssql_database_config: ContainerDatabaseConfig,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path = mssql_database_config.write_app_config(tmp_path / "wybra-it.toml")

    caplog.set_level(logging.INFO)
    exit_code = await initialise_migrations(config_path)

    captured = capsys.readouterr()
    assert exit_code == 0
    assert_database_secrets_absent(caplog.text, mssql_database_config)
    assert_database_secrets_absent(captured.out + captured.err, mssql_database_config)
    assert await mssql_fetch_value(
        mssql_database_config,
        "SELECT COUNT(*) FROM sys.databases WHERE name = ?",
        mssql_database_config.database,
        database=mssql_database_config.service_database,
    )
    assert await mssql_fetch_value(
        mssql_database_config,
        "SELECT COUNT(*) FROM sys.server_principals WHERE name = ?",
        mssql_database_config.runtime_user,
        database=mssql_database_config.service_database,
    )


@pytest.mark.anyio
async def test_mssql_tasks_list_safe_maintenance_metadata(
    mssql_database_config: ContainerDatabaseConfig,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path = mssql_database_config.write_app_config(tmp_path / "wybra-it.toml")

    exit_code = await list_maintenance_tasks(config_path)

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "repair-privs: Reapply runtime user database role grants" in captured.out
    assert "migrations: Report Tortoise migration recorder state" in captured.out
    assert (
        "prerequisites: Report SQL Server external setup prerequisites" in captured.out
    )
    assert_database_secrets_absent(captured.out + captured.err, mssql_database_config)


@pytest.mark.anyio
async def test_mssql_migrate_runs_lifecycle(
    mssql_database_config: ContainerDatabaseConfig,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path = mssql_database_config.write_app_config(tmp_path / "wybra-it.toml")

    assert await initialise_migrations(config_path) == 0
    assert await apply_migrations(config_path) == 0
    assert await run_maintenance_task(config_path, "migrations") == 0
    captured = capsys.readouterr()
    assert_database_secrets_absent(captured.out + captured.err, mssql_database_config)

    migration_count = await mssql_fetch_value(
        mssql_database_config,
        "SELECT COUNT(*) FROM tortoise_migrations",
    )
    assert isinstance(migration_count, int)
    assert migration_count > 0


@pytest.mark.anyio
async def test_mssql_destroy_removes_disposable_database(
    mssql_database_config: ContainerDatabaseConfig,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path = mssql_database_config.write_app_config(tmp_path / "wybra-it.toml")

    assert await initialise_migrations(config_path) == 0
    assert await destroy_database(config_path, mssql_database_config.database) == 0
    assert await destroy_database(config_path, mssql_database_config.database) == 0
    captured = capsys.readouterr()
    assert_database_secrets_absent(captured.out + captured.err, mssql_database_config)

    database_count = await mssql_fetch_value(
        mssql_database_config,
        "SELECT COUNT(*) FROM sys.databases WHERE name = ?",
        mssql_database_config.database,
        database=mssql_database_config.service_database,
    )
    assert database_count == 0
