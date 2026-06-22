"""Shared exception types exported by Wybra modules.

Error categories are intentionally separated so callers can handle the real
kind of failure instead of inspecting messages:

* configuration errors: invalid app config, environment, or runtime setup;
* data validation errors: supplied data is structurally invalid;
* operation/domain errors: accepted input failed during lookup, IO, or runtime
  work and should stay module-specific.
"""

from starlette.exceptions import HTTPException


class DataValidationError(ValueError):
    """Base for invalid data values supplied to Wybra APIs."""


class InputValidationError(DataValidationError):
    """Raised when direct caller-provided input is invalid."""


class InvalidConfigurationError(DataValidationError):
    """Raised when configuration data is present but structurally invalid."""


class ConfigurationError(ValueError):
    """Raised when runtime or module configuration is invalid."""


class HttpException(HTTPException):
    """Base for HTTP control-flow exceptions."""

    default_status_code = 500
    default_detail = "Internal Server Error"

    def __init__(
        self,
        detail: str | None = None,
        *,
        status_code: int | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        resolved_status_code = (
            self.default_status_code if status_code is None else status_code
        )
        resolved_detail = self.default_detail if detail is None else detail
        super().__init__(
            status_code=resolved_status_code,
            detail=resolved_detail,
            headers=headers,
        )


class Http400(HttpException):
    """Raised when the request is malformed."""

    default_status_code = 400
    default_detail = "Bad Request"


class Http401(HttpException):
    """Raised when authentication is required."""

    default_status_code = 401
    default_detail = "Authentication Required"


class Http403(HttpException):
    """Raised when access is forbidden."""

    default_status_code = 403
    default_detail = "Forbidden"


class Http404(HttpException):
    """Raised when a requested resource is not found."""

    default_status_code = 404
    default_detail = "Not Found"
