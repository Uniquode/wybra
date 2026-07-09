from __future__ import annotations

from typing import Final

from wybra.config import (
    ConfigDef,
    ConfigField,
    ConfigGroup,
    to_bool,
    to_non_blank_string,
    to_optional_non_blank_string,
)
from wybra.config import (
    to_optional_positive_float as _to_optional_positive_float,
)
from wybra.services.secrets import SecretSource, normalise_secret_source

ENV_CSRF_SECRET_KEY: Final = "CSRF_SECRET_KEY"
ENV_CSRF_SECURE: Final = "CSRF_SECURE"
FORMS_CONFIG_SECTION: Final = "wybra.forms"
CSRF_TOKEN_SECRET_KEY_CURRENT: Final = "auth/forms/csrf-token-secret/current"
CSRF_TOKEN_SECRET_KEY_PREVIOUS: Final = "auth/forms/csrf-token-secret/previous"


def to_csrf_token_secret(value: object) -> str:
    """Normalise an explicitly configured CSRF token secret."""
    return to_non_blank_string(value)


def to_optional_bool(value: object) -> bool | None:
    if value is None:
        return None
    return to_bool(value)


def to_optional_secret_source(value: object) -> SecretSource | None:
    if value is None:
        return None
    return normalise_secret_source(value, name="CSRF token secret source")


def normalise_optional_positive_float(value: object) -> float | None:
    return _to_optional_positive_float(value)


def to_optional_positive_float(value: object) -> float | None:
    return normalise_optional_positive_float(value)


module_config: Final = ConfigDef(
    {
        FORMS_CONFIG_SECTION: ConfigGroup(
            fields=(
                ConfigField(
                    name="csrf_cookie_secure",
                    default=None,
                    env=ENV_CSRF_SECURE,
                    transform=to_optional_bool,
                ),
                ConfigField(
                    name="csrf_token_secret",
                    env=ENV_CSRF_SECRET_KEY,
                    transform=to_csrf_token_secret,
                ),
                ConfigField(
                    name="csrf_token_secret_source",
                    transform=to_optional_secret_source,
                ),
                ConfigField(
                    name="csrf_token_secret_key",
                    transform=to_optional_non_blank_string,
                ),
                ConfigField(
                    name="csrf_token_secret_previous_key",
                    transform=to_optional_non_blank_string,
                ),
                ConfigField(
                    name="csrf_token_max_age_seconds",
                    transform=to_optional_positive_float,
                ),
            ),
        ),
    }
)


__all__ = (
    "ENV_CSRF_SECRET_KEY",
    "ENV_CSRF_SECURE",
    "CSRF_TOKEN_SECRET_KEY_CURRENT",
    "CSRF_TOKEN_SECRET_KEY_PREVIOUS",
    "FORMS_CONFIG_SECTION",
    "normalise_optional_positive_float",
    "module_config",
    "to_csrf_token_secret",
    "to_optional_non_blank_string",
    "to_optional_positive_float",
    "to_optional_bool",
    "to_optional_secret_source",
)
