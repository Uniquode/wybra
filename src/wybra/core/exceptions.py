"""Shared exception types exported by Wybra modules.

Error categories are intentionally separated so callers can handle the real
kind of failure instead of inspecting messages:

* configuration errors: invalid app config, environment, or runtime setup;
* data validation errors: supplied data is structurally invalid;
* operation/domain errors: accepted input failed during lookup, IO, or runtime
  work and should stay module-specific.
"""


class DataValidationError(ValueError):
    """Base for invalid data values supplied to Wybra APIs."""


class InputValidationError(DataValidationError):
    """Raised when direct caller-provided input is invalid."""


class InvalidConfigurationError(DataValidationError):
    """Raised when configuration data is present but structurally invalid."""


class ConfigurationError(ValueError):
    """Raised when runtime or module configuration is invalid."""
