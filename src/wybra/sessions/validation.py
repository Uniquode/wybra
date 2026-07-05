from __future__ import annotations

from typing import Protocol

from wybra.config import ConfigService
from wybra.core.runtime import DEFAULT_DEPLOYMENT_ENVIRONMENT
from wybra.sessions.config import SessionStorageBackend
from wybra.sessions.ids import create_session_id, validate_session_id
from wybra.sessions.settings import SessionsSettings
from wybra.tools.validation.core import ValidationCheck, ValidationResult, record_check


class SessionsValidationSettings(Protocol):
    config: ConfigService
    deployment_environment: str | None


def validate_sessions(settings: SessionsValidationSettings) -> ValidationResult:
    errors: list[str] = []
    checks: list[ValidationCheck] = []

    try:
        session_settings = SessionsSettings.load_settings(
            settings.config,
            deployment_environment=getattr(
                settings,
                "deployment_environment",
                DEFAULT_DEPLOYMENT_ENVIRONMENT,
            ),
        )
    except Exception as exc:
        record_check(
            checks,
            errors,
            passed=False,
            description="sessions settings load",
            error=f"Sessions settings failed to load: {exc}",
        )
        return ValidationResult(
            name="sessions",
            errors=tuple(errors),
            checks=tuple(checks),
        )

    record_check(
        checks,
        errors,
        passed=True,
        description=(
            "sessions settings load: "
            f"storage_backend={session_settings.resolved_storage_backend.value}"
        ),
    )
    record_check(
        checks,
        errors,
        passed=_session_id_policy_valid(),
        description="session identifier policy validates generated IDs",
        error="Generated session identifier did not validate.",
    )
    if session_settings.resolved_storage_backend is SessionStorageBackend.MEMORY:
        record_check(
            checks,
            errors,
            passed=session_settings.deployment_environment == "local",
            description="memory session storage is local-only",
            error="Memory session storage is only valid locally.",
        )
    if session_settings.resolved_storage_backend is SessionStorageBackend.CACHE:
        record_check(
            checks,
            errors,
            passed=session_settings.cache_url is not None,
            description="cache session storage has cache URL",
            error="Cache-backed sessions require wybra.sessions.cache_url.",
        )
    if session_settings.resolved_storage_backend is SessionStorageBackend.FILE:
        record_check(
            checks,
            errors,
            passed=session_settings.resolved_file_directory.parent.exists(),
            description="file session storage parent directory exists",
            error=(
                "File-backed sessions require the parent directory to exist: "
                f"{session_settings.resolved_file_directory.parent}"
            ),
        )
    return ValidationResult(name="sessions", errors=tuple(errors), checks=tuple(checks))


def _session_id_policy_valid() -> bool:
    try:
        validate_session_id(create_session_id())
    except Exception:
        return False
    return True


validation_targets = {"sessions": validate_sessions}

__all__ = ("SessionsValidationSettings", "validate_sessions", "validation_targets")
