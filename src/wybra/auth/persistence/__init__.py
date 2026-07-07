"""Authentication persistence helpers and session-token strategy factories.

Hosts may import these factories when wiring Wybra auth persistence against an
already configured SQLAlchemy session factory.
"""

from importlib import import_module
from typing import Any

_EXPORTS = {
    "SqlAlchemyAuthPersistenceCapability": "wybra.auth.persistence.strategies",
    "SqlAlchemyAuthPersistenceScope": "wybra.auth.persistence.strategies",
    "PersistentSessionTokenStrategy": "wybra.auth.persistence.strategies",
    "auth_persistence_session": "wybra.auth.persistence.strategies",
    "create_access_token_database": "wybra.auth.persistence.strategies",
    "create_database_strategy": "wybra.auth.persistence.strategies",
    "create_user_database": "wybra.auth.persistence.strategies",
    "delete_session_token_by_value": "wybra.auth.persistence.strategies",
}

__all__ = sorted(_EXPORTS)


def __getattr__(name: str) -> Any:
    try:
        module_name = _EXPORTS[name]
    except KeyError as exc:
        raise AttributeError(name) from exc

    value = getattr(import_module(module_name), name)
    globals()[name] = value
    return value
