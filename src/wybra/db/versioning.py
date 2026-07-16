"""Opt-in optimistic-locking model field support."""

from __future__ import annotations

import re
from collections.abc import Iterable
from hashlib import sha256
from typing import Any

from tortoise import fields
from tortoise.backends.base.client import BaseDBAsyncClient
from tortoise.migrations.constraints import CheckConstraint
from tortoise.models import Model


class VersionFieldError(ValueError):
    """Raised when a model's optimistic-lock declaration is invalid."""


class OptimisticLockConflict(RuntimeError):
    """Raised when an atomic version comparison does not match."""


class PositiveIntField(fields.IntField):
    """Tortoise-compatible non-negative integer model field.

    Tortoise 1.1 does not provide ``PositiveIntField``. This field supplies
    that portable semantic while retaining a normal integer database column.
    """

    def to_db_value(self, value: Any, instance: type[Model] | Model) -> int:
        return self._validated_value(super().to_db_value(value, instance))

    def to_python_value(self, value: Any) -> int:
        return self._validated_value(super().to_python_value(value))

    @staticmethod
    def _validated_value(value: int) -> int:
        if value < 0:
            raise VersionFieldError("PositiveIntField values must be non-negative.")
        return value


class VersionField(PositiveIntField):
    """Non-null, non-negative model version counter starting at zero."""

    def __init__(self, **kwargs: Any) -> None:
        if kwargs.get("null"):
            raise VersionFieldError("VersionField cannot be nullable.")
        default = kwargs.pop("default", 0)
        if default != 0:
            raise VersionFieldError("VersionField default must be zero.")
        super().__init__(null=False, default=0, **kwargs)


def version_field_check_constraint(
    model: type[Model],
    field_name: str,
) -> CheckConstraint:
    """Return the native migration constraint for a model version field."""
    column_name = model._meta.fields_db_projection[field_name]
    return version_column_check_constraint(model._meta.db_table, column_name)


def version_column_check_constraint(
    table_name: str,
    column_name: str,
) -> CheckConstraint:
    if not column_name.isidentifier():
        raise VersionFieldError(
            "VersionField database columns must use a Python-style identifier."
        )
    identifier = re.sub(
        r"[^a-zA-Z0-9_]",
        "_",
        f"{table_name}_{column_name}_non_negative",
    )
    suffix = sha256(identifier.encode()).hexdigest()[:8]
    name = f"{identifier[:54]}_{suffix}"
    return CheckConstraint(check=f"{column_name} >= 0", name=name)


def version_field_name(model: type[Model]) -> str | None:
    """Return the model's version field, rejecting invalid declarations."""
    names = tuple(
        name
        for name, field in model._meta.fields_map.items()
        if isinstance(field, VersionField)
    )
    if len(names) > 1:
        raise VersionFieldError(
            f"Model {model.__name__} declares multiple VersionField values: "
            + ", ".join(names)
        )
    return names[0] if names else None


def validate_version_fields(models: Iterable[type[Model]]) -> None:
    """Validate all version declarations in a resolved model collection."""
    for model in models:
        version_field_name(model)


async def save_model_update(
    target: Model,
    *,
    client: BaseDBAsyncClient,
    expected_version: int | None = None,
) -> None:
    """Persist a model update, applying optimistic locking when configured."""
    if not target._saved_in_db:
        await target.save(using_db=client)
        return
    version_name = version_field_name(type(target))
    if version_name is None:
        await target.save(using_db=client)
        return
    if expected_version is None:
        raise VersionFieldError("Versioned updates require a submitted version.")
    values = {
        name: getattr(target, name)
        for name in target._meta.fields_db_projection
        if name not in {target._meta.pk_attr, version_name}
        and not target._meta.fields_map[name].generated
    }
    values[version_name] = expected_version + 1
    updated = await (
        type(target)
        .filter(pk=target.pk, **{version_name: expected_version})
        .using_db(client)
        .update(**values)
    )
    if updated == 0:
        raise OptimisticLockConflict()
    setattr(target, version_name, expected_version + 1)


async def delete_model_instance(
    target: Model,
    *,
    client: BaseDBAsyncClient,
    expected_version: int | None = None,
) -> None:
    """Delete a model instance, applying optimistic locking when configured."""
    version_name = version_field_name(type(target))
    if version_name is None:
        await target.delete(using_db=client)
        return
    if expected_version is None:
        raise VersionFieldError("Versioned deletions require a submitted version.")
    deleted = await (
        type(target)
        .filter(pk=target.pk, **{version_name: expected_version})
        .using_db(client)
        .delete()
    )
    if deleted == 0:
        raise OptimisticLockConflict()


__all__ = (
    "VersionField",
    "PositiveIntField",
    "VersionFieldError",
    "OptimisticLockConflict",
    "delete_model_instance",
    "save_model_update",
    "validate_version_fields",
    "version_column_check_constraint",
    "version_field_check_constraint",
    "version_field_name",
)
