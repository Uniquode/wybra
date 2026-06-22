from __future__ import annotations

import asyncio
import inspect
import sys
from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from typing import Any, TextIO, TypeGuard

import click

from wybra.core.composition import CompositionError
from wybra.core.routes import RouteInspection, inspect_route_tree
from wybra.tools.app_startup import (
    CONFIG_SOURCE_CONTEXT_KEY,
    CONFIG_SOURCE_HELP,
    CONFIG_SOURCE_OPTION,
    resolve_configured_asgi_app_target,
)
from wybra.tools.project import (
    ProjectToolConfigurationError,
    import_from_string,
    runtime_project_root,
)
from wybra.tools.route_rendering import (
    RouteOutputFormat,
    render_graph,
    render_inspection,
    render_json,
    render_mermaid,
    render_succinct,
)


def load_configured_asgi_app(config_source: str | None = None) -> Any:
    project_root = runtime_project_root()
    app_target = resolve_configured_asgi_app_target(
        project_root=project_root,
        config_source=config_source,
    )
    return import_from_string(app_target)


@click.command(
    name="wybra-routes",
    context_settings={"help_option_names": ["-h", "--help"]},
    help="Inspect the configured application's installed route tree.",
)
@click.option(
    "--format",
    "output_format",
    type=click.Choice(tuple(item.value for item in RouteOutputFormat)),
    default=None,
    help="Route tree output format. Defaults to succinct.",
)
@click.option(
    "--succinct",
    "succinct_format",
    is_flag=True,
    help="Render compact one-line route output.",
)
@click.option(
    "--graph",
    "graph_format",
    is_flag=True,
    help="Render expanded graph-like route output.",
)
@click.option(
    "--mermaid",
    "mermaid_format",
    is_flag=True,
    help="Render Mermaid diagram route output.",
)
@click.option(
    "--json",
    "json_format",
    is_flag=True,
    help="Render structured JSON route output.",
)
@click.option(
    "--check",
    is_flag=True,
    help="Exit with failure when route smoke-check problems are detected.",
)
@click.option(
    "--quiet",
    is_flag=True,
    help="With --check, suppress route-tree output and report only exit status.",
)
@click.option(
    CONFIG_SOURCE_OPTION,
    CONFIG_SOURCE_CONTEXT_KEY,
    help=CONFIG_SOURCE_HELP,
)
def routes_command(
    output_format: str | None,
    succinct_format: bool,
    graph_format: bool,
    mermaid_format: bool,
    json_format: bool,
    check: bool,
    quiet: bool,
    config_source: str | None,
) -> int:
    return asyncio.run(
        _run_routes_command(
            output_format=output_format,
            succinct_format=succinct_format,
            graph_format=graph_format,
            mermaid_format=mermaid_format,
            json_format=json_format,
            check=check,
            quiet=quiet,
            config_source=config_source,
        )
    )


async def _run_routes_command(
    *,
    output_format: str | None,
    succinct_format: bool,
    graph_format: bool,
    mermaid_format: bool,
    json_format: bool,
    check: bool,
    quiet: bool,
    config_source: str | None,
) -> int:
    if quiet and not check:
        raise click.UsageError("--quiet can only be used with --check.")

    route_output_format = _resolve_output_format(
        output_format,
        succinct=succinct_format,
        graph=graph_format,
        mermaid=mermaid_format,
        json_format=json_format,
    )

    try:
        app = (
            load_configured_asgi_app(config_source)
            if config_source is not None
            else load_configured_asgi_app()
        )
    except (CompositionError, ProjectToolConfigurationError) as exc:
        print("configuration: failed", file=sys.stderr)
        print(f"- {exc}", file=sys.stderr)
        return 1

    if not hasattr(app, "routes"):
        print("configuration: failed", file=sys.stderr)
        print(
            "- Configured ASGI app target does not expose a route tree.",
            file=sys.stderr,
        )
        return 1

    routes = app.routes
    if not _is_supported_route_tree(routes):
        print("configuration: failed", file=sys.stderr)
        print(
            "- Configured ASGI app target exposes an unsupported route tree; "
            "expected an iterable of route objects on 'app.routes'.",
            file=sys.stderr,
        )
        return 1

    try:
        inspection = await _inspect_installed_route_tree(app)
    except TypeError as exc:
        print("configuration: failed", file=sys.stderr)
        print(
            "- Failed to inspect configured ASGI app route tree. Ensure "
            "'app.routes' is an iterable of Starlette routes.",
            file=sys.stderr,
        )
        print(f"- {exc}", file=sys.stderr)
        return 1
    if not quiet:
        _write_output(render_inspection(inspection, route_output_format))
    return 1 if check and inspection.problems else 0


def _resolve_output_format(
    output_format: str | None,
    *,
    succinct: bool,
    graph: bool,
    mermaid: bool,
    json_format: bool,
) -> RouteOutputFormat:
    selected = []
    if output_format is not None:
        selected.append(RouteOutputFormat(output_format))
    if succinct:
        selected.append(RouteOutputFormat.SUCCINCT)
    if graph:
        selected.append(RouteOutputFormat.GRAPH)
    if mermaid:
        selected.append(RouteOutputFormat.MERMAID)
    if json_format:
        selected.append(RouteOutputFormat.JSON)

    if len(selected) > 1:
        raise click.UsageError("Choose only one route tree output format.")

    return selected[0] if selected else RouteOutputFormat.SUCCINCT


def _is_supported_route_tree(routes: object) -> bool:
    return isinstance(routes, Iterable) and not isinstance(
        routes,
        (Mapping, str, bytes),
    )


async def _inspect_installed_route_tree(app: Any) -> RouteInspection:
    router = getattr(app, "router", None)
    if router is None or not hasattr(router, "lifespan_context"):
        return inspect_route_tree(app)

    lifespan_context = getattr(router, "lifespan_context", None)
    # Starlette/FastAPI expose either an async context manager or an
    # `lifespan_context(app)` factory. Unsupported factory signatures are
    # ignored, but TypeError raised while entering lifespan is reported by the
    # command as a configuration failure.
    if _is_async_context_manager(lifespan_context):
        async with lifespan_context:
            return inspect_route_tree(app)
    if callable(lifespan_context) and _accepts_app_argument(lifespan_context, app):
        context = lifespan_context(app)
        if _is_async_context_manager(context):
            async with context:
                return inspect_route_tree(app)
    return inspect_route_tree(app)


def _accepts_app_argument(factory: Callable[..., object], app: Any) -> bool:
    """Return whether a lifespan factory can be called with the ASGI app.

    Starlette/FastAPI expose router lifespan factories as
    ``lifespan_context(app)``. Factories with incompatible or uninspectable
    signatures are ignored so route inspection can fall back to the installed
    route tree without accidentally calling a non-standard hook with the wrong
    argument shape.
    """
    try:
        signature = inspect.signature(factory)
    except (TypeError, ValueError):
        return False
    try:
        signature.bind(app)
    except TypeError:
        return False
    return True


def _is_async_context_manager(
    value: object,
) -> TypeGuard[AbstractAsyncContextManager[Any]]:
    if value is None or not isinstance(value, AbstractAsyncContextManager):
        return False

    aenter = getattr(value, "__aenter__", None)
    aexit = getattr(value, "__aexit__", None)
    return inspect.iscoroutinefunction(aenter) and inspect.iscoroutinefunction(aexit)


def main(argv: Sequence[str] | None = None) -> int:
    try:
        result = routes_command.main(
            args=None if argv is None else list(argv),
            prog_name="wybra-routes",
            standalone_mode=False,
        )
    except click.exceptions.Exit as exc:
        return int(exc.exit_code or 0)
    except click.ClickException as exc:
        exc.show()
        return int(exc.exit_code or 1)
    return int(result or 0)


def _write_output(output: str, *, file: TextIO | None = None) -> None:
    stream = sys.stdout if file is None else file
    if output:
        print(output, file=stream)


__all__ = [
    "RouteOutputFormat",
    "load_configured_asgi_app",
    "main",
    "render_graph",
    "render_inspection",
    "render_json",
    "render_mermaid",
    "render_succinct",
    "routes_command",
]
