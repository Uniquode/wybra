from __future__ import annotations

import os
from collections.abc import Sequence

from data_core import migrate as data_migrate
from tools.project import runtime_project_root
from uniquode.configuration import ConfigurationError
from uniquode.environment import ENV_DATABASE_URL
from uniquode.settings import load_settings
from web_core.diagnostics import wrapped_error

AlembicError = data_migrate.AlembicError
MigrationConfigurationError = data_migrate.MigrationConfigurationError
SQLAlchemyError = data_migrate.SQLAlchemyError
build_alembic_config = data_migrate.build_alembic_config
command = data_migrate.command


def _build_settings(database_url: str | None) -> data_migrate.MigrationSettings:
    project_root = runtime_project_root()
    try:
        if database_url is None:
            return load_settings(project_root=project_root)

        if not database_url.strip():
            raise data_migrate.MigrationConfigurationError(
                "DATABASE_URL must not be blank."
            )

        environment = dict(os.environ)
        environment[ENV_DATABASE_URL] = database_url
        return load_settings(environ=environment, project_root=project_root)
    except ConfigurationError as exc:
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
