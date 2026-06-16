from collections.abc import Awaitable, Callable
from typing import Any

import pytest

from wybra.core.composition import APP_CONFIG_ENV, APP_ROOT_ENV
from wybra.core.config import ENV_APP_ENV
from wybra.db.config import ENV_DATABASE_URL
from wybra.db.models import metadata


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(autouse=True)
def isolate_runtime_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep project-level shell config from changing test config discovery."""
    for name in (APP_ROOT_ENV, APP_CONFIG_ENV, ENV_DATABASE_URL, ENV_APP_ENV):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def create_database_schema() -> Callable[[Any], Awaitable[None]]:
    async def _create_database_schema(capability: Any) -> None:
        async with capability.database.transaction() as db_session:

            def _create_all(sync_session: Any) -> None:
                metadata.create_all(sync_session.get_bind())

            await db_session.run_sync(_create_all)

    return _create_database_schema
