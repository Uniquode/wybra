from collections.abc import Awaitable, Callable
from typing import Any

import pytest

from wybra.db.models import metadata


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def create_database_schema() -> Callable[[Any], Awaitable[None]]:
    async def _create_database_schema(capability: Any) -> None:
        async with capability.database.transaction() as db_session:

            def _create_all(sync_session: Any) -> None:
                metadata.create_all(sync_session.get_bind())

            await db_session.run_sync(_create_all)

    return _create_database_schema
