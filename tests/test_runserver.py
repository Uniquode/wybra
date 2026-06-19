from pathlib import Path

from click.testing import CliRunner

import wybra.tools.runserver as runserver
from wybra.core.composition import APP_CONFIG_ENV, APP_ROOT_ENV
from wybra.core.config import ENV_APP_ENV
from wybra.db.config import ENV_DATABASE_URL


def _write_app_config(
    config_path: Path,
    *,
    asgi_app: str | None = "host_app.configured:app",
    reload_env: str | None = "APP_CONFIG_RELOAD",
) -> None:
    runserver_section = (
        f"""
        [app.runserver]
        asgi_app = "{asgi_app}"
        reload_env = "{reload_env}"
        """
        if asgi_app is not None and reload_env is not None
        else ""
    )
    config_path.write_text(
        f"""
        [app]
        modules = ["host_app"]

        {runserver_section}

        [app.templates]
        auto_reload = true
        cache_size = 0

        [app.assets]
        url_path = "/static/"
        root = "static"
        """,
        encoding="utf-8",
    )


def test_runserver_command_writes_cli_overrides_to_server_environment(
    tmp_path: Path,
    monkeypatch,
) -> None:
    (tmp_path / "app").mkdir()
    _write_app_config(tmp_path / "app" / "app.toml")
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
        "host_app.configured:app",
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


def test_runserver_command_uses_app_config_startup_defaults(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config_path = tmp_path / "configured.toml"
    _write_app_config(config_path)
    monkeypatch.setattr(runserver.os, "environ", {"APP_CONFIG_RELOAD": "true"})
    observed: dict[str, object] = {}

    def run_uvicorn_command(args):
        observed["args"] = list(args)

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(runserver, "run_uvicorn_command", run_uvicorn_command)

    result = CliRunner().invoke(
        runserver.runserver_command,
        ["--config", config_path.as_posix()],
    )

    assert result.exit_code == 0, result.output
    assert observed["args"] == [
        "host_app.configured:app",
        "--host",
        "127.0.0.1",
        "--port",
        "8000",
        "--reload",
    ]


def test_runserver_rejects_missing_app_config_startup_target(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config_path = tmp_path / "configured.toml"
    _write_app_config(config_path, asgi_app=None, reload_env=None)
    monkeypatch.setattr(runserver.os, "environ", {})

    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(
        runserver.runserver_command,
        ["--config", config_path.as_posix()],
    )

    assert result.exit_code == 1
    assert "[app.runserver].asgi_app must be configured" in result.output
