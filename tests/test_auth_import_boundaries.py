import ast
import tomllib
from pathlib import Path

FASTAPI_USERS_IMPORTS = ("fastapi_users", "fastapi_users_db_sqlalchemy")
FASTAPI_USERS_DEPENDENCIES = ("fastapi-users", "fastapi-users-db-sqlalchemy")
SQLALCHEMY_IMPORT = "sqlalchemy"
AUTH_SQLALCHEMY_ALLOWED_PATHS = {
    Path("src/wybra/auth/accounts/bootstrap.py"),
    Path("src/wybra/auth/admin/management.py"),
    Path("src/wybra/auth/authorisation/effective.py"),
    Path("src/wybra/auth/capabilities.py"),
    Path("src/wybra/auth/cli/authmgr/schema.py"),
    Path("src/wybra/auth/context.py"),
    Path("src/wybra/auth/emails.py"),
    Path("src/wybra/auth/mfa/storage.py"),
    Path("src/wybra/auth/models.py"),
    Path("src/wybra/auth/persistence/database.py"),
    Path("src/wybra/auth/persistence/strategies.py"),
    Path("src/wybra/auth/provider_credentials.py"),
}


def test_project_dependencies_exclude_fastapi_users() -> None:
    project = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    dependencies = tuple(project["project"]["dependencies"])

    for dependency in dependencies:
        normalised_dependency = dependency.lower()
        assert not normalised_dependency.startswith(FASTAPI_USERS_DEPENDENCIES)


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


def test_auth_runtime_sqlalchemy_imports_stay_in_adapter_boundaries() -> None:
    blocked_imports: dict[Path, set[str]] = {}
    for path in Path("src/wybra/auth").rglob("*.py"):
        if _is_auth_sqlalchemy_boundary(path):
            continue

        imports = _imported_modules(path)
        sqlalchemy_imports = {
            imported
            for imported in imports
            if imported == SQLALCHEMY_IMPORT
            or imported.startswith(f"{SQLALCHEMY_IMPORT}.")
        }
        if sqlalchemy_imports:
            blocked_imports[path] = sqlalchemy_imports

    assert blocked_imports == {}


def _is_auth_sqlalchemy_boundary(path: Path) -> bool:
    if path in AUTH_SQLALCHEMY_ALLOWED_PATHS:
        return True
    return "migrations" in path.parts


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
