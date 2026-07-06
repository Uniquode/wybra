from __future__ import annotations

from typing import Final, Literal

DiagnosticLevel = Literal["info", "debug", "trace"]

DIAGNOSTIC_LEVEL_VALUES: Final[dict[DiagnosticLevel, int]] = {
    "trace": 5,
    "debug": 10,
    "info": 20,
}


def normalise_diagnostics_level(value: object) -> DiagnosticLevel:
    if isinstance(value, str):
        normalised = value.strip().lower()
        if normalised in DIAGNOSTIC_LEVEL_VALUES:
            return normalised  # ty: ignore[invalid-return-type]
    raise ValueError("Diagnostics level must be one of: info, debug, trace.")


def to_diagnostics_level(value: object) -> DiagnosticLevel:
    try:
        return normalise_diagnostics_level(value)
    except ValueError as exc:
        raise ValueError("must be one of: info, debug, trace.") from exc


__all__ = (
    "DIAGNOSTIC_LEVEL_VALUES",
    "DiagnosticLevel",
    "normalise_diagnostics_level",
    "to_diagnostics_level",
)
