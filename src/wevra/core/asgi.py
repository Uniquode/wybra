"""ASGI application loading helpers for host app entry points."""

from __future__ import annotations

import sys
from collections.abc import Callable

from wevra.core.exceptions import ConfigurationError


def load_asgi_app[AppT](create_app: Callable[[], AppT]) -> AppT:
    """Load an ASGI app and report configuration failures without a traceback."""
    try:
        return create_app()
    except ConfigurationError as exc:
        message = f"Application configuration failed: {exc}"
        print(message, file=sys.stderr)
        raise SystemExit(message) from None


__all__ = ("load_asgi_app",)
