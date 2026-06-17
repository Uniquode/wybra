from __future__ import annotations

import os
from collections.abc import Sequence
from pathlib import Path

import click
import uvicorn

from wybra.config import ConfigService, MappingConfigSource
from wybra.core.composition import APP_CONFIG_ENV, APP_ROOT_ENV, CompositionError
from wybra.core.config import ENV_APP_ENV
from wybra.core.environment import load_environment
from wybra.core.runtime import ALLOWED_DEPLOYMENT_ENVIRONMENTS
from wybra.db.config import ENV_DATABASE_URL
from wybra.tools.app_startup import (
    normalise_cli_config_source,
    resolve_configured_app_startup,
)
from wybra.tools.project import (
    ProjectToolConfigurationError,
    runtime_project_root,
)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000
DEFAULT_RELOAD = False
CONTEXT_SETTINGS = {
    "allow_extra_args": True,
    "help_option_names": ["-h", "--help"],
    "ignore_unknown_options": True,
}


def env_requests_reload(value: str | None) -> bool:
    if value is None:
        return DEFAULT_RELOAD

    return value.strip().lower() in {"1", "true", "on"}


def build_uvicorn_args(
    *,
    app_target: str,
    host: str,
    port: int,
    reload_enabled: bool,
    extra_args: Sequence[str],
) -> list[str]:
    args = [
        app_target,
        "--host",
        host,
        "--port",
        str(port),
    ]
    if reload_enabled:
        args.append("--reload")
    args.extend(extra_args)
    return args


def _reject_extra_app_target(
    extra_args: Sequence[str],
    *,
    app_target: str,
) -> None:
    for arg in extra_args:
        if not _looks_like_app_target(arg):
            continue
        if _same_app_target(arg, app_target):
            continue
        raise click.UsageError(
            "wybra-runserver owns the Uvicorn app target; pass Uvicorn options "
            "after `--`, not another app target."
        )


def _parse_app_target(target: str) -> tuple[str, str | None]:
    module, separator, attribute = target.partition(":")
    return module, attribute if separator else None


def _same_app_target(target: str, default: str) -> bool:
    return _parse_app_target(target) == _parse_app_target(default)


def _looks_like_app_target(value: str) -> bool:
    if value.startswith("-"):
        return False

    module_name, separator, _attribute = value.partition(":")
    if separator != ":":
        return False

    module_parts = module_name.split(".")
    return bool(module_parts) and all(part.isidentifier() for part in module_parts)


def run_uvicorn_command(args: Sequence[str]) -> None:
    uvicorn.main.main(args=list(args), prog_name="uvicorn")


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
    "--config",
    "config_source",
    help="App config file for this invocation.",
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
        config = load_runserver_config(
            project_root=selected_project_root,
            reload_env_var=startup.reload_env_var,
        )
    except (CompositionError, ProjectToolConfigurationError) as exc:
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
    _reject_extra_app_target(uvicorn_args, app_target=startup.app_target)
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
        )
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
