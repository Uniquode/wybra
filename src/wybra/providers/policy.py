from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from wybra.providers.settings import ProviderSettings, provider_name_value


class ProviderPolicyOutcome(StrEnum):
    LINKED_USER = "linked_user"
    LINK_ALLOWED = "link_allowed"
    EMAIL_MATCH_LINK_ALLOWED = "email_match_link_allowed"
    ALREADY_LINKED = "already_linked"
    COLLISION = "collision"
    CREATION_ALLOWED = "creation_allowed"
    CREATION_DENIED = "creation_denied"
    DISABLED_PROVIDER = "disabled_provider"
    INACTIVE_USER = "inactive_user"
    INVALID_CLAIMS = "invalid_claims"


@dataclass(frozen=True, slots=True)
class ProviderAssertion:
    provider_name: str
    provider_subject: str
    claims: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "provider_name", provider_name_value(self.provider_name)
        )
        if (
            not isinstance(self.provider_subject, str)
            or not self.provider_subject.strip()
        ):
            raise ValueError("Provider subject must be a non-blank string.")
        object.__setattr__(self, "provider_subject", self.provider_subject.strip())


@dataclass(frozen=True, slots=True)
class ProviderPolicyDecision:
    outcome: ProviderPolicyOutcome
    provider_name: str
    provider_subject: str
    user_id: str | None = None
    reason: str | None = None

    @property
    def accepted(self) -> bool:
        return self.outcome in {
            ProviderPolicyOutcome.ALREADY_LINKED,
            ProviderPolicyOutcome.CREATION_ALLOWED,
            ProviderPolicyOutcome.EMAIL_MATCH_LINK_ALLOWED,
            ProviderPolicyOutcome.LINK_ALLOWED,
            ProviderPolicyOutcome.LINKED_USER,
        }


class ProviderAccountPolicy:
    def evaluate_login(
        self,
        *,
        provider: ProviderSettings,
        assertion: ProviderAssertion,
        linked_user_id: str | None = None,
        linked_user_active: bool = True,
        email_match_user_id: str | None = None,
        email_match_user_active: bool = True,
    ) -> ProviderPolicyDecision:
        invalid_claims = _invalid_claim_reason(provider, assertion)
        if invalid_claims is not None:
            return _decision(
                ProviderPolicyOutcome.INVALID_CLAIMS,
                assertion,
                reason=invalid_claims,
            )
        if not provider.enabled:
            return _decision(
                ProviderPolicyOutcome.DISABLED_PROVIDER,
                assertion,
                reason="Provider is disabled.",
            )
        if linked_user_id is not None:
            if not linked_user_active:
                return _decision(
                    ProviderPolicyOutcome.INACTIVE_USER,
                    assertion,
                    user_id=linked_user_id,
                    reason="Linked local user is inactive.",
                )
            return _decision(
                ProviderPolicyOutcome.LINKED_USER,
                assertion,
                user_id=linked_user_id,
            )
        if email_match_user_id is not None:
            if not provider.email_match_linking_enabled:
                return _decision(
                    ProviderPolicyOutcome.CREATION_DENIED,
                    assertion,
                    reason="Provider email-match linking is not allowed.",
                )
            if assertion.claims.get("email_verified") is not True:
                return _decision(
                    ProviderPolicyOutcome.INVALID_CLAIMS,
                    assertion,
                    reason="Provider email-match linking requires verified email.",
                )
            if not email_match_user_active:
                return _decision(
                    ProviderPolicyOutcome.INACTIVE_USER,
                    assertion,
                    user_id=email_match_user_id,
                    reason="Matched local user is inactive.",
                )
            return _decision(
                ProviderPolicyOutcome.EMAIL_MATCH_LINK_ALLOWED,
                assertion,
                user_id=email_match_user_id,
            )
        if provider.account_creation_enabled and _creation_claims_allowed(
            provider,
            assertion,
        ):
            return _decision(ProviderPolicyOutcome.CREATION_ALLOWED, assertion)
        return _decision(
            ProviderPolicyOutcome.CREATION_DENIED,
            assertion,
            reason="Provider account creation is not allowed.",
        )

    def evaluate_linking(
        self,
        *,
        provider: ProviderSettings,
        assertion: ProviderAssertion,
        current_user_id: str,
        linked_user_id: str | None = None,
    ) -> ProviderPolicyDecision:
        invalid_claims = _invalid_claim_reason(provider, assertion)
        if invalid_claims is not None:
            return _decision(
                ProviderPolicyOutcome.INVALID_CLAIMS,
                assertion,
                reason=invalid_claims,
            )
        if not provider.enabled:
            return _decision(
                ProviderPolicyOutcome.DISABLED_PROVIDER,
                assertion,
                reason="Provider is disabled.",
            )
        if linked_user_id is None:
            return _decision(
                ProviderPolicyOutcome.LINK_ALLOWED,
                assertion,
                user_id=current_user_id,
            )
        if linked_user_id == current_user_id:
            return _decision(
                ProviderPolicyOutcome.ALREADY_LINKED,
                assertion,
                user_id=current_user_id,
            )
        return _decision(
            ProviderPolicyOutcome.COLLISION,
            assertion,
            user_id=current_user_id,
            reason="Provider subject is already linked to another local user.",
        )


def _invalid_claim_reason(
    provider: ProviderSettings,
    assertion: ProviderAssertion,
) -> str | None:
    for claim in provider.required_claims:
        value = assertion.claims.get(claim)
        if value is None or value == "":
            return f"Required provider claim is missing: {claim}."
    return None


def _creation_claims_allowed(
    provider: ProviderSettings,
    assertion: ProviderAssertion,
) -> bool:
    if not provider.allowed_emails and not provider.allowed_domains:
        return True
    email = assertion.claims.get("email")
    if not isinstance(email, str) or "@" not in email:
        return False
    if assertion.claims.get("email_verified") is not True:
        return False
    normalised_email = email.strip().lower()
    if normalised_email in provider.allowed_emails:
        return True
    domain = normalised_email.rsplit("@", maxsplit=1)[-1]
    return domain in provider.allowed_domains


def _decision(
    outcome: ProviderPolicyOutcome,
    assertion: ProviderAssertion,
    *,
    user_id: str | None = None,
    reason: str | None = None,
) -> ProviderPolicyDecision:
    return ProviderPolicyDecision(
        outcome=outcome,
        provider_name=assertion.provider_name,
        provider_subject=assertion.provider_subject,
        user_id=user_id,
        reason=reason,
    )


__all__ = (
    "ProviderAccountPolicy",
    "ProviderAssertion",
    "ProviderPolicyDecision",
    "ProviderPolicyOutcome",
)
