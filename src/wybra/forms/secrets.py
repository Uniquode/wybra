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
    reference = settings.csrf_token_secret_reference
    if reference is None:
        return ()
    source, key = reference
    if source != KEYCHAIN_SOURCE:
        return ()
    previous_reference = settings.csrf_token_secret_previous_reference
    if previous_reference is None:
        return (key,)
    previous_source, previous_key = previous_reference
    if previous_source != KEYCHAIN_SOURCE:
        return (key,)
    return (key, previous_key)


__all__ = ("forms_keychain_secret_references",)
