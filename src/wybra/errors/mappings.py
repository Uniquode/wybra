from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ErrorMapping:
    exception_type: type[BaseException]
    target_exception_type: type[BaseException]
    detail: str | None = None


def translate_exception(
    exc: BaseException,
    *,
    mappings: Iterable[ErrorMapping],
) -> BaseException:
    for mapping in mappings:
        if isinstance(exc, mapping.exception_type):
            return _mapped_exception(exc, mapping)
    return exc


def _mapped_exception(exc: BaseException, mapping: ErrorMapping) -> BaseException:
    if mapping.detail is None:
        mapped = mapping.target_exception_type()
    else:
        mapped = mapping.target_exception_type(mapping.detail)
    mapped.__cause__ = exc
    return mapped
