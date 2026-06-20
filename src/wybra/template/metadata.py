from __future__ import annotations

from collections.abc import Callable
from typing import Any

from wybra.core import InputValidationError

ROUTE_TEMPLATE_ATTRIBUTE = "__wybra_template_name__"


def route_template(
    template_name: str,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Attach explicit template metadata to an endpoint for route inspection."""

    if not isinstance(template_name, str) or not template_name.strip():
        raise InputValidationError("Route template name must be a non-blank string.")

    def decorator(endpoint: Callable[..., Any]) -> Callable[..., Any]:
        setattr(endpoint, ROUTE_TEMPLATE_ATTRIBUTE, template_name.strip())
        return endpoint

    return decorator


__all__ = ("ROUTE_TEMPLATE_ATTRIBUTE", "route_template")
