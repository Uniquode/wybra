from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy import exists, select
from sqlalchemy.orm import aliased

from wybra.db import DatabaseCapability
from wybra.profile.exceptions import ProfileInputError
from wybra.profile.models import UserPhoneContact, UserProfile
from wybra.profile.phone import normalise_phone_contact
from wybra.profile.types import ProfileFieldValue
from wybra.site import SiteCapabilityProxy

type ProfileFieldSetter = Callable[[UserProfile, ProfileFieldValue], None]


class ProfileRepository(Protocol):
    """Storage-neutral profile persistence operations."""

    async def get_profile(self, user_id: uuid.UUID) -> UserProfile | None: ...

    async def ensure_profile(self, user_id: uuid.UUID) -> UserProfile: ...

    async def set_profile_picture(
        self,
        user_id: uuid.UUID,
        media_id: uuid.UUID | None,
    ) -> UserProfile: ...

    async def save_profile_fields(
        self,
        user_id: uuid.UUID,
        values: Mapping[str, ProfileFieldValue],
        *,
        field_setters: Mapping[str, ProfileFieldSetter],
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
        profile_values: Mapping[str, ProfileFieldValue],
        *,
        field_setters: Mapping[str, ProfileFieldSetter],
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


@dataclass(frozen=True, slots=True)
class SqlAlchemyProfileRepository:
    """SQLAlchemy-backed profile repository adapter."""

    database: SiteCapabilityProxy[DatabaseCapability]

    async def get_profile(self, user_id: uuid.UUID) -> UserProfile | None:
        async with self.database.session() as session:
            return await session.scalar(
                select(UserProfile).where(UserProfile.user_id == user_id)
            )

    async def ensure_profile(self, user_id: uuid.UUID) -> UserProfile:
        async with self.database.transaction() as session:
            return await _ensure_profile(session, user_id)

    async def set_profile_picture(
        self,
        user_id: uuid.UUID,
        media_id: uuid.UUID | None,
    ) -> UserProfile:
        async with self.database.transaction() as session:
            profile = await _ensure_profile(session, user_id)
            profile.profile_picture_media_id = media_id
            await session.flush()
            return profile

    async def save_profile_fields(
        self,
        user_id: uuid.UUID,
        values: Mapping[str, ProfileFieldValue],
        *,
        field_setters: Mapping[str, ProfileFieldSetter],
    ) -> UserProfile:
        async with self.database.transaction() as session:
            return await _save_profile_fields(
                session,
                user_id,
                values,
                field_setters=field_setters,
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
        async with self.database.transaction() as session:
            return await _save_phone_contact(
                session,
                user_id,
                number=number,
                country_code=country_code,
                subdivision_code=subdivision_code,
                contact_id=contact_id,
            )

    async def save_profile_edit(
        self,
        user_id: uuid.UUID,
        profile_values: Mapping[str, ProfileFieldValue],
        *,
        field_setters: Mapping[str, ProfileFieldSetter],
        phone_contact: Mapping[str, str | None] | None = None,
    ) -> None:
        async with self.database.transaction() as session:
            if profile_values:
                await _save_profile_fields(
                    session,
                    user_id,
                    profile_values,
                    field_setters=field_setters,
                )
            if phone_contact is not None:
                await _save_phone_contact(
                    session,
                    user_id,
                    number=phone_contact["number"] or "",
                    country_code=phone_contact["country_code"],
                    subdivision_code=phone_contact["subdivision_code"],
                )

    async def list_phone_contacts(
        self,
        user_id: uuid.UUID,
    ) -> tuple[UserPhoneContact, ...]:
        async with self.database.session() as session:
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
        async with self.database.session() as session:
            return tuple((await session.scalars(query)).all())


async def _ensure_profile(session, user_id: uuid.UUID) -> UserProfile:
    existing = await session.scalar(
        select(UserProfile).where(UserProfile.user_id == user_id)
    )
    if existing is not None:
        return existing
    profile = UserProfile(user_id=user_id)
    session.add(profile)
    await session.flush()
    return profile


async def _save_profile_fields(
    session,
    user_id: uuid.UUID,
    values: Mapping[str, ProfileFieldValue],
    *,
    field_setters: Mapping[str, ProfileFieldSetter],
) -> UserProfile:
    profile = await _ensure_profile(session, user_id)
    for field_name, value in values.items():
        try:
            setter = field_setters[field_name]
        except KeyError as exc:
            raise ProfileInputError(
                f"Unknown profile field submitted: {field_name}."
            ) from exc
        setter(profile, value)
    await session.flush()
    return profile


async def _save_phone_contact(
    session,
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
        await _phone_contact(session, user_id, contact_id)
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


async def _phone_contact(
    session,
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
