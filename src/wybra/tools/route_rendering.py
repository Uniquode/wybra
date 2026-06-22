from __future__ import annotations

import html
import json
from collections.abc import Iterator, Mapping
from dataclasses import asdict
from enum import StrEnum
from typing import Any, cast

from wybra.core.routes import (
    RouteInspection,
    RouteProblem,
    RouteRecord,
    RouteTreeNode,
    RouteType,
)

RouteOriginLabel = tuple[str, str]


class RouteOutputFormat(StrEnum):
    SUCCINCT = "succinct"
    GRAPH = "graph"
    MERMAID = "mermaid"
    JSON = "json"


def render_inspection(
    inspection: RouteInspection,
    output_format: RouteOutputFormat,
) -> str:
    match output_format:
        case RouteOutputFormat.SUCCINCT:
            return render_succinct(inspection)
        case RouteOutputFormat.GRAPH:
            return render_graph(inspection)
        case RouteOutputFormat.MERMAID:
            return render_mermaid(inspection)
        case RouteOutputFormat.JSON:
            return render_json(inspection)
        case _:
            raise ValueError(f"Unsupported route output format: {output_format!r}")


def render_succinct(inspection: RouteInspection) -> str:
    lines = [_succinct_line(record) for record in inspection.routes]
    lines.extend(_problem_lines(inspection.problems))
    return "\n".join(lines)


def render_graph(inspection: RouteInspection) -> str:
    lines: list[str] = []
    route_map = {route.id: route for route in inspection.routes}
    _render_graph_tree(inspection.tree, lines=lines, route_map=route_map)
    if inspection.problems:
        lines.append("")
        lines.extend(_problem_lines(inspection.problems))
    return "\n".join(lines)


def render_mermaid(inspection: RouteInspection) -> str:
    lines = ["flowchart TD"]
    route_map = {route.id: route for route in inspection.routes}
    _render_mermaid_node(inspection.tree, lines=lines, route_map=route_map)
    return "\n".join(lines)


def render_json(inspection: RouteInspection) -> str:
    return json.dumps(_inspection_to_dict(inspection), indent=2, sort_keys=True)


def _succinct_line(record: RouteRecord) -> str:
    methods = ",".join(record.methods) if record.methods else record.kind.value
    name = f" name={record.name}" if record.name else ""
    origin = (
        f" origin={record.origin.module_name}:{record.origin.router_label}"
        if record.origin
        else ""
    )
    shape = f" shape={record.shape.route_type.value}"
    return f"{methods:<12} {record.path}{name}{origin}{shape}"


def _problem_lines(problems: tuple[RouteProblem, ...]) -> list[str]:
    return [f"problem {problem.kind.value}: {problem.message}" for problem in problems]


def _render_graph_tree(
    node: RouteTreeNode,
    *,
    lines: list[str],
    route_map: Mapping[str, RouteRecord],
) -> None:
    root_route = next(
        (child for child in node.children if child.path == "/" and child.route_ids),
        None,
    )
    root_route_ids = () if root_route is None else root_route.route_ids
    root_origin = _graph_node_origin(root_route_ids, route_map=route_map)
    lines.append(
        _graph_node_label(
            "/",
            root_route_ids,
            route_map=route_map,
            inherited_origin=None,
            is_root=True,
        )
    )

    children = tuple(child for child in node.children if child is not root_route)
    for index, child in enumerate(children):
        _render_graph_branch(
            child,
            lines=lines,
            route_map=route_map,
            prefix="",
            is_last=index == len(children) - 1,
            inherited_origin=root_origin,
        )


def _render_graph_branch(
    node: RouteTreeNode,
    *,
    lines: list[str],
    route_map: Mapping[str, RouteRecord],
    prefix: str,
    is_last: bool,
    inherited_origin: RouteOriginLabel | None,
) -> None:
    connector = "└─ " if is_last else "├─ "
    node_origin = _graph_node_origin(node.route_ids, route_map=route_map)
    subtree_origin = None
    if node_origin is None:
        subtree_origin = _graph_subtree_origin(node, route_map=route_map)
    label_origin = node_origin if node_origin is not None else subtree_origin
    label = _graph_node_label(
        _graph_path_label(node),
        node.route_ids,
        route_map=route_map,
        inherited_origin=inherited_origin,
        group_origin=label_origin,
        is_root=False,
    )
    lines.append(f"{prefix}{connector}{label}")
    child_prefix = f"{prefix}{'   ' if is_last else '│  '}"
    next_origin = inherited_origin if label_origin is None else label_origin
    for index, child in enumerate(node.children):
        _render_graph_branch(
            child,
            lines=lines,
            route_map=route_map,
            prefix=child_prefix,
            is_last=index == len(node.children) - 1,
            inherited_origin=next_origin,
        )


def _graph_path_label(node: RouteTreeNode) -> str:
    if node.label == "/":
        return "/"
    return "/" if node.path == "/" else f"/{node.label}"


def _graph_node_label(
    path_label: str,
    route_ids: tuple[str, ...],
    *,
    route_map: Mapping[str, RouteRecord],
    inherited_origin: RouteOriginLabel | None,
    group_origin: RouteOriginLabel | None = None,
    is_root: bool = False,
) -> str:
    if not route_ids:
        if group_origin is not None and group_origin != inherited_origin:
            return f"{path_label} {_graph_origin_label(group_origin)}"
        return path_label

    route_labels = tuple(
        _graph_route_label(
            path_label,
            route_map[route_id],
            inherited_origin=inherited_origin,
            is_root=is_root,
        )
        for route_id in route_ids
    )
    return " | ".join(route_labels)


def _graph_route_label(
    path_label: str,
    record: RouteRecord,
    *,
    inherited_origin: RouteOriginLabel | None,
    is_root: bool,
) -> str:
    parts = [path_label, _graph_route_methods(record)]
    if not is_root:
        parts.reverse()
    if record.name:
        parts.append(record.name)
    origin = _graph_route_origin(record)
    if origin is not None and origin != inherited_origin:
        parts.append(_graph_origin_label(origin))
    if record.shape.route_type is not RouteType.UNKNOWN:
        parts.append(f"({record.shape.route_type.value})")
    return " ".join(parts)


def _graph_node_origin(
    route_ids: tuple[str, ...],
    *,
    route_map: Mapping[str, RouteRecord],
) -> RouteOriginLabel | None:
    if not route_ids:
        return None

    origins = tuple(_graph_route_origin(route_map[route_id]) for route_id in route_ids)
    first_origin = origins[0]
    if first_origin is None:
        return None
    return first_origin if all(origin == first_origin for origin in origins) else None


def _graph_subtree_origin(
    node: RouteTreeNode,
    *,
    route_map: Mapping[str, RouteRecord],
) -> RouteOriginLabel | None:
    origins = tuple(_graph_descendant_origins(node, route_map=route_map))
    if len(origins) <= 1:
        return None

    first_origin = origins[0]
    if first_origin is None:
        return None
    return first_origin if all(origin == first_origin for origin in origins) else None


def _graph_descendant_origins(
    node: RouteTreeNode,
    *,
    route_map: Mapping[str, RouteRecord],
) -> Iterator[RouteOriginLabel | None]:
    for route_id in node.route_ids:
        yield _graph_route_origin(route_map[route_id])
    for child in node.children:
        yield from _graph_descendant_origins(child, route_map=route_map)


def _graph_route_origin(record: RouteRecord) -> RouteOriginLabel | None:
    if record.origin is None:
        return None
    return (record.origin.module_name, record.origin.router_label)


def _graph_origin_label(origin: RouteOriginLabel) -> str:
    return f"{origin[0]}:{origin[1]}"


def _graph_route_methods(record: RouteRecord) -> str:
    methods = (
        ",".join(method.lower() for method in record.methods)
        if record.methods
        else record.kind.value
    )
    return f"[{methods}]"


def _render_mermaid_node(
    node: RouteTreeNode,
    *,
    lines: list[str],
    route_map: Mapping[str, RouteRecord],
) -> None:
    label = _mermaid_label(node, route_map=route_map)
    lines.append(f"  {node.id}[{label}]")
    for child in node.children:
        lines.append(f"  {node.id} --> {child.id}")
        _render_mermaid_node(child, lines=lines, route_map=route_map)


def _mermaid_label(
    node: RouteTreeNode,
    *,
    route_map: Mapping[str, RouteRecord],
) -> str:
    details = [node.path]
    if node.kind:
        details.append(node.kind.value)
    if node.route_ids:
        details.extend(
            _route_detail(route_map[route_id]) for route_id in node.route_ids
        )
    return json.dumps("<br/>".join(html.escape(part) for part in details))


def _route_detail(record: RouteRecord) -> str:
    methods = ",".join(record.methods) if record.methods else record.kind.value
    name = f" {record.name}" if record.name else ""
    origin = (
        f" {record.origin.module_name}:{record.origin.router_label}"
        if record.origin
        else ""
    )
    return f"{methods}{name}{origin} {record.shape.route_type.value}"


def _inspection_to_dict(inspection: RouteInspection) -> dict[str, object]:
    return {
        "tree": _tree_to_dict(inspection.tree),
        "routes": [_dataclass_to_dict(route) for route in inspection.routes],
        "problems": [_dataclass_to_dict(problem) for problem in inspection.problems],
        "warnings": list(inspection.warnings),
    }


def _tree_to_dict(node: RouteTreeNode) -> dict[str, object]:
    data = _dataclass_to_dict(node)
    data["children"] = [_tree_to_dict(child) for child in node.children]
    return data


def _dataclass_to_dict(value: Any) -> dict[str, object]:
    data = asdict(value)
    return cast(dict[str, object], _stringify_enums(data))


def _stringify_enums(value: object) -> object:
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, dict):
        return {key: _stringify_enums(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_stringify_enums(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_stringify_enums(item) for item in value)
    return value


__all__ = [
    "RouteOutputFormat",
    "render_graph",
    "render_inspection",
    "render_json",
    "render_mermaid",
    "render_succinct",
]
