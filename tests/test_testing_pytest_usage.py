import pytest

pytest_plugins = ("wybra.testing_pytest",)


@pytest.mark.anyio
async def test_pytest_plugin_uses_repository_fixture_scopes(
    wybra_test_client,
    wybra_test_database,
) -> None:
    """Exercise the plugin with this repository's shared ``conftest.py``."""
    response = await wybra_test_client.get("/missing")
    _count, rows = await wybra_test_database.connection().execute_query(
        "SELECT COUNT(*) AS count FROM tortoise_migrations"
    )

    assert response.status_code == 404
    assert rows[0]["count"] > 0
