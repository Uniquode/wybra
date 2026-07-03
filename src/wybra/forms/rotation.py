from __future__ import annotations

from dataclasses import dataclass, field
from secrets import token_urlsafe

from wybra.forms.settings import CSRF_TOKEN_SECRET_BYTES


@dataclass(frozen=True, slots=True)
class CsrfTokenSecretRotationPlan:
    current_value: str = field(repr=False)
    previous_value: str = field(repr=False)
    previous_secret_count: int


def generate_csrf_token_secret(
    *,
    existing_secrets: set[str] | frozenset[str] = frozenset(),
) -> str:
    for _attempt in range(32):
        secret = token_urlsafe(CSRF_TOKEN_SECRET_BYTES)
        if secret not in existing_secrets:
            return secret
    raise ValueError("Could not generate a unique CSRF token secret.")


def plan_csrf_token_secret_rotation(
    *,
    current: str | None,
    previous: str | None,
) -> CsrfTokenSecretRotationPlan:
    current_value = _normalise_current_secret(current)
    previous_values = _normalise_previous_secrets(previous)
    if current_value in previous_values:
        raise ValueError("CSRF token secrets must be unique.")
    existing_secrets = {current_value, *previous_values}
    new_current = generate_csrf_token_secret(existing_secrets=existing_secrets)
    new_previous_values = (current_value, *previous_values)
    return CsrfTokenSecretRotationPlan(
        current_value=new_current,
        previous_value=",".join(new_previous_values),
        previous_secret_count=len(new_previous_values),
    )


def _normalise_current_secret(value: str | None) -> str:
    if value is None or not isinstance(value, str) or not value.strip():
        raise ValueError("The current CSRF token secret must be configured.")
    return value.strip()


def _normalise_previous_secrets(value: str | None) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Previous CSRF token secrets must be non-blank.")
    previous_values = tuple(entry.strip() for entry in value.split(","))
    if any(not entry for entry in previous_values):
        raise ValueError(
            "Previous CSRF token secrets must be comma-separated and non-blank."
        )
    if len(set(previous_values)) != len(previous_values):
        raise ValueError("Previous CSRF token secrets must be unique.")
    return previous_values


__all__ = (
    "CsrfTokenSecretRotationPlan",
    "generate_csrf_token_secret",
    "plan_csrf_token_secret_rotation",
)
