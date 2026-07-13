from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import pytest
from tests_support.database_containers import (
    ContainerDatabaseConfig,
    assert_database_secrets_absent,
    mssql_fetch_value,
)

from wybra.tools import migrate as tools_migrate


def test_mssql_init_provisions_database_and_login(
    mssql_database_config: ContainerDatabaseConfig,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path = mssql_database_config.write_app_config(tmp_path / "wybra-it.toml")

    caplog.set_level(logging.INFO)
    exit_code = tools_migrate.main(["--config", config_path.as_posix(), "init"])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert_database_secrets_absent(caplog.text, mssql_database_config)
    assert_database_secrets_absent(captured.out + captured.err, mssql_database_config)
    assert asyncio.run(
        mssql_fetch_value(
            mssql_database_config,
            "SELECT COUNT(*) FROM sys.databases WHERE name = ?",
            mssql_database_config.database,
            database=mssql_database_config.service_database,
        )
    )
    assert asyncio.run(
        mssql_fetch_value(
            mssql_database_config,
            "SELECT COUNT(*) FROM sys.server_principals WHERE name = ?",
            mssql_database_config.runtime_user,
            database=mssql_database_config.service_database,
        )
    )


def test_mssql_tasks_list_safe_maintenance_metadata(
    mssql_database_config: ContainerDatabaseConfig,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path = mssql_database_config.write_app_config(tmp_path / "wybra-it.toml")

    exit_code = tools_migrate.main(["--config", config_path.as_posix(), "tasks"])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "repair-privs: Reapply runtime user database role grants" in captured.out
    assert "migrations: Report Tortoise migration recorder state" in captured.out
    assert (
        "prerequisites: Report SQL Server external setup prerequisites" in captured.out
    )
    assert_database_secrets_absent(captured.out + captured.err, mssql_database_config)


def test_mssql_migrate_runs_lifecycle(
    mssql_database_config: ContainerDatabaseConfig,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path = mssql_database_config.write_app_config(tmp_path / "wybra-it.toml")

    assert tools_migrate.main(["--config", config_path.as_posix(), "init"]) == 0
    assert tools_migrate.main(["--config", config_path.as_posix(), "migrate"]) == 0
    assert (
        tools_migrate.main(["--config", config_path.as_posix(), "run", "migrations"])
        == 0
    )
    captured = capsys.readouterr()
    assert_database_secrets_absent(captured.out + captured.err, mssql_database_config)

    migration_count = asyncio.run(
        mssql_fetch_value(
            mssql_database_config,
            "SELECT COUNT(*) FROM tortoise_migrations",
        )
    )
    assert isinstance(migration_count, int)
    assert migration_count > 0


def test_mssql_destroy_removes_disposable_database(
    mssql_database_config: ContainerDatabaseConfig,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path = mssql_database_config.write_app_config(tmp_path / "wybra-it.toml")

    assert tools_migrate.main(["--config", config_path.as_posix(), "init"]) == 0
    assert (
        tools_migrate.main(
            [
                "--config",
                config_path.as_posix(),
                "destroy",
                "--confirm",
                mssql_database_config.database,
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
                mssql_database_config.database,
            ]
        )
        == 0
    )
    captured = capsys.readouterr()
    assert_database_secrets_absent(captured.out + captured.err, mssql_database_config)

    database_count = asyncio.run(
        mssql_fetch_value(
            mssql_database_config,
            "SELECT COUNT(*) FROM sys.databases WHERE name = ?",
            mssql_database_config.database,
            database=mssql_database_config.service_database,
        )
    )
    assert database_count == 0
