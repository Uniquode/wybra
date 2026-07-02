from __future__ import annotations

import sys
from collections import Counter
from collections.abc import Sequence
from pathlib import Path
from typing import TextIO

import click

from wybra.assets import (
    StaticCollectionError,
    StaticCollectionStatus,
    StaticCollectResult,
    collect_configured_static_assets,
)
from wybra.core.composition import CompositionError
from wybra.core.exceptions import ConfigurationError
from wybra.tools.app_startup import (
    CONFIG_SOURCE_CONTEXT_KEY,
    CONFIG_SOURCE_HELP,
    CONFIG_SOURCE_OPTION,
    normalise_cli_config_source,
)
from wybra.tools.project import (
    ProjectToolConfigurationError,
    runtime_project_root,
)


@click.command(
    name="wybra-collect",
    context_settings={"help_option_names": ["-h", "--help"], "max_content_width": 120},
    help="Collect configured static assets into a deployable filesystem tree.",
)
@click.option(
    CONFIG_SOURCE_OPTION,
    CONFIG_SOURCE_CONTEXT_KEY,
    help=CONFIG_SOURCE_HELP,
)
@click.option(
    "--dest",
    "destination",
    type=click.Path(path_type=Path, file_okay=False, dir_okay=True),
    help="Override the collection output directory for this run.",
)
@click.option(
    "--no-delete",
    "delete",
    is_flag=True,
    flag_value=False,
    default=True,
    help="Leave stale files in the destination tree.",
)
@click.option(
    "--nginx-cors",
    "nginx_cors",
    type=click.Path(path_type=Path, dir_okay=False, writable=True),
    help="Write an nginx CORS config section for externally served assets.",
)
def collect_command(
    config_source: str | None,
    destination: Path | None,
    delete: bool,
    nginx_cors: Path | None,
) -> int:
    try:
        project_root = runtime_project_root()
        result = collect_configured_static_assets(
            project_root=project_root,
            config_path=(
                Path(normalise_cli_config_source(config_source))
                if config_source is not None
                else None
            ),
            delete=delete,
            root=destination,
            nginx_cors=nginx_cors,
        )
    except (CompositionError, ConfigurationError, ProjectToolConfigurationError) as exc:
        print("configuration: failed", file=sys.stderr)
        print(f"- {exc}", file=sys.stderr)
        return 1
    except StaticCollectionError as exc:
        print("static collection: failed", file=sys.stderr)
        print(f"- {exc}", file=sys.stderr)
        return 1

    _print_collection_result(result)
    return 0


def _print_collection_result(
    result: StaticCollectResult,
    *,
    file: TextIO | None = None,
) -> None:
    output = sys.stdout if file is None else file
    statuses = Counter(asset.status for asset in result.collected_assets)
    print("static collection: ok", file=output)
    print(f"- root: {result.root}", file=output)
    for status in (
        StaticCollectionStatus.COPIED,
        StaticCollectionStatus.UPDATED,
        StaticCollectionStatus.UNCHANGED,
    ):
        print(f"- {status.value}: {statuses[status]}", file=output)
    print(f"- deleted: {len(result.deleted_assets)}", file=output)
    print(f"- skipped: {len(result.skipped_assets)}", file=output)
    print(f"- duplicates: {len(result.duplicates)}", file=output)


def main(argv: Sequence[str] | None = None) -> int:
    try:
        result = collect_command.main(
            args=None if argv is None else list(argv),
            prog_name="wybra-collect",
            standalone_mode=False,
        )
    except click.exceptions.Exit as exc:
        return int(exc.exit_code or 0)
    except click.exceptions.Abort:
        click.echo("Aborted!", err=True)
        return 1
    except click.ClickException as exc:
        exc.show()
        return int(exc.exit_code or 1)
    return int(result or 0)


__all__ = [
    "collect_command",
    "main",
]
