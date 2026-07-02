from typing import Final, Literal, TypeGuard

ProviderOAuthPurpose = Literal["login", "link"]

PROVIDER_OAUTH_LOGIN_PURPOSE: Final[ProviderOAuthPurpose] = "login"
PROVIDER_OAUTH_LINK_PURPOSE: Final[ProviderOAuthPurpose] = "link"
PROVIDER_OAUTH_PURPOSES: Final = frozenset(
    (PROVIDER_OAUTH_LOGIN_PURPOSE, PROVIDER_OAUTH_LINK_PURPOSE)
)

PROVIDER_EMAIL_MATCH_USER_UNRESOLVED_REASON: Final = (
    "Provider email-match user could not be resolved."
)
PROVIDER_LINKING_USER_UNAVAILABLE_REASON: Final = (
    "Linking user is inactive or unavailable."
)


def is_provider_oauth_purpose(value: object) -> TypeGuard[ProviderOAuthPurpose]:
    return isinstance(value, str) and value in PROVIDER_OAUTH_PURPOSES


def provider_invalid_email_reason(provider_label: str) -> str:
    return f"{provider_label} account email is invalid."


def provider_invalid_linking_state_reason(provider_label: str) -> str:
    return f"{provider_label} linking session is invalid."


def provider_missing_access_token_reason(provider_label: str) -> str:
    return f"{provider_label} access token is missing."
