"""Template and static resource composition support."""

from __future__ import annotations

from dataclasses import dataclass
from importlib import resources
from importlib.resources.abc import Traversable
from pathlib import PurePosixPath


class ResourcePathError(ValueError):
    """Raised when a logical resource path is not package-local."""


@dataclass(frozen=True, slots=True)
class PackageResourceSource:
    package: str
    directory: str


@dataclass(frozen=True, slots=True)
class PackageResourceFile:
    source: PackageResourceSource
    logical_path: str
    resource: Traversable


def resolve_package_resource(
    source: PackageResourceSource,
    logical_path: str,
) -> Traversable:
    parts = _logical_path_parts(logical_path)
    return resources.files(source.package).joinpath(source.directory, *parts)


def first_existing_resource(
    sources: tuple[PackageResourceSource, ...],
    logical_path: str,
) -> Traversable | None:
    for source in sources:
        resource = resolve_package_resource(source, logical_path)
        if resource.is_file():
            return resource

    return None


def iter_package_resource_files(
    source: PackageResourceSource,
) -> tuple[PackageResourceFile, ...]:
    root = resources.files(source.package).joinpath(source.directory)
    if not root.is_dir():
        return ()

    return tuple(_iter_resource_files(source, root, ()))


def read_text_resource(
    sources: tuple[PackageResourceSource, ...],
    logical_path: str,
) -> str | None:
    resource = first_existing_resource(sources, logical_path)
    if resource is None:
        return None

    return resource.read_text(encoding="utf-8")


def _logical_path_parts(logical_path: str) -> tuple[str, ...]:
    path = PurePosixPath(logical_path)
    if path.is_absolute():
        raise ResourcePathError(f"Resource path must be relative: {logical_path}")

    parts = tuple(part for part in path.parts if part != ".")
    if not parts or any(part == ".." for part in parts):
        raise ResourcePathError(
            f"Resource path must stay within package: {logical_path}"
        )

    return parts


def _iter_resource_files(
    source: PackageResourceSource,
    root: Traversable,
    path_parts: tuple[str, ...],
) -> tuple[PackageResourceFile, ...]:
    files: list[PackageResourceFile] = []
    for child in sorted(root.iterdir(), key=lambda resource: resource.name):
        child_parts = (*path_parts, child.name)
        if child.is_dir():
            files.extend(_iter_resource_files(source, child, child_parts))
        elif child.is_file():
            files.append(
                PackageResourceFile(
                    source=source,
                    logical_path="/".join(child_parts),
                    resource=child,
                )
            )

    return tuple(files)


__all__ = [
    "PackageResourceFile",
    "PackageResourceSource",
    "ResourcePathError",
    "first_existing_resource",
    "iter_package_resource_files",
    "read_text_resource",
    "resolve_package_resource",
]
