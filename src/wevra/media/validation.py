from __future__ import annotations

from pathlib import Path
from typing import Protocol

from wevra.media.config import MEDIA_URL_MODES
from wevra.tools.validation.core import ValidationCheck, ValidationResult, record_check


class MediaValidationSettings(Protocol):
    media_root: Path | None
    media_mount_path: str
    media_serve: bool
    media_url_mode: str


def validate_media(settings: MediaValidationSettings) -> ValidationResult:
    errors: list[str] = []
    checks: list[ValidationCheck] = []
    root = settings.media_root
    if root is None:
        record_check(
            checks,
            errors,
            passed=False,
            description="media root is configured",
            error="Media root must be configured when wevra.media is enabled.",
        )
        return ValidationResult(
            name="media", errors=tuple(errors), checks=tuple(checks)
        )
    record_check(
        checks,
        errors,
        passed=root.exists(),
        description=f"media root exists: {root}",
        error=f"Media root must exist: {root}",
    )
    record_check(
        checks,
        errors,
        passed=not root.exists() or root.is_dir(),
        description=f"media root is a directory: {root}",
        error=f"Media root must be a directory: {root}",
    )
    record_check(
        checks,
        errors,
        passed=settings.media_mount_path.startswith("/"),
        description=f"media mount path is absolute: {settings.media_mount_path}",
        error="Media mount path must start with '/'.",
    )
    record_check(
        checks,
        errors,
        passed=isinstance(settings.media_serve, bool),
        description="media serve setting is boolean",
        error="Media serve setting must be boolean.",
    )
    record_check(
        checks,
        errors,
        passed=settings.media_url_mode in MEDIA_URL_MODES,
        description=f"media URL mode is supported: {settings.media_url_mode}",
        error=f"Media URL mode must be one of: {', '.join(sorted(MEDIA_URL_MODES))}.",
    )
    return ValidationResult(name="media", errors=tuple(errors), checks=tuple(checks))


validation_targets = {"media": validate_media}

__all__ = ("MediaValidationSettings", "validate_media", "validation_targets")
