import logging
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from textwrap import dedent

import pytest

import wybra.db.migrate as migrate_module
import wybra.tools.migrate as tools_migrate
from support_database import sqlite_file_url
from wybra.core.composition import AppConfig, load_app_config
from wybra.db.surfaces import model_package_name


@dataclass(frozen=True, slots=True)
class MigrationTestSettings:
    database_url: str
    project_root: Path = Path.cwd()
    migrations_root: Path | None = None
    app_config: AppConfig | None = None
    modules: tuple[str, ...] = ("wybra.sessions",)
    database_connection: object | None = None
    provisioning_connection: object | None = None


@dataclass(frozen=True, slots=True)
class MigrationCommandFixture:
    app_config: Path
    command: object

    def cleanup(self) -> None:
        self.app_config.unlink(missing_ok=True)


def create_migrate_command(
    *,
    modules: tuple[str, ...] = ("wybra.sessions",),
) -> MigrationCommandFixture:
    app_config_file = tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        suffix=".toml",
        delete=False,
    )
    app_config_file.close()
    app_config_path = _write_test_app_config(Path(app_config_file.name), modules)
    app_config = load_app_config(config_path=app_config_path)

    def load_settings(database_url: str | None) -> MigrationTestSettings:
        if database_url is None:
            raise migrate_module.MigrationConfigurationError(
                "Test database URL is required."
            )

        return MigrationTestSettings(
            database_url=database_url,
            project_root=app_config.project_root,
            app_config=app_config,
            modules=modules,
        )

    return MigrationCommandFixture(
        app_config=app_config_path,
        command=migrate_module.create_migrate_command(load_settings),
    )


def run_migrate(
    argv: list[str],
    *,
    modules: tuple[str, ...] = ("wybra.sessions",),
) -> int:
    fixture = create_migrate_command(modules=modules)
    try:
        return migrate_module.run_migrate_command(fixture.command, argv)
    finally:
        fixture.cleanup()


def _write_test_app_config(config_path: Path, modules: tuple[str, ...]) -> Path:
    modules_toml = ", ".join(f'"{module}"' for module in modules)
    config_path.write_text(
        dedent(
            f"""
            [app]
            modules = [{modules_toml}]

            [app.templates]
            auto_reload = true
            cache_size = 0

            [app.assets]
            url_path = "/static/"

            [app.runserver]
            asgi_app = "test_app:app"
            reload_env = "APP_RELOAD"
            """
        ),
        encoding="utf-8",
    )
    return config_path


def test_migrate_help_exposes_native_tortoise_commands(capsys) -> None:
    fixture = create_migrate_command()

    try:
        exit_code = migrate_module.run_migrate_command(fixture.command, ["--help"])
    finally:
        fixture.cleanup()

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "Run application schema migrations through Tortoise." in captured.out
    for command_name in (
        "init",
        "destroy",
        "makemigrations",
        "migrate",
        "downgrade",
        "history",
        "heads",
        "sqlmigrate",
    ):
        assert command_name in captured.out
    assert "provision" not in captured.out
    assert "upgrade" not in captured.out
    assert "revision" not in captured.out
    assert "current" not in captured.out


@pytest.mark.parametrize("command_name", ["upgrade", "revision", "current"])
def test_migrate_rejects_removed_legacy_command_names(
    command_name: str,
    capsys,
) -> None:
    exit_code = run_migrate(
        [
            "--database-url",
            "sqlite://:memory:",
            command_name,
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 2
    assert command_name in captured.err


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["init"], ["init"]),
        (["init", "wybra_sessions"], ["init", "wybra_sessions"]),
        (
            ["makemigrations", "wybra_sessions", "--empty", "-n", "initial"],
            ["makemigrations", "wybra_sessions", "--empty", "-n", "initial"],
        ),
        (["migrate"], ["migrate"]),
        (
            ["migrate", "wybra_sessions", "0001_initial", "--fake", "--dry-run"],
            ["migrate", "wybra_sessions", "0001_initial", "--fake", "--dry-run"],
        ),
        (
            ["downgrade", "wybra_sessions", "0001_initial", "--fake"],
            ["downgrade", "wybra_sessions", "0001_initial", "--fake"],
        ),
        (["history"], ["history"]),
        (["history", "wybra_sessions"], ["history", "wybra_sessions"]),
        (["heads"], ["heads"]),
        (["heads", "wybra_sessions"], ["heads", "wybra_sessions"]),
        (
            ["sqlmigrate", "wybra_sessions", "0001_initial", "--backward"],
            ["sqlmigrate", "wybra_sessions", "0001_initial", "--backward"],
        ),
    ],
)
def test_migrate_commands_delegate_to_tortoise_cli(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    argv: list[str],
    expected: list[str],
) -> None:
    database_url = sqlite_file_url(tmp_path / "database.sqlite3")
    observed: dict[str, object] = {}

    def record_tortoise_cli(
        context: migrate_module.MigrationContext,
        args: list[str],
    ) -> None:
        observed["args"] = args
        observed["config"] = context.config

    monkeypatch.setattr(migrate_module, "_run_tortoise_cli", record_tortoise_cli)

    exit_code = run_migrate(["--database-url", database_url, *argv])

    assert exit_code == 0
    assert observed["args"] == expected
    assert observed["config"] == {
        "connections": {"default": database_url},
        "apps": {
            "wybra_sessions": {
                "models": ["wybra.sessions.models"],
                "migrations": "wybra.sessions.migrations",
                "default_connection": "default",
            }
        },
    }


def test_migrate_init_uses_normalised_postgresql_tortoise_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = "postgresql://app:secret@db.example/app"
    observed: dict[str, object] = {}

    def record_tortoise_cli(
        context: migrate_module.MigrationContext,
        args: list[str],
    ) -> None:
        observed["args"] = args
        observed["config"] = context.config

    monkeypatch.setattr(migrate_module, "is_supported_database_url", lambda _url: True)
    monkeypatch.setattr(migrate_module, "_run_tortoise_cli", record_tortoise_cli)

    exit_code = run_migrate(
        [
            "--database-url",
            database_url,
            "heads",
        ]
    )

    assert exit_code == 0
    assert observed["args"] == ["heads"]
    assert observed["config"] == {
        "connections": {"default": "asyncpg://app:secret@db.example/app"},
        "apps": {
            "wybra_sessions": {
                "models": ["wybra.sessions.models"],
                "migrations": "wybra.sessions.migrations",
                "default_connection": "default",
            }
        },
    }


def test_migrate_init_runs_provisioning_before_tortoise(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = sqlite_file_url(tmp_path / "database.sqlite3")
    observed: list[str] = []

    def record_initialise_database(
        _context: migrate_module.MigrationContext,
    ) -> None:
        observed.append("provision")

    def record_tortoise_cli(
        _context: migrate_module.MigrationContext,
        args: list[str],
    ) -> None:
        observed.append(f"tortoise:{args[0]}")

    monkeypatch.setattr(
        migrate_module,
        "initialise_database_lifecycle",
        record_initialise_database,
    )
    monkeypatch.setattr(migrate_module, "_run_tortoise_cli", record_tortoise_cli)

    exit_code = run_migrate(["--database-url", database_url, "init"])

    assert exit_code == 0
    assert observed == ["provision", "tortoise:init"]


def test_migrate_lifecycle_reports_provisioning_results(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings = MigrationTestSettings(
        database_url=sqlite_file_url(tmp_path / "database.sqlite3"),
        project_root=tmp_path,
    )
    context = migrate_module.build_migration_context(settings)

    def record_initialise_database(
        _context: migrate_module.ProvisioningContext,
    ) -> tuple[migrate_module.ProvisioningPhaseResult, ...]:
        return (
            migrate_module.ProvisioningPhaseResult(
                family="sqlite",
                phase="init",
                status="skipped",
                message="already provisioned",
            ),
        )

    monkeypatch.setattr(
        migrate_module,
        "initialise_database",
        record_initialise_database,
    )
    caplog.set_level(logging.INFO, logger=migrate_module.logger.name)

    migrate_module.initialise_database_lifecycle(context)

    assert "database lifecycle: sqlite init skipped: already provisioned" in caplog.text


@pytest.mark.parametrize("argv", [["heads"], ["migrate"], ["downgrade"], ["history"]])
def test_ordinary_migration_commands_do_not_run_provisioning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    argv: list[str],
) -> None:
    database_url = sqlite_file_url(tmp_path / "database.sqlite3")

    def fail_lifecycle(*_args, **_kwargs):
        raise AssertionError("database lifecycle should not run")

    def record_tortoise_cli(
        _context: migrate_module.MigrationContext,
        _args: list[str],
    ) -> None:
        return None

    monkeypatch.setattr(migrate_module, "initialise_database_lifecycle", fail_lifecycle)
    monkeypatch.setattr(migrate_module, "destroy_database_lifecycle", fail_lifecycle)
    monkeypatch.setattr(
        migrate_module,
        "run_database_maintenance_lifecycle",
        fail_lifecycle,
    )
    monkeypatch.setattr(migrate_module, "_run_tortoise_cli", record_tortoise_cli)

    exit_code = run_migrate(["--database-url", database_url, *argv])

    assert exit_code == 0


def test_migrate_destroy_requires_confirmation(tmp_path: Path) -> None:
    database_url = sqlite_file_url(tmp_path / "database.sqlite3")

    exit_code = run_migrate(["--database-url", database_url, "destroy"])

    assert exit_code == 2


def test_migrate_destroy_dispatches_database_lifecycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = sqlite_file_url(tmp_path / "database.sqlite3")
    observed: dict[str, object] = {}

    def record_destroy(
        context: migrate_module.MigrationContext,
        request: migrate_module.DestroyDatabaseRequest,
    ) -> None:
        observed["confirm"] = request.confirm
        observed["database"] = context.database_connection.redacted_description

    monkeypatch.setattr(migrate_module, "destroy_database_lifecycle", record_destroy)

    exit_code = run_migrate(
        [
            "--database-url",
            database_url,
            "destroy",
            "--confirm",
            "database.sqlite3",
        ]
    )

    assert exit_code == 0
    assert observed == {
        "confirm": "database.sqlite3",
        "database": "database URL: sqlite://<redacted>",
    }


def test_migrate_run_dispatches_database_maintenance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = sqlite_file_url(tmp_path / "database.sqlite3")
    observed: dict[str, object] = {}

    def record_maintenance(
        context: migrate_module.MigrationContext,
        request: migrate_module.DatabaseMaintenanceRequest,
    ) -> None:
        observed["task"] = request.task
        observed["database"] = context.database_connection.redacted_description

    monkeypatch.setattr(
        migrate_module,
        "run_database_maintenance_lifecycle",
        record_maintenance,
    )

    exit_code = run_migrate(
        [
            "--database-url",
            database_url,
            "migrate",
            "run",
            "vacuum",
        ]
    )

    assert exit_code == 0
    assert observed == {
        "task": "vacuum",
        "database": "database URL: sqlite://<redacted>",
    }


def test_migrate_rejects_unknown_options(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database_url = sqlite_file_url(tmp_path / "database.sqlite3")

    exit_code = run_migrate(
        [
            "--database-url",
            database_url,
            "migrate",
            "--dyr-run",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 2
    assert "No such option" in captured.err
    assert "--dyr-run" in captured.err


@pytest.mark.parametrize("flag", ["--fake", "--dry-run"])
def test_migrate_run_rejects_migration_flags(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    flag: str,
) -> None:
    database_url = sqlite_file_url(tmp_path / "database.sqlite3")

    exit_code = run_migrate(
        [
            "--database-url",
            database_url,
            "migrate",
            "run",
            "vacuum",
            flag,
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 2
    assert "migrate run does not support migration flags" in captured.err


def test_supported_loader_kwargs_logs_when_introspection_fails(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class SignaturelessLoader:
        __signature__ = "not a signature"

        def __call__(self, database_url: str | None) -> MigrationTestSettings:
            if database_url is None:
                raise migrate_module.MigrationConfigurationError(
                    "Test database URL is required."
                )
            return MigrationTestSettings(database_url=database_url)

    caplog.set_level(logging.WARNING, logger=migrate_module.logger.name)

    result = migrate_module._supported_loader_kwargs(
        SignaturelessLoader(),
        {"include_provisioning_connection": True},
    )

    assert result == {}
    assert "migration settings loader signature could not be inspected" in caplog.text


def test_tortoise_config_uses_configured_module_model_surfaces(tmp_path: Path) -> None:
    app_config_path = _write_test_app_config(
        tmp_path / "app.toml",
        ("wybra.messages",),
    )
    app_config = load_app_config(project_root=tmp_path, config_path=app_config_path)
    settings = MigrationTestSettings(
        database_url="sqlite:///app.sqlite3",
        project_root=tmp_path,
        app_config=app_config,
        modules=("wybra.messages",),
    )

    config = migrate_module.build_tortoise_config(settings)

    assert config == {
        "connections": {"default": "sqlite:///app.sqlite3"},
        "apps": {
            "wybra_sessions": {
                "models": [model_package_name("wybra.sessions")],
                "migrations": "wybra.sessions.migrations",
                "default_connection": "default",
            },
            "wybra_messages": {
                "models": [model_package_name("wybra.messages")],
                "migrations": "wybra.messages.migrations",
                "default_connection": "default",
            },
        },
    }


def test_run_tortoise_cli_uses_transient_config_module_and_removes_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, object] = {}
    settings = MigrationTestSettings(
        database_url=sqlite_file_url(tmp_path / "database.sqlite3"),
        project_root=tmp_path,
    )
    context = migrate_module.build_migration_context(settings)

    async def record_cli(argv: list[str]) -> int:
        observed["argv"] = argv
        module_name, variable_name = argv[1].rsplit(".", 1)
        observed["config_module"] = module_name
        observed["config_variable"] = variable_name
        observed["config"] = getattr(sys.modules[module_name], variable_name)
        return 0

    monkeypatch.setattr(migrate_module.tortoise_cli, "run_cli_async", record_cli)

    migrate_module._run_tortoise_cli(context, ["heads"])

    assert observed["argv"] == [
        "--config",
        f"{observed['config_module']}.{migrate_module.TORTOISE_CONFIG_VARIABLE}",
        "heads",
    ]
    assert observed["config_variable"] == migrate_module.TORTOISE_CONFIG_VARIABLE
    assert observed["config"] == context.config
    assert observed["config_module"] not in sys.modules


def test_run_tortoise_cli_reports_non_zero_exit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = MigrationTestSettings(
        database_url=sqlite_file_url(tmp_path / "database.sqlite3"),
        project_root=tmp_path,
    )
    context = migrate_module.build_migration_context(settings)

    async def fail_cli(_argv: list[str]) -> int:
        return 2

    monkeypatch.setattr(migrate_module.tortoise_cli, "run_cli_async", fail_cli)

    with pytest.raises(migrate_module.MigrationStateError, match="exit_code=2"):
        migrate_module._run_tortoise_cli(context, ["heads"])


def test_load_migration_settings_passes_keyword_only_config_source(
    tmp_path: Path,
) -> None:
    observed: dict[str, str | None] = {}

    def load_settings(
        database_url: str | None,
        *,
        config_source: str | None = None,
        include_provisioning_connection: bool = False,
    ) -> MigrationTestSettings:
        observed["database_url"] = database_url
        observed["config_source"] = config_source
        observed["include_provisioning_connection"] = include_provisioning_connection
        return MigrationTestSettings(
            database_url=database_url or "sqlite://:memory:",
            project_root=tmp_path,
        )

    migrate_module._load_migration_settings(
        load_settings,
        "sqlite:///app.sqlite3",
        "app.toml",
    )

    assert observed == {
        "database_url": "sqlite:///app.sqlite3",
        "config_source": "app.toml",
        "include_provisioning_connection": False,
    }


def test_load_migration_settings_passes_config_source_to_kwargs_loader(
    tmp_path: Path,
) -> None:
    observed: dict[str, object] = {}

    def load_settings(
        database_url: str | None, **kwargs: object
    ) -> MigrationTestSettings:
        observed["database_url"] = database_url
        observed["kwargs"] = kwargs
        return MigrationTestSettings(
            database_url=database_url or "sqlite://:memory:",
            project_root=tmp_path,
        )

    migrate_module._load_migration_settings(
        load_settings,
        "sqlite:///app.sqlite3",
        "app.toml",
    )

    assert observed == {
        "database_url": "sqlite:///app.sqlite3",
        "kwargs": {
            "config_source": "app.toml",
            "include_provisioning_connection": False,
        },
    }


def test_load_migration_settings_uses_legacy_loader_without_config_source(
    tmp_path: Path,
) -> None:
    observed: dict[str, str | None] = {}

    def load_settings(database_url: str | None) -> MigrationTestSettings:
        observed["database_url"] = database_url
        return MigrationTestSettings(
            database_url=database_url or "sqlite://:memory:",
            project_root=tmp_path,
        )

    migrate_module._load_migration_settings(
        load_settings,
        "sqlite:///app.sqlite3",
        None,
    )

    assert observed == {"database_url": "sqlite:///app.sqlite3"}


def test_load_migration_settings_rejects_config_source_for_unsupported_loader(
    tmp_path: Path,
) -> None:
    def load_settings(
        database_url: str | None,
        config_source: str | None = None,
        /,
    ) -> MigrationTestSettings:
        return MigrationTestSettings(
            database_url=database_url or "sqlite://:memory:",
            project_root=tmp_path,
        )

    with pytest.raises(
        migrate_module.MigrationConfigurationError,
        match="must accept config_source",
    ):
        migrate_module._load_migration_settings(
            load_settings,
            "sqlite:///app.sqlite3",
            "app.toml",
        )


def test_wybra_migrate_config_option_overrides_app_config_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ambient_config = tmp_path / "ambient.toml"
    selected_config = tmp_path / "selected.toml"
    monkeypatch.setenv("APP_CONFIG", ambient_config.as_posix())
    observed: dict[str, object] = {}

    def load_project_settings(
        *,
        environ=None,
        project_root=None,
        read_dotenv=True,
        database_credential_purpose="runtime",
        fallback_to_runtime_credentials=False,
        include_provisioning_connection=False,
    ):
        observed["app_config"] = None if environ is None else environ.get("APP_CONFIG")
        observed["database_credential_purpose"] = database_credential_purpose
        observed["fallback_to_runtime_credentials"] = fallback_to_runtime_credentials
        observed["include_provisioning_connection"] = include_provisioning_connection
        return MigrationTestSettings(
            database_url="sqlite://:memory:",
            project_root=tmp_path,
        )

    def record_tortoise_cli(
        _context: migrate_module.MigrationContext,
        _args: list[str],
    ) -> None:
        return None

    monkeypatch.setattr(tools_migrate, "runtime_project_root", lambda: tmp_path)
    monkeypatch.setattr(tools_migrate, "load_project_settings", load_project_settings)
    monkeypatch.setattr(
        tools_migrate.data_migrate,
        "_run_tortoise_cli",
        record_tortoise_cli,
    )

    exit_code = tools_migrate.main(["--config", selected_config.as_posix(), "heads"])

    assert exit_code == 0
    assert observed["app_config"] == selected_config.as_posix()
    assert observed["database_credential_purpose"] == "runtime"
    assert observed["fallback_to_runtime_credentials"] is False
    assert observed["include_provisioning_connection"] is False


def test_wybra_migrate_init_requests_provisioning_connection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selected_config = tmp_path / "selected.toml"
    observed: dict[str, object] = {}

    def load_project_settings(
        *,
        environ=None,
        project_root=None,
        read_dotenv=True,
        database_credential_purpose="runtime",
        fallback_to_runtime_credentials=False,
        include_provisioning_connection=False,
    ):
        observed["app_config"] = None if environ is None else environ.get("APP_CONFIG")
        observed["database_credential_purpose"] = database_credential_purpose
        observed["fallback_to_runtime_credentials"] = fallback_to_runtime_credentials
        observed["include_provisioning_connection"] = include_provisioning_connection
        return MigrationTestSettings(
            database_url="sqlite://:memory:",
            project_root=tmp_path,
        )

    def record_lifecycle(_context: migrate_module.MigrationContext) -> None:
        return None

    def record_tortoise_cli(
        _context: migrate_module.MigrationContext,
        _args: list[str],
    ) -> None:
        return None

    monkeypatch.setattr(tools_migrate, "runtime_project_root", lambda: tmp_path)
    monkeypatch.setattr(tools_migrate, "load_project_settings", load_project_settings)
    monkeypatch.setattr(
        tools_migrate.data_migrate,
        "initialise_database_lifecycle",
        record_lifecycle,
    )
    monkeypatch.setattr(
        tools_migrate.data_migrate,
        "_run_tortoise_cli",
        record_tortoise_cli,
    )

    exit_code = tools_migrate.main(["--config", selected_config.as_posix(), "init"])

    assert exit_code == 0
    assert observed["app_config"] == selected_config.as_posix()
    assert observed["database_credential_purpose"] == "runtime"
    assert observed["fallback_to_runtime_credentials"] is False
    assert observed["include_provisioning_connection"] is True
