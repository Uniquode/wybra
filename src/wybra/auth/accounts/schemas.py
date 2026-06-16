import uuid

from fastapi_users import schemas


class UserRead(schemas.BaseUser[uuid.UUID]):
    """Public representation of a local user account."""


class UserCreate(schemas.BaseUserCreate):
    """Input schema for creating a local user account."""


class UserUpdate(schemas.BaseUserUpdate):
    """Input schema for updating a local user account."""
