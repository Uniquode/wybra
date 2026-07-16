from __future__ import annotations

from collections.abc import Awaitable, Mapping
from dataclasses import dataclass
from inspect import isawaitable
from typing import TYPE_CHECKING, cast

from fastapi import Request

from wybra.forms.csrf import request_form_data
from wybra.forms.fields import Form, FormResult
from wybra.site import get_site

if TYPE_CHECKING:
    from wybra.messages import MessagesCapability

_UNSET = object()


@dataclass(frozen=True, slots=True)
class FormPostResult[FormT: Form]:
    form: FormT
    result: FormResult
    committed: bool

    @property
    def is_valid(self) -> bool:
        return self.result.is_valid


class FormPostHandler[FormT: Form]:
    success_message: str | None = None
    failure_message: str | None = None

    def __init__(
        self,
        form: FormT,
        *,
        messages: MessagesCapability | None = None,
        success_message: str | None | object = _UNSET,
        failure_message: str | None | object = _UNSET,
    ) -> None:
        self.form = form
        self.messages = messages
        if success_message is not _UNSET:
            self.success_message = cast(str | None, success_message)
        if failure_message is not _UNSET:
            self.failure_message = cast(str | None, failure_message)

    async def handle(
        self,
        request: Request,
        data: Mapping[str, object] | None = None,
    ) -> FormPostResult[FormT]:
        form_data = data if data is not None else await request_form_data(request)
        result = await self.form.parse(form_data)
        if not result.is_valid:
            await self.add_failure_message(request)
            return FormPostResult(
                form=self.form,
                result=result,
                committed=False,
            )

        await _maybe_await(self.commit(request, self.form))
        result = self.form.result
        if not result.is_valid:
            await self.add_failure_message(request)
            return FormPostResult(
                form=self.form,
                result=result,
                committed=False,
            )

        await self.add_success_message(request)
        return FormPostResult(
            form=self.form,
            result=result,
            committed=True,
        )

    def commit(
        self,
        request: Request,
        form: FormT,
    ) -> Awaitable[None] | None:
        return None

    def get_success_message(self) -> str | None:
        return self.success_message

    def get_failure_message(self) -> str | None:
        return self.failure_message

    async def add_success_message(self, request: Request) -> None:
        message = self.get_success_message()
        messages = self.resolve_messages(request)
        if message is not None and messages is not None:
            await messages.success(request, message)

    async def add_failure_message(self, request: Request) -> None:
        message = self.get_failure_message()
        messages = self.resolve_messages(request)
        if message is not None and messages is not None:
            await messages.error(request, message)

    def resolve_messages(self, request: Request) -> MessagesCapability | None:
        if self.messages is not None:
            return self.messages

        from wybra.messages import MessagesCapability

        return get_site(request.app).optional_capability(MessagesCapability)


async def _maybe_await[ReturnT](value: Awaitable[ReturnT] | ReturnT) -> ReturnT:
    if isawaitable(value):
        return await cast(Awaitable[ReturnT], value)
    return value


__all__ = (
    "FormPostHandler",
    "FormPostResult",
)
