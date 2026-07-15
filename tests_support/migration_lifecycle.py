from __future__ import annotations

from pathlib import Path

from wybra.db import migrate as data_migrate
from wybra.tools import migrate as tools_migrate


async def initialise_migrations(config_path: Path) -> int:
    return await tools_migrate.run_migration(
        None,
        config_source=config_path.as_posix(),
        operation=lambda backend, context: data_migrate.initialise_migration_lifecycle(
            backend,
            context,
            app_labels=(),
        ),
        include_provisioning_connection=True,
    )


async def apply_migrations(config_path: Path) -> int:
    return await tools_migrate.run_migration(
        None,
        config_source=config_path.as_posix(),
        operation=lambda backend, context: backend.migrate(
            context,
            data_migrate.MigrationTargetRequest(
                app_label=None,
                migration=None,
                fake=False,
                dry_run=False,
            ),
        ),
        database_credential_purpose="service_account",
    )


async def list_maintenance_tasks(config_path: Path) -> int:
    return await tools_migrate.run_migration(
        None,
        config_source=config_path.as_posix(),
        operation=data_migrate.list_database_maintenance_tasks_lifecycle,
        resolve_database_credentials=False,
    )


async def run_maintenance_task(config_path: Path, task: str) -> int:
    return await tools_migrate.run_migration(
        None,
        config_source=config_path.as_posix(),
        operation=lambda _backend, context: (
            data_migrate.run_database_maintenance_lifecycle(
                context,
                data_migrate.DatabaseMaintenanceRequest(task=task, confirm=None),
            )
        ),
        include_provisioning_connection=True,
    )


async def destroy_database(config_path: Path, confirm: str) -> int:
    return await tools_migrate.run_migration(
        None,
        config_source=config_path.as_posix(),
        operation=lambda _backend, context: data_migrate.destroy_database_lifecycle(
            context,
            data_migrate.DestroyDatabaseRequest(confirm=confirm),
        ),
        include_provisioning_connection=True,
    )
