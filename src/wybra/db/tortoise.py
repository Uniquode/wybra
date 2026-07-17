from __future__ import annotations

from collections.abc import Mapping, Sequence
from hashlib import sha256
from pathlib import Path
from typing import Any

from wybra.db.settings import ResolvedDatabaseConnection
from wybra.db.surfaces import model_packages_by_module
from wybra.db.urls import tortoise_database_url


def build_tortoise_config(
    *,
    database_url: str | None = None,
    database_connection: ResolvedDatabaseConnection | None = None,
    database_connections: Mapping[str, ResolvedDatabaseConnection] | None = None,
    default_connection: str = "default",
    routers: Sequence[type[object]] = (),
    modules: Sequence[str],
    migrations_root: Path | None = None,
) -> dict[str, Any]:
    if database_connections is not None:
        if database_url is not None or database_connection is not None:
            raise ValueError(
                "database_connections cannot be combined with a single database "
                "connection."
            )
        connections = {
            name: _connection_config(
                database_url=None,
                database_connection=connection,
            )
            for name, connection in database_connections.items()
        }
    else:
        connections = {
            "default": _connection_config(
                database_url=database_url,
                database_connection=database_connection,
            )
        }
    if default_connection not in connections:
        raise ValueError("default_connection must identify a configured connection.")
    config: dict[str, Any] = {
        "connections": connections,
        "apps": {
            tortoise_app_label(module_name): {
                "models": list(model_packages),
                "migrations": tortoise_migrations_module(
                    module_name,
                    migrations_root=migrations_root,
                ),
                "default_connection": default_connection,
            }
            for module_name, model_packages in model_packages_by_module(
                tuple(modules)
            ).items()
        },
    }
    if routers:
        config["routers"] = list(routers)
    return config


def _connection_config(
    *,
    database_url: str | None,
    database_connection: ResolvedDatabaseConnection | None,
) -> str | dict[str, Any]:
    if database_connection is not None:
        if database_url is not None:
            raise ValueError(
                "database_url and database_connection are mutually exclusive."
            )
        return database_connection.tortoise_connection_config
    if database_url is None:
        raise ValueError("database_url or database_connection is required.")
    return tortoise_database_url(database_url)


def tortoise_app_label(module_name: str) -> str:
    return module_name.replace(".", "_")


def tortoise_migrations_module(
    module_name: str,
    *,
    migrations_root: Path | None = None,
) -> str:
    if migrations_root is not None:
        migrations_package = tortoise_migrations_package(migrations_root)
        return f"{migrations_package}.{tortoise_app_label(module_name)}"
    return f"{module_name}.migrations"


def tortoise_migrations_package(migrations_root: Path) -> str:
    """Return the stable import package name for an isolated migration root."""
    resolved_root = migrations_root.resolve()
    digest = sha256(str(resolved_root).encode()).hexdigest()[:16]
    return f"_wybra_migrations_{digest}"
