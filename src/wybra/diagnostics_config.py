"""Shared runtime configuration definition for diagnostics.

This module deliberately sits outside :mod:`wybra.diagnostics` so core startup
can load it without initialising the diagnostics package.
"""

from __future__ import annotations

from typing import Final

from wybra.config import (
    ConfigDef,
    ConfigField,
    ConfigGroup,
    to_bool,
    to_positive_float,
    to_positive_int,
)
from wybra.events import DEFAULT_EVENT_SCOPES, parse_event_scopes

ENV_WYBRA_EVENTS_ENABLED: Final = "WYBRA_EVENTS_ENABLED"
ENV_WYBRA_EVENTS_FILTER: Final = "WYBRA_EVENTS_FILTER"
ENV_WYBRA_DIAG_ENABLED: Final = "WYBRA_DIAG_ENABLED"
ENV_WYBRA_DIAG_ALLOWED_HOSTS: Final = "WYBRA_DIAG_ALLOWED_HOSTS"
ENV_WYBRA_DIAG_RETENTION_LIMIT: Final = "WYBRA_DIAG_RETENTION_LIMIT"
ENV_WYBRA_DIAG_SUBSCRIPTION_QUEUE_LIMIT: Final = "WYBRA_DIAG_SUBSCRIPTION_QUEUE_LIMIT"
ENV_WYBRA_DIAGNOSTICS_LEVEL: Final = "WYBRA_DIAGNOSTICS_LEVEL"
ENV_WYBRA_DIAGNOSTICS_LOGGING_BRIDGE: Final = "WYBRA_DIAGNOSTICS_LOGGING_BRIDGE"
ENV_WYBRA_DIAGNOSTICS_SLOW_SQL_SECONDS: Final = "WYBRA_DIAGNOSTICS_SLOW_SQL_SECONDS"

DIAGNOSTICS_CONFIG_DEF: Final = ConfigDef(
    {
        "wybra.diagnostics": ConfigGroup(
            fields=(
                ConfigField(
                    name="events_enabled",
                    default=False,
                    env=ENV_WYBRA_EVENTS_ENABLED,
                    transform=to_bool,
                ),
                ConfigField(
                    name="event_scopes",
                    default=DEFAULT_EVENT_SCOPES,
                    env=ENV_WYBRA_EVENTS_FILTER,
                    transform=parse_event_scopes,
                ),
                ConfigField(
                    name="debug_enabled",
                    default=False,
                    env=ENV_WYBRA_DIAG_ENABLED,
                    transform=to_bool,
                ),
                ConfigField(
                    name="debug_allowed_hosts",
                    default=("localhost", "127.0.0.1", "::1"),
                    env=ENV_WYBRA_DIAG_ALLOWED_HOSTS,
                ),
                ConfigField(
                    name="retention_limit",
                    default=100,
                    env=ENV_WYBRA_DIAG_RETENTION_LIMIT,
                    transform=to_positive_int,
                ),
                ConfigField(
                    name="subscription_queue_limit",
                    default=32,
                    env=ENV_WYBRA_DIAG_SUBSCRIPTION_QUEUE_LIMIT,
                    transform=to_positive_int,
                ),
                ConfigField(
                    name="level",
                    default="info",
                    env=ENV_WYBRA_DIAGNOSTICS_LEVEL,
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
            )
        )
    }
)


__all__ = (
    "DIAGNOSTICS_CONFIG_DEF",
    "ENV_WYBRA_DIAG_ALLOWED_HOSTS",
    "ENV_WYBRA_DIAG_ENABLED",
    "ENV_WYBRA_DIAG_RETENTION_LIMIT",
    "ENV_WYBRA_DIAG_SUBSCRIPTION_QUEUE_LIMIT",
    "ENV_WYBRA_DIAGNOSTICS_LEVEL",
    "ENV_WYBRA_DIAGNOSTICS_LOGGING_BRIDGE",
    "ENV_WYBRA_DIAGNOSTICS_SLOW_SQL_SECONDS",
    "ENV_WYBRA_EVENTS_ENABLED",
    "ENV_WYBRA_EVENTS_FILTER",
)
