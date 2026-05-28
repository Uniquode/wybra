from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Final

type ResultValue = int | float | str | Decimal | list[str] | dict[str, Any]
type ResultErrorType = str

ERROR_ALREADY_EXISTS: Final[ResultErrorType] = "already_exists"
ERROR_ALREADY_VERIFIED: Final[ResultErrorType] = "already_verified"
ERROR_IDENTITY_CHANGED: Final[ResultErrorType] = "identity_changed"
ERROR_INACTIVE_USER: Final[ResultErrorType] = "inactive_user"
ERROR_INVALID_EMAIL: Final[ResultErrorType] = "invalid_email"
ERROR_INVALID_TOKEN: Final[ResultErrorType] = "invalid_token"
ERROR_INVALID_PASSWORD: Final[ResultErrorType] = "invalid_password"
ERROR_POLICY_DISABLED: Final[ResultErrorType] = "policy_disabled"
ERROR_TOKEN_REJECTED: Final[ResultErrorType] = "token_rejected"

__all__ = [
    "ERROR_ALREADY_EXISTS",
    "ERROR_ALREADY_VERIFIED",
    "ERROR_IDENTITY_CHANGED",
    "ERROR_INACTIVE_USER",
    "ERROR_INVALID_EMAIL",
    "ERROR_INVALID_TOKEN",
    "ERROR_INVALID_PASSWORD",
    "ERROR_POLICY_DISABLED",
    "ERROR_TOKEN_REJECTED",
    "Result",
    "ResultErrorType",
    "ResultValue",
]


@dataclass(frozen=True, slots=True)
class Result[T: ResultValue]:
    """Generic result value for explicit success and failure outcomes."""

    success: bool
    value: T | None = None
    error_type: ResultErrorType | None = None
    message: str | None = None

    @classmethod
    def ok(cls, value: T | None = None, message: str | None = None) -> Result[T]:
        return cls(success=True, value=value, message=message)

    @classmethod
    def failure(
        cls,
        error_type: ResultErrorType,
        message: str | None = None,
    ) -> Result[T]:
        return cls(success=False, error_type=error_type, message=message)

    def is_ok(self) -> bool:
        return self.success

    def is_failure(self) -> bool:
        return not self.success
