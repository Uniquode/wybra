import ast
import tomllib
from pathlib import Path

FASTAPI_USERS_IMPORTS = ("fastapi_users", "fastapi_users_db_sqlalchemy")
FASTAPI_USERS_DEPENDENCIES = ("fastapi-users", "fastapi-users-db-sqlalchemy")
SQLALCHEMY_IMPORT = "sqlalchemy"
ALEMBIC_IMPORT = "alembic"
PERSISTENCE_IMPORTS = (SQLALCHEMY_IMPORT, ALEMBIC_IMPORT)
PERSISTENCE_DEPENDENCIES = ("sqlalchemy", "alembic")


def test_project_dependencies_exclude_fastapi_users() -> None:
    project = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    dependencies = tuple(project["project"]["dependencies"])

    for dependency in dependencies:
        normalised_dependency = dependency.lower()
        assert not normalised_dependency.startswith(FASTAPI_USERS_DEPENDENCIES)


def test_project_dependencies_exclude_removed_persistence_packages() -> None:
    project = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    dependencies = tuple(project["project"]["dependencies"])

    for dependency in dependencies:
        normalised_dependency = dependency.lower()
        assert not normalised_dependency.startswith(PERSISTENCE_DEPENDENCIES)


def test_source_and_tests_do_not_import_fastapi_users() -> None:
    imported_modules: dict[Path, set[str]] = {}
    for root in (Path("src"), Path("tests")):
        for path in root.rglob("*.py"):
            imports = _imported_modules(path)
            blocked_imports = {
                imported
                for imported in imports
                if imported in FASTAPI_USERS_IMPORTS
                or imported.startswith(
                    tuple(f"{name}." for name in FASTAPI_USERS_IMPORTS)
                )
            }
            if blocked_imports:
                imported_modules[path] = blocked_imports

    assert imported_modules == {}


def test_source_does_not_import_removed_persistence_packages() -> None:
    blocked_imports: dict[Path, set[str]] = {}
    for path in Path("src").rglob("*.py"):
        imports = _imported_modules(path)
        persistence_imports = {
            imported
            for imported in imports
            if imported in PERSISTENCE_IMPORTS
            or imported.startswith(tuple(f"{name}." for name in PERSISTENCE_IMPORTS))
        }
        if persistence_imports:
            blocked_imports[path] = persistence_imports

    assert blocked_imports == {}


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
