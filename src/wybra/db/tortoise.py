from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from wybra.db.settings import ResolvedDatabaseConnection
from wybra.db.surfaces import model_packages_by_module
from wybra.db.urls import tortoise_database_url


def build_tortoise_config(
    *,
    database_url: str | None = None,
    database_connection: ResolvedDatabaseConnection | None = None,
    modules: Sequence[str],
) -> dict[str, Any]:
    connection_config = _connection_config(
        database_url=database_url,
        database_connection=database_connection,
    )
    return {
        "connections": {
            "default": connection_config,
        },
        "apps": {
            tortoise_app_label(module_name): {
                "models": list(model_packages),
                "migrations": tortoise_migrations_module(module_name),
                "default_connection": "default",
            }
            for module_name, model_packages in model_packages_by_module(
                tuple(modules)
            ).items()
        },
    }


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


def tortoise_migrations_module(module_name: str) -> str:
    return f"{module_name}.migrations"
