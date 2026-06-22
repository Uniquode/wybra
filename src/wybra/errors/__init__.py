"""Error handling module contract."""

from __future__ import annotations

from typing import TYPE_CHECKING

from wybra.errors.config import module_config
from wybra.errors.validation import validation_targets

if TYPE_CHECKING:
    from wybra.site import Site


async def setup_site(site: Site) -> None:
    from wybra.errors.capabilities import setup_site as setup_errors_site

    await setup_errors_site(site)


__all__ = ("module_config", "setup_site", "validation_targets")
