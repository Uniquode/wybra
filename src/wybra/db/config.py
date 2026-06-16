from __future__ import annotations

from pathlib import Path
from typing import Final

from wybra.config import ConfigDef, ConfigField, ConfigGroup

DEFAULT_ALEMBIC_CONFIG: Final = Path("alembic.ini")
ENV_ALEMBIC_CONFIG: Final = "ALEMBIC_CONFIG"
ENV_DATABASE_URL: Final = "DATABASE_URL"
ENV_MIGRATIONS_ROOT: Final = "MIGRATIONS_ROOT"

module_config: Final = ConfigDef(
    {
        "app": ConfigGroup(
            fields=(
                ConfigField(name="alembic_config", env=ENV_ALEMBIC_CONFIG),
                ConfigField(name="database_url", env=ENV_DATABASE_URL),
                ConfigField(name="migrations_root", env=ENV_MIGRATIONS_ROOT),
            ),
        )
    }
)

__all__ = (
    "DEFAULT_ALEMBIC_CONFIG",
    "ENV_ALEMBIC_CONFIG",
    "ENV_DATABASE_URL",
    "ENV_MIGRATIONS_ROOT",
    "module_config",
)
