from __future__ import annotations

from fastapi import Request

from wybra.messages.capabilities import MessagesCapability
from wybra.site import get_site
from wybra.template.context import TemplateContext, add_to_context


async def messages_context(
    request: Request,
    context: TemplateContext,
) -> TemplateContext:
    capability = get_site(request.app).optional_capability(MessagesCapability)
    if capability is None:
        return context

    alerts = await capability.renderable_alerts(request)
    return context.with_values(
        alerts=alerts,
        has_alerts=alerts,
        messages_enabled=True,
    )


add_to_context(messages_context)


__all__ = ("messages_context",)
