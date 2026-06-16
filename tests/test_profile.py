from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI

from wevra.auth import models as auth_models  # noqa: F401
from wevra.config import ConfigService, MappingConfigSource
from wevra.db import DatabaseCapability, SqlAlchemyDatabaseCapability
from wevra.db.models import metadata
from wevra.db.persistence import create_database
from wevra.media import (
    FilesystemMediaCapability,
    MediaCapability,
    MediaCapabilityError,
    MediaSettings,
)
from wevra.profile import (
    ProfileCapability,
    SiteProfileCapability,
    profile_picture_storage_key,
)
from wevra.profile.models import UserProfile
from wevra.profile.validation import validate_profile
from wevra.site import Site, start


@dataclass(frozen=True, slots=True)
class ProfileUser:
    id: uuid.UUID
    email: str


def _site_with_database(tmp_path: Path) -> Site:
    site = Site(
        app=FastAPI(),
        config=ConfigService(
            [MappingConfigSource({"app": {"modules": ()}})],
            discover_module_config=False,
        ),
    )
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'profile.sqlite3'}")
    site.provide_capability(
        DatabaseCapability,
        SqlAlchemyDatabaseCapability.from_connections({"default": database}),
    )
    return site


def test_profile_metadata_exposes_profile_table() -> None:
    table = metadata.tables["profile_user_profile"]

    assert table.c.user_id.foreign_keys
    assert table.c.profile_picture_media_id.nullable is True
    assert table.c.bio.nullable is True
    assert table.c.first_name.nullable is True
    assert table.c.last_name.nullable is True
    assert table.c.pronouns.nullable is True
    assert table.c.phone_number.nullable is True
    assert table.c.website_links.nullable is True
    assert table.c.country_region.nullable is True
    assert table.c.city.nullable is True
    assert table.c.postal_code.nullable is True
    assert table.c.job_title.nullable is True
    assert table.c.company.nullable is True
    assert table.c.company_industry.nullable is True
    assert table.c.department.nullable is True
    assert table.c.date_time_format.nullable is True
    assert table.c.theme.nullable is True
    assert table.c.notification_preferences.nullable is True
    assert table.c.profile_visibility.nullable is False
    assert table.c.marketing_consent.nullable is False
    assert table.c.terms_accepted_at.nullable is True
    assert table.c.data_deletion_requested.nullable is False


def test_validate_profile_accepts_configured_profile_module() -> None:
    class Settings:
        modules = ("wevra.profile",)

    result = validate_profile(Settings())

    assert result.is_ok is True


def test_validate_profile_reports_absent_profile_module() -> None:
    class Settings:
        modules = ()

    result = validate_profile(Settings())

    assert result.is_ok is False
    assert result.errors == (
        "wevra.profile must be configured to validate profile resources.",
    )


@pytest.mark.anyio
async def test_profile_setup_registers_profile_capability_before_media_exists() -> None:
    site = await start(
        FastAPI(),
        config_source=MappingConfigSource(
            {"app": {"modules": ("wevra.profile", "wevra.media")}}
        ),
    )

    assert site.has_capability(ProfileCapability) is True
    assert site.has_capability(MediaCapability) is True


@pytest.mark.anyio
async def test_profile_image_descriptor_uses_email_initial_without_media() -> None:
    site = await start(
        FastAPI(),
        config_source=MappingConfigSource({"app": {"modules": ("wevra.profile",)}}),
    )
    capability = site.require_capability(ProfileCapability)

    image = await capability.profile_image_for_user(
        ProfileUser(id=uuid.uuid4(), email="_david@example.test")
    )

    assert image.src is None
    assert image.alt == "Profile picture"
    assert image.fallback_text == "D"


@pytest.mark.anyio
async def test_profile_image_descriptor_resolves_media_reference(
    tmp_path: Path,
) -> None:
    site = _site_with_database(tmp_path)
    capability = SiteProfileCapability(media=site.capability_proxy(MediaCapability))
    media_capability = FilesystemMediaCapability(
        MediaSettings(root=tmp_path),
        database=site.capability_proxy(DatabaseCapability),
    )
    site.provide_capability(
        MediaCapability,
        media_capability,
    )
    async with media_capability.database.transaction() as session:

        def _create_all(sync_session: Any) -> None:
            metadata.create_all(sync_session.get_bind())

        await session.run_sync(_create_all)
    item = await media_capability.register(
        category="profile",
        storage_key="profile/ab/cd/david.png",
        size=10,
    )

    image = await capability.profile_image_for_user(
        ProfileUser(id=uuid.uuid4(), email="david@example.test"),
        UserProfile(user_id=uuid.uuid4(), profile_picture_media_id=item.id),
    )

    assert image.src == "/media/profile/ab/cd/david.png"
    assert image.fallback_text is None


@pytest.mark.anyio
async def test_profile_image_descriptor_falls_back_when_media_is_unavailable(
    tmp_path: Path,
) -> None:
    site = _site_with_database(tmp_path)

    class MissingMedia:
        root = tmp_path
        mount_path = "/media"
        serve = False
        url_mode = "storage-key"

        async def register(
            self,
            *,
            category: str,  # pylint: disable=unused-argument
            storage_key: str,  # pylint: disable=unused-argument
            content_type: str | None = None,  # pylint: disable=unused-argument
            size: int = 0,  # pylint: disable=unused-argument
        ) -> object:
            raise MediaCapabilityError("register not supported for fallback test.")

        async def store(
            self,
            *,
            category: str,  # pylint: disable=unused-argument
            storage_key: str,  # pylint: disable=unused-argument
            upload: object,  # pylint: disable=unused-argument
            chunk_size: int = 0,  # pylint: disable=unused-argument
        ) -> object:
            raise MediaCapabilityError("store not supported for fallback test.")

        async def get(self, media_id: uuid.UUID) -> object:  # pylint: disable=unused-argument
            raise MediaCapabilityError(f"Unknown media item: media_id={media_id}.")

        async def path_for(self, media_id: uuid.UUID) -> Path:  # pylint: disable=unused-argument
            raise MediaCapabilityError(f"Unknown media item: media_id={media_id}.")

        async def url_for(self, media_id: uuid.UUID) -> str:  # pylint: disable=unused-argument
            raise MediaCapabilityError(f"Unknown media item: media_id={media_id}.")

        async def get_by_resource_key(self, resource_key: str) -> object:  # pylint: disable=unused-argument
            raise MediaCapabilityError(f"Unknown media resource key: {resource_key}.")

        async def assign_resource_key(  # pylint: disable=unused-argument
            self,
            media_id: uuid.UUID,
            resource_key: str,
        ) -> None:
            raise MediaCapabilityError(
                f"assign_resource_key not supported: {media_id=}, {resource_key=}."
            )

        def path_for_key(self, storage_key: str | Path) -> Path:
            return Path(storage_key)

        def url_for_key(self, storage_key: str | Path) -> str:
            return f"/media/{storage_key}"

        def validate_writable(self) -> None:
            return None

    site.provide_capability(MediaCapability, MissingMedia())
    capability = SiteProfileCapability(media=site.capability_proxy(MediaCapability))

    image = await capability.profile_image_for_user(
        ProfileUser(id=uuid.uuid4(), email="david@example.test"),
        UserProfile(
            user_id=uuid.uuid4(),
            profile_picture_media_id=uuid.uuid4(),
        ),
    )

    assert image.src is None
    assert image.alt == "Profile picture"
    assert image.fallback_text == "D"


def test_profile_picture_storage_key_uses_profile_category_and_buckets() -> None:
    user_id = uuid.UUID("8ef0c57e-0000-4000-8000-000000000001")

    assert (
        profile_picture_storage_key(user_id, "png")
        == "profile/8e/f0/8ef0c57e000040008000000000000001.png"
    )
