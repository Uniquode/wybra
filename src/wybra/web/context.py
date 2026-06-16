"""Template context provider composition support."""

from __future__ import annotations

import inspect
import logging
from collections.abc import Awaitable, Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from wybra.core.composition import CompositionError
from wybra.core.diagnostics import diagnostic_message

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class TemplateContext(Mapping[str, Any]):
    values: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "values", MappingProxyType(dict(self.values)))

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any]) -> TemplateContext:
        return cls(values=dict(values))

    def with_values(self, **kwargs: Any) -> TemplateContext:
        return self.merge(kwargs)

    def merge(self, values: Mapping[str, Any]) -> TemplateContext:
        context_values = dict(self.values)
        ignored_keys = tuple(sorted(set(context_values) & set(values)))
        if ignored_keys:
            logger.warning(
                "Ignored template context key overwrite.",
                extra={"template_context_keys": ignored_keys},
            )
        context_values.update(
            {key: value for key, value in values.items() if key not in context_values}
        )
        return TemplateContext.from_mapping(context_values)

    def as_dict(self) -> dict[str, Any]:
        return dict(self.values)

    def __getitem__(self, key: str) -> Any:
        return self.values[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self.values)

    def __len__(self) -> int:
        return len(self.values)


type ContextResult = TemplateContext | Awaitable[TemplateContext]
type ContextProvider = Callable[[Any, TemplateContext], ContextResult]
type ContextProviderRegistration = ContextProvider | Mapping[str, Any]

_CONTEXT_PROVIDERS: dict[str, list[ContextProvider]] = {}
REQUEST_CONTEXT_STATE_ATTRIBUTE = "wybra_web_template_context"


class ContextProviderError(CompositionError):
    """Raised when a context provider cannot be registered."""


def add_to_context(provider: ContextProviderRegistration) -> ContextProvider:
    """Register a provider for the context module that calls this function."""
    registered_provider = _normalise_provider(provider)
    module_name = _calling_module_name()
    _CONTEXT_PROVIDERS.setdefault(module_name, []).append(registered_provider)
    return registered_provider


def _normalise_provider(provider: ContextProviderRegistration) -> ContextProvider:
    if isinstance(provider, Mapping):
        context_values = dict(provider)

        def static_context(_request: Any, context: TemplateContext) -> TemplateContext:
            return context.merge(context_values)

        return static_context

    if not callable(provider):
        raise ContextProviderError("Context provider must be callable.")

    return provider


def get_context_providers(module_name: str) -> tuple[ContextProvider, ...]:
    return tuple(_CONTEXT_PROVIDERS.get(module_name, ()))


def validate_context_providers(
    providers: Iterable[ContextProvider],
) -> tuple[ContextProvider, ...]:
    validated_providers = tuple(providers)
    for provider in validated_providers:
        if not callable(provider):
            raise ContextProviderError("Context provider must be callable.")

    return validated_providers


async def resolve_context_providers(
    providers: Iterable[ContextProvider],
    request: Any,
    *,
    initial_context: TemplateContext | None = None,
) -> TemplateContext:
    context = initial_context if initial_context is not None else TemplateContext()
    for provider in providers:
        context = await _call_provider(provider, request, context)

    return context


def set_request_context(request: Any, context: TemplateContext) -> None:
    setattr(request.state, REQUEST_CONTEXT_STATE_ATTRIBUTE, context)


def get_request_context(request: Any) -> dict[str, Any]:
    context = getattr(request.state, REQUEST_CONTEXT_STATE_ATTRIBUTE, None)
    if context is None:
        return {}
    if isinstance(context, TemplateContext):
        return context.as_dict()

    raise ContextProviderError(
        "Stored request template context must be a TemplateContext; migrate raw "
        "mapping values with TemplateContext.from_mapping(...)."
    )


def clear_context_providers(module_name: str | None = None) -> None:
    if module_name is None:
        _CONTEXT_PROVIDERS.clear()
        return

    _CONTEXT_PROVIDERS.pop(module_name, None)


def _calling_module_name() -> str:
    frame = inspect.currentframe()
    calling_frame = frame.f_back if frame is not None else None
    provider_frame = calling_frame.f_back if calling_frame is not None else None
    module_name = (
        provider_frame.f_globals.get("__name__") if provider_frame is not None else None
    )
    if not isinstance(module_name, str) or not module_name:
        raise ContextProviderError("Context provider module could not be resolved.")

    return module_name


async def _call_provider(
    provider: ContextProvider,
    request: Any,
    context: TemplateContext,
) -> TemplateContext:
    provider_context = provider(request, context)
    if inspect.isawaitable(provider_context):
        provider_context = await provider_context
    if not isinstance(provider_context, TemplateContext):
        raise ContextProviderError(
            diagnostic_message(
                f"Context provider {_provider_name(provider)}",
                "must return a TemplateContext.",
            )
        )

    for key, _value in provider_context.items():
        if not isinstance(key, str):
            raise ContextProviderError(
                diagnostic_message(
                    f"Context provider {_provider_name(provider)}",
                    "returned a non-string template context key.",
                )
            )

    return provider_context


def _provider_name(provider: ContextProvider) -> str:
    provider_module = getattr(provider, "__module__", "")
    provider_name = getattr(provider, "__qualname__", repr(provider))
    if provider_module:
        return f"{provider_module}.{provider_name}"

    return str(provider_name)


__all__ = [
    "ContextProvider",
    "ContextProviderError",
    "ContextProviderRegistration",
    "ContextResult",
    "REQUEST_CONTEXT_STATE_ATTRIBUTE",
    "TemplateContext",
    "add_to_context",
    "clear_context_providers",
    "get_context_providers",
    "get_request_context",
    "resolve_context_providers",
    "set_request_context",
    "validate_context_providers",
]
