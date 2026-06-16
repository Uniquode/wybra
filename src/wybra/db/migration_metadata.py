"""Compose migration metadata from configured module data surfaces.

This adapter is the boundary between generic data surface discovery and Alembic.
It preserves nested import failures, but wraps composition/configuration errors
as `MigrationConfigError` so CLIs can report expected configuration failures
without catching unrelated runtime exceptions.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path

from sqlalchemy import MetaData

from wybra.core.composition import (
    CompositionError,
    load_app_config_modules,
)
from wybra.core.diagnostics import wrapped_error
from wybra.db.surfaces import (
    DataCompositionError,
)
from wybra.db.surfaces import (
    metadata_from_model_package as _db_metadata_from_model_package,
)
from wybra.db.surfaces import (
    model_packages_from_modules as _model_packages_from_modules,
)


class MigrationConfigError(RuntimeError):
    """Raised when migration metadata configuration cannot be resolved."""


def load_model_metadata(
    model_packages: Sequence[str] | None = None,
    *,
    modules: Sequence[str] | None = None,
    default_modules: Sequence[str] | None = None,
    project_root: Path | None = None,
    config_path: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> tuple[MetaData, ...]:
    """Load SQLAlchemy metadata from configured module model surfaces.

    Host applications can still pass an explicit ordered model package sequence
    for tests and specialised migration flows. The default path derives
    conventional ``<module>.models`` packages from the shared composition
    configuration.
    """
    if model_packages is None:
        model_packages = model_packages_from_modules(
            modules
            if modules is not None
            else _load_modules_from_config(
                project_root=project_root,
                config_path=config_path,
                environ=environ,
                default_modules=default_modules,
            )
        )

    return _deduplicate_metadata(
        _metadata_from_model_package(package_name) for package_name in model_packages
    )


def model_packages_from_modules(
    modules: Sequence[str],
) -> tuple[str, ...]:
    try:
        return _model_packages_from_modules(tuple(modules))
    except DataCompositionError as exc:
        raise wrapped_error(MigrationConfigError, exc) from exc


def _load_modules_from_config(
    *,
    project_root: Path | None,
    config_path: Path | None,
    environ: Mapping[str, str] | None,
    default_modules: Sequence[str] | None,
) -> tuple[str, ...]:
    try:
        return load_app_config_modules(
            project_root=project_root,
            config_path=config_path,
            environ=environ if environ is not None else os.environ,
            default_modules=default_modules,
        )
    except CompositionError as exc:
        raise wrapped_error(MigrationConfigError, exc) from exc


def _metadata_from_model_package(package_name: str) -> MetaData:
    try:
        return _db_metadata_from_model_package(package_name)
    except DataCompositionError as exc:
        raise wrapped_error(MigrationConfigError, exc) from exc


def _deduplicate_metadata(metadata_values: Iterable[MetaData]) -> tuple[MetaData, ...]:
    unique_values: list[MetaData] = []
    seen_ids: set[int] = set()
    for metadata in metadata_values:
        metadata_id = id(metadata)
        if metadata_id in seen_ids:
            continue
        seen_ids.add(metadata_id)
        unique_values.append(metadata)

    return tuple(unique_values)
