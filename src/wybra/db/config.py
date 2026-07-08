from __future__ import annotations

from typing import Final

from wybra.config import ConfigDef, ConfigField, ConfigGroup

DATABASE_CONFIG_SECTION: Final = "app.database"
ENV_DATABASE_URL: Final = "DATABASE_URL"
ENV_MIGRATIONS_ROOT: Final = "MIGRATIONS_ROOT"

module_config: Final = ConfigDef(
    {
        "app": ConfigGroup(
            fields=(
                ConfigField(name="database_url", env=ENV_DATABASE_URL),
                ConfigField(name="migrations_root", env=ENV_MIGRATIONS_ROOT),
            ),
        ),
        DATABASE_CONFIG_SECTION: ConfigGroup(
            fields=(
                ConfigField(name="backend"),
                ConfigField(name="host"),
                ConfigField(name="port"),
                ConfigField(name="database"),
                ConfigField(name="options"),
                ConfigField(name="credential_source"),
                ConfigField(name="user"),
                ConfigField(name="password"),
                ConfigField(name="user_key"),
                ConfigField(name="password_key"),
                ConfigField(name="sa_user"),
                ConfigField(name="sa_password"),
                ConfigField(name="sa_user_key"),
                ConfigField(name="sa_password_key"),
            ),
        ),
    }
)

__all__ = (
    "DATABASE_CONFIG_SECTION",
    "ENV_DATABASE_URL",
    "ENV_MIGRATIONS_ROOT",
    "module_config",
)
