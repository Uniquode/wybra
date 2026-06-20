"""URL path normalisation helpers."""

from __future__ import annotations

from wybra.config.transforms import to_url_path


def matches_path_prefix(path: str, prefix: str) -> bool:
    normalised_prefix = to_url_path(prefix, name="path prefix")
    return path == normalised_prefix or path.startswith(f"{normalised_prefix}/")


__all__ = ("matches_path_prefix",)
