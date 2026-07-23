"""Router integration for class-based views."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

from fastapi import Request
from fastapi.routing import APIRouter
from starlette.responses import Response

from wybra.core.routes import (
    ROUTE_METHODS_ATTRIBUTE,
    ROUTE_PATH_ATTRIBUTE,
    ROUTE_TYPE_ATTRIBUTE,
    RouteType,
    route,
)
from wybra.scopes import bind_scope_target
from wybra.views.base import View


class ViewRegistrationError(ValueError):
    """Raised when a class-based view cannot be registered on a router."""


@dataclass(frozen=True, slots=True)
class ViewRoute:
    """One router-relative route exposed by a class-based view."""

    path: str
    methods: tuple[str, ...]
    name_suffix: str | None = None
    path_parameter: str | None = None
    dispatch_kwargs: Mapping[str, object] = field(default_factory=dict)


class ViewRouter(APIRouter):
    """An application router with a declarative class-based view decorator."""

    def view(
        self,
        path: str,
        route_type: RouteType | str = RouteType.PAGE,
        *,
        template: str | None = None,
        methods: Iterable[str] | None = None,
        name: str | None = None,
    ):
        """Declare and register a class-based view at a router-relative path."""

        def decorator(view_class: type[View]) -> type[View]:
            route(
                path,
                route_type,
                template=template,
                methods=methods,
            )(view_class)
            register_view(self, view_class, name=name)
            return view_class

        return decorator


def register_view(
    router: APIRouter,
    view_class: type[View],
    *,
    name: str | None = None,
) -> None:
    """Register a route-decorated view class on an existing router."""
    path = getattr(view_class, ROUTE_PATH_ATTRIBUTE, None)
    route_type = getattr(view_class, ROUTE_TYPE_ATTRIBUTE, None)
    if not isinstance(path, str) or not isinstance(route_type, str):
        raise ViewRegistrationError(
            "View classes must be decorated with route(...) before registration."
        )

    for view_route in _view_routes(view_class, path):
        endpoint = _endpoint_for(view_class, view_route, route_type)
        endpoint_name = _route_name(name or view_class.__name__, view_route)
        endpoint.__name__ = endpoint_name
        route(view_route.path, route_type, methods=view_route.methods)(endpoint)
        router.add_api_route(
            view_route.path,
            endpoint,
            methods=list(view_route.methods),
            name=endpoint_name,
        )


def _view_methods(view_class: type[View]) -> tuple[str, ...]:
    """Return the explicitly declared or implemented HTTP methods for a view."""
    instance = view_class()
    supported = instance._allowed_methods()
    declared = getattr(view_class, ROUTE_METHODS_ATTRIBUTE, ())
    methods = tuple(declared) if declared else supported
    unsupported = tuple(method for method in methods if method not in supported)
    if unsupported:
        raise ViewRegistrationError(
            f"View {view_class.__name__} declares unsupported method(s): "
            + ", ".join(unsupported)
            + "."
        )
    if not methods:
        raise ViewRegistrationError(
            f"View {view_class.__name__} does not define an HTTP method."
        )
    return methods


def _view_routes(view_class: type[View], path: str) -> tuple[ViewRoute, ...]:
    """Return registered routes, allowing resource views to expand a base path."""
    route_definitions = getattr(view_class, "route_definitions", None)
    if callable(route_definitions):
        routes = route_definitions(path)
        if not isinstance(routes, Sequence) or not all(
            isinstance(view_route, ViewRoute) for view_route in routes
        ):
            raise ViewRegistrationError(
                f"View {view_class.__name__} returned invalid route definitions."
            )
        return tuple(routes)
    return (ViewRoute(path, _view_methods(view_class)),)


def _endpoint_for(
    view_class: type[View],
    view_route: ViewRoute,
    route_type: str,
):
    """Create a FastAPI endpoint that dispatches a fresh view instance."""
    dispatch_kwargs = _dispatch_kwargs(
        view_class,
        view_route,
        route_type,
    )
    if view_route.path_parameter is None:

        async def endpoint(request: Request) -> Response:
            return await view_class().dispatch(request, **dispatch_kwargs)

        return bind_scope_target(endpoint, view_class)

    if view_route.path_parameter == "action":

        async def endpoint(request: Request, action: str) -> Response:
            kwargs = {"action": action, **dispatch_kwargs}
            return await view_class().dispatch(request, **kwargs)

        return bind_scope_target(endpoint, view_class)

    if view_route.path_parameter != "id":
        raise ViewRegistrationError(
            "Generic view route path parameters must currently be named 'id'."
        )

    async def endpoint(request: Request, id: str) -> Response:
        kwargs = {"id": id, **dispatch_kwargs}
        return await view_class().dispatch(request, **kwargs)

    return bind_scope_target(endpoint, view_class)


def _dispatch_kwargs(
    view_class: type[View],
    view_route: ViewRoute,
    route_type: str,
) -> dict[str, object]:
    """Pass registration representation only to generic resource views."""
    dispatch_kwargs = dict(view_route.dispatch_kwargs)
    from wybra.views.generic import GenericView

    if issubclass(view_class, GenericView):
        dispatch_kwargs["_route_type"] = route_type
        dispatch_kwargs["_collection_path"] = _collection_path(view_route.path)
    return dispatch_kwargs


def _collection_path(path: str) -> str:
    """Return the collection component of a generic resource route path."""
    return path.split("/{", maxsplit=1)[0].removesuffix("/bulk") or "/"


def _route_name(base_name: str, view_route: ViewRoute) -> str:
    if view_route.name_suffix is None:
        return base_name
    return f"{base_name}:{view_route.name_suffix}"


__all__ = [
    "ViewRegistrationError",
    "ViewRoute",
    "ViewRouter",
    "register_view",
]
