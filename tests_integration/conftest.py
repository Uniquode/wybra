from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from tests_support.database_containers import (
    ContainerDatabaseConfig,
    MySQLContainerService,
    PostgreSQLContainerService,
    SQLServerContainerService,
    cleanup_mssql_target,
    cleanup_mysql_target,
    cleanup_postgresql_target,
    start_mssql_container,
    start_mysql_container,
    start_postgresql_container,
)


@pytest.fixture(scope="session")
def postgresql_service() -> Iterator[PostgreSQLContainerService]:
    container, service = start_postgresql_container(root=Path.cwd())
    try:
        yield service
    finally:
        container.stop()


@pytest.fixture
async def postgresql_database_config(
    postgresql_service: PostgreSQLContainerService,
) -> AsyncIterator[ContainerDatabaseConfig]:
    config = postgresql_service.runtime_config()
    try:
        yield config
    finally:
        await cleanup_postgresql_target(config)


@pytest.fixture(scope="session")
def mysql_service() -> Iterator[MySQLContainerService]:
    container, service = start_mysql_container(backend="mysql", root=Path.cwd())
    try:
        yield service
    finally:
        container.stop()


@pytest.fixture(scope="session")
def mariadb_service() -> Iterator[MySQLContainerService]:
    container, service = start_mysql_container(backend="mariadb", root=Path.cwd())
    try:
        yield service
    finally:
        container.stop()


@pytest.fixture
async def mysql_compatible_database_config(
    request: pytest.FixtureRequest,
) -> AsyncIterator[ContainerDatabaseConfig]:
    backend = request.param
    if backend not in {"mysql", "mariadb"}:
        raise ValueError(f"Unsupported MySQL-compatible backend: {backend!r}.")
    service = request.getfixturevalue(f"{backend}_service")
    assert isinstance(service, MySQLContainerService)
    config = service.runtime_config()
    try:
        yield config
    finally:
        await cleanup_mysql_target(config)


@pytest.fixture(scope="session")
def mssql_service() -> Iterator[SQLServerContainerService]:
    container, service = start_mssql_container(root=Path.cwd())
    try:
        yield service
    finally:
        container.stop()


@pytest.fixture
async def mssql_database_config(
    mssql_service: SQLServerContainerService,
) -> AsyncIterator[ContainerDatabaseConfig]:
    config = mssql_service.runtime_config()
    try:
        yield config
    finally:
        await cleanup_mssql_target(config)
