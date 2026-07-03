from __future__ import annotations

from typing import Final

from wybra.config import ConfigDef, ConfigField, ConfigGroup, to_bool
from wybra.services.secrets import SecretSource, normalise_secret_source

ENV_CSRF_SECRET: Final = "CSRF_SECRET"
ENV_CSRF_SECURE: Final = "CSRF_SECURE"
FORMS_CONFIG_SECTION: Final = "wybra.forms"
CSRF_TOKEN_SECRET_KEY_CURRENT: Final = "auth/forms/csrf-token-secret/current"
GENERATE_LOCAL_CSRF_SECRET: Final = "__generate-local-csrf-secret__"


def to_csrf_token_secret(value: object) -> str:
    """Normalise an explicitly configured CSRF token secret."""
    if not isinstance(value, str):
        raise ValueError("must be a non-blank string.")
    secret = value.strip()
    if not secret:
        raise ValueError("must be a non-blank string.")
    if secret == GENERATE_LOCAL_CSRF_SECRET:
        raise ValueError(
            f"{GENERATE_LOCAL_CSRF_SECRET!r} is reserved for internal use; "
            f"unset {ENV_CSRF_SECRET} to request automatic CSRF secret generation."
        )
    return secret


def to_optional_bool(value: object) -> bool | None:
    if value is None:
        return None
    return to_bool(value)


def to_optional_secret_source(value: object) -> SecretSource | None:
    if value is None:
        return None
    return normalise_secret_source(value, name="CSRF token secret source")


def to_optional_non_blank_string(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str) and value.strip():
        return value.strip()
    raise ValueError("must be a non-blank string when configured.")


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
                    env=ENV_CSRF_SECRET,
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
            ),
        ),
    }
)


__all__ = (
    "ENV_CSRF_SECRET",
    "ENV_CSRF_SECURE",
    "CSRF_TOKEN_SECRET_KEY_CURRENT",
    "FORMS_CONFIG_SECTION",
    "GENERATE_LOCAL_CSRF_SECRET",
    "module_config",
    "to_csrf_token_secret",
    "to_optional_non_blank_string",
    "to_optional_bool",
    "to_optional_secret_source",
)
