from dataclasses import dataclass, field
from secrets import token_urlsafe
from typing import Final, Literal, cast

from auth_ext.configuration import ConfigurationError
from auth_ext.passwords import (
    DEFAULT_COMMON_PASSWORD_FRAGMENTS,
    DEFAULT_MINIMUM_CHARACTER_CATEGORIES,
    DEFAULT_MINIMUM_LENGTH,
    DEFAULT_MINIMUM_SCORE,
    DefaultPasswordPolicy,
    PasswordPolicy,
)

AccountCreationPolicy = Literal["admin-created", "public-signup"]
IdentityIntegration = Literal["oauth-account-linking", "advanced-authentication"]
VALID_ACCOUNT_CREATION_POLICIES: Final[tuple[AccountCreationPolicy, ...]] = (
    "admin-created",
    "public-signup",
)
ACCOUNT_CREATION_POLICY_ERROR: Final = (
    "Account creation policy must be one of: "
    + ", ".join(VALID_ACCOUNT_CREATION_POLICIES)
    + "."
)

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
    password_minimum_length: int = DEFAULT_MINIMUM_LENGTH
    password_minimum_strength: float = DEFAULT_MINIMUM_SCORE
    password_minimum_character_categories: int = DEFAULT_MINIMUM_CHARACTER_CATEGORIES
    password_common_fragments: tuple[str, ...] = DEFAULT_COMMON_PASSWORD_FRAGMENTS
    password_policy: PasswordPolicy | None = None
    token_secrets_configured: bool = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if self.account_creation_policy not in VALID_ACCOUNT_CREATION_POLICIES:
            raise ConfigurationError(ACCOUNT_CREATION_POLICY_ERROR)

        if self.session_lifetime_seconds <= 0:
            raise ConfigurationError(
                "Session lifetime must be a positive number of seconds."
            )

        self._configure_password_policy()
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

    def resolved_password_policy(self) -> PasswordPolicy:
        return cast(PasswordPolicy, self.password_policy)

    def _configure_password_policy(self) -> None:
        if self.password_minimum_length <= 0:
            raise ConfigurationError("Password minimum length must be positive.")

        if not 0 <= self.password_minimum_strength <= 1:
            raise ConfigurationError(
                "Password minimum strength must be between 0 and 1."
            )

        if self.password_minimum_character_categories <= 0:
            raise ConfigurationError(
                "Password minimum character categories must be positive."
            )

        common_fragments = self._normalise_password_common_fragments()
        object.__setattr__(self, "password_common_fragments", common_fragments)

        if self.password_policy is None:
            object.__setattr__(
                self,
                "password_policy",
                DefaultPasswordPolicy(
                    minimum_length=self.password_minimum_length,
                    minimum_score=self.password_minimum_strength,
                    minimum_character_categories=(
                        self.password_minimum_character_categories
                    ),
                    common_fragments=common_fragments,
                ),
            )

    def _normalise_password_common_fragments(self) -> tuple[str, ...]:
        try:
            common_fragments = tuple(self.password_common_fragments)
        except TypeError as exc:
            raise ConfigurationError(
                "Password common fragments must be a list of strings."
            ) from exc

        if not all(isinstance(fragment, str) for fragment in common_fragments):
            raise ConfigurationError(
                "Password common fragments must be a list of strings."
            )

        return common_fragments

    @staticmethod
    def _reject_blank_secret(label: str, value: str) -> None:
        if not is_generate_local_identity_secret(value) and not value.strip():
            raise ConfigurationError(f"{label} must not be blank.")
