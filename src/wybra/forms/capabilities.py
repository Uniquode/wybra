from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from fastapi import Request
from fastapi.responses import Response
from starlette.datastructures import FormData

from wybra.forms.csrf import (
    CsrfProtector,
    csrf_response_finalisation_requested,
    request_form_data,
)
from wybra.forms.security import is_form_content_type, is_safe_method


@runtime_checkable
class FormsCapability(Protocol):
    def token_context(self, request: Request) -> dict[str, Any]: ...

    def finalise_response(self, request: Request, response: Response) -> None: ...

    async def validate_request(self, request: Request) -> bool: ...

    async def request_form_data(self, request: Request) -> FormData: ...

    def is_form_content_type(self, content_type: str) -> bool: ...

    def is_safe_method(self, method: str) -> bool: ...


@dataclass(frozen=True, slots=True)
class DefaultFormsCapability:
    csrf: CsrfProtector

    def token_context(self, request: Request) -> dict[str, Any]:
        return self.csrf.token_context(request)

    def finalise_response(self, request: Request, response: Response) -> None:
        if csrf_response_finalisation_requested(request):
            self.csrf.set_cookie(request, response)

    async def validate_request(self, request: Request) -> bool:
        return await self.csrf.validate_request(request)

    async def request_form_data(self, request: Request) -> FormData:
        return await request_form_data(request)

    def is_form_content_type(self, content_type: str) -> bool:
        return is_form_content_type(content_type)

    def is_safe_method(self, method: str) -> bool:
        return is_safe_method(method)


def forms_provider_configured(modules: tuple[str, ...]) -> bool:
    if "wybra.forms" in modules:
        return True
    return any(
        _module_provides_forms_capability(module_name) for module_name in modules
    )


def _module_provides_forms_capability(module_name: str) -> bool:
    from importlib import import_module

    try:
        module = import_module(module_name)
    except ModuleNotFoundError:
        return False
    return getattr(module, "provides_forms_capability", False) is True


__all__ = (
    "DefaultFormsCapability",
    "FormsCapability",
    "forms_provider_configured",
)
