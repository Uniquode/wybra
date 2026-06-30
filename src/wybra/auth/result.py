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
ERROR_PASSWORD_TOO_SHORT: Final[ResultErrorType] = "password_too_short"
ERROR_PASSWORD_TOO_WEAK: Final[ResultErrorType] = "password_too_weak"
ERROR_AUTHENTICATION_METHOD_REQUIRED: Final[ResultErrorType] = (
    "authentication_method_required"
)
ERROR_EMAIL_VERIFICATION_REQUIRED: Final[ResultErrorType] = (
    "email_verification_required"
)
ERROR_POLICY_DISABLED: Final[ResultErrorType] = "policy_disabled"
ERROR_TOTP_CODE_REQUIRED: Final[ResultErrorType] = "totp_code_required"
ERROR_TOTP_INVALID: Final[ResultErrorType] = "totp_invalid"
ERROR_TOTP_RECOVERY_INVALID: Final[ResultErrorType] = "totp_recovery_invalid"
ERROR_TOTP_SETUP_REQUIRED: Final[ResultErrorType] = "totp_setup_required"
ERROR_TOKEN_REJECTED: Final[ResultErrorType] = "token_rejected"
ERROR_VERIFICATION_CODE_INVALID: Final[ResultErrorType] = "verification_code_invalid"

__all__ = [
    "ERROR_ALREADY_EXISTS",
    "ERROR_ALREADY_VERIFIED",
    "ERROR_IDENTITY_CHANGED",
    "ERROR_INACTIVE_USER",
    "ERROR_INVALID_EMAIL",
    "ERROR_INVALID_TOKEN",
    "ERROR_INVALID_PASSWORD",
    "ERROR_PASSWORD_TOO_SHORT",
    "ERROR_PASSWORD_TOO_WEAK",
    "ERROR_AUTHENTICATION_METHOD_REQUIRED",
    "ERROR_EMAIL_VERIFICATION_REQUIRED",
    "ERROR_POLICY_DISABLED",
    "ERROR_TOTP_CODE_REQUIRED",
    "ERROR_TOTP_INVALID",
    "ERROR_TOTP_RECOVERY_INVALID",
    "ERROR_TOTP_SETUP_REQUIRED",
    "ERROR_TOKEN_REJECTED",
    "ERROR_VERIFICATION_CODE_INVALID",
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
