"""Static asset collection and deployment manifest handling."""

from __future__ import annotations

import json
import shutil
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from enum import StrEnum
from importlib import resources
from pathlib import Path
from uuid import uuid4

from wybra.assets.config import AssetExportMode
from wybra.assets.storage import normalise_logical_asset_path, static_asset_storage
from wybra.core.composition import AppConfig, load_app_config
from wybra.core.exceptions import InputValidationError
from wybra.core.resources import (
    PackageResourceFile,
    PackageResourceSource,
    iter_package_resource_files,
)

STATIC_COLLECT_MANIFEST_FILENAME = ".wybra-static-collect.json"
STATIC_COLLECT_MANIFEST_VERSION = 1
STATIC_COLLECT_MANIFEST_VERSION_KEY = "version"
STATIC_COLLECT_MANIFEST_ASSETS_KEY = "assets"


class StaticCollectionStatus(StrEnum):
    COPIED = "copied"
    UPDATED = "updated"
    UNCHANGED = "unchanged"


class StaticCollectionError(OSError):
    """Raised when static asset collection cannot complete."""


@dataclass(frozen=True, slots=True)
class StaticCollectedAsset:
    logical_path: str
    source: PackageResourceSource
    destination: Path
    status: StaticCollectionStatus


@dataclass(frozen=True, slots=True)
class StaticDeletedAsset:
    logical_path: str
    destination: Path


@dataclass(frozen=True, slots=True)
class StaticSkippedAsset:
    logical_path: str
    source: PackageResourceSource
    reason: str


@dataclass(frozen=True, slots=True)
class StaticAssetDuplicate:
    logical_path: str
    winner: PackageResourceSource
    shadowed: PackageResourceSource


@dataclass(frozen=True, slots=True)
class StaticCollectResult:
    root: Path
    collected_assets: tuple[StaticCollectedAsset, ...]
    deleted_assets: tuple[StaticDeletedAsset, ...]
    skipped_assets: tuple[StaticSkippedAsset, ...]
    duplicates: tuple[StaticAssetDuplicate, ...]


def collect_configured_static_assets(
    *,
    project_root: Path | None = None,
    config_path: Path | None = None,
    environ: Mapping[str, str] | None = None,
    root: Path | None = None,
) -> StaticCollectResult:
    from wybra.assets.serving import static_sources_from_modules

    config = load_app_config(
        project_root=project_root,
        config_path=config_path,
        environ=environ,
    )
    return collect_static_assets(
        static_sources_from_modules(config.modules),
        root=_resolve_root(config, root),
        export_mode=config.assets.export_mode,
    )


def collect_static_assets(
    sources: tuple[PackageResourceSource, ...],
    *,
    root: Path,
    delete: bool = True,
    export_mode: AssetExportMode | str = AssetExportMode.NORMAL,
) -> StaticCollectResult:
    storage = static_asset_storage(export_mode)
    return storage.collect(sources, root=root, delete=delete)


def collect_normal_static_assets(
    sources: tuple[PackageResourceSource, ...],
    *,
    root: Path,
    delete: bool,
) -> StaticCollectResult:
    resolved_root = root.resolve()
    try:
        resolved_root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise StaticCollectionError(
            f"create failed for static collection root {resolved_root}: {exc}"
        ) from exc
    previous_manifest_paths = _read_collection_manifest(resolved_root)
    winners: dict[str, PackageResourceFile] = {}
    collected_assets: list[StaticCollectedAsset] = []
    deleted_assets: list[StaticDeletedAsset] = []
    skipped_assets: list[StaticSkippedAsset] = []
    duplicates: list[StaticAssetDuplicate] = []

    for source in sources:
        for asset in iter_package_resource_files(source):
            if asset.logical_path == STATIC_COLLECT_MANIFEST_FILENAME:
                skipped_assets.append(
                    StaticSkippedAsset(
                        logical_path=asset.logical_path,
                        source=asset.source,
                        reason="reserved",
                    )
                )
                continue

            winner = winners.get(asset.logical_path)
            if winner is not None:
                duplicates.append(
                    StaticAssetDuplicate(
                        logical_path=asset.logical_path,
                        winner=winner.source,
                        shadowed=asset.source,
                    )
                )
                skipped_assets.append(
                    StaticSkippedAsset(
                        logical_path=asset.logical_path,
                        source=asset.source,
                        reason="shadowed",
                    )
                )
                continue

            winners[asset.logical_path] = asset
            destination = resolved_root / asset.logical_path
            collected_assets.append(
                StaticCollectedAsset(
                    logical_path=asset.logical_path,
                    source=asset.source,
                    destination=destination,
                    status=_copy_asset(asset, destination),
                )
            )

    expected_logical_paths = frozenset(winners)
    if delete:
        deleted_assets.extend(
            _delete_stale_assets(
                resolved_root,
                previous_manifest_paths,
                expected_logical_paths,
            )
        )
        manifest_paths = expected_logical_paths
    else:
        manifest_paths = previous_manifest_paths | expected_logical_paths
    _write_collection_manifest(
        resolved_root,
        manifest_paths,
        previous_logical_paths=previous_manifest_paths,
    )

    return StaticCollectResult(
        root=resolved_root,
        collected_assets=tuple(collected_assets),
        deleted_assets=tuple(deleted_assets),
        skipped_assets=tuple(skipped_assets),
        duplicates=tuple(duplicates),
    )


def _resolve_root(config: AppConfig, root: Path | None) -> Path:
    path = root if root is not None else config.assets.root
    if not path.is_absolute():
        path = config.project_root / path

    return path


def _copy_asset(
    asset: PackageResourceFile,
    destination: Path,
) -> StaticCollectionStatus:
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        with resources.as_file(asset.resource) as source_path:
            status = (
                StaticCollectionStatus.UNCHANGED
                if _same_file(source_path, destination)
                else _changed_status(destination)
            )
            if status is not StaticCollectionStatus.UNCHANGED:
                shutil.copy2(source_path, destination)
            return status
    except OSError as exc:
        raise StaticCollectionError(
            f"copy failed for static asset {asset.logical_path!r}: {exc}"
        ) from exc


def _same_file(source: Path, destination: Path) -> bool:
    """Return True when source and destination have the same file content."""
    if not destination.is_file():
        return False

    source_stat = source.stat()
    destination_stat = destination.stat()

    if (
        source_stat.st_dev == destination_stat.st_dev
        and source_stat.st_ino == destination_stat.st_ino
        and source_stat.st_size == destination_stat.st_size
        and source_stat.st_mtime_ns == destination_stat.st_mtime_ns
    ):
        return True

    if (
        source_stat.st_size == destination_stat.st_size
        and source_stat.st_mtime_ns == destination_stat.st_mtime_ns
    ):
        return _same_file_content(source, destination)

    return False


def _same_file_content(source: Path, destination: Path) -> bool:
    buffer_size = 1024 * 1024
    with source.open("rb") as source_file, destination.open("rb") as destination_file:
        while True:
            source_chunk = source_file.read(buffer_size)
            destination_chunk = destination_file.read(buffer_size)
            if source_chunk != destination_chunk:
                return False
            if not source_chunk:
                return True


def _changed_status(destination: Path) -> StaticCollectionStatus:
    return (
        StaticCollectionStatus.UPDATED
        if destination.exists()
        else StaticCollectionStatus.COPIED
    )


def _delete_stale_assets(
    root: Path,
    managed_logical_paths: frozenset[str],
    expected_logical_paths: frozenset[str],
) -> tuple[StaticDeletedAsset, ...]:
    deleted_assets: list[StaticDeletedAsset] = []
    for logical_path in sorted(managed_logical_paths - expected_logical_paths):
        path = root / logical_path
        if not path.exists():
            continue
        if not path.is_file():
            continue
        try:
            path.unlink()
        except OSError as exc:
            raise StaticCollectionError(
                f"delete failed for static asset {logical_path!r}: {exc}"
            ) from exc
        deleted_assets.append(
            StaticDeletedAsset(
                logical_path=logical_path,
                destination=path,
            )
        )
    return tuple(deleted_assets)


def _read_collection_manifest(root: Path) -> frozenset[str]:
    manifest_path = root / STATIC_COLLECT_MANIFEST_FILENAME
    if not manifest_path.is_file():
        return frozenset()

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise StaticCollectionError(
            f"static collect manifest {manifest_path} could not be read: {exc}"
        ) from exc

    if not isinstance(manifest, Mapping):
        raise StaticCollectionError(
            f"static collect manifest {manifest_path} must be a JSON object."
        )
    if (
        manifest.get(STATIC_COLLECT_MANIFEST_VERSION_KEY)
        != STATIC_COLLECT_MANIFEST_VERSION
    ):
        raise StaticCollectionError(
            f"static collect manifest {manifest_path} has unsupported version."
        )

    assets = manifest.get(STATIC_COLLECT_MANIFEST_ASSETS_KEY)
    if not isinstance(assets, list):
        raise StaticCollectionError(
            f"static collect manifest {manifest_path} must contain an assets list."
        )

    logical_paths: set[str] = set()
    for index, asset in enumerate(assets):
        if not isinstance(asset, str):
            raise StaticCollectionError(
                "static collect manifest "
                f"{manifest_path} contains a non-text path at index {index}: "
                f"{asset!r}"
            )
        try:
            logical_paths.add(normalise_logical_asset_path(asset))
        except InputValidationError as exc:
            raise StaticCollectionError(
                "static collect manifest "
                f"{manifest_path} contains an invalid path at index {index}: "
                f"{asset!r}"
            ) from exc

    return frozenset(logical_paths)


def _write_collection_manifest(
    root: Path,
    logical_paths: frozenset[str],
    *,
    previous_logical_paths: frozenset[str],
) -> None:
    if logical_paths == previous_logical_paths:
        return

    manifest_path = root / STATIC_COLLECT_MANIFEST_FILENAME
    manifest = {
        STATIC_COLLECT_MANIFEST_VERSION_KEY: STATIC_COLLECT_MANIFEST_VERSION,
        STATIC_COLLECT_MANIFEST_ASSETS_KEY: sorted(logical_paths),
    }
    temp_manifest_path = manifest_path.with_name(
        f"{manifest_path.name}.{uuid4().hex}.tmp"
    )
    try:
        temp_manifest_path.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temp_manifest_path.replace(manifest_path)
    except OSError as exc:
        with suppress(OSError):
            temp_manifest_path.unlink()
        raise StaticCollectionError(
            f"static collect manifest {manifest_path} could not be written: {exc}"
        ) from exc


__all__ = [
    "STATIC_COLLECT_MANIFEST_ASSETS_KEY",
    "STATIC_COLLECT_MANIFEST_FILENAME",
    "STATIC_COLLECT_MANIFEST_VERSION",
    "STATIC_COLLECT_MANIFEST_VERSION_KEY",
    "StaticAssetDuplicate",
    "StaticCollectResult",
    "StaticCollectedAsset",
    "StaticCollectionError",
    "StaticCollectionStatus",
    "StaticDeletedAsset",
    "StaticSkippedAsset",
    "collect_configured_static_assets",
    "collect_normal_static_assets",
    "collect_static_assets",
]
