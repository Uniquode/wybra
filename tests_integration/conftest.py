from __future__ import annotations

from collections.abc import Iterator
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
def postgresql_database_config(
    postgresql_service: PostgreSQLContainerService,
) -> Iterator[ContainerDatabaseConfig]:
    config = postgresql_service.runtime_config()
    try:
        yield config
    finally:
        cleanup_postgresql_target(config)


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
def mysql_database_config(
    mysql_service: MySQLContainerService,
) -> Iterator[ContainerDatabaseConfig]:
    config = mysql_service.runtime_config()
    try:
        yield config
    finally:
        cleanup_mysql_target(config)


@pytest.fixture
def mariadb_database_config(
    mariadb_service: MySQLContainerService,
) -> Iterator[ContainerDatabaseConfig]:
    config = mariadb_service.runtime_config()
    try:
        yield config
    finally:
        cleanup_mysql_target(config)


@pytest.fixture(scope="session")
def mssql_service() -> Iterator[SQLServerContainerService]:
    container, service = start_mssql_container(root=Path.cwd())
    try:
        yield service
    finally:
        container.stop()


@pytest.fixture
def mssql_database_config(
    mssql_service: SQLServerContainerService,
) -> Iterator[ContainerDatabaseConfig]:
    config = mssql_service.runtime_config()
    try:
        yield config
    finally:
        cleanup_mssql_target(config)
