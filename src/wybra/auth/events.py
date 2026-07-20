"""Secret-safe account and credential event helpers."""

from __future__ import annotations

from fastapi import Request

from wybra.events import (
    ACCOUNT,
    CREDENTIAL,
    EVT_ACCOUNT,
    EVT_CREDENTIAL,
    AccountLifecycleEvent,
    CredentialAccessEvent,
    EventsCapability,
    publish_observation,
    scoped,
)
from wybra.site import SiteCapabilityError, get_site


def mask_email(value: object) -> str | None:
    """Mask an email while retaining only minimal diagnostic context."""

    if not isinstance(value, str):
        return None
    local, separator, domain = value.strip().partition("@")
    if not separator or not local or not domain:
        return None
    domain_label = domain.split(".", maxsplit=1)[0]
    return f"{local[:1]}**@{domain_label[:8]}**"


async def publish_account_lifecycle(
    request: Request,
    *,
    operation: str,
    outcome: str,
    user_id: object | None = None,
    email: object | None = None,
    error: Exception | None = None,
) -> None:
    """Publish a non-controlling account lifecycle observation."""

    try:
        events = get_site(request.app).optional_capability(EventsCapability)
    except SiteCapabilityError:
        return
    if events is None:
        return
    with scoped(EVT_ACCOUNT(ACCOUNT)):
        await publish_observation(
            events,
            AccountLifecycleEvent(
                operation=operation,
                outcome=outcome,
                user_id=str(user_id) if user_id is not None else None,
                masked_email=mask_email(email),
                error_type=type(error).__name__ if error is not None else None,
            ),
            message="account lifecycle event",
        )


async def publish_credential_access(
    request: Request,
    *,
    operation: str,
    provider: str,
    outcome: str,
    user_id: object | None = None,
    email: object | None = None,
    error: Exception | None = None,
) -> None:
    """Publish a non-controlling credential/access-control observation."""

    try:
        events = get_site(request.app).optional_capability(EventsCapability)
    except SiteCapabilityError:
        return
    if events is None:
        return
    with scoped(EVT_CREDENTIAL(CREDENTIAL)):
        await publish_observation(
            events,
            CredentialAccessEvent(
                operation=operation,
                provider=provider,
                outcome=outcome,
                user_id=str(user_id) if user_id is not None else None,
                masked_email=mask_email(email),
                error_type=type(error).__name__ if error is not None else None,
            ),
            message="credential access event",
        )


__all__ = ("mask_email", "publish_account_lifecycle", "publish_credential_access")
