import json
import tempfile
from dataclasses import dataclass
from pathlib import Path
from textwrap import dedent
from types import SimpleNamespace

import pytest

import wybra.db.migrate as migrate_module
import wybra.db.provisioning as provisioning_module
import wybra.tools.migrate as tools_migrate
from support_database import sqlite_file_url
from wybra.core.composition import AppConfig, load_app_config
from wybra.db.surfaces import model_package_name
from wybra.db.tortoise import tortoise_database_url


@dataclass(frozen=True, slots=True)
class MigrationTestSettings:
    database_url: str
    project_root: Path = Path.cwd()
    migrations_root: Path | None = None
    app_config: AppConfig | None = None
    modules: tuple[str, ...] = ("wybra.sessions",)


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
        "makemigrations",
        "migrate",
        "downgrade",
        "history",
        "heads",
        "sqlmigrate",
    ):
        assert command_name in captured.out
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
            "sqlite+aiosqlite:///:memory:",
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
        "connections": {"default": tortoise_database_url(database_url)},
        "apps": {
            "wybra_sessions": {
                "models": ["wybra.sessions.models"],
                "migrations": "wybra.sessions.migrations",
                "default_connection": "default",
            }
        },
    }


def test_migrate_init_provisions_postgresql_before_tortoise_init(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = "postgresql+asyncpg://app:secret@db.example/app"
    admin_database_url = "postgresql+asyncpg://admin:admin-secret@db.example/postgres"
    events: list[str] = []
    observed: dict[str, object] = {}

    def provision(database_url_arg: str, admin_database_url_arg: str | None) -> None:
        events.append("provision")
        observed["provision"] = (database_url_arg, admin_database_url_arg)

    def record_tortoise_cli(
        context: migrate_module.MigrationContext,
        args: list[str],
    ) -> None:
        events.append("tortoise")
        observed["args"] = args
        observed["config"] = context.config

    monkeypatch.setattr(migrate_module, "provision_postgresql_database", provision)
    monkeypatch.setattr(migrate_module, "_run_tortoise_cli", record_tortoise_cli)

    exit_code = run_migrate(
        [
            "--database-url",
            database_url,
            "init",
            "--admin-database-url",
            admin_database_url,
        ]
    )

    assert exit_code == 0
    assert events == ["provision", "tortoise"]
    assert observed["provision"] == (database_url, admin_database_url)
    assert observed["args"] == ["init"]
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


def test_migrate_init_reports_missing_postgresql_admin_url(
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    database_url = "postgresql+asyncpg://app:secret@db.example/app"

    def fail_tortoise_cli(
        _context: migrate_module.MigrationContext,
        _args: list[str],
    ) -> None:
        raise AssertionError("PostgreSQL init should provision before Tortoise")

    monkeypatch.delenv("SA_DATABASE_URL", raising=False)
    monkeypatch.setattr(migrate_module, "_run_tortoise_cli", fail_tortoise_cli)

    exit_code = run_migrate(["--database-url", database_url, "init"])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "configuration: failed" in captured.err
    assert "--admin-database-url" in captured.err
    assert "SA_DATABASE_URL" in captured.err
    assert "secret" not in captured.err
    assert "Traceback" not in captured.err


def test_migrate_init_rejects_blank_admin_database_url_without_env_fallback(
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    database_url = "postgresql+asyncpg://app:secret@db.example/app"

    def fail_tortoise_cli(
        _context: migrate_module.MigrationContext,
        _args: list[str],
    ) -> None:
        raise AssertionError("PostgreSQL init should provision before Tortoise")

    monkeypatch.setenv(
        "SA_DATABASE_URL",
        "postgresql+asyncpg://admin:env-secret@db.example/postgres",
    )
    monkeypatch.setattr(migrate_module, "_run_tortoise_cli", fail_tortoise_cli)

    exit_code = run_migrate(
        ["--database-url", database_url, "init", "--admin-database-url", ""]
    )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "configuration: failed" in captured.err
    assert "--admin-database-url must not be blank" in captured.err
    assert "env-secret" not in captured.err


def test_tortoise_config_uses_configured_module_model_surfaces(tmp_path: Path) -> None:
    app_config_path = _write_test_app_config(
        tmp_path / "app.toml",
        ("wybra.messages",),
    )
    app_config = load_app_config(project_root=tmp_path, config_path=app_config_path)
    settings = MigrationTestSettings(
        database_url="sqlite+aiosqlite:///app.sqlite3",
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


@pytest.mark.parametrize(
    ("database_url", "expected"),
    [
        ("sqlite+aiosqlite:///:memory:", "sqlite://:memory:"),
        ("sqlite+aiosqlite:///relative.sqlite3", "sqlite:///relative.sqlite3"),
        ("sqlite+aiosqlite:////tmp/app.sqlite3", "sqlite:////tmp/app.sqlite3"),
        (
            "postgresql+asyncpg://user:secret@db.example/app",
            "asyncpg://user:secret@db.example/app",
        ),
        (
            "postgres://user:secret@db.example/app",
            "postgres://user:secret@db.example/app",
        ),
    ],
)
def test_tortoise_database_url_converts_wybra_database_urls(
    database_url: str,
    expected: str,
) -> None:
    assert tortoise_database_url(database_url) == expected


def test_run_tortoise_cli_writes_temp_json_config_and_removes_it(
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
        config_path = Path(argv[1])
        observed["argv"] = argv
        observed["config_path"] = config_path
        observed["config"] = json.loads(config_path.read_text(encoding="utf-8"))
        return 0

    monkeypatch.setattr(migrate_module.tortoise_cli, "run_cli_async", record_cli)

    migrate_module._run_tortoise_cli(context, ["heads"])

    assert observed["argv"] == [
        "--config-file",
        Path(observed["config_path"]).as_posix(),
        "heads",
    ]
    assert observed["config"] == context.config
    assert not Path(observed["config_path"]).exists()


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


def test_postgresql_provisioning_uses_dbscripts_with_sync_urls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, object] = {}
    monkeypatch.delenv("SA_DATABASE_URL", raising=False)

    def pg_db_info(*, url: str):
        observed["database_url"] = url
        return SimpleNamespace(name="app")

    def pg_setup(db: object) -> None:
        observed["setup"] = db
        observed["admin_url"] = provisioning_module.os.environ.get("SA_DATABASE_URL")

    monkeypatch.setattr(
        provisioning_module,
        "_dbscripts_dblib",
        lambda: SimpleNamespace(pg_db_info=pg_db_info, pg_setup=pg_setup),
        raising=False,
    )

    migrate_module.provision_postgresql_database(
        "postgresql+asyncpg://app:secret@db.example/app",
        "postgresql+asyncpg://admin:admin-secret@db.example/postgres",
    )

    assert observed["database_url"] == "postgresql://app:secret@db.example/app"
    assert (
        observed["admin_url"] == "postgresql://admin:admin-secret@db.example/postgres"
    )
    assert "SA_DATABASE_URL" not in provisioning_module.os.environ


def test_postgresql_provisioning_rejects_unsupported_database_url() -> None:
    with pytest.raises(
        provisioning_module.DatabaseProvisioningConfigurationError,
        match="requires a postgresql database URL",
    ) as excinfo:
        migrate_module.provision_postgresql_database(
            "sqlite+aiosqlite:///:memory:",
            "postgresql+asyncpg://admin:admin-secret@db.example/postgres",
        )

    assert isinstance(excinfo.value, provisioning_module.DatabaseProvisioningError)


def test_postgresql_provisioning_reports_dbscripts_failures_as_operations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def pg_db_info(*, url: str):
        return SimpleNamespace(name="app", url=url)

    def pg_setup(_db: object) -> None:
        raise RuntimeError("could not connect")

    monkeypatch.setattr(
        provisioning_module,
        "_dbscripts_dblib",
        lambda: SimpleNamespace(pg_db_info=pg_db_info, pg_setup=pg_setup),
        raising=False,
    )

    with pytest.raises(
        provisioning_module.DatabaseProvisioningOperationError,
        match="could not connect",
    ) as excinfo:
        migrate_module.provision_postgresql_database(
            "postgresql+asyncpg://app:secret@db.example/app",
            "postgresql+asyncpg://admin:admin-secret@db.example/postgres",
        )

    assert isinstance(excinfo.value, provisioning_module.DatabaseProvisioningError)


def test_load_migration_settings_passes_keyword_only_config_source(
    tmp_path: Path,
) -> None:
    observed: dict[str, str | None] = {}

    def load_settings(
        database_url: str | None,
        *,
        config_source: str | None = None,
    ) -> MigrationTestSettings:
        observed["database_url"] = database_url
        observed["config_source"] = config_source
        return MigrationTestSettings(
            database_url=database_url or "sqlite+aiosqlite:///:memory:",
            project_root=tmp_path,
        )

    migrate_module._load_migration_settings(
        load_settings,
        "sqlite+aiosqlite:///app.sqlite3",
        "app.toml",
    )

    assert observed == {
        "database_url": "sqlite+aiosqlite:///app.sqlite3",
        "config_source": "app.toml",
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
            database_url=database_url or "sqlite+aiosqlite:///:memory:",
            project_root=tmp_path,
        )

    migrate_module._load_migration_settings(
        load_settings,
        "sqlite+aiosqlite:///app.sqlite3",
        "app.toml",
    )

    assert observed == {
        "database_url": "sqlite+aiosqlite:///app.sqlite3",
        "kwargs": {"config_source": "app.toml"},
    }


def test_load_migration_settings_uses_legacy_loader_without_config_source(
    tmp_path: Path,
) -> None:
    observed: dict[str, str | None] = {}

    def load_settings(database_url: str | None) -> MigrationTestSettings:
        observed["database_url"] = database_url
        return MigrationTestSettings(
            database_url=database_url or "sqlite+aiosqlite:///:memory:",
            project_root=tmp_path,
        )

    migrate_module._load_migration_settings(
        load_settings,
        "sqlite+aiosqlite:///app.sqlite3",
        None,
    )

    assert observed == {"database_url": "sqlite+aiosqlite:///app.sqlite3"}


def test_load_migration_settings_rejects_config_source_for_unsupported_loader(
    tmp_path: Path,
) -> None:
    def load_settings(
        database_url: str | None,
        config_source: str | None = None,
        /,
    ) -> MigrationTestSettings:
        return MigrationTestSettings(
            database_url=database_url or "sqlite+aiosqlite:///:memory:",
            project_root=tmp_path,
        )

    with pytest.raises(
        migrate_module.MigrationConfigurationError,
        match="must accept config_source",
    ):
        migrate_module._load_migration_settings(
            load_settings,
            "sqlite+aiosqlite:///app.sqlite3",
            "app.toml",
        )


def test_wybra_migrate_config_option_overrides_app_config_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ambient_config = tmp_path / "ambient.toml"
    selected_config = tmp_path / "selected.toml"
    monkeypatch.setenv("APP_CONFIG", ambient_config.as_posix())
    observed: dict[str, str | None] = {}

    def load_project_settings(*, environ=None, project_root=None, read_dotenv=True):
        observed["app_config"] = None if environ is None else environ.get("APP_CONFIG")
        return MigrationTestSettings(
            database_url="sqlite+aiosqlite:///:memory:",
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
