"""Compatibility exports for shared secrets exceptions."""

from __future__ import annotations

from wybra.services.secrets import (
    InvalidSecretKeyError,
    MissingSecretError,
    MissingSecretSourceDependencyError,
    SecretsConfigurationError,
    SecretsError,
    SecretSourceOperationError,
    SecretSourceUnavailableError,
    UnknownSecretSourceError,
    UnsupportedSecretSourceError,
)

__all__ = (
    "InvalidSecretKeyError",
    "MissingSecretError",
    "MissingSecretSourceDependencyError",
    "SecretSourceOperationError",
    "SecretSourceUnavailableError",
    "SecretsConfigurationError",
    "SecretsError",
    "UnknownSecretSourceError",
    "UnsupportedSecretSourceError",
)
