"""Static asset storage backends and URL resolution."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import TYPE_CHECKING, Protocol, runtime_checkable
from urllib.parse import quote

from wybra.assets.config import AssetExportMode, parse_asset_export_mode
from wybra.core.exceptions import ConfigurationError, InputValidationError
from wybra.core.resources import PackageResourceSource

if TYPE_CHECKING:
    from wybra.assets.collection import StaticCollectResult


@runtime_checkable
class StaticAssetStorage(Protocol):
    mode: AssetExportMode

    def collect(
        self,
        sources: tuple[PackageResourceSource, ...],
        *,
        root: Path,
        delete: bool,
    ) -> StaticCollectResult: ...

    def url(self, logical_path: str, *, url_path: str) -> str: ...


class NormalStaticAssetStorage:
    mode: AssetExportMode = AssetExportMode.NORMAL

    def collect(
        self,
        sources: tuple[PackageResourceSource, ...],
        *,
        root: Path,
        delete: bool,
    ) -> StaticCollectResult:
        from wybra.assets.collection import collect_normal_static_assets

        return collect_normal_static_assets(sources, root=root, delete=delete)

    def url(self, logical_path: str, *, url_path: str) -> str:
        return _joined_static_asset_url(url_path, logical_path)


def asset_url(
    logical_path: str,
    *,
    url_path: str = "/static/",
    export_mode: AssetExportMode | str = AssetExportMode.NORMAL,
) -> str:
    """Return the public URL for a logical static asset path.

    ``logical_path`` is an asset-namespace path such as ``"styles/app.css"``.
    It must be relative and must not contain ``..`` path segments.
    """
    storage = static_asset_storage(export_mode)
    return storage.url(logical_path, url_path=url_path)


def static_asset_storage(export_mode: AssetExportMode | str) -> StaticAssetStorage:
    try:
        resolved_mode = parse_asset_export_mode(export_mode)
    except ValueError as exc:
        raise ConfigurationError(str(exc)) from exc

    try:
        return _STATIC_ASSET_STORAGES[resolved_mode]
    except KeyError as exc:
        raise ConfigurationError(
            f"static asset export mode {resolved_mode.value!r} is not implemented"
        ) from exc


def _joined_static_asset_url(url_path: str, logical_path: str) -> str:
    prefix = _normalise_url_path(url_path).rstrip("/")
    path = normalise_logical_asset_path(logical_path)
    return f"{prefix}/{quote(path, safe='/')}"


def _normalise_url_path(path: str) -> str:
    """Normalise a request URL path without forcing a trailing slash."""
    return f"/{path.strip('/')}" if path.strip("/") else "/"


def normalise_logical_asset_path(logical_path: str) -> str:
    if logical_path != logical_path.strip():
        raise InputValidationError(
            "Logical asset paths must not have leading or trailing whitespace."
        )

    path = PurePosixPath(logical_path)
    if path.is_absolute():
        raise InputValidationError(
            "Logical asset paths must be relative and cannot start with '/'."
        )

    parts = tuple(part for part in path.parts if part != ".")
    if not parts or any(part == ".." for part in parts):
        raise InputValidationError(
            "Logical asset paths must not contain '..' segments that escape "
            "the namespace."
        )
    return "/".join(parts)


_NORMAL_STATIC_ASSET_STORAGE = NormalStaticAssetStorage()
_STATIC_ASSET_STORAGES: Mapping[AssetExportMode, StaticAssetStorage] = MappingProxyType(
    {
        _NORMAL_STATIC_ASSET_STORAGE.mode: _NORMAL_STATIC_ASSET_STORAGE,
    }
)


__all__ = [
    "NormalStaticAssetStorage",
    "StaticAssetStorage",
    "asset_url",
    "normalise_logical_asset_path",
    "static_asset_storage",
]
