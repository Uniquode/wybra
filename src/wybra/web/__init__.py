"""Web foundation module contract."""

from __future__ import annotations

from wybra.site import Site
from wybra.web.config import module_config


async def setup_site(_site: Site) -> None:
    """Web foundation setup hook."""


__all__ = [
    "module_config",
    "setup_site",
]
