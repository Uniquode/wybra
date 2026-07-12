from __future__ import annotations

from wybra.db.provisioning.core import (
    DatabaseFamily,
    DatabaseMaintenanceRequest,
    DatabaseMaintenanceTask,
    DatabaseProvisioningConfigurationError,
    DatabaseProvisioningOperationError,
    DestroyDatabaseRequest,
    ProvisioningContext,
    ProvisioningPhaseResult,
    _ensure_family,
    _require_service_account_connection,
)
from wybra.db.sql import quote_sql_identifier


class UnsupportedFamilyProvisioner:
    def __init__(self, family: DatabaseFamily) -> None:
        self.family = family

    async def initialise(
        self,
        context: ProvisioningContext,
    ) -> tuple[ProvisioningPhaseResult, ...]:
        _ensure_family(context, self.family)
        _require_service_account_connection(context, phase="init")
        raise DatabaseProvisioningOperationError(
            f"Database family {self.family} init provisioning is not implemented."
        )

    async def destroy(
        self,
        context: ProvisioningContext,
        request: DestroyDatabaseRequest,
    ) -> tuple[ProvisioningPhaseResult, ...]:
        del request
        _ensure_family(context, self.family)
        _require_service_account_connection(context, phase="destroy")
        raise DatabaseProvisioningOperationError(
            f"Database family {self.family} destroy is not implemented."
        )

    def maintenance_tasks(
        self,
        context: ProvisioningContext,
    ) -> tuple[DatabaseMaintenanceTask, ...]:
        _ensure_family(context, self.family)
        return ()

    async def run_maintenance(
        self,
        context: ProvisioningContext,
        request: DatabaseMaintenanceRequest,
    ) -> tuple[ProvisioningPhaseResult, ...]:
        _ensure_family(context, self.family)
        _require_service_account_connection(
            context,
            phase=f"maintenance:{request.task}",
        )
        raise DatabaseProvisioningConfigurationError(
            f"Unknown {self.family} maintenance task: {request.task}."
        )

    def quote_identifier(self, identifier: str) -> str:
        return quote_sql_identifier(identifier)


__all__ = ("UnsupportedFamilyProvisioner",)
