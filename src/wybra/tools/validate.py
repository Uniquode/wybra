import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, TextIO

import click

from wybra.assets.validation import validate_assets
from wybra.core.exceptions import ConfigurationError
from wybra.tools.project import (
    ProjectToolConfigurationError,
    runtime_project_root,
)
from wybra.tools.settings import load_project_settings
from wybra.tools.validation.core import ValidationResult
from wybra.tools.validation.registry import (
    ValidationDiscoveryError,
    ValidationTarget,
    discover_validation_target_details,
)

__all__ = (
    "UnknownValidationTargetError",
    "ValidationOverrides",
    "main",
    "validate_command",
)


class UnknownValidationTargetError(ValueError):
    """Raised when validation is requested for unknown target names."""


BUILTIN_VALIDATION_TARGETS: Mapping[str, ValidationTarget] = {
    "assets": validate_assets,
}


@dataclass(frozen=True, slots=True)
class ValidationOverrides:
    database_url: str | None = None
    template_root: Path | None = None
    static_root: Path | None = None
    migrations_root: Path | None = None
    static_url_path: str | None = None


def _resolve_targets(
    targets: Sequence[str],
    available_targets: Sequence[str],
) -> tuple[str, ...]:
    if not targets:
        return tuple(available_targets)

    invalid_targets = sorted(set(targets) - set(available_targets))
    if invalid_targets:
        invalid = ", ".join(invalid_targets)
        raise UnknownValidationTargetError(f"Unknown validation target(s): {invalid}")

    return tuple(dict.fromkeys(targets))


def _merge_validation_targets(
    *,
    builtin_targets: Mapping[str, ValidationTarget],
    discovered_targets: Mapping[str, ValidationTarget],
    discovered_origins: Mapping[str, str],
) -> Mapping[str, ValidationTarget]:
    duplicate_names = {
        name
        for name in builtin_targets.keys() & discovered_targets.keys()
        if discovered_targets[name] is not builtin_targets[name]
    }
    if duplicate_names:
        duplicates = ", ".join(
            f"{name} from {discovered_origins.get(name, 'unknown validation surface')}"
            for name in sorted(duplicate_names)
        )
        raise ConfigurationError(
            "Validation target name(s) conflict with built-in targets: "
            f"{duplicates}. Built-in validation targets cannot be overridden."
        )

    merged: dict[str, ValidationTarget] = dict(builtin_targets)
    merged.update(
        {
            name: target
            for name, target in discovered_targets.items()
            if name not in builtin_targets
        }
    )
    return merged


def _build_settings(overrides: ValidationOverrides) -> Any:
    project_root = runtime_project_root()
    try:
        defaults = load_project_settings(project_root=project_root)
        return replace(
            defaults,
            database_url=(
                overrides.database_url
                if overrides.database_url is not None
                else defaults.database_url
            ),
            template_root=(
                overrides.template_root
                if overrides.template_root is not None
                else defaults.template_root
            ),
            static_root=(
                overrides.static_root
                if overrides.static_root is not None
                else defaults.static_root
            ),
            static_root_configured=overrides.static_root is not None,
            migrations_root=(
                overrides.migrations_root
                if overrides.migrations_root is not None
                else defaults.migrations_root
            ),
            static_url_path=(
                overrides.static_url_path
                if overrides.static_url_path is not None
                else defaults.static_url_path
            ),
        )
    except ConfigurationError as exc:
        raise ProjectToolConfigurationError(str(exc)) from exc


@click.command(
    name="wybra-validate",
    context_settings={"help_option_names": ["-h", "--help"]},
    help=(
        "Run project validation checks. Examples: wybra-validate, "
        "wybra-validate --verbose web persistence."
    ),
)
@click.option(
    "-v",
    "--verbose",
    is_flag=True,
    help="Show the concrete validation checks performed for each target.",
)
@click.option(
    "--template-root",
    type=click.Path(path_type=Path),
    help="Override the configured template root for web validation.",
)
@click.option(
    "--static-root",
    type=click.Path(path_type=Path),
    help="Override the configured static root for web validation.",
)
@click.option(
    "--static-url-path",
    help="Override the configured static URL prefix for web validation.",
)
@click.option(
    "--database-url",
    help=(
        "Override the configured SQLAlchemy async database URL. Verbose output "
        "redacts embedded credentials."
    ),
)
@click.option(
    "--migrations-root",
    type=click.Path(path_type=Path),
    help="Override the configured Alembic migrations root.",
)
@click.argument("targets", nargs=-1)
def validate_command(
    targets: tuple[str, ...],
    verbose: bool,
    template_root: Path | None,
    static_root: Path | None,
    static_url_path: str | None,
    database_url: str | None,
    migrations_root: Path | None,
) -> int:
    overrides = ValidationOverrides(
        database_url=database_url,
        template_root=template_root,
        static_root=static_root,
        migrations_root=migrations_root,
        static_url_path=static_url_path,
    )
    try:
        settings = _build_settings(overrides)
    except ProjectToolConfigurationError as exc:
        print("configuration: failed", file=sys.stderr)
        print(f"- {exc}", file=sys.stderr)
        return 1

    try:
        discovered = discover_validation_target_details(settings.modules)
        validation_targets = _merge_validation_targets(
            builtin_targets=BUILTIN_VALIDATION_TARGETS,
            discovered_targets=discovered.targets,
            discovered_origins=discovered.origins,
        )
    except ValidationDiscoveryError as exc:
        print("validation discovery: failed", file=sys.stderr)
        print(f"- {exc}", file=sys.stderr)
        return 1
    except ConfigurationError as exc:
        print("configuration: failed", file=sys.stderr)
        print(f"- {exc}", file=sys.stderr)
        return 1

    try:
        resolved_targets = _resolve_targets(targets, tuple(validation_targets))
    except UnknownValidationTargetError as exc:
        raise click.UsageError(str(exc)) from exc

    return _run_validation_targets(
        resolved_targets,
        validation_targets,
        settings,
        verbose=verbose,
    )


def main(argv: Sequence[str] | None = None) -> int:
    try:
        result = validate_command.main(
            args=None if argv is None else list(argv),
            prog_name="wybra-validate",
            standalone_mode=False,
        )
    except click.exceptions.Exit as exc:
        return int(exc.exit_code or 0)
    except click.ClickException as exc:
        exc.show()
        return int(exc.exit_code or 1)
    return int(result or 0)


def _run_validation_targets(
    target_names: Sequence[str],
    validation_targets: Mapping[str, ValidationTarget],
    settings: Any,
    *,
    verbose: bool,
) -> int:
    exit_code = 0

    for target_name in target_names:
        result = validation_targets[target_name](settings)

        if result.is_ok:
            print(f"{result.name}: ok")
            if verbose:
                _print_verbose_checks(result)
            continue

        exit_code = 1
        print(f"{result.name}: failed", file=sys.stderr)
        if verbose:
            _print_verbose_checks(result, file=sys.stderr)
        for error in result.errors:
            print(f"- {error}", file=sys.stderr)

    return exit_code


def _print_verbose_checks(
    result: ValidationResult, *, file: TextIO | None = None
) -> None:
    output = sys.stdout if file is None else file
    for check in result.checks:
        status = "ok" if check.passed else "failed"
        print(f"  - {status}: {check.description}", file=output)
