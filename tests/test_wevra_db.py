import ast
import importlib
import importlib.util
import tomllib
from pathlib import Path

import pytest
from sqlalchemy import MetaData

from wevra.db.models import Base, metadata
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
    resolve_database_url,
)


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


def test_wevra_db_package_imports() -> None:
    package = importlib.import_module("wevra.db")

    assert package.__name__ == "wevra.db"


def test_wevra_db_modules_do_not_import_application_or_auth_packages() -> None:
    project_root = Path(__file__).resolve().parents[1]
    forbidden_modules = ("wevra.auth", "uniquode")
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
    assert importlib.util.find_spec("uniquode.database_urls") is None
    assert importlib.util.find_spec("uniquode.persistence") is None


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


def test_migration_version_locations_are_discovered_from_configured_modules() -> None:
    version_locations = migration_version_locations_from_modules(
        ("uniquode", "wevra.auth")
    )

    assert len(version_locations) == 1
    assert version_locations[0].as_posix().endswith("wevra/auth/migrations/versions")
    assert discover_migration_version_locations("uniquode") == ()


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
