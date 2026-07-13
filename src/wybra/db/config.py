from __future__ import annotations

from typing import Final

from wybra.config import ConfigDef, ConfigField, ConfigGroup

DATABASE_CONFIG_SECTION: Final = "app.database"
AWS_CONFIG_SECTION: Final = "app.aws"
DATABASE_AWS_CONFIG_SECTION: Final = "app.database.aws"
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
                ConfigField(name="sa_database"),
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
        AWS_CONFIG_SECTION: ConfigGroup(
            fields=(
                ConfigField(name="region"),
                ConfigField(name="profile"),
                ConfigField(name="account_id"),
                ConfigField(name="partition"),
                ConfigField(name="role_arn"),
                ConfigField(name="role_session_name"),
                ConfigField(name="external_id"),
                ConfigField(name="external_id_source"),
                ConfigField(name="external_id_key"),
                ConfigField(name="sso_region"),
                ConfigField(name="sso_account_id"),
                ConfigField(name="sso_role_name"),
                ConfigField(name="sso_start_url"),
            ),
        ),
        DATABASE_AWS_CONFIG_SECTION: ConfigGroup(
            fields=(
                ConfigField(name="managed"),
                ConfigField(name="region"),
                ConfigField(name="profile"),
                ConfigField(name="account_id"),
                ConfigField(name="partition"),
                ConfigField(name="role_arn"),
                ConfigField(name="role_session_name"),
                ConfigField(name="external_id"),
                ConfigField(name="external_id_source"),
                ConfigField(name="external_id_key"),
                ConfigField(name="sso_region"),
                ConfigField(name="sso_account_id"),
                ConfigField(name="sso_role_name"),
                ConfigField(name="sso_start_url"),
                ConfigField(name="db_instance_identifier"),
                ConfigField(name="cluster_identifier"),
                ConfigField(name="engine"),
                ConfigField(name="endpoint"),
                ConfigField(name="port"),
            ),
        ),
    }
)

__all__ = (
    "AWS_CONFIG_SECTION",
    "DATABASE_CONFIG_SECTION",
    "DATABASE_AWS_CONFIG_SECTION",
    "ENV_DATABASE_URL",
    "ENV_MIGRATIONS_ROOT",
    "module_config",
)
