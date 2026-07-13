from __future__ import annotations

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
