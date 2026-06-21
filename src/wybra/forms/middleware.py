from __future__ import annotations

from collections.abc import Awaitable, Callable

from fastapi import Request
from fastapi.responses import Response

from wybra.forms.capabilities import FormsCapability
from wybra.site import Site, get_site

FORMS_RESPONSE_FINALISATION_MIDDLEWARE_STATE_ATTRIBUTE = (
    "wybra_forms_response_finalisation_middleware_registered"
)


def register_forms_response_finalisation_middleware(site: Site) -> None:
    if getattr(
        site.app.state,
        FORMS_RESPONSE_FINALISATION_MIDDLEWARE_STATE_ATTRIBUTE,
        False,
    ):
        return

    site.app.middleware("http")(forms_response_finalisation_middleware)
    setattr(
        site.app.state,
        FORMS_RESPONSE_FINALISATION_MIDDLEWARE_STATE_ATTRIBUTE,
        True,
    )


async def forms_response_finalisation_middleware(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    response = await call_next(request)
    site = get_site(request.app)
    site.require_capability(FormsCapability).finalise_response(request, response)
    return response


__all__ = (
    "FORMS_RESPONSE_FINALISATION_MIDDLEWARE_STATE_ATTRIBUTE",
    "forms_response_finalisation_middleware",
    "register_forms_response_finalisation_middleware",
)
