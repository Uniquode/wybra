from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class ProfileUser(Protocol):
    email: str


@dataclass(frozen=True, slots=True)
class ProfileImage:
    src: str | None
    alt: str
    fallback_text: str | None = None


def profile_image_for_user(user: ProfileUser) -> ProfileImage:
    return ProfileImage(
        src=None,
        alt="Profile picture",
        fallback_text=_email_initial(user.email),
    )


def _email_initial(email: str) -> str | None:
    cleaned_email = email.strip()
    if not cleaned_email:
        return None
    return cleaned_email[0].upper()


__all__ = (
    "ProfileImage",
    "ProfileUser",
    "profile_image_for_user",
)
