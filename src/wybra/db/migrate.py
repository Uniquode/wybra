from __future__ import annotations

import inspect
import logging
import sys
import types
import uuid
from collections.abc import Awaitable, Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any, Protocol, cast

import anyio
import click
from tortoise import fields
from tortoise.backends.base.client import BaseDBAsyncClient
from tortoise.cli import cli as tortoise_cli
from tortoise.cli import utils as tortoise_cli_utils
from tortoise.exceptions import ConfigurationError as TortoiseConfigurationError
from tortoise.migrations.executor import MigrationExecutor
from tortoise.migrations.recorder import MigrationRecorder
from tortoise.models import Model

from wybra.core.composition import AppConfig
from wybra.core.logging import LoggingConfigurationError
from wybra.db.provisioning import (
    DatabaseMaintenanceRequest,
    DatabaseMaintenanceTask,
    DatabaseProvisioningConfigurationError,
    DatabaseProvisioningOperationError,
    DestroyDatabaseRequest,
    ProvisioningContext,
    ProvisioningPhaseResult,
    database_maintenance_tasks,
    destroy_database,
    initialise_database,
    provisioning_context,
    run_database_maintenance,
)
from wybra.db.provisioning.mysql import quote_mysql_identifier
from wybra.db.settings import CredentialPurpose, ResolvedDatabaseConnection
from wybra.db.sql import ident, param, render_sql
from wybra.db.surfaces import DataCompositionError
from wybra.db.tortoise import build_tortoise_config as build_config
from wybra.db.urls import (
    database_backend_for_url,
    database_url_support_error,
    is_supported_database_url,
    safe_database_error_message,
)
from wybra.tools.app_startup import (
    CONFIG_SOURCE_CONTEXT_KEY,
    CONFIG_SOURCE_HELP,
    CONFIG_SOURCE_OPTION,
    config_source_from_click_context,
)
from wybra.tools.cli_logging import configure_cli_logging

DATABASE_URL_HELP = "Override the configured database URL for this migration command."

logger = logging.getLogger(__name__)
TORTOISE_CONFIG_VARIABLE = "TORTOISE_ORM"


class MigrationSettings(Protocol):
    """Settings shape required by Tortoise migration command construction.

    Host adapters provide this shape from their concrete settings object.
    Migration commands need the effective database URL, configured modules,
    and optional composition metadata.
    """

    database_url: str | None
    database_connection: ResolvedDatabaseConnection | None
    provisioning_connection: ResolvedDatabaseConnection | None
    project_root: Path
    migrations_root: Path | None
    app_config: AppConfig | None

    @property
    def modules(self) -> tuple[str, ...]: ...


class MigrationSettingsLoader(Protocol):
    """Callable shape used to build migration settings.

    Loaders must accept the database URL as their first argument. Newer loaders
    may accept ``config_source`` as a keyword-only argument; extra keywords are
    permitted so wrappers can forward command context without losing type
    information for the required database URL.
    """

    def __call__(
        self,
        database_url: str | None,
        *,
        config_source: str | None = ...,
        database_credential_purpose: CredentialPurpose = ...,
        fallback_to_runtime_credentials: bool = ...,
        include_provisioning_connection: bool = ...,
        resolve_database_credentials: bool = ...,
        **_: object,
    ) -> MigrationSettings: ...


class MigrationConfigurationError(ValueError):
    """Raised when host application migration settings cannot be built."""


class MigrationStateError(RuntimeError):
    """Raised when a migration operation is invalid for the database state."""


@dataclass(frozen=True, slots=True)
class MigrationContext:
    """Resolved migration command context passed to migration backends."""

    settings: MigrationSettings
    config: dict[str, Any]
    database_connection: ResolvedDatabaseConnection
    provisioning_connection: ResolvedDatabaseConnection | None = None


class MigrationBackend(Protocol):
    """Backend boundary for migration lifecycle operations."""

    async def initialise(
        self,
        context: MigrationContext,
        *,
        app_labels: tuple[str, ...],
    ) -> None: ...

    async def makemigrations(
        self,
        context: MigrationContext,
        request: MakeMigrationsRequest,
    ) -> None: ...

    async def migrate(
        self,
        context: MigrationContext,
        request: MigrationTargetRequest,
    ) -> None: ...

    async def downgrade(
        self,
        context: MigrationContext,
        request: MigrationTargetRequest,
    ) -> None: ...

    async def history(
        self,
        context: MigrationContext,
        app_labels: tuple[str, ...],
    ) -> None: ...

    async def heads(
        self,
        context: MigrationContext,
        app_labels: tuple[str, ...],
    ) -> None: ...

    async def sqlmigrate(
        self,
        context: MigrationContext,
        request: SqlMigrateRequest,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class MakeMigrationsRequest:
    app_labels: tuple[str, ...]
    empty: bool
    name: str | None


@dataclass(frozen=True, slots=True)
class MigrationTargetRequest:
    app_label: str | None
    migration: str | None
    fake: bool
    dry_run: bool


@dataclass(frozen=True, slots=True)
class SqlMigrateRequest:
    app_label: str | None
    migration_name: str | None
    backward: bool


class TortoiseMigrationBackend:
    """Tortoise-backed migration backend."""

    async def initialise(
        self,
        context: MigrationContext,
        *,
        app_labels: tuple[str, ...],
    ) -> None:
        await _run_tortoise_cli(context, ["init", *app_labels])

    async def makemigrations(
        self,
        context: MigrationContext,
        request: MakeMigrationsRequest,
    ) -> None:
        args = ["makemigrations", *request.app_labels]
        if request.empty:
            args.append("--empty")
        if request.name is not None:
            args.extend(("-n", request.name))
        await _run_tortoise_cli(context, args)

    async def migrate(
        self,
        context: MigrationContext,
        request: MigrationTargetRequest,
    ) -> None:
        await _run_tortoise_cli(context, _target_args("migrate", request))

    async def downgrade(
        self,
        context: MigrationContext,
        request: MigrationTargetRequest,
    ) -> None:
        await _run_tortoise_cli(context, _target_args("downgrade", request))

    async def history(
        self,
        context: MigrationContext,
        app_labels: tuple[str, ...],
    ) -> None:
        await _run_tortoise_cli(context, ["history", *app_labels])

    async def heads(
        self,
        context: MigrationContext,
        app_labels: tuple[str, ...],
    ) -> None:
        await _run_tortoise_cli(context, ["heads", *app_labels])

    async def sqlmigrate(
        self,
        context: MigrationContext,
        request: SqlMigrateRequest,
    ) -> None:
        args = ["sqlmigrate"]
        if request.app_label is not None:
            args.append(request.app_label)
        if request.migration_name is not None:
            args.append(request.migration_name)
        if request.backward:
            args.append("--backward")
        await _run_tortoise_cli(context, args)


def _target_args(command_name: str, request: MigrationTargetRequest) -> list[str]:
    args = [command_name]
    if request.app_label is not None:
        args.append(request.app_label)
    if request.migration is not None:
        args.append(request.migration)
    if request.fake:
        args.append("--fake")
    if request.dry_run:
        args.append("--dry-run")
    return args


def _database_url_option[F: Callable[..., Any]](function: F) -> F:
    """Add the optional ``database_url: str | None`` Click option."""

    return click.option("--database-url", help=DATABASE_URL_HELP)(function)


def create_migrate_command(
    settings_loader: MigrationSettingsLoader,
    *,
    migration_runner: Callable[..., int] | None = None,
) -> click.Group:
    if migration_runner is None:

        def migration_runner(
            database_url: str | None,
            config_source: str | None,
            operation: Callable[[MigrationBackend, MigrationContext], Awaitable[None]],
            *,
            database_credential_purpose: CredentialPurpose = "runtime",
            fallback_to_runtime_credentials: bool = False,
            include_provisioning_connection: bool = False,
            resolve_database_credentials: bool = True,
        ) -> int:
            return _run_migration(
                settings_loader,
                database_url,
                config_source,
                operation,
                database_credential_purpose=database_credential_purpose,
                fallback_to_runtime_credentials=fallback_to_runtime_credentials,
                include_provisioning_connection=include_provisioning_connection,
                resolve_database_credentials=resolve_database_credentials,
            )

    @click.group(
        name="wybra-migrate",
        context_settings={
            "help_option_names": ["-h", "--help"],
            "max_content_width": 120,
        },
        help="Run application schema migrations through Tortoise.",
    )
    @_database_url_option
    @click.option(
        CONFIG_SOURCE_OPTION, CONFIG_SOURCE_CONTEXT_KEY, help=CONFIG_SOURCE_HELP
    )
    @click.pass_context
    def migrate_command(
        ctx: click.Context,
        database_url: str | None,
        config_source: str | None,
    ) -> None:
        ctx.ensure_object(dict)
        ctx.obj["database_url"] = database_url
        ctx.obj[CONFIG_SOURCE_CONTEXT_KEY] = config_source

    @migrate_command.command(
        "init",
        help="Initialise migration packages.",
    )
    @_database_url_option
    @click.argument("app_labels", nargs=-1)
    @click.pass_context
    def init_command(
        ctx: click.Context,
        database_url: str | None,
        app_labels: tuple[str, ...],
    ) -> int:
        return migration_runner(
            _database_url_for_command(ctx, database_url),
            _config_source_for_command(ctx),
            operation=lambda backend, context: initialise_migration_lifecycle(
                backend,
                context,
                app_labels=app_labels,
            ),
            include_provisioning_connection=True,
        )

    @migrate_command.command(
        "destroy",
        help="Destroy database objects owned by lifecycle setup.",
    )
    @_database_url_option
    @click.option(
        "--confirm",
        required=True,
        help="Confirm the target database/schema name before destructive work runs.",
    )
    @click.pass_context
    def destroy_command(
        ctx: click.Context,
        database_url: str | None,
        confirm: str,
    ) -> int:
        return migration_runner(
            _database_url_for_command(ctx, database_url),
            _config_source_for_command(ctx),
            include_provisioning_connection=True,
            operation=lambda _backend, context: destroy_database_lifecycle(
                context,
                DestroyDatabaseRequest(confirm=confirm),
            ),
        )

    @migrate_command.command(
        "tasks",
        help="List database maintenance tasks.",
    )
    @_database_url_option
    @click.pass_context
    def tasks_command(
        ctx: click.Context,
        database_url: str | None,
    ) -> int:
        return migration_runner(
            _database_url_for_command(ctx, database_url),
            _config_source_for_command(ctx),
            operation=list_database_maintenance_tasks_lifecycle,
            resolve_database_credentials=False,
        )

    @migrate_command.command(
        "run",
        help="Run a database maintenance task.",
    )
    @_database_url_option
    @click.option(
        "--confirm",
        help="Confirm the maintenance task name before protected work runs.",
    )
    @click.argument("task")
    @click.pass_context
    def run_command(
        ctx: click.Context,
        database_url: str | None,
        confirm: str | None,
        task: str,
    ) -> int:
        return migration_runner(
            _database_url_for_command(ctx, database_url),
            _config_source_for_command(ctx),
            include_provisioning_connection=True,
            operation=lambda _backend, context: run_database_maintenance_lifecycle(
                context,
                DatabaseMaintenanceRequest(
                    task=_maintenance_task_from_argument(task),
                    confirm=confirm,
                ),
            ),
        )

    @migrate_command.command(
        "makemigrations",
        help="Create migrations from model changes.",
    )
    @_database_url_option
    @click.argument("app_labels", nargs=-1)
    @click.option("--empty", is_flag=True, help="Create an empty migration.")
    @click.option("-n", "--name", help="Use this name for the migration file.")
    @click.pass_context
    def makemigrations_command(
        ctx: click.Context,
        database_url: str | None,
        app_labels: tuple[str, ...],
        empty: bool,
        name: str | None,
    ) -> int:
        return migration_runner(
            _database_url_for_command(ctx, database_url),
            _config_source_for_command(ctx),
            lambda backend, context: backend.makemigrations(
                context,
                MakeMigrationsRequest(app_labels=app_labels, empty=empty, name=name),
            ),
        )

    @migrate_command.command(
        "migrate",
        help="Apply migrations.",
    )
    @_database_url_option
    @click.argument("args", nargs=-1)
    @click.option(
        "--fake",
        is_flag=True,
        help="Record migrations without executing SQL.",
    )
    @click.option(
        "--dry-run",
        is_flag=True,
        help="Show what would run without changing database state.",
    )
    @click.pass_context
    def migrate_command_command(
        ctx: click.Context,
        database_url: str | None,
        args: tuple[str, ...],
        fake: bool,
        dry_run: bool,
    ) -> int:
        app_label, migration = _migration_target_from_args(args)
        return migration_runner(
            _database_url_for_command(ctx, database_url),
            _config_source_for_command(ctx),
            lambda backend, context: backend.migrate(
                context,
                MigrationTargetRequest(
                    app_label=app_label,
                    migration=migration,
                    fake=fake,
                    dry_run=dry_run,
                ),
            ),
            database_credential_purpose="service_account",
        )

    @migrate_command.command("downgrade", help="Unapply migrations.")
    @_database_url_option
    @click.argument("app_label", required=False)
    @click.argument("migration", required=False)
    @click.option(
        "--fake",
        is_flag=True,
        help="Record migrations without executing SQL.",
    )
    @click.option(
        "--dry-run",
        is_flag=True,
        help="Show what would run without changing database state.",
    )
    @click.pass_context
    def downgrade_command(
        ctx: click.Context,
        database_url: str | None,
        app_label: str | None,
        migration: str | None,
        fake: bool,
        dry_run: bool,
    ) -> int:
        return migration_runner(
            _database_url_for_command(ctx, database_url),
            _config_source_for_command(ctx),
            lambda backend, context: backend.downgrade(
                context,
                MigrationTargetRequest(
                    app_label=app_label,
                    migration=migration,
                    fake=fake,
                    dry_run=dry_run,
                ),
            ),
            database_credential_purpose="service_account",
        )

    @migrate_command.command("history", help="Show migration history.")
    @_database_url_option
    @click.argument("app_labels", nargs=-1)
    @click.pass_context
    def history_command(
        ctx: click.Context,
        database_url: str | None,
        app_labels: tuple[str, ...],
    ) -> int:
        return migration_runner(
            _database_url_for_command(ctx, database_url),
            _config_source_for_command(ctx),
            lambda backend, context: backend.history(context, app_labels),
        )

    @migrate_command.command("heads", help="Show migration heads on disk.")
    @_database_url_option
    @click.argument("app_labels", nargs=-1)
    @click.pass_context
    def heads_command(
        ctx: click.Context,
        database_url: str | None,
        app_labels: tuple[str, ...],
    ) -> int:
        return migration_runner(
            _database_url_for_command(ctx, database_url),
            _config_source_for_command(ctx),
            lambda backend, context: backend.heads(context, app_labels),
        )

    @migrate_command.command("sqlmigrate", help="Print SQL for a migration.")
    @_database_url_option
    @click.argument("app_label", required=False)
    @click.argument("migration_name", required=False)
    @click.option(
        "--backward",
        is_flag=True,
        help="Generate SQL to unapply the migration.",
    )
    @click.pass_context
    def sqlmigrate_command(
        ctx: click.Context,
        database_url: str | None,
        app_label: str | None,
        migration_name: str | None,
        backward: bool,
    ) -> int:
        return migration_runner(
            _database_url_for_command(ctx, database_url),
            _config_source_for_command(ctx),
            lambda backend, context: backend.sqlmigrate(
                context,
                SqlMigrateRequest(
                    app_label=app_label,
                    migration_name=migration_name,
                    backward=backward,
                ),
            ),
        )

    return migrate_command


def _database_url_for_command(
    ctx: click.Context, command_database_url: str | None
) -> str | None:
    if command_database_url is not None:
        return command_database_url

    if ctx.obj is None:
        return None

    if not isinstance(ctx.obj, dict):
        raise click.UsageError(
            "Invalid Click context object for wybra-migrate; expected a dictionary."
        )

    root_database_url = ctx.obj.get("database_url")
    if root_database_url is None:
        return None
    if not isinstance(root_database_url, str):
        raise click.UsageError(
            "Invalid root database_url type "
            f"{type(root_database_url)!r}; expected a string."
        )
    return root_database_url


def _migration_target_from_args(args: Sequence[str]) -> tuple[str | None, str | None]:
    if len(args) > 2:
        raise click.UsageError(
            "migrate accepts at most APP_LABEL and MIGRATION arguments."
        )
    app_label = args[0] if len(args) >= 1 else None
    migration = args[1] if len(args) == 2 else None
    return app_label, migration


def _maintenance_task_from_argument(task_argument: str) -> str:
    task = task_argument.strip()
    if not task:
        raise click.UsageError("run TASK must not be blank.")
    return task


def _config_source_for_command(ctx: click.Context) -> str | None:
    return config_source_from_click_context(
        ctx,
        error_factory=click.UsageError,
        invalid_context_message=(
            "Invalid Click context object for wybra-migrate; expected a dictionary."
        ),
        invalid_type_message=lambda value_type: (
            f"Invalid root config_source type {value_type!r}; expected a string."
        ),
    )


def _run_migration(
    settings_loader: MigrationSettingsLoader,
    database_url: str | None,
    config_source: str | None,
    operation: Callable[[MigrationBackend, MigrationContext], Awaitable[None]],
    *,
    database_credential_purpose: CredentialPurpose = "runtime",
    fallback_to_runtime_credentials: bool = False,
    include_provisioning_connection: bool = False,
    resolve_database_credentials: bool = True,
) -> int:
    return anyio.run(
        partial(
            run_migration,
            settings_loader,
            database_url,
            config_source,
            operation,
            database_credential_purpose=database_credential_purpose,
            fallback_to_runtime_credentials=fallback_to_runtime_credentials,
            include_provisioning_connection=include_provisioning_connection,
            resolve_database_credentials=resolve_database_credentials,
        )
    )


async def run_migration(
    settings_loader: MigrationSettingsLoader,
    database_url: str | None,
    config_source: str | None,
    operation: Callable[[MigrationBackend, MigrationContext], Awaitable[None]],
    *,
    database_credential_purpose: CredentialPurpose = "runtime",
    fallback_to_runtime_credentials: bool = False,
    include_provisioning_connection: bool = False,
    resolve_database_credentials: bool = True,
) -> int:
    try:
        configure_cli_logging()
        settings = _load_migration_settings(
            settings_loader,
            database_url,
            config_source,
            database_credential_purpose=database_credential_purpose,
            fallback_to_runtime_credentials=fallback_to_runtime_credentials,
            include_provisioning_connection=include_provisioning_connection,
            resolve_database_credentials=resolve_database_credentials,
        )
        configure_cli_logging(settings.app_config)
        context = build_migration_context(settings)
        backend: MigrationBackend = TortoiseMigrationBackend()
    except (
        LoggingConfigurationError,
        MigrationConfigurationError,
        DataCompositionError,
    ) as exc:
        logger.error("configuration: failed: %s", exc)
        return 1

    try:
        await operation(backend, context)
    except MigrationConfigurationError as exc:
        logger.error("configuration: failed: %s", exc)
        return 1
    except MigrationStateError as exc:
        logger.error("migration: failed: %s", exc)
        return 1
    except DatabaseProvisioningConfigurationError as exc:
        logger.error("configuration: failed: %s", exc)
        return 1
    except DatabaseProvisioningOperationError as exc:
        logger.error("database lifecycle: failed: %s", safe_database_error_message(exc))
        return 1
    except (TortoiseConfigurationError, tortoise_cli_utils.CLIError) as exc:
        logger.error("migration: failed: %s", safe_database_error_message(exc))
        return 1
    return 0


def _load_migration_settings(
    settings_loader: MigrationSettingsLoader,
    database_url: str | None,
    config_source: str | None,
    *,
    database_credential_purpose: CredentialPurpose = "runtime",
    fallback_to_runtime_credentials: bool = False,
    include_provisioning_connection: bool = False,
    resolve_database_credentials: bool = True,
) -> MigrationSettings:
    optional_kwargs = {
        "database_credential_purpose": database_credential_purpose,
        "fallback_to_runtime_credentials": fallback_to_runtime_credentials,
        "include_provisioning_connection": include_provisioning_connection,
        "resolve_database_credentials": resolve_database_credentials,
    }
    if config_source is None:
        return _call_settings_loader(
            settings_loader,
            database_url,
            _supported_loader_kwargs(settings_loader, optional_kwargs),
        )

    if _loader_accepts_keyword_config_source(settings_loader):
        kwargs = _supported_loader_kwargs(settings_loader, optional_kwargs)
        kwargs["config_source"] = config_source
        return _call_settings_loader(
            settings_loader,
            database_url,
            kwargs,
        )
    raise MigrationConfigurationError(
        "Migration settings loader must accept config_source when --config is used."
    )


def _call_settings_loader(
    settings_loader: MigrationSettingsLoader,
    database_url: str | None,
    kwargs: dict[str, object],
) -> MigrationSettings:
    return cast(Any, settings_loader)(database_url, **kwargs)


def _loader_accepts_keyword_config_source(
    settings_loader: MigrationSettingsLoader,
) -> bool:
    """Return whether a migration settings loader can accept ``config_source``.

    Loader adapters may be simple functions, callable objects, or wrappers.
    Runtime ``--config`` support is enabled only when the callable can accept a
    keyword named ``config_source``, either explicitly or through ``**kwargs``.
    Callables whose signatures cannot be inspected are treated as unsupported
    so the command fails with a configuration error instead of guessing.
    """
    try:
        signature = inspect.signature(settings_loader)
    except TypeError, ValueError:
        return False

    has_explicit_config_source = any(
        parameter.name == "config_source"
        and parameter.kind
        in (
            inspect.Parameter.KEYWORD_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        )
        for parameter in signature.parameters.values()
    )
    if has_explicit_config_source:
        return True

    return any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in signature.parameters.values()
    )


def _supported_loader_kwargs(
    settings_loader: MigrationSettingsLoader,
    values: dict[str, object],
) -> dict[str, object]:
    try:
        signature = inspect.signature(settings_loader)
    except (TypeError, ValueError) as exc:
        if any(values.values()):
            logger.warning(
                "migration settings loader signature could not be inspected; "
                "optional loader arguments ignored: %s",
                exc,
            )
        return {}
    if any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in signature.parameters.values()
    ):
        return dict(values)
    return {
        name: value
        for name, value in values.items()
        if any(
            parameter.name == name
            and parameter.kind
            in (
                inspect.Parameter.KEYWORD_ONLY,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
            )
            for parameter in signature.parameters.values()
        )
    }


def run_migrate_command(
    migrate_command: click.Group,
    argv: Sequence[str] | None = None,
) -> int:
    try:
        result = migrate_command.main(
            args=None if argv is None else list(argv),
            prog_name="wybra-migrate",
            standalone_mode=False,
        )
    except click.exceptions.Exit as exc:
        return int(exc.exit_code or 0)
    except click.ClickException as exc:
        exc.show()
        return int(exc.exit_code or 1)
    return int(result or 0)


def main(argv: Sequence[str] | None = None) -> int:
    return run_migrate_command(migrate_command, argv)


def build_migration_context(settings: MigrationSettings) -> MigrationContext:
    database_connection = _database_connection_for_settings(settings)

    return MigrationContext(
        settings=settings,
        config=build_tortoise_config(settings),
        database_connection=database_connection,
        provisioning_connection=getattr(settings, "provisioning_connection", None),
    )


def build_tortoise_config(settings: MigrationSettings) -> dict[str, Any]:
    database_connection = getattr(settings, "database_connection", None)
    if database_connection is not None:
        return build_config(
            database_connection=database_connection,
            modules=settings.modules,
        )
    if settings.database_url is None:
        raise MigrationConfigurationError("Database URL is required.")
    return build_config(database_url=settings.database_url, modules=settings.modules)


async def initialise_migration_lifecycle(
    backend: MigrationBackend,
    context: MigrationContext,
    *,
    app_labels: tuple[str, ...],
) -> None:
    await initialise_database_lifecycle(context)
    await backend.initialise(context, app_labels=app_labels)


async def initialise_database_lifecycle(context: MigrationContext) -> None:
    _report_provisioning_results(
        await initialise_database(_provisioning_context(context))
    )


async def destroy_database_lifecycle(
    context: MigrationContext,
    request: DestroyDatabaseRequest,
) -> None:
    _report_provisioning_results(
        await destroy_database(_provisioning_context(context), request)
    )


async def list_database_maintenance_tasks_lifecycle(
    _backend: MigrationBackend,
    context: MigrationContext,
) -> None:
    provisioning_context = _provisioning_context(context)
    _report_database_maintenance_tasks(
        provisioning_context.family,
        database_maintenance_tasks(provisioning_context),
    )


async def run_database_maintenance_lifecycle(
    context: MigrationContext,
    request: DatabaseMaintenanceRequest,
) -> None:
    _report_provisioning_results(
        await run_database_maintenance(_provisioning_context(context), request)
    )


def _report_database_maintenance_tasks(
    family: str,
    tasks: Sequence[DatabaseMaintenanceTask],
) -> None:
    if not tasks:
        click.echo(f"No database maintenance tasks are available for {family}.")
        return

    for task in tasks:
        click.echo(f"{task.name}: {task.description}")
        click.echo(f"  credentials: {task.credential_scope}")
        if task.recommended_frequency is not None:
            click.echo(f"  recommended: {task.recommended_frequency}")
        if task.requires_confirmation:
            click.echo("  confirmation: required")


def _report_provisioning_results(
    results: Sequence[ProvisioningPhaseResult],
) -> None:
    for result in results:
        logger.info(
            "database lifecycle: %s %s %s: %s",
            result.family,
            result.phase,
            result.status,
            result.message,
        )


def _provisioning_context(context: MigrationContext) -> ProvisioningContext:
    return provisioning_context(
        runtime_connection=context.database_connection,
        provisioning_connection=context.provisioning_connection,
        project_root=context.settings.project_root,
        modules=context.settings.modules,
    )


def _database_connection_for_settings(
    settings: MigrationSettings,
) -> ResolvedDatabaseConnection:
    database_connection = getattr(settings, "database_connection", None)
    if database_connection is not None:
        return database_connection

    if settings.database_url is None or not settings.database_url.strip():
        raise MigrationConfigurationError("Database URL is required.")
    if not is_supported_database_url(settings.database_url):
        raise MigrationConfigurationError(
            database_url_support_error(settings.database_url)
        )
    backend = database_backend_for_url(settings.database_url)
    if backend is None:  # pragma: no cover - is_supported_database_url checked above
        raise MigrationConfigurationError(
            database_url_support_error(settings.database_url)
        )
    return ResolvedDatabaseConnection.from_url(settings.database_url, backend=backend)


async def _run_tortoise_cli(context: MigrationContext, args: Sequence[str]) -> None:
    config_module = _register_tortoise_config_module(context.config)
    try:
        with _tortoise_migration_recorder_compatibility():
            exit_code = await tortoise_cli.run_cli_async(
                [
                    "--config",
                    f"{config_module}.{TORTOISE_CONFIG_VARIABLE}",
                    *args,
                ]
            )
    finally:
        sys.modules.pop(config_module, None)

    if exit_code:
        command_name = args[0] if args else "<unknown>"
        raise MigrationStateError(
            "Tortoise migration command failed: "
            f"command={command_name}, exit_code={exit_code}."
        )


def _register_tortoise_config_module(config: dict[str, Any]) -> str:
    module_name = f"_wybra_tortoise_config_{uuid.uuid4().hex}"
    module = types.ModuleType(module_name)
    setattr(module, TORTOISE_CONFIG_VARIABLE, config)
    sys.modules[module_name] = module
    return module_name


async def apply_tortoise_migrations(
    connection: BaseDBAsyncClient,
    apps: dict[str, dict[str, object]],
) -> None:
    """Apply native Tortoise migrations using an existing database connection."""
    with _tortoise_migration_recorder_compatibility():
        await MigrationExecutor(connection, apps).migrate()


@contextmanager
def _tortoise_migration_recorder_compatibility() -> Iterator[None]:
    original_record_applied = MigrationRecorder.record_applied
    original_make_model = MigrationRecorder._make_model

    def recorder_dialect(self: Any) -> str:
        dialect = getattr(self, "_dialect", "")
        if isinstance(dialect, str) and dialect:
            return dialect
        connection = getattr(self, "connection", None)
        capabilities = getattr(connection, "capabilities", None)
        return str(getattr(capabilities, "dialect", ""))

    async def record_applied(self: Any, app: str, name: str) -> None:
        if recorder_dialect(self) != "mysql":
            await original_record_applied(self, app, name)
            return

        # Tortoise currently writes timezone-aware ISO strings for migration
        # recorder timestamps. MariaDB rejects those for DATETIME in strict
        # mode, so use a plain UTC DATETIME literal for MySQL-family backends
        # until the upstream recorder handles this natively.
        applied_at = datetime.now(UTC).replace(tzinfo=None).isoformat(" ")
        query = render_sql(
            t"INSERT INTO {ident(self.table_name)} "
            t"({ident('app')}, {ident('name')}, {ident('applied_at')}) "
            t"VALUES ({param(app)}, {param(name)}, {param(applied_at)})",
            dialect="mysql",
            quote_identifier=quote_mysql_identifier,
        )
        await self.connection.execute_query(query.statement, list(query.parameters))

    def make_model(self: Any, table_name: str) -> type[Model]:
        class MigrationRecord(Model):
            # Tortoise 1.1 still calls this deprecated field alias with
            # ``pk=True``. Use the current spelling until upstream updates its
            # migration recorder implementation.
            id = fields.IntField(primary_key=True)
            app = fields.CharField(max_length=255)
            name = fields.CharField(max_length=255)
            applied_at = fields.DatetimeField()

            class Meta:
                table = table_name
                app = "_migrations"
                unique_together = (("app", "name"),)

        return MigrationRecord

    MigrationRecorder._make_model = make_model
    MigrationRecorder.record_applied = record_applied
    try:
        yield
    finally:
        MigrationRecorder._make_model = original_make_model
        MigrationRecorder.record_applied = original_record_applied


def _missing_settings_loader(
    database_url: str | None,
    *,
    config_source: str | None = None,
    **_extra: object,
) -> MigrationSettings:
    del database_url, config_source, _extra
    raise MigrationConfigurationError("Migration settings loader is not configured.")


migrate_command = create_migrate_command(_missing_settings_loader)
