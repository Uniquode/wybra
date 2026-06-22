from __future__ import annotations

from fastapi import Request

from wybra.core.routes.contracts import PARTIAL_PATH_PREFIX
from wybra.forms.capabilities import FormsCapability
from wybra.forms.csrf import request_csrf_response_finalisation
from wybra.template.context import TemplateContext, add_to_context


def forms_context(request: Request, context: TemplateContext) -> TemplateContext:
    capability = request.app.state.site.require_capability(FormsCapability)
    if not request.url.path.startswith(PARTIAL_PATH_PREFIX.rstrip("/") + "/"):
        request_csrf_response_finalisation(request)
    return context.with_layer(capability.token_context(request))


add_to_context(forms_context)


__all__ = ("forms_context",)
