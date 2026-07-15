from __future__ import annotations

import os
from collections.abc import Awaitable, Callable, Sequence
from functools import partial
from typing import cast

import anyio

from wybra.core.composition import APP_CONFIG_ENV
from wybra.core.diagnostics import wrapped_error
from wybra.core.exceptions import ConfigurationError
from wybra.db import migrate as data_migrate
from wybra.db.config import ENV_DATABASE_URL
from wybra.db.settings import CredentialPurpose
from wybra.tools.app_startup import normalise_cli_config_source
from wybra.tools.project import (
    ProjectToolConfigurationError,
    runtime_project_root,
)
from wybra.tools.settings import load_project_settings

MigrationConfigurationError = data_migrate.MigrationConfigurationError


def _build_settings(
    database_url: str | None,
    *,
    config_source: str | None = None,
    database_credential_purpose: CredentialPurpose = "runtime",
    fallback_to_runtime_credentials: bool = False,
    include_provisioning_connection: bool = False,
    resolve_database_credentials: bool = True,
    **_extra: object,
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
                load_project_settings(
                    project_root=project_root,
                    database_credential_purpose=database_credential_purpose,
                    fallback_to_runtime_credentials=fallback_to_runtime_credentials,
                    include_provisioning_connection=include_provisioning_connection,
                    resolve_database_credentials=resolve_database_credentials,
                ),
            )

        return cast(
            data_migrate.MigrationSettings,
            load_project_settings(
                environ=environment,
                project_root=project_root,
                database_credential_purpose=database_credential_purpose,
                fallback_to_runtime_credentials=fallback_to_runtime_credentials,
                include_provisioning_connection=include_provisioning_connection,
                resolve_database_credentials=resolve_database_credentials,
            ),
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
        environment[APP_CONFIG_ENV] = normalise_cli_config_source(config_source)
    return environment


async def run_migration(
    database_url: str | None,
    *,
    config_source: str | None,
    operation: Callable[
        [data_migrate.MigrationBackend, data_migrate.MigrationContext], Awaitable[None]
    ],
    database_credential_purpose: CredentialPurpose = "runtime",
    fallback_to_runtime_credentials: bool = False,
    include_provisioning_connection: bool = False,
    resolve_database_credentials: bool = True,
) -> int:
    """Run one migration operation using this application's settings loader."""
    return await data_migrate.run_migration(
        _build_settings,
        database_url,
        config_source,
        operation,
        database_credential_purpose=database_credential_purpose,
        fallback_to_runtime_credentials=fallback_to_runtime_credentials,
        include_provisioning_connection=include_provisioning_connection,
        resolve_database_credentials=resolve_database_credentials,
    )


def _run_migration(
    database_url: str | None,
    config_source: str | None,
    operation: Callable[
        [data_migrate.MigrationBackend, data_migrate.MigrationContext], Awaitable[None]
    ],
    *,
    database_credential_purpose: CredentialPurpose = "runtime",
    fallback_to_runtime_credentials: bool = False,
    include_provisioning_connection: bool = False,
    resolve_database_credentials: bool = True,
) -> int:
    return anyio.run(
        partial(
            run_migration,
            database_url,
            config_source=config_source,
            operation=operation,
            database_credential_purpose=database_credential_purpose,
            fallback_to_runtime_credentials=fallback_to_runtime_credentials,
            include_provisioning_connection=include_provisioning_connection,
            resolve_database_credentials=resolve_database_credentials,
        )
    )


migrate_command = data_migrate.create_migrate_command(
    _build_settings,
    migration_runner=_run_migration,
)


def main(argv: Sequence[str] | None = None) -> int:
    return data_migrate.run_migrate_command(migrate_command, argv)


_database_url_for_command = data_migrate._database_url_for_command

__all__ = (
    "MigrationConfigurationError",
    "_database_url_for_command",
    "main",
    "migrate_command",
    "run_migration",
)
