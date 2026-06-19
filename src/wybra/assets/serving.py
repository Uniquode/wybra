"""Runtime static asset discovery and ASGI serving."""

from __future__ import annotations

import mimetypes
from collections.abc import Callable
from dataclasses import dataclass
from importlib import resources
from importlib.util import find_spec
from pathlib import Path

from fastapi.staticfiles import StaticFiles
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import PlainTextResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

from wybra.assets.config import AssetCorsOptions, AssetCorsPolicy
from wybra.core.composition import CompositionError
from wybra.core.conventions import STATIC_RESOURCE_DIRECTORY
from wybra.core.diagnostics import configured_module_message
from wybra.core.exceptions import ConfigurationError, InputValidationError
from wybra.core.resources import (
    PackageResourceSource,
    ResourcePathError,
    first_existing_resource,
)
from wybra.utils.paths import resolve_project_path


class ComposedStaticFiles:
    """Serve static assets from a composed logical package-resource namespace."""

    def __init__(self, sources: tuple[PackageResourceSource, ...]) -> None:
        if not sources:
            raise InputValidationError("At least one static source is required.")

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

    A directly supplied filesystem static root takes precedence over module
    static assets. Site startup passes no filesystem root so app-served runtime
    static files come from configured module static sources.
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
    resolved = resolve_project_path(project_root, static_root)
    if resolved is None:
        return None
    if not resolved.exists():
        raise ConfigurationError(
            f"Configured static asset root does not exist: {resolved}"
        )
    if not resolved.is_dir():
        raise ConfigurationError(
            f"Configured static asset root is not a directory: {resolved}"
        )
    return resolved


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
    "discover_static_sources",
    "static_app_from_config",
    "static_asset_response",
    "static_sources_from_modules",
]
