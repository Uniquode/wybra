from __future__ import annotations

from dataclasses import dataclass, field
from secrets import token_urlsafe
from typing import Final, Literal, cast
from urllib.parse import urlsplit

from wybra.auth.accounts.passwords import (
    DEFAULT_COMMON_PASSWORD_FRAGMENTS,
    DEFAULT_MINIMUM_CHARACTER_CATEGORIES,
    DEFAULT_MINIMUM_LENGTH,
    DEFAULT_MINIMUM_SCORE,
    DefaultPasswordPolicy,
    PasswordPolicy,
)
from wybra.auth.mfa.totp import (
    DEFAULT_TOTP_ALLOWED_DRIFT,
    DEFAULT_TOTP_PERIOD_SECONDS,
    DEFAULT_TOTP_RECOVERY_WINDOW_SECONDS,
    MAX_TOTP_ALLOWED_DRIFT,
    MAX_TOTP_PERIOD_SECONDS,
    MAX_TOTP_RECOVERY_WINDOW_SECONDS,
)
from wybra.core.exceptions import ConfigurationError

PROVIDER: Final[str] = "provider"
TOTP: Final[str] = "totp"
PASSKEY: Final[str] = "passkey"
TOTP_DISABLED: Final[str] = "disabled"
TOTP_OPT_IN: Final[str] = "opt_in"
TOTP_REQUIRED: Final[str] = "required"
DEFAULT_TOTP_CHALLENGE_EXPIRY_SECONDS: Final[float] = 300.0
TOTP_MODE: Final[str] = "totp_mode"
DEFAULT_PASSKEY_TIMEOUT_SECONDS: Final[int] = 300
DEFAULT_PASSKEY_USER_VERIFICATION_SATISFIES_TOTP: Final[bool] = True
PASSKEY_USER_VERIFICATION_REQUIRED: Final = "required"
PASSKEY_USER_VERIFICATION_PREFERRED: Final = "preferred"
PASSKEY_USER_VERIFICATION_DISCOURAGED: Final = "discouraged"
PASSKEY_ATTESTATION_NONE: Final = "none"
PASSKEY_ATTESTATION_INDIRECT: Final = "indirect"
PASSKEY_ATTESTATION_DIRECT: Final = "direct"
PASSKEY_ATTESTATION_ENTERPRISE: Final = "enterprise"
PASSKEY_DISCOVERABLE_DISCOURAGED: Final = "discouraged"
PASSKEY_DISCOVERABLE_PREFERRED: Final = "preferred"
PASSKEY_DISCOVERABLE_REQUIRED: Final = "required"
PASSKEY_COUNTER_REJECT_REGRESSION: Final = "reject-regression"
VALID_TOTP_MODES: Final[tuple[str, ...]] = (
    TOTP_DISABLED,
    TOTP_OPT_IN,
    TOTP_REQUIRED,
)
VALID_PASSKEY_USER_VERIFICATION_VALUES: Final[
    tuple[Literal["required", "preferred", "discouraged"], ...]
] = (
    PASSKEY_USER_VERIFICATION_REQUIRED,
    PASSKEY_USER_VERIFICATION_PREFERRED,
    PASSKEY_USER_VERIFICATION_DISCOURAGED,
)
VALID_PASSKEY_ATTESTATION_VALUES: Final[
    tuple[Literal["none", "indirect", "direct", "enterprise"], ...]
] = (
    PASSKEY_ATTESTATION_NONE,
    PASSKEY_ATTESTATION_INDIRECT,
    PASSKEY_ATTESTATION_DIRECT,
    PASSKEY_ATTESTATION_ENTERPRISE,
)
VALID_PASSKEY_DISCOVERABLE_VALUES: Final[
    tuple[Literal["discouraged", "preferred", "required"], ...]
] = (
    PASSKEY_DISCOVERABLE_DISCOURAGED,
    PASSKEY_DISCOVERABLE_PREFERRED,
    PASSKEY_DISCOVERABLE_REQUIRED,
)
VALID_PASSKEY_COUNTER_POLICIES: Final[tuple[Literal["reject-regression"], ...]] = (
    PASSKEY_COUNTER_REJECT_REGRESSION,
)

IdentityIntegration = Literal["provider", "totp", "passkey"]
VALID_IDENTITY_INTEGRATIONS: Final[tuple[IdentityIntegration, ...]] = (
    PROVIDER,
    TOTP,
    PASSKEY,
)
VALID_ACCOUNT_CREATION_POLICIES: Final[
    tuple[Literal["admin-created", "public-signup"], ...]
] = (
    "admin-created",
    "public-signup",
)
AccountCreationPolicy = Literal["admin-created", "public-signup"]
ACCOUNT_CREATION_POLICY_ERROR: Final = (
    "Account creation policy must be one of: "
    + ", ".join(VALID_ACCOUNT_CREATION_POLICIES)
    + "."
)

_GENERATE_LOCAL_SECRET = "__generate-local-identity-secret__"
DEFAULT_SESSION_COOKIE_NAME: Final = "wybra_session"


def identity_env_setting_name(integration: IdentityIntegration) -> str:
    return f"{integration.upper()}_ENABLED"


def is_generate_local_identity_secret(value: str) -> bool:
    return value == _GENERATE_LOCAL_SECRET


@dataclass(frozen=True, slots=True)
class IdentityOptions:
    account_creation_policy: AccountCreationPolicy = "admin-created"
    session_cookie_name: str = DEFAULT_SESSION_COOKIE_NAME
    # Force secure cookies for static transports that cannot inspect a request.
    # Defaults to False so ordinary HTTP development remains possible; host
    # applications should require True for non-local deployments.
    session_cookie_force_secure: bool = False
    session_lifetime_seconds: int = 2_592_000
    reset_password_token_secret: str = _GENERATE_LOCAL_SECRET
    verification_token_secret: str = _GENERATE_LOCAL_SECRET
    provider_enabled: bool = False
    totp_mode: Literal["disabled", "opt_in", "required"] = TOTP_DISABLED
    passkey_enabled: bool = False
    passkey_rp_id: str = ""
    passkey_rp_name: str = ""
    passkey_allowed_origins: tuple[str, ...] = ()
    passkey_timeout_seconds: int = DEFAULT_PASSKEY_TIMEOUT_SECONDS
    passkey_user_verification: Literal["required", "preferred", "discouraged"] = (
        PASSKEY_USER_VERIFICATION_PREFERRED
    )
    passkey_user_verification_satisfies_totp: bool = (
        DEFAULT_PASSKEY_USER_VERIFICATION_SATISFIES_TOTP
    )
    passkey_attestation: Literal["none", "indirect", "direct", "enterprise"] = (
        PASSKEY_ATTESTATION_NONE
    )
    passkey_discoverable_credentials: Literal[
        "discouraged", "preferred", "required"
    ] = PASSKEY_DISCOVERABLE_PREFERRED
    passkey_counter_policy: Literal["reject-regression"] = (
        PASSKEY_COUNTER_REJECT_REGRESSION
    )
    totp_allowed_drift: int = DEFAULT_TOTP_ALLOWED_DRIFT
    totp_period_seconds: int = DEFAULT_TOTP_PERIOD_SECONDS
    totp_challenge_expiry_seconds: float = DEFAULT_TOTP_CHALLENGE_EXPIRY_SECONDS
    totp_recovery_window_seconds: int = DEFAULT_TOTP_RECOVERY_WINDOW_SECONDS
    password_minimum_length: int = DEFAULT_MINIMUM_LENGTH
    password_minimum_strength: float = DEFAULT_MINIMUM_SCORE
    password_minimum_character_categories: int = DEFAULT_MINIMUM_CHARACTER_CATEGORIES
    password_common_fragments: tuple[str, ...] = DEFAULT_COMMON_PASSWORD_FRAGMENTS
    password_policy: PasswordPolicy | None = None
    token_secrets_configured: bool = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._validate_totp_mode()
        self._validate_totp_settings()
        self._validate_passkey_settings()

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
            self.reset_password_token_secret,
        )
        verification_secret_configured = not is_generate_local_identity_secret(
            self.verification_token_secret,
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
        if integration not in VALID_IDENTITY_INTEGRATIONS:
            raise ConfigurationError(
                f"Unknown identity integration: {integration}. Valid values are: "
                f"{', '.join(VALID_IDENTITY_INTEGRATIONS)}"
            )

        if integration == TOTP:
            return self.totp_mode != TOTP_DISABLED

        return cast(bool, getattr(self, f"{integration}_enabled"))

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

    def _validate_totp_mode(self) -> None:
        if self.totp_mode not in VALID_TOTP_MODES:
            raise ConfigurationError(
                "TOTP mode must be one of: disabled, opt_in, required."
            )

    @staticmethod
    def _reject_blank_secret(label: str, value: str) -> None:
        if not is_generate_local_identity_secret(value) and not value.strip():
            raise ConfigurationError(f"{label} must not be blank.")

    def _validate_totp_settings(self) -> None:
        if self.totp_allowed_drift < 0:
            raise ConfigurationError(
                "TOTP allowed drift must be a non-negative integer."
            )
        if self.totp_allowed_drift > MAX_TOTP_ALLOWED_DRIFT:
            raise ConfigurationError(
                f"TOTP allowed drift must not exceed {MAX_TOTP_ALLOWED_DRIFT}."
            )

        if self.totp_period_seconds <= 0:
            raise ConfigurationError("TOTP period must be a positive integer.")
        if self.totp_period_seconds > MAX_TOTP_PERIOD_SECONDS:
            raise ConfigurationError(
                f"TOTP period must not exceed {MAX_TOTP_PERIOD_SECONDS} seconds."
            )

        if self.totp_challenge_expiry_seconds <= 0:
            raise ConfigurationError(
                "TOTP challenge expiry must be a positive number of seconds."
            )

        if self.totp_recovery_window_seconds <= 0:
            raise ConfigurationError(
                "TOTP recovery window must be a positive number of seconds."
            )
        if self.totp_recovery_window_seconds > MAX_TOTP_RECOVERY_WINDOW_SECONDS:
            raise ConfigurationError(
                "TOTP recovery window must not exceed "
                f"{MAX_TOTP_RECOVERY_WINDOW_SECONDS} seconds."
            )

    def _validate_passkey_settings(self) -> None:
        object.__setattr__(
            self,
            "passkey_allowed_origins",
            self._normalise_passkey_allowed_origins(),
        )

        if self.passkey_timeout_seconds <= 0:
            raise ConfigurationError(
                "Passkey timeout must be a positive number of seconds."
            )

        if self.passkey_user_verification not in VALID_PASSKEY_USER_VERIFICATION_VALUES:
            raise ConfigurationError(
                "Passkey user verification must be one of: "
                f"{', '.join(VALID_PASSKEY_USER_VERIFICATION_VALUES)}."
            )

        if not isinstance(self.passkey_user_verification_satisfies_totp, bool):
            raise ConfigurationError(
                "Passkey user verification TOTP policy must be a boolean."
            )

        if self.passkey_attestation not in VALID_PASSKEY_ATTESTATION_VALUES:
            raise ConfigurationError(
                "Passkey attestation must be one of: "
                f"{', '.join(VALID_PASSKEY_ATTESTATION_VALUES)}."
            )

        if (
            self.passkey_discoverable_credentials
            not in VALID_PASSKEY_DISCOVERABLE_VALUES
        ):
            raise ConfigurationError(
                "Passkey discoverable credentials must be one of: "
                f"{', '.join(VALID_PASSKEY_DISCOVERABLE_VALUES)}."
            )

        if self.passkey_counter_policy not in VALID_PASSKEY_COUNTER_POLICIES:
            raise ConfigurationError(
                "Passkey counter policy must be one of: "
                f"{', '.join(VALID_PASSKEY_COUNTER_POLICIES)}."
            )

        if not self.passkey_enabled:
            return

        if not self.passkey_rp_id.strip():
            raise ConfigurationError("Passkey relying-party ID must be configured.")
        if _origin_like(self.passkey_rp_id):
            raise ConfigurationError(
                "Passkey relying-party ID must be a domain, not a URL."
            )

        if not self.passkey_rp_name.strip():
            raise ConfigurationError("Passkey relying-party name must be configured.")

        if not self.passkey_allowed_origins:
            raise ConfigurationError("Passkey allowed origins must be configured.")

        for origin in self.passkey_allowed_origins:
            _validate_passkey_origin(origin)

    def _normalise_passkey_allowed_origins(self) -> tuple[str, ...]:
        try:
            origins = tuple(self.passkey_allowed_origins)
        except TypeError as exc:
            raise ConfigurationError(
                "Passkey allowed origins must be a list of origin strings."
            ) from exc

        if not all(isinstance(origin, str) for origin in origins):
            raise ConfigurationError(
                "Passkey allowed origins must be a list of origin strings."
            )

        return tuple(origin.strip().rstrip("/") for origin in origins if origin.strip())


def _origin_like(value: str) -> bool:
    parsed = urlsplit(value)
    return bool(parsed.scheme or parsed.netloc or "/" in value)


def _validate_passkey_origin(origin: str) -> None:
    parsed = urlsplit(origin)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ConfigurationError(
            "Passkey allowed origins must include scheme and host."
        )
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise ConfigurationError(
            "Passkey allowed origins must not include a path, query, or fragment."
        )
