from __future__ import annotations

import os
from collections.abc import Callable, Sequence
from typing import cast

from wevra.core.diagnostics import wrapped_error
from wevra.db import migrate as data_migrate
from wevra.tools.project import (
    ProjectToolConfigurationError,
    load_wevra_tool_runtime,
    runtime_project_root,
    wevra_tool_option,
)

AlembicError = data_migrate.AlembicError
MigrationConfigurationError = data_migrate.MigrationConfigurationError
SQLAlchemyError = data_migrate.SQLAlchemyError
build_alembic_config = data_migrate.build_alembic_config
command = data_migrate.command


def _build_settings(database_url: str | None) -> data_migrate.MigrationSettings:
    try:
        tool_runtime = load_wevra_tool_runtime(project_root=runtime_project_root())
    except ProjectToolConfigurationError as exc:
        raise wrapped_error(data_migrate.MigrationConfigurationError, exc) from exc

    project_root = tool_runtime.project_root
    load_settings = cast(
        Callable[..., data_migrate.MigrationSettings],
        tool_runtime.settings_loader,
    )
    configuration_error = tool_runtime.configuration_error

    try:
        if database_url is None:
            return load_settings(project_root=project_root)

        if not database_url.strip():
            raise data_migrate.MigrationConfigurationError(
                "DATABASE_URL must not be blank."
            )

        environment = dict(os.environ)
        environment[
            wevra_tool_option("database_url_env", project_root=project_root)
        ] = database_url
        return load_settings(environ=environment, project_root=project_root)
    except ProjectToolConfigurationError as exc:
        raise wrapped_error(data_migrate.MigrationConfigurationError, exc) from exc
    except configuration_error as exc:
        raise wrapped_error(data_migrate.MigrationConfigurationError, exc) from exc


migrate_command = data_migrate.create_migrate_command(_build_settings)


def main(argv: Sequence[str] | None = None) -> int:
    return data_migrate.run_migrate_command(migrate_command, argv)


_database_url_for_command = data_migrate._database_url_for_command

__all__ = (
    "AlembicError",
    "MigrationConfigurationError",
    "SQLAlchemyError",
    "_database_url_for_command",
    "build_alembic_config",
    "command",
    "main",
    "migrate_command",
)
