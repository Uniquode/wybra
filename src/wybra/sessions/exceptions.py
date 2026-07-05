from __future__ import annotations

from wybra.core.exceptions import ConfigurationError


class SessionsError(RuntimeError):
    """Base class for Wybra session failures."""


class SessionsConfigurationError(ConfigurationError, SessionsError):
    """Raised when session configuration is invalid."""


class SessionIdentifierError(SessionsError, ValueError):
    """Raised when a session identifier is malformed or unsafe."""


class SessionUnavailableError(SessionsError):
    """Raised when request session state is unavailable."""


class SessionStorageError(SessionsError):
    """Raised when session storage cannot load or persist state."""


__all__ = (
    "SessionIdentifierError",
    "SessionStorageError",
    "SessionUnavailableError",
    "SessionsConfigurationError",
    "SessionsError",
)
