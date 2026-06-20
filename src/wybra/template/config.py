from __future__ import annotations

from typing import Final

from wybra.config import ConfigDef, ConfigField, ConfigGroup, to_bool

ENV_REQUEST_CONTEXT_ENABLED: Final = "REQUEST_CONTEXT_ENABLED"
ENV_TEMPLATE_ROOT: Final = "TEMPLATE_ROOT"

module_config: Final = ConfigDef(
    {
        "app.templates": ConfigGroup(
            fields=(
                ConfigField(name="auto_reload", transform=to_bool),
                ConfigField(name="cache_size"),
                ConfigField(name="root", env=ENV_TEMPLATE_ROOT),
                ConfigField(
                    name="request_context_enabled",
                    default=True,
                    env=ENV_REQUEST_CONTEXT_ENABLED,
                    transform=to_bool,
                ),
            ),
        ),
    }
)

__all__ = (
    "ENV_REQUEST_CONTEXT_ENABLED",
    "ENV_TEMPLATE_ROOT",
    "module_config",
)
