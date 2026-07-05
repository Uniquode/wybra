"""Queued user-facing alert capability."""

from __future__ import annotations

from importlib import import_module
from typing import Any

_EXPORT_MODULES = {
    "ALERT_SEVERITIES": "wybra.messages.records",
    "ERROR_ALERT": "wybra.messages.records",
    "SUCCESS_ALERT": "wybra.messages.records",
    "WARNING_ALERT": "wybra.messages.records",
    "AlertRecord": "wybra.messages.records",
    "AlertSeverity": "wybra.messages.records",
    "DefaultMessagesCapability": "wybra.messages.capabilities",
    "InvalidAlertError": "wybra.messages.exceptions",
    "MESSAGES_CONFIG_SECTION": "wybra.messages.config",
    "MessageQueueUnavailableError": "wybra.messages.exceptions",
    "MessageStorageBackend": "wybra.messages.config",
    "MessageStorageError": "wybra.messages.exceptions",
    "MessagesCapability": "wybra.messages.capabilities",
    "MessagesConfigurationError": "wybra.messages.exceptions",
    "MessagesError": "wybra.messages.exceptions",
    "MessagesSettings": "wybra.messages.settings",
    "module_config": "wybra.messages.config",
    "setup_site": "wybra.messages.setup",
    "post_setup_site": "wybra.messages.setup",
    "validate_alerts": "wybra.messages.validation",
    "validation_targets": "wybra.messages.validation",
}

provides_messages_capability = True


def __getattr__(name: str) -> Any:
    module_name = _EXPORT_MODULES.get(name)
    if module_name is None:
        raise AttributeError(f"module 'wybra.messages' has no attribute {name!r}")
    value = getattr(import_module(module_name), name)
    globals()[name] = value
    return value


__all__ = [
    "ALERT_SEVERITIES",
    "ERROR_ALERT",
    "SUCCESS_ALERT",
    "WARNING_ALERT",
    "AlertRecord",
    "AlertSeverity",
    "DefaultMessagesCapability",
    "InvalidAlertError",
    "MESSAGES_CONFIG_SECTION",
    "MessageQueueUnavailableError",
    "MessageStorageBackend",
    "MessageStorageError",
    "MessagesCapability",
    "MessagesConfigurationError",
    "MessagesError",
    "MessagesSettings",
    "module_config",
    "post_setup_site",
    "provides_messages_capability",
    "setup_site",
    "validate_alerts",
    "validation_targets",
]
