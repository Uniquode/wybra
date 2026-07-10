import ast
import asyncio
import importlib
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import pytest
from fastapi import FastAPI
from tortoise.backends.base.client import BaseDBAsyncClient
from tortoise.models import Model

import wybra.db.migrate as migrate_module
import wybra.db.urls as database_urls
from support_database import sqlite_file_url
from wybra import SiteCapabilityError
from wybra.config import ConfigService, MappingConfigSource
from wybra.core.exceptions import ConfigurationError
from wybra.db import DatabaseCapability
from wybra.db.capabilities import (
    DatabaseCapabilityError,
    TortoiseDatabaseCapability,
)
from wybra.db.config import ENV_DATABASE_URL
from wybra.db.config import module_config as db_module_config
from wybra.db.models import Model as WybraModel
from wybra.db.persistence import (
    Database,
    close_database,
    close_database_connections,
    create_database,
)
from wybra.db.settings import (
    EffectiveDatabaseConfig,
    StructuredDatabaseConfig,
    resolve_database_connection_from_config,
    resolve_database_provisioning_connection_from_config,
)
from wybra.db.surfaces import (
    DataCompositionError,
    discover_migration_version_locations,
    discover_model_package,
    migration_version_location_for_configured_module,
    migration_version_locations_from_modules,
    model_package_name,
    model_packages_from_modules,
)
from wybra.db.urls import (
    available_database_url_schemes,
    database_url_support_error,
    is_supported_database_url,
    parse_sqlite_database_url,
    redact_database_url,
    redact_database_urls,
    resolve_database_url,
    safe_database_error_message,
    supported_database_url_schemes,
    tortoise_database_url,
)
from wybra.db.validation import validate_persistence
from wybra.services.secrets import SecretValue
from wybra.site import start
from wybra.tools.settings import load_project_settings
from wybra.tools.validation.core import ValidationResult


@dataclass(frozen=True, slots=True)
class _PersistenceSettings:
    database_url: str | None
    migrations_root: Path | None
    database_connection: object | None = None
    configured_modules: tuple[str, ...] = ()

    @property
    def modules(self) -> tuple[str, ...]:
        return self.configured_modules


@dataclass(frozen=True, slots=True)
class _MigrationCommandSettings:
    database_url: str
    project_root: Path
    migrations_root: Path | None = None
    app_config: None = None

    @property
    def modules(self) -> tuple[str, ...]:
        return ()


class _RecordingSecretsCapability:
    def __init__(self, values: dict[tuple[str, str], str]) -> None:
        self.values = values

    def resolve(self, source: str, key: str) -> SecretValue:
        return SecretValue(self.values[(source, key)], source=source, key=key)

    def exists(self, source: str, key: str) -> bool:
        return (source, key) in self.values


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imported_modules = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }
    imported_modules.update(
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    )
    return imported_modules


def _persistence_settings(
    tmp_path: Path,
    *,
    database_url: str | None = "sqlite:///local.sqlite3",
    database_connection: object | None = None,
    migrations_root: Path | None = None,
    modules: tuple[str, ...] = (),
) -> _PersistenceSettings:
    return _PersistenceSettings(
        database_url=database_url,
        database_connection=database_connection,
        migrations_root=migrations_root,
        configured_modules=modules,
    )


def _failed_check_descriptions(result_errors: tuple[str, ...]) -> str:
    return "\n".join(result_errors)


def _database_config_source(tmp_path: Path) -> MappingConfigSource:
    return MappingConfigSource(
        {
            "app": {
                "modules": ("wybra.db",),
                "database_url": sqlite_file_url(tmp_path / "app.sqlite3"),
            }
        }
    )


def _structured_database_config_source(
    tmp_path: Path,
    *,
    database: str = "structured.sqlite3",
) -> MappingConfigSource:
    return MappingConfigSource(
        {
            "app": {
                "modules": ("wybra.db",),
                "project_root": tmp_path,
            },
            "app.database": {
                "backend": "sqlite",
                "database": database,
            },
        }
    )


def test_wybra_db_package_imports() -> None:
    package = importlib.import_module("wybra.db")

    assert package.__name__ == "wybra.db"


@pytest.mark.anyio
async def test_wybra_db_setup_site_registers_database_capability(
    tmp_path: Path,
) -> None:
    site = await start(FastAPI(), config_source=_database_config_source(tmp_path))

    try:
        database = site.require_capability(DatabaseCapability)

        assert site.has_capability(DatabaseCapability) is True
        assert isinstance(database, DatabaseCapability)
    finally:
        await site.close()


@pytest.mark.anyio
async def test_database_capability_exposes_public_connection_helper(
    tmp_path: Path,
) -> None:
    site = await start(FastAPI(), config_source=_database_config_source(tmp_path))
    database = site.require_capability(DatabaseCapability)
    try:
        assert isinstance(database.connection(), BaseDBAsyncClient)
    finally:
        await database.close()


@pytest.mark.anyio
async def test_wybra_db_setup_site_resolves_relative_database_url(
    tmp_path: Path,
) -> None:
    site = await start(
        FastAPI(),
        config_source=MappingConfigSource(
            {
                "app": {
                    "modules": ("wybra.db",),
                    "project_root": tmp_path,
                    "database_url": "sqlite:///relative.sqlite3",
                }
            }
        ),
    )
    database = site.require_capability(DatabaseCapability)
    try:
        async with database.transaction() as connection:
            await connection.execute_script("CREATE TABLE runtime_probe (id INTEGER)")

        assert (tmp_path / "relative.sqlite3").exists()
    finally:
        await database.close()


@pytest.mark.anyio
async def test_wybra_db_setup_site_uses_structured_sqlite_config(
    tmp_path: Path,
) -> None:
    site = await start(
        FastAPI(),
        config_source=_structured_database_config_source(tmp_path),
    )
    database = site.require_capability(DatabaseCapability)
    try:
        async with database.transaction() as connection:
            await connection.execute_script(
                "CREATE TABLE structured_probe (id INTEGER)"
            )

        assert (tmp_path / "structured.sqlite3").exists()
    finally:
        await database.close()


@pytest.mark.anyio
async def test_wybra_db_setup_site_requires_database_url() -> None:
    with pytest.raises(SiteCapabilityError, match="database_url"):
        await start(
            FastAPI(),
            config_source=MappingConfigSource({"app": {"modules": ("wybra.db",)}}),
        )


def test_structured_database_config_builds_sqlite_connection(tmp_path: Path) -> None:
    config = ConfigService([_structured_database_config_source(tmp_path)])

    connection = resolve_database_connection_from_config(
        config,
        project_root=tmp_path,
    )

    assert connection is not None
    assert connection.tortoise_connection_config == {
        "engine": "tortoise.backends.sqlite",
        "credentials": {
            "file_path": (tmp_path / "structured.sqlite3").resolve().as_posix()
        },
    }


def test_structured_sqlite_config_resolves_relative_path_from_project_root(
    tmp_path: Path,
) -> None:
    config_directory = tmp_path / "config"
    project_root = tmp_path / "project"
    config = ConfigService(
        [
            MappingConfigSource(
                {
                    "app": {
                        "project_root": project_root,
                    },
                    "app.database": {
                        "backend": "sqlite",
                        "database": "structured.sqlite3",
                    },
                }
            )
        ],
        config_defs=(db_module_config,),
    )

    connection = resolve_database_connection_from_config(
        config,
        project_root=project_root,
    )

    assert config_directory != project_root
    assert connection is not None
    assert (
        connection.credentials["file_path"]
        == (project_root / "structured.sqlite3").resolve().as_posix()
    )


def test_structured_database_config_overrides_legacy_url_with_info_log(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    config = ConfigService(
        [
            MappingConfigSource(
                {
                    "app": {
                        "modules": ("wybra.db",),
                        "database_url": "sqlite:///legacy.sqlite3",
                    },
                    "app.database": {
                        "backend": "sqlite",
                        "database": "structured.sqlite3",
                    },
                }
            )
        ],
        config_defs=(db_module_config,),
    )

    with caplog.at_level("INFO", logger="wybra.db.settings"):
        connection = resolve_database_connection_from_config(
            config,
            project_root=tmp_path,
        )

    assert connection is not None
    assert connection.source == "structured"
    assert "overrides [app].database_url" in caplog.text


def test_database_url_environment_overrides_structured_database_config(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    env_url = sqlite_file_url(tmp_path / "env.sqlite3")
    ConfigService.set_runtime_environment({ENV_DATABASE_URL: env_url})
    config = ConfigService(
        [_structured_database_config_source(tmp_path)],
        config_defs=(db_module_config,),
    )

    with caplog.at_level("INFO", logger="wybra.db.settings"):
        connection = resolve_database_connection_from_config(
            config,
            project_root=tmp_path,
        )

    assert connection is not None
    assert connection.source == "url"
    assert connection.database_url == env_url
    assert "Using DATABASE_URL, overriding config" in caplog.text


def test_structured_database_config_resolves_environment_credentials(
    tmp_path: Path,
) -> None:
    ConfigService.set_runtime_environment(
        {
            "WYBRA_DB_USER": "app_user",
            "WYBRA_DB_PASSWORD": "app_password",
        }
    )
    config = ConfigService(
        [
            MappingConfigSource(
                {
                    "app.database": {
                        "backend": "postgresql",
                        "host": "/var/run/postgresql",
                        "database": "uniquode",
                        "credential_source": "environment",
                        "user_key": "WYBRA_DB_USER",
                        "password_key": "WYBRA_DB_PASSWORD",
                    }
                }
            )
        ],
        config_defs=(db_module_config,),
    )

    connection = resolve_database_connection_from_config(
        config,
        project_root=tmp_path,
    )

    assert connection is not None
    assert connection.tortoise_connection_config == {
        "engine": "tortoise.backends.asyncpg",
        "credentials": {
            "database": "uniquode",
            "host": "/var/run/postgresql",
            "user": "app_user",
            "password": "app_password",
        },
    }


def test_project_settings_resolve_environment_database_credentials(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "app.toml"
    config_path.write_text(
        """
        [app]
        modules = ["wybra.db"]

        [app.templates]
        auto_reload = false
        cache_size = 400

        [app.assets]
        url_path = "/static/"

        [app.database]
        backend = "postgresql"
        database = "uniquode"
        credential_source = "environment"
        user_key = "UNIQUODE_DB_USER"
        password_key = "UNIQUODE_DB_PASSWORD"
        """,
        encoding="utf-8",
    )

    settings = load_project_settings(
        project_root=tmp_path,
        environ={
            "APP_CONFIG": config_path.as_posix(),
            "UNIQUODE_DB_USER": "app_user",
            "UNIQUODE_DB_PASSWORD": "app_password",
        },
        read_dotenv=False,
    )

    assert settings.database_connection is not None
    assert settings.database_connection.tortoise_connection_config == {
        "engine": "tortoise.backends.asyncpg",
        "credentials": {
            "database": "uniquode",
            "user": "app_user",
            "password": "app_password",
        },
    }


def test_project_settings_resolve_service_account_database_credentials(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "app.toml"
    config_path.write_text(
        """
        [app]
        modules = ["wybra.db"]

        [app.templates]
        auto_reload = false
        cache_size = 400

        [app.assets]
        url_path = "/static/"

        [app.database]
        backend = "postgresql"
        database = "uniquode"
        user = "app_user"
        password = "app_password"
        sa_user = "admin_user"
        sa_password = "admin_password"
        """,
        encoding="utf-8",
    )

    settings = load_project_settings(
        project_root=tmp_path,
        environ={"APP_CONFIG": config_path.as_posix()},
        read_dotenv=False,
        database_credential_purpose="service_account",
    )

    assert settings.database_connection is not None
    assert settings.database_connection.tortoise_connection_config == {
        "engine": "tortoise.backends.asyncpg",
        "credentials": {
            "database": "uniquode",
            "user": "admin_user",
            "password": "admin_password",
        },
    }


def test_project_settings_service_account_can_fallback_to_runtime_credentials(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "app.toml"
    config_path.write_text(
        """
        [app]
        modules = ["wybra.db"]

        [app.templates]
        auto_reload = false
        cache_size = 400

        [app.assets]
        url_path = "/static/"

        [app.database]
        backend = "postgresql"
        database = "uniquode"
        user = "app_user"
        password = "app_password"
        """,
        encoding="utf-8",
    )

    settings = load_project_settings(
        project_root=tmp_path,
        environ={"APP_CONFIG": config_path.as_posix()},
        read_dotenv=False,
        database_credential_purpose="service_account",
        fallback_to_runtime_credentials=True,
    )

    assert settings.database_connection is not None
    assert settings.database_connection.tortoise_connection_config == {
        "engine": "tortoise.backends.asyncpg",
        "credentials": {
            "database": "uniquode",
            "user": "app_user",
            "password": "app_password",
        },
    }


def test_structured_database_config_resolves_secret_credentials(
    tmp_path: Path,
) -> None:
    ConfigService.set_runtime_environment({"WYBRA_DB_USER": "app_user"})
    config = ConfigService(
        [
            MappingConfigSource(
                {
                    "app.database": {
                        "backend": "postgresql",
                        "database": "uniquode",
                        "credential_source": "keychain",
                        "user_key": "database/app/user",
                        "password_key": "database/app/password",
                    }
                }
            )
        ],
        config_defs=(db_module_config,),
    )
    secrets = _RecordingSecretsCapability(
        {
            ("keychain", "database/app/user"): "app_user",
            ("keychain", "database/app/password"): "app_password",
        }
    )

    connection = resolve_database_connection_from_config(
        config,
        project_root=tmp_path,
        secrets=secrets,
    )

    assert connection is not None
    assert connection.credentials["user"] == "app_user"
    assert connection.credentials["password"] == "app_password"
    assert "app_password" not in repr(connection)
    assert "app_password" not in connection.redacted_description


def test_structured_database_config_exposes_credential_references() -> None:
    config = StructuredDatabaseConfig.from_values(
        {
            "backend": "postgresql",
            "database": "uniquode",
            "credential_source": "keychain",
            "user_key": "database/app/user",
            "password_key": "database/app/password",
            "sa_user_key": "database/app/service-account-user",
            "sa_password_key": "database/app/service-account-password",
        }
    )

    references = config.credential_references()

    assert [
        (
            reference.name,
            reference.key,
            reference.owner,
            reference.source,
            reference.required,
            reference.description,
        )
        for reference in references
    ] == [
        (
            "database-user",
            "database/app/user",
            "database",
            "keychain",
            True,
            "Configured runtime database username.",
        ),
        (
            "database-password",
            "database/app/password",
            "database",
            "keychain",
            True,
            "Configured runtime database password.",
        ),
        (
            "database-sa-user",
            "database/app/service-account-user",
            "database",
            "keychain",
            True,
            "Configured database service-account username for provisioning.",
        ),
        (
            "database-sa-password",
            "database/app/service-account-password",
            "database",
            "keychain",
            True,
            "Configured database service-account password for provisioning.",
        ),
    ]
    assert all(not hasattr(reference, "value") for reference in references)


def test_structured_database_config_defaults_keychain_credential_references() -> None:
    config = StructuredDatabaseConfig.from_values(
        {
            "backend": "postgresql",
            "database": "uniquode",
            "credential_source": "keychain",
        }
    )

    references = config.credential_references()

    assert {reference.name: reference.key for reference in references} == {
        "database-user": "database/uniquode/app/user",
        "database-password": "database/uniquode/app/password",
        "database-sa-user": "database/uniquode/service-account/user",
        "database-sa-password": ("database/uniquode/service-account/password"),
    }


def test_structured_database_config_default_keys_allow_unicode_database_name() -> None:
    config = StructuredDatabaseConfig.from_values(
        {
            "backend": "postgresql",
            "database": "uniquodé",
            "credential_source": "keychain",
        }
    )

    assert {
        reference.name: reference.key for reference in config.credential_references()
    }["database-user"] == "database/uniquodé/app/user"


@pytest.mark.parametrize("database", ("tenant/prod", "tenant prod", "tenant\nprod"))
def test_structured_database_config_rejects_unsafe_default_key_database_segment(
    database: str,
) -> None:
    with pytest.raises(
        ConfigurationError,
        match="Database name cannot be used in default credential keys",
    ):
        StructuredDatabaseConfig.from_values(
            {
                "backend": "postgresql",
                "database": database,
                "credential_source": "keychain",
            }
        )


def test_structured_database_config_allows_unsafe_name_with_explicit_keys() -> None:
    config = StructuredDatabaseConfig.from_values(
        {
            "backend": "postgresql",
            "database": "tenant/prod",
            "credential_source": "keychain",
            "user_key": "database/tenant-prod/app/user",
            "password_key": "database/tenant-prod/app/password",
            "sa_user_key": "database/tenant-prod/service-account/user",
            "sa_password_key": "database/tenant-prod/service-account/password",
        }
    )

    assert {
        reference.name: reference.key for reference in config.credential_references()
    }["database-user"] == "database/tenant-prod/app/user"


@pytest.mark.parametrize(
    ("credential_fields", "expected_keys"),
    (
        (
            {"user_key": "custom/app/user"},
            {
                "database-user": "custom/app/user",
                "database-password": "database/uniquode/app/password",
                "database-sa-user": ("database/uniquode/service-account/user"),
                "database-sa-password": ("database/uniquode/service-account/password"),
            },
        ),
        (
            {"sa_password_key": "custom/service-account/password"},
            {
                "database-user": "database/uniquode/app/user",
                "database-password": "database/uniquode/app/password",
                "database-sa-user": ("database/uniquode/service-account/user"),
                "database-sa-password": "custom/service-account/password",
            },
        ),
        (
            {"user": "plain_user", "password_key": "custom/app/password"},
            {
                "database-password": "custom/app/password",
                "database-sa-user": ("database/uniquode/service-account/user"),
                "database-sa-password": ("database/uniquode/service-account/password"),
            },
        ),
    ),
)
def test_structured_database_config_exposes_partial_credential_references(
    credential_fields: dict[str, str],
    expected_keys: dict[str, str],
) -> None:
    config = StructuredDatabaseConfig.from_values(
        {
            "backend": "postgresql",
            "database": "uniquode",
            "credential_source": "keychain",
            **credential_fields,
        }
    )

    assert {
        reference.name: reference.key for reference in config.credential_references()
    } == expected_keys


def test_structured_database_config_preserves_non_keychain_credential_source() -> None:
    config = StructuredDatabaseConfig.from_values(
        {
            "backend": "postgresql",
            "database": "uniquode",
            "credential_source": "environment",
            "user_key": "WYBRA_DB_USER",
            "password_key": "WYBRA_DB_PASSWORD",
        }
    )

    references = config.credential_references()

    assert [reference.name for reference in references] == [
        "database-user",
        "database-password",
    ]
    assert {reference.source for reference in references} == {"environment"}


def test_effective_database_config_delegates_credential_references(
    tmp_path: Path,
) -> None:
    structured = StructuredDatabaseConfig.from_values(
        {
            "backend": "postgresql",
            "database": "uniquode",
            "credential_source": "keychain",
            "user_key": "database/app/user",
        }
    )
    effective = EffectiveDatabaseConfig.from_structured(
        structured,
        project_root=tmp_path,
    )

    assert effective.credential_references() == structured.credential_references()


def test_effective_database_url_config_exposes_no_credential_references(
    tmp_path: Path,
) -> None:
    effective = EffectiveDatabaseConfig.from_url(
        "postgresql://app_user:secret@db.example/uniquode",
        project_root=tmp_path,
    )

    assert effective.credential_references() == ()


def test_structured_sqlite_config_exposes_no_credential_references() -> None:
    config = StructuredDatabaseConfig.from_values(
        {
            "backend": "sqlite",
            "database": "local.sqlite3",
            "credential_source": "keychain",
            "user_key": "database/app/user",
            "password_key": "database/app/password",
        }
    )

    assert config.credential_references() == ()


def test_structured_database_config_rejects_plain_and_key_credentials(
    tmp_path: Path,
) -> None:
    config = ConfigService(
        [
            MappingConfigSource(
                {
                    "app.database": {
                        "backend": "postgresql",
                        "database": "uniquode",
                        "credential_source": "environment",
                        "user": "plain_user",
                        "user_key": "WYBRA_DB_USER",
                    }
                }
            )
        ],
        config_defs=(db_module_config,),
    )

    with pytest.raises(ConfigurationError, match="mutually exclusive"):
        resolve_database_connection_from_config(config, project_root=tmp_path)


def test_structured_database_config_ignores_keys_without_credential_source(
    tmp_path: Path,
) -> None:
    config = ConfigService(
        [
            MappingConfigSource(
                {
                    "app.database": {
                        "backend": "postgresql",
                        "database": "uniquode",
                        "user": "plain_user",
                        "user_key": "WYBRA_DB_USER",
                        "password_key": "WYBRA_DB_PASSWORD",
                    }
                }
            )
        ],
        config_defs=(db_module_config,),
    )

    connection = resolve_database_connection_from_config(config, project_root=tmp_path)

    assert connection is not None
    assert connection.credentials == {
        "database": "uniquode",
        "user": "plain_user",
    }


def test_structured_database_config_rejects_credential_source_without_keys(
    tmp_path: Path,
) -> None:
    config = ConfigService(
        [
            MappingConfigSource(
                {
                    "app.database": {
                        "backend": "postgresql",
                        "database": "uniquode",
                        "credential_source": "environment",
                        "user": "database/app/user",
                        "password": "database/app/password",
                    }
                }
            )
        ],
        config_defs=(db_module_config,),
    )

    with pytest.raises(
        ConfigurationError,
        match="missing credential keys",
    ):
        resolve_database_connection_from_config(config, project_root=tmp_path)


@pytest.mark.parametrize(
    "credential_fields",
    (
        {"user": "app_user"},
        {"password": "app_password"},
        {"user": "app_user", "user_key": "WYBRA_DB_USER"},
        {"credential_source": "environment", "user_key": "WYBRA_DB_USER"},
        {"credential_source": "keychain", "user_key": "database/app/user"},
        {"sa_user": "admin_user"},
        {"sa_password": "admin_password"},
    ),
)
def test_structured_sqlite_config_ignores_credentials(
    tmp_path: Path,
    credential_fields: dict[str, str],
) -> None:
    config = ConfigService(
        [
            MappingConfigSource(
                {
                    "app.database": {
                        "backend": "sqlite",
                        "database": "structured.sqlite3",
                        **credential_fields,
                    }
                }
            )
        ],
        config_defs=(db_module_config,),
    )

    connection = resolve_database_connection_from_config(config, project_root=tmp_path)

    assert connection is not None
    assert connection.credentials == {
        "file_path": (tmp_path / "structured.sqlite3").resolve().as_posix()
    }


@pytest.mark.parametrize(
    "sqlite_fields",
    (
        {"host": "db.internal"},
        {"port": 5432},
        {"host": "db.internal", "port": 5432},
    ),
)
def test_structured_sqlite_config_ignores_network_fields(
    tmp_path: Path,
    sqlite_fields: dict[str, str | int],
) -> None:
    config = ConfigService(
        [
            MappingConfigSource(
                {
                    "app.database": {
                        "backend": "sqlite",
                        "database": "structured.sqlite3",
                        **sqlite_fields,
                    }
                }
            )
        ],
        config_defs=(db_module_config,),
    )

    connection = resolve_database_connection_from_config(config, project_root=tmp_path)

    assert connection is not None
    assert connection.credentials == {
        "file_path": (tmp_path / "structured.sqlite3").resolve().as_posix()
    }


def test_service_account_credentials_are_separate_from_runtime_credentials(
    tmp_path: Path,
) -> None:
    config = ConfigService(
        [
            MappingConfigSource(
                {
                    "app.database": {
                        "backend": "postgresql",
                        "database": "uniquode",
                        "user": "app_user",
                        "password": "app_password",
                        "sa_user": "admin_user",
                        "sa_password": "admin_password",
                    }
                }
            )
        ],
        config_defs=(db_module_config,),
    )

    runtime_connection = resolve_database_connection_from_config(
        config,
        project_root=tmp_path,
    )
    service_account_connection = resolve_database_connection_from_config(
        config,
        project_root=tmp_path,
        purpose="service_account",
    )

    assert runtime_connection is not None
    assert service_account_connection is not None
    assert runtime_connection.credentials["user"] == "app_user"
    assert runtime_connection.credentials["password"] == "app_password"
    assert service_account_connection.credentials["user"] == "admin_user"
    assert service_account_connection.credentials["password"] == "admin_password"


def test_runtime_connection_does_not_require_service_account_secret_source(
    tmp_path: Path,
) -> None:
    config = {
        "app.database": {
            "backend": "postgresql",
            "database": "uniquode",
            "credential_source": "keychain",
            "user": "app_user",
            "password": "app_password",
            "sa_user_key": "database/app/service-account-user",
            "sa_password_key": "database/app/service-account-password",
        }
    }

    connection = resolve_database_connection_from_config(
        config,
        project_root=tmp_path,
    )

    assert connection is not None
    assert connection.credentials["user"] == "app_user"
    assert connection.credentials["password"] == "app_password"


def test_service_account_connection_requires_secret_source_for_service_keys(
    tmp_path: Path,
) -> None:
    config = {
        "app.database": {
            "backend": "postgresql",
            "database": "uniquode",
            "credential_source": "keychain",
            "user": "app_user",
            "password": "app_password",
            "sa_user_key": "database/app/service-account-user",
            "sa_password_key": "database/app/service-account-password",
        }
    }

    with pytest.raises(
        ConfigurationError,
        match="SecretsCapability is required to resolve database credentials",
    ):
        resolve_database_connection_from_config(
            config,
            project_root=tmp_path,
            purpose="service_account",
        )


def test_provisioning_connection_resolves_service_account_keys(
    tmp_path: Path,
) -> None:
    ConfigService.set_runtime_environment(
        {
            "WYBRA_DB_ADMIN_USER": "admin_user",
            "WYBRA_DB_ADMIN_PASSWORD": "admin_password",
        }
    )
    config = ConfigService(
        [
            MappingConfigSource(
                {
                    "app.database": {
                        "backend": "postgresql",
                        "database": "uniquode",
                        "credential_source": "environment",
                        "user": "app_user",
                        "password": "app_password",
                        "sa_user_key": "WYBRA_DB_ADMIN_USER",
                        "sa_password_key": "WYBRA_DB_ADMIN_PASSWORD",
                    }
                }
            )
        ],
        config_defs=(db_module_config,),
    )

    connection = resolve_database_provisioning_connection_from_config(
        config,
        project_root=tmp_path,
    )

    assert connection.credentials["user"] == "admin_user"
    assert connection.credentials["password"] == "admin_password"


def test_provisioning_connection_rejects_runtime_credentials_only(
    tmp_path: Path,
) -> None:
    config = ConfigService(
        [
            MappingConfigSource(
                {
                    "app.database": {
                        "backend": "postgresql",
                        "database": "uniquode",
                        "user": "app_user",
                        "password": "app_password",
                    }
                }
            )
        ],
        config_defs=(db_module_config,),
    )

    with pytest.raises(ConfigurationError, match="service-account database user"):
        resolve_database_provisioning_connection_from_config(
            config,
            project_root=tmp_path,
        )


def test_provisioning_connection_rejects_sqlite_backend(
    tmp_path: Path,
) -> None:
    config = ConfigService(
        [_structured_database_config_source(tmp_path)],
        config_defs=(db_module_config,),
    )

    with pytest.raises(
        ConfigurationError,
        match="provisioning is not supported for the sqlite backend",
    ):
        resolve_database_provisioning_connection_from_config(
            config,
            project_root=tmp_path,
        )


def test_provisioning_connection_accepts_admin_database_url_override(
    tmp_path: Path,
) -> None:
    admin_url = "postgresql://admin:secret@db.example/uniquode"
    config = ConfigService([MappingConfigSource({})], config_defs=(db_module_config,))

    connection = resolve_database_provisioning_connection_from_config(
        config,
        project_root=tmp_path,
        admin_database_url=admin_url,
    )

    assert connection.source == "url"
    assert connection.database_url == admin_url


def test_provisioning_connection_rejects_application_database_url(
    tmp_path: Path,
) -> None:
    config = ConfigService(
        [
            MappingConfigSource(
                {
                    "app": {
                        "database_url": "postgresql://admin:secret@db.example/app",
                    }
                }
            )
        ],
        config_defs=(db_module_config,),
    )

    with pytest.raises(
        ConfigurationError,
        match="does not use application database_url configuration",
    ):
        resolve_database_provisioning_connection_from_config(
            config,
            project_root=tmp_path,
        )


@pytest.mark.anyio
async def test_database_capability_provides_clean_sessions(
    tmp_path: Path,
) -> None:
    site = await start(FastAPI(), config_source=_database_config_source(tmp_path))
    database = site.require_capability(DatabaseCapability)
    try:
        first_connection = database.connection()
        second_connection = database.connection()

        assert first_connection is second_connection
    finally:
        await database.close()


@pytest.mark.anyio
async def test_database_capability_transaction_commits_and_rolls_back(
    tmp_path: Path,
) -> None:
    site = await start(FastAPI(), config_source=_database_config_source(tmp_path))
    database = site.require_capability(DatabaseCapability)
    try:
        async with database.transaction() as connection:
            await connection.execute_script(
                "create table records (value text not null)"
            )
            await connection.execute_query(
                "insert into records values (?)",
                ["committed"],
            )

        with pytest.raises(RuntimeError, match="rollback"):
            async with database.transaction() as connection:
                await connection.execute_query(
                    "insert into records values (?)",
                    ["rolled-back"],
                )
                raise RuntimeError("rollback")

        _row_count, rows = await database.connection().execute_query(
            "select value from records order by value"
        )

        assert [row["value"] for row in rows] == ["committed"]
    finally:
        await database.close()


@pytest.mark.anyio
async def test_database_capability_supports_named_connection_aliases(
    tmp_path: Path,
) -> None:
    site = await start(FastAPI(), config_source=_database_config_source(tmp_path))
    database = site.require_capability(DatabaseCapability)
    try:
        reader_connection = database.connection("reader")
        assert reader_connection is database.connection("default")
        async with database.transaction("writer") as writer_connection:
            assert callable(writer_connection.execute_query)
    finally:
        await database.close()


@pytest.mark.anyio
async def test_close_database_connections_does_not_reconnect_cross_loop_client(
    tmp_path: Path,
) -> None:
    database = await create_database(
        sqlite_file_url(tmp_path / "loop-switch.sqlite3"),
        modules=("wybra.db",),
    )
    connection = database.connection()
    other_loop = asyncio.new_event_loop()
    try:
        connection._bound_loop = other_loop

        def fail_create_connection(_alias: str) -> BaseDBAsyncClient:
            raise AssertionError("close must not create replacement connections")

        database.context.connections._create_connection = fail_create_connection

        await close_database_connections(database)

        assert database._connections == []
        assert database.context.connections._copy_storage() == {}
    finally:
        other_loop.close()
        if database._connections:
            await close_database_connections(database)


@pytest.mark.anyio
async def test_close_database_connections_restores_connection_factory(
    tmp_path: Path,
) -> None:
    database = await create_database(
        sqlite_file_url(tmp_path / "restore-factory.sqlite3"),
        modules=("wybra.db",),
    )

    try:
        assert "_create_connection" in database.context.connections.__dict__

        await close_database_connections(database)

        assert "_create_connection" not in database.context.connections.__dict__
    finally:
        if database._connections:
            await close_database_connections(database)


@pytest.mark.anyio
async def test_create_close_database_cycles_do_not_rewrap_connection_factory(
    tmp_path: Path,
) -> None:
    for index in range(2):
        database = await create_database(
            sqlite_file_url(tmp_path / f"cycle-{index}.sqlite3"),
            modules=("wybra.db",),
        )

        try:
            database.connection()
            await close_database(database)

            assert "_create_connection" not in database.context.connections.__dict__
            assert database._connections == []
        finally:
            if database._connections:
                await close_database_connections(database)


@pytest.mark.anyio
async def test_database_capability_rejects_unknown_connection_name(
    tmp_path: Path,
) -> None:
    site = await start(FastAPI(), config_source=_database_config_source(tmp_path))
    database = site.require_capability(DatabaseCapability)
    try:
        with pytest.raises(
            DatabaseCapabilityError, match="Unknown database connection"
        ):
            database.connection("analytics")
    finally:
        await database.close()


@pytest.mark.anyio
async def test_database_capability_rejects_use_after_close(
    tmp_path: Path,
) -> None:
    site = await start(FastAPI(), config_source=_database_config_source(tmp_path))
    database = site.require_capability(DatabaseCapability)

    await database.close()

    with pytest.raises(DatabaseCapabilityError, match="Database capability is closed"):
        database.connection()


@pytest.mark.anyio
async def test_database_capability_attempts_all_distinct_closes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_database = cast(Database, object())
    closed_databases: list[Database] = []

    async def close_or_fail(database: Database) -> None:
        closed_databases.append(database)
        raise RuntimeError("close failed")

    monkeypatch.setattr(
        "wybra.db.capabilities.close_database",
        close_or_fail,
    )
    database = TortoiseDatabaseCapability(
        first_database,
        {"default": "default", "reader": "default", "writer": "default"},
    )

    with pytest.raises(DatabaseCapabilityError, match="error_count=1"):
        await database.close()

    assert closed_databases == [first_database]


def test_wybra_db_modules_do_not_import_application_or_auth_packages() -> None:
    project_root = Path(__file__).resolve().parents[1]
    forbidden_modules = ("wybra.auth", "host_app")
    wybra_db_files = sorted((project_root / "src/wybra/db").rglob("*.py"))

    assert wybra_db_files
    for path in wybra_db_files:
        imported_modules = _imported_modules(path)
        assert not any(
            module == forbidden_module or module.startswith(f"{forbidden_module}.")
            for module in imported_modules
            for forbidden_module in forbidden_modules
        )


def test_wybra_db_package_is_included_in_build_modules() -> None:
    pyproject = tomllib.loads(
        (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text()
    )

    assert "wybra" in pyproject["tool"]["uv"]["build-backend"]["module-name"]


def test_wybra_db_models_exposes_tortoise_model_base() -> None:
    assert WybraModel is Model


def test_wybra_db_owns_database_url_helpers(tmp_path: Path) -> None:
    database_url = resolve_database_url("sqlite:///local.sqlite3", tmp_path)

    sqlite_url = parse_sqlite_database_url(database_url)

    assert sqlite_url is not None
    assert sqlite_url.path == tmp_path / "local.sqlite3"
    assert sqlite_url.is_absolute is True
    assert (
        redact_database_url("postgresql://user:password@host.example/app")
        == "postgresql://***:***@host.example/app"
    )


def test_database_url_parser_handles_windows_absolute_sqlite_path() -> None:
    sqlite_url = parse_sqlite_database_url("sqlite:///C:/data/app.sqlite3")

    assert sqlite_url is not None
    assert sqlite_url.path.as_posix() == "C:/data/app.sqlite3"
    assert sqlite_url.is_absolute is True


@pytest.mark.parametrize(
    "database_url",
    (
        "sqlite:////tmp/absolute.sqlite3",
        "sqlite:///C:/data/app.sqlite3",
        "postgresql://user:password@example.test/app",
    ),
)
def test_resolve_database_url_leaves_absolute_and_non_sqlite_urls_unchanged(
    tmp_path: Path,
    database_url: str,
) -> None:
    assert resolve_database_url(database_url, tmp_path) == database_url


@pytest.mark.parametrize(
    ("database_url", "suffix"),
    (
        ("sqlite:///app.db", ""),
        ("sqlite:///app.db?cache=shared", "?cache=shared"),
        ("sqlite:///app.db?mode=rwc#fragment", "?mode=rwc#fragment"),
    ),
)
def test_resolve_database_url_preserves_relative_suffix(
    tmp_path: Path,
    database_url: str,
    suffix: str,
) -> None:
    assert resolve_database_url(database_url, tmp_path) == (
        f"{sqlite_file_url(tmp_path / 'app.db')}{suffix}"
    )


@pytest.mark.parametrize(
    ("database_url", "expected_error"),
    (
        ("", "Database URL must not be empty."),
        (
            "ftp://example.com/database",
            "Database URL must use a supported Tortoise database scheme:",
        ),
        (
            "sqlite://:memory:",
            "SQLite database URL must not force in-memory storage.",
        ),
    ),
)
def test_validate_persistence_reports_database_url_failures(
    tmp_path: Path,
    database_url: str,
    expected_error: str,
) -> None:
    result = validate_persistence(
        _persistence_settings(tmp_path, database_url=database_url)
    )

    assert any(expected_error in error for error in result.errors)
    assert not result.is_ok


def test_validate_persistence_reports_missing_database_connection(
    tmp_path: Path,
) -> None:
    result = validate_persistence(_persistence_settings(tmp_path, database_url=None))

    assert any(
        check.description == "database connection is not configured"
        and not check.passed
        for check in result.checks
    )
    assert "Database connection must be configured." in result.errors
    assert not result.is_ok


def test_supported_database_url_schemes_cover_tortoise_backends() -> None:
    assert supported_database_url_schemes() == (
        "sqlite",
        "postgresql",
        "postgres",
        "asyncpg",
        "psycopg",
        "mysql",
        "mssql",
        "oracle",
    )


def test_database_url_support_uses_available_backend_modules(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    available_modules = {"aiosqlite", "asyncpg", "asyncodbc", "pyodbc"}

    def find_spec(module_name: str):
        return object() if module_name in available_modules else None

    monkeypatch.setattr(database_urls.importlib.util, "find_spec", find_spec)

    assert available_database_url_schemes() == (
        "sqlite",
        "postgresql",
        "postgres",
        "asyncpg",
        "mssql",
        "oracle",
    )
    assert is_supported_database_url("postgresql://user:password@host.example/app")
    assert is_supported_database_url("mssql://user:password@host.example/app")
    assert not is_supported_database_url("mysql://user:password@host.example/app")


def test_database_url_support_error_names_missing_backend_extra(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    available_modules = {"aiosqlite"}

    def find_spec(module_name: str):
        return object() if module_name in available_modules else None

    monkeypatch.setattr(database_urls.importlib.util, "find_spec", find_spec)

    assert database_url_support_error(
        "postgresql://user:password@host.example/app"
    ) == (
        "Database URL scheme postgresql:// requires the wybra[postgresql] "
        "optional dependency."
    )


@pytest.mark.anyio
async def test_create_database_rejects_unavailable_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    available_modules = {"aiosqlite"}

    def find_spec(module_name: str):
        return object() if module_name in available_modules else None

    monkeypatch.setattr(database_urls.importlib.util, "find_spec", find_spec)

    with pytest.raises(
        ConfigurationError,
        match=r"Database URL scheme postgresql:// requires the wybra\[postgresql\]",
    ):
        await create_database(
            "postgresql://user:password@host.example/app",
            modules=(),
        )


@pytest.mark.parametrize(
    ("database_url", "expected"),
    (
        (
            "postgresql://user:password@host.example/app",
            "asyncpg://user:password@host.example/app",
        ),
        (
            "postgres://user:password@host.example/app",
            "asyncpg://user:password@host.example/app",
        ),
        (
            "asyncpg://user:password@host.example/app",
            "asyncpg://user:password@host.example/app",
        ),
        (
            "psycopg://user:password@host.example/app",
            "psycopg://user:password@host.example/app",
        ),
        (
            "mysql://user:password@host.example/app",
            "mysql://user:password@host.example/app",
        ),
        (
            "sqlite://:memory:",
            "sqlite://:memory:",
        ),
        (
            "sqlite:///app.db",
            "sqlite:///app.db",
        ),
        (
            "mssql://user:password@host.example/app",
            "mssql://user:password@host.example/app",
        ),
        (
            "oracle://user:password@host.example/app",
            "oracle://user:password@host.example/app",
        ),
        (
            "ftp://host.example/app",
            "ftp://host.example/app",
        ),
    ),
)
def test_tortoise_database_url_normalises_public_scheme(
    database_url: str,
    expected: str,
) -> None:
    assert tortoise_database_url(database_url) == expected


def test_validate_persistence_requires_tortoise_migration_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package_root = tmp_path / "models_with_empty_migrations"
    package_root.mkdir()
    (package_root / "__init__.py").write_text("", encoding="utf-8")
    (package_root / "models.py").write_text(
        (
            "from tortoise import fields\n"
            "from tortoise.models import Model\n\n"
            "class Example(Model):\n"
            "    id = fields.IntField(primary_key=True)\n"
        ),
        encoding="utf-8",
    )
    (package_root / "migrations").mkdir(parents=True)
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()

    result = validate_persistence(
        _persistence_settings(tmp_path, modules=("models_with_empty_migrations",))
    )

    assert "At least one Tortoise migration file is required." in result.errors
    assert "Development database initialisation requires migrations." in result.errors
    assert not _check_passed(
        result,
        "development database initialisation command is available",
    )


def test_validate_persistence_fails_initialisation_when_module_discovery_fails(
    tmp_path: Path,
) -> None:
    result = validate_persistence(
        _persistence_settings(tmp_path, modules=("missing_data_module",))
    )

    assert "Module migration version location discovery failed:" in (
        _failed_check_descriptions(result.errors)
    )
    assert "Development database initialisation requires migrations." in result.errors
    assert not _check_passed(
        result,
        "development database initialisation command is available",
    )


def test_validate_persistence_requires_migrations_for_configured_model_surface(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package_root = tmp_path / "models_without_migrations"
    package_root.mkdir()
    (package_root / "__init__.py").write_text("", encoding="utf-8")
    (package_root / "models.py").write_text(
        (
            "from tortoise import fields\n"
            "from tortoise.models import Model\n\n"
            "class Example(Model):\n"
            "    id = fields.IntField(primary_key=True)\n"
        ),
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()

    result = validate_persistence(
        _persistence_settings(tmp_path, modules=("models_without_migrations",))
    )

    assert (
        "At least one configured module migration version location is required."
        in result.errors
    )
    assert "Development database initialisation requires migrations." in result.errors
    assert not _check_passed(
        result,
        "development database initialisation command is available",
    )


def test_validate_persistence_accepts_configured_model_surface_with_migration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package_root = tmp_path / "models_with_migrations"
    package_root.mkdir()
    (package_root / "__init__.py").write_text("", encoding="utf-8")
    (package_root / "models.py").write_text(
        (
            "from tortoise import fields\n"
            "from tortoise.models import Model\n\n"
            "class Example(Model):\n"
            "    id = fields.IntField(primary_key=True)\n"
        ),
        encoding="utf-8",
    )
    migrations_root = package_root / "migrations"
    migrations_root.mkdir(parents=True)
    (migrations_root / "0001_initial.py").write_text(
        "revision = '0001'\ndown_revision = None\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()

    result = validate_persistence(
        _persistence_settings(tmp_path, modules=("models_with_migrations",))
    )

    assert (
        "At least one configured module migration version location is required."
        not in result.errors
    )
    assert "At least one Tortoise migration file is required." not in result.errors
    assert _check_passed(
        result,
        "development database initialisation command is available",
    )


def _check_passed(result: ValidationResult, description_prefix: str) -> bool:
    return any(
        check.description.startswith(description_prefix) and check.passed
        for check in result.checks
    )


def test_redact_database_url_masks_sensitive_query_parameters() -> None:
    assert redact_database_url(
        "postgresql://user:password@host.example/app"
        "?sslmode=require&password=query-secret&token=abc&application_name=app%40local"
    ) == (
        "postgresql://***:***@host.example/app"
        "?sslmode=require&password=%2A%2A%2A&token=%2A%2A%2A"
        "&application_name=app%40local"
    )
    assert redact_database_url(
        "postgresql://host.example/app?api_key=secret&sslmode=require"
    ) == ("postgresql://host.example/app?api_key=%2A%2A%2A&sslmode=require")


def test_redact_database_urls_masks_bare_postgresql_urls_in_messages() -> None:
    assert redact_database_urls(
        "failed for postgresql://user:secret@host.example/app and "
        "mysql://admin:admin-secret@host.example/app"
    ) == (
        "failed for postgresql://***:***@host.example/app and "
        "mysql://***:***@host.example/app"
    )


def test_safe_database_error_message_redacts_database_urls() -> None:
    error = RuntimeError("failed for postgresql://user:secret@host.example/app")

    assert (
        safe_database_error_message(error)
        == "failed for postgresql://***:***@host.example/app"
    )


def test_migration_version_locations_are_discovered_from_configured_modules(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package_root = tmp_path / "host_app"
    package_root.mkdir()
    (package_root / "__init__.py").write_text("", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()

    version_locations = migration_version_locations_from_modules(
        ("host_app", "wybra.auth")
    )

    assert len(version_locations) == 2
    assert version_locations[0].as_posix().endswith("wybra/sessions/migrations")
    assert version_locations[1].as_posix().endswith("wybra/auth/migrations")
    assert discover_migration_version_locations("host_app") == ()


def test_run_migration_dispatches_through_tortoise_backend_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _MigrationCommandSettings(
        database_url=sqlite_file_url(tmp_path / "dispatch.sqlite3"),
        project_root=tmp_path,
    )
    calls: list[migrate_module.MigrationContext] = []

    class RecordingMigrationBackend:
        def heads(
            self,
            context: migrate_module.MigrationContext,
            _app_labels: tuple[str, ...],
        ) -> None:
            calls.append(context)

    backend = RecordingMigrationBackend()
    monkeypatch.setattr(migrate_module, "TortoiseMigrationBackend", lambda: backend)

    result = migrate_module._run_migration(
        lambda _database_url: settings,
        None,
        None,
        lambda migration_backend, context: migration_backend.heads(context, ()),
    )

    assert result == 0
    assert len(calls) == 1
    assert calls[0].settings is settings


def test_core_sessions_revision_location_requires_module_config() -> None:
    with pytest.raises(DataCompositionError, match="wybra.sessions"):
        migration_version_location_for_configured_module(
            "wybra.sessions",
            (),
        )


def test_model_packages_from_modules_uses_conventional_models_surface(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package_root = tmp_path / "models_surface_app"
    package_root.mkdir()
    (package_root / "__init__.py").write_text("", encoding="utf-8")
    (package_root / "models.py").write_text(
        (
            "from tortoise import fields\n"
            "from tortoise.models import Model\n\n"
            "class Example(Model):\n"
            "    id = fields.IntField(primary_key=True)\n"
        ),
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()

    assert model_package_name("models_surface_app") == "models_surface_app.models"
    assert model_packages_from_modules(("models_surface_app",)) == (
        "wybra.sessions.models",
        "models_surface_app.models",
    )
    assert discover_model_package("models_surface_app") == "models_surface_app.models"


def test_discover_model_package_ignores_modules_without_tortoise_models(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package_root = tmp_path / "bad_models_surface_app"
    package_root.mkdir()
    (package_root / "__init__.py").write_text("", encoding="utf-8")
    (package_root / "models.py").write_text("metadata = object()\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()

    assert discover_model_package("bad_models_surface_app") is None
