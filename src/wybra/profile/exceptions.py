from __future__ import annotations

from wybra.core import InputValidationError


class ProfileCapabilityError(RuntimeError):
    """Raised when a profile capability operation cannot be completed."""


class ProfileInputError(InputValidationError):
    """Raised when caller-provided profile input is invalid."""


__all__ = ("ProfileCapabilityError", "ProfileInputError")
