import ast
import importlib
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import pytest
from fastapi import FastAPI
from sqlalchemy import MetaData, text

from wevra import SiteCapabilityError
from wevra.config import MappingConfigSource
from wevra.db import DatabaseCapability
from wevra.db.capabilities import (
    DatabaseCapabilityError,
    SqlAlchemyDatabaseCapability,
)
from wevra.db.migrate import (
    DEFAULT_MIGRATIONS_SCRIPT_LOCATION,
    migration_script_location,
    migration_script_root,
)
from wevra.db.models import Base, metadata
from wevra.db.persistence import Database
from wevra.db.surfaces import (
    DataCompositionError,
    discover_migration_version_locations,
    discover_model_metadata,
    migration_version_locations_from_modules,
    model_package_name,
    model_packages_from_modules,
)
from wevra.db.urls import (
    parse_sqlite_database_url,
    redact_database_url,
    redact_database_urls,
    resolve_database_url,
    safe_database_error_message,
)
from wevra.db.validation import validate_persistence
from wevra.site import start
from wevra.tools.validation.core import ValidationResult


@dataclass(frozen=True, slots=True)
class _PersistenceSettings:
    database_url: str
    alembic_config: Path
    migrations_root: Path | None
    configured_modules: tuple[str, ...] = ()

    @property
    def modules(self) -> tuple[str, ...]:
        return self.configured_modules


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


def _write_alembic_config(path: Path, content: str | None = None) -> Path:
    path.write_text(
        content
        if content is not None
        else "[alembic]\nscript_location = wevra.db:migrations\n",
        encoding="utf-8",
    )
    return path


def _create_migration_root(root: Path) -> Path:
    versions_root = root / "versions"
    versions_root.mkdir(parents=True)
    (root / "env.py").write_text("", encoding="utf-8")
    (root / "script.py.mako").write_text("", encoding="utf-8")

    return root


def _persistence_settings(
    tmp_path: Path,
    *,
    database_url: str = "sqlite+aiosqlite:///local.sqlite3",
    alembic_config: Path | None = None,
    migrations_root: Path | None = None,
    modules: tuple[str, ...] = (),
) -> _PersistenceSettings:
    return _PersistenceSettings(
        database_url=database_url,
        alembic_config=alembic_config
        or _write_alembic_config(tmp_path / "alembic.ini"),
        migrations_root=migrations_root
        if migrations_root is not None
        else _create_migration_root(tmp_path / "migrations"),
        configured_modules=modules,
    )


def _failed_check_descriptions(result_errors: tuple[str, ...]) -> str:
    return "\n".join(result_errors)


def _database_config_source(tmp_path: Path) -> MappingConfigSource:
    return MappingConfigSource(
        {
            "app": {
                "modules": ("wevra.db",),
                "database_url": f"sqlite+aiosqlite:///{tmp_path / 'app.sqlite3'}",
            }
        }
    )


def test_wevra_db_package_imports() -> None:
    package = importlib.import_module("wevra.db")

    assert package.__name__ == "wevra.db"


@pytest.mark.anyio
async def test_wevra_db_setup_site_registers_database_capability(
    tmp_path: Path,
) -> None:
    site = await start(FastAPI(), config_source=_database_config_source(tmp_path))

    database = site.require_capability(DatabaseCapability)

    assert site.has_capability(DatabaseCapability) is True
    assert isinstance(database, DatabaseCapability)


@pytest.mark.anyio
async def test_wevra_db_setup_site_resolves_relative_database_url(
    tmp_path: Path,
) -> None:
    site = await start(
        FastAPI(),
        config_source=MappingConfigSource(
            {
                "app": {
                    "modules": ("wevra.db",),
                    "project_root": tmp_path,
                    "database_url": "sqlite+aiosqlite:///relative.sqlite3",
                }
            }
        ),
    )
    database = site.require_capability(DatabaseCapability)
    try:
        async with database.session() as session:
            await session.execute(text("CREATE TABLE runtime_probe (id INTEGER)"))
            await session.commit()

        assert (tmp_path / "relative.sqlite3").exists()
    finally:
        await database.close()


@pytest.mark.anyio
async def test_wevra_db_setup_site_requires_database_url() -> None:
    with pytest.raises(SiteCapabilityError, match="database_url"):
        await start(
            FastAPI(),
            config_source=MappingConfigSource({"app": {"modules": ("wevra.db",)}}),
        )


@pytest.mark.anyio
async def test_database_capability_provides_clean_sessions(
    tmp_path: Path,
) -> None:
    site = await start(FastAPI(), config_source=_database_config_source(tmp_path))
    database = site.require_capability(DatabaseCapability)
    try:
        async with database.session() as first_session:
            async with database.session() as second_session:
                assert first_session is not second_session
    finally:
        await database.close()


@pytest.mark.anyio
async def test_database_capability_transaction_commits_and_rolls_back(
    tmp_path: Path,
) -> None:
    site = await start(FastAPI(), config_source=_database_config_source(tmp_path))
    database = site.require_capability(DatabaseCapability)
    try:
        async with database.transaction() as session:
            await session.execute(text("create table records (value text not null)"))
            await session.execute(text("insert into records values ('committed')"))

        with pytest.raises(RuntimeError, match="rollback"):
            async with database.transaction() as session:
                await session.execute(
                    text("insert into records values ('rolled-back')")
                )
                raise RuntimeError("rollback")

        async with database.session() as session:
            rows = (
                await session.execute(text("select value from records order by value"))
            ).all()

        assert rows == [("committed",)]
    finally:
        await database.close()


@pytest.mark.anyio
async def test_database_capability_supports_named_connection_aliases(
    tmp_path: Path,
) -> None:
    site = await start(FastAPI(), config_source=_database_config_source(tmp_path))
    database = site.require_capability(DatabaseCapability)
    try:
        async with database.session("reader") as reader_session:
            async with database.transaction("writer") as writer_session:
                assert reader_session is not writer_session
    finally:
        await database.close()


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
            database.session("analytics")
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
        database.session()


@pytest.mark.anyio
async def test_database_capability_attempts_all_distinct_closes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_database = cast(Database, object())
    second_database = cast(Database, object())
    closed_databases: list[Database] = []

    async def close_or_fail(database: Database) -> None:
        closed_databases.append(database)
        if database is first_database:
            raise RuntimeError("close failed")

    monkeypatch.setattr(
        "wevra.db.capabilities.close_database",
        close_or_fail,
    )
    database = SqlAlchemyDatabaseCapability.from_connections(
        {
            "default": first_database,
            "reader": second_database,
            "writer": first_database,
        }
    )

    with pytest.raises(DatabaseCapabilityError, match="error_count=1"):
        await database.close()

    assert closed_databases == [first_database, second_database]


def test_wevra_db_modules_do_not_import_application_or_auth_packages() -> None:
    project_root = Path(__file__).resolve().parents[1]
    forbidden_modules = ("wevra.auth", "host_app")
    wevra_db_files = sorted((project_root / "src/wevra/db").rglob("*.py"))

    assert wevra_db_files
    for path in wevra_db_files:
        imported_modules = _imported_modules(path)
        assert not any(
            module == forbidden_module or module.startswith(f"{forbidden_module}.")
            for module in imported_modules
            for forbidden_module in forbidden_modules
        )


def test_wevra_db_package_is_included_in_build_modules() -> None:
    pyproject = tomllib.loads(
        (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text()
    )

    assert "wevra" in pyproject["tool"]["uv"]["build-backend"]["module-name"]


def test_wevra_db_models_expose_shared_metadata() -> None:
    assert metadata is Base.metadata


def test_wevra_db_owns_database_url_helpers(tmp_path: Path) -> None:
    database_url = resolve_database_url("sqlite+aiosqlite:///local.sqlite3", tmp_path)

    sqlite_url = parse_sqlite_database_url(database_url)

    assert sqlite_url is not None
    assert sqlite_url.path == tmp_path / "local.sqlite3"
    assert (
        redact_database_url("postgresql+asyncpg://user:password@host.example/app")
        == "postgresql+asyncpg://***:***@host.example/app"
    )


def test_wevra_db_owns_default_migration_script_location() -> None:
    script_root = migration_script_root()

    assert migration_script_location() == DEFAULT_MIGRATIONS_SCRIPT_LOCATION
    assert script_root.is_dir()
    assert script_root.joinpath("env.py").is_file()
    assert script_root.joinpath("script.py.mako").is_file()


@pytest.mark.parametrize(
    ("database_url", "expected_error"),
    (
        ("", "Database URL must not be empty."),
        (
            "ftp://example.com/database",
            "Database URL must use sqlite+aiosqlite:// or postgresql+asyncpg://.",
        ),
        (
            "sqlite+aiosqlite:///:memory:",
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

    assert expected_error in result.errors
    assert not result.is_ok


@pytest.mark.parametrize(
    ("config_content", "expected_error"),
    (
        (None, "Missing Alembic config:"),
        (
            "[alembic]\nsqlalchemy.url = sqlite+aiosqlite:///local.sqlite3\n",
            "Alembic config does not define script_location:",
        ),
        (
            "[alembic]\nscript_location = wevra.db:migrations\n"
            "sqlalchemy.url = sqlite+aiosqlite:///:memory:\n",
            "Alembic config must not force in-memory SQLite.",
        ),
    ),
)
def test_validate_persistence_reports_alembic_config_failures(
    tmp_path: Path,
    config_content: str | None,
    expected_error: str,
) -> None:
    alembic_config = tmp_path / "alembic.ini"
    if config_content is not None:
        _write_alembic_config(alembic_config, config_content)

    result = validate_persistence(
        _persistence_settings(tmp_path, alembic_config=alembic_config)
    )

    assert expected_error in _failed_check_descriptions(result.errors)
    assert not result.is_ok


def test_validate_persistence_fails_initialisation_when_migration_files_missing(
    tmp_path: Path,
) -> None:
    migrations_root = tmp_path / "migrations"
    migrations_root.mkdir()

    result = validate_persistence(
        _persistence_settings(tmp_path, migrations_root=migrations_root)
    )

    assert "Missing Alembic migration file:" in _failed_check_descriptions(
        result.errors
    )
    assert (
        "Development database initialisation requires Alembic config and migrations."
        in result.errors
    )
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
    assert (
        "Development database initialisation requires Alembic config and migrations."
        in result.errors
    )
    assert not _check_passed(
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
        "postgresql+asyncpg://user:password@host.example/app"
        "?sslmode=require&password=query-secret&token=abc&application_name=app%40local"
    ) == (
        "postgresql+asyncpg://***:***@host.example/app"
        "?sslmode=require&password=%2A%2A%2A&token=%2A%2A%2A"
        "&application_name=app%40local"
    )
    assert redact_database_url(
        "postgresql+asyncpg://host.example/app?api_key=secret&sslmode=require"
    ) == ("postgresql+asyncpg://host.example/app?api_key=%2A%2A%2A&sslmode=require")


def test_redact_database_urls_masks_bare_postgresql_urls_in_messages() -> None:
    assert redact_database_urls(
        "failed for postgresql://user:secret@host.example/app and "
        "postgresql+asyncpg://admin:admin-secret@host.example/postgres"
    ) == (
        "failed for postgresql://***:***@host.example/app and "
        "postgresql+asyncpg://***:***@host.example/postgres"
    )


def test_safe_database_error_message_redacts_database_urls() -> None:
    error = RuntimeError("failed for postgresql+asyncpg://user:secret@host.example/app")

    assert (
        safe_database_error_message(error)
        == "failed for postgresql+asyncpg://***:***@host.example/app"
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
        ("host_app", "wevra.auth")
    )

    assert len(version_locations) == 1
    assert version_locations[0].as_posix().endswith("wevra/auth/migrations/versions")
    assert discover_migration_version_locations("host_app") == ()


def test_model_packages_from_modules_uses_conventional_models_surface(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package_root = tmp_path / "models_surface_app"
    package_root.mkdir()
    (package_root / "__init__.py").write_text("", encoding="utf-8")
    (package_root / "models.py").write_text(
        "from sqlalchemy import MetaData\nmetadata = MetaData()\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()

    assert model_package_name("models_surface_app") == "models_surface_app.models"
    assert model_packages_from_modules(("models_surface_app",)) == (
        "models_surface_app.models",
    )
    assert isinstance(discover_model_metadata("models_surface_app"), MetaData)


def test_discover_model_metadata_rejects_malformed_present_surface(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package_root = tmp_path / "bad_models_surface_app"
    package_root.mkdir()
    (package_root / "__init__.py").write_text("", encoding="utf-8")
    (package_root / "models.py").write_text("metadata = object()\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()

    with pytest.raises(
        DataCompositionError,
        match="bad_models_surface_app.models.*must expose SQLAlchemy metadata",
    ):
        discover_model_metadata("bad_models_surface_app")
