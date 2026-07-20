"""HTTP security header helpers for FastAPI applications."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal, cast, get_args

from fastapi import FastAPI, Request
from fastapi.responses import Response

from wybra.events import (
    EVT_SECURITY,
    POLICY,
    EventsCapability,
    SecurityPolicyEvent,
    publish_observation,
    scoped,
)
from wybra.site import SiteCapabilityError, get_site

CrossOriginOpenerPolicy = Literal[
    "same-origin",
    "same-origin-allow-popups",
    "unsafe-none",
    "noopener-allow-popups",
]
COOP_HEADER_NAME = "Cross-Origin-Opener-Policy"
COOP_STATE_ATTRIBUTE = "wybra_cross_origin_opener_policy"
SECURITY_MIDDLEWARE_STATE_ATTRIBUTE = "wybra_security_middleware_registered"
SECURITY_OPTIONS_STATE_ATTRIBUTE = "wybra_security_options"
_ALLOWED_COOP_POLICIES = frozenset(get_args(CrossOriginOpenerPolicy))
_UNSET = object()


@dataclass(frozen=True, slots=True)
class SecurityHeaderOptions:
    cross_origin_opener_policy: CrossOriginOpenerPolicy | None = "same-origin"

    def __post_init__(self) -> None:
        validate_cross_origin_opener_policy(self.cross_origin_opener_policy)


def cross_origin_opener_policy(
    policy: CrossOriginOpenerPolicy | None,
) -> Callable[[Request], None]:
    validate_cross_origin_opener_policy(policy)

    def dependency(request: Request) -> None:
        setattr(request.state, COOP_STATE_ATTRIBUTE, policy)

    return dependency


def register_security_headers(
    app: FastAPI,
    *,
    options: SecurityHeaderOptions | None = None,
) -> None:
    security_options = options or SecurityHeaderOptions()
    setattr(app.state, SECURITY_OPTIONS_STATE_ATTRIBUTE, security_options)
    if getattr(app.state, SECURITY_MIDDLEWARE_STATE_ATTRIBUTE, False):
        return

    app.middleware("http")(_security_header_middleware)
    setattr(app.state, SECURITY_MIDDLEWARE_STATE_ATTRIBUTE, True)


async def _security_header_middleware(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    response = await call_next(request)
    if COOP_HEADER_NAME in response.headers:
        await _publish_policy(request, outcome="preserved")
        return response

    policy = _effective_cross_origin_opener_policy(request)
    if policy is not None:
        response.headers[COOP_HEADER_NAME] = policy
        await _publish_policy(request, outcome="applied")
    else:
        await _publish_policy(request, outcome="disabled")

    return response


async def _publish_policy(request: Request, *, outcome: str) -> None:
    """Publish a safe security-policy observation without header values."""

    try:
        events = get_site(request.app).optional_capability(EventsCapability)
    except SiteCapabilityError:
        return
    if events is None:
        return
    with scoped(EVT_SECURITY(POLICY)):
        await publish_observation(
            events,
            SecurityPolicyEvent(
                policy="cross_origin_opener",
                outcome=outcome,
            ),
            message="security policy event",
        )


def _effective_cross_origin_opener_policy(
    request: Request,
) -> CrossOriginOpenerPolicy | None:
    request_policy = getattr(request.state, COOP_STATE_ATTRIBUTE, _UNSET)
    if request_policy is not _UNSET:
        policy = cast(CrossOriginOpenerPolicy | None, request_policy)
        validate_cross_origin_opener_policy(policy)
        return policy

    options = getattr(request.app.state, SECURITY_OPTIONS_STATE_ATTRIBUTE, None)
    if isinstance(options, SecurityHeaderOptions):
        return options.cross_origin_opener_policy

    return SecurityHeaderOptions().cross_origin_opener_policy


def validate_cross_origin_opener_policy(
    policy: CrossOriginOpenerPolicy | None,
) -> None:
    if policy is None:
        return
    if policy not in _ALLOWED_COOP_POLICIES:
        allowed = ", ".join(sorted(_ALLOWED_COOP_POLICIES))
        raise ValueError(
            "Cross-Origin-Opener-Policy must be one of "
            f"{allowed}, or None; got {policy!r}."
        )
