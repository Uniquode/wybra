"""Configuration definitions for web-facing security policy."""

from __future__ import annotations

from typing import Final, cast

from wybra.config.transforms import to_bool
from wybra.config.types import ConfigDef, ConfigField, ConfigGroup
from wybra.security.headers import (
    CrossOriginOpenerPolicy,
    validate_cross_origin_opener_policy,
)


def _coop_value(value: object) -> CrossOriginOpenerPolicy | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("app.security.cross_origin_opener_policy must be a string.")
    policy = cast(CrossOriginOpenerPolicy, value)
    validate_cross_origin_opener_policy(policy)
    return policy


module_config: Final = ConfigDef(
    {
        "app.security": ConfigGroup(
            fields=(
                ConfigField(
                    name="cross_origin_opener_policy",
                    default="same-origin",
                    transform=_coop_value,
                ),
            ),
        ),
        "app.assets.cors": ConfigGroup(
            fields=(
                ConfigField(name="enabled", default=False, transform=to_bool),
                ConfigField(name="allow_origins", default=("*",)),
                ConfigField(name="allow_methods", default=("GET", "HEAD")),
                ConfigField(name="allow_headers", default=()),
                ConfigField(name="expose_headers", default=()),
                ConfigField(name="allow_credentials", default=False, transform=to_bool),
                ConfigField(name="max_age", default=600),
            ),
        ),
    }
)
