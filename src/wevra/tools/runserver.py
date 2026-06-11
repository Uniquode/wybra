from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import click
import uvicorn

from wevra.config import ConfigService, MappingConfigSource
from wevra.tools.project import (
    ProjectToolConfigurationError,
    import_wevra_tool_callable,
    runtime_project_root,
    wevra_tool_option,
)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000
DEFAULT_RELOAD = False
APP_TARGET_OPTION = "runserver_app"
RELOAD_ENV_VAR_OPTION = "runserver_reload_env"
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
            "wevra-runserver owns the Uvicorn app target; pass Uvicorn options "
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


def load_environment(**kwargs: object):
    project_root = kwargs.get("project_root")
    loader = import_wevra_tool_callable(
        "environment_loader",
        project_root=project_root if isinstance(project_root, Path) else None,
    )
    return loader(**kwargs)


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
    name="wevra-runserver",
    context_settings=CONTEXT_SETTINGS,
    help="Start the local Uvicorn development server.",
)
@click.option("--host", default=DEFAULT_HOST, show_default=True)
@click.option("--port", default=DEFAULT_PORT, show_default=True, type=int)
@click.option(
    "--reload/--no-reload",
    "reload_requested",
    default=None,
    help="Enable or disable reload. Defaults to APP_RELOAD.",
)
@click.argument("uvicorn_args", nargs=-1, type=click.UNPROCESSED)
def runserver_command(
    host: str,
    port: int,
    reload_requested: bool | None,
    uvicorn_args: tuple[str, ...],
) -> None:
    project_root = runtime_project_root()
    try:
        app_target = wevra_tool_option(APP_TARGET_OPTION, project_root=project_root)
        reload_env_var = wevra_tool_option(
            RELOAD_ENV_VAR_OPTION,
            project_root=project_root,
        )
        config = load_runserver_config(
            project_root=project_root,
            reload_env_var=reload_env_var,
        )
    except ProjectToolConfigurationError as exc:
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
    _reject_extra_app_target(uvicorn_args, app_target=app_target)
    run_uvicorn_command(
        build_uvicorn_args(
            app_target=app_target,
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
            prog_name="wevra-runserver",
            standalone_mode=False,
        )
    except click.exceptions.Exit as exc:
        return int(exc.exit_code or 0)
    except click.ClickException as exc:
        exc.show()
        return int(exc.exit_code or 1)
    return int(result or 0)
