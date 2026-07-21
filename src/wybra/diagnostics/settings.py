from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import ClassVar, cast

from wybra.config import (
    BaseSettings,
    ConfigDef,
    to_bool,
    to_positive_float,
    to_positive_int,
)
from wybra.diagnostics.records import DiagnosticLevel, normalise_diagnostics_level
from wybra.diagnostics_config import DIAGNOSTICS_CONFIG_DEF
from wybra.events._core import DEFAULT_EVENT_SCOPES, EventScope, parse_event_scopes

DIAGNOSTICS_CONFIG_SECTION = "wybra.diagnostics"


@dataclass(frozen=True, slots=True)
class DiagnosticsSettings(BaseSettings):
    module_config: ClassVar[ConfigDef] = DIAGNOSTICS_CONFIG_DEF
    config_section: ClassVar[str | None] = DIAGNOSTICS_CONFIG_SECTION

    events_enabled: bool = False
    event_scopes: tuple[EventScope, ...] = DEFAULT_EVENT_SCOPES
    debug_enabled: bool = False
    debug_allowed_hosts: tuple[str, ...] = ("localhost", "127.0.0.1", "::1")
    retention_limit: int = 100
    subscription_queue_limit: int = 32
    level: DiagnosticLevel = "info"
    logging_bridge: bool = False
    slow_sql_threshold_seconds: float = 0.5

    def __post_init__(self) -> None:
        object.__setattr__(self, "events_enabled", to_bool(self.events_enabled))
        object.__setattr__(self, "event_scopes", _event_scopes(self.event_scopes))
        object.__setattr__(self, "debug_enabled", to_bool(self.debug_enabled))
        object.__setattr__(
            self,
            "debug_allowed_hosts",
            _allowed_hosts(self.debug_allowed_hosts),
        )
        object.__setattr__(
            self,
            "retention_limit",
            to_positive_int(self.retention_limit),
        )
        object.__setattr__(
            self,
            "subscription_queue_limit",
            to_positive_int(self.subscription_queue_limit),
        )
        object.__setattr__(
            self,
            "level",
            normalise_diagnostics_level(self.level),
        )
        object.__setattr__(self, "logging_bridge", to_bool(self.logging_bridge))
        object.__setattr__(
            self,
            "slow_sql_threshold_seconds",
            to_positive_float(self.slow_sql_threshold_seconds),
        )


def _event_scopes(value: object) -> tuple[EventScope, ...]:
    if isinstance(value, tuple) and all(
        isinstance(scope, EventScope) for scope in value
    ):
        return cast(tuple[EventScope, ...], value)
    if isinstance(value, str):
        return parse_event_scopes(value)
    return parse_event_scopes(cast(Iterable[str | EventScope], value))


def _allowed_hosts(value: str | Iterable[str]) -> tuple[str, ...]:
    values = value.split(",") if isinstance(value, str) else value
    hosts = tuple(host.strip().lower() for host in values if host.strip())
    if not hosts:
        raise ValueError("wybra.diagnostics.debug_allowed_hosts must not be empty.")
    if any("/" in host or "://" in host for host in hosts):
        raise ValueError(
            "wybra.diagnostics.debug_allowed_hosts must contain host names only."
        )
    return hosts


__all__ = (
    "DIAGNOSTICS_CONFIG_SECTION",
    "DiagnosticsSettings",
)
