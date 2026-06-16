from __future__ import annotations

from typing import Final

from wevra.config import ConfigDef, ConfigField, ConfigGroup, to_bool

ENV_CSRF_SECRET: Final = "CSRF_SECRET"
ENV_CSRF_SECURE: Final = "CSRF_SECURE"
ENV_REQUEST_CONTEXT_ENABLED: Final = "REQUEST_CONTEXT_ENABLED"
ENV_STATIC_ROOT: Final = "STATIC_ROOT"
ENV_STATIC_SERVE: Final = "STATIC_SERVE"
ENV_STATIC_URL: Final = "STATIC_URL"
ENV_TEMPLATE_ROOT: Final = "TEMPLATE_ROOT"

module_config: Final = ConfigDef(
    {
        "wevra.web": ConfigGroup(
            fields=(
                ConfigField(name="csrf_cookie_secure", env=ENV_CSRF_SECURE),
                ConfigField(name="csrf_token_secret", env=ENV_CSRF_SECRET),
                ConfigField(
                    name="request_context_enabled",
                    env=ENV_REQUEST_CONTEXT_ENABLED,
                    transform=to_bool,
                ),
            ),
        ),
        "app.static": ConfigGroup(
            fields=(
                ConfigField(name="root", env=ENV_STATIC_ROOT),
                ConfigField(
                    name="serve",
                    default=True,
                    env=ENV_STATIC_SERVE,
                    transform=to_bool,
                ),
                ConfigField(name="url_path", env=ENV_STATIC_URL),
            ),
        ),
        "app.templates": ConfigGroup(
            fields=(
                ConfigField(name="auto_reload"),
                ConfigField(name="cache_size"),
                ConfigField(name="root", env=ENV_TEMPLATE_ROOT),
            ),
        ),
    }
)

__all__ = (
    "ENV_CSRF_SECRET",
    "ENV_CSRF_SECURE",
    "ENV_REQUEST_CONTEXT_ENABLED",
    "ENV_STATIC_ROOT",
    "ENV_STATIC_SERVE",
    "ENV_STATIC_URL",
    "ENV_TEMPLATE_ROOT",
    "module_config",
)
