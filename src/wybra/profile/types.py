from __future__ import annotations

from typing import TypedDict


class Pronouns(TypedDict, total=False):
    direct: str
    possessive: str


class ProfileLinks(TypedDict, total=False):
    website: str


type ProfileFieldValue = str | Pronouns | ProfileLinks | None


__all__ = ("ProfileFieldValue", "ProfileLinks", "Pronouns")
