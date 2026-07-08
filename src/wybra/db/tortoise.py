from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from wybra.db.surfaces import model_packages_by_module
from wybra.db.urls import tortoise_database_url


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


def tortoise_app_label(module_name: str) -> str:
    return module_name.replace(".", "_")


def tortoise_migrations_module(module_name: str) -> str:
    return f"{module_name}.migrations"
