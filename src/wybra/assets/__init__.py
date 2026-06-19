"""Public static asset API."""

from __future__ import annotations

from importlib import import_module
from typing import Any

_EXPORT_MODULES = {
    "ComposedStaticFiles": "wybra.assets.serving",
    "NoStaticFiles": "wybra.assets.serving",
    "NormalStaticAssetStorage": "wybra.assets.storage",
    "PathCorsStaticFiles": "wybra.assets.serving",
    "StaticAssetDuplicate": "wybra.assets.collection",
    "StaticAssetStorage": "wybra.assets.storage",
    "StaticCollectionError": "wybra.assets.collection",
    "StaticCollectionStatus": "wybra.assets.collection",
    "StaticCollectResult": "wybra.assets.collection",
    "StaticCollectedAsset": "wybra.assets.collection",
    "StaticDeletedAsset": "wybra.assets.collection",
    "StaticSkippedAsset": "wybra.assets.collection",
    "asset_url": "wybra.assets.storage",
    "collect_configured_static_assets": "wybra.assets.collection",
    "collect_static_assets": "wybra.assets.collection",
    "discover_static_sources": "wybra.assets.serving",
    "static_app_from_config": "wybra.assets.serving",
    "static_asset_response": "wybra.assets.serving",
    "static_asset_storage": "wybra.assets.storage",
    "static_sources_from_modules": "wybra.assets.serving",
}


def __getattr__(name: str) -> Any:
    module_name = _EXPORT_MODULES.get(name)
    if module_name is None:
        raise AttributeError(f"module 'wybra.assets' has no attribute {name!r}")
    value = getattr(import_module(module_name), name)
    globals()[name] = value
    return value


__all__ = [
    "ComposedStaticFiles",
    "NoStaticFiles",
    "NormalStaticAssetStorage",
    "PathCorsStaticFiles",
    "StaticAssetDuplicate",
    "StaticAssetStorage",
    "StaticCollectionError",
    "StaticCollectionStatus",
    "StaticCollectResult",
    "StaticCollectedAsset",
    "StaticDeletedAsset",
    "StaticSkippedAsset",
    "asset_url",
    "collect_configured_static_assets",
    "collect_static_assets",
    "discover_static_sources",
    "static_app_from_config",
    "static_asset_response",
    "static_asset_storage",
    "static_sources_from_modules",
]
