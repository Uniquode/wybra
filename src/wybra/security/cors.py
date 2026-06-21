"""CORS policy data and parsing owned by the security module."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from wybra.config.transforms import to_bool


@dataclass(frozen=True, slots=True)
class CorsPolicy:
    allow_origins: tuple[str, ...] = ("*",)
    allow_methods: tuple[str, ...] = ("GET", "HEAD")
    allow_headers: tuple[str, ...] = ()
    expose_headers: tuple[str, ...] = ()
    allow_credentials: bool = False
    max_age: int = 600


@dataclass(frozen=True, slots=True)
class CorsPolicySet(CorsPolicy):
    enabled: bool = False
    paths: Mapping[str, CorsPolicy] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "paths", MappingProxyType(dict(self.paths)))


def load_cors_policy_set(
    data: Mapping[str, Any],
    name: str,
    *,
    error_type: type[Exception] = ValueError,
) -> CorsPolicySet:
    if not data:
        return CorsPolicySet()

    base = load_cors_policy(data, name, error_type=error_type)
    paths_data = _optional_mapping(data, f"{name}.paths", error_type=error_type)
    paths: dict[str, CorsPolicy] = {}
    for path, path_data in paths_data.items():
        if not isinstance(path, str) or not path.strip():
            raise error_type(f"{name}.paths must contain only non-blank URL path keys.")
        if not isinstance(path_data, Mapping):
            raise error_type(f"{name}.paths.{path} must be a table.")
        paths[normalise_url_path_prefix(path)] = load_cors_policy(
            path_data,
            f"{name}.paths.{path}",
            defaults=base,
            error_type=error_type,
        )

    return CorsPolicySet(
        enabled=_bool_value(data, f"{name}.enabled", False, error_type=error_type),
        allow_origins=base.allow_origins,
        allow_methods=base.allow_methods,
        allow_headers=base.allow_headers,
        expose_headers=base.expose_headers,
        allow_credentials=base.allow_credentials,
        max_age=base.max_age,
        paths=paths,
    )


def load_cors_policy(
    data: Mapping[str, Any],
    name: str,
    *,
    defaults: CorsPolicy | None = None,
    error_type: type[Exception] = ValueError,
) -> CorsPolicy:
    defaults = defaults or CorsPolicy()
    return CorsPolicy(
        allow_origins=_optional_str_list(
            data,
            f"{name}.allow_origins",
            defaults.allow_origins,
            error_type=error_type,
        ),
        allow_methods=_optional_str_list(
            data,
            f"{name}.allow_methods",
            defaults.allow_methods,
            error_type=error_type,
        ),
        allow_headers=_optional_str_list(
            data,
            f"{name}.allow_headers",
            defaults.allow_headers,
            allow_empty=True,
            error_type=error_type,
        ),
        expose_headers=_optional_str_list(
            data,
            f"{name}.expose_headers",
            defaults.expose_headers,
            allow_empty=True,
            error_type=error_type,
        ),
        allow_credentials=_bool_value(
            data,
            f"{name}.allow_credentials",
            defaults.allow_credentials,
            error_type=error_type,
        ),
        max_age=_optional_non_negative_int(
            data,
            f"{name}.max_age",
            defaults.max_age,
            error_type=error_type,
        ),
    )


def normalise_url_path_prefix(path: str) -> str:
    """Normalise a configured URL prefix and retain a trailing slash."""
    return f"/{path.strip('/')}/" if path.strip("/") else "/"


def _optional_mapping(
    data: Mapping[str, Any],
    name: str,
    *,
    error_type: type[Exception],
) -> Mapping[str, Any]:
    key = name.rsplit(".", maxsplit=1)[-1]
    value = data.get(key)
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return value
    raise error_type(f"{name} must be a table.")


def _bool_value(
    data: Mapping[str, Any],
    name: str,
    default: bool,
    *,
    error_type: type[Exception],
) -> bool:
    key = name.rsplit(".", maxsplit=1)[-1]
    value = data.get(key)
    if value is None:
        return default
    try:
        return to_bool(value)
    except ValueError as exc:
        raise error_type(f"{name} must be a boolean.") from exc


def _optional_non_negative_int(
    data: Mapping[str, Any],
    name: str,
    default: int,
    *,
    error_type: type[Exception],
) -> int:
    key = name.rsplit(".", maxsplit=1)[-1]
    value = data.get(key)
    if value is None:
        return default
    if isinstance(value, int) and value >= 0:
        return value
    raise error_type(f"{name} must be a non-negative integer.")


def _optional_str_list(
    data: Mapping[str, Any],
    name: str,
    default: tuple[str, ...],
    *,
    allow_empty: bool = False,
    error_type: type[Exception],
) -> tuple[str, ...]:
    key = name.rsplit(".", maxsplit=1)[-1]
    value = data.get(key)
    if value is None:
        return default
    if (
        isinstance(value, (list, tuple))
        and (allow_empty or value)
        and all(isinstance(item, str) and item.strip() for item in value)
    ):
        return tuple(item.strip() for item in value)
    requirement = "a string list" if allow_empty else "a non-empty string list"
    raise error_type(f"{name} must be {requirement}.")
