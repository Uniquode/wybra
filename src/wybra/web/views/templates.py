"""Reusable HTML view helpers."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from inspect import isawaitable
from typing import Any, cast

from fastapi import Request
from fastapi.responses import Response

from wybra.template import TemplateCapability

type ContextBuilder = Callable[[Request], dict[str, Any] | Awaitable[dict[str, Any]]]


async def resolve_context(
    builder: ContextBuilder | None, request: Request
) -> dict[str, Any]:
    if builder is None:
        return {}

    context = builder(request)
    if isawaitable(context):
        return cast("dict[str, Any]", await context)

    return context


@dataclass(frozen=True, slots=True)
class TemplateView:
    template_name: str
    context_builder: ContextBuilder | None = None

    async def render(self, request: Request, renderer: TemplateCapability) -> Response:
        context = await resolve_context(self.context_builder, request)
        return renderer.render_page(request, self.template_name, context)


__all__ = [
    "ContextBuilder",
    "TemplateView",
    "resolve_context",
]
