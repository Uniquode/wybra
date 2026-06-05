"""Diagnostic message helpers for reusable composition layers.

This module is text-only and must not import host applications, runtime
startup, configured modules, route surfaces, or database infrastructure.
"""

from typing import TypeVar

ExceptionT = TypeVar("ExceptionT", bound=Exception)


def diagnostic_message(subject: str, message: str) -> str:
    return f"{subject} {message}"


def app_config_message(path: object, message: str) -> str:
    return diagnostic_message("App config file", f"{message}: {path}")


def configured_module_message(module_name: str, message: str) -> str:
    return diagnostic_message(f"Configured module {module_name!r}", message)


def configured_module_import_message(module_name: str) -> str:
    return f"Configured module cannot be imported: {module_name}"


def surface_message(surface_type: str, surface_name: str, message: str) -> str:
    return diagnostic_message(f"{surface_type} {surface_name!r}", message)


def validation_target_message(target_name: str, surface_name: str, message: str) -> str:
    return diagnostic_message(
        f"Validation target {target_name!r} from {surface_name!r}",
        message,
    )


def wrapped_error(  # noqa: UP047
    error_type: type[ExceptionT],
    exc: BaseException,
) -> ExceptionT:
    return error_type(str(exc))


__all__ = [
    "app_config_message",
    "configured_module_import_message",
    "configured_module_message",
    "diagnostic_message",
    "surface_message",
    "validation_target_message",
    "wrapped_error",
]
