"""Reusable media storage and serving infrastructure."""

import uuid

from fastapi import HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from wybra.db import DatabaseCapability
from wybra.media.capabilities import (
    FilesystemMediaCapability,
    MediaCapability,
    MediaCapabilityError,
    MediaError,
    MediaInputError,
    MediaNotFoundError,
    MediaStorageOperationError,
    MediaStorageReadinessError,
)
from wybra.media.config import MediaSettings, module_config
from wybra.media.persistence import SqlAlchemyMediaCatalogueRepository
from wybra.media.validation import MediaValidationSettings, validate_media
from wybra.site import Site


async def setup_site(site: Site) -> None:
    settings = MediaSettings.load_settings(site.config)
    capability = FilesystemMediaCapability(
        settings=settings,
        catalogue=SqlAlchemyMediaCatalogueRepository(
            site.capability_proxy(DatabaseCapability)
        ),
    )
    site.provide_capability(MediaCapability, capability)
    if capability.serve and capability.url_mode == "storage-key":
        site.app.mount(
            capability.mount_path,
            StaticFiles(directory=capability.root, check_dir=False),
            name="media",
        )
    if capability.serve and capability.url_mode == "id":
        _register_media_item_route(site, capability)


async def post_setup_site(site: Site) -> None:
    capability = site.require_capability(MediaCapability)
    if isinstance(capability, FilesystemMediaCapability):
        catalogue = capability.catalogue
        if isinstance(catalogue, SqlAlchemyMediaCatalogueRepository):
            catalogue.database.finalise_required()


def _register_media_item_route(site: Site, capability: MediaCapability) -> None:
    async def media_item(media_id: uuid.UUID):
        try:
            item = await capability.get(media_id)
            return FileResponse(
                await capability.path_for(media_id),
                media_type=item.content_type,
            )
        except (FileNotFoundError, MediaNotFoundError) as exc:
            raise HTTPException(status_code=404) from exc

    site.app.add_api_route(
        f"{capability.mount_path}/items/{{media_id}}",
        media_item,
        name="media:item",
        include_in_schema=False,
    )


__all__ = (
    "FilesystemMediaCapability",
    "MediaCapability",
    "MediaCapabilityError",
    "MediaError",
    "MediaInputError",
    "MediaNotFoundError",
    "MediaStorageOperationError",
    "MediaStorageReadinessError",
    "MediaSettings",
    "MediaValidationSettings",
    "module_config",
    "post_setup_site",
    "setup_site",
    "validate_media",
)
