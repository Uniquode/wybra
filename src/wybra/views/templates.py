"""Reusable HTML view helpers."""

from dataclasses import dataclass
from typing import Any, ClassVar

from fastapi import Request
from starlette.responses import HTMLResponse

from wybra.template import render_page
from wybra.views.base import View


@dataclass(frozen=True, slots=True)
class TemplateResponse:
    """Deferred template rendering result for a class-based view."""

    request: Request
    template_name: str
    context: dict[str, Any]
    status_code: int = 200

    async def render_response(self) -> HTMLResponse:
        """Resolve the request template capability and render the page."""
        return await render_page(
            self.request,
            self.template_name,
            self.context,
            status_code=self.status_code,
        )


class TemplateView(View):
    """GET-only view base for rendering a declared template."""

    template_name: ClassVar[str | None] = None

    async def get(self, request: Request, **kwargs: Any) -> TemplateResponse:
        context = await self.get_context({}, request, **kwargs)
        return TemplateResponse(request, self.get_template(), context)

    def get_template(self) -> str:
        """Return the declared template name for this view."""
        if self.template_name is None:
            raise ValueError(
                "TemplateView requires template_name or an overridden get_template()."
            )
        return self.template_name

    async def get_context(
        self,
        context: dict[str, Any],
        request: Request,
        **_kwargs: Any,
    ) -> dict[str, Any]:
        """Return the context used to render this request's template."""
        return context


__all__ = [
    "TemplateResponse",
    "TemplateView",
]
