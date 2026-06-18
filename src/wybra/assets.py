"""Static asset discovery, serving, and export support."""

from __future__ import annotations

import mimetypes
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from importlib import resources
from importlib.util import find_spec
from pathlib import Path

from fastapi.staticfiles import StaticFiles
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import PlainTextResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

from wybra.core.composition import (
    AppConfig,
    AssetCorsOptions,
    AssetCorsPolicy,
    CompositionError,
    load_app_config,
)
from wybra.core.conventions import STATIC_RESOURCE_DIRECTORY
from wybra.core.diagnostics import configured_module_message
from wybra.core.resources import (
    PackageResourceFile,
    PackageResourceSource,
    ResourcePathError,
    first_existing_resource,
    iter_package_resource_files,
)
from wybra.utils.paths import resolve_project_path


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


def discover_static_sources(module_name: str) -> tuple[PackageResourceSource, ...]:
    return _discover_resource_sources(module_name, STATIC_RESOURCE_DIRECTORY)


def static_sources_from_modules(
    module_names: tuple[str, ...],
) -> tuple[PackageResourceSource, ...]:
    return _resource_sources_from_modules(module_names, discover_static_sources)


def static_app_from_config(
    *,
    project_root: Path,
    static_root: Path | None,
    static_sources: tuple[PackageResourceSource, ...],
    cors: AssetCorsOptions | None = None,
    url_path: str | None = None,
) -> ASGIApp:
    """Build the runtime static ASGI app.

    A configured filesystem static root takes precedence over module static
    assets. Module assets are served only when no filesystem root is configured.
    """
    resolved_static_root = _resolve_static_root(project_root, static_root)
    if resolved_static_root is not None:
        app: ASGIApp = StaticFiles(directory=resolved_static_root, check_dir=True)
        return _asset_cors_app(app, cors, url_path)
    if static_sources:
        return _asset_cors_app(ComposedStaticFiles(static_sources), cors, url_path)

    return _asset_cors_app(NoStaticFiles(), cors, url_path)


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


def _discover_resource_sources(
    module_name: str,
    directory: str,
) -> tuple[PackageResourceSource, ...]:
    if _resource_directory_exists(module_name, directory):
        return (PackageResourceSource(package=module_name, directory=directory),)

    return ()


def _resource_sources_from_modules(
    module_names: tuple[str, ...],
    discover_sources: Callable[[str], tuple[PackageResourceSource, ...]],
) -> tuple[PackageResourceSource, ...]:
    sources: list[PackageResourceSource] = []
    for module_name in module_names:
        _require_configured_module(module_name)
        sources.extend(discover_sources(module_name))

    return tuple(sources)


def _resource_directory_exists(module_name: str, directory: str) -> bool:
    try:
        return resources.files(module_name).joinpath(directory).is_dir()
    except (ModuleNotFoundError, TypeError):
        return False


def _require_configured_module(module_name: str) -> None:
    if find_spec(module_name) is None:
        raise CompositionError(
            configured_module_message(module_name, "could not be imported.")
        )


def _resolve_static_root(project_root: Path, static_root: Path | None) -> Path | None:
    return resolve_project_path(project_root, static_root)


def _resolve_export_root(config: AppConfig, export_root: Path | None) -> Path:
    path = export_root if export_root is not None else config.assets.export_root
    if not path.is_absolute():
        path = config.project_root / path

    return path


def _asset_cors_app(
    app: ASGIApp,
    cors: AssetCorsOptions | None,
    url_path: str | None,
) -> ASGIApp:
    if cors is None or (not cors.enabled and not cors.paths):
        return app

    default_app = _cors_wrapped_app(app, cors) if cors.enabled else app
    path_apps = tuple(
        (_cors_path_candidates(path, url_path), _cors_wrapped_app(app, policy))
        for path, policy in sorted(
            cors.paths.items(),
            key=lambda item: len(item[0]),
            reverse=True,
        )
    )
    if not path_apps:
        return default_app
    return PathCorsStaticFiles(default_app=default_app, path_apps=path_apps)


@dataclass(frozen=True, slots=True)
class PathCorsStaticFiles:
    default_app: ASGIApp
    path_apps: tuple[tuple[tuple[str, ...], ASGIApp], ...]

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.default_app(scope, receive, send)
            return

        request_paths = _request_path_candidates(scope)
        for prefixes, app in self.path_apps:
            if any(
                _path_matches_prefix(path, prefix)
                for prefix in prefixes
                for path in request_paths
            ):
                await app(scope, receive, send)
                return

        await self.default_app(scope, receive, send)


def _cors_wrapped_app(app: ASGIApp, policy: AssetCorsPolicy) -> ASGIApp:
    return CORSMiddleware(
        app=app,
        allow_origins=list(policy.allow_origins),
        allow_methods=list(policy.allow_methods),
        allow_headers=list(policy.allow_headers),
        allow_credentials=policy.allow_credentials,
        expose_headers=list(policy.expose_headers),
        max_age=policy.max_age,
    )


def _request_path_candidates(scope: Scope) -> tuple[str, ...]:
    path = _normalise_url_path(scope.get("path", ""))
    logical_path = _normalise_url_path(_logical_path_from_scope(scope))
    return (path,) if path == logical_path else (path, logical_path)


def _cors_path_candidates(path: str, url_path: str | None) -> tuple[str, ...]:
    configured_path = _normalise_url_path(path)
    if url_path is None:
        return (configured_path,)

    mount_path = _normalise_url_path(url_path)
    if configured_path == mount_path:
        return (configured_path, "/")
    mount_prefix = f"{mount_path.rstrip('/')}/"
    if configured_path.startswith(mount_prefix):
        relative_path = configured_path[len(mount_path.rstrip("/")) :]
        return (configured_path, _normalise_url_path(relative_path))
    return (configured_path,)


def _path_matches_prefix(path: str, prefix: str) -> bool:
    normalised_prefix = _normalise_url_path(prefix).rstrip("/")
    if normalised_prefix == "":
        return True
    return path == normalised_prefix or path.startswith(f"{normalised_prefix}/")


def _normalise_url_path(path: str) -> str:
    """Normalise a request URL path without forcing a trailing slash."""
    return f"/{path.strip('/')}" if path.strip("/") else "/"


__all__ = [
    "ComposedStaticFiles",
    "NoStaticFiles",
    "PathCorsStaticFiles",
    "StaticAssetDuplicate",
    "StaticExportResult",
    "StaticExportedAsset",
    "discover_static_sources",
    "export_configured_static_assets",
    "export_static_assets",
    "static_app_from_config",
    "static_asset_response",
    "static_sources_from_modules",
]
