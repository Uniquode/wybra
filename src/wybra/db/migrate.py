from __future__ import annotations

import asyncio
import inspect
import os
import sys
from collections.abc import Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass
from importlib import resources
from importlib.resources.abc import Traversable
from pathlib import Path
from typing import Any, Protocol

import click
from alembic import command
from alembic.config import Config
from alembic.util.exc import CommandError as AlembicError
from sqlalchemy import MetaData, Table, select
from sqlalchemy import inspect as sqlalchemy_inspect
from sqlalchemy.exc import SQLAlchemyError

from wybra.core.composition import AppConfig
from wybra.db.migration_metadata import MigrationConfigError
from wybra.db.persistence import close_database, create_database_engine
from wybra.db.provisioning import (
    DatabaseProvisioningError,
    is_postgresql_database_url,
    provision_postgresql_database,
)
from wybra.db.surfaces import (
    DataCompositionError,
    migration_version_location_for_configured_module,
    migration_version_locations_from_modules,
)
from wybra.db.urls import safe_database_error_message, sqlite_database_path
from wybra.tools.app_startup import (
    CONFIG_SOURCE_CONTEXT_KEY,
    CONFIG_SOURCE_HELP,
    CONFIG_SOURCE_OPTION,
    config_source_from_click_context,
)

DEFAULT_DATABASE_URL_CONFIG_KEY = "default_database_url"
DEFAULT_MIGRATIONS_SCRIPT_LOCATION = "wybra.db:migrations"
ALEMBIC_VERSION_TABLE = "alembic_version"

DATABASE_URL_HELP = (
    "Override the configured SQLAlchemy async database URL for this migration command."
)

REVISION_HELP = (
    "Create an Alembic revision in a configured module.\n\n"
    "Roll-forward order: Upgrade to the current head before autogenerate; "
    "update the owning module models; generate the owning module revision; "
    "Review generated operations plus down_revision and depends_on; run "
    "wybra-migrate upgrade; then run validation."
)


class MigrationSettings(Protocol):
    """Settings shape required by generic Alembic command construction.

    Host adapters provide this shape from their concrete settings object. It is
    wider than `DatabaseUrlSettings` because migrations also need script paths,
    configured modules, and optional composition metadata.
    """

    database_url: str
    alembic_config: Path
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
class MigrationState:
    initialised: bool
    current_revisions: tuple[str, ...] = ()
    detail: str | None = None


def _database_url_option[F: Callable[..., Any]](function: F) -> F:
    """Add the optional ``database_url: str | None`` Click option."""

    return click.option("--database-url", help=DATABASE_URL_HELP)(function)


def create_migrate_command(
    settings_loader: MigrationSettingsLoader,
) -> click.Group:
    @click.group(
        name="wybra-migrate",
        context_settings={"help_option_names": ["-h", "--help"]},
        help="Run application schema migrations through Alembic.",
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
        help="Provision database infrastructure and initialise migration state.",
    )
    @_database_url_option
    @click.option(
        "--admin-database-url",
        help=(
            "PostgreSQL administrative database URL for database, user, role, "
            "and privilege provisioning."
        ),
    )
    @click.pass_context
    def init_command(
        ctx: click.Context,
        database_url: str | None,
        admin_database_url: str | None,
    ) -> int:
        return _run_migration(
            settings_loader,
            _database_url_for_command(ctx, database_url),
            _config_source_for_command(ctx),
            lambda config: _initialise_database(config, admin_database_url),
        )

    @migrate_command.command("upgrade", help="Upgrade schema revisions.")
    @_database_url_option
    @click.argument("revision", default="heads", required=False)
    @click.pass_context
    def upgrade_command(
        ctx: click.Context, revision: str, database_url: str | None
    ) -> int:
        return _run_migration(
            settings_loader,
            _database_url_for_command(ctx, database_url),
            _config_source_for_command(ctx),
            lambda config: _upgrade_initialised_database(config, revision),
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
            _config_source_for_command(ctx),
            lambda config: command.downgrade(config, revision),
        )

    @migrate_command.command("current", help="Show the current database revision.")
    @_database_url_option
    @click.pass_context
    def current_command(ctx: click.Context, database_url: str | None) -> int:
        return _run_migration(
            settings_loader,
            _database_url_for_command(ctx, database_url),
            _config_source_for_command(ctx),
            _show_current_revision,
        )

    @migrate_command.command("history", help="Show migration history.")
    @_database_url_option
    @click.pass_context
    def history_command(ctx: click.Context, database_url: str | None) -> int:
        return _run_migration(
            settings_loader,
            _database_url_for_command(ctx, database_url),
            _config_source_for_command(ctx),
            command.history,
        )

    @migrate_command.command("revision", help=REVISION_HELP)
    @_database_url_option
    @click.option(
        "--module",
        "module_name",
        required=True,
        help="Configured owning module for the generated revision file.",
    )
    @click.option("-m", "--message", required=True, help="Revision message.")
    @click.option("--autogenerate", is_flag=True, help="Populate from model diff.")
    @click.option("--head", default="head", show_default=True, help="Head revision.")
    @click.option("--splice", is_flag=True, help="Allow a non-head parent revision.")
    @click.option("--branch-label", help="Branch label for the new revision.")
    @click.option("--depends-on", help="Revision dependency for cross-module graphs.")
    @click.option("--rev-id", help="Explicit revision identifier.")
    @click.pass_context
    def revision_command(
        ctx: click.Context,
        database_url: str | None,
        module_name: str,
        message: str,
        autogenerate: bool,
        head: str,
        splice: bool,
        branch_label: str | None,
        depends_on: str | None,
        rev_id: str | None,
    ) -> int:
        return _run_revision(
            settings_loader,
            _database_url_for_command(ctx, database_url),
            _config_source_for_command(ctx),
            module_name=module_name,
            message=message,
            autogenerate=autogenerate,
            head=head,
            splice=splice,
            branch_label=branch_label,
            depends_on=depends_on,
            rev_id=rev_id,
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
    operation: Callable[[Config], None],
) -> int:
    try:
        settings = _load_migration_settings(
            settings_loader,
            database_url,
            config_source,
        )
        config = build_alembic_config(settings)
    except (MigrationConfigurationError, DataCompositionError) as exc:
        print("configuration: failed", file=sys.stderr)
        print(f"- {exc}", file=sys.stderr)
        return 1

    try:
        operation(config)
    except (MigrationConfigurationError, MigrationConfigError) as exc:
        print("configuration: failed", file=sys.stderr)
        print(f"- {exc}", file=sys.stderr)
        return 1
    except MigrationStateError as exc:
        print("migration: failed", file=sys.stderr)
        print(f"- {exc}", file=sys.stderr)
        return 1
    except DatabaseProvisioningError as exc:
        print("provisioning: failed", file=sys.stderr)
        print(f"- {exc}", file=sys.stderr)
        return 1
    except (AlembicError, SQLAlchemyError) as exc:
        print("migration: failed", file=sys.stderr)
        print(f"- {safe_database_error_message(exc)}", file=sys.stderr)
        return 1
    return 0


def _run_revision(
    settings_loader: MigrationSettingsLoader,
    database_url: str | None,
    config_source: str | None,
    *,
    module_name: str,
    message: str,
    autogenerate: bool,
    head: str,
    splice: bool,
    branch_label: str | None,
    depends_on: str | None,
    rev_id: str | None,
) -> int:
    try:
        settings = _load_migration_settings(
            settings_loader,
            database_url,
            config_source,
        )
        version_path = migration_version_location_for_configured_module(
            module_name,
            settings.modules,
        )
        version_path.mkdir(parents=True, exist_ok=True)
        config = build_alembic_config(
            settings,
            additional_version_locations=(version_path,),
        )
    except (MigrationConfigurationError, DataCompositionError) as exc:
        print("configuration: failed", file=sys.stderr)
        print(f"- {exc}", file=sys.stderr)
        return 1

    try:
        command.revision(
            config,
            message=message,
            autogenerate=autogenerate,
            head=head,
            splice=splice,
            branch_label=branch_label,
            version_path=version_path,
            rev_id=rev_id,
            depends_on=depends_on,
        )
    except MigrationConfigError as exc:
        print("configuration: failed", file=sys.stderr)
        print(f"- {exc}", file=sys.stderr)
        return 1
    except (AlembicError, SQLAlchemyError) as exc:
        print("migration: failed", file=sys.stderr)
        print(f"- {safe_database_error_message(exc)}", file=sys.stderr)
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


def build_alembic_config(
    settings: MigrationSettings,
    *,
    additional_version_locations: Sequence[Path] = (),
) -> Config:
    config = Config(str(settings.alembic_config))
    config.set_main_option(
        "script_location",
        migration_script_location(settings.migrations_root),
    )
    version_locations = _deduplicated_paths(
        (
            *migration_version_locations_from_modules(settings.modules),
            *additional_version_locations,
        )
    )
    if version_locations:
        config.set_main_option("path_separator", "os")
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
    if settings.app_config is not None:
        config.set_main_option("app_config", settings.app_config.config_path.as_posix())
    return config


def _deduplicated_paths(paths: Sequence[Path]) -> tuple[Path, ...]:
    return tuple(dict.fromkeys(path.resolve() for path in paths))


def _initialise_database(config: Config, admin_database_url: str | None) -> None:
    database_url = _database_url_from_config(config)

    if is_postgresql_database_url(database_url):
        provision_postgresql_database(database_url, admin_database_url)

    _initialise_database_state(config, database_url)


def _initialise_database_state(config: Config, database_url: str) -> None:
    def initialise(connection: Any) -> None:
        state = _migration_state_from_connection(connection)
        if state.initialised and state.current_revisions:
            click.echo("database: already initialised")
            return

        _run_alembic_with_connection(
            config,
            connection,
            lambda: command.stamp(config, "base"),
        )
        click.echo("database: initialised")

    _run_with_database_connection(database_url, initialise)


def _upgrade_initialised_database(config: Config, revision: str) -> None:
    database_url = _database_url_from_config(config)
    database_path = sqlite_database_path(database_url)
    if database_path is not None and not database_path.exists():
        raise MigrationStateError(
            "Database is not initialised; run `uv run wybra-migrate init` "
            "for first-time schema setup before using `wybra-migrate upgrade`."
        )

    def upgrade(connection: Any) -> None:
        state = _migration_state_from_connection(connection)
        if not state.initialised:
            raise MigrationStateError(
                "Database is not initialised; run `uv run wybra-migrate init` "
                "for first-time schema setup before using `wybra-migrate upgrade`."
            )

        _run_alembic_with_connection(
            config,
            connection,
            lambda: command.upgrade(config, revision),
        )

    _run_with_database_connection(database_url, upgrade)


def _run_alembic_with_connection(
    config: Config,
    connection: Any,
    operation: Callable[[], None],
) -> None:
    previous_connection = config.attributes.get("connection")
    config.attributes["connection"] = connection
    try:
        operation()
    finally:
        if previous_connection is None:
            config.attributes.pop("connection", None)
        else:
            config.attributes["connection"] = previous_connection


def _run_with_database_connection[T](
    database_url: str,
    operation: Callable[[Any], T],
) -> T:
    engine = create_database_engine(database_url)

    async def run_operation() -> T:
        operation_failed = False
        try:
            async with engine.begin() as connection:
                return await connection.run_sync(operation)
        except BaseException:
            operation_failed = True
            raise
        finally:
            if operation_failed:
                with suppress(Exception):
                    await close_database(engine)
            else:
                await close_database(engine)

    return asyncio.run(run_operation())


def _show_current_revision(config: Config) -> None:
    state = inspect_migration_state(_database_url_from_config(config))
    if not state.initialised:
        click.echo("database: not initialised")
        if state.detail:
            click.echo(f"detail: {state.detail}")
        click.echo("current revision: none")
        click.echo("hint: run `uv run wybra-migrate init` for first-time setup")
        return

    if not state.current_revisions:
        click.echo("database: initialised")
        click.echo("current revision: base")
        return

    command.current(config)


def inspect_migration_state(database_url: str) -> MigrationState:
    """Inspect migration state using a database URL and managed connection."""

    database_path = sqlite_database_path(database_url)
    if database_path is not None and not database_path.exists():
        return MigrationState(
            initialised=False,
            detail="SQLite database file does not exist.",
        )

    return _run_with_database_connection(database_url, _migration_state_from_connection)


def _migration_state_from_connection(connection: Any) -> MigrationState:
    inspector = sqlalchemy_inspect(connection)
    if not inspector.has_table(ALEMBIC_VERSION_TABLE):
        return MigrationState(initialised=False)

    version_table = Table(
        ALEMBIC_VERSION_TABLE,
        MetaData(),
        autoload_with=connection,
    )

    revisions = tuple(
        str(row[0]) for row in connection.execute(select(version_table.c.version_num))
    )
    return MigrationState(initialised=True, current_revisions=revisions)


def _database_url_from_config(config: Config) -> str:
    database_url = config.get_main_option("sqlalchemy.url")
    if database_url is None or not database_url.strip():
        raise MigrationConfigurationError("Migration database URL is not configured.")
    return database_url


def migration_script_location(migrations_root: Path | None = None) -> str:
    if migrations_root is None:
        return DEFAULT_MIGRATIONS_SCRIPT_LOCATION

    return migrations_root.as_posix()


def migration_script_root(migrations_root: Path | None = None) -> Path | Traversable:
    if migrations_root is None:
        return resources.files("wybra.db") / "migrations"

    return migrations_root


def _alembic_config_value(value: str) -> str:
    return value.replace("%", "%%")


def _missing_settings_loader(
    database_url: str | None,
    *,
    config_source: str | None = None,
    **_extra: object,
) -> MigrationSettings:
    del database_url, config_source, _extra
    raise MigrationConfigurationError("Migration settings loader is not configured.")


migrate_command = create_migrate_command(_missing_settings_loader)
