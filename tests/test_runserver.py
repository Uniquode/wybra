from pathlib import Path

from click.testing import CliRunner

import wybra.tools.runserver as runserver
import wybra.tools.runserver_uvicorn as runserver_uvicorn
from wybra.core.composition import APP_CONFIG_ENV, APP_ROOT_ENV
from wybra.core.config import ENV_APP_DEBUG, ENV_APP_ENV
from wybra.core.logging import (
    DEFAULT_LOG_DATE_FORMAT,
    DEFAULT_LOG_FORMAT,
    default_logging_config,
)
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

    def run_uvicorn_command(args, *, logging_config):
        observed["args"] = list(args)
        observed["logging_config"] = logging_config
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
    assert observed["logging_config"]["formatters"]["simple"]["format"] == (
        DEFAULT_LOG_FORMAT
    )
    assert observed["logging_config"]["formatters"]["simple"]["datefmt"] == (
        DEFAULT_LOG_DATE_FORMAT
    )
    assert observed["environment"] == {
        APP_ROOT_ENV: tmp_path.resolve().as_posix(),
        APP_CONFIG_ENV: "app/app.toml",
        ENV_DATABASE_URL: "sqlite+aiosqlite:///runtime.sqlite3",
        ENV_APP_ENV: "staging",
    }


def test_runserver_command_writes_debug_override_to_server_environment(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config_path = tmp_path / "configured.toml"
    _write_app_config(config_path)
    monkeypatch.setattr(runserver.os, "environ", {})
    observed: dict[str, str | None] = {}

    def run_uvicorn_command(args, *, logging_config):
        observed["debug"] = runserver.os.environ.get(ENV_APP_DEBUG)

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(runserver, "run_uvicorn_command", run_uvicorn_command)

    result = CliRunner().invoke(
        runserver.runserver_command,
        ["--config", config_path.as_posix(), "--debug"],
    )

    assert result.exit_code == 0, result.output
    assert observed["debug"] == "true"


def test_runserver_command_writes_no_debug_override_to_server_environment(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config_path = tmp_path / "configured.toml"
    _write_app_config(config_path)
    monkeypatch.setattr(runserver.os, "environ", {})
    observed: dict[str, str | None] = {}

    def run_uvicorn_command(args, *, logging_config):
        observed["debug"] = runserver.os.environ.get(ENV_APP_DEBUG)

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(runserver, "run_uvicorn_command", run_uvicorn_command)

    result = CliRunner().invoke(
        runserver.runserver_command,
        ["--config", config_path.as_posix(), "--no-debug"],
    )

    assert result.exit_code == 0, result.output
    assert observed["debug"] == "false"


def test_runserver_command_omits_debug_override_when_not_supplied(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config_path = tmp_path / "configured.toml"
    _write_app_config(config_path)
    monkeypatch.setattr(runserver.os, "environ", {})
    observed: dict[str, str | None] = {}

    def run_uvicorn_command(args, *, logging_config):
        observed["debug"] = runserver.os.environ.get(ENV_APP_DEBUG)

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(runserver, "run_uvicorn_command", run_uvicorn_command)

    result = CliRunner().invoke(
        runserver.runserver_command,
        ["--config", config_path.as_posix()],
    )

    assert result.exit_code == 0, result.output
    assert observed["debug"] is None


def test_runserver_debug_help_distinguishes_logging_verbosity() -> None:
    result = CliRunner().invoke(runserver.runserver_command, ["--help"])

    assert result.exit_code == 0
    assert "--debug / --no-debug" in result.output
    assert "not logging verbosity" in result.output


def test_runserver_command_uses_app_config_startup_defaults(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config_path = tmp_path / "configured.toml"
    _write_app_config(config_path)
    monkeypatch.setattr(runserver.os, "environ", {"APP_CONFIG_RELOAD": "true"})
    observed: dict[str, object] = {}

    def run_uvicorn_command(args, *, logging_config):
        observed["args"] = list(args)
        observed["logging_config"] = logging_config

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
    assert observed["logging_config"]["formatters"]["simple"]["format"] == (
        DEFAULT_LOG_FORMAT
    )
    assert observed["logging_config"]["formatters"]["simple"]["datefmt"] == (
        DEFAULT_LOG_DATE_FORMAT
    )


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


def test_uvicorn_logging_config_adds_uvicorn_loggers_without_colour_formatters() -> (
    None
):
    config = runserver_uvicorn.uvicorn_logging_config(default_logging_config())

    for logger_name in runserver_uvicorn.UVICORN_LOGGER_NAMES:
        assert config["loggers"][logger_name]["handlers"] == ["console"]
        assert config["loggers"][logger_name]["propagate"] is False

    assert "DefaultFormatter" not in repr(config)
    assert "AccessFormatter" not in repr(config)
    assert "ColourizedFormatter" not in repr(config)
    assert "\x1b[" not in repr(config)


def test_uvicorn_log_config_path_is_added_to_existing_args(tmp_path: Path) -> None:
    log_config = tmp_path / "uvicorn-log.json"

    args = runserver_uvicorn.build_uvicorn_args_from_existing(
        ["host.app:app", "--host", "127.0.0.1"],
        log_config_path=log_config,
    )

    assert args == [
        "host.app:app",
        "--host",
        "127.0.0.1",
        "--log-config",
        log_config.as_posix(),
    ]


def test_effective_client_endpoint_uses_direct_peer_without_trusted_proxy() -> None:
    endpoint = runserver_uvicorn.effective_client_endpoint(
        client=("127.0.0.1", 55320),
        headers=[(b"x-forwarded-for", b"203.0.113.10:443")],
        trusted_proxy=False,
    )

    assert endpoint == runserver_uvicorn.ClientEndpoint("127.0.0.1", 55320)


def test_effective_client_endpoint_uses_trusted_forwarded_header() -> None:
    endpoint = runserver_uvicorn.effective_client_endpoint(
        client=("10.0.0.5", 41234),
        headers=[(b"forwarded", b'for="203.0.113.10:443";proto=https')],
        trusted_proxy=True,
    )

    assert endpoint == runserver_uvicorn.ClientEndpoint("203.0.113.10", 443)


def test_effective_client_endpoint_uses_trusted_x_forwarded_for_port_fallback() -> None:
    endpoint = runserver_uvicorn.effective_client_endpoint(
        client=("10.0.0.5", 41234),
        headers=[(b"x-forwarded-for", b"203.0.113.10, 10.0.0.5")],
        trusted_proxy=True,
    )

    assert endpoint == runserver_uvicorn.ClientEndpoint("203.0.113.10", 41234)
