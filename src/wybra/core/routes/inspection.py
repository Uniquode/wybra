"""Installed FastAPI/Starlette route tree inspection."""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from fastapi.routing import APIRoute
from starlette.routing import BaseRoute, Mount, Route, WebSocketRoute
from starlette.staticfiles import StaticFiles

from wybra.core.exceptions import InputValidationError
from wybra.core.routes.contracts import API_PATH_PREFIX, PARTIAL_PATH_PREFIX

ROUTE_ORIGINS_STATE_KEY = "_wybra_route_origins"
ROUTE_PATH_ATTRIBUTE = "__wybra_route_path__"
ROUTE_METHODS_ATTRIBUTE = "__wybra_route_methods__"
ROUTE_TYPE_ATTRIBUTE = "__wybra_route_type__"
ROUTE_TEMPLATE_ATTRIBUTE = "__wybra_template_name__"
PATH_PARAMETER_PATTERN = re.compile(r"{([^}:]+)(?::[^}]+)?}")


class RouteKind(StrEnum):
    HTTP = "http"
    WEBSOCKET = "websocket"
    MOUNT = "mount"
    STATIC = "static"
    UNKNOWN = "unknown"


class RouteType(StrEnum):
    API = "api"
    PAGE = "page"
    PARTIAL = "partial"
    STATIC = "static"
    MOUNT = "mount"
    UNKNOWN = "unknown"


class RouteProblemKind(StrEnum):
    DUPLICATE_NAME = "duplicate-name"
    DUPLICATE_METHOD_PATH = "duplicate-method-path"
    MALFORMED_ROUTE = "malformed-route"
    INCOHERENT_ORIGIN = "incoherent-origin"


@dataclass(frozen=True, slots=True)
class RouteOrigin:
    module_name: str
    router_label: str
    include_prefix: str
    route_name: str | None
    path: str
    methods: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class EndpointShape:
    route_type: RouteType = RouteType.UNKNOWN
    declared_route_type: RouteType | None = None
    accepts_body: bool = False
    accepts_form: bool = False
    path_parameters: tuple[str, ...] = ()
    response_class: str | None = None
    response_media_type: str | None = None
    dependencies: tuple[str, ...] = ()
    template: str | None = None


@dataclass(frozen=True, slots=True)
class RouteRecord:
    id: str
    kind: RouteKind
    path: str
    methods: tuple[str, ...] = ()
    name: str | None = None
    endpoint: str | None = None
    origin: RouteOrigin | None = None
    shape: EndpointShape = field(default_factory=EndpointShape)
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RouteTreeNode:
    id: str
    label: str
    path: str
    kind: RouteKind | None = None
    route_ids: tuple[str, ...] = ()
    children: tuple[RouteTreeNode, ...] = ()
    opaque: bool = False


@dataclass(frozen=True, slots=True)
class RouteProblem:
    kind: RouteProblemKind
    message: str
    route_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RouteInspection:
    tree: RouteTreeNode
    routes: tuple[RouteRecord, ...]
    problems: tuple[RouteProblem, ...] = ()
    warnings: tuple[str, ...] = ()


def route_type(
    route_type: RouteType | str,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Attach explicit route type metadata to an endpoint for route inspection."""

    route_type = RouteType(route_type)

    def decorator(endpoint: Callable[..., Any]) -> Callable[..., Any]:
        setattr(endpoint, ROUTE_TYPE_ATTRIBUTE, route_type.value)
        return endpoint

    return decorator


def route_template(
    template_name: str,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Attach explicit template metadata to an endpoint or view class."""

    if not isinstance(template_name, str) or not template_name.strip():
        raise InputValidationError("Route template name must be a non-blank string.")

    def decorator(endpoint: Callable[..., Any]) -> Callable[..., Any]:
        setattr(endpoint, ROUTE_TEMPLATE_ATTRIBUTE, template_name.strip())
        return endpoint

    return decorator


def route(
    path: str,
    route_type: RouteType | str,
    *,
    template: str | None = None,
    methods: Iterable[str] | None = None,
) -> Callable[[Any], Any]:
    """Attach core route metadata to a function endpoint or view class."""

    if not isinstance(path, str) or not path.strip():
        raise InputValidationError("Route path must be a non-blank string.")
    normalised_path = path.strip()
    if not normalised_path.startswith("/"):
        raise InputValidationError("Route path must start with '/'.")

    resolved_route_type = RouteType(route_type)
    resolved_methods = ()
    if methods is not None:
        resolved_methods = tuple(
            method.strip().upper() if isinstance(method, str) else ""
            for method in methods
        )
        if any(not method for method in resolved_methods):
            raise InputValidationError("Route methods must be non-blank strings.")
    if template is not None and (not isinstance(template, str) or not template.strip()):
        raise InputValidationError("Route template name must be a non-blank string.")

    def decorator(endpoint: Any) -> Any:
        setattr(endpoint, ROUTE_PATH_ATTRIBUTE, normalised_path)
        setattr(endpoint, ROUTE_TYPE_ATTRIBUTE, resolved_route_type.value)
        if resolved_methods:
            setattr(endpoint, ROUTE_METHODS_ATTRIBUTE, resolved_methods)
        if template is not None:
            setattr(endpoint, ROUTE_TEMPLATE_ATTRIBUTE, template.strip())
        return endpoint

    return decorator


def record_route_origin(app: Any, route: BaseRoute, origin: RouteOrigin) -> None:
    """Record observational Wybra origin metadata for an installed route."""

    origins = _route_origins_for_write(app)
    origins[id(route)] = origin


def inspect_route_tree(app: Any) -> RouteInspection:
    """Inspect an installed FastAPI/Starlette application route tree."""

    origin_map = _route_origins_for_read(app)
    route_records: list[RouteRecord] = []
    warnings: list[str] = []
    counter = _RouteIdCounter()
    _collect_routes(
        getattr(app, "routes", ()),
        parent_path="",
        route_records=route_records,
        origin_map=origin_map,
        warnings=warnings,
        counter=counter,
    )
    ordered_routes = tuple(
        sorted(
            route_records,
            key=lambda route: (route.path, route.kind, route.methods, route.name or ""),
        )
    )
    route_ids = {route.id for route in ordered_routes}
    problems = _detect_problems(
        ordered_routes, origin_map=origin_map, route_ids=route_ids
    )
    return RouteInspection(
        tree=_build_route_tree(ordered_routes),
        routes=ordered_routes,
        problems=problems,
        warnings=tuple(warnings),
    )


def _collect_routes(
    routes: Iterable[BaseRoute],
    *,
    parent_path: str,
    name_prefix: str = "",
    route_records: list[RouteRecord],
    origin_map: Mapping[int, RouteOrigin],
    warnings: list[str],
    counter: _RouteIdCounter,
) -> None:
    for route in routes:
        if _is_included_router(route):
            _collect_included_router_routes(
                route,
                route_records=route_records,
                origin_map=origin_map,
                counter=counter,
            )
            continue

        path = _join_paths(parent_path, getattr(route, "path", ""))
        record = _route_record(
            route,
            path=path,
            name_prefix=name_prefix,
            origin_map=origin_map,
            counter=counter,
        )
        route_records.append(record)

        if isinstance(route, Mount) and record.kind != RouteKind.STATIC:
            child_routes = tuple(getattr(route, "routes", ()) or ())
            if child_routes:
                _collect_routes(
                    child_routes,
                    parent_path=path,
                    name_prefix=_child_route_name_prefix(route, record, name_prefix),
                    route_records=route_records,
                    origin_map=origin_map,
                    warnings=warnings,
                    counter=counter,
                )
            else:
                warnings.append(f"Mount {path} is opaque.")


def _collect_included_router_routes(
    route: object,
    *,
    route_records: list[RouteRecord],
    origin_map: Mapping[int, RouteOrigin],
    counter: _RouteIdCounter,
) -> None:
    for candidate in _included_router_candidates(route):
        original_route = getattr(candidate, "original_route", None)
        path = getattr(candidate, "path", None)
        if not isinstance(original_route, BaseRoute) or not isinstance(path, str):
            continue
        route_records.append(
            _route_record(
                original_route,
                path=path,
                name_prefix="",
                origin_map=origin_map,
                counter=counter,
            )
        )


def _is_included_router(route: object) -> bool:
    return callable(getattr(route, "effective_route_contexts", None))


def _included_router_candidates(route: object) -> tuple[object, ...]:
    route_contexts = getattr(route, "effective_route_contexts", None)
    if not callable(route_contexts):
        return ()
    return tuple(route_contexts())


def _route_record(
    route: BaseRoute,
    *,
    path: str,
    name_prefix: str,
    origin_map: Mapping[int, RouteOrigin],
    counter: _RouteIdCounter,
) -> RouteRecord:
    kind = _route_kind(route)
    methods = _normalised_methods(getattr(route, "methods", ()) or ())
    name = _route_name(getattr(route, "name", None), name_prefix=name_prefix)
    endpoint = _endpoint_identifier(getattr(route, "endpoint", None))
    origin = origin_map.get(id(route))
    warnings: list[str] = []
    if kind == RouteKind.UNKNOWN:
        warnings.append(
            f"Route {path} has unsupported route type {type(route).__name__}."
        )

    return RouteRecord(
        id=counter.next(),
        kind=kind,
        path=path,
        methods=methods,
        name=name,
        endpoint=endpoint,
        origin=origin,
        shape=_endpoint_shape(route, path=path, kind=kind),
        warnings=tuple(warnings),
    )


def _route_name(name: object, *, name_prefix: str) -> str | None:
    if not isinstance(name, str) or not name:
        return None
    return f"{name_prefix}:{name}" if name_prefix else name


def _child_route_name_prefix(
    route: Mount,
    record: RouteRecord,
    current_prefix: str,
) -> str:
    route_name = getattr(route, "name", None)
    if isinstance(route_name, str) and route_name:
        return record.name or current_prefix
    return current_prefix


def _endpoint_shape(route: BaseRoute, *, path: str, kind: RouteKind) -> EndpointShape:
    endpoint = getattr(route, "endpoint", None)
    response_class = getattr(route, "response_class", None)
    response_class_name = _response_class_name(response_class)
    response_media_type = getattr(response_class, "media_type", None)
    if isinstance(route, APIRoute) and response_media_type is None:
        response_media_type = getattr(
            getattr(route, "response_class", None),
            "media_type",
            None,
        )

    declared_route_type = _declared_route_type(endpoint)

    return EndpointShape(
        route_type=_route_type(
            path=path,
            kind=kind,
            media_type=response_media_type,
            declared_route_type=declared_route_type,
        ),
        declared_route_type=declared_route_type,
        accepts_body=bool(getattr(route, "body_field", None)),
        accepts_form=_accepts_form(route),
        path_parameters=_path_parameters(route),
        response_class=response_class_name,
        response_media_type=response_media_type,
        dependencies=_dependency_identifiers(route),
        template=_template_name(endpoint),
    )


def _route_kind(route: BaseRoute) -> RouteKind:
    if isinstance(route, (APIRoute, Route)):
        return RouteKind.HTTP
    if isinstance(route, WebSocketRoute):
        return RouteKind.WEBSOCKET
    if isinstance(route, Mount):
        return RouteKind.STATIC if _is_static_mount(route) else RouteKind.MOUNT
    return RouteKind.UNKNOWN


def _route_type(
    *,
    path: str,
    kind: RouteKind,
    media_type: str | None,
    declared_route_type: RouteType | None,
) -> RouteType:
    if declared_route_type is not None:
        return declared_route_type
    if kind == RouteKind.STATIC:
        return RouteType.STATIC
    if kind == RouteKind.MOUNT:
        return RouteType.MOUNT
    if path == API_PATH_PREFIX or path.startswith(f"{API_PATH_PREFIX}/"):
        return RouteType.API
    if path == PARTIAL_PATH_PREFIX or path.startswith(f"{PARTIAL_PATH_PREFIX}/"):
        return RouteType.PARTIAL
    if media_type == "text/html":
        return RouteType.PAGE
    return RouteType.UNKNOWN


def _declared_route_type(endpoint: object) -> RouteType | None:
    explicit_route_type = getattr(endpoint, ROUTE_TYPE_ATTRIBUTE, None)
    if explicit_route_type is None:
        return None
    return RouteType(explicit_route_type)


def _accepts_form(route: BaseRoute) -> bool:
    dependant = getattr(route, "dependant", None)
    body_params = getattr(dependant, "body_params", ()) or ()
    return any(type(param.field_info).__name__ == "Form" for param in body_params)


def _path_parameters(route: BaseRoute) -> tuple[str, ...]:
    path = getattr(route, "path", "")
    if not isinstance(path, str):
        return ()
    return tuple(PATH_PARAMETER_PATTERN.findall(path))


def _dependency_identifiers(route: BaseRoute) -> tuple[str, ...]:
    dependant = getattr(route, "dependant", None)
    dependencies = getattr(dependant, "dependencies", ()) or ()
    identifiers = [
        _endpoint_identifier(getattr(dependency, "call", None))
        for dependency in dependencies
    ]
    return tuple(identifier for identifier in identifiers if identifier is not None)


def _template_name(endpoint: object) -> str | None:
    template_name = getattr(endpoint, ROUTE_TEMPLATE_ATTRIBUTE, None)
    return template_name if isinstance(template_name, str) and template_name else None


def _endpoint_identifier(endpoint: object) -> str | None:
    if endpoint is None:
        return None
    module = getattr(endpoint, "__module__", None)
    qualname = getattr(endpoint, "__qualname__", None)
    if isinstance(module, str) and isinstance(qualname, str):
        return f"{module}.{qualname}"
    name = getattr(endpoint, "__name__", None)
    return name if isinstance(name, str) else None


def _response_class_name(response_class: object) -> str | None:
    if response_class is None:
        return None
    if hasattr(response_class, "__mro__"):
        return getattr(response_class, "__name__", None)
    value = getattr(response_class, "value", None)
    if hasattr(value, "__mro__"):
        return getattr(value, "__name__", None)
    return None


def _normalised_methods(methods: Iterable[object]) -> tuple[str, ...]:
    normalised = {
        method.strip().upper()
        for method in methods
        if isinstance(method, str) and method.strip()
    }
    if "GET" in normalised and "HEAD" in normalised:
        normalised.remove("HEAD")
    return tuple(sorted(normalised))


def _join_paths(parent: str, child: object) -> str:
    child_path = child if isinstance(child, str) and child else ""
    if child_path == "/":
        return parent or "/"
    if not child_path.startswith("/"):
        child_path = f"/{child_path}" if child_path else ""
    if parent in {"", "/"}:
        return child_path or "/"
    return f"{parent.rstrip('/')}{child_path}" or "/"


def _is_static_mount(route: Mount) -> bool:
    route_app = getattr(route, "app", None)
    if isinstance(route_app, StaticFiles):
        return True
    return type(route_app).__name__ in {"ComposedStaticFiles", "NoStaticFiles"}


def _route_origins_for_write(app: Any) -> dict[int, RouteOrigin]:
    state = getattr(app, "state", None)
    if state is None:
        raise TypeError("Route origins can only be recorded on apps with state.")
    origins = getattr(state, ROUTE_ORIGINS_STATE_KEY, None)
    if origins is None:
        origins = {}
        setattr(state, ROUTE_ORIGINS_STATE_KEY, origins)
    return origins


def _route_origins_for_read(app: Any) -> Mapping[int, RouteOrigin]:
    state = getattr(app, "state", None)
    origins = getattr(state, ROUTE_ORIGINS_STATE_KEY, {}) if state is not None else {}
    return origins if isinstance(origins, Mapping) else {}


def _detect_problems(
    routes: tuple[RouteRecord, ...],
    *,
    origin_map: Mapping[int, RouteOrigin],
    route_ids: set[str],
) -> tuple[RouteProblem, ...]:
    problems: list[RouteProblem] = []
    problems.extend(_duplicate_name_problems(routes))
    problems.extend(_duplicate_method_path_problems(routes))
    problems.extend(_malformed_route_problems(routes))
    problems.extend(
        _origin_problems(routes, origin_map=origin_map, route_ids=route_ids)
    )
    return tuple(problems)


def _duplicate_name_problems(
    routes: tuple[RouteRecord, ...],
) -> tuple[RouteProblem, ...]:
    by_name: dict[str, list[RouteRecord]] = defaultdict(list)
    for route in routes:
        if route.name:
            by_name[route.name].append(route)

    return tuple(
        RouteProblem(
            kind=RouteProblemKind.DUPLICATE_NAME,
            message=f"Route name {name!r} is used by {len(named_routes)} routes.",
            route_ids=tuple(route.id for route in named_routes),
        )
        for name, named_routes in sorted(by_name.items())
        if len(named_routes) > 1
    )


def _duplicate_method_path_problems(
    routes: tuple[RouteRecord, ...],
) -> tuple[RouteProblem, ...]:
    by_method_path: dict[tuple[str, str], list[RouteRecord]] = defaultdict(list)
    for route in routes:
        if route.kind != RouteKind.HTTP:
            continue
        for method in route.methods:
            by_method_path[(method, route.path)].append(route)

    return tuple(
        RouteProblem(
            kind=RouteProblemKind.DUPLICATE_METHOD_PATH,
            message=f"{method} {path} is handled by {len(method_routes)} routes.",
            route_ids=tuple(route.id for route in method_routes),
        )
        for (method, path), method_routes in sorted(by_method_path.items())
        if len(method_routes) > 1
    )


def _malformed_route_problems(
    routes: tuple[RouteRecord, ...],
) -> tuple[RouteProblem, ...]:
    problems: list[RouteProblem] = []
    for route in routes:
        if not route.path:
            problems.append(
                RouteProblem(
                    kind=RouteProblemKind.MALFORMED_ROUTE,
                    message=f"Route {route.id} has an empty path.",
                    route_ids=(route.id,),
                )
            )
        if route.kind == RouteKind.HTTP and not route.methods:
            problems.append(
                RouteProblem(
                    kind=RouteProblemKind.MALFORMED_ROUTE,
                    message=f"HTTP route {route.path} has no methods.",
                    route_ids=(route.id,),
                )
            )
    return tuple(problems)


def _origin_problems(
    routes: tuple[RouteRecord, ...],
    *,
    origin_map: Mapping[int, RouteOrigin],
    route_ids: set[str],
) -> tuple[RouteProblem, ...]:
    if len(origin_map) == len([route for route in routes if route.origin is not None]):
        return ()

    origin_route_ids = tuple(route.id for route in routes if route.origin is not None)
    missing = len(origin_map) - len(origin_route_ids)
    if missing <= 0:
        return ()
    return (
        RouteProblem(
            kind=RouteProblemKind.INCOHERENT_ORIGIN,
            message=f"{missing} recorded route origins did not match installed routes.",
            route_ids=tuple(
                route_id for route_id in origin_route_ids if route_id in route_ids
            ),
        ),
    )


def _build_route_tree(routes: tuple[RouteRecord, ...]) -> RouteTreeNode:
    root = _MutableRouteTreeNode(id="node_000", label="/", path="/")
    node_counter = _NodeIdCounter()
    for route in routes:
        current = root
        segments = _path_segments(route.path)
        path = ""
        for segment in segments:
            path = _join_tree_segment(path, segment)
            current = current.child(segment, path=path, node_counter=node_counter)
        current.kind = route.kind
        current.route_ids.append(route.id)
        current.opaque = route.kind in {RouteKind.MOUNT, RouteKind.STATIC}
    return root.freeze()


def _path_segments(path: str) -> tuple[str, ...]:
    if path in {"", "/"}:
        return ("/",)

    segments = tuple(segment for segment in path.strip("/").split("/") if segment)
    if path.endswith("/"):
        return (*segments, "/")
    return segments


def _join_tree_segment(parent: str, segment: str) -> str:
    if segment == "/":
        if parent in {"", "/"}:
            return "/"
        return f"{parent.rstrip('/')}/"
    return _join_paths(parent, segment)


@dataclass(slots=True)
class _MutableRouteTreeNode:
    id: str
    label: str
    path: str
    kind: RouteKind | None = None
    route_ids: list[str] = field(default_factory=list)
    children: dict[str, _MutableRouteTreeNode] = field(default_factory=dict)
    opaque: bool = False

    def child(
        self,
        label: str,
        *,
        path: str,
        node_counter: _NodeIdCounter,
    ) -> _MutableRouteTreeNode:
        if label not in self.children:
            self.children[label] = _MutableRouteTreeNode(
                id=node_counter.next(),
                label=label,
                path=path,
            )
        return self.children[label]

    def freeze(self) -> RouteTreeNode:
        children = tuple(
            child.freeze()
            for child in sorted(self.children.values(), key=lambda item: item.label)
        )
        return RouteTreeNode(
            id=self.id,
            label=self.label,
            path=self.path,
            kind=self.kind,
            route_ids=tuple(self.route_ids),
            children=children,
            opaque=self.opaque and not children,
        )


class _RouteIdCounter:
    def __init__(self) -> None:
        self.value = 0

    def next(self) -> str:
        self.value += 1
        return f"route-{self.value:03d}"


class _NodeIdCounter:
    def __init__(self) -> None:
        self.value = 0

    def next(self) -> str:
        self.value += 1
        return f"node_{self.value:03d}"


__all__ = [
    "EndpointShape",
    "ROUTE_METHODS_ATTRIBUTE",
    "ROUTE_ORIGINS_STATE_KEY",
    "ROUTE_PATH_ATTRIBUTE",
    "ROUTE_TEMPLATE_ATTRIBUTE",
    "ROUTE_TYPE_ATTRIBUTE",
    "RouteInspection",
    "RouteKind",
    "RouteOrigin",
    "RouteProblem",
    "RouteProblemKind",
    "RouteRecord",
    "RouteType",
    "RouteTreeNode",
    "inspect_route_tree",
    "record_route_origin",
    "route",
    "route_template",
    "route_type",
]
