from __future__ import annotations

from typing import Final

from wybra.config import ConfigDef, ConfigField, ConfigGroup, to_bool
from wybra.diagnostics_config import (
    DIAGNOSTICS_CONFIG_DEF,
    ENV_WYBRA_DIAG_ALLOWED_HOSTS,
    ENV_WYBRA_DIAG_ENABLED,
    ENV_WYBRA_DIAG_RETENTION_LIMIT,
    ENV_WYBRA_DIAG_SUBSCRIPTION_QUEUE_LIMIT,
    ENV_WYBRA_DIAGNOSTICS_LEVEL,
    ENV_WYBRA_DIAGNOSTICS_LOGGING_BRIDGE,
    ENV_WYBRA_DIAGNOSTICS_SLOW_SQL_SECONDS,
    ENV_WYBRA_EVENTS_ENABLED,
    ENV_WYBRA_EVENTS_FILTER,
)

ENV_APP_ENV: Final = "APP_ENV"
ENV_APP_DEBUG: Final = "APP_DEBUG"
RUNTIME_CONFIG_DEF: Final = ConfigDef(
    {
        "app": ConfigGroup(
            fields=(
                ConfigField(
                    name="deployment_environment",
                    env=ENV_APP_ENV,
                ),
                ConfigField(
                    name="debug",
                    default=False,
                    env=ENV_APP_DEBUG,
                    transform=to_bool,
                ),
            ),
        ),
        **DIAGNOSTICS_CONFIG_DEF.sections,
    }
)

__all__ = (
    "ENV_APP_DEBUG",
    "ENV_APP_ENV",
    "ENV_WYBRA_DIAG_ALLOWED_HOSTS",
    "ENV_WYBRA_DIAG_ENABLED",
    "ENV_WYBRA_DIAG_RETENTION_LIMIT",
    "ENV_WYBRA_DIAG_SUBSCRIPTION_QUEUE_LIMIT",
    "ENV_WYBRA_EVENTS_ENABLED",
    "ENV_WYBRA_EVENTS_FILTER",
    "ENV_WYBRA_DIAGNOSTICS_LEVEL",
    "ENV_WYBRA_DIAGNOSTICS_LOGGING_BRIDGE",
    "ENV_WYBRA_DIAGNOSTICS_SLOW_SQL_SECONDS",
    "RUNTIME_CONFIG_DEF",
)
