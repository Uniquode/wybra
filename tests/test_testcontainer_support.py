from __future__ import annotations

import builtins
import sys
from types import SimpleNamespace

import pytest
import tests_support.database_containers as database_containers
from tests_support.database_containers import (
    DEFAULT_MARIADB_IMAGE,
    DEFAULT_MSSQL_IMAGE,
    DEFAULT_MYSQL_IMAGE,
    DEFAULT_POSTGRES_IMAGE,
    MARIADB_IMAGE_ENV,
    MSSQL_IMAGE_ENV,
    MYSQL_IMAGE_ENV,
    POSTGRES_IMAGE_ENV,
    ContainerDatabaseConfig,
    ContainerImageConfig,
)


def test_container_image_config_uses_pinned_defaults(
    monkeypatch,
    tmp_path,
) -> None:
    for name in (
        POSTGRES_IMAGE_ENV,
        MYSQL_IMAGE_ENV,
        MARIADB_IMAGE_ENV,
        MSSQL_IMAGE_ENV,
    ):
        monkeypatch.delenv(name, raising=False)

    config = ContainerImageConfig.from_environment(tmp_path)

    assert config.postgres == DEFAULT_POSTGRES_IMAGE
    assert config.mysql == DEFAULT_MYSQL_IMAGE
    assert config.mariadb == DEFAULT_MARIADB_IMAGE
    assert config.mssql == DEFAULT_MSSQL_IMAGE


def test_container_image_config_uses_environment_override(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setenv(POSTGRES_IMAGE_ENV, "postgres:override")

    config = ContainerImageConfig.from_environment(tmp_path)

    assert config.postgres == "postgres:override"


def test_container_image_config_uses_dotenv_override(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.delenv(MYSQL_IMAGE_ENV, raising=False)
    (tmp_path / ".env").write_text(
        f"{MYSQL_IMAGE_ENV}=mysql:override\n",
        encoding="utf-8",
    )

    config = ContainerImageConfig.from_environment(tmp_path)

    assert config.mysql == "mysql:override"


def test_container_database_config_writes_structured_app_config(tmp_path) -> None:
    config = ContainerDatabaseConfig(
        backend="postgresql",
        host="127.0.0.1",
        port=54320,
        database="wybra_it",
        runtime_user="wybra_app",
        runtime_password="runtime-secret",
        service_user="postgres",
        service_password="service-secret",
        service_database="postgres",
        options={"TrustServerCertificate": "yes"},
    )

    config_path = config.write_app_config(tmp_path / "app.toml")

    content = config_path.read_text(encoding="utf-8")
    assert "database_url" not in content
    assert 'backend = "postgresql"' in content
    assert 'database = "wybra_it"' in content
    assert 'user = "wybra_app"' in content
    assert 'sa_user = "postgres"' in content
    assert "[app.database.options]" in content
    assert 'TrustServerCertificate = "yes"' in content
    assert "runtime-secret" in content
    assert "service-secret" in content


def test_container_database_config_redacts_metadata() -> None:
    config = ContainerDatabaseConfig(
        backend="postgresql",
        host="127.0.0.1",
        port=54320,
        database="wybra_it",
        runtime_user="wybra_app",
        runtime_password="runtime-secret",
        service_user="postgres",
        service_password="service-secret",
        service_database="postgres",
    )

    metadata = config.redacted_metadata

    assert metadata["runtime_password"] == "<redacted>"
    assert metadata["service_password"] == "<redacted>"
    assert metadata["runtime_user"] == "wy...pp"
    assert metadata["service_user"] == "po...es"


def test_docker_availability_reports_missing_client(monkeypatch) -> None:
    original_import = builtins.__import__

    def import_without_docker(
        name: str,
        *args: object,
        **kwargs: object,
    ) -> object:
        if name == "docker":
            raise ImportError("missing docker")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", import_without_docker)
    database_containers.docker_availability.cache_clear()

    try:
        available, reason = database_containers.docker_availability()
    finally:
        database_containers.docker_availability.cache_clear()

    assert not available
    assert reason == "Docker Python client is not installed."


def test_docker_availability_reports_ping_failure(monkeypatch) -> None:
    class FailingDockerClient:
        closed = False

        def ping(self) -> None:
            raise RuntimeError("daemon unavailable")

        def close(self) -> None:
            self.closed = True

    client = FailingDockerClient()
    monkeypatch.setitem(
        sys.modules,
        "docker",
        SimpleNamespace(from_env=lambda: client),
    )
    database_containers.docker_availability.cache_clear()

    try:
        available, reason = database_containers.docker_availability()
    finally:
        database_containers.docker_availability.cache_clear()

    assert not available
    assert reason == "Docker is unavailable for testcontainers: daemon unavailable"
    assert client.closed


def test_docker_availability_reports_success(monkeypatch) -> None:
    class AvailableDockerClient:
        closed = False

        def ping(self) -> None:
            return None

        def close(self) -> None:
            self.closed = True

    client = AvailableDockerClient()
    monkeypatch.setitem(
        sys.modules,
        "docker",
        SimpleNamespace(from_env=lambda: client),
    )
    database_containers.docker_availability.cache_clear()

    try:
        available, reason = database_containers.docker_availability()
    finally:
        database_containers.docker_availability.cache_clear()

    assert available
    assert reason == "Docker is available."
    assert client.closed


def test_skip_if_docker_unavailable_skips(monkeypatch) -> None:
    monkeypatch.setattr(
        database_containers,
        "docker_availability",
        lambda: (False, "Docker unavailable in test"),
    )

    with pytest.raises(pytest.skip.Exception, match="Docker unavailable in test"):
        database_containers.skip_if_docker_unavailable()


def test_skip_if_mssql_driver_unavailable_skips_import_errors(monkeypatch) -> None:
    def missing_driver_modules() -> tuple[object, object]:
        raise ImportError("missing asyncodbc")

    monkeypatch.setattr(
        database_containers,
        "_mssql_driver_modules",
        missing_driver_modules,
    )

    with pytest.raises(pytest.skip.Exception, match="ImportError"):
        database_containers.skip_if_mssql_driver_unavailable()


def test_skip_if_mssql_driver_unavailable_skips_missing_driver(monkeypatch) -> None:
    pyodbc = SimpleNamespace(drivers=lambda: ["Other Driver"])
    monkeypatch.setattr(
        database_containers,
        "_mssql_driver_modules",
        lambda: (object(), pyodbc),
    )

    with pytest.raises(pytest.skip.Exception, match="ODBC Driver 18 missing"):
        database_containers.skip_if_mssql_driver_unavailable()


def test_skip_if_mssql_driver_unavailable_accepts_available_driver(monkeypatch) -> None:
    pyodbc = SimpleNamespace(drivers=lambda: ["ODBC Driver 18 for SQL Server"])
    monkeypatch.setattr(
        database_containers,
        "_mssql_driver_modules",
        lambda: (object(), pyodbc),
    )

    database_containers.skip_if_mssql_driver_unavailable()


def test_mssql_dsn_uses_service_credentials() -> None:
    config = ContainerDatabaseConfig(
        backend="mssql",
        host="127.0.0.1",
        port=14330,
        database="wybra_it",
        runtime_user="wybra_app",
        runtime_password="runtime-secret",
        service_user="SA",
        service_password="service-secret",
        service_database="master",
    )

    dsn = database_containers._mssql_dsn(config, database="master")

    assert dsn == (
        "DRIVER={ODBC Driver 18 for SQL Server};"
        "SERVER={127.0.0.1,14330};"
        "DATABASE={master};"
        "UID={SA};"
        "PWD={service-secret};"
        "Encrypt={yes};"
        "TrustServerCertificate={yes}"
    )
