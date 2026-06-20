from __future__ import annotations

from typing import Final

from wybra.config import ConfigDef, ConfigField, ConfigGroup, to_bool

ENV_CSRF_SECRET: Final = "CSRF_SECRET"
ENV_CSRF_SECURE: Final = "CSRF_SECURE"
GENERATE_LOCAL_CSRF_SECRET: Final = "__generate-local-csrf-secret__"
WEB_CONFIG_SECTION: Final = "wybra.web"


def to_csrf_token_secret(value: object) -> str:
    """Normalise an explicitly configured CSRF token secret.

    ``GENERATE_LOCAL_CSRF_SECRET`` is reserved across all config sources as the
    internal marker for omitted local-development secrets.
    """
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


module_config: Final = ConfigDef(
    {
        WEB_CONFIG_SECTION: ConfigGroup(
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
            ),
        ),
    }
)


__all__ = (
    "ENV_CSRF_SECRET",
    "ENV_CSRF_SECURE",
    "GENERATE_LOCAL_CSRF_SECRET",
    "WEB_CONFIG_SECTION",
    "module_config",
    "to_csrf_token_secret",
    "to_optional_bool",
)
