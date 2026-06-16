"""Shared email normalisation helpers for auth models and lookup paths."""

from __future__ import annotations

from pydantic import EmailStr, TypeAdapter, ValidationError

_EMAIL_ADAPTER = TypeAdapter(EmailStr)


def normalise_email(value: str) -> str:
    """Return a canonical email address in casefolded form.

    Raises:
        ValidationError: If ``value`` is not a valid email address.
    """

    return str(_EMAIL_ADAPTER.validate_python(value)).casefold()


def normalise_email_target(target: str) -> str | None:
    """Return a canonical email value, or ``None`` when invalid."""

    try:
        return normalise_email(target)
    except ValidationError:
        return None
