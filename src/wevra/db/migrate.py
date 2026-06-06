from __future__ import annotations

import os
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, Protocol

import click
from alembic import command
from alembic.config import Config
from alembic.util.exc import CommandError as AlembicError
from sqlalchemy.exc import SQLAlchemyError

from wevra.core.composition import AppConfig
from wevra.db.migration_metadata import MigrationConfigError
from wevra.db.surfaces import (
    DataCompositionError,
    migration_version_locations_from_modules,
)

DEFAULT_DATABASE_URL_CONFIG_KEY = "default_database_url"
DEFAULT_MODULES_CONFIG_KEY = "default_modules"

DATABASE_URL_HELP = (
    "Override the configured SQLAlchemy async database URL for this migration command."
)


class MigrationSettings(Protocol):
    """Settings shape required by generic Alembic command construction.

    Host adapters provide this shape from their concrete settings object. It is
    wider than `DatabaseUrlSettings` because migrations also need script paths,
    configured modules, and optional composition metadata.
    """

    database_url: str
    alembic_config: Path
    migrations_root: Path
    app_config: AppConfig | None

    @property
    def modules(self) -> tuple[str, ...]: ...


MigrationSettingsLoader = Callable[[str | None], MigrationSettings]


class MigrationConfigurationError(ValueError):
    """Raised when host application migration settings cannot be built."""


def _database_url_option[F: Callable[..., Any]](function: F) -> F:
    """Add the optional ``database_url: str | None`` Click option."""

    return click.option("--database-url", help=DATABASE_URL_HELP)(function)


def create_migrate_command(
    settings_loader: MigrationSettingsLoader,
) -> click.Group:
    @click.group(
        name="migrate",
        context_settings={"help_option_names": ["-h", "--help"]},
        help="Run application schema migrations through Alembic.",
    )
    @_database_url_option
    @click.pass_context
    def migrate_command(ctx: click.Context, database_url: str | None) -> None:
        ctx.ensure_object(dict)
        ctx.obj["database_url"] = database_url

    @migrate_command.command("upgrade", help="Upgrade schema revisions.")
    @_database_url_option
    @click.argument("revision", default="head", required=False)
    @click.pass_context
    def upgrade_command(
        ctx: click.Context, revision: str, database_url: str | None
    ) -> int:
        return _run_migration(
            settings_loader,
            _database_url_for_command(ctx, database_url),
            lambda config: command.upgrade(config, revision),
        )

    @migrate_command.command("downgrade", help="Downgrade schema revisions.")
    @_database_url_option
    @click.argument("revision")
    @click.pass_context
    def downgrade_command(
        ctx: click.Context, revision: str, database_url: str | None
    ) -> int:
        return _run_migration(
            settings_loader,
            _database_url_for_command(ctx, database_url),
            lambda config: command.downgrade(config, revision),
        )

    @migrate_command.command("current", help="Show the current database revision.")
    @_database_url_option
    @click.pass_context
    def current_command(ctx: click.Context, database_url: str | None) -> int:
        return _run_migration(
            settings_loader,
            _database_url_for_command(ctx, database_url),
            command.current,
        )

    @migrate_command.command("history", help="Show migration history.")
    @_database_url_option
    @click.pass_context
    def history_command(ctx: click.Context, database_url: str | None) -> int:
        return _run_migration(
            settings_loader,
            _database_url_for_command(ctx, database_url),
            command.history,
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
            "Invalid Click context object for migrate; expected a dictionary."
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


def _run_migration(
    settings_loader: MigrationSettingsLoader,
    database_url: str | None,
    operation: Callable[[Config], None],
) -> int:
    try:
        settings = settings_loader(database_url)
        config = build_alembic_config(settings)
    except (MigrationConfigurationError, DataCompositionError) as exc:
        print("configuration: failed", file=sys.stderr)
        print(f"- {exc}", file=sys.stderr)
        return 1

    try:
        operation(config)
    except MigrationConfigError as exc:
        print("configuration: failed", file=sys.stderr)
        print(f"- {exc}", file=sys.stderr)
        return 1
    except (AlembicError, SQLAlchemyError) as exc:
        print("migration: failed", file=sys.stderr)
        print(f"- {exc}", file=sys.stderr)
        return 1
    return 0


def run_migrate_command(
    migrate_command: click.Group,
    argv: Sequence[str] | None = None,
) -> int:
    try:
        result = migrate_command.main(
            args=None if argv is None else list(argv),
            prog_name="migrate",
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


def build_alembic_config(settings: MigrationSettings) -> Config:
    config = Config(str(settings.alembic_config))
    config.set_main_option("script_location", settings.migrations_root.as_posix())
    version_locations = migration_version_locations_from_modules(settings.modules)
    if version_locations:
        config.set_main_option("version_path_separator", "os")
        config.set_main_option(
            "version_locations",
            os.pathsep.join(path.as_posix() for path in version_locations),
        )
    config.set_main_option(
        "sqlalchemy.url", _alembic_config_value(settings.database_url)
    )
    config.set_main_option(
        DEFAULT_DATABASE_URL_CONFIG_KEY,
        _alembic_config_value(settings.database_url),
    )
    config.set_main_option(
        DEFAULT_MODULES_CONFIG_KEY,
        _module_config_value(settings.modules),
    )
    if settings.app_config is not None:
        config.set_main_option("app_config", settings.app_config.config_path.as_posix())
    return config


def _alembic_config_value(value: str) -> str:
    return value.replace("%", "%%")


def _module_config_value(modules: Sequence[str]) -> str:
    return ",".join(modules)


def _missing_settings_loader(_database_url: str | None) -> MigrationSettings:
    raise MigrationConfigurationError("Migration settings loader is not configured.")


migrate_command = create_migrate_command(_missing_settings_loader)
