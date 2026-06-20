"""Static asset runtime capability."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from wybra.assets.config import AssetExportMode
from wybra.assets.serving import static_app_from_config, static_sources_from_modules
from wybra.assets.settings import AssetSettings
from wybra.assets.storage import StaticAssetStorage, static_asset_storage
from wybra.core.resources import PackageResourceSource
from wybra.errors import structured_error
from wybra.site import Site, SiteCapabilityError, SiteCapabilityProxy


@runtime_checkable
class StaticAssetCapability(Protocol):
    url_path: str
    root: Path
    export_mode: AssetExportMode
    serve: bool
    sources: tuple[PackageResourceSource, ...]

    def url(self, logical_path: str) -> str: ...


@dataclass(frozen=True, slots=True)
class DefaultStaticAssetCapability:
    settings: AssetSettings
    sources: tuple[PackageResourceSource, ...]
    _storage: StaticAssetStorage

    @property
    def url_path(self) -> str:
        return self.settings.url_path

    @property
    def root(self) -> Path:
        return self.settings.root

    @property
    def export_mode(self) -> AssetExportMode:
        return self.settings.export_mode

    @property
    def serve(self) -> bool:
        return self.settings.serve

    def url(self, logical_path: str) -> str:
        return self._storage.url(logical_path, url_path=self.url_path)


def require_static_asset_capability(
    proxy: SiteCapabilityProxy[StaticAssetCapability],
) -> StaticAssetCapability:
    try:
        return proxy.require()
    except SiteCapabilityError as exc:
        raise SiteCapabilityError(
            structured_error(
                "Missing static asset capability provider",
                requirement=(
                    "configure wybra.assets or another StaticAssetCapability provider "
                    "when static asset URL behaviour is used"
                ),
            )
        ) from exc


async def setup_site(site: Site) -> None:
    settings = AssetSettings.load_settings(site.config)
    capability = DefaultStaticAssetCapability(
        settings=settings,
        sources=static_sources_from_modules(site.modules),
        _storage=static_asset_storage(settings.export_mode),
    )
    site.provide_capability(StaticAssetCapability, capability)
    if capability.serve:
        site.app.mount(
            capability.url_path,
            static_app_from_config(
                project_root=settings.project_root,
                static_root=None,
                static_sources=capability.sources,
                cors=settings.cors,
                url_path=capability.url_path,
            ),
            name="static",
        )


__all__ = (
    "DefaultStaticAssetCapability",
    "StaticAssetCapability",
    "require_static_asset_capability",
    "setup_site",
)
