from pathlib import Path

from click.testing import CliRunner

import wevra.tools.runserver as runserver
from wevra.core.composition import APP_CONFIG_ENV, APP_ROOT_ENV
from wevra.core.config import ENV_APP_ENV
from wevra.db.config import ENV_DATABASE_URL


def _write_pyproject(project_root: Path) -> None:
    (project_root / "pyproject.toml").write_text(
        """
        [tool.wevra]
        runserver_app = "host_app.asgi:app"
        runserver_reload_env = "APP_RELOAD"
        """,
        encoding="utf-8",
    )


def test_runserver_environment_overrides_include_cli_values(tmp_path: Path) -> None:
    database_url = "sqlite+aiosqlite:///runtime.sqlite3"

    assert runserver.runserver_environment_overrides(
        project_root=tmp_path,
        config_source="app/app.toml",
        database_url=database_url,
        deployment_environment="staging",
    ) == {
        APP_ROOT_ENV: tmp_path.resolve().as_posix(),
        APP_CONFIG_ENV: "app/app.toml",
        ENV_DATABASE_URL: database_url,
        ENV_APP_ENV: "staging",
    }


def test_runserver_command_writes_cli_overrides_to_server_environment(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _write_pyproject(tmp_path)
    monkeypatch.setattr(runserver.os, "environ", {})
    observed: dict[str, object] = {}

    def run_uvicorn_command(args):
        observed["args"] = list(args)
        observed["environment"] = {
            name: runserver.os.environ.get(name)
            for name in (APP_ROOT_ENV, APP_CONFIG_ENV, ENV_DATABASE_URL, ENV_APP_ENV)
        }

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(runserver, "run_uvicorn_command", run_uvicorn_command)

    result = CliRunner().invoke(
        runserver.runserver_command,
        [
            "--project",
            tmp_path.as_posix(),
            "--config",
            "app/app.toml",
            "--database-url",
            "sqlite+aiosqlite:///runtime.sqlite3",
            "--deploy",
            "staging",
            "--",
            "--log-level",
            "debug",
        ],
    )

    assert result.exit_code == 0, result.output
    assert observed["args"] == [
        "host_app.asgi:app",
        "--host",
        "127.0.0.1",
        "--port",
        "8000",
        "--log-level",
        "debug",
    ]
    assert observed["environment"] == {
        APP_ROOT_ENV: tmp_path.resolve().as_posix(),
        APP_CONFIG_ENV: "app/app.toml",
        ENV_DATABASE_URL: "sqlite+aiosqlite:///runtime.sqlite3",
        ENV_APP_ENV: "staging",
    }
