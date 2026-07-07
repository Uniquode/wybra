from __future__ import annotations

from collections.abc import Sequence
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from wybra.db.surfaces import model_packages_by_module


def build_tortoise_config(
    *,
    database_url: str,
    modules: Sequence[str],
) -> dict[str, Any]:
    return {
        "connections": {
            "default": tortoise_database_url(database_url),
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


def tortoise_database_url(database_url: str) -> str:
    if database_url == "sqlite+aiosqlite:///:memory:":
        return "sqlite://:memory:"
    parsed = urlsplit(database_url)
    if parsed.scheme == "sqlite+aiosqlite":
        return urlunsplit(parsed._replace(scheme="sqlite"))
    if parsed.scheme == "postgresql+asyncpg":
        return urlunsplit(parsed._replace(scheme="asyncpg"))
    return database_url


def tortoise_app_label(module_name: str) -> str:
    return module_name.replace(".", "_")


def tortoise_migrations_module(module_name: str) -> str:
    return f"{module_name}.migrations"
