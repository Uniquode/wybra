"""Runtime secret lookup capability."""

from __future__ import annotations

from importlib import import_module
from typing import Any

_EXPORTS = {
    "AwsSecretsManagerSourceDriver": "wybra.secrets.sources",
    "CryptoSecretSourceSettings": "wybra.secrets.config",
    "DefaultSecretsCapability": "wybra.secrets.capabilities",
    "EnvironmentSecretSourceDriver": "wybra.secrets.sources",
    "InvalidSecretKeyError": "wybra.services.secrets",
    "KeychainSecretSourceDriver": "wybra.secrets.sources",
    "KeychainSecretSourceSettings": "wybra.secrets.config",
    "KmsSecretSourceSettings": "wybra.secrets.config",
    "MissingSecretError": "wybra.services.secrets",
    "MissingSecretSourceDependencyError": "wybra.services.secrets",
    "SECRET_SOURCES": "wybra.services.secrets",
    "SecretSource": "wybra.services.secrets",
    "SecretSourceDriver": "wybra.secrets.sources",
    "SecretSourceOperationError": "wybra.services.secrets",
    "SecretSourceUnavailableError": "wybra.services.secrets",
    "SecretValue": "wybra.services.secrets",
    "SecretsCapability": "wybra.services.secrets",
    "SecretsConfigurationError": "wybra.services.secrets",
    "SecretsError": "wybra.services.secrets",
    "SecretsSettings": "wybra.secrets.config",
    "UnknownSecretSourceError": "wybra.services.secrets",
    "UnsupportedSecretSourceError": "wybra.services.secrets",
    "VaultSecretSourceDriver": "wybra.secrets.sources",
    "VaultSecretSourceSettings": "wybra.secrets.config",
    "environment_key_name": "wybra.secrets.sources",
    "known_keychain_secret_keys": "wybra.secrets.keys",
    "module_config": "wybra.secrets.config",
    "normalise_secret_source": "wybra.services.secrets",
    "secret_key_value": "wybra.services.secrets",
    "setup_site": "wybra.secrets.capabilities",
    "validate_secrets": "wybra.secrets.validation",
    "validation_targets": "wybra.secrets.validation",
}

__all__ = sorted(_EXPORTS)


def __getattr__(name: str) -> Any:
    module_name = _EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(module_name), name)
    globals()[name] = value
    return value
