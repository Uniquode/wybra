"""Route composition contracts and helpers."""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import Response

from wevra.core.composition import CompositionError
from wevra.core.diagnostics import surface_message

if TYPE_CHECKING:
    from wevra.core.composition import AppConfig
    from wevra.web.routes.dispatcher import HtmlDispatcher

HtmlSurface = Literal["page", "partial"]


@runtime_checkable
class HtmlView(Protocol):
    async def render(self, request: Request, renderer: Any) -> Response: ...


@dataclass(frozen=True, slots=True)
class HtmlRouteDefinition:
    path: str
    name: str
    methods: tuple[str, ...]
    surface: HtmlSurface
    view: HtmlView


@dataclass(frozen=True, slots=True)
class ModuleRoutes:
    page_routes: tuple[HtmlRouteDefinition, ...] = ()
    partial_routes: tuple[HtmlRouteDefinition, ...] = ()
    api_routers: tuple[APIRouter, ...] = ()


class RouteCompositionError(CompositionError):
    """Raised when configured module routes cannot be composed safely."""


class ConfiguredRouteSettings(Protocol):
    """Minimal settings shape for route composition.

    This is intentionally narrower than `WebValidationSettings`; runtime route
    registration only needs configured modules and optional route prefixes from
    app composition.
    """

    @property
    def modules(self) -> tuple[str, ...]: ...

    @property
    def app_config(self) -> "AppConfig | None": ...


@dataclass(frozen=True, slots=True)
class _RouteOwner:
    module_name: str
    surface: str

    @property
    def label(self) -> str:
        return f"{self.module_name} {self.surface}"


def load_module_routes(
    module_names: Iterable[str],
    *,
    route_prefixes: Mapping[str, str] | None = None,
) -> ModuleRoutes:
    from wevra.web.routes.discovery import discover_module_routes

    return compose_module_routes(
        (
            (module_name, discover_module_routes(module_name))
            for module_name in module_names
        ),
        route_prefixes=route_prefixes,
    )


def load_configured_module_routes(settings: ConfiguredRouteSettings) -> ModuleRoutes:
    return load_module_routes(
        settings.modules,
        route_prefixes=route_prefixes_from_app_config(settings.app_config),
    )


def route_prefixes_from_app_config(
    app_config: "AppConfig | None",
) -> Mapping[str, str]:
    if app_config is None:
        return {}

    prefixes = app_config.routes.prefixes
    if isinstance(prefixes, Mapping):
        return prefixes

    return {}


def register_configured_module_routes(
    app: FastAPI,
    settings: ConfiguredRouteSettings,
    dispatcher: "HtmlDispatcher",
) -> None:
    register_module_routes(app, dispatcher, load_configured_module_routes(settings))


def register_module_routes(
    app: FastAPI,
    dispatcher: "HtmlDispatcher",
    route_set: ModuleRoutes,
) -> None:
    from wevra.web.routes.dispatcher import register_html_routes

    register_html_routes(app, dispatcher, route_set.page_routes)
    register_html_routes(app, dispatcher, route_set.partial_routes)
    for api_router in route_set.api_routers:
        app.include_router(api_router)


def merge_module_routes(*route_sets: ModuleRoutes) -> ModuleRoutes:
    return ModuleRoutes(
        page_routes=tuple(
            route for route_set in route_sets for route in route_set.page_routes
        ),
        partial_routes=tuple(
            route for route_set in route_sets for route in route_set.partial_routes
        ),
        api_routers=tuple(
            router for route_set in route_sets for router in route_set.api_routers
        ),
    )


def compose_module_routes(
    module_route_sets: Iterable[tuple[str, ModuleRoutes]],
    *,
    route_prefixes: Mapping[str, str] | None = None,
) -> ModuleRoutes:
    prefixes = route_prefixes or {}
    route_names: dict[str, _RouteOwner] = {}
    method_paths: dict[tuple[str, str], _RouteOwner] = {}
    page_routes: list[HtmlRouteDefinition] = []
    partial_routes: list[HtmlRouteDefinition] = []
    api_routers: list[APIRouter] = []

    for module_name, route_set in module_route_sets:
        if not isinstance(route_set, ModuleRoutes):
            raise RouteCompositionError(
                surface_message(
                    "Route surface",
                    module_name,
                    "must be a ModuleRoutes instance.",
                )
            )

        prefix = prefixes.get(module_name, "")
        prefixed_page_routes = tuple(
            _prefixed_route_definition(module_name, prefix, definition)
            for definition in route_set.page_routes
        )
        prefixed_partial_routes = tuple(
            _prefixed_route_definition(module_name, prefix, definition)
            for definition in route_set.partial_routes
        )

        for definition in prefixed_page_routes:
            _record_html_route(
                module_name,
                definition,
                route_names,
                method_paths,
            )
        for definition in prefixed_partial_routes:
            _record_html_route(
                module_name,
                definition,
                route_names,
                method_paths,
            )
        for router in route_set.api_routers:
            _record_api_router(module_name, router, route_names, method_paths)

        page_routes.extend(prefixed_page_routes)
        partial_routes.extend(prefixed_partial_routes)
        api_routers.extend(route_set.api_routers)

    return ModuleRoutes(
        page_routes=tuple(page_routes),
        partial_routes=tuple(partial_routes),
        api_routers=tuple(api_routers),
    )


def _prefixed_route_definition(
    module_name: str,
    prefix: str,
    definition: HtmlRouteDefinition,
) -> HtmlRouteDefinition:
    path = _compose_route_path(module_name, definition.path, prefix)
    if path == definition.path:
        return definition

    return HtmlRouteDefinition(
        path=path,
        name=definition.name,
        methods=definition.methods,
        surface=definition.surface,
        view=definition.view,
    )


def _compose_route_path(module_name: str, path: str, prefix: str) -> str:
    route_path = path.strip()
    if not route_path:
        raise RouteCompositionError(
            f"Route path for configured module {module_name!r} must not be blank."
        )

    if route_path.startswith("/"):
        return route_path

    relative_path = route_path.strip("/")
    route_prefix = _normalise_route_prefix(prefix)
    if not route_prefix:
        return f"/{relative_path}"
    if not relative_path:
        return route_prefix

    return f"{route_prefix}/{relative_path}"


def _normalise_route_prefix(prefix: str) -> str:
    stripped_prefix = prefix.strip()
    if not stripped_prefix or stripped_prefix == "/":
        return ""
    if not stripped_prefix.startswith("/"):
        stripped_prefix = f"/{stripped_prefix}"

    return stripped_prefix.rstrip("/")


def _record_html_route(
    module_name: str,
    definition: HtmlRouteDefinition,
    route_names: dict[str, _RouteOwner],
    method_paths: dict[tuple[str, str], _RouteOwner],
) -> None:
    owner = _RouteOwner(module_name=module_name, surface=definition.surface)
    _record_route_name(definition.name, owner, route_names)
    for method in _normalised_methods(definition.methods, owner, definition.path):
        _record_method_path(method, definition.path, owner, method_paths)


def _record_api_router(
    module_name: str,
    router: APIRouter,
    route_names: dict[str, _RouteOwner],
    method_paths: dict[tuple[str, str], _RouteOwner],
) -> None:
    owner = _RouteOwner(module_name=module_name, surface="api")
    for route in router.routes:
        route_name = getattr(route, "name", None)
        if isinstance(route_name, str) and route_name:
            _record_route_name(route_name, owner, route_names)

        route_path = getattr(route, "path", None)
        route_methods = getattr(route, "methods", None)
        if not isinstance(route_path, str) or route_methods is None:
            continue

        for method in _normalised_methods(route_methods, owner, route_path):
            _record_method_path(method, route_path, owner, method_paths)


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


__all__ = [
    "ConfiguredRouteSettings",
    "HtmlRouteDefinition",
    "HtmlSurface",
    "HtmlView",
    "ModuleRoutes",
    "RouteCompositionError",
    "compose_module_routes",
    "load_configured_module_routes",
    "load_module_routes",
    "merge_module_routes",
    "register_configured_module_routes",
    "register_module_routes",
    "route_prefixes_from_app_config",
]
