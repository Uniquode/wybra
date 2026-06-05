from __future__ import annotations

from collections.abc import Sequence

import click
import uvicorn

from tools.project import runtime_project_root
from uniquode.environment import ENV_APP_RELOAD, load_environment

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000
DEFAULT_RELOAD = False
APP_TARGET = "uniquode.asgi:app"
RELOAD_ENV_VAR = ENV_APP_RELOAD
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
    host: str,
    port: int,
    reload_enabled: bool,
    extra_args: Sequence[str],
) -> list[str]:
    args = [
        APP_TARGET,
        "--host",
        host,
        "--port",
        str(port),
    ]
    if reload_enabled:
        args.append("--reload")
    args.extend(extra_args)
    return args


def _reject_extra_app_target(extra_args: Sequence[str]) -> None:
    for arg in extra_args:
        if not _looks_like_app_target(arg):
            continue
        if _same_app_target(arg, APP_TARGET):
            continue
        raise click.UsageError(
            "runserver owns the Uvicorn app target; pass Uvicorn options after "
            "`--`, not another app target."
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


@click.command(
    name="runserver",
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
    env = load_environment(project_root=runtime_project_root())
    reload_env_value = env.get(RELOAD_ENV_VAR, None) or None
    reload_enabled = (
        env_requests_reload(reload_env_value)
        if reload_requested is None
        else reload_requested
    )
    _reject_extra_app_target(uvicorn_args)
    run_uvicorn_command(
        build_uvicorn_args(
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
            prog_name="runserver",
            standalone_mode=False,
        )
    except click.exceptions.Exit as exc:
        return int(exc.exit_code or 0)
    except click.ClickException as exc:
        exc.show()
        return int(exc.exit_code or 1)
    return int(result or 0)
