from __future__ import annotations

import sys
from collections.abc import Iterable, Mapping, Sequence
from enum import StrEnum
from typing import Any, TextIO

import click

from wybra.tools.project import (
    ProjectToolConfigurationError,
    import_wybra_tool_option,
    runtime_project_root,
)
from wybra.web.routes import (
    RouteInspection,
    inspect_route_tree,
    render_graph,
    render_json,
    render_mermaid,
    render_succinct,
)

APP_TARGET_OPTION = "runserver_app"


class RouteOutputFormat(StrEnum):
    SUCCINCT = "succinct"
    GRAPH = "graph"
    MERMAID = "mermaid"
    JSON = "json"


def load_configured_asgi_app() -> Any:
    project_root = runtime_project_root()
    return import_wybra_tool_option(APP_TARGET_OPTION, project_root=project_root)


def render_inspection(
    inspection: RouteInspection,
    output_format: RouteOutputFormat,
) -> str:
    match output_format:
        case RouteOutputFormat.SUCCINCT:
            return render_succinct(inspection)
        case RouteOutputFormat.GRAPH:
            return render_graph(inspection)
        case RouteOutputFormat.MERMAID:
            return render_mermaid(inspection)
        case RouteOutputFormat.JSON:
            return render_json(inspection)
        case _:
            raise ValueError(f"Unsupported route output format: {output_format!r}")


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
def routes_command(
    output_format: str | None,
    succinct_format: bool,
    graph_format: bool,
    mermaid_format: bool,
    json_format: bool,
    check: bool,
    quiet: bool,
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
        app = load_configured_asgi_app()
    except ProjectToolConfigurationError as exc:
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
        inspection = inspect_route_tree(app)
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
    "render_inspection",
    "routes_command",
]
