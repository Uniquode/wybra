from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from wybra.config import ConfigService, MappingConfigSource
from wybra.forms.settings import FormsSettings
from wybra.services.secrets import KEYCHAIN_SOURCE


def forms_keychain_secret_references(
    raw_config: Mapping[str, Mapping[str, Any]],
) -> tuple[str, ...]:
    config = ConfigService(
        [MappingConfigSource(raw_config)],
        config_defs=(FormsSettings.module_config,),
        discover_module_config=False,
    )
    settings = FormsSettings.load_settings(config)
    return tuple(
        reference.key
        for reference in settings.credential_references()
        if reference.source == KEYCHAIN_SOURCE
    )


__all__ = ("forms_keychain_secret_references",)
