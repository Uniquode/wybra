from collections.abc import Iterator

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


@pytest.fixture(scope="module")
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
