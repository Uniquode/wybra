from __future__ import annotations

from jinja2.exceptions import TemplateNotFound

from wybra.core.exceptions import Http404
from wybra.errors.mappings import ErrorMapping, translate_exception


def test_error_mapping_translates_exception_and_preserves_cause() -> None:
    original = TemplateNotFound("pages/missing.html")
    mapping = ErrorMapping(
        exception_type=TemplateNotFound,
        target_exception_type=Http404,
        detail="Page template not found.",
    )

    translated = translate_exception(original, mappings=(mapping,))

    assert isinstance(translated, Http404)
    assert translated.detail == "Page template not found."
    assert translated.__cause__ is original


def test_error_mapping_returns_original_when_no_mapping_matches() -> None:
    original = RuntimeError("boom")

    assert translate_exception(original, mappings=()) is original
