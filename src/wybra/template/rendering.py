from __future__ import annotations

from typing import Any

from fastapi import Request
from fastapi.responses import HTMLResponse

from wybra.errors.diagnostics import structured_error
from wybra.site import SiteCapabilityError, get_site
from wybra.template.capabilities import TemplateCapability


def template_capability_from(request: Request) -> TemplateCapability:
    try:
        return get_site(request.app).require_capability(TemplateCapability)
    except SiteCapabilityError as exc:
        raise SiteCapabilityError(
            structured_error(
                "Missing template capability provider",
                requirement=(
                    "configure wybra.template or another TemplateCapability provider "
                    "when template rendering is used"
                ),
            )
        ) from exc


async def render_page(
    request: Request,
    template_name: str,
    context: dict[str, Any] | None = None,
    *,
    status_code: int = 200,
) -> HTMLResponse:
    return await template_capability_from(request).render_page(
        request,
        template_name,
        context or {},
        status_code=status_code,
    )


async def render_partial(
    request: Request,
    template_name: str,
    context: dict[str, Any] | None = None,
    *,
    status_code: int = 200,
) -> HTMLResponse:
    return await template_capability_from(request).render_partial(
        request,
        template_name,
        context or {},
        status_code=status_code,
    )


__all__ = ("render_page", "render_partial", "template_capability_from")
