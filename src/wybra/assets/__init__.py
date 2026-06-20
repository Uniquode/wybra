"""Public static asset API."""

from __future__ import annotations

from importlib import import_module
from typing import Any

_EXPORT_MODULES = {
    "ComposedStaticFiles": "wybra.assets.serving",
    "AssetSettings": "wybra.assets.settings",
    "DefaultStaticAssetCapability": "wybra.assets.capabilities",
    "NoStaticFiles": "wybra.assets.serving",
    "NormalStaticAssetStorage": "wybra.assets.storage",
    "PathCorsStaticFiles": "wybra.assets.serving",
    "StaticAssetCapability": "wybra.assets.capabilities",
    "StaticAssetDuplicate": "wybra.assets.collection",
    "StaticAssetStorage": "wybra.assets.storage",
    "StaticCollectionError": "wybra.assets.collection",
    "StaticCollectionStatus": "wybra.assets.collection",
    "StaticCollectResult": "wybra.assets.collection",
    "StaticCollectedAsset": "wybra.assets.collection",
    "StaticDeletedAsset": "wybra.assets.collection",
    "StaticSkippedAsset": "wybra.assets.collection",
    "asset_collection_root": "wybra.assets.validation",
    "asset_url": "wybra.assets.storage",
    "collect_configured_static_assets": "wybra.assets.collection",
    "collect_static_assets": "wybra.assets.collection",
    "discover_static_sources": "wybra.assets.serving",
    "module_config": "wybra.assets.config",
    "record_asset_collection_root_check": "wybra.assets.validation",
    "require_static_asset_capability": "wybra.assets.capabilities",
    "static_app_from_config": "wybra.assets.serving",
    "static_asset_response": "wybra.assets.serving",
    "static_asset_storage": "wybra.assets.storage",
    "static_location_for_validation": "wybra.assets.validation",
    "static_resource_for_validation": "wybra.assets.validation",
    "static_sources_for_validation": "wybra.assets.validation",
    "static_sources_from_modules": "wybra.assets.serving",
    "setup_site": "wybra.assets.capabilities",
    "validate_assets": "wybra.assets.validation",
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
    "AssetSettings",
    "DefaultStaticAssetCapability",
    "NoStaticFiles",
    "NormalStaticAssetStorage",
    "PathCorsStaticFiles",
    "StaticAssetCapability",
    "StaticAssetDuplicate",
    "StaticAssetStorage",
    "StaticCollectionError",
    "StaticCollectionStatus",
    "StaticCollectResult",
    "StaticCollectedAsset",
    "StaticDeletedAsset",
    "StaticSkippedAsset",
    "asset_collection_root",
    "asset_url",
    "collect_configured_static_assets",
    "collect_static_assets",
    "discover_static_sources",
    "module_config",
    "record_asset_collection_root_check",
    "require_static_asset_capability",
    "static_app_from_config",
    "static_asset_response",
    "static_asset_storage",
    "static_location_for_validation",
    "static_resource_for_validation",
    "static_sources_for_validation",
    "static_sources_from_modules",
    "setup_site",
    "validate_assets",
]
