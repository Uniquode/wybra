from dataclasses import dataclass, field
from secrets import token_urlsafe
from typing import Literal

from auth_ext.configuration import ConfigurationError

AccountCreationPolicy = Literal["admin-created"]
IdentityIntegration = Literal["oauth-account-linking", "advanced-authentication"]

_GENERATE_LOCAL_SECRET = "__generate-local-identity-secret__"


def is_generate_local_identity_secret(value: str) -> bool:
    return value == _GENERATE_LOCAL_SECRET


@dataclass(frozen=True, slots=True)
class IdentityOptions:
    account_creation_policy: AccountCreationPolicy = "admin-created"
    session_cookie_name: str = "uniquode_session"
    session_cookie_secure: bool = True
    session_lifetime_seconds: int = 2_592_000
    reset_password_token_secret: str = _GENERATE_LOCAL_SECRET
    verification_token_secret: str = _GENERATE_LOCAL_SECRET
    oauth_account_linking_enabled: bool = False
    advanced_authentication_enabled: bool = False
    token_secrets_configured: bool = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if self.session_lifetime_seconds <= 0:
            raise ConfigurationError(
                "Session lifetime must be a positive number of seconds."
            )

        self._reject_blank_secret(
            "Reset password token secret",
            self.reset_password_token_secret,
        )
        self._reject_blank_secret(
            "Verification token secret",
            self.verification_token_secret,
        )
        reset_secret_configured = not is_generate_local_identity_secret(
            self.reset_password_token_secret
        )
        verification_secret_configured = not is_generate_local_identity_secret(
            self.verification_token_secret
        )

        if not reset_secret_configured:
            object.__setattr__(
                self,
                "reset_password_token_secret",
                token_urlsafe(32),
            )
        if not verification_secret_configured:
            object.__setattr__(
                self,
                "verification_token_secret",
                token_urlsafe(32),
            )

        object.__setattr__(
            self,
            "token_secrets_configured",
            reset_secret_configured and verification_secret_configured,
        )

    def integration_enabled(self, integration: IdentityIntegration) -> bool:
        if integration == "oauth-account-linking":
            return self.oauth_account_linking_enabled

        return self.advanced_authentication_enabled

    @staticmethod
    def _reject_blank_secret(label: str, value: str) -> None:
        if not is_generate_local_identity_secret(value) and not value.strip():
            raise ConfigurationError(f"{label} must not be blank.")
