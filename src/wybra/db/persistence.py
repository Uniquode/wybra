from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from tortoise import Tortoise
from tortoise.backends.base.client import BaseDBAsyncClient
from tortoise.context import TortoiseContext

from wybra.db.tortoise import build_tortoise_config
from wybra.db.urls import (
    is_memory_database_url,
    is_supported_database_url,
    sqlite_database_path,
)
from wybra.diagnostics.tortoise import instrument_tortoise_context

__all__ = (
    "Database",
    "close_database",
    "create_database",
    "is_memory_database_url",
    "is_supported_database_url",
    "sqlite_database_path",
)


class DatabaseUrlSettings(Protocol):
    """Smallest settings shape accepted by reusable persistence helpers."""

    database_url: str


@dataclass(frozen=True, slots=True)
class Database:
    context: TortoiseContext
    config: dict[str, object]

    def connection(self, name: str = "default") -> BaseDBAsyncClient:
        return self.context.connections.get(name)


async def create_database(
    settings_or_url: DatabaseUrlSettings | str,
    *,
    modules: Sequence[str],
    enable_global_fallback: bool = False,
) -> Database:
    config = build_tortoise_config(
        database_url=_database_url_from(settings_or_url),
        modules=modules,
    )
    context = await Tortoise.init(
        config=config,
        _enable_global_fallback=enable_global_fallback,
    )
    instrument_tortoise_context(context)
    return Database(context=context, config=config)


async def close_database(database: Database) -> None:
    with database.context:
        await database.context.close_connections()


def _database_url_from(settings_or_url: DatabaseUrlSettings | str) -> str:
    if isinstance(settings_or_url, str):
        return settings_or_url

    return settings_or_url.database_url
