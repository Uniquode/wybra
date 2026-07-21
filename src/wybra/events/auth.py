"""Account and credential event contracts with secret-safe descriptors."""

from __future__ import annotations

from dataclasses import dataclass
from inspect import BoundArguments
from typing import ClassVar

from fastapi import Request

from wybra.events._core import (
    ACCOUNT,
    COMPLETED,
    CREDENTIAL,
    EVT_ACCOUNT,
    EVT_CREDENTIAL,
    Event,
    EventOutcome,
    EventSegment,
    observe,
)


@dataclass(frozen=True, slots=True, kw_only=True)
class AccountLifecycleEvent(Event):
    """An account lifecycle outcome with opaque identity metadata only."""

    kind: ClassVar[EventSegment] = COMPLETED
    operation: str
    outcome: str
    user_id: str | None = None
    masked_email: str | None = None
    error_type: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class CredentialAccessEvent(Event):
    """A credential/access-control outcome without credential material."""

    kind: ClassVar[EventSegment] = COMPLETED
    operation: str
    provider: str
    outcome: str
    user_id: str | None = None
    masked_email: str | None = None
    error_type: str | None = None


def account_event(
    call: BoundArguments, observation: EventOutcome | None
) -> Event | None:
    """Build an account lifecycle event from already-redacted call metadata."""

    if observation is None:
        return None
    arguments = call.arguments
    operation = arguments["operation"]
    outcome = arguments["outcome"]
    if not isinstance(operation, str) or not isinstance(outcome, str):
        raise TypeError("Account events require string operation and outcome values.")
    return AccountLifecycleEvent(
        topic=EVT_ACCOUNT(ACCOUNT, COMPLETED),
        operation=operation,
        outcome=outcome,
        user_id=_opaque_id(arguments.get("user_id")),
        masked_email=mask_email(arguments.get("email")),
        error_type=_error_type(arguments.get("error")),
    )


def credential_event(
    call: BoundArguments,
    observation: EventOutcome | None,
) -> Event | None:
    """Build a credential event without retaining provider or token material."""

    if observation is None:
        return None
    arguments = call.arguments
    operation = arguments["operation"]
    provider = arguments["provider"]
    outcome = arguments["outcome"]
    if not all(isinstance(value, str) for value in (operation, provider, outcome)):
        raise TypeError("Credential events require string operation metadata.")
    return CredentialAccessEvent(
        topic=EVT_CREDENTIAL(CREDENTIAL, COMPLETED),
        operation=operation,
        provider=provider,
        outcome=outcome,
        user_id=_opaque_id(arguments.get("user_id")),
        masked_email=mask_email(arguments.get("email")),
        error_type=_error_type(arguments.get("error")),
    )


def totp_verification_event(
    call: BoundArguments,
    observation: EventOutcome | None,
) -> Event | None:
    """Build a credential event for a TOTP verification result.

    The TOTP helper is shared by login, setup, and security-assertion flows,
    so it owns the common credential-verification observation.  It deliberately
    reads only the opaque user identifier and the boolean result, never the
    code, credential identifier, or challenge data.
    """

    if observation is None:
        return None
    result = observation.result
    accepted = (
        isinstance(result, tuple)
        and bool(result)
        and isinstance(result[0], bool)
        and result[0]
    )
    return CredentialAccessEvent(
        topic=EVT_CREDENTIAL(CREDENTIAL, COMPLETED),
        operation="verify",
        provider="totp",
        outcome="succeeded" if accepted else "rejected",
        user_id=_opaque_id(call.arguments.get("user_id")),
        error_type=observation.error_type,
    )


def mask_email(value: object) -> str | None:
    """Mask an email while retaining only minimal diagnostic context."""

    if not isinstance(value, str):
        return None
    local, separator, domain = value.strip().partition("@")
    if not separator or not local or not domain:
        return None
    domain_label = domain.split(".", maxsplit=1)[0]
    return f"{local[:1]}**@{domain_label[:8]}**"


@observe(account_event)
async def publish_account_lifecycle(
    request: Request,
    *,
    operation: str,
    outcome: str,
    user_id: object | None = None,
    email: object | None = None,
    error: Exception | None = None,
) -> None:
    """Observe a non-controlling account lifecycle outcome."""
    del request, operation, outcome, user_id, email, error


@observe(credential_event)
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
    """Observe a non-controlling credential/access-control outcome."""
    del request, operation, provider, outcome, user_id, email, error


def _opaque_id(value: object | None) -> str | None:
    return str(value) if value is not None else None


def _error_type(value: object | None) -> str | None:
    return type(value).__name__ if isinstance(value, Exception) else None


__all__ = (
    "account_event",
    "credential_event",
    "mask_email",
    "publish_account_lifecycle",
    "publish_credential_access",
    "totp_verification_event",
)
