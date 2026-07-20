"""Version validation for Wybra's private Tortoise adapter."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version
from typing import Final

_MINIMUM_VERSION: Final = (1, 1, 7)
_MAXIMUM_VERSION: Final = (1, 2, 0)


class TortoiseCompatibilityError(RuntimeError):
    """Raised when the installed Tortoise version is not adapter-compatible."""


def ensure_supported_tortoise_version() -> None:
    """Fail before private instrumentation touches an unverified Tortoise."""

    try:
        installed = version("tortoise-orm")
    except PackageNotFoundError as exc:  # pragma: no cover - required dependency
        raise TortoiseCompatibilityError(
            "Wybra database instrumentation requires tortoise-orm."
        ) from exc
    parsed = _parse_version(installed)
    if not _MINIMUM_VERSION <= parsed < _MAXIMUM_VERSION:
        raise TortoiseCompatibilityError(
            "Wybra database instrumentation supports tortoise-orm "
            ">=1.1.7,<1.2.0; installed version is "
            f"{installed!r}. Upgrade Wybra or install a supported Tortoise version."
        )


def _parse_version(value: str) -> tuple[int, int, int]:
    """Return the release tuple needed for Wybra's intentionally narrow pin."""

    release = value.split("+", maxsplit=1)[0].split(".")
    try:
        major, minor, patch = (int(part) for part in release[:3])
    except ValueError as exc:
        raise TortoiseCompatibilityError(
            f"Installed tortoise-orm version {value!r} is not a supported release."
        ) from exc
    return major, minor, patch


__all__ = (
    "TortoiseCompatibilityError",
    "ensure_supported_tortoise_version",
)
