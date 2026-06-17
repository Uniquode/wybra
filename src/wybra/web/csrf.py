from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from secrets import token_urlsafe
from typing import Any, ClassVar, Self

from wybra.config import BaseSettings, ConfigDef, ConfigService
from wybra.core.exceptions import ConfigurationError
from wybra.core.runtime import DeploymentEnvironment, normalise_deployment_environment
from wybra.web.config import (
    GENERATE_LOCAL_CSRF_SECRET,
    WEB_CONFIG_SECTION,
    module_config,
)
from wybra.web.forms.csrf import CsrfProtector

CSRF_TOKEN_SECRET_BYTES = 32
logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class CsrfSettings(BaseSettings):
    module_config: ClassVar[ConfigDef] = module_config
    config_section: ClassVar[str | None] = WEB_CONFIG_SECTION

    csrf_token_secret: str = GENERATE_LOCAL_CSRF_SECRET
    csrf_cookie_secure: bool | str | None = None
    deployment_environment: DeploymentEnvironment | str | None = None

    @classmethod
    def load_settings(
        cls,
        config: ConfigService | Mapping[str, Any],
    ) -> Self:
        values = cls.settings_kwargs(config)
        deployment_environment = _deployment_environment_from_config(config)
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
        cookie_secure = (
            deployment_environment != "local"
            if cookie_secure is None
            else cookie_secure
        )
        token_secret = self.csrf_token_secret
        token_secret_configured = _token_secret_is_configured(token_secret)
        if deployment_environment != "local" and not token_secret_configured:
            raise ConfigurationError(
                "Non-local deployments must configure a stable CSRF token secret."
            )
        if deployment_environment != "local" and not cookie_secure:
            raise ConfigurationError(
                "Non-local deployments must use secure CSRF cookies."
            )
        if not token_secret_configured:
            logger.info(
                "Generated startup-local CSRF token secret. Configure "
                "csrf_token_secret for stable tokens across reloads or workers.",
                extra={"deployment_environment": deployment_environment},
            )
            token_secret = token_urlsafe(CSRF_TOKEN_SECRET_BYTES)
        object.__setattr__(self, "deployment_environment", deployment_environment)
        object.__setattr__(self, "csrf_cookie_secure", cookie_secure)
        object.__setattr__(self, "csrf_token_secret", token_secret)

    def protector(self) -> CsrfProtector:
        return CsrfProtector(
            self.csrf_token_secret,
            cookie_secure=bool(self.csrf_cookie_secure),
        )

    @property
    def token_secret(self) -> str:
        """Runtime view of ``csrf_token_secret`` after local secret generation."""
        return self.csrf_token_secret

    @property
    def cookie_secure(self) -> bool | None:
        """Runtime view of ``csrf_cookie_secure`` after environment defaults."""
        if self.csrf_cookie_secure is None:
            return None
        return bool(self.csrf_cookie_secure)


def _token_secret_is_configured(csrf_token_secret: str) -> bool:
    if csrf_token_secret == GENERATE_LOCAL_CSRF_SECRET:
        return False
    if not csrf_token_secret.strip():
        raise ConfigurationError("CSRF token secret must not be blank.")
    return True


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


def _deployment_environment_from_config(
    config: ConfigService | Mapping[str, Any],
) -> str | None:
    """Return configured deployment environment.

    Missing values are left as None so CsrfSettings construction applies its
    local default. Blank or non-text values are explicit configuration errors.
    """
    if isinstance(config, ConfigService):
        app_config = CsrfSettings.section_values(config, "app")
        value = app_config.get("deployment_environment")
    else:
        app_config = config.get("app")
        if app_config is not None:
            if not isinstance(app_config, Mapping):
                raise ConfigurationError("[app] must be a table.")
            value = app_config.get("deployment_environment")
        else:
            value = config.get("deployment_environment")
    if value is None:
        return None
    if isinstance(value, str) and value.strip():
        return value
    raise ConfigurationError("deployment_environment must be a non-blank string.")


__all__ = (
    "CSRF_TOKEN_SECRET_BYTES",
    "CsrfSettings",
    "GENERATE_LOCAL_CSRF_SECRET",
)
