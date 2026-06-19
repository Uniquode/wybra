from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from wybra.core import InputValidationError
from wybra.media import MediaCapability, MediaCapabilityError
from wybra.profile.models import UserProfile
from wybra.site import SiteCapabilityError, SiteCapabilityProxy


class ProfileCapabilityError(RuntimeError):
    """Raised when a profile capability operation cannot be completed."""


class ProfileInputError(InputValidationError):
    """Raised when caller-provided profile input is invalid."""


logger = logging.getLogger(__name__)


class ProfileUser(Protocol):
    id: uuid.UUID
    email: str


@dataclass(frozen=True, slots=True)
class ProfileImage:
    src: str | None
    alt: str
    fallback_text: str | None = None


@runtime_checkable
class ProfileCapability(Protocol):
    """Public profile capability exposed through ``Site``."""

    async def get_profile(
        self,
        session: AsyncSession,
        user_id: uuid.UUID,
    ) -> UserProfile | None: ...

    async def ensure_profile(
        self,
        session: AsyncSession,
        user_id: uuid.UUID,
    ) -> UserProfile: ...

    async def set_profile_picture(
        self,
        session: AsyncSession,
        user_id: uuid.UUID,
        media_id: uuid.UUID | None,
    ) -> UserProfile: ...

    async def profile_image_for_user(
        self,
        user: ProfileUser,
        profile: UserProfile | None = None,
    ) -> ProfileImage: ...


@dataclass(frozen=True, slots=True)
class SiteProfileCapability:
    media: SiteCapabilityProxy[MediaCapability]

    async def get_profile(
        self,
        session: AsyncSession,
        user_id: uuid.UUID,
    ) -> UserProfile | None:
        return await session.scalar(
            select(UserProfile).where(UserProfile.user_id == user_id)
        )

    async def ensure_profile(
        self,
        session: AsyncSession,
        user_id: uuid.UUID,
    ) -> UserProfile:
        existing = await self.get_profile(session, user_id)
        if existing is not None:
            return existing
        profile = UserProfile(user_id=user_id)
        session.add(profile)
        await session.flush()
        return profile

    async def set_profile_picture(
        self,
        session: AsyncSession,
        user_id: uuid.UUID,
        media_id: uuid.UUID | None,
    ) -> UserProfile:
        profile = await self.ensure_profile(session, user_id)
        profile.profile_picture_media_id = media_id
        await session.flush()
        return profile

    async def profile_image_for_user(
        self,
        user: ProfileUser,
        profile: UserProfile | None = None,
    ) -> ProfileImage:
        if profile is not None and profile.profile_picture_media_id is not None:
            try:
                return ProfileImage(
                    src=await self.media.url_for(profile.profile_picture_media_id),
                    alt="Profile picture",
                    fallback_text=None,
                )
            except (MediaCapabilityError, SiteCapabilityError):
                logger.warning(
                    "Profile image resolution via media capability failed; "
                    "using fallback profile initial.",
                    extra={"user_id": str(user.id)},
                )
        return ProfileImage(
            src=None,
            alt="Profile picture",
            fallback_text=_email_initial(user.email),
        )


def _email_initial(email: str) -> str | None:
    cleaned_email = email.strip()
    if not cleaned_email:
        return None
    local_part = cleaned_email.split("@", maxsplit=1)[0]
    for character in local_part:
        if character.isalpha():
            return character.upper()
    return None


def profile_picture_storage_key(user_id: uuid.UUID, extension: str) -> str:
    if not isinstance(extension, str):
        raise ProfileInputError("Profile picture extension must be text.")
    raw_suffix = extension.strip()
    if not raw_suffix:
        raise ProfileInputError("Profile picture extension must not be blank.")
    if raw_suffix.startswith("."):
        raise ProfileInputError("Profile picture extension must not start with a dot.")
    if "." in raw_suffix:
        raise ProfileInputError(
            "Profile picture extension must not contain additional dots."
        )
    suffix = raw_suffix.lower()
    if "/" in suffix or "\\" in suffix:
        raise ProfileInputError(
            "Profile picture extension must not contain path separators."
        )
    user_key = user_id.hex
    return f"profile/{user_key[:2]}/{user_key[2:4]}/{user_key}.{suffix}"


__all__ = (
    "ProfileCapability",
    "ProfileCapabilityError",
    "ProfileImage",
    "ProfileInputError",
    "ProfileUser",
    "SiteProfileCapability",
    "profile_picture_storage_key",
)
