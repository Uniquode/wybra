from __future__ import annotations

import os
from collections.abc import Sequence
from pathlib import Path

import click

from wybra.config import ConfigService, MappingConfigSource
from wybra.core.composition import APP_CONFIG_ENV, APP_ROOT_ENV, CompositionError
from wybra.core.config import ENV_APP_ENV
from wybra.core.environment import load_environment
from wybra.core.logging import LoggingConfigurationError
from wybra.core.runtime import ALLOWED_DEPLOYMENT_ENVIRONMENTS
from wybra.db.config import ENV_DATABASE_URL
from wybra.tools.app_startup import (
    CONFIG_SOURCE_CONTEXT_KEY,
    CONFIG_SOURCE_HELP,
    CONFIG_SOURCE_OPTION,
    normalise_cli_config_source,
    resolve_configured_app_startup,
)
from wybra.tools.cli_logging import configure_cli_logging
from wybra.tools.project import (
    ProjectToolConfigurationError,
    runtime_project_root,
)
from wybra.tools.runserver_uvicorn import (
    build_uvicorn_args,
    reject_extra_app_target,
    run_uvicorn_command,
)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000
DEFAULT_RELOAD = False
CONTEXT_SETTINGS = {
    "allow_extra_args": True,
    "help_option_names": ["-h", "--help"],
    "ignore_unknown_options": True,
    "max_content_width": 120,
}


def env_requests_reload(value: str | None) -> bool:
    if value is None:
        return DEFAULT_RELOAD

    return value.strip().lower() in {"1", "true", "on"}


def runserver_environment_overrides(
    *,
    project_root: Path | None,
    config_source: str | None,
    database_url: str | None,
    deployment_environment: str | None,
) -> dict[str, str]:
    overrides: dict[str, str] = {}
    if project_root is not None:
        overrides[APP_ROOT_ENV] = project_root.resolve().as_posix()
    if config_source is not None:
        overrides[APP_CONFIG_ENV] = normalise_cli_config_source(config_source)
    if database_url is not None:
        overrides[ENV_DATABASE_URL] = _non_blank_option(database_url, "--database-url")
    if deployment_environment is not None:
        overrides[ENV_APP_ENV] = _non_blank_option(deployment_environment, "--deploy")
    return overrides


def _non_blank_option(value: str, option_name: str) -> str:
    # `None` means the option was not supplied; blank strings are supplied but
    # invalid CLI override values.
    if not value.strip():
        raise click.UsageError(f"{option_name} must not be blank.")
    return value.strip()


def load_runserver_config(
    *,
    project_root: Path,
    reload_env_var: str,
) -> ConfigService:
    env = load_environment(project_root=project_root)
    reload_env_value = env.get(reload_env_var)
    if not isinstance(reload_env_value, str):
        reload_env_value = None
    return ConfigService(
        [
            MappingConfigSource(
                {"runserver": {"reload_env_value": reload_env_value}},
                source="runserver",
            )
        ],
        discover_module_config=False,
    )


@click.command(
    name="wybra-runserver",
    context_settings=CONTEXT_SETTINGS,
    help="Start the local Uvicorn development server.",
)
@click.option("--host", default=DEFAULT_HOST, show_default=True)
@click.option("--port", default=DEFAULT_PORT, show_default=True, type=int)
@click.option(
    "--reload/--no-reload",
    "reload_requested",
    default=None,
    help="Override APP_RELOAD; reload on changes.",
)
@click.option(
    "--project",
    "project_root",
    type=click.Path(path_type=Path, file_okay=False, dir_okay=True),
    help="Override APP_ROOT or current directory.",
)
@click.option(
    CONFIG_SOURCE_OPTION,
    CONFIG_SOURCE_CONTEXT_KEY,
    help=CONFIG_SOURCE_HELP,
)
@click.option(
    "--database-url",
    help="Override DATABASE_URL or configured default.",
)
@click.option(
    "--deploy",
    "deployment_environment",
    type=click.Choice(ALLOWED_DEPLOYMENT_ENVIRONMENTS),
    help="Override APP_ENV or configured default.",
)
@click.argument("uvicorn_args", nargs=-1, type=click.UNPROCESSED)
def runserver_command(
    host: str,
    port: int,
    reload_requested: bool | None,
    project_root: Path | None,
    config_source: str | None,
    database_url: str | None,
    deployment_environment: str | None,
    uvicorn_args: tuple[str, ...],
) -> None:
    selected_project_root = (
        project_root.resolve() if project_root is not None else runtime_project_root()
    )
    try:
        startup = resolve_configured_app_startup(
            project_root=selected_project_root,
            config_source=config_source,
        )
        logging_config = configure_cli_logging(startup.app_config)
        config = load_runserver_config(
            project_root=selected_project_root,
            reload_env_var=startup.reload_env_var,
        )
    except (
        CompositionError,
        LoggingConfigurationError,
        ProjectToolConfigurationError,
    ) as exc:
        raise click.ClickException(str(exc)) from exc

    runserver_config = config.get_config("runserver") or {}
    reload_env_value = runserver_config.get("reload_env_value")
    if not isinstance(reload_env_value, str):
        reload_env_value = None
    reload_enabled = (
        env_requests_reload(reload_env_value)
        if reload_requested is None
        else reload_requested
    )
    reject_extra_app_target(uvicorn_args, app_target=startup.app_target)
    os.environ.update(
        runserver_environment_overrides(
            project_root=project_root,
            config_source=config_source,
            database_url=database_url,
            deployment_environment=deployment_environment,
        )
    )
    run_uvicorn_command(
        build_uvicorn_args(
            app_target=startup.app_target,
            host=host,
            port=port,
            reload_enabled=reload_enabled,
            extra_args=uvicorn_args,
        ),
        logging_config=logging_config,
    )


def main(argv: Sequence[str] | None = None) -> int:
    try:
        result = runserver_command.main(
            args=None if argv is None else list(argv),
            prog_name="wybra-runserver",
            standalone_mode=False,
        )
    except click.exceptions.Exit as exc:
        return int(exc.exit_code or 0)
    except click.ClickException as exc:
        exc.show()
        return int(exc.exit_code or 1)
    return int(result or 0)
