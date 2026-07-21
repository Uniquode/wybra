"""Security event contracts and safe policy descriptors."""

from __future__ import annotations

from dataclasses import dataclass
from inspect import BoundArguments
from typing import ClassVar

from wybra.events._core import (
    COMPLETED,
    CSRF,
    DENIED,
    EVT_SECURITY,
    POLICY,
    Event,
    EventOutcome,
    EventSegment,
)


@dataclass(frozen=True, slots=True, kw_only=True)
class SecurityPolicyEvent(Event):
    """A safe outcome for one configured security policy."""

    kind: ClassVar[EventSegment] = COMPLETED
    policy: str
    outcome: str


@dataclass(frozen=True, slots=True, kw_only=True)
class SecurityDenialEvent(Event):
    """A secret-free observation of a security rejection."""

    kind: ClassVar[EventSegment] = DENIED
    mechanism: str


def security_policy_event(
    call: BoundArguments,
    observation: EventOutcome | None,
) -> Event | None:
    """Build a safe policy event without inspecting headers or request values."""

    if observation is None:
        return None
    outcome = call.arguments["outcome"]
    if not isinstance(outcome, str):
        raise TypeError("Security policy events require a string outcome.")
    return SecurityPolicyEvent(
        topic=EVT_SECURITY(POLICY, COMPLETED),
        policy="cross_origin_opener",
        outcome=outcome,
    )


def csrf_denial_event(
    _call: BoundArguments,
    outcome: EventOutcome | None,
) -> Event | None:
    """Build a secret-free CSRF rejection observation."""

    if outcome is None:
        return None
    return SecurityDenialEvent(
        topic=EVT_SECURITY(CSRF, DENIED),
        mechanism="csrf",
    )


__all__ = ("csrf_denial_event", "security_policy_event")
