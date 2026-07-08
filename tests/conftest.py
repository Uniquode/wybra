from collections.abc import Awaitable, Callable, Iterator
from typing import Any

import pytest

from wybra.config import ConfigService
from wybra.core.composition import APP_CONFIG_ENV, APP_ROOT_ENV
from wybra.core.config import (
    ENV_APP_DEBUG,
    ENV_APP_ENV,
    ENV_WYBRA_DIAGNOSTICS_ENABLED,
    ENV_WYBRA_DIAGNOSTICS_LEVEL,
    ENV_WYBRA_DIAGNOSTICS_LOGGING_BRIDGE,
    ENV_WYBRA_DIAGNOSTICS_SLOW_SQL_SECONDS,
)
from wybra.db.config import ENV_DATABASE_URL


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(autouse=True)
def isolate_runtime_environment(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Keep project-level shell config from changing test config discovery."""
    ConfigService.set_runtime_environment({})
    for name in (
        APP_ROOT_ENV,
        APP_CONFIG_ENV,
        ENV_DATABASE_URL,
        ENV_APP_ENV,
        ENV_APP_DEBUG,
        ENV_WYBRA_DIAGNOSTICS_ENABLED,
        ENV_WYBRA_DIAGNOSTICS_LEVEL,
        ENV_WYBRA_DIAGNOSTICS_LOGGING_BRIDGE,
        ENV_WYBRA_DIAGNOSTICS_SLOW_SQL_SECONDS,
    ):
        monkeypatch.delenv(name, raising=False)
    yield
    ConfigService.set_runtime_environment({})


@pytest.fixture
def create_database_schema() -> Callable[[Any], Awaitable[None]]:
    async def _create_database_schema(capability: Any) -> None:
        database = getattr(
            capability,
            "database",
            getattr(getattr(capability, "catalogue", None), "database", None),
        )
        if database is None:
            raise RuntimeError(
                "Test capability does not expose a database for schema creation."
            )
        backing_database = getattr(database, "_database", None)
        context = getattr(backing_database, "context", None)
        if context is None:
            raise RuntimeError(
                "Test database does not expose a Tortoise context for schema creation."
            )
        await context.generate_schemas()

    return _create_database_schema
