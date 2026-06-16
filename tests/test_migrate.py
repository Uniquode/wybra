import asyncio
import sqlite3
import tempfile
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from textwrap import dedent
from types import SimpleNamespace

import pytest
from sqlalchemy.exc import SQLAlchemyError

import wybra.db.migrate as migrate_module
import wybra.db.provisioning as provisioning_module


@dataclass(frozen=True, slots=True)
class MigrationTestSettings:
    database_url: str
    alembic_config: Path
    migrations_root: Path | None = None
    app_config: None = None
    modules: tuple[str, ...] = ("wybra.auth",)


def sqlite_file_url(path: Path) -> str:
    return f"sqlite+aiosqlite:///{path.resolve().as_posix()}"


def alembic_config_path() -> Path:
    alembic_config = tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        suffix=".ini",
        delete=False,
    )
    alembic_config.write(
        dedent(
            """
            [alembic]

            [loggers]
            keys = root,sqlalchemy,alembic

            [handlers]
            keys = console

            [formatters]
            keys = generic

            [logger_root]
            level = WARNING
            handlers = console
            qualname =

            [logger_sqlalchemy]
            level = WARNING
            handlers =
            qualname = sqlalchemy.engine

            [logger_alembic]
            level = INFO
            handlers =
            qualname = alembic

            [handler_console]
            class = StreamHandler
            args = (sys.stderr,)
            level = NOTSET
            formatter = generic

            [formatter_generic]
            format = %(levelname)-5.5s [%(name)s] %(message)s
            datefmt = %H:%M:%S
            """
        )
    )
    alembic_config.close()
    return Path(alembic_config.name)


def create_migrate_command(
    *,
    modules: tuple[str, ...] = ("wybra.auth",),
) -> tuple[Path, object]:
    config_path = alembic_config_path()

    def load_settings(database_url: str | None) -> MigrationTestSettings:
        if database_url is None:
            raise migrate_module.MigrationConfigurationError(
                "Test database URL is required."
            )

        return MigrationTestSettings(
            database_url=database_url,
            alembic_config=config_path,
            modules=modules,
        )

    return config_path, migrate_module.create_migrate_command(load_settings)


def run_migrate(
    argv: list[str],
    *,
    modules: tuple[str, ...] = ("wybra.auth",),
) -> int:
    config_path, command = create_migrate_command(modules=modules)
    try:
        return migrate_module.run_migrate_command(command, argv)
    finally:
        config_path.unlink(missing_ok=True)


def create_importable_module(root: Path, module_name: str) -> Path:
    module_path = root.joinpath(*module_name.split("."))
    module_path.mkdir(parents=True)
    (module_path / "__init__.py").write_text("", encoding="utf-8")
    return module_path


def test_migrate_current_reports_missing_sqlite_database_without_creating_file(
    tmp_path: Path,
    capsys,
) -> None:
    database_path = tmp_path / "missing.sqlite3"

    exit_code = run_migrate(
        ["--database-url", sqlite_file_url(database_path), "current"]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "database: not initialised" in captured.out
    assert "SQLite database file does not exist" in captured.out
    assert "current revision: none" in captured.out
    assert not database_path.exists()


def test_migrate_current_reports_reachable_uninitialised_database(
    tmp_path: Path,
    capsys,
) -> None:
    database_path = tmp_path / "empty.sqlite3"
    sqlite3.connect(database_path).close()

    exit_code = run_migrate(
        ["--database-url", sqlite_file_url(database_path), "current"]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "database: not initialised" in captured.out
    assert "current revision: none" in captured.out


def test_migrate_current_reports_connection_failure_without_credentials(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    database_url = "postgresql+asyncpg://user:secret@db.example/app"
    driver_error_url = "postgresql://user:secret@db.example/app"

    def fail_inspection(_database_url: str) -> migrate_module.MigrationState:
        raise SQLAlchemyError(f"could not connect to {driver_error_url}")

    monkeypatch.setattr(migrate_module, "inspect_migration_state", fail_inspection)

    exit_code = run_migrate(["--database-url", database_url, "current"])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "migration: failed" in captured.err
    assert "postgresql://***:***@db.example/app" in captured.err
    assert "secret" not in captured.err
    assert "Traceback" not in captured.err


def test_migrate_current_preserves_original_error_when_cleanup_fails(
    monkeypatch,
    capsys,
) -> None:
    class BrokenConnection:
        async def __aenter__(self):
            raise SQLAlchemyError(
                "could not connect to postgresql://user:secret@db.example/app"
            )

        async def __aexit__(self, *_args: object) -> None:
            return None

    class BrokenEngine:
        def begin(self) -> BrokenConnection:
            return BrokenConnection()

    async def fail_close(_engine: object) -> None:
        raise RuntimeError("cleanup failure")

    monkeypatch.setattr(
        migrate_module,
        "create_database_engine",
        lambda _database_url: BrokenEngine(),
    )
    monkeypatch.setattr(migrate_module, "close_database", fail_close)

    exit_code = run_migrate(
        ["--database-url", "postgresql+asyncpg://user:secret@db.example/app", "current"]
    )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "could not connect to postgresql://***:***@db.example/app" in captured.err
    assert "cleanup failure" not in captured.err
    assert "secret" not in captured.err


def test_migrate_disposes_engine_on_same_event_loop(monkeypatch) -> None:
    observed: dict[str, int] = {}

    class RecordingConnection:
        async def __aenter__(self):
            observed["connection_loop"] = id(asyncio.get_running_loop())
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def run_sync(self, operation):
            return operation(object())

    class RecordingEngine:
        def begin(self) -> RecordingConnection:
            return RecordingConnection()

    async def close_on_current_loop(_engine: object) -> None:
        observed["close_loop"] = id(asyncio.get_running_loop())

    monkeypatch.setattr(
        migrate_module,
        "create_database_engine",
        lambda _database_url: RecordingEngine(),
    )
    monkeypatch.setattr(migrate_module, "close_database", close_on_current_loop)
    monkeypatch.setattr(
        migrate_module,
        "_migration_state_from_connection",
        lambda _connection: migrate_module.MigrationState(initialised=False),
    )

    exit_code = run_migrate(
        ["--database-url", "sqlite+aiosqlite:///:memory:", "current"]
    )

    assert exit_code == 0
    assert observed["connection_loop"] == observed["close_loop"]


def test_migrate_reports_runtime_configuration_errors_cleanly(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    def fail_history(_config) -> None:
        raise migrate_module.MigrationConfigurationError("missing database url")

    monkeypatch.setattr(migrate_module.command, "history", fail_history)

    exit_code = run_migrate(
        ["--database-url", sqlite_file_url(tmp_path / "database.sqlite3"), "history"]
    )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "configuration: failed" in captured.err
    assert "missing database url" in captured.err
    assert "Traceback" not in captured.err


def test_migrate_init_creates_sqlite_migration_state_without_schema(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "initialised.sqlite3"

    exit_code = run_migrate(["--database-url", sqlite_file_url(database_path), "init"])

    assert exit_code == 0
    assert database_path.is_file()

    with closing(sqlite3.connect(database_path)) as connection:
        table_names = {
            row[0]
            for row in connection.execute(
                "select name from sqlite_master where type = 'table'"
            )
        }

    assert "alembic_version" in table_names
    assert "identity_user" not in table_names


def test_migrate_init_stamps_empty_alembic_version_table(
    tmp_path: Path,
    monkeypatch,
) -> None:
    database_path = tmp_path / "empty-version.sqlite3"
    database_url = sqlite_file_url(database_path)
    observed: dict[str, object] = {}

    with closing(sqlite3.connect(database_path)) as connection, connection:
        connection.execute("CREATE TABLE alembic_version (version_num VARCHAR(32))")

    def record_stamp(config, revision: str) -> None:
        observed["revision"] = revision
        observed["connection_supplied"] = (
            config.attributes.get("connection") is not None
        )

    monkeypatch.setattr(migrate_module.command, "stamp", record_stamp)

    assert run_migrate(["--database-url", database_url, "init"]) == 0

    assert observed == {"revision": "base", "connection_supplied": True}


def test_migrate_upgrade_after_init_creates_sqlite_schema(tmp_path: Path) -> None:
    database_path = tmp_path / "upgraded.sqlite3"
    database_url = sqlite_file_url(database_path)

    assert run_migrate(["--database-url", database_url, "init"]) == 0
    assert run_migrate(["--database-url", database_url, "upgrade"]) == 0

    with closing(sqlite3.connect(database_path)) as connection:
        table_names = {
            row[0]
            for row in connection.execute(
                "select name from sqlite_master where type = 'table'"
            )
        }
        totp_columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(identity_totp_credential)")
        }
        recovery_columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(identity_totp_recovery_code)"
            )
        }

    assert "alembic_version" in table_names
    assert {
        "identity_user",
        "identity_provider",
        "identity_external_identity_link",
        "identity_access_token",
        "identity_authentication_challenge",
        "identity_totp_credential",
        "identity_totp_recovery_code",
    }.issubset(table_names)
    assert "crypt_secret" in totp_columns
    assert "code_verifier" in recovery_columns


def test_migrate_init_provisions_postgresql_before_stamping_base(
    tmp_path: Path,
    monkeypatch,
) -> None:
    database_url = "postgresql+asyncpg://app:secret@db.example/app"
    admin_database_url = "postgresql+asyncpg://admin:admin-secret@db.example/postgres"
    observed: dict[str, object] = {}
    events: list[str] = []

    def run_with_connection(_database_url: str, operation):
        events.append("connect")
        return operation(object())

    def provision(database_url_arg: str, admin_database_url_arg: str | None) -> None:
        events.append("provision")
        observed["provision"] = (database_url_arg, admin_database_url_arg)

    def stamp(config, revision: str) -> None:
        events.append("stamp")
        observed["stamp"] = (config.get_main_option("sqlalchemy.url"), revision)

    def fail_upgrade(_config, _revision: str) -> None:
        raise AssertionError("init must not run schema upgrades")

    monkeypatch.setattr(
        migrate_module, "_run_with_database_connection", run_with_connection
    )
    monkeypatch.setattr(
        migrate_module,
        "_migration_state_from_connection",
        lambda _connection: migrate_module.MigrationState(initialised=False),
    )
    monkeypatch.setattr(migrate_module, "provision_postgresql_database", provision)
    monkeypatch.setattr(migrate_module.command, "stamp", stamp)
    monkeypatch.setattr(migrate_module.command, "upgrade", fail_upgrade)

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
    assert events == ["provision", "connect", "stamp"]
    assert observed["provision"] == (database_url, admin_database_url)
    assert observed["stamp"] == (database_url, "base")


def test_migrate_init_reports_missing_postgresql_admin_url(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    database_url = "postgresql+asyncpg://app:secret@db.example/app"

    def fail_connection(_database_url: str, _operation):
        raise AssertionError("PostgreSQL init should provision before connecting")

    monkeypatch.delenv("SA_DATABASE_URL", raising=False)
    monkeypatch.setattr(
        migrate_module, "_run_with_database_connection", fail_connection
    )

    exit_code = run_migrate(["--database-url", database_url, "init"])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "provisioning: failed" in captured.err
    assert "--admin-database-url" in captured.err
    assert "SA_DATABASE_URL" in captured.err
    assert "secret" not in captured.err
    assert "Traceback" not in captured.err


def test_migrate_init_rejects_blank_admin_database_url_without_env_fallback(
    monkeypatch,
    capsys,
) -> None:
    database_url = "postgresql+asyncpg://app:secret@db.example/app"

    def fail_connection(_database_url: str, _operation):
        raise AssertionError("PostgreSQL init should provision before connecting")

    monkeypatch.setenv(
        "SA_DATABASE_URL",
        "postgresql+asyncpg://admin:env-secret@db.example/postgres",
    )
    monkeypatch.setattr(
        migrate_module, "_run_with_database_connection", fail_connection
    )

    exit_code = run_migrate(
        ["--database-url", database_url, "init", "--admin-database-url", ""]
    )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "--admin-database-url must not be blank" in captured.err
    assert "env-secret" not in captured.err


def test_migrate_init_redacts_non_postgresql_sqlalchemy_errors(
    monkeypatch,
    capsys,
) -> None:
    def fail_connection(_database_url: str, _operation):
        raise SQLAlchemyError(
            "sqlite unavailable after seeing postgresql://user:secret@db.example/app"
        )

    monkeypatch.setattr(
        migrate_module, "_run_with_database_connection", fail_connection
    )

    exit_code = run_migrate(["--database-url", "sqlite+aiosqlite:///:memory:", "init"])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "migration: failed" in captured.err
    assert "postgresql://***:***@db.example/app" in captured.err
    assert "secret" not in captured.err


def test_postgresql_provisioning_uses_dbscripts_with_sync_urls(
    monkeypatch,
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


def test_migrate_upgrade_rejects_uninitialised_sqlite_database(
    tmp_path: Path,
    capsys,
) -> None:
    database_path = tmp_path / "uninitialised.sqlite3"

    exit_code = run_migrate(
        ["--database-url", sqlite_file_url(database_path), "upgrade"]
    )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "migration: failed" in captured.err
    assert "Database is not initialised" in captured.err
    assert "uv run wybra-migrate init" in captured.err
    assert not database_path.exists()


def test_migrate_upgrade_preserves_subcommand_database_url_override(
    tmp_path: Path,
    monkeypatch,
) -> None:
    observed: list[tuple[str, str]] = []
    database_url = sqlite_file_url(tmp_path / "override.sqlite3")

    with (
        closing(sqlite3.connect(tmp_path / "override.sqlite3")) as connection,
        connection,
    ):
        connection.execute("CREATE TABLE alembic_version (version_num VARCHAR(32))")

    def record_upgrade(config, revision: str) -> None:
        assert config.attributes.get("connection") is not None
        observed.append((revision, config.get_main_option("sqlalchemy.url")))

    monkeypatch.setattr(migrate_module.command, "upgrade", record_upgrade)

    exit_code = run_migrate(
        [
            "upgrade",
            "--database-url",
            database_url,
        ]
    )

    assert exit_code == 0
    assert observed == [("heads", database_url)]


@pytest.mark.parametrize(
    ("argv", "command_name", "revision"),
    [
        (["downgrade", "base"], "downgrade", "base"),
        (["history"], "history", None),
    ],
)
def test_migrate_existing_subcommands_still_dispatch(
    tmp_path: Path,
    monkeypatch,
    argv: list[str],
    command_name: str,
    revision: str | None,
) -> None:
    observed: list[tuple[str | None, str]] = []

    def record_command(config, revision_arg: str | None = None) -> None:
        observed.append((revision_arg, config.get_main_option("sqlalchemy.url")))

    monkeypatch.setattr(migrate_module.command, command_name, record_command)

    exit_code = run_migrate(
        [
            "--database-url",
            sqlite_file_url(tmp_path / "database.sqlite3"),
            *argv,
        ]
    )

    assert exit_code == 0
    assert observed == [(revision, sqlite_file_url(tmp_path / "database.sqlite3"))]


def test_migrate_revision_passes_module_version_path_and_graph_options(
    tmp_path: Path,
    monkeypatch,
) -> None:
    base_module_path = create_importable_module(tmp_path, "base_app")
    base_version_path = base_module_path / "migrations" / "versions"
    base_version_path.mkdir(parents=True)
    create_importable_module(tmp_path, "revision_app")
    monkeypatch.syspath_prepend(str(tmp_path))
    observed: dict[str, object] = {}

    def record_revision(config, **kwargs) -> None:
        observed["path_separator"] = config.get_main_option("path_separator")
        observed["version_path_separator"] = config.get_main_option(
            "version_path_separator"
        )
        observed["version_locations"] = config.get_main_option("version_locations")
        observed["version_locations_list"] = config.get_version_locations_list()
        observed.update(kwargs)

    monkeypatch.setattr(migrate_module.command, "revision", record_revision)

    exit_code = run_migrate(
        [
            "--database-url",
            sqlite_file_url(tmp_path / "revision.sqlite3"),
            "revision",
            "--module",
            "revision_app",
            "-m",
            "add revision",
            "--autogenerate",
            "--head",
            "abc123",
            "--splice",
            "--branch-label",
            "revision_app",
            "--depends-on",
            "def456",
            "--rev-id",
            "999999999999",
        ],
        modules=("base_app", "revision_app"),
    )

    version_path = tmp_path / "revision_app" / "migrations" / "versions"
    assert exit_code == 0
    assert observed["message"] == "add revision"
    assert observed["autogenerate"] is True
    assert observed["head"] == "abc123"
    assert observed["splice"] is True
    assert observed["branch_label"] == "revision_app"
    assert observed["depends_on"] == "def456"
    assert observed["rev_id"] == "999999999999"
    assert observed["version_path"] == version_path
    assert observed["path_separator"] == "os"
    assert observed["version_path_separator"] is None
    assert version_path.as_posix() in str(observed["version_locations"])
    assert set(observed["version_locations_list"] or []) == {
        base_version_path.resolve().as_posix(),
        version_path.resolve().as_posix(),
    }
    assert version_path.is_dir()


def test_migrate_revision_rejects_unconfigured_module(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    module_path = create_importable_module(tmp_path, "unused_app")
    monkeypatch.syspath_prepend(str(tmp_path))

    exit_code = run_migrate(
        [
            "--database-url",
            sqlite_file_url(tmp_path / "revision.sqlite3"),
            "revision",
            "--module",
            "unused_app",
            "-m",
            "add revision",
        ],
        modules=("wybra.auth",),
    )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "configuration: failed" in captured.err
    assert "unused_app" in captured.err
    assert not (module_path / "migrations").exists()


def test_migrate_revision_requires_module_and_message() -> None:
    _config_path, command = create_migrate_command()

    try:
        runner = migrate_module.run_migrate_command
        missing_module = runner(command, ["revision", "-m", "add revision"])
        missing_message = runner(command, ["revision", "--module", "wybra.auth"])
    finally:
        _config_path.unlink(missing_ok=True)

    assert missing_module == 2
    assert missing_message == 2


def test_migrate_revision_help_describes_roll_forward_order(capsys) -> None:
    _config_path, command = create_migrate_command()

    try:
        exit_code = migrate_module.run_migrate_command(command, ["revision", "--help"])
    finally:
        _config_path.unlink(missing_ok=True)

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "Upgrade to the current head before autogenerate" in captured.out
    assert "Review" in captured.out
    assert "generated operations" in captured.out
    assert "down_revision" in captured.out
    assert "depends_on" in captured.out
