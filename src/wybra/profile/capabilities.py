from __future__ import annotations

import logging
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Protocol, TypeGuard, runtime_checkable

from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from wybra.media import MediaCapability, MediaCapabilityError
from wybra.profile.editing import profile_field_values
from wybra.profile.exceptions import ProfileCapabilityError, ProfileInputError
from wybra.profile.models import UserPhoneContact, UserProfile
from wybra.profile.phone import normalise_phone_contact
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

    async def save_profile_fields(
        self,
        session: AsyncSession,
        user_id: uuid.UUID,
        data: dict[str, object],
        *,
        settings: ProfileSettings,
    ) -> UserProfile: ...

    async def save_phone_contact(
        self,
        session: AsyncSession,
        user_id: uuid.UUID,
        *,
        number: str,
        country_code: str | None,
        subdivision_code: str | None = None,
        contact_id: uuid.UUID | None = None,
    ) -> UserPhoneContact: ...

    async def list_phone_contacts(
        self,
        session: AsyncSession,
        user_id: uuid.UUID,
    ) -> tuple[UserPhoneContact, ...]: ...

    async def recovery_eligible_phone_contacts(
        self,
        session: AsyncSession,
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

    async def save_profile_fields(
        self,
        session: AsyncSession,
        user_id: uuid.UUID,
        data: dict[str, object],
        *,
        settings: ProfileSettings,
    ) -> UserProfile:
        values = profile_field_values(data, settings=settings)
        profile = await self.ensure_profile(session, user_id)
        for field_name, value in values.items():
            try:
                setter = PROFILE_FIELD_SETTERS[field_name]
            except KeyError as exc:
                raise ProfileInputError(
                    f"Unknown profile field submitted: {field_name}."
                ) from exc
            setter(profile, value)
        await session.flush()
        return profile

    async def save_phone_contact(
        self,
        session: AsyncSession,
        user_id: uuid.UUID,
        *,
        number: str,
        country_code: str | None,
        subdivision_code: str | None = None,
        contact_id: uuid.UUID | None = None,
    ) -> UserPhoneContact:
        normalised = normalise_phone_contact(
            number,
            country_code=country_code,
            subdivision_code=subdivision_code,
        )
        contact = (
            await self._phone_contact(session, user_id, contact_id)
            if contact_id is not None
            else UserPhoneContact(user_id=user_id)
        )
        if contact_id is None:
            session.add(contact)

        previous_number = contact.normalised_number
        contact.country_code = normalised.country_code
        contact.subdivision_code = normalised.subdivision_code
        contact.normalised_number = normalised.normalised_number
        contact.number_type = normalised.number_type
        contact.sms_capable = normalised.sms_capable
        if previous_number != normalised.normalised_number:
            contact.verified_at = None
        await session.flush()
        return contact

    async def list_phone_contacts(
        self,
        session: AsyncSession,
        user_id: uuid.UUID,
    ) -> tuple[UserPhoneContact, ...]:
        return tuple(
            (
                await session.scalars(
                    select(UserPhoneContact)
                    .where(UserPhoneContact.user_id == user_id)
                    .order_by(UserPhoneContact.id)
                )
            ).all()
        )

    async def recovery_eligible_phone_contacts(
        self,
        session: AsyncSession,
        user_id: uuid.UUID,
        *,
        require_sms: bool = True,
    ) -> tuple[UserPhoneContact, ...]:
        other_contact = aliased(UserPhoneContact)
        duplicate_verified_number_exists = exists().where(
            other_contact.user_id != UserPhoneContact.user_id,
            other_contact.normalised_number == UserPhoneContact.normalised_number,
            other_contact.verified_at.is_not(None),
        )
        query = (
            select(UserPhoneContact)
            .where(UserPhoneContact.user_id == user_id)
            .where(UserPhoneContact.verified_at.is_not(None))
            .where(~duplicate_verified_number_exists)
            .order_by(UserPhoneContact.id)
        )
        if require_sms:
            query = query.where(UserPhoneContact.sms_capable.is_(True))
        return tuple((await session.scalars(query)).all())

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

    async def _phone_contact(
        self,
        session: AsyncSession,
        user_id: uuid.UUID,
        contact_id: uuid.UUID,
    ) -> UserPhoneContact:
        contact = await session.scalar(
            select(UserPhoneContact)
            .where(UserPhoneContact.id == contact_id)
            .where(UserPhoneContact.user_id == user_id)
        )
        if contact is None:
            raise ProfileInputError("Phone contact was not found for this user.")
        return contact


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
    profile.bio = value if isinstance(value, str) else None


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
