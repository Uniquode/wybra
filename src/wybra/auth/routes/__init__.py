"""Authentication route pages, context providers, and router wiring.

Hosts normally compose the ``module_routers`` route module through configured
application modules rather than importing routers directly.
"""

from wybra.auth.routes.pages import (
    account_router,
    api_router,
    current_user_api,
    current_user_state,
    module_routers,
    normalise_return_to,
)
from wybra.auth.routes.wiring import RouteReplacement, RouterExtensionPlan

__all__ = [
    "RouteReplacement",
    "RouterExtensionPlan",
    "account_router",
    "api_router",
    "current_user_api",
    "current_user_state",
    "module_routers",
    "normalise_return_to",
]
