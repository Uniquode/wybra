"""Explicit pytest fixtures built on :mod:`wybra.testing` helpers.

Enable this module in a test suite's ``conftest.py`` with::

    pytest_plugins = ("wybra.testing_pytest",)

The module-scoped database fixture requires a module-scoped ``anyio_backend``
fixture. Tests can override the fixture defaults to configure application
modules and application settings.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence

import httpx2
import pytest
from fastapi import FastAPI

from wybra.testing import (
    MigratedTestApplication,
    MigratedTestDatabase,
    application_test_config,
    create_test_application,
    migrated_test_application,
)


@pytest.fixture(scope="module")
def wybra_test_modules() -> tuple[str, ...]:
    """Configured application modules for the test database and application."""
    return ("wybra.db",)


@pytest.fixture(scope="module")
async def wybra_test_application(
    anyio_backend: object,
    wybra_test_app: FastAPI,
) -> AsyncIterator[MigratedTestApplication]:
    """Compose one migrated in-memory application for the test module."""
    del anyio_backend
    async with migrated_test_application(wybra_test_app) as application:
        yield application


@pytest.fixture(scope="module")
def _wybra_test_database(
    wybra_test_application: MigratedTestApplication,
) -> MigratedTestDatabase:
    """Return the module-scoped database after native migrations run."""
    return wybra_test_application.database


@pytest.fixture
async def wybra_test_database(
    _wybra_test_database: MigratedTestDatabase,
) -> AsyncIterator[MigratedTestDatabase]:
    """Yield a clean migrated database for one database-backed test."""
    await _wybra_test_database.clear()
    try:
        yield _wybra_test_database
    finally:
        await _wybra_test_database.clear()


@pytest.fixture(scope="module")
def wybra_test_config(
    wybra_test_modules: Sequence[str],
) -> dict[str, dict[str, object]]:
    """Return the minimal local config for a migrated test application."""
    return application_test_config(
        modules=wybra_test_modules,
    )


@pytest.fixture(scope="module")
def wybra_test_app(wybra_test_config: dict[str, dict[str, object]]) -> FastAPI:
    """Return an application composed when ``wybra_test_client`` is entered."""
    return create_test_application(wybra_test_config)


@pytest.fixture
async def wybra_test_client(
    wybra_test_database: MigratedTestDatabase,
    wybra_test_application: MigratedTestApplication,
) -> AsyncIterator[httpx2.AsyncClient]:
    """Yield an async client for the composed, migrated test application."""
    del wybra_test_database
    yield wybra_test_application.client
