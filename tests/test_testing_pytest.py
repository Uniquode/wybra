from __future__ import annotations

pytest_plugins = ("pytester",)


def test_pytest_plugin_provides_migrated_database_and_client(pytester) -> None:
    pytester.makeconftest(
        """
        pytest_plugins = ("wybra.testing_pytest",)

        import pytest
        from fastapi import FastAPI, Request

        from wybra.db import DatabaseCapability
        from wybra.db.capabilities import tortoise_connection
        from wybra.site import get_site
        from wybra.testing import create_test_application


        @pytest.fixture(scope="module")
        def anyio_backend():
            return "asyncio"


        @pytest.fixture(scope="module")
        def wybra_test_app(wybra_test_config):
            app = FastAPI()

            @app.get("/database")
            async def database(request: Request):
                database = get_site(request.app).require_capability(DatabaseCapability)
                connection = tortoise_connection(
                    database,
                    database.database().for_write(),
                )
                await connection.execute_query(
                    "INSERT INTO sessions_session "
                    "(id, data, created_at, updated_at, expires_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    ["first", "{}", 1.0, 1.0, 2.0],
                )
                return {"saved": True}

            return create_test_application(wybra_test_config, app=app)
        """
    )
    pytester.makepyfile(
        """
        import pytest


        @pytest.mark.anyio
        async def test_first(wybra_test_client):
            response = await wybra_test_client.get("/database")
            assert response.json() == {"saved": True}


        @pytest.mark.anyio
        async def test_second(wybra_test_database):
            _count, rows = await wybra_test_database.connection().execute_query(
                "SELECT COUNT(*) AS count FROM sessions_session"
            )
            assert rows[0]["count"] == 0
        """
    )

    result = pytester.runpytest("-q", "-W", "error")

    result.assert_outcomes(passed=2)
