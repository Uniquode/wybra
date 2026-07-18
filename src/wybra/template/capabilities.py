from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from fastapi import Request
from fastapi.responses import HTMLResponse
from jinja2 import Environment, select_autoescape

from wybra.assets import StaticAssetCapability, require_static_asset_capability
from wybra.core.resources import PackageResourceSource
from wybra.diagnostics import template_render_diagnostics
from wybra.site import SiteCapabilityProxy
from wybra.template.cache import (
    CacheExtension,
    CacheKeyNormaliser,
    CacheKeyNormalisers,
    CacheProvider,
    configure_cache_extension,
)
from wybra.template.context import TemplateContext, get_request_context
from wybra.template.templating import build_template_loader


@runtime_checkable
class TemplateCapability(Protocol):
    async def render_template(
        self, template_name: str, context: dict[str, Any]
    ) -> str: ...

    async def render_page(
        self,
        request: Request,
        template_name: str,
        context: dict[str, Any],
        *,
        status_code: int = 200,
    ) -> HTMLResponse: ...

    async def render_partial(
        self,
        request: Request,
        template_name: str,
        context: dict[str, Any],
        *,
        status_code: int = 200,
    ) -> HTMLResponse: ...


@dataclass(slots=True)
class DefaultTemplateCapability:
    template_sources: tuple[PackageResourceSource, ...] = ()
    template_root: Path | None = None
    assets: SiteCapabilityProxy[StaticAssetCapability] | None = None
    cache_provider: CacheProvider | None = None
    cache_key_normalisers: CacheKeyNormalisers | None = None
    include_request_context: bool = True
    auto_reload: bool | None = None
    cache_size: int = 400
    environment: Environment = field(init=False)
    _asset_url: Callable[[str], str] = field(init=False)

    def __post_init__(self) -> None:
        loader = build_template_loader(
            template_root=self.template_root,
            template_sources=self.template_sources,
        )
        environment_options: dict[str, Any] = {}
        if self.auto_reload is not None:
            environment_options["auto_reload"] = self.auto_reload
        self.environment = Environment(
            loader=loader,
            autoescape=select_autoescape(("html", "xml")),
            cache_size=self.cache_size,
            enable_async=True,
            extensions=(CacheExtension,),
            **environment_options,
        )
        configure_cache_extension(
            self.environment,
            self.cache_provider,
            cache_key_normalisers=self.cache_key_normalisers,
        )
        self._asset_url = self._resolve_asset_url()

    def register_cache_key_normaliser(
        self,
        value_type: type[object],
        normaliser: CacheKeyNormaliser,
    ) -> None:
        """Register a canonical cache-key representation for an application type."""
        normalisers = dict(self.cache_key_normalisers or {})
        normalisers[value_type] = normaliser
        configure_cache_extension(
            self.environment,
            self.cache_provider,
            cache_key_normalisers=normalisers,
        )
        self.cache_key_normalisers = normalisers

    async def render_template(self, template_name: str, context: dict[str, Any]) -> str:
        with template_render_diagnostics(template_name):
            return await self.environment.get_template(template_name).render_async(
                context
            )

    async def render_page(
        self,
        request: Request,
        template_name: str,
        context: dict[str, Any],
        *,
        status_code: int = 200,
    ) -> HTMLResponse:
        return await self._render_response(
            request,
            template_name,
            context,
            status_code=status_code,
        )

    async def render_partial(
        self,
        request: Request,
        template_name: str,
        context: dict[str, Any],
        *,
        status_code: int = 200,
    ) -> HTMLResponse:
        return await self._render_response(
            request,
            template_name,
            context,
            status_code=status_code,
        )

    async def _render_response(
        self,
        request: Request,
        template_name: str,
        context: dict[str, Any],
        *,
        status_code: int,
    ) -> HTMLResponse:
        return HTMLResponse(
            await self.render_template(
                template_name,
                self._template_context(request, context),
            ),
            status_code=status_code,
        )

    def _template_context(
        self,
        request: Request,
        context: dict[str, Any],
    ) -> dict[str, Any]:
        return (
            TemplateContext.from_mapping(get_request_context(request))
            .with_layer(context)
            .with_layer(self._protected_context(request))
            .as_dict()
        )

    def _protected_context(self, request: Request) -> dict[str, Any]:
        protected_context: dict[str, Any] = {
            "route_name": _resolve_route_name(request),
            "asset_url": self._asset_url,
        }
        if self.include_request_context:
            protected_context["request"] = request
        return protected_context

    def _resolve_asset_url(self) -> Callable[[str], str]:
        capability: StaticAssetCapability | None = None

        def asset_url(logical_path: str) -> str:
            nonlocal capability
            if capability is None:
                if self.assets is None:
                    raise RuntimeError(
                        "Static asset capability proxy is not configured."
                    )
                capability = require_static_asset_capability(self.assets)
            return capability.url(logical_path)

        return asset_url


def _resolve_route_name(request: Request) -> str:
    route = request.scope.get("route")
    route_name = getattr(route, "name", None)
    if isinstance(route_name, str):
        return route_name
    return "unknown"


__all__ = (
    "DefaultTemplateCapability",
    "TemplateCapability",
)
