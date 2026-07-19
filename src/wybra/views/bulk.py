"""Explicit collection-action contracts for generic views."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from fastapi import Request
from tortoise.models import Model


@dataclass(frozen=True, slots=True)
class BulkActionResult:
    """Backend-neutral outcome of an explicit collection action."""

    affected_ids: tuple[str, ...] = ()
    skipped_ids: tuple[str, ...] = ()
    failed_ids: tuple[str, ...] = ()


@runtime_checkable
class BulkAction(Protocol):
    """One opt-in action over records selected from a generic collection."""

    async def execute(
        self,
        view: BulkActionView,
        request: Request,
        records: Sequence[Model],
    ) -> BulkActionResult: ...


class BulkActionView(Protocol):
    """Minimal generic-view hooks available to a bulk action."""

    async def delete_record(self, request: Request, record: Model) -> bool: ...


@dataclass(frozen=True, slots=True)
class BulkDeleteAction:
    """Built-in destructive action; views must explicitly register it."""

    async def execute(
        self,
        view: BulkActionView,
        request: Request,
        records: Sequence[Model],
    ) -> BulkActionResult:
        affected: list[str] = []
        failed: list[str] = []
        for record in records:
            record_id = str(getattr(record, record._meta.pk_attr))
            if await view.delete_record(request, record):
                affected.append(record_id)
            else:
                failed.append(record_id)
        return BulkActionResult(tuple(affected), failed_ids=tuple(failed))


__all__ = ["BulkAction", "BulkActionResult", "BulkDeleteAction"]
