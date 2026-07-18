"""Typed contracts for rendering one bound form field."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from markupsafe import Markup

if TYPE_CHECKING:
    from wybra.forms.fields import Field, Form
    from wybra.forms.rendering import TemplateFormRenderer


@dataclass(frozen=True, slots=True)
class FormRenderContext:
    """Presentation context supplied to one field renderer."""

    form: Form
    renderer: TemplateFormRenderer
    widget: str | None = None
    attr: Mapping[str, str | bool] | None = None


class FieldRenderer(Protocol):
    """Render one already-bound form field."""

    async def render(self, field: Field, context: FormRenderContext) -> Markup: ...


__all__ = ("FieldRenderer", "FormRenderContext")
