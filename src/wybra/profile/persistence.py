from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from tortoise.expressions import Q

from wybra.db import DatabaseCapability
from wybra.db.capabilities import tortoise_connection, tortoise_transaction
from wybra.profile.exceptions import ProfileInputError
from wybra.profile.models import UserPhoneContact, UserProfile
from wybra.profile.phone import normalise_phone_contact
from wybra.profile.types import ProfileFieldValue
from wybra.site import SiteCapabilityProxy

type ProfileFieldSetter = Callable[[UserProfile, ProfileFieldValue], None]


class ProfileRepository(Protocol):
    """Profile persistence operations."""

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
class TortoiseProfileRepository:
    """Tortoise-backed profile repository."""

    database: SiteCapabilityProxy[DatabaseCapability]

    async def get_profile(self, user_id: uuid.UUID) -> UserProfile | None:
        database = await self.database.require()
        return await UserProfile.get_or_none(
            user_id=user_id,
            using_db=tortoise_connection(database, database.database().default()),
        )

    async def ensure_profile(self, user_id: uuid.UUID) -> UserProfile:
        database = await self.database.require()
        async with tortoise_transaction(
            database,
            database.database().for_write(),
        ) as connection:
            return await _ensure_profile(connection, user_id)

    async def set_profile_picture(
        self,
        user_id: uuid.UUID,
        media_id: uuid.UUID | None,
    ) -> UserProfile:
        database = await self.database.require()
        async with tortoise_transaction(
            database,
            database.database().for_write(),
        ) as connection:
            profile = await _ensure_profile(connection, user_id)
            profile.profile_picture_media_id = media_id
            await profile.save(using_db=connection)
            return profile

    async def save_profile_fields(
        self,
        user_id: uuid.UUID,
        values: Mapping[str, ProfileFieldValue],
        *,
        field_setters: Mapping[str, ProfileFieldSetter],
    ) -> UserProfile:
        database = await self.database.require()
        async with tortoise_transaction(
            database,
            database.database().for_write(),
        ) as connection:
            return await _save_profile_fields(
                connection,
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
        database = await self.database.require()
        async with tortoise_transaction(
            database,
            database.database().for_write(),
        ) as connection:
            return await _save_phone_contact(
                connection,
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
        database = await self.database.require()
        async with tortoise_transaction(
            database,
            database.database().for_write(),
        ) as connection:
            if profile_values:
                await _save_profile_fields(
                    connection,
                    user_id,
                    profile_values,
                    field_setters=field_setters,
                )
            if phone_contact is not None:
                await _save_phone_contact(
                    connection,
                    user_id,
                    number=phone_contact["number"] or "",
                    country_code=phone_contact["country_code"],
                    subdivision_code=phone_contact["subdivision_code"],
                )

    async def list_phone_contacts(
        self,
        user_id: uuid.UUID,
    ) -> tuple[UserPhoneContact, ...]:
        return tuple(
            await UserPhoneContact.filter(user_id=user_id)
            .using_db(
                tortoise_connection(
                    database := await self.database.require(),
                    database.database().default(),
                )
            )
            .order_by("id")
            .all()
        )

    async def recovery_eligible_phone_contacts(
        self,
        user_id: uuid.UUID,
        *,
        require_sms: bool = True,
    ) -> tuple[UserPhoneContact, ...]:
        query = (
            UserPhoneContact.filter(user_id=user_id)
            .filter(Q(verified_at__isnull=False))
            .using_db(
                tortoise_connection(
                    database := await self.database.require(),
                    database.database().default(),
                )
            )
            .order_by("id")
        )
        if require_sms:
            query = query.filter(sms_capable=True)

        contacts = tuple(await query.all())
        if not contacts:
            return ()

        numbers = {contact.normalised_number for contact in contacts}
        shared_numbers = set(
            await UserPhoneContact.filter(
                normalised_number__in=numbers,
            )
            .filter(Q(verified_at__isnull=False))
            .exclude(user_id=user_id)
            .using_db(
                tortoise_connection(
                    database := await self.database.require(),
                    database.database().default(),
                )
            )
            .values_list("normalised_number", flat=True)
        )
        return tuple(
            contact
            for contact in contacts
            if contact.normalised_number not in shared_numbers
        )


async def _ensure_profile(connection: Any, user_id: uuid.UUID) -> UserProfile:
    existing = await UserProfile.get_or_none(
        user_id=user_id,
        using_db=connection,
    )
    if existing is not None:
        return existing
    return await UserProfile.create(user_id=user_id, using_db=connection)


async def _save_profile_fields(
    connection: Any,
    user_id: uuid.UUID,
    values: Mapping[str, ProfileFieldValue],
    *,
    field_setters: Mapping[str, ProfileFieldSetter],
) -> UserProfile:
    profile = await _ensure_profile(connection, user_id)
    for field_name, value in values.items():
        try:
            setter = field_setters[field_name]
        except KeyError as exc:
            raise ProfileInputError(
                f"Unknown profile field submitted: {field_name}."
            ) from exc
        setter(profile, value)
    await profile.save(using_db=connection)
    return profile


async def _save_phone_contact(
    connection: Any,
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
        await _phone_contact(connection, user_id, contact_id)
        if contact_id is not None
        else UserPhoneContact(user_id=user_id)
    )
    previous_number = getattr(contact, "normalised_number", None)
    contact.country_code = normalised.country_code
    contact.subdivision_code = normalised.subdivision_code
    contact.normalised_number = normalised.normalised_number
    contact.number_type = normalised.number_type
    contact.sms_capable = normalised.sms_capable
    if previous_number != normalised.normalised_number:
        contact.verified_at = None
    await contact.save(using_db=connection)
    return contact


async def save_phone_contact_in_transaction(
    connection: Any,
    user_id: uuid.UUID,
    *,
    number: str,
    country_code: str | None,
    subdivision_code: str | None = None,
) -> UserPhoneContact:
    """Persist one phone contact within the caller's writer transaction."""
    return await _save_phone_contact(
        connection,
        user_id,
        number=number,
        country_code=country_code,
        subdivision_code=subdivision_code,
    )


async def _phone_contact(
    connection: Any,
    user_id: uuid.UUID,
    contact_id: uuid.UUID,
) -> UserPhoneContact:
    contact = await UserPhoneContact.get_or_none(
        id=contact_id,
        user_id=user_id,
        using_db=connection,
    )
    if contact is None:
        raise ProfileInputError("Phone contact was not found for this user.")
    return contact
