"""Static asset serving and export support."""

from __future__ import annotations

import mimetypes
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from fastapi.staticfiles import StaticFiles
from starlette.responses import PlainTextResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

from wevra.core.composition import AppConfig, load_app_config
from wevra.core.resources import (
    PackageResourceFile,
    PackageResourceSource,
    ResourcePathError,
    first_existing_resource,
    iter_package_resource_files,
)
from wevra.utils.paths import resolve_project_path
from wevra.web.routes.discovery import static_sources_from_modules


@dataclass(frozen=True, slots=True)
class StaticExportedAsset:
    logical_path: str
    source: PackageResourceSource
    destination: Path


@dataclass(frozen=True, slots=True)
class StaticAssetDuplicate:
    logical_path: str
    winner: PackageResourceSource
    shadowed: PackageResourceSource


@dataclass(frozen=True, slots=True)
class StaticExportResult:
    export_root: Path
    exported_assets: tuple[StaticExportedAsset, ...]
    duplicates: tuple[StaticAssetDuplicate, ...]


class ComposedStaticFiles:
    """Serve static assets from a composed logical package-resource namespace."""

    def __init__(self, sources: tuple[PackageResourceSource, ...]) -> None:
        if not sources:
            raise ValueError("At least one static source is required.")

        self.sources = sources

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            raise RuntimeError("Composed static files only support HTTP scopes.")

        method = scope.get("method", "GET").upper()
        if method not in {"GET", "HEAD"}:
            response = PlainTextResponse(
                "Method Not Allowed",
                status_code=405,
                headers={"Allow": "GET, HEAD"},
            )
            await response(scope, receive, send)
            return

        logical_path = _logical_path_from_scope(scope)
        response = static_asset_response(
            self.sources,
            logical_path,
            include_body=method != "HEAD",
        )
        await response(scope, receive, send)


class NoStaticFiles:
    """Preserve static URL generation when no static source is configured."""

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            raise RuntimeError("No static files only supports HTTP scopes.")

        method = scope.get("method", "GET").upper()
        if method not in {"GET", "HEAD"}:
            response = PlainTextResponse(
                "Method Not Allowed",
                status_code=405,
                headers={"Allow": "GET, HEAD"},
            )
            await response(scope, receive, send)
            return

        response = PlainTextResponse(
            "" if method == "HEAD" else "Not Found",
            status_code=404,
        )
        await response(scope, receive, send)


def static_app_from_config(
    *,
    project_root: Path,
    static_root: Path | None,
    static_sources: tuple[PackageResourceSource, ...],
) -> ASGIApp:
    """Build the runtime static ASGI app for filesystem and module assets."""
    resolved_static_root = _resolve_static_root(project_root, static_root)
    if resolved_static_root is not None:
        return StaticFiles(directory=resolved_static_root, check_dir=True)
    if static_sources:
        return ComposedStaticFiles(static_sources)

    return NoStaticFiles()


def static_asset_response(
    sources: tuple[PackageResourceSource, ...],
    logical_path: str,
    *,
    include_body: bool = True,
) -> Response:
    try:
        resource = first_existing_resource(sources, logical_path)
    except ResourcePathError:
        resource = None

    if resource is None:
        return PlainTextResponse("Not Found", status_code=404)

    content = resource.read_bytes() if include_body else b""
    media_type, _encoding = mimetypes.guess_type(logical_path)
    return Response(
        content,
        media_type=media_type or "application/octet-stream",
    )


def export_configured_static_assets(
    *,
    project_root: Path | None = None,
    config_path: Path | None = None,
    environ: Mapping[str, str] | None = None,
    export_root: Path | None = None,
) -> StaticExportResult:
    config = load_app_config(
        project_root=project_root,
        config_path=config_path,
        environ=environ,
    )
    return export_static_assets(
        static_sources_from_modules(config.modules),
        export_root=_resolve_export_root(config, export_root),
    )


def export_static_assets(
    sources: tuple[PackageResourceSource, ...],
    *,
    export_root: Path,
) -> StaticExportResult:
    resolved_export_root = export_root.resolve()
    resolved_export_root.mkdir(parents=True, exist_ok=True)
    winners: dict[str, PackageResourceFile] = {}
    exported_assets: list[StaticExportedAsset] = []
    duplicates: list[StaticAssetDuplicate] = []

    for source in sources:
        for asset in iter_package_resource_files(source):
            winner = winners.get(asset.logical_path)
            if winner is not None:
                duplicates.append(
                    StaticAssetDuplicate(
                        logical_path=asset.logical_path,
                        winner=winner.source,
                        shadowed=asset.source,
                    )
                )
                continue

            winners[asset.logical_path] = asset
            destination = resolved_export_root / asset.logical_path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(asset.resource.read_bytes())
            exported_assets.append(
                StaticExportedAsset(
                    logical_path=asset.logical_path,
                    source=asset.source,
                    destination=destination,
                )
            )

    return StaticExportResult(
        export_root=resolved_export_root,
        exported_assets=tuple(exported_assets),
        duplicates=tuple(duplicates),
    )


def _logical_path_from_scope(scope: Scope) -> str:
    path = scope.get("path", "")
    root_path = scope.get("root_path", "")
    if root_path and path.startswith(root_path):
        path = path[len(root_path) :]

    return path.lstrip("/")


def _resolve_static_root(project_root: Path, static_root: Path | None) -> Path | None:
    return resolve_project_path(project_root, static_root)


def _resolve_export_root(config: AppConfig, export_root: Path | None) -> Path:
    path = export_root if export_root is not None else config.static.export_root
    if not path.is_absolute():
        path = config.project_root / path

    return path


__all__ = [
    "ComposedStaticFiles",
    "NoStaticFiles",
    "StaticAssetDuplicate",
    "StaticExportResult",
    "StaticExportedAsset",
    "export_configured_static_assets",
    "export_static_assets",
    "static_app_from_config",
    "static_asset_response",
]
