"""FastAPI router composition contracts and helpers."""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Protocol

from fastapi import FastAPI
from fastapi.routing import APIRouter

from wevra.core.composition import AppConfig, CompositionError

ModuleRouters = Mapping[str, APIRouter]
RoutePrefixMap = Mapping[str, Mapping[str, str]]
logger = logging.getLogger(__name__)


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

        selected_labels = _published_router_labels(
            module_name,
            module_routers,
            route_prefixes,
        )
        for label in selected_labels:
            router = module_routers[label]
            prefix = _prefix_for_published_router(
                module_name=module_name,
                label=label,
                prefix=""
                if route_prefixes is None
                else route_prefixes[module_name][label],
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
    method_paths = _registered_method_paths(app)
    for configured_router in routers:
        filtered_router = _first_winning_router(configured_router, method_paths)
        if not filtered_router.routes:
            continue

        route_start = len(app.routes)
        app.include_router(filtered_router, prefix=configured_router.prefix)
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


def _prefix_for_published_router(
    *,
    module_name: str,
    label: str,
    prefix: str,
) -> str:
    return _normalise_include_prefix(module_name, label, prefix)


def _published_router_labels(
    module_name: str,
    module_routers: ModuleRouters,
    route_prefixes: RoutePrefixMap | None,
) -> tuple[str, ...]:
    if route_prefixes is None:
        return tuple(module_routers)

    module_prefixes = route_prefixes.get(module_name)
    if module_prefixes is None:
        return ()

    unknown_labels = tuple(
        label for label in module_prefixes if label not in module_routers
    )
    if unknown_labels:
        raise RouteCompositionError(
            f"Route config for configured module {module_name!r} references "
            f"unknown router label {_format_label_list(unknown_labels)}."
        )

    return tuple(module_prefixes)


def _format_label_list(labels: tuple[str, ...]) -> str:
    return ", ".join(repr(label) for label in labels)


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
            _normalised_methods(route_methods, owner, full_path)


def _registered_method_paths(app: FastAPI) -> dict[tuple[str, str], _RouteOwner]:
    method_paths: dict[tuple[str, str], _RouteOwner] = {}
    for route in app.routes:
        path = getattr(route, "path", None)
        methods = getattr(route, "methods", None)
        if not isinstance(path, str) or methods is None:
            continue
        owner = _RouteOwner(module_name="app", router_label="existing")
        for method in _route_origin_methods(route):
            method_paths[(method, path)] = owner
    return method_paths


def _first_winning_router(
    configured_router: ConfiguredModuleRouter,
    method_paths: dict[tuple[str, str], _RouteOwner],
) -> APIRouter:
    filtered_router = APIRouter()
    owner = _RouteOwner(
        module_name=configured_router.module_name,
        router_label=configured_router.label,
    )

    for route in configured_router.router.routes:
        route_path = getattr(route, "path", None)
        route_methods = getattr(route, "methods", None)
        if not isinstance(route_path, str) or route_methods is None:
            filtered_router.routes.append(route)
            continue

        full_path = f"{configured_router.prefix}{route_path}"
        methods = _normalised_methods(route_methods, owner, full_path)
        winning_owners = {
            method: method_paths[(method, full_path)]
            for method in methods
            if (method, full_path) in method_paths
        }
        if winning_owners:
            _warn_duplicate_route(
                route=route,
                owner=owner,
                methods=tuple(winning_owners),
                path=full_path,
                winning_owners=winning_owners,
            )
            continue

        filtered_router.routes.append(route)
        for method in methods:
            method_paths[(method, full_path)] = owner

    return filtered_router


def _warn_duplicate_route(
    *,
    route: object,
    owner: _RouteOwner,
    methods: tuple[str, ...],
    path: str,
    winning_owners: Mapping[str, _RouteOwner],
) -> None:
    for method in methods:
        winning_owner = winning_owners[method]
        logger.warning(
            "Skipping duplicate configured route.",
            extra={
                "route_module": owner.module_name,
                "route_router": owner.router_label,
                "route_name": getattr(route, "name", None),
                "route_method": method,
                "route_path": path,
                "winning_route_module": winning_owner.module_name,
                "winning_route_router": winning_owner.router_label,
            },
        )


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
