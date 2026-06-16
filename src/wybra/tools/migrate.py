from __future__ import annotations

import os
from collections.abc import Sequence
from typing import cast

from wybra.core.composition import APP_CONFIG_ENV
from wybra.core.diagnostics import wrapped_error
from wybra.core.exceptions import ConfigurationError
from wybra.db import migrate as data_migrate
from wybra.db.config import ENV_DATABASE_URL
from wybra.tools.app_startup import normalise_config_source
from wybra.tools.project import (
    ProjectToolConfigurationError,
    runtime_project_root,
)
from wybra.tools.settings import load_project_settings

AlembicError = data_migrate.AlembicError
MigrationConfigurationError = data_migrate.MigrationConfigurationError
SQLAlchemyError = data_migrate.SQLAlchemyError
build_alembic_config = data_migrate.build_alembic_config
command = data_migrate.command


def _build_settings(
    database_url: str | None,
    config_source: str | None = None,
) -> data_migrate.MigrationSettings:
    project_root = runtime_project_root()

    try:
        environment = _command_environment(
            database_url=database_url,
            config_source=config_source,
        )
        if environment is None:
            return cast(
                data_migrate.MigrationSettings,
                load_project_settings(project_root=project_root),
            )

        return cast(
            data_migrate.MigrationSettings,
            load_project_settings(environ=environment, project_root=project_root),
        )
    except ProjectToolConfigurationError as exc:
        raise wrapped_error(data_migrate.MigrationConfigurationError, exc) from exc
    except ConfigurationError as exc:
        raise wrapped_error(data_migrate.MigrationConfigurationError, exc) from exc
    except data_migrate.MigrationConfigurationError:
        raise


def _command_environment(
    *,
    database_url: str | None,
    config_source: str | None,
) -> dict[str, str] | None:
    if database_url is None and config_source is None:
        return None

    environment = dict(os.environ)
    if database_url is not None:
        if not database_url.strip():
            raise data_migrate.MigrationConfigurationError(
                "DATABASE_URL must not be blank."
            )
        environment[ENV_DATABASE_URL] = database_url.strip()
    if config_source is not None:
        environment[APP_CONFIG_ENV] = normalise_config_source(config_source)
    return environment


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
