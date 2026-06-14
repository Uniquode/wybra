from __future__ import annotations

from typing import Final

from wevra.config import ConfigDef, ConfigField, ConfigSection

ENV_APP_ENV: Final = "APP_ENV"

RUNTIME_CONFIG_DEF: Final = ConfigDef(
    {
        "app": ConfigSection(
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
