"""Application-facing persistence exceptions."""

from tortoise.exceptions import (
    BaseORMException,
    DoesNotExist,
    IntegrityError,
    MultipleObjectsReturned,
    OperationalError,
)

__all__ = (
    "BaseORMException",
    "DoesNotExist",
    "IntegrityError",
    "MultipleObjectsReturned",
    "OperationalError",
)
