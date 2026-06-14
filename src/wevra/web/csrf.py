from __future__ import annotations

import logging
from dataclasses import dataclass
from secrets import token_urlsafe
from typing import Any

from wevra.core.exceptions import ConfigurationError
from wevra.core.runtime import DeploymentEnvironment, normalise_deployment_environment
from wevra.web.forms.csrf import CsrfProtector

CSRF_TOKEN_SECRET_BYTES = 32
GENERATE_LOCAL_CSRF_SECRET = "__generate-local-csrf-secret__"
logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class CsrfSettings:
    token_secret: str = GENERATE_LOCAL_CSRF_SECRET
    cookie_secure: bool | str | None = None
    deployment_environment: DeploymentEnvironment | str | None = None

    def __post_init__(self) -> None:
        deployment_environment = normalise_deployment_environment(
            self.deployment_environment
        )
        cookie_secure = _normalise_optional_bool(
            self.cookie_secure,
            "csrf_cookie_secure",
        )
        cookie_secure = (
            deployment_environment != "local"
            if cookie_secure is None
            else cookie_secure
        )
        token_secret_configured = _token_secret_is_configured(self.token_secret)
        if deployment_environment != "local" and not token_secret_configured:
            raise ConfigurationError(
                "Non-local deployments must configure a stable CSRF token secret."
            )
        if deployment_environment != "local" and not cookie_secure:
            raise ConfigurationError(
                "Non-local deployments must use secure CSRF cookies."
            )
        token_secret = self.token_secret
        if not token_secret_configured:
            logger.info(
                "Generated startup-local CSRF token secret. Configure "
                "csrf_token_secret for stable tokens across reloads or workers.",
                extra={"deployment_environment": deployment_environment},
            )
            token_secret = token_urlsafe(CSRF_TOKEN_SECRET_BYTES)
        object.__setattr__(self, "deployment_environment", deployment_environment)
        object.__setattr__(self, "cookie_secure", cookie_secure)
        object.__setattr__(self, "token_secret", token_secret)

    def protector(self) -> CsrfProtector:
        return CsrfProtector(
            self.token_secret,
            cookie_secure=bool(self.cookie_secure),
        )


def csrf_settings_from_config(
    app_config: dict[str, Any],
    web_config: dict[str, Any],
) -> CsrfSettings:
    return CsrfSettings(
        token_secret=_str_value(
            web_config,
            "csrf_token_secret",
            GENERATE_LOCAL_CSRF_SECRET,
        ),
        cookie_secure=web_config.get("csrf_cookie_secure"),
        deployment_environment=_str_value(
            app_config,
            "deployment_environment",
            "local",
        ),
    )


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


def _str_value(config: dict[str, Any], key: str, default: str) -> str:
    value = config.get(key)
    if value is None:
        return default
    if isinstance(value, str) and value.strip():
        return value
    raise ConfigurationError(f"{key} must be a non-blank string.")


__all__ = (
    "CSRF_TOKEN_SECRET_BYTES",
    "CsrfSettings",
    "GENERATE_LOCAL_CSRF_SECRET",
    "csrf_settings_from_config",
)
