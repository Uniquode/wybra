from __future__ import annotations

import logging
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol, TypeGuard, cast, runtime_checkable

from wybra.media import MediaCapability, MediaError
from wybra.profile.editing import profile_field_values
from wybra.profile.exceptions import ProfileCapabilityError, ProfileInputError
from wybra.profile.models import UserPhoneContact, UserProfile
from wybra.profile.persistence import ProfileRepository
from wybra.profile.settings import ProfileSettings
from wybra.profile.types import ProfileFieldValue, ProfileLinks, Pronouns
from wybra.site import SiteCapabilityError, SiteCapabilityProxy

logger = logging.getLogger(__name__)
type ProfileFieldSetter = Callable[[UserProfile, ProfileFieldValue], None]


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
        user_id: uuid.UUID,
    ) -> UserProfile | None: ...

    async def ensure_profile(
        self,
        user_id: uuid.UUID,
    ) -> UserProfile: ...

    async def set_profile_picture(
        self,
        user_id: uuid.UUID,
        media_id: uuid.UUID | None,
    ) -> UserProfile: ...

    async def save_profile_fields(
        self,
        user_id: uuid.UUID,
        data: dict[str, object],
        *,
        settings: ProfileSettings,
    ) -> UserProfile: ...

    async def save_phone_contact(
        self,
        user_id: uuid.UUID,
        *,
        number: str,
        country_code: str | None,
        subdivision_code: str | None = None,
        contact_id: uuid.UUID | None = None,
    ) -> UserPhoneContact: ...

    async def save_profile_edit(
        self,
        user_id: uuid.UUID,
        profile_data: dict[str, object],
        *,
        settings: ProfileSettings,
        phone_contact: Mapping[str, str | None] | None = None,
    ) -> None: ...

    async def list_phone_contacts(
        self,
        user_id: uuid.UUID,
    ) -> tuple[UserPhoneContact, ...]: ...

    async def recovery_eligible_phone_contacts(
        self,
        user_id: uuid.UUID,
        *,
        require_sms: bool = True,
    ) -> tuple[UserPhoneContact, ...]: ...

    async def profile_image_for_user(
        self,
        user: ProfileUser,
        profile: UserProfile | None = None,
    ) -> ProfileImage: ...


@dataclass(frozen=True, slots=True)
class SiteProfileCapability:
    media: SiteCapabilityProxy[MediaCapability]
    repository: ProfileRepository

    async def get_profile(
        self,
        user_id: uuid.UUID,
    ) -> UserProfile | None:
        return await self.repository.get_profile(user_id)

    async def ensure_profile(
        self,
        user_id: uuid.UUID,
    ) -> UserProfile:
        return await self.repository.ensure_profile(user_id)

    async def set_profile_picture(
        self,
        user_id: uuid.UUID,
        media_id: uuid.UUID | None,
    ) -> UserProfile:
        return await self.repository.set_profile_picture(user_id, media_id)

    async def save_profile_fields(
        self,
        user_id: uuid.UUID,
        data: dict[str, object],
        *,
        settings: ProfileSettings,
    ) -> UserProfile:
        values = profile_field_values(data, settings=settings)
        return await self.repository.save_profile_fields(
            user_id,
            values,
            field_setters=PROFILE_FIELD_SETTERS,
        )

    async def save_phone_contact(
        self,
        user_id: uuid.UUID,
        *,
        number: str,
        country_code: str | None,
        subdivision_code: str | None = None,
        contact_id: uuid.UUID | None = None,
    ) -> UserPhoneContact:
        return await self.repository.save_phone_contact(
            user_id,
            number=number,
            country_code=country_code,
            subdivision_code=subdivision_code,
            contact_id=contact_id,
        )

    async def save_profile_edit(
        self,
        user_id: uuid.UUID,
        profile_data: dict[str, object],
        *,
        settings: ProfileSettings,
        phone_contact: Mapping[str, str | None] | None = None,
    ) -> None:
        await self.repository.save_profile_edit(
            user_id,
            profile_field_values(profile_data, settings=settings),
            field_setters=PROFILE_FIELD_SETTERS,
            phone_contact=phone_contact,
        )

    async def list_phone_contacts(
        self,
        user_id: uuid.UUID,
    ) -> tuple[UserPhoneContact, ...]:
        return await self.repository.list_phone_contacts(user_id)

    async def recovery_eligible_phone_contacts(
        self,
        user_id: uuid.UUID,
        *,
        require_sms: bool = True,
    ) -> tuple[UserPhoneContact, ...]:
        return await self.repository.recovery_eligible_phone_contacts(
            user_id,
            require_sms=require_sms,
        )

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
            except (MediaError, SiteCapabilityError):
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


def _set_preferred_name(profile: UserProfile, value: ProfileFieldValue) -> None:
    profile.preferred_name = value if isinstance(value, str) else None


def _set_display_name(profile: UserProfile, value: ProfileFieldValue) -> None:
    profile.display_name = value if isinstance(value, str) else None


def _set_pronouns(profile: UserProfile, value: ProfileFieldValue) -> None:
    profile.pronouns = value if _is_pronouns(value) else None


def _set_profile_links(profile: UserProfile, value: ProfileFieldValue) -> None:
    profile.website_links = value if _is_profile_links(value) else None


def _set_bio(profile: UserProfile, value: ProfileFieldValue) -> None:
    cast(Any, profile).bio = value if isinstance(value, str) else None


def _is_pronouns(value: ProfileFieldValue) -> TypeGuard[Pronouns]:
    return isinstance(value, dict) and "website" not in value


def _is_profile_links(value: ProfileFieldValue) -> TypeGuard[ProfileLinks]:
    return isinstance(value, dict) and set(value).issubset({"website"})


PROFILE_FIELD_SETTERS: Mapping[str, ProfileFieldSetter] = {
    "preferred_name": _set_preferred_name,
    "display_name": _set_display_name,
    "pronouns": _set_pronouns,
    "profile_links": _set_profile_links,
    "bio": _set_bio,
}


__all__ = (
    "ProfileCapability",
    "ProfileCapabilityError",
    "ProfileImage",
    "ProfileInputError",
    "ProfileUser",
    "SiteProfileCapability",
    "profile_picture_storage_key",
)
