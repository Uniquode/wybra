from __future__ import annotations

import logging
from pathlib import Path

import pytest
from tests_support.database_containers import (
    ContainerDatabaseConfig,
    assert_database_secrets_absent,
    mysql_fetch_value,
)
from tests_support.migration_lifecycle import (
    apply_migrations,
    destroy_database,
    initialise_migrations,
    list_maintenance_tasks,
    run_maintenance_task,
)


@pytest.mark.parametrize(
    ("fixture_name", "label"),
    (
        ("mysql_database_config", "MySQL"),
        ("mariadb_database_config", "MariaDB"),
    ),
)
@pytest.mark.anyio
async def test_mysql_compatible_init_provisions_database_and_user(
    fixture_name: str,
    label: str,
    request: pytest.FixtureRequest,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = _database_config(request, fixture_name)
    config_path = config.write_app_config(tmp_path / "wybra-it.toml")

    caplog.set_level(logging.INFO)
    exit_code = await initialise_migrations(config_path)

    captured = capsys.readouterr()
    assert exit_code == 0
    assert_database_secrets_absent(caplog.text, config)
    assert_database_secrets_absent(captured.out + captured.err, config)
    assert await mysql_fetch_value(
        config,
        "SELECT COUNT(*) FROM INFORMATION_SCHEMA.SCHEMATA WHERE SCHEMA_NAME = %s",
        config.database,
    )
    assert await mysql_fetch_value(
        config,
        "SELECT COUNT(*) FROM mysql.user WHERE User = %s",
        config.runtime_user,
    )
    assert label in {"MySQL", "MariaDB"}


@pytest.mark.parametrize(
    ("fixture_name", "label"),
    (
        ("mysql_database_config", "MySQL"),
        ("mariadb_database_config", "MariaDB"),
    ),
)
@pytest.mark.anyio
async def test_mysql_compatible_tasks_list_safe_maintenance_metadata(
    fixture_name: str,
    label: str,
    request: pytest.FixtureRequest,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = _database_config(request, fixture_name)
    config_path = config.write_app_config(tmp_path / "wybra-it.toml")

    exit_code = await list_maintenance_tasks(config_path)

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "repair-privs: Reapply runtime user database grants" in captured.out
    assert "migrations: Report Tortoise migration recorder state" in captured.out
    assert_database_secrets_absent(captured.out + captured.err, config)
    assert label in {"MySQL", "MariaDB"}


@pytest.mark.parametrize(
    "fixture_name",
    ("mysql_database_config", "mariadb_database_config"),
)
@pytest.mark.anyio
async def test_mysql_compatible_migrate_runs_lifecycle(
    fixture_name: str,
    request: pytest.FixtureRequest,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = _database_config(request, fixture_name)
    config_path = config.write_app_config(tmp_path / "wybra-it.toml")

    assert await initialise_migrations(config_path) == 0
    assert await apply_migrations(config_path) == 0
    assert await run_maintenance_task(config_path, "migrations") == 0
    captured = capsys.readouterr()
    assert_database_secrets_absent(captured.out + captured.err, config)

    migration_count = await mysql_fetch_value(
        config,
        "SELECT COUNT(*) FROM tortoise_migrations",
        database=config.database,
    )
    assert isinstance(migration_count, int)
    assert migration_count > 0


@pytest.mark.parametrize(
    "fixture_name",
    ("mysql_database_config", "mariadb_database_config"),
)
@pytest.mark.anyio
async def test_mysql_compatible_destroy_removes_disposable_database(
    fixture_name: str,
    request: pytest.FixtureRequest,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = _database_config(request, fixture_name)
    config_path = config.write_app_config(tmp_path / "wybra-it.toml")

    assert await initialise_migrations(config_path) == 0
    assert await destroy_database(config_path, config.database) == 0
    assert await destroy_database(config_path, config.database) == 0
    captured = capsys.readouterr()
    assert_database_secrets_absent(captured.out + captured.err, config)

    database_count = await mysql_fetch_value(
        config,
        "SELECT COUNT(*) FROM INFORMATION_SCHEMA.SCHEMATA WHERE SCHEMA_NAME = %s",
        config.database,
    )
    assert database_count == 0


def _database_config(
    request: pytest.FixtureRequest,
    fixture_name: str,
) -> ContainerDatabaseConfig:
    return request.getfixturevalue(fixture_name)
