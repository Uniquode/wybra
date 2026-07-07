from __future__ import annotations

import uuid

from pydantic import BaseModel, ConfigDict, field_validator

from wybra.auth.email_normalisation import normalise_email


class _CreateUpdateDictModel(BaseModel):
    def create_update_dict(self) -> dict[str, object]:
        return self.model_dump(
            exclude_unset=True,
            exclude={
                "id",
                "is_superuser",
                "is_active",
                "is_verified",
                "oauth_accounts",
            },
        )

    def create_update_dict_superuser(self) -> dict[str, object]:
        return self.model_dump(exclude_unset=True, exclude={"id"})


class UserRead(_CreateUpdateDictModel):
    """Public representation of a local user account."""

    id: uuid.UUID
    email: str
    is_active: bool = True
    is_superuser: bool = False
    is_verified: bool = False

    model_config = ConfigDict(from_attributes=True)


class UserCreate(_CreateUpdateDictModel):
    """Input schema for creating a local user account."""

    email: str
    password: str
    is_active: bool | None = True
    is_superuser: bool | None = False
    is_verified: bool | None = False

    @field_validator("email")
    @classmethod
    def _normalise_email(cls, value: str) -> str:
        normalised = normalise_email(value)
        if normalised is None:
            raise ValueError("Email address is invalid.")
        return normalised


class UserUpdate(_CreateUpdateDictModel):
    """Input schema for updating a local user account."""

    password: str | None = None
    email: str | None = None
    is_active: bool | None = None
    is_superuser: bool | None = None
    is_verified: bool | None = None

    @field_validator("email")
    @classmethod
    def _normalise_optional_email(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalised = normalise_email(value)
        if normalised is None:
            raise ValueError("Email address is invalid.")
        return normalised
