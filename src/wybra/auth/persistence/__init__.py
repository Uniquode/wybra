"""Authentication persistence helpers and Tortoise-backed stores."""

from importlib import import_module
from typing import Any

_EXPORTS = {
    "PersistentSessionTokenStrategy": "wybra.auth.persistence.strategies",
    "TortoiseAuthPersistenceCapability": "wybra.auth.persistence.strategies",
    "TortoiseAuthPersistenceScope": "wybra.auth.persistence.strategies",
    "auth_persistence_scope": "wybra.auth.persistence.strategies",
    "create_session_token_store": "wybra.auth.persistence.strategies",
    "create_session_token_strategy": "wybra.auth.persistence.strategies",
    "create_user_store": "wybra.auth.persistence.strategies",
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
