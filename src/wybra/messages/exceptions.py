from __future__ import annotations

from wybra.core.exceptions import ConfigurationError


class MessagesError(RuntimeError):
    """Base class for user-facing alert queue errors."""


class InvalidAlertError(ValueError):
    """Raised when alert input cannot be stored safely."""


class MessageQueueUnavailableError(MessagesError):
    """Raised when a request cannot be mapped to an alert queue."""


class MessageStorageError(MessagesError):
    """Raised when alert storage cannot complete an operation."""


class MessagesConfigurationError(ConfigurationError):
    """Raised when messages configuration is invalid."""


__all__ = (
    "InvalidAlertError",
    "MessageQueueUnavailableError",
    "MessageStorageError",
    "MessagesConfigurationError",
    "MessagesError",
)
