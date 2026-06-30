"""Authentication page route package."""

from .account import (
    account,
    logout,
    password_reset,
    password_reset_confirm,
    security,
    signup,
    verify,
    verify_confirm,
)
from .api import current_user_api, current_user_state
from .login import login
from .shared import account_router, api_router, normalise_return_to
from .totp_management import (
    disable_totp,
    regenerate_totp_recovery_codes,
    reset_totp,
    totp_setup,
)

module_routers = {
    "account": account_router,
    "api": api_router,
}

__all__ = [
    "account",
    "account_router",
    "api_router",
    "current_user_api",
    "current_user_state",
    "disable_totp",
    "login",
    "logout",
    "module_routers",
    "normalise_return_to",
    "password_reset",
    "password_reset_confirm",
    "regenerate_totp_recovery_codes",
    "reset_totp",
    "security",
    "signup",
    "totp_setup",
    "verify",
    "verify_confirm",
]
