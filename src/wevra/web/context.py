"""Template context provider composition support."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Iterable, Mapping, Set
from typing import Any

from wevra.core.composition import CompositionError
from wevra.core.diagnostics import diagnostic_message

type ContextValue = Mapping[str, Any]
type ContextResult = ContextValue | Awaitable[ContextValue]
type ContextProvider = Callable[[Any], ContextResult]
type ContextProviderRegistration = ContextProvider | ContextValue

_CONTEXT_PROVIDERS: dict[str, list[ContextProvider]] = {}
REQUEST_CONTEXT_STATE_ATTRIBUTE = "wevra_web_template_context"


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
        context = dict(provider)

        def static_context(request: Any) -> ContextValue:
            del request
            return dict(context)

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
    reserved_keys: Set[str] = frozenset(),
) -> dict[str, Any]:
    context: dict[str, Any] = {}
    for provider in providers:
        provider_context = await _call_provider(provider, request)
        _merge_provider_context(context, provider_context, reserved_keys, provider)

    return context


def set_request_context(request: Any, context: Mapping[str, Any]) -> None:
    setattr(request.state, REQUEST_CONTEXT_STATE_ATTRIBUTE, dict(context))


def get_request_context(request: Any) -> dict[str, Any]:
    context = getattr(request.state, REQUEST_CONTEXT_STATE_ATTRIBUTE, None)
    if context is None:
        return {}
    if isinstance(context, Mapping):
        return dict(context)

    raise ContextProviderError("Stored request template context must be a mapping.")


def clear_context_providers(module_name: str | None = None) -> None:
    if module_name is None:
        _CONTEXT_PROVIDERS.clear()
        return

    _CONTEXT_PROVIDERS.pop(module_name, None)


def wevra_web_theme_context(request: Any) -> dict[str, str]:
    from starlette.routing import NoMatchFound

    from wevra.web.theme import THEME_MODE_ROUTE_NAME, theme_template_context

    context = theme_template_context(request)
    try:
        theme_update_path = str(request.url_for(THEME_MODE_ROUTE_NAME))
    except NoMatchFound:
        return context

    return context | {
        "theme_update_path": theme_update_path,
        "theme_return_path": request.url.path,
    }


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


async def _call_provider(provider: ContextProvider, request: Any) -> ContextValue:
    provider_context = provider(request)
    if inspect.isawaitable(provider_context):
        provider_context = await provider_context
    if not isinstance(provider_context, Mapping):
        raise ContextProviderError(
            diagnostic_message(
                f"Context provider {_provider_name(provider)}",
                "must return a mapping.",
            )
        )

    context: dict[str, Any] = {}
    for key, value in provider_context.items():
        if not isinstance(key, str):
            raise ContextProviderError(
                diagnostic_message(
                    f"Context provider {_provider_name(provider)}",
                    "returned a non-string template context key.",
                )
            )
        context[key] = value

    return context


def _merge_provider_context(
    context: dict[str, Any],
    provider_context: ContextValue,
    reserved_keys: Set[str],
    provider: ContextProvider,
) -> None:
    provider_keys = set(provider_context)
    reserved_collisions = provider_keys & set(reserved_keys)
    if reserved_collisions:
        keys = ", ".join(sorted(reserved_collisions))
        raise ContextProviderError(
            diagnostic_message(
                f"Context provider {_provider_name(provider)}",
                f"overrides reserved template context keys: {keys}",
            )
        )

    provider_collisions = provider_keys & context.keys()
    if provider_collisions:
        keys = ", ".join(sorted(provider_collisions))
        raise ContextProviderError(
            diagnostic_message(
                f"Context provider {_provider_name(provider)}",
                f"collides with existing template context keys: {keys}",
            )
        )

    context.update(dict(provider_context))


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
    "ContextValue",
    "REQUEST_CONTEXT_STATE_ATTRIBUTE",
    "add_to_context",
    "clear_context_providers",
    "get_context_providers",
    "get_request_context",
    "resolve_context_providers",
    "set_request_context",
    "validate_context_providers",
    "wevra_web_theme_context",
]


add_to_context(wevra_web_theme_context)
