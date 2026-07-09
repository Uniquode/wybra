from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from secrets import token_urlsafe
from typing import Any, ClassVar, Self, cast

from wybra.config import BaseSettings, ConfigDef, ConfigService, CredentialReference
from wybra.core.exceptions import ConfigurationError
from wybra.core.runtime import DeploymentEnvironment, normalise_deployment_environment
from wybra.forms.config import (
    CSRF_TOKEN_SECRET_KEY_CURRENT,
    CSRF_TOKEN_SECRET_KEY_PREVIOUS,
    FORMS_CONFIG_SECTION,
    module_config,
    normalise_optional_positive_float,
)
from wybra.forms.csrf import CSRF_TOKEN_MAX_AGE_SECONDS, CsrfProtector
from wybra.services.secrets import KEYCHAIN_SOURCE, SecretSource

CSRF_TOKEN_SECRET_BYTES = 32
logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class FormsSettings(BaseSettings):
    module_config: ClassVar[ConfigDef] = module_config
    config_section: ClassVar[str | None] = FORMS_CONFIG_SECTION

    csrf_token_secret: str | None = None
    csrf_token_secret_source: SecretSource | str | None = None
    csrf_token_secret_key: str | None = None
    csrf_token_secret_previous_key: str | None = None
    csrf_token_max_age_seconds: float | str | None = None
    csrf_cookie_secure: bool | str | None = None
    deployment_environment: DeploymentEnvironment | str | None = None

    @classmethod
    def load_settings(
        cls,
        config: ConfigService | Mapping[str, Any],
        *,
        deployment_environment: DeploymentEnvironment | str | None = None,
    ) -> Self:
        values = cls.settings_kwargs(config)
        if deployment_environment is not None:
            values["deployment_environment"] = deployment_environment
        return cls(**values)

    def __post_init__(self) -> None:
        deployment_environment = normalise_deployment_environment(
            self.deployment_environment
        )
        cookie_secure = _normalise_optional_bool(
            self.csrf_cookie_secure,
            "csrf_cookie_secure",
        )
        token_max_age_seconds = _normalise_positive_float(
            self.csrf_token_max_age_seconds,
            "csrf_token_max_age_seconds",
        )
        cookie_secure = (
            deployment_environment != "local"
            if cookie_secure is None
            else cookie_secure
        )
        secret_reference = _normalise_csrf_token_secret_reference(
            self.csrf_token_secret_source,
            self.csrf_token_secret_key,
        )
        previous_secret_reference = _normalise_csrf_token_secret_previous_reference(
            self.csrf_token_secret_source,
            self.csrf_token_secret_previous_key,
        )
        token_secret = _normalise_token_secret(self.csrf_token_secret)
        if (
            deployment_environment != "local"
            and token_secret is None
            and secret_reference is None
        ):
            raise ConfigurationError(
                "Non-local deployments must configure a stable CSRF token secret."
            )
        if deployment_environment != "local" and not cookie_secure:
            raise ConfigurationError(
                "Non-local deployments must use secure CSRF cookies."
            )
        if token_secret is None and secret_reference is None:
            logger.info(
                "Generated startup-local CSRF token secret. Configure "
                "csrf_token_secret for stable tokens across reloads or workers.",
                extra={"deployment_environment": deployment_environment},
            )
            token_secret = token_urlsafe(CSRF_TOKEN_SECRET_BYTES)
        object.__setattr__(self, "deployment_environment", deployment_environment)
        object.__setattr__(self, "csrf_cookie_secure", cookie_secure)
        object.__setattr__(
            self,
            "csrf_token_max_age_seconds",
            token_max_age_seconds,
        )
        object.__setattr__(self, "csrf_token_secret", token_secret)
        object.__setattr__(
            self,
            "csrf_token_secret_source",
            None if secret_reference is None else secret_reference[0],
        )
        object.__setattr__(
            self,
            "csrf_token_secret_key",
            None if secret_reference is None else secret_reference[1],
        )
        object.__setattr__(
            self,
            "csrf_token_secret_previous_key",
            None if previous_secret_reference is None else previous_secret_reference[1],
        )

    def protector(
        self,
        token_secret: str | None = None,
        previous_token_secrets: tuple[str, ...] = (),
    ) -> CsrfProtector:
        resolved_secret = token_secret or self.fallback_token_secret
        if resolved_secret is None:
            raise ConfigurationError("CSRF token secret has not been resolved.")
        token_max_age_seconds = self.csrf_token_max_age_seconds
        if not isinstance(token_max_age_seconds, (int, float)):
            raise ConfigurationError("CSRF token max age has not been resolved.")
        return CsrfProtector(
            resolved_secret,
            previous_secrets=previous_token_secrets,
            cookie_secure=bool(self.csrf_cookie_secure),
            token_max_age_seconds=float(token_max_age_seconds),
        )

    @property
    def token_secret(self) -> str | None:
        """Runtime view of a configured or generated fallback token secret."""
        return self.csrf_token_secret

    @property
    def fallback_token_secret(self) -> str | None:
        """Configured or generated fallback secret used when keychain lookup misses."""
        return self.csrf_token_secret

    @property
    def csrf_token_secret_reference(self) -> tuple[SecretSource, str] | None:
        if self.csrf_token_secret_source is None or self.csrf_token_secret_key is None:
            return None
        return (
            cast(SecretSource, self.csrf_token_secret_source),
            self.csrf_token_secret_key,
        )

    @property
    def csrf_token_secret_previous_reference(self) -> tuple[SecretSource, str] | None:
        if (
            self.csrf_token_secret_source is None
            or self.csrf_token_secret_previous_key is None
        ):
            return None
        return (
            cast(SecretSource, self.csrf_token_secret_source),
            self.csrf_token_secret_previous_key,
        )

    @property
    def cookie_secure(self) -> bool | None:
        """Runtime view of ``csrf_cookie_secure`` after environment defaults."""
        if self.csrf_cookie_secure is None:
            return None
        return bool(self.csrf_cookie_secure)

    def credential_references(self) -> tuple[CredentialReference, ...]:
        reference = self.csrf_token_secret_reference
        if reference is None:
            return ()
        source, key = reference
        references = [
            CredentialReference(
                name="csrf",
                key=key,
                owner="forms",
                description="Configured current forms CSRF token secret.",
                source=source,
                required=True,
                rotation_role="current",
            )
        ]
        previous_reference = self.csrf_token_secret_previous_reference
        if previous_reference is not None:
            previous_source, previous_key = previous_reference
            references.append(
                CredentialReference(
                    name="csrf-prev",
                    key=previous_key,
                    owner="forms",
                    description="Configured previous forms CSRF token secrets.",
                    source=previous_source,
                    rotation_role="previous",
                )
            )
        return tuple(references)


def _normalise_token_secret(csrf_token_secret: str | None) -> str | None:
    if csrf_token_secret is None:
        return None
    if not isinstance(csrf_token_secret, str) or not csrf_token_secret.strip():
        raise ConfigurationError("CSRF token secret must not be blank.")
    return csrf_token_secret.strip()


def _normalise_optional_bool(
    value: bool | str | None,
    setting_name: str,
) -> bool | None:
    if value is None or isinstance(value, bool):
        return value
    normalised = value.strip().lower()
    if normalised in {"1", "true", "yes", "on"}:
        return True
    if normalised in {"0", "false", "no", "off"}:
        return False
    raise ConfigurationError(f"{setting_name} must be a boolean value.")


def _normalise_positive_float(
    value: float | str | None,
    setting_name: str,
) -> float:
    try:
        normalised = normalise_optional_positive_float(value)
    except ValueError as exc:
        raise ConfigurationError(f"{setting_name} must be a positive number.") from exc
    if normalised is None:
        return float(CSRF_TOKEN_MAX_AGE_SECONDS)
    return normalised


def _normalise_csrf_token_secret_reference(
    source: SecretSource | str | None,
    key: str | None,
) -> tuple[SecretSource, str] | None:
    if source is None and key is None:
        return None
    if source is None:
        raise ConfigurationError(
            "csrf_token_secret_source is required when csrf_token_secret_key is set."
        )
    if source != KEYCHAIN_SOURCE:
        raise ConfigurationError("csrf_token_secret_source must be keychain.")
    if key is None:
        return KEYCHAIN_SOURCE, CSRF_TOKEN_SECRET_KEY_CURRENT
    if not isinstance(key, str) or not key.strip():
        raise ConfigurationError("csrf_token_secret_key must be a non-blank string.")
    return KEYCHAIN_SOURCE, key.strip()


def _normalise_csrf_token_secret_previous_reference(
    source: SecretSource | str | None,
    key: str | None,
) -> tuple[SecretSource, str] | None:
    if source is None and key is None:
        return None
    if source is None:
        raise ConfigurationError(
            "csrf_token_secret_source is required when "
            "csrf_token_secret_previous_key is set."
        )
    if source != KEYCHAIN_SOURCE:
        raise ConfigurationError("csrf_token_secret_source must be keychain.")
    if key is None:
        return KEYCHAIN_SOURCE, CSRF_TOKEN_SECRET_KEY_PREVIOUS
    if not isinstance(key, str) or not key.strip():
        raise ConfigurationError(
            "csrf_token_secret_previous_key must be a non-blank string."
        )
    return KEYCHAIN_SOURCE, key.strip()


__all__ = (
    "CSRF_TOKEN_SECRET_BYTES",
    "CSRF_TOKEN_MAX_AGE_SECONDS",
    "CSRF_TOKEN_SECRET_KEY_CURRENT",
    "CSRF_TOKEN_SECRET_KEY_PREVIOUS",
    "FormsSettings",
)
