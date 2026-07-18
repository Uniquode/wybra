"""Jinja support for cacheable rendered fragments."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable
from typing import Any
from weakref import WeakKeyDictionary

from jinja2 import Environment, nodes
from jinja2.environment import Template
from jinja2.exceptions import TemplateNotFound, TemplateRuntimeError
from jinja2.ext import Extension
from jinja2.runtime import Context

from wybra.cache import CacheCapability

FRAGMENT_CACHE_OWNER = "template.fragment"
type CacheProvider = Callable[[], CacheCapability | None]
type FragmentCaller = Callable[[], Awaitable[str]]


class CacheExtension(Extension):
    """Render a block through the optional Wybra cache capability."""

    tags = {"cache"}

    def __init__(self, environment: Environment) -> None:
        super().__init__(environment)
        self.cache_provider: CacheProvider | None = None
        self._template_fingerprints: WeakKeyDictionary[Template, str] = (
            WeakKeyDictionary()
        )

    def parse(self, parser: Any) -> nodes.Node:
        lineno = next(parser.stream).lineno
        fragment_name = parser.parse_expression()
        parser.stream.expect("name:ttl")
        parser.stream.expect("assign")
        ttl = parser.parse_expression()
        vary_by: nodes.Expr = nodes.Tuple([], "load")
        if parser.stream.skip_if("name:vary_by"):
            parser.stream.expect("assign")
            vary_by = parser.parse_expression()
        body = parser.parse_statements(("name:endcache",), drop_needle=True)
        return nodes.CallBlock(
            self.call_method(
                "_render_fragment",
                [nodes.ContextReference(), fragment_name, ttl, vary_by],
            ),
            [],
            [],
            body,
        ).set_lineno(lineno)

    async def _render_fragment(
        self,
        context: Context,
        fragment_name: object,
        ttl: object,
        vary_by: object,
        caller: FragmentCaller,
    ) -> str:
        if not isinstance(fragment_name, str) or not fragment_name:
            raise TemplateRuntimeError(
                "Cache fragment names must be non-empty strings."
            )
        if isinstance(ttl, bool) or not isinstance(ttl, int | float) or ttl <= 0:
            raise TemplateRuntimeError("Cache fragment TTL must be a positive number.")

        provider = self.cache_provider
        cache = provider() if provider is not None else None
        if cache is None:
            return await caller()

        key = self._fragment_key(context, fragment_name, vary_by)

        async def render_fragment() -> bytes:
            return (await caller()).encode("utf-8")

        return (
            await cache.get_or_set(
                FRAGMENT_CACHE_OWNER,
                key,
                ttl=float(ttl),
                factory=render_fragment,
            )
        ).decode("utf-8")

    def _fragment_key(
        self,
        context: Context,
        fragment_name: str,
        vary_by: object,
    ) -> str:
        variation = json.dumps(
            vary_by,
            default=_canonical_value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        variation_fingerprint = hashlib.sha256(variation.encode("utf-8")).hexdigest()
        return ":".join(
            (
                self._template_fingerprint(context.environment, context.name),
                fragment_name,
                variation_fingerprint,
            )
        )

    def _template_fingerprint(
        self,
        environment: Environment,
        template_name: str | None,
    ) -> str:
        if template_name is None:
            return _hash_template_source("<inline>", "<inline>")

        template = environment.get_template(template_name)
        fingerprint = self._template_fingerprints.get(template)
        if fingerprint is not None:
            return fingerprint

        source = template_name
        if environment.loader is not None:
            try:
                source, _, _ = environment.loader.get_source(environment, template_name)
            except TemplateNotFound:
                pass
        fingerprint = _hash_template_source(template_name, source)
        self._template_fingerprints[template] = fingerprint
        return fingerprint


def configure_cache_extension(
    environment: Environment,
    cache_provider: CacheProvider | None,
) -> None:
    extension = environment.extensions[CacheExtension.identifier]
    if not isinstance(extension, CacheExtension):
        raise RuntimeError("Jinja cache extension is not registered.")
    extension.cache_provider = cache_provider


def _hash_template_source(identity: str, source: str) -> str:
    return hashlib.sha256(f"{identity}\0{source}".encode()).hexdigest()


def _canonical_value(value: object) -> str:
    return str(value)


__all__ = (
    "CacheExtension",
    "CacheProvider",
    "FRAGMENT_CACHE_OWNER",
    "configure_cache_extension",
)
