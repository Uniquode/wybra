from __future__ import annotations

from typing import Final

from wybra.config import ConfigDef, ConfigField, ConfigGroup, to_bool, to_positive_float

ENV_APP_ENV: Final = "APP_ENV"
ENV_APP_DEBUG: Final = "APP_DEBUG"
ENV_WYBRA_DIAGNOSTICS_ENABLED: Final = "WYBRA_DIAGNOSTICS_ENABLED"
ENV_WYBRA_DIAGNOSTICS_LEVEL: Final = "WYBRA_DIAGNOSTICS_LEVEL"
ENV_WYBRA_DIAGNOSTICS_LOGGING_BRIDGE: Final = "WYBRA_DIAGNOSTICS_LOGGING_BRIDGE"
ENV_WYBRA_DIAGNOSTICS_SLOW_SQL_SECONDS: Final = "WYBRA_DIAGNOSTICS_SLOW_SQL_SECONDS"

DIAGNOSTICS_LEVELS: Final = frozenset({"info", "debug", "trace"})


def to_diagnostics_level(value: object) -> str:
    if isinstance(value, str):
        normalised = value.strip().lower()
        if normalised in DIAGNOSTICS_LEVELS:
            return normalised
    raise ValueError("must be one of: info, debug, trace.")


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
        "wybra.diagnostics": ConfigGroup(
            fields=(
                ConfigField(
                    name="enabled",
                    default=False,
                    env=ENV_WYBRA_DIAGNOSTICS_ENABLED,
                    transform=to_bool,
                ),
                ConfigField(
                    name="level",
                    default="info",
                    env=ENV_WYBRA_DIAGNOSTICS_LEVEL,
                    transform=to_diagnostics_level,
                ),
                ConfigField(
                    name="logging_bridge",
                    default=False,
                    env=ENV_WYBRA_DIAGNOSTICS_LOGGING_BRIDGE,
                    transform=to_bool,
                ),
                ConfigField(
                    name="slow_sql_threshold_seconds",
                    default=0.5,
                    env=ENV_WYBRA_DIAGNOSTICS_SLOW_SQL_SECONDS,
                    transform=to_positive_float,
                ),
            ),
        ),
    }
)

__all__ = (
    "DIAGNOSTICS_LEVELS",
    "ENV_APP_DEBUG",
    "ENV_APP_ENV",
    "ENV_WYBRA_DIAGNOSTICS_ENABLED",
    "ENV_WYBRA_DIAGNOSTICS_LEVEL",
    "ENV_WYBRA_DIAGNOSTICS_LOGGING_BRIDGE",
    "ENV_WYBRA_DIAGNOSTICS_SLOW_SQL_SECONDS",
    "RUNTIME_CONFIG_DEF",
    "to_diagnostics_level",
)
