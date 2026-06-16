from __future__ import annotations

from typing import Final

from wybra.config import ConfigDef, ConfigField, ConfigGroup

ENV_APP_ENV: Final = "APP_ENV"

RUNTIME_CONFIG_DEF: Final = ConfigDef(
    {
        "app": ConfigGroup(
            fields=(
                ConfigField(
                    name="deployment_environment",
                    env=ENV_APP_ENV,
                ),
            ),
        )
    }
)

__all__ = (
    "ENV_APP_ENV",
    "RUNTIME_CONFIG_DEF",
)
