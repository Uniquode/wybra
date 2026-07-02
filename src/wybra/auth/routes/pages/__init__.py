"""Authentication page route package."""

from ..paths import normalise_return_to
from .account import (
    account,
    disable_password_login,
    logout,
    password_reset,
    password_reset_confirm,
    security,
    signup,
    unlink_apple_provider,
    unlink_github_provider,
    unlink_google_provider,
    verify,
    verify_confirm,
)
from .api import current_user_api, current_user_state
from .login import login
from .passkeys import (
    passkey_login_complete,
    passkey_login_options,
    passkey_register_complete,
    passkey_register_options,
    revoke_passkey,
)
from .shared import account_router, api_router
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
    "disable_password_login",
    "login",
    "logout",
    "module_routers",
    "normalise_return_to",
    "password_reset",
    "password_reset_confirm",
    "passkey_login_complete",
    "passkey_login_options",
    "passkey_register_complete",
    "passkey_register_options",
    "regenerate_totp_recovery_codes",
    "reset_totp",
    "revoke_passkey",
    "security",
    "signup",
    "totp_setup",
    "unlink_apple_provider",
    "unlink_github_provider",
    "unlink_google_provider",
    "verify",
    "verify_confirm",
]
