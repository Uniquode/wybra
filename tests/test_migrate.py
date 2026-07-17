import logging
import re
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from textwrap import dedent
from types import SimpleNamespace

import pytest

import wybra.db.migrate as migrate_module
import wybra.tools.migrate as tools_migrate
from support_database import sqlite_file_url
from wybra.core.composition import AppConfig, load_app_config
from wybra.db.surfaces import model_package_name
from wybra.db.tortoise import build_tortoise_config, tortoise_migrations_package


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


def sqlite_provisioning_context(
    database_url: str,
    *,
    project_root: Path,
) -> migrate_module.ProvisioningContext:
    backend = migrate_module.database_backend_for_url(database_url)
    assert backend is not None
    return migrate_module.provisioning_context(
        runtime_connection=migrate_module.ResolvedDatabaseConnection.from_url(
            database_url,
            backend=backend,
        ),
        provisioning_connection=None,
        project_root=project_root,
        modules=("wybra.sessions",),
    )


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


class TestMigrationCommands:
    def test_special_migration_marker_requires_non_empty_description(
        self,
        tmp_path: Path,
    ) -> None:
        migration_path = tmp_path / "0002_special.py"
        migration_path.write_text(
            dedent(
                """
            from tortoise import migrations


            class Migration(migrations.Migration):
                not_generated = True
                not_generated_description = "Carries an operational data repair."
                """
            ),
            encoding="utf-8",
        )

        assert (
            migrate_module.special_migration_description(migration_path)
            == "Carries an operational data repair."
        )

        migration_path.write_text(
            dedent(
                """
            from tortoise import migrations


            class Migration(migrations.Migration):
                not_generated = True
                not_generated_description = " "
                """
            ),
            encoding="utf-8",
        )

        with pytest.raises(
            migrate_module.MigrationStateError,
            match="not_generated_description",
        ):
            migrate_module.special_migration_description(migration_path)

    def test_special_migration_marker_supports_annotated_declarations(
        self,
        tmp_path: Path,
    ) -> None:
        migration_path = tmp_path / "0002_special.py"
        migration_path.write_text(
            dedent(
                """
                from tortoise import migrations


                class Migration(migrations.Migration):
                    not_generated: bool = True
                    not_generated_description: str = "Carries an operational repair."
                """
            ),
            encoding="utf-8",
        )

        assert (
            migrate_module.special_migration_description(migration_path)
            == "Carries an operational repair."
        )

    def test_wybra_migrate_main_delegates_to_async_entry_point(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        observed: dict[str, object] = {}

        async def run_migration(
            database_url: str | None,
            *,
            config_source: str | None,
            operation: object,
            **kwargs: object,
        ) -> int:
            observed.update(
                database_url=database_url,
                config_source=config_source,
                operation=operation,
                **kwargs,
            )
            return 0

        monkeypatch.setattr(tools_migrate, "run_migration", run_migration)

        assert tools_migrate.main(["heads"]) == 0
        assert observed["database_url"] is None
        assert observed["config_source"] is None
        assert callable(observed["operation"])

    def test_migrate_help_exposes_native_tortoise_commands(self, capsys) -> None:
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
            "tasks",
            "run",
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
        self,
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
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        argv: list[str],
        expected: list[str],
    ) -> None:
        database_url = sqlite_file_url(tmp_path / "database.sqlite3")
        observed: dict[str, object] = {}

        async def record_tortoise_cli(
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
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        database_url = "postgresql://app:secret@db.example/app"
        observed: dict[str, object] = {}

        async def record_tortoise_cli(
            context: migrate_module.MigrationContext,
            args: list[str],
        ) -> None:
            observed["args"] = args
            observed["config"] = context.config

        monkeypatch.setattr(
            migrate_module, "is_supported_database_url", lambda _url: True
        )
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
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        database_url = sqlite_file_url(tmp_path / "database.sqlite3")
        observed: list[str] = []

        async def record_initialise_database(
            _context: migrate_module.MigrationContext,
        ) -> None:
            observed.append("provision")

        async def record_tortoise_cli(
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

    @pytest.mark.anyio
    async def test_migrate_lifecycle_reports_provisioning_results(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        settings = MigrationTestSettings(
            database_url=sqlite_file_url(tmp_path / "database.sqlite3"),
            project_root=tmp_path,
        )
        context = migrate_module.build_migration_context(settings)

        async def record_initialise_database(
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

        await migrate_module.initialise_database_lifecycle(context)

        assert (
            "database lifecycle: sqlite init skipped: already provisioned"
            in caplog.text
        )

    @pytest.mark.anyio
    async def test_sqlite_provisioning_creates_file_target_and_parent_directory(
        self,
        tmp_path: Path,
    ) -> None:
        database_path = tmp_path / "nested" / "database.sqlite3"
        context = sqlite_provisioning_context(
            sqlite_file_url(database_path),
            project_root=tmp_path,
        )

        results = await migrate_module.initialise_database(context)

        assert database_path.is_file()
        assert [(result.status, result.phase) for result in results] == [
            ("created", "init")
        ]

    @pytest.mark.anyio
    async def test_sqlite_provisioning_is_idempotent_for_existing_file(
        self,
        tmp_path: Path,
    ) -> None:
        database_path = tmp_path / "database.sqlite3"
        database_path.write_text("", encoding="utf-8")
        context = sqlite_provisioning_context(
            sqlite_file_url(database_path),
            project_root=tmp_path,
        )

        results = await migrate_module.initialise_database(context)

        assert database_path.is_file()
        assert [(result.status, result.phase) for result in results] == [
            ("skipped", "init")
        ]

    @pytest.mark.anyio
    async def test_sqlite_provisioning_treats_concurrent_file_creation_as_skipped(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        database_path = tmp_path / "database.sqlite3"
        context = sqlite_provisioning_context(
            sqlite_file_url(database_path),
            project_root=tmp_path,
        )
        original_touch = Path.touch

        def create_file_then_raise_file_exists(
            path: Path,
            mode: int = 0o666,
            exist_ok: bool = True,
        ) -> None:
            if path.resolve() == database_path.resolve():
                original_touch(path, mode=mode, exist_ok=True)
                raise FileExistsError(path)
            original_touch(path, mode=mode, exist_ok=exist_ok)

        monkeypatch.setattr(Path, "touch", create_file_then_raise_file_exists)

        results = await migrate_module.initialise_database(context)

        assert database_path.is_file()
        assert [(result.status, result.phase) for result in results] == [
            ("skipped", "init")
        ]

    @pytest.mark.anyio
    async def test_sqlite_provisioning_accepts_in_memory_without_credentials(
        self,
        tmp_path: Path,
    ) -> None:
        context = sqlite_provisioning_context(
            "sqlite://:memory:", project_root=tmp_path
        )

        results = await migrate_module.initialise_database(context)

        assert [(result.status, result.phase) for result in results] == [
            ("noop", "init")
        ]
        assert list(tmp_path.iterdir()) == []

    @pytest.mark.anyio
    async def test_sqlite_destroy_removes_confirmed_file_and_sidecars(
        self,
        tmp_path: Path,
    ) -> None:
        database_path = tmp_path / "database.sqlite3"
        database_path.write_text("", encoding="utf-8")
        sidecars = [
            database_path.with_name(f"{database_path.name}-wal"),
            database_path.with_name(f"{database_path.name}-shm"),
            database_path.with_name(f"{database_path.name}-journal"),
        ]
        for sidecar in sidecars:
            sidecar.write_text("", encoding="utf-8")
        context = sqlite_provisioning_context(
            sqlite_file_url(database_path),
            project_root=tmp_path,
        )

        results = await migrate_module.destroy_database(
            context,
            migrate_module.DestroyDatabaseRequest(confirm=database_path.name),
        )

        assert [(result.status, result.phase) for result in results] == [
            ("removed", "destroy")
        ]
        assert not database_path.exists()
        assert all(not sidecar.exists() for sidecar in sidecars)

    @pytest.mark.anyio
    async def test_sqlite_destroy_removes_sidecars_when_main_file_is_absent(
        self,
        tmp_path: Path,
    ) -> None:
        database_path = tmp_path / "database.sqlite3"
        sidecar = database_path.with_name(f"{database_path.name}-wal")
        sidecar.write_text("", encoding="utf-8")
        context = sqlite_provisioning_context(
            sqlite_file_url(database_path),
            project_root=tmp_path,
        )

        results = await migrate_module.destroy_database(
            context,
            migrate_module.DestroyDatabaseRequest(confirm=database_path.name),
        )

        assert [(result.status, result.phase) for result in results] == [
            ("removed", "destroy")
        ]
        assert not database_path.exists()
        assert not sidecar.exists()

    @pytest.mark.anyio
    async def test_sqlite_destroy_reports_already_absent_file_as_skipped(
        self,
        tmp_path: Path,
    ) -> None:
        database_path = tmp_path / "database.sqlite3"
        context = sqlite_provisioning_context(
            sqlite_file_url(database_path),
            project_root=tmp_path,
        )

        results = await migrate_module.destroy_database(
            context,
            migrate_module.DestroyDatabaseRequest(confirm=database_path.name),
        )

        assert [(result.status, result.phase) for result in results] == [
            ("skipped", "destroy")
        ]

    @pytest.mark.anyio
    async def test_sqlite_destroy_refuses_confirmation_mismatch(
        self,
        tmp_path: Path,
    ) -> None:
        database_path = tmp_path / "database.sqlite3"
        database_path.write_text("", encoding="utf-8")
        context = sqlite_provisioning_context(
            sqlite_file_url(database_path),
            project_root=tmp_path,
        )

        with pytest.raises(
            migrate_module.DatabaseProvisioningConfigurationError,
            match="confirmation does not match",
        ):
            await migrate_module.destroy_database(
                context,
                migrate_module.DestroyDatabaseRequest(confirm="other.sqlite3"),
            )

        assert database_path.exists()

    @pytest.mark.anyio
    async def test_sqlite_destroy_refuses_directory_target(
        self, tmp_path: Path
    ) -> None:
        database_path = tmp_path / "database.sqlite3"
        database_path.mkdir()
        context = sqlite_provisioning_context(
            sqlite_file_url(database_path),
            project_root=tmp_path,
        )

        with pytest.raises(
            migrate_module.DatabaseProvisioningConfigurationError,
            match="target is a directory",
        ):
            await migrate_module.destroy_database(
                context,
                migrate_module.DestroyDatabaseRequest(confirm=database_path.name),
            )

        assert database_path.is_dir()

    @pytest.mark.anyio
    async def test_sqlite_in_memory_destroy_is_noop(self, tmp_path: Path) -> None:
        context = sqlite_provisioning_context(
            "sqlite://:memory:", project_root=tmp_path
        )

        results = await migrate_module.destroy_database(
            context,
            migrate_module.DestroyDatabaseRequest(confirm=":memory:"),
        )

        assert [(result.status, result.phase) for result in results] == [
            ("noop", "destroy")
        ]

    def test_migrate_destroy_removes_sqlite_file_target(self, tmp_path: Path) -> None:
        database_path = tmp_path / "database.sqlite3"
        database_path.write_text("", encoding="utf-8")

        exit_code = run_migrate(
            [
                "--database-url",
                sqlite_file_url(database_path),
                "destroy",
                "--confirm",
                database_path.name,
            ]
        )

        assert exit_code == 0
        assert not database_path.exists()

    @pytest.mark.parametrize(
        "argv",
        [
            ["heads"],
            ["migrate"],
            ["migrate", "run", "vacuum"],
            ["downgrade"],
            ["history"],
        ],
    )
    def test_ordinary_migration_commands_do_not_run_provisioning(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        argv: list[str],
    ) -> None:
        database_url = sqlite_file_url(tmp_path / "database.sqlite3")

        async def fail_lifecycle(*_args, **_kwargs):
            raise AssertionError("database lifecycle should not run")

        async def record_tortoise_cli(
            _context: migrate_module.MigrationContext,
            _args: list[str],
        ) -> None:
            return None

        monkeypatch.setattr(
            migrate_module, "initialise_database_lifecycle", fail_lifecycle
        )
        monkeypatch.setattr(
            migrate_module, "destroy_database_lifecycle", fail_lifecycle
        )
        monkeypatch.setattr(
            migrate_module,
            "run_database_maintenance_lifecycle",
            fail_lifecycle,
        )
        monkeypatch.setattr(migrate_module, "_run_tortoise_cli", record_tortoise_cli)

        exit_code = run_migrate(["--database-url", database_url, *argv])

        assert exit_code == 0

    def test_migrate_destroy_requires_confirmation(self, tmp_path: Path) -> None:
        database_url = sqlite_file_url(tmp_path / "database.sqlite3")

        exit_code = run_migrate(["--database-url", database_url, "destroy"])

        assert exit_code == 2

    def test_migrate_destroy_dispatches_database_lifecycle(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        database_url = sqlite_file_url(tmp_path / "database.sqlite3")
        observed: dict[str, object] = {}

        async def record_destroy(
            context: migrate_module.MigrationContext,
            request: migrate_module.DestroyDatabaseRequest,
        ) -> None:
            observed["confirm"] = request.confirm
            observed["database"] = context.database_connection.redacted_description

        monkeypatch.setattr(
            migrate_module, "destroy_database_lifecycle", record_destroy
        )

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

    def test_migrate_destroy_keeps_runtime_credentials_for_role_cleanup(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        observed: dict[str, object] = {}

        def load_settings(
            database_url: str | None,
            *,
            database_credential_purpose="runtime",
            **_kwargs: object,
        ) -> MigrationTestSettings:
            observed["database_credential_purpose"] = database_credential_purpose
            return MigrationTestSettings(
                database_url=database_url or "sqlite://:memory:"
            )

        async def record_destroy(
            _context: migrate_module.MigrationContext,
            _request: migrate_module.DestroyDatabaseRequest,
        ) -> None:
            return None

        command = migrate_module.create_migrate_command(load_settings)
        monkeypatch.setattr(
            migrate_module, "destroy_database_lifecycle", record_destroy
        )

        assert (
            migrate_module.run_migrate_command(
                command,
                [
                    "--database-url",
                    "sqlite://:memory:",
                    "destroy",
                    "--confirm",
                    ":memory:",
                ],
            )
            == 0
        )
        assert observed["database_credential_purpose"] == "runtime"

    def test_migrate_tasks_lists_database_maintenance_tasks(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        database_url = sqlite_file_url(tmp_path / "database.sqlite3")

        def available_tasks(
            _context: migrate_module.ProvisioningContext,
        ) -> tuple[migrate_module.DatabaseMaintenanceTask, ...]:
            return (
                migrate_module.DatabaseMaintenanceTask(
                    name="repair-privs",
                    description="Reapply runtime privileges.",
                    recommended_frequency="after migrations",
                ),
            )

        monkeypatch.setattr(
            migrate_module, "database_maintenance_tasks", available_tasks
        )

        exit_code = run_migrate(["--database-url", database_url, "tasks"])

        captured = capsys.readouterr()
        assert exit_code == 0
        assert "repair-privs: Reapply runtime privileges." in captured.out
        assert "credentials: service_account" in captured.out
        assert "recommended: after migrations" in captured.out

    def test_migrate_tasks_does_not_resolve_database_credentials(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        database_url = "postgresql://app:secret@db.example/app"
        observed: dict[str, object] = {}

        def load_settings(
            database_url: str | None,
            *,
            database_credential_purpose="runtime",
            fallback_to_runtime_credentials=False,
            include_provisioning_connection=False,
            resolve_database_credentials=True,
        ) -> MigrationTestSettings:
            observed["database_url"] = database_url
            observed["database_credential_purpose"] = database_credential_purpose
            observed["fallback_to_runtime_credentials"] = (
                fallback_to_runtime_credentials
            )
            observed["include_provisioning_connection"] = (
                include_provisioning_connection
            )
            observed["resolve_database_credentials"] = resolve_database_credentials
            return MigrationTestSettings(
                database_url=database_url or "sqlite://:memory:"
            )

        def available_tasks(
            _context: migrate_module.ProvisioningContext,
        ) -> tuple[migrate_module.DatabaseMaintenanceTask, ...]:
            return (
                migrate_module.DatabaseMaintenanceTask(
                    name="migrations",
                    description="Report migration state.",
                ),
            )

        command = migrate_module.create_migrate_command(load_settings)
        monkeypatch.setattr(
            migrate_module, "is_supported_database_url", lambda _url: True
        )
        monkeypatch.setattr(
            migrate_module, "database_maintenance_tasks", available_tasks
        )

        exit_code = migrate_module.run_migrate_command(
            command,
            ["--database-url", database_url, "tasks"],
        )

        captured = capsys.readouterr()
        assert exit_code == 0
        assert "migrations: Report migration state." in captured.out
        assert observed["database_url"] == database_url
        assert observed["database_credential_purpose"] == "runtime"
        assert observed["fallback_to_runtime_credentials"] is False
        assert observed["include_provisioning_connection"] is False
        assert observed["resolve_database_credentials"] is False

    def test_migrate_run_dispatches_database_maintenance(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        database_url = sqlite_file_url(tmp_path / "database.sqlite3")
        observed: dict[str, object] = {}

        async def record_maintenance(
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
                "run",
                "vacuum",
            ]
        )

        assert exit_code == 0
        assert observed == {
            "task": "vacuum",
            "database": "database URL: sqlite://<redacted>",
        }

    def test_migrate_run_passes_maintenance_confirmation(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        database_url = sqlite_file_url(tmp_path / "database.sqlite3")
        observed: dict[str, object] = {}

        async def record_maintenance(
            _context: migrate_module.MigrationContext,
            request: migrate_module.DatabaseMaintenanceRequest,
        ) -> None:
            observed["task"] = request.task
            observed["confirm"] = request.confirm

        monkeypatch.setattr(
            migrate_module,
            "run_database_maintenance_lifecycle",
            record_maintenance,
        )

        exit_code = run_migrate(
            [
                "--database-url",
                database_url,
                "run",
                "--confirm",
                "repair-privs",
                "repair-privs",
            ]
        )

        assert exit_code == 0
        assert observed == {"task": "repair-privs", "confirm": "repair-privs"}

    def test_migrate_rejects_unknown_options(
        self,
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

    def test_migrate_run_rejects_migration_flags(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        database_url = sqlite_file_url(tmp_path / "database.sqlite3")

        exit_code = run_migrate(
            [
                "--database-url",
                database_url,
                "run",
                "--fake",
                "vacuum",
            ]
        )

        captured = capsys.readouterr()
        assert exit_code == 2
        assert "No such option" in captured.err
        assert "--fake" in captured.err

    def test_migrate_run_requests_provisioning_connection(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        database_url = "postgresql://app:secret@db.example/app"
        observed: dict[str, object] = {}

        def load_settings(
            database_url: str | None,
            *,
            database_credential_purpose="runtime",
            fallback_to_runtime_credentials=False,
            include_provisioning_connection=False,
            resolve_database_credentials=True,
        ) -> MigrationTestSettings:
            observed["database_url"] = database_url
            observed["database_credential_purpose"] = database_credential_purpose
            observed["fallback_to_runtime_credentials"] = (
                fallback_to_runtime_credentials
            )
            observed["include_provisioning_connection"] = (
                include_provisioning_connection
            )
            observed["resolve_database_credentials"] = resolve_database_credentials
            return MigrationTestSettings(
                database_url=database_url or "sqlite://:memory:"
            )

        async def record_maintenance(
            _context: migrate_module.MigrationContext,
            _request: migrate_module.DatabaseMaintenanceRequest,
        ) -> None:
            return None

        command = migrate_module.create_migrate_command(load_settings)
        monkeypatch.setattr(
            migrate_module, "is_supported_database_url", lambda _url: True
        )
        monkeypatch.setattr(
            migrate_module,
            "run_database_maintenance_lifecycle",
            record_maintenance,
        )

        exit_code = migrate_module.run_migrate_command(
            command,
            ["--database-url", database_url, "run", "repair-privs"],
        )

        assert exit_code == 0
        assert observed["database_url"] == database_url
        assert observed["database_credential_purpose"] == "runtime"
        assert observed["fallback_to_runtime_credentials"] is False
        assert observed["include_provisioning_connection"] is True
        assert observed["resolve_database_credentials"] is True

    def test_migrate_run_rejects_blank_task(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        database_url = sqlite_file_url(tmp_path / "database.sqlite3")

        exit_code = run_migrate(
            [
                "--database-url",
                database_url,
                "run",
                " ",
            ]
        )

        captured = capsys.readouterr()
        assert exit_code == 2
        assert "run TASK must not be blank" in captured.err

    def test_supported_loader_kwargs_logs_when_introspection_fails(
        self,
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
        assert (
            "migration settings loader signature could not be inspected" in caplog.text
        )

    def test_tortoise_config_uses_configured_module_model_surfaces(
        self,
        tmp_path: Path,
    ) -> None:
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

    def test_tortoise_config_routes_migrations_to_configured_root(
        self,
        tmp_path: Path,
    ) -> None:
        migrations_root = tmp_path / "generated-migrations"
        settings = MigrationTestSettings(
            database_url="sqlite:///app.sqlite3",
            project_root=tmp_path,
            migrations_root=migrations_root,
            modules=("wybra.messages",),
        )

        config = migrate_module.build_tortoise_config(settings)

        migrations_package = tortoise_migrations_package(migrations_root)
        assert config["apps"] == {
            "wybra_sessions": {
                "models": [model_package_name("wybra.sessions")],
                "migrations": f"{migrations_package}.wybra_sessions",
                "default_connection": "default",
            },
            "wybra_messages": {
                "models": [model_package_name("wybra.messages")],
                "migrations": f"{migrations_package}.wybra_messages",
                "default_connection": "default",
            },
        }

    def test_temporary_migrations_root_is_importable_only_during_command(
        self,
        tmp_path: Path,
    ) -> None:
        migrations_root = tmp_path / "generated-migrations"
        package_name = tortoise_migrations_package(migrations_root)

        with migrate_module._temporary_migrations_package(migrations_root):
            package = sys.modules[package_name]
            assert package.__path__ == [str(migrations_root)]

        assert package_name not in sys.modules

    def test_migration_context_overrides_model_surface_and_root(
        self,
        tmp_path: Path,
    ) -> None:
        settings = MigrationTestSettings(
            database_url="sqlite:///app.sqlite3",
            project_root=tmp_path,
        )
        context = migrate_module.build_migration_context(settings)
        migrations_root = tmp_path / "generated-migrations"

        overridden = migrate_module._migration_context_with_overrides(
            context,
            model_modules=("wybra.messages.models",),
            migrations_root=migrations_root,
        )

        package = tortoise_migrations_package(migrations_root)
        assert overridden.config["apps"] == {
            "wybra_sessions": {
                "models": [model_package_name("wybra.sessions")],
                "migrations": f"{package}.wybra_sessions",
                "default_connection": "default",
            },
            "wybra_messages": {
                "models": [model_package_name("wybra.messages")],
                "migrations": f"{package}.wybra_messages",
                "default_connection": "default",
            },
        }

    @pytest.mark.anyio
    async def test_makemigrations_writes_selected_models_to_temporary_root(
        self,
        tmp_path: Path,
    ) -> None:
        settings = MigrationTestSettings(
            database_url=sqlite_file_url(tmp_path / "database.sqlite3"),
            project_root=tmp_path,
        )
        context = migrate_module._migration_context_with_overrides(
            migrate_module.build_migration_context(settings),
            model_modules=("tests_support.form_binding.models",),
            migrations_root=tmp_path / "generated-migrations",
        )

        await migrate_module.TortoiseMigrationBackend().makemigrations(
            context,
            migrate_module.MakeMigrationsRequest(
                app_labels=(),
                empty=False,
                name=None,
            ),
        )

        generated_files = tuple(
            (tmp_path / "generated-migrations").glob("*/[0-9][0-9][0-9][0-9]_*.py")
        )
        assert generated_files
        form_binding_migration = next(
            path
            for path in generated_files
            if path.parent.name == "tests_support_form_binding"
        )
        generated_source = form_binding_migration.read_text(encoding="utf-8")
        assert "from wybra.db.versioning import VersionField" in generated_source
        assert "('version', VersionField(default=0))" in generated_source
        assert "test_form_versioned_record_version_non_negative" in generated_source
        assert "version >= 0" in generated_source

    @pytest.mark.anyio
    async def test_model_migration_plan_uses_relations_not_configured_order(
        self,
    ) -> None:
        config = build_tortoise_config(
            database_url="sqlite://:memory:",
            modules=("wybra.auth", "wybra.media", "wybra.profile"),
        )
        apps = config["apps"]
        assert isinstance(apps, dict)
        config["apps"] = {
            "wybra_profile": apps["wybra_profile"],
            "wybra_media": apps["wybra_media"],
            "wybra_auth": apps["wybra_auth"],
            "wybra_sessions": apps["wybra_sessions"],
        }

        plan = await migrate_module.model_migration_plan(config)

        assert plan.app_labels.index("wybra_auth") < plan.app_labels.index(
            "wybra_profile"
        )
        assert plan.app_labels.index("wybra_media") < plan.app_labels.index(
            "wybra_profile"
        )
        assert plan.dependencies_for("wybra_profile") == (
            "wybra_auth",
            "wybra_media",
        )

    @pytest.mark.anyio
    async def test_generated_profile_initial_migration_has_model_dependencies(
        self,
    ) -> None:
        config = build_tortoise_config(
            database_url="sqlite://:memory:",
            modules=("wybra.auth", "wybra.media", "wybra.profile"),
        )

        async with migrate_module.generated_temporary_migrations(config) as generated:
            profile_migration = next(
                path for path in generated.paths if path.parent.name == "wybra_profile"
            )
            dependencies = set(migrate_module.migration_dependencies(profile_migration))

        assert dependencies >= {
            ("wybra_auth", "0001_initial"),
            ("wybra_media", "0001_initial"),
        }

    @pytest.mark.anyio
    async def test_temporary_generation_redirects_committed_apps_before_detection(
        self,
    ) -> None:
        config = build_tortoise_config(
            database_url="sqlite://:memory:",
            modules=("wybra.sessions", "tests_support.form_binding"),
        )
        apps = config["apps"]
        assert isinstance(apps, dict)
        committed_sessions_migrations = apps["wybra_sessions"]["migrations"]

        async with migrate_module.generated_temporary_migrations(
            config,
            app_labels=("tests_support_form_binding",),
        ) as generated:
            generated_apps = generated.config["apps"]
            assert isinstance(generated_apps, dict)
            assert generated_apps["wybra_sessions"]["migrations"] == (
                committed_sessions_migrations
            )
            assert any(path.parent.name == "wybra_sessions" for path in generated.paths)
            assert any(
                path.parent.name == "tests_support_form_binding"
                for path in generated.paths
            )

    @pytest.mark.anyio
    async def test_temporary_relation_targets_retained_migration_leaf(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        package = tmp_path / "committed_sessions" / "migrations"
        package.mkdir(parents=True)
        for path in (package.parent / "__init__.py", package / "__init__.py"):
            path.write_text("", encoding="utf-8")
        (package / "0001_initial.py").write_text(
            dedent(
                """\
                from tortoise import migrations


                class Migration(migrations.Migration):
                    initial = True
                    dependencies = []
                    operations = []
                """
            ),
            encoding="utf-8",
        )
        (package / "0002_add_name.py").write_text(
            dedent(
                """\
                from tortoise import migrations


                class Migration(migrations.Migration):
                    dependencies = [("wybra_sessions", "0001_initial")]
                    operations = []
                """
            ),
            encoding="utf-8",
        )
        monkeypatch.syspath_prepend(str(tmp_path))
        config = build_tortoise_config(
            database_url="sqlite://:memory:",
            modules=("wybra.sessions", "tests_support.migration_dependency"),
        )
        apps = config["apps"]
        assert isinstance(apps, dict)
        apps["wybra_sessions"]["migrations"] = "committed_sessions.migrations"

        async with migrate_module.generated_temporary_migrations(
            config,
            app_labels=("tests_support_migration_dependency",),
        ) as generated:
            migration = next(
                path
                for path in generated.paths
                if path.parent.name == "tests_support_migration_dependency"
            )
            dependencies = set(migrate_module.migration_dependencies(migration))

        assert ("wybra_sessions", "0002_add_name") in dependencies
        assert ("wybra_sessions", "0001_initial") not in dependencies

    def test_model_migration_plan_rejects_cross_app_cycle(self) -> None:
        with pytest.raises(migrate_module.MigrationStateError, match="cycle"):
            migrate_module._topological_migration_app_order(
                ("app_a", "app_b"),
                {"app_a": {"app_b"}, "app_b": {"app_a"}},
            )

    @pytest.mark.anyio
    async def test_generated_temporary_migrations_are_removed_after_failure(
        self,
        tmp_path: Path,
    ) -> None:
        settings = MigrationTestSettings(
            database_url=sqlite_file_url(tmp_path / "database.sqlite3"),
            project_root=tmp_path,
            modules=("tests_support.form_binding",),
        )
        root: Path | None = None

        with pytest.raises(RuntimeError, match="test failure"):
            async with migrate_module.generated_temporary_migrations(
                migrate_module.build_tortoise_config(settings)
            ) as generated:
                root = generated.root
                assert generated.paths
                raise RuntimeError("test failure")

        assert root is not None
        assert not root.exists()

    @pytest.mark.anyio
    async def test_generated_temporary_migrations_are_removed_after_success(
        self,
        tmp_path: Path,
    ) -> None:
        settings = MigrationTestSettings(
            database_url=sqlite_file_url(tmp_path / "database.sqlite3"),
            project_root=tmp_path,
            modules=("tests_support.form_binding",),
        )

        async with migrate_module.generated_temporary_migrations(
            migrate_module.build_tortoise_config(settings)
        ) as generated:
            root = generated.root
            assert generated.paths

        assert not root.exists()

    @pytest.mark.anyio
    async def test_reset_plan_rejects_unplanned_special_migration(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        migrations = tmp_path / "migrations"
        migrations.mkdir()
        special = migrations / "0002_special.py"
        special.write_text(
            dedent(
                """
                from tortoise import migrations


                class Migration(migrations.Migration):
                    not_generated = True
                    not_generated_description = "Backfill historical values."
                """
            ),
            encoding="utf-8",
        )
        monkeypatch.setattr(
            migrate_module,
            "migration_version_locations_from_modules",
            lambda _modules: (migrations,),
        )
        context = migrate_module.build_migration_context(
            MigrationTestSettings(
                database_url="sqlite://:memory:",
                project_root=tmp_path,
            )
        )

        with pytest.raises(migrate_module.MigrationStateError, match="provenance"):
            await migrate_module.plan_migration_reset(context)

    @pytest.mark.anyio
    async def test_reset_migrations_destroys_database_before_removing_history(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        migrations = tmp_path / "migrations"
        migrations.mkdir()
        initialiser = migrations / "__init__.py"
        initialiser.write_text("", encoding="utf-8")
        generated = migrations / "0001_initial.py"
        generated.write_text("generated", encoding="utf-8")
        plan = migrate_module.MigrationResetPlan(
            generated_baseline=(tmp_path / "baseline.py",),
            migration_locations=(migrations,),
        )
        events: list[str] = []

        async def plan_reset(
            _context: migrate_module.MigrationContext,
        ) -> migrate_module.MigrationResetPlan:
            return plan

        async def destroy(
            _context: migrate_module.MigrationContext,
            _request: migrate_module.DestroyDatabaseRequest,
        ) -> None:
            events.append("destroy")
            assert generated.exists()

        monkeypatch.setattr(migrate_module, "plan_migration_reset", plan_reset)
        monkeypatch.setattr(migrate_module, "destroy_database_lifecycle", destroy)
        context = migrate_module.build_migration_context(
            MigrationTestSettings(
                database_url="sqlite://:memory:",
                project_root=tmp_path,
            )
        )

        assert (
            await migrate_module.reset_migrations_lifecycle(
                context,
                confirm="database",
            )
            == plan
        )
        assert events == ["destroy"]
        assert initialiser.exists()
        assert not generated.exists()

    @pytest.mark.anyio
    async def test_run_tortoise_cli_uses_transient_config_module_and_removes_it(
        self,
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

        await migrate_module._run_tortoise_cli(context, ["heads"])

        assert observed["argv"] == [
            "--config",
            f"{observed['config_module']}.{migrate_module.TORTOISE_CONFIG_VARIABLE}",
            "heads",
        ]
        assert observed["config_variable"] == migrate_module.TORTOISE_CONFIG_VARIABLE
        assert observed["config"] == context.config
        assert observed["config_module"] not in sys.modules

    @pytest.mark.anyio
    async def test_run_tortoise_cli_reports_non_zero_exit(
        self,
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
            await migrate_module._run_tortoise_cli(context, ["heads"])

    @pytest.mark.anyio
    async def test_tortoise_migration_recorder_uses_mariadb_safe_datetime_literals(
        self,
    ) -> None:
        class RecordingConnection:
            def __init__(self) -> None:
                self.capabilities = SimpleNamespace(dialect="mysql")
                self.query: str | None = None
                self.values: list[object] | None = None

            async def execute_query(
                self, query: str, values: list[object] | None = None
            ) -> None:
                self.query = query
                self.values = values

        original_record_applied = migrate_module.MigrationRecorder.record_applied
        connection = RecordingConnection()

        with migrate_module._tortoise_migration_recorder_compatibility():
            recorder = migrate_module.MigrationRecorder(connection)
            await recorder.record_applied("wybra_sessions", "0001_initial")

        assert (
            migrate_module.MigrationRecorder.record_applied is original_record_applied
        )
        assert connection.query is not None
        assert connection.values is not None
        assert connection.query.endswith("VALUES (%s, %s, %s)")
        assert connection.values[:2] == ["wybra_sessions", "0001_initial"]
        applied_at = connection.values[2]
        assert isinstance(applied_at, str)
        assert "+00:00" not in applied_at
        assert re.fullmatch(
            r"[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]+",
            applied_at,
        )

    def test_tortoise_migration_recorder_uses_current_primary_key_field_name(
        self,
    ) -> None:
        class RecordingConnection:
            capabilities = SimpleNamespace(dialect="postgres")

        with migrate_module._tortoise_migration_recorder_compatibility():
            recorder = migrate_module.MigrationRecorder(RecordingConnection())

        assert recorder.model._meta.pk_attr == "id"

    def test_load_migration_settings_passes_keyword_only_config_source(
        self,
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
            observed["include_provisioning_connection"] = (
                include_provisioning_connection
            )
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
        self,
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
                "database_credential_purpose": "runtime",
                "fallback_to_runtime_credentials": False,
                "include_provisioning_connection": False,
                "resolve_database_credentials": True,
            },
        }

    def test_load_migration_settings_uses_legacy_loader_without_config_source(
        self,
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
        self,
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
        self,
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
            resolve_database_credentials=True,
        ):
            observed["app_config"] = (
                None if environ is None else environ.get("APP_CONFIG")
            )
            observed["database_credential_purpose"] = database_credential_purpose
            observed["fallback_to_runtime_credentials"] = (
                fallback_to_runtime_credentials
            )
            observed["include_provisioning_connection"] = (
                include_provisioning_connection
            )
            observed["resolve_database_credentials"] = resolve_database_credentials
            return MigrationTestSettings(
                database_url="sqlite://:memory:",
                project_root=tmp_path,
            )

        async def record_tortoise_cli(
            _context: migrate_module.MigrationContext,
            _args: list[str],
        ) -> None:
            return None

        monkeypatch.setattr(tools_migrate, "runtime_project_root", lambda: tmp_path)
        monkeypatch.setattr(
            tools_migrate, "load_project_settings", load_project_settings
        )
        monkeypatch.setattr(
            tools_migrate.data_migrate,
            "_run_tortoise_cli",
            record_tortoise_cli,
        )

        exit_code = tools_migrate.main(
            ["--config", selected_config.as_posix(), "heads"]
        )

        assert exit_code == 0
        assert observed["app_config"] == selected_config.as_posix()
        assert observed["database_credential_purpose"] == "runtime"
        assert observed["fallback_to_runtime_credentials"] is False
        assert observed["include_provisioning_connection"] is False
        assert observed["resolve_database_credentials"] is True

    def test_wybra_migrate_init_requests_provisioning_connection(
        self,
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
            resolve_database_credentials=True,
        ):
            observed["app_config"] = (
                None if environ is None else environ.get("APP_CONFIG")
            )
            observed["database_credential_purpose"] = database_credential_purpose
            observed["fallback_to_runtime_credentials"] = (
                fallback_to_runtime_credentials
            )
            observed["include_provisioning_connection"] = (
                include_provisioning_connection
            )
            observed["resolve_database_credentials"] = resolve_database_credentials
            return MigrationTestSettings(
                database_url="sqlite://:memory:",
                project_root=tmp_path,
            )

        async def record_lifecycle(_context: migrate_module.MigrationContext) -> None:
            return None

        async def record_tortoise_cli(
            _context: migrate_module.MigrationContext,
            _args: list[str],
        ) -> None:
            return None

        monkeypatch.setattr(tools_migrate, "runtime_project_root", lambda: tmp_path)
        monkeypatch.setattr(
            tools_migrate, "load_project_settings", load_project_settings
        )
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
        assert observed["resolve_database_credentials"] is True

    def test_wybra_migrate_migrate_uses_service_account_connection(
        self,
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
            resolve_database_credentials=True,
        ):
            observed["app_config"] = (
                None if environ is None else environ.get("APP_CONFIG")
            )
            observed["database_credential_purpose"] = database_credential_purpose
            observed["fallback_to_runtime_credentials"] = (
                fallback_to_runtime_credentials
            )
            observed["include_provisioning_connection"] = (
                include_provisioning_connection
            )
            observed["resolve_database_credentials"] = resolve_database_credentials
            return MigrationTestSettings(
                database_url="sqlite://:memory:",
                project_root=tmp_path,
            )

        async def record_tortoise_cli(
            _context: migrate_module.MigrationContext,
            _args: list[str],
        ) -> None:
            return None

        monkeypatch.setattr(tools_migrate, "runtime_project_root", lambda: tmp_path)
        monkeypatch.setattr(
            tools_migrate, "load_project_settings", load_project_settings
        )
        monkeypatch.setattr(
            tools_migrate.data_migrate,
            "_run_tortoise_cli",
            record_tortoise_cli,
        )

        exit_code = tools_migrate.main(
            ["--config", selected_config.as_posix(), "migrate"]
        )

        assert exit_code == 0
        assert observed["app_config"] == selected_config.as_posix()
        assert observed["database_credential_purpose"] == "service_account"
        assert observed["fallback_to_runtime_credentials"] is False
        assert observed["include_provisioning_connection"] is False
        assert observed["resolve_database_credentials"] is True

    def test_wybra_migrate_migrate_url_does_not_request_provisioning_connection(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        database_url = "postgresql://app:secret@db.example/app"
        observed: dict[str, object] = {}

        def load_settings(
            database_url: str | None,
            *,
            database_credential_purpose="runtime",
            fallback_to_runtime_credentials=False,
            include_provisioning_connection=False,
        ) -> MigrationTestSettings:
            observed["database_url"] = database_url
            observed["database_credential_purpose"] = database_credential_purpose
            observed["fallback_to_runtime_credentials"] = (
                fallback_to_runtime_credentials
            )
            observed["include_provisioning_connection"] = (
                include_provisioning_connection
            )
            return MigrationTestSettings(
                database_url=database_url or "sqlite://:memory:"
            )

        async def record_tortoise_cli(
            _context: migrate_module.MigrationContext,
            _args: list[str],
        ) -> None:
            return None

        command = migrate_module.create_migrate_command(load_settings)
        monkeypatch.setattr(
            migrate_module, "is_supported_database_url", lambda _url: True
        )
        monkeypatch.setattr(migrate_module, "_run_tortoise_cli", record_tortoise_cli)

        exit_code = migrate_module.run_migrate_command(
            command,
            ["--database-url", database_url, "migrate"],
        )

        assert exit_code == 0
        assert observed["database_url"] == database_url
        assert observed["database_credential_purpose"] == "service_account"
        assert observed["fallback_to_runtime_credentials"] is False
        assert observed["include_provisioning_connection"] is False
