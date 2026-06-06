"""Runtime authorisation and effective-scope helpers.

Hosts may import these helpers when views, templates, or API handlers need the
current user's resolved groups, scopes, or active-account status.
"""

from wevra.auth.authorisation.effective import (
    effective_scope_sets_for_user,
    is_user_effectively_active,
)

__all__ = [
    "effective_scope_sets_for_user",
    "is_user_effectively_active",
]
