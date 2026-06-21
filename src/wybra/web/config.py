from __future__ import annotations

from typing import Final

from wybra.config import ConfigDef

WEB_CONFIG_SECTION: Final = "wybra.web"

module_config: Final = ConfigDef({})


__all__ = (
    "WEB_CONFIG_SECTION",
    "module_config",
)
