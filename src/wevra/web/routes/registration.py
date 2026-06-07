"""FastAPI router composition contracts and helpers."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Protocol

from fastapi import FastAPI
from fastapi.routing import APIRouter

from wevra.core.composition import AppConfig, CompositionError

ModuleRouters = Mapping[str, APIRouter]
RoutePrefixMap = Mapping[str, Mapping[str, str]]


@dataclass(frozen=True, slots=True)
class ConfiguredModuleRouter:
    module_name: str
    label: str
    router: APIRouter
    prefix: str


class RouteCompositionError(CompositionError):
    """Raised when configured module routers cannot be composed safely."""


class ConfiguredRouteSettings(Protocol):
    """Minimal settings shape for route composition."""

    @property
    def modules(self) -> tuple[str, ...]: ...

    @property
    def app_config(self) -> AppConfig | None: ...


@dataclass(frozen=True, slots=True)
class _RouteOwner:
    module_name: str
    router_label: str

    @property
    def label(self) -> str:
        return f"{self.module_name} router {self.router_label}"


def load_module_routes(
    module_names: Iterable[str],
    *,
    route_prefixes: RoutePrefixMap | None = None,
) -> tuple[ConfiguredModuleRouter, ...]:
    from wevra.web.routes.discovery import discover_module_routers

    configured_routers: list[ConfiguredModuleRouter] = []
    for module_name in module_names:
        module_routers = discover_module_routers(module_name)
        if not module_routers:
            continue

        module_prefixes = (
            None if route_prefixes is None else route_prefixes.get(module_name)
        )
        for label, router in module_routers.items():
            prefix = _prefix_for_module_router(
                module_name,
                label,
                module_prefixes,
            )
            configured_routers.append(
                ConfiguredModuleRouter(
                    module_name=module_name,
                    label=label,
                    router=router,
                    prefix=prefix,
                )
            )

    _validate_configured_routers(configured_routers)
    return tuple(configured_routers)


def load_configured_module_routes(
    settings: ConfiguredRouteSettings,
) -> tuple[ConfiguredModuleRouter, ...]:
    return load_module_routes(
        settings.modules,
        route_prefixes=route_prefixes_from_settings(settings),
    )


def route_prefixes_from_settings(
    settings: ConfiguredRouteSettings,
) -> RoutePrefixMap:
    explicit_route_prefixes = getattr(settings, "route_prefixes", None)
    if isinstance(explicit_route_prefixes, Mapping):
        return explicit_route_prefixes

    return route_prefixes_from_app_config(settings.app_config)


def route_prefixes_from_app_config(
    app_config: AppConfig | None,
) -> RoutePrefixMap:
    if app_config is None:
        return {}

    prefixes = app_config.routes.prefixes
    if isinstance(prefixes, Mapping):
        return prefixes

    return {}


def register_configured_module_routes(
    app: FastAPI,
    settings: ConfiguredRouteSettings,
) -> None:
    register_module_routes(app, load_configured_module_routes(settings))


def register_module_routes(
    app: FastAPI,
    configured_routers: Iterable[ConfiguredModuleRouter],
) -> None:
    from wevra.web.routes.inspection import RouteOrigin, record_route_origin

    routers = tuple(configured_routers)
    _validate_configured_routers(routers)
    for configured_router in routers:
        route_start = len(app.routes)
        app.include_router(configured_router.router, prefix=configured_router.prefix)
        for route in app.routes[route_start:]:
            route_path = getattr(route, "path", None)
            route_name = getattr(route, "name", None)
            if not isinstance(route_path, str):
                continue
            record_route_origin(
                app,
                route,
                RouteOrigin(
                    module_name=configured_router.module_name,
                    router_label=configured_router.label,
                    include_prefix=configured_router.prefix,
                    route_name=route_name if isinstance(route_name, str) else None,
                    path=route_path,
                    methods=_route_origin_methods(route),
                ),
            )


def _prefix_for_module_router(
    module_name: str,
    label: str,
    module_prefixes: Mapping[str, str] | None,
) -> str:
    if module_prefixes is None:
        return ""

    if label not in module_prefixes:
        raise RouteCompositionError(
            f"Route config for configured module {module_name!r} is missing "
            f"router label {label!r}."
        )

    return _normalise_include_prefix(module_name, label, module_prefixes[label])


def _normalise_include_prefix(module_name: str, label: str, prefix: str) -> str:
    if not isinstance(prefix, str):
        raise RouteCompositionError(
            f"Route prefix for {module_name!r} router {label!r} must be a string."
        )
    if prefix == "":
        return ""

    normalised_prefix = prefix.rstrip("/")
    if not normalised_prefix:
        return ""
    if not normalised_prefix.startswith("/"):
        raise RouteCompositionError(
            f"Route prefix for {module_name!r} router {label!r} must start with '/'."
        )

    return normalised_prefix


def _validate_configured_routers(
    configured_routers: Iterable[ConfiguredModuleRouter],
) -> None:
    route_names: dict[str, _RouteOwner] = {}
    method_paths: dict[tuple[str, str], _RouteOwner] = {}
    for configured_router in configured_routers:
        owner = _RouteOwner(
            module_name=configured_router.module_name,
            router_label=configured_router.label,
        )
        if not configured_router.router.routes:
            raise RouteCompositionError(
                f"Route surface for {owner.label} did not register any routes; "
                "ensure decorated handler modules are imported before exposing "
                "module_routers."
            )

        for route in configured_router.router.routes:
            route_name = getattr(route, "name", None)
            if isinstance(route_name, str) and route_name:
                _record_route_name(route_name, owner, route_names)

            route_path = getattr(route, "path", None)
            route_methods = getattr(route, "methods", None)
            if route_path is None or route_methods is None:
                continue
            if not isinstance(route_path, str):
                raise RouteCompositionError(
                    f"Route {owner.label} has invalid path {route_path!r}; "
                    "paths must be strings."
                )
            if route_path != "" and not route_path.startswith("/"):
                raise RouteCompositionError(
                    f"Route {owner.label} has invalid path {route_path!r}; "
                    "paths must be empty or start with '/'."
                )

            full_path = f"{configured_router.prefix}{route_path}"
            if not full_path:
                raise RouteCompositionError(
                    f"Route {owner.label} resolves to an empty path."
                )
            for method in _normalised_methods(route_methods, owner, full_path):
                _record_method_path(method, full_path, owner, method_paths)


def _record_route_name(
    route_name: str,
    owner: _RouteOwner,
    route_names: dict[str, _RouteOwner],
) -> None:
    if route_name in route_names:
        previous = route_names[route_name]
        raise RouteCompositionError(
            f"Route name conflict for {route_name!r}: "
            f"{previous.label} conflicts with {owner.label}."
        )

    route_names[route_name] = owner


def _record_method_path(
    method: str,
    path: str,
    owner: _RouteOwner,
    method_paths: dict[tuple[str, str], _RouteOwner],
) -> None:
    key = (method, path)
    if key in method_paths:
        previous = method_paths[key]
        raise RouteCompositionError(
            f"Route method/path conflict for {method} {path}: "
            f"{previous.label} conflicts with {owner.label}."
        )

    method_paths[key] = owner


def _normalised_methods(
    methods: Iterable[str],
    owner: _RouteOwner,
    path: str,
) -> tuple[str, ...]:
    normalised_methods: list[str] = []
    for method in methods:
        if not isinstance(method, str):
            raise RouteCompositionError(
                f"Route {owner.label} at {path} has invalid HTTP method "
                f"{method!r}; methods must be strings."
            )

        stripped_method = method.strip()
        if stripped_method:
            normalised_methods.append(stripped_method.upper())

    if not normalised_methods:
        raise RouteCompositionError(
            f"Route {owner.label} at {path} must declare at least one HTTP method."
        )

    return tuple(normalised_methods)


def _route_origin_methods(route: object) -> tuple[str, ...]:
    methods = getattr(route, "methods", None)
    if methods is None:
        return ()
    return tuple(
        sorted(
            method.strip().upper()
            for method in methods
            if isinstance(method, str) and method.strip()
        )
    )


__all__ = [
    "ConfiguredModuleRouter",
    "ConfiguredRouteSettings",
    "ModuleRouters",
    "RouteCompositionError",
    "RoutePrefixMap",
    "load_configured_module_routes",
    "load_module_routes",
    "register_configured_module_routes",
    "register_module_routes",
    "route_prefixes_from_app_config",
    "route_prefixes_from_settings",
]
