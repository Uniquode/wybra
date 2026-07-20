"""Core configuration for optional event delivery."""

from __future__ import annotations

from typing import Final

from wybra.config import ConfigDef, ConfigField, ConfigGroup, to_bool

EVENTS_CONFIG_SECTION: Final = "wybra.events"
ENV_WYBRA_EVENT_DELIVERY_ENABLED: Final = "WYBRA_EVENT_DELIVERY_ENABLED"

EVENTS_CONFIG_DEF: Final = ConfigDef(
    {
        EVENTS_CONFIG_SECTION: ConfigGroup(
            fields=(
                ConfigField(
                    name="enabled",
                    default=False,
                    env=ENV_WYBRA_EVENT_DELIVERY_ENABLED,
                    transform=to_bool,
                ),
            )
        )
    }
)

__all__ = (
    "ENV_WYBRA_EVENT_DELIVERY_ENABLED",
    "EVENTS_CONFIG_DEF",
    "EVENTS_CONFIG_SECTION",
)
