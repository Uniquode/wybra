from __future__ import annotations

from collections.abc import Mapping
from typing import Any, NoReturn

from wybra.services.secrets import (
    MissingSecretError,
    SecretSourceOperationError,
    SecretSourceUnavailableError,
)


def aws_error_code(exc: Exception) -> str | None:
    response = getattr(exc, "response", None)
    if isinstance(response, Mapping):
        error = response.get("Error")
        if isinstance(error, Mapping):
            code = error.get("Code")
            if isinstance(code, str):
                return code
    return None


def aws_secret_missing(exc: Exception) -> bool:
    return aws_error_code(exc) == "ResourceNotFoundException"


def raise_aws_secret_source_error(
    exc: Exception,
    *,
    source: str,
    key: str,
    operation: str,
) -> NoReturn:
    code = aws_error_code(exc)
    if aws_secret_missing(exc):
        raise MissingSecretError(source=source, key=key) from exc
    if type(exc).__name__ in {"NoCredentialsError", "PartialCredentialsError"}:
        raise SecretSourceUnavailableError(
            source=source,
            key=key,
            reason=type(exc).__name__,
        ) from exc
    if code in {"AccessDeniedException", "UnrecognizedClientException"}:
        raise SecretSourceUnavailableError(
            source=source,
            key=key,
            reason=code,
        ) from exc
    raise SecretSourceOperationError(
        source=source,
        key=key,
        operation=operation,
        reason=code or type(exc).__name__,
    ) from exc


def vault_secret_value(response: Mapping[str, Any]) -> str | None:
    data = response.get("data")
    if isinstance(data, Mapping):
        nested = data.get("data")
        if isinstance(nested, Mapping):
            if "value" in nested:
                return str(nested["value"])
            if len(nested) == 1:
                return str(next(iter(nested.values())))
        if "value" in data:
            return str(data["value"])
    return None


def vault_secret_missing(exc: Exception) -> bool:
    if type(exc).__name__ == "InvalidPath":
        return True
    response = getattr(exc, "response", None)
    return getattr(response, "status_code", None) == 404


def raise_vault_secret_source_error(
    exc: Exception,
    *,
    source: str,
    key: str,
    operation: str,
) -> NoReturn:
    if vault_secret_missing(exc):
        raise MissingSecretError(source=source, key=key) from exc
    if type(exc).__name__ in {"Forbidden", "Unauthorized"}:
        raise SecretSourceUnavailableError(
            source=source,
            key=key,
            reason=type(exc).__name__,
        ) from exc
    raise SecretSourceOperationError(
        source=source,
        key=key,
        operation=operation,
        reason=type(exc).__name__,
    ) from exc


def raise_keyring_secret_source_error(
    exc: Exception,
    *,
    source: str,
    key: str,
    operation: str,
) -> NoReturn:
    if type(exc).__name__ in {"InitError", "KeyringLocked", "NoKeyringError"}:
        raise SecretSourceUnavailableError(
            source=source,
            key=key,
            reason=f"keyring backend unavailable: {type(exc).__name__}",
        ) from exc
    raise SecretSourceOperationError(
        source=source,
        key=key,
        operation=operation,
        reason=type(exc).__name__,
    ) from exc


def keyring_reports_missing_secret(exc: Exception) -> bool:
    """Return whether a keyring read error is a platform missing-item response."""

    return type(exc).__name__ == "KeyringError" and "(-50," in str(exc)


__all__ = (
    "aws_error_code",
    "aws_secret_missing",
    "keyring_reports_missing_secret",
    "raise_aws_secret_source_error",
    "raise_keyring_secret_source_error",
    "raise_vault_secret_source_error",
    "vault_secret_missing",
    "vault_secret_value",
)
