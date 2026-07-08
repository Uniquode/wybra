from __future__ import annotations

import asyncio
import inspect
import json
import logging
import tempfile
from collections.abc import Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import click
from tortoise.cli import cli as tortoise_cli
from tortoise.cli import utils as tortoise_cli_utils
from tortoise.exceptions import ConfigurationError as TortoiseConfigurationError

from wybra.core.composition import AppConfig
from wybra.core.logging import LoggingConfigurationError
from wybra.db.provisioning import (
    DatabaseProvisioningConfigurationError,
    DatabaseProvisioningError,
    is_postgresql_database_url,
    provision_postgresql_database,
)
from wybra.db.surfaces import DataCompositionError
from wybra.db.tortoise import build_tortoise_config as build_config
from wybra.db.urls import (
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


class MigrationSettings(Protocol):
    """Settings shape required by Tortoise migration command construction.

    Host adapters provide this shape from their concrete settings object.
    Migration commands need the effective database URL, configured modules,
    and optional composition metadata.
    """

    database_url: str
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


class MigrationBackend(Protocol):
    """Backend boundary for migration lifecycle operations."""

    def initialise(
        self,
        context: MigrationContext,
        *,
        admin_database_url: str | None,
        app_labels: tuple[str, ...],
    ) -> None: ...

    def makemigrations(
        self,
        context: MigrationContext,
        request: MakeMigrationsRequest,
    ) -> None: ...

    def migrate(
        self,
        context: MigrationContext,
        request: MigrationTargetRequest,
    ) -> None: ...

    def downgrade(
        self,
        context: MigrationContext,
        request: MigrationTargetRequest,
    ) -> None: ...

    def history(
        self,
        context: MigrationContext,
        app_labels: tuple[str, ...],
    ) -> None: ...

    def heads(self, context: MigrationContext, app_labels: tuple[str, ...]) -> None: ...

    def sqlmigrate(
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

    def initialise(
        self,
        context: MigrationContext,
        *,
        admin_database_url: str | None,
        app_labels: tuple[str, ...],
    ) -> None:
        if is_postgresql_database_url(context.settings.database_url):
            provision_postgresql_database(
                context.settings.database_url,
                admin_database_url,
            )
        _run_tortoise_cli(context, ["init", *app_labels])

    def makemigrations(
        self,
        context: MigrationContext,
        request: MakeMigrationsRequest,
    ) -> None:
        args = ["makemigrations", *request.app_labels]
        if request.empty:
            args.append("--empty")
        if request.name is not None:
            args.extend(("-n", request.name))
        _run_tortoise_cli(context, args)

    def migrate(
        self,
        context: MigrationContext,
        request: MigrationTargetRequest,
    ) -> None:
        _run_tortoise_cli(context, _target_args("migrate", request))

    def downgrade(
        self,
        context: MigrationContext,
        request: MigrationTargetRequest,
    ) -> None:
        _run_tortoise_cli(context, _target_args("downgrade", request))

    def history(self, context: MigrationContext, app_labels: tuple[str, ...]) -> None:
        _run_tortoise_cli(context, ["history", *app_labels])

    def heads(self, context: MigrationContext, app_labels: tuple[str, ...]) -> None:
        _run_tortoise_cli(context, ["heads", *app_labels])

    def sqlmigrate(
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
        _run_tortoise_cli(context, args)


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
) -> click.Group:
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
        help="Provision database infrastructure and create migration packages.",
    )
    @_database_url_option
    @click.option(
        "--admin-database-url",
        help=(
            "PostgreSQL administrative database URL for database, user, role, "
            "and privilege provisioning."
        ),
    )
    @click.argument("app_labels", nargs=-1)
    @click.pass_context
    def init_command(
        ctx: click.Context,
        database_url: str | None,
        admin_database_url: str | None,
        app_labels: tuple[str, ...],
    ) -> int:
        return _run_migration(
            settings_loader,
            _database_url_for_command(ctx, database_url),
            _config_source_for_command(ctx),
            lambda backend, context: backend.initialise(
                context,
                admin_database_url=admin_database_url,
                app_labels=app_labels,
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
        return _run_migration(
            settings_loader,
            _database_url_for_command(ctx, database_url),
            _config_source_for_command(ctx),
            lambda backend, context: backend.makemigrations(
                context,
                MakeMigrationsRequest(app_labels=app_labels, empty=empty, name=name),
            ),
        )

    @migrate_command.command("migrate", help="Apply migrations.")
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
    def migrate_command_command(
        ctx: click.Context,
        database_url: str | None,
        app_label: str | None,
        migration: str | None,
        fake: bool,
        dry_run: bool,
    ) -> int:
        return _run_migration(
            settings_loader,
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
        return _run_migration(
            settings_loader,
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
        return _run_migration(
            settings_loader,
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
        return _run_migration(
            settings_loader,
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
        return _run_migration(
            settings_loader,
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
    operation: Callable[[MigrationBackend, MigrationContext], None],
) -> int:
    try:
        configure_cli_logging()
        settings = _load_migration_settings(
            settings_loader,
            database_url,
            config_source,
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
        operation(backend, context)
    except (
        MigrationConfigurationError,
        DatabaseProvisioningConfigurationError,
    ) as exc:
        logger.error("configuration: failed: %s", exc)
        return 1
    except MigrationStateError as exc:
        logger.error("migration: failed: %s", exc)
        return 1
    except DatabaseProvisioningError as exc:
        logger.error("provisioning: failed: %s", exc)
        return 1
    except (TortoiseConfigurationError, tortoise_cli_utils.CLIError) as exc:
        logger.error("migration: failed: %s", safe_database_error_message(exc))
        return 1
    return 0


def _load_migration_settings(
    settings_loader: MigrationSettingsLoader,
    database_url: str | None,
    config_source: str | None,
) -> MigrationSettings:
    if config_source is None:
        return settings_loader(database_url)

    if _loader_accepts_keyword_config_source(settings_loader):
        return settings_loader(database_url, config_source=config_source)
    raise MigrationConfigurationError(
        "Migration settings loader must accept config_source when --config is used."
    )


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
    except (TypeError, ValueError):
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
    if not is_supported_database_url(settings.database_url):
        raise MigrationConfigurationError(database_url_support_error())

    return MigrationContext(
        settings=settings,
        config=build_tortoise_config(settings),
    )


def build_tortoise_config(settings: MigrationSettings) -> dict[str, Any]:
    return build_config(database_url=settings.database_url, modules=settings.modules)


def _run_tortoise_cli(context: MigrationContext, args: Sequence[str]) -> None:
    config_file = _write_tortoise_config_file(context.config)
    try:
        exit_code = asyncio.run(
            tortoise_cli.run_cli_async(["--config-file", config_file.as_posix(), *args])
        )
    finally:
        with suppress(OSError):
            config_file.unlink()

    if exit_code:
        command_name = args[0] if args else "<unknown>"
        raise MigrationStateError(
            "Tortoise migration command failed: "
            f"command={command_name}, exit_code={exit_code}."
        )


def _write_tortoise_config_file(config: dict[str, Any]) -> Path:
    handle = tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        suffix=".json",
        delete=False,
    )
    with handle:
        json.dump(config, handle, sort_keys=True)
    return Path(handle.name)


def _missing_settings_loader(
    database_url: str | None,
    *,
    config_source: str | None = None,
    **_extra: object,
) -> MigrationSettings:
    del database_url, config_source, _extra
    raise MigrationConfigurationError("Migration settings loader is not configured.")


migrate_command = create_migrate_command(_missing_settings_loader)
