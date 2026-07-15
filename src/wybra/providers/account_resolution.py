from __future__ import annotations

from dataclasses import dataclass

from fastapi import Request

from wybra.auth.authorisation.effective import is_user_effectively_active
from wybra.auth.email_normalisation import normalise_email_target
from wybra.auth.ids import parse_uuid
from wybra.auth.models import User
from wybra.auth.provider_credentials import (
    ProviderCredentialStore,
    provider_credential_store,
)
from wybra.auth.timestamps import current_timestamp
from wybra.db import DatabaseCapability
from wybra.db.capabilities import tortoise_transaction
from wybra.providers.flow import (
    PROVIDER_EMAIL_MATCH_USER_UNRESOLVED_REASON,
    PROVIDER_LINKING_USER_UNAVAILABLE_REASON,
    PROVIDER_OAUTH_LINK_PURPOSE,
    ProviderOAuthPurpose,
    provider_invalid_email_reason,
    provider_invalid_linking_state_reason,
    provider_missing_access_token_reason,
)
from wybra.providers.policy import (
    ProviderAccountPolicy,
    ProviderAssertion,
    ProviderPolicyDecision,
    ProviderPolicyOutcome,
)
from wybra.providers.settings import ProviderSettings
from wybra.site import get_site


@dataclass(frozen=True, slots=True)
class ProviderAccountResolution:
    provider: ProviderSettings
    assertion: ProviderAssertion
    purpose: ProviderOAuthPurpose
    state_user_id: str | None
    provider_label: str
    account_email: str
    email_verified: bool
    access_token: str | None
    refresh_token: str | None
    expires_in: int | None
    provider_metadata: dict[str, object]


async def resolve_provider_account(
    request: Request,
    *,
    resolution: ProviderAccountResolution,
    linking_user: User | None,
) -> ProviderPolicyDecision:
    database = get_site(request.app).require_capability(DatabaseCapability)
    async with tortoise_transaction(
        database, database.database().for_write()
    ) as session:
        store = provider_credential_store(
            session,
            getattr(request.app.state, "secret_envelope_service", None),
        )
        provider_record = await store.get_provider_by_identity(
            resolution.assertion.provider_name,
            resolution.assertion.provider_subject,
        )
        linked_user = (
            await store.get_linked_user(provider_record)
            if provider_record is not None and provider_record.provider_enabled
            else None
        )
        if resolution.purpose == PROVIDER_OAUTH_LINK_PURPOSE:
            return await _resolve_provider_linking_account(
                resolution=resolution,
                store=store,
                linked_user=linked_user,
                linking_user=linking_user,
            )

        email_match_user = await _provider_email_match_user(
            store,
            resolution.account_email,
        )
        decision = ProviderAccountPolicy().evaluate_login(
            provider=resolution.provider,
            assertion=resolution.assertion,
            linked_user_id=str(linked_user.id) if linked_user is not None else None,
            linked_user_active=(
                is_user_effectively_active(linked_user)
                if linked_user is not None
                else True
            ),
            email_match_user_id=(
                str(email_match_user.id) if email_match_user is not None else None
            ),
            email_match_user_active=(
                is_user_effectively_active(email_match_user)
                if email_match_user is not None
                else True
            ),
        )
        if decision.outcome is ProviderPolicyOutcome.EMAIL_MATCH_LINK_ALLOWED:
            if email_match_user is None:
                return _provider_policy_decision(
                    ProviderPolicyOutcome.INVALID_CLAIMS,
                    resolution.assertion,
                    reason=PROVIDER_EMAIL_MATCH_USER_UNRESOLVED_REASON,
                )
            persisted = await _persist_provider_link(
                store=store,
                resolution=resolution,
                user=email_match_user,
            )
            if not persisted:
                return _provider_policy_decision(
                    ProviderPolicyOutcome.INVALID_CLAIMS,
                    resolution.assertion,
                    reason=provider_missing_access_token_reason(
                        resolution.provider_label
                    ),
                )
        if (
            decision.outcome is ProviderPolicyOutcome.LINKED_USER
            and linked_user is not None
        ):
            await _apply_verified_provider_email(store, linked_user, resolution)
        if decision.outcome is ProviderPolicyOutcome.CREATION_ALLOWED:
            return await _create_provider_user(
                store=store,
                resolution=resolution,
            )
        return decision


async def _resolve_provider_linking_account(
    *,
    resolution: ProviderAccountResolution,
    store: ProviderCredentialStore,
    linked_user: User | None,
    linking_user: User | None,
) -> ProviderPolicyDecision:
    user_id = (
        parse_uuid(resolution.state_user_id)
        if resolution.state_user_id is not None
        else None
    )
    if (
        user_id is None
        or linking_user is None
        or parse_uuid(linking_user.id) != user_id
    ):
        return _provider_policy_decision(
            ProviderPolicyOutcome.INVALID_CLAIMS,
            resolution.assertion,
            reason=provider_invalid_linking_state_reason(resolution.provider_label),
        )
    current_user = await store.get_user(user_id)
    if current_user is None or not is_user_effectively_active(current_user):
        return _provider_policy_decision(
            ProviderPolicyOutcome.INACTIVE_USER,
            resolution.assertion,
            user_id=str(user_id),
            reason=PROVIDER_LINKING_USER_UNAVAILABLE_REASON,
        )
    decision = ProviderAccountPolicy().evaluate_linking(
        provider=resolution.provider,
        assertion=resolution.assertion,
        current_user_id=str(current_user.id),
        linked_user_id=str(linked_user.id) if linked_user is not None else None,
    )
    if decision.outcome is ProviderPolicyOutcome.LINK_ALLOWED:
        persisted = await _persist_provider_link(
            store=store,
            resolution=resolution,
            user=current_user,
        )
        if not persisted:
            return _provider_policy_decision(
                ProviderPolicyOutcome.INVALID_CLAIMS,
                resolution.assertion,
                reason=provider_missing_access_token_reason(resolution.provider_label),
            )
    return decision


async def _provider_email_match_user(
    store: ProviderCredentialStore,
    email: str,
) -> User | None:
    normalised_email = normalise_email_target(email)
    if normalised_email is None:
        return None
    return await store.get_user_by_normalised_email(normalised_email)


async def _create_provider_user(
    *,
    store: ProviderCredentialStore,
    resolution: ProviderAccountResolution,
) -> ProviderPolicyDecision:
    normalised_email = normalise_email_target(resolution.account_email)
    if normalised_email is None:
        return _provider_policy_decision(
            ProviderPolicyOutcome.INVALID_CLAIMS,
            resolution.assertion,
            reason=provider_invalid_email_reason(resolution.provider_label),
        )
    created_user = await store.create_provider_user(
        email=normalised_email,
        is_verified=resolution.email_verified,
    )
    persisted = await _persist_provider_link(
        store=store,
        resolution=resolution,
        user=created_user,
    )
    if not persisted:
        return _provider_policy_decision(
            ProviderPolicyOutcome.INVALID_CLAIMS,
            resolution.assertion,
            reason=provider_missing_access_token_reason(resolution.provider_label),
        )
    return _provider_policy_decision(
        ProviderPolicyOutcome.CREATION_ALLOWED,
        resolution.assertion,
        user_id=str(created_user.id),
    )


async def _persist_provider_link(
    *,
    store: ProviderCredentialStore,
    resolution: ProviderAccountResolution,
    user: User,
) -> bool:
    access_token = resolution.access_token
    if access_token is None or not access_token.strip():
        return False
    provider = await store.upsert_provider_credential(
        provider_name=resolution.assertion.provider_name,
        provider_subject=resolution.assertion.provider_subject,
        access_token=access_token,
        refresh_token=resolution.refresh_token,
        expires_at=_provider_token_expires_at(resolution.expires_in),
        account_email=resolution.account_email,
        provider_metadata=resolution.provider_metadata,
    )
    await store.link_provider_to_user(provider_id=provider.id, user_id=user.id)
    await _apply_verified_provider_email(store, user, resolution)
    return True


async def _apply_verified_provider_email(
    store: ProviderCredentialStore,
    user: User,
    resolution: ProviderAccountResolution,
) -> None:
    normalised_email = normalise_email_target(resolution.account_email)
    if normalised_email is None:
        return
    await store.verify_matching_user_email(
        user,
        normalised_email,
        is_verified=resolution.email_verified,
    )


def _provider_token_expires_at(expires_in: int | None) -> float | None:
    if expires_in is None:
        return None
    return current_timestamp() + expires_in


def _provider_policy_decision(
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
    "ProviderAccountResolution",
    "resolve_provider_account",
)
