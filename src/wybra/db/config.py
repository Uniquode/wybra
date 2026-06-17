from __future__ import annotations

from typing import Final

from wybra.config import ConfigDef, ConfigField, ConfigGroup

ENV_DATABASE_URL: Final = "DATABASE_URL"
ENV_MIGRATIONS_ROOT: Final = "MIGRATIONS_ROOT"

module_config: Final = ConfigDef(
    {
        "app": ConfigGroup(
            fields=(
                ConfigField(name="database_url", env=ENV_DATABASE_URL),
                ConfigField(name="migrations_root", env=ENV_MIGRATIONS_ROOT),
            ),
        )
    }
)

__all__ = (
    "ENV_DATABASE_URL",
    "ENV_MIGRATIONS_ROOT",
    "module_config",
)
