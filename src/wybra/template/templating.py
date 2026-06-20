"""Template loading for composed module package sources."""

from __future__ import annotations

from pathlib import Path

from jinja2 import BaseLoader, ChoiceLoader, DictLoader, FileSystemLoader, PackageLoader

from wybra.core.resources import PackageResourceSource


def build_template_loader(
    *,
    template_sources: tuple[PackageResourceSource, ...] = (),
    template_root: Path | None = None,
) -> BaseLoader:
    loaders: list[BaseLoader] = []
    if template_root is not None:
        loaders.append(FileSystemLoader(str(template_root)))

    loaders.extend(
        PackageLoader(source.package, source.directory) for source in template_sources
    )
    if not loaders:
        return DictLoader({})
    if len(loaders) == 1:
        return loaders[0]
    return ChoiceLoader(loaders)


__all__ = ["build_template_loader"]
