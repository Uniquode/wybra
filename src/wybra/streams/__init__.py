"""Provider-neutral live opaque-byte publication and subscription."""

from __future__ import annotations

from importlib import import_module
from typing import Any

_EXPORT_MODULES = {
    "DefaultStreamsCapability": "wybra.streams.capabilities",
    "MAX_STREAM_PAYLOAD_BYTES": "wybra.streams.contracts",
    "StreamSubscription": "wybra.streams.contracts",
    "StreamSubscriptionClosedError": "wybra.streams.exceptions",
    "StreamsCapability": "wybra.streams.contracts",
    "StreamsConfigurationDiagnostic": "wybra.streams.settings",
    "StreamsError": "wybra.streams.exceptions",
    "StreamsProviderError": "wybra.streams.exceptions",
    "StreamsSettings": "wybra.streams.settings",
    "module_config": "wybra.streams.config",
    "setup_site": "wybra.streams.setup",
}

provides_streams_capability = True


def __getattr__(name: str) -> Any:
    module_name = _EXPORT_MODULES.get(name)
    if module_name is None:
        raise AttributeError(f"module 'wybra.streams' has no attribute {name!r}")
    value = getattr(import_module(module_name), name)
    globals()[name] = value
    return value


__all__ = (
    "DefaultStreamsCapability",
    "MAX_STREAM_PAYLOAD_BYTES",
    "StreamSubscription",
    "StreamSubscriptionClosedError",
    "StreamsCapability",
    "StreamsConfigurationDiagnostic",
    "StreamsError",
    "StreamsProviderError",
    "StreamsSettings",
    "module_config",
    "provides_streams_capability",
    "setup_site",
)
