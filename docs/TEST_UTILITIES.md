# Test Utilities

`wybra.testing` provides pytest-native helpers for testing Wybra modules and
applications. The ordinary test suite stays separate from the Docker-backed
backend integration suite in `tests_integration`.

## Pytest Plugin

Enable the explicit plugin in a test suite's `conftest.py`:

```python
pytest_plugins = ("wybra.testing_pytest",)


@pytest.fixture(scope="module")
def anyio_backend() -> str:
    return "asyncio"
```

The plugin creates one local, in-memory SQLite database for each test module,
applies the configured modules' native migrations once, and clears application
tables before and after every test. Migration history is retained, so the
schema is not rebuilt between tests.

The plugin provides:

| Fixture | Purpose |
| --- | --- |
| `wybra_test_modules` | Module tuple to migrate and compose. Override it for application modules. |
| `wybra_test_config` | Minimal local application configuration. |
| `wybra_test_app` | Composed FastAPI application. Override it to add test routes. |
| `wybra_test_application` | Composed application, client, and migrated database. |
| `wybra_test_client` | Async in-process HTTP client with application lifespan active. |
| `wybra_test_database` | Live migrated Tortoise connection wrapper. |

For example, an end-to-end test can write through a real route and inspect the
same migrated database:

```python
@pytest.mark.anyio
async def test_settings_save(wybra_test_client, wybra_test_database) -> None:
    response = await wybra_test_client.post("/settings", data={"title": "New"})

    assert response.status_code == 200
    assert await Setting.filter(title="New").exists()
```

The client is asynchronous. It runs through `httpx2.ASGITransport`, so no
network server or port is created. Cookies persist between requests while the
fixture is active.

Use `WybraTestClient` for direct application tests. It manages the application
lifespan as either a normal or async context manager.

For an application already composed with an in-memory database, use the async
context manager or `wybra_test_client` fixture. An ad-hoc synchronous request
such as `WybraTestClient(app).get(...)` runs on its own portal thread and is
only supported for persistent database URLs.

## Direct Helpers

Use direct helpers when a test does not need the pytest plugin:

```python
import pytest

from wybra.testing import (
    create_test_user,
    memory_session_storage,
    migrated_test_database,
)


@pytest.mark.anyio
async def test_account_service() -> None:
    async with migrated_test_database(modules=("wybra.db", "wybra.auth")):
        user = await create_test_user(email="person@example.test")
        sessions = memory_session_storage()

        assert user.is_verified
```

`migrated_test_database()` is an async context manager. It owns an isolated
in-memory database connection and applies the supplied modules' native
migrations before yielding it. Call `await database.clear()` when a direct test
needs to reset its data without rebuilding its schema.

`create_test_database()` returns the underlying database for tests that need to
own a temporary file-backed database or manage its lifecycle explicitly. It
also applies native migrations. `migrate_test_database()` applies the same
migrations to a database already created by a composed site.

`create_test_application()` and `migrated_test_application()` compose a FastAPI
application and provide the same lifespan-managed client/database pair without
pytest fixtures.

## Test Doubles

`RecordingMessages` captures queued messages as `AlertRecord` values.
`RecordingIdentityDelivery` captures password-reset and verification delivery
requests. `create_test_site()` creates an uncomposed `Site` for narrow module
tests that register a test capability directly.

`configuration_with_overrides()` returns a copied configuration mapping with
section-specific overrides. The input mapping is never mutated.
