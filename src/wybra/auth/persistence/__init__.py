"""Authentication persistence helpers and database strategy factories.

Hosts may import these factories when wiring FastAPI Users persistence against
an already configured SQLAlchemy session factory.
"""

from wybra.auth.persistence.strategies import (
    create_access_token_database,
    create_database_strategy,
    create_user_database,
    delete_session_token_by_value,
)

__all__ = [
    "create_access_token_database",
    "create_database_strategy",
    "create_user_database",
    "delete_session_token_by_value",
]
