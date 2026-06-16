"""Shared validation contracts and target discovery."""

from wybra.tools.validation.core import (
    ValidationCheck,
    ValidationResult,
    read_text_for_validation,
    record_check,
)
from wybra.tools.validation.registry import (
    ValidationDiscoveryError,
    ValidationTarget,
    discover_validation_targets,
    validation_target_names,
)

__all__ = (
    "ValidationCheck",
    "ValidationDiscoveryError",
    "ValidationResult",
    "ValidationTarget",
    "discover_validation_targets",
    "read_text_for_validation",
    "record_check",
    "validation_target_names",
)
