from __future__ import annotations

from dataclasses import dataclass

from wybra.config import ConfigService, to_bool, to_positive_float
from wybra.diagnostics.events import DiagnosticLevel, normalise_diagnostics_level

DIAGNOSTICS_CONFIG_SECTION = "wybra.diagnostics"


@dataclass(frozen=True, slots=True)
class DiagnosticsSettings:
    enabled: bool = False
    level: DiagnosticLevel = "info"
    logging_bridge: bool = False
    slow_sql_threshold_seconds: float = 0.5

    @classmethod
    def load_settings(cls, config: ConfigService) -> DiagnosticsSettings:
        values = config.get_config(DIAGNOSTICS_CONFIG_SECTION) or {}
        return cls(
            enabled=to_bool(values.get("enabled", False)),
            level=normalise_diagnostics_level(values.get("level", "info")),
            logging_bridge=to_bool(values.get("logging_bridge", False)),
            slow_sql_threshold_seconds=to_positive_float(
                values.get("slow_sql_threshold_seconds", 0.5)
            ),
        )

    def __post_init__(self) -> None:
        object.__setattr__(self, "enabled", bool(self.enabled))
        object.__setattr__(
            self,
            "level",
            normalise_diagnostics_level(self.level),
        )
        object.__setattr__(self, "logging_bridge", bool(self.logging_bridge))
        if self.slow_sql_threshold_seconds <= 0:
            raise ValueError(
                "wybra.diagnostics.slow_sql_threshold_seconds must be positive."
            )


__all__ = (
    "DIAGNOSTICS_CONFIG_SECTION",
    "DiagnosticsSettings",
)
