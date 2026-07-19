"""Application-facing database field declarations.

Applications import persistence fields from this module rather than directly
from Tortoise. Most declarations intentionally forward Tortoise unchanged;
Wybra-owned fields add only explicit framework semantics.
"""

from __future__ import annotations

import uuid
from typing import Any

from tortoise.fields import (
    CASCADE,
    NO_ACTION,
    RESTRICT,
    SET_DEFAULT,
    SET_NULL,
    BigIntField,
    BinaryField,
    BooleanField,
    CharEnumField,
    CharField,
    DateField,
    DatetimeField,
    DecimalField,
    Field,
    FloatField,
    ForeignKeyField,
    IntEnumField,
    IntField,
    JSONField,
    ManyToManyField,
    OnDelete,
    OneToOneField,
    SmallIntField,
    TextField,
    TimeDeltaField,
    TimeField,
)
from tortoise.fields import UUIDField as TortoiseUUIDField


class UUIDField(TortoiseUUIDField):
    """Tortoise UUID field whose implicit primary-key default is UUIDv7."""

    def __init__(self, **kwargs: Any) -> None:
        if (kwargs.get("primary_key") or kwargs.get("pk")) and "default" not in kwargs:
            kwargs["default"] = uuid.uuid7
        super().__init__(**kwargs)


__all__ = (
    "CASCADE",
    "NO_ACTION",
    "RESTRICT",
    "SET_DEFAULT",
    "SET_NULL",
    "BigIntField",
    "BinaryField",
    "BooleanField",
    "CharEnumField",
    "CharField",
    "DateField",
    "DatetimeField",
    "DecimalField",
    "Field",
    "FloatField",
    "ForeignKeyField",
    "IntEnumField",
    "IntField",
    "JSONField",
    "ManyToManyField",
    "OnDelete",
    "OneToOneField",
    "SmallIntField",
    "TextField",
    "TimeDeltaField",
    "TimeField",
    "UUIDField",
)
