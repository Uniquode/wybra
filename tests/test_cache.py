from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI

from wybra.cache import CacheCapability, CacheSettings, InMemoryCache, RedisCache
from wybra.config import MappingConfigSource
from wybra.core.exceptions import ConfigurationError
from wybra.site import start
from wybra.template import DefaultTemplateCapability, TemplateCapability


class TestCacheSettings:
    def test_defaults_to_memory_backend(self) -> None:
        settings = CacheSettings.load_settings({"cache": {}})

        assert settings.backend == "memory"
        assert settings.url is None


class TestInMemoryCache:
    @pytest.mark.anyio
    async def test_expires_entries_after_ttl(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        now = 100.0
        monkeypatch.setattr("wybra.cache.capabilities.time.monotonic", lambda: now)
        cache = InMemoryCache()

        await cache.set("template", "fragment", b"content", ttl=30)

        now = 131.0
        assert await cache.get("template", "fragment") is None

    @pytest.mark.anyio
    async def test_owner_prefixes_entries_and_supports_operations(self) -> None:
        cache = InMemoryCache()

        await cache.set("template", "fragment", b"content", ttl=60)

        assert await cache.get("template", "fragment") == b"content"
        assert await cache.get("other", "fragment") is None
        await cache.delete("template", "fragment")
        assert await cache.get("template", "fragment") is None

    @pytest.mark.anyio
    async def test_rejects_colons_in_owner_names(self) -> None:
        cache = InMemoryCache()

        with pytest.raises(ValueError, match="must not contain ':'"):
            await cache.set("template:fragment", "content", b"value", ttl=60)

    @pytest.mark.anyio
    async def test_get_or_set_uses_factory_only_for_missing_entry(self) -> None:
        cache = InMemoryCache()
        calls = 0

        async def factory() -> bytes:
            nonlocal calls
            calls += 1
            return b"value"

        assert (
            await cache.get_or_set("template", "fragment", ttl=60, factory=factory)
            == b"value"
        )
        assert (
            await cache.get_or_set("template", "fragment", ttl=60, factory=factory)
            == b"value"
        )
        assert calls == 1

    @pytest.mark.anyio
    async def test_get_or_set_allows_only_one_concurrent_factory(self) -> None:
        cache = InMemoryCache()
        started = asyncio.Event()
        release = asyncio.Event()
        calls = 0

        async def factory() -> bytes:
            nonlocal calls
            calls += 1
            started.set()
            await release.wait()
            return b"value"

        async def unexpected_factory() -> bytes:
            pytest.fail("A waiting cache caller must not run its factory.")

        first = asyncio.create_task(
            cache.get_or_set("template", "fragment", ttl=60, factory=factory)
        )
        await started.wait()
        second = asyncio.create_task(
            cache.get_or_set(
                "template",
                "fragment",
                ttl=60,
                factory=unexpected_factory,
            )
        )
        await asyncio.sleep(0)
        release.set()

        assert await first == b"value"
        assert await second == b"value"
        assert calls == 1

    @pytest.mark.anyio
    async def test_get_or_set_releases_waiters_after_a_failed_factory(self) -> None:
        cache = InMemoryCache()

        async def failing_factory() -> bytes:
            raise RuntimeError("source unavailable")

        async def succeeding_factory() -> bytes:
            return b"recovered"

        with pytest.raises(RuntimeError, match="source unavailable"):
            await cache.get_or_set(
                "template",
                "fragment",
                ttl=60,
                factory=failing_factory,
            )

        assert (
            await cache.get_or_set(
                "template",
                "fragment",
                ttl=60,
                factory=succeeding_factory,
            )
            == b"recovered"
        )

    @pytest.mark.anyio
    async def test_get_or_set_times_out_a_stalled_factory(self) -> None:
        cache = InMemoryCache()
        release = asyncio.Event()

        async def factory() -> bytes:
            await release.wait()
            return b"value"

        with pytest.raises(TimeoutError):
            await cache.get_or_set(
                "template",
                "fragment",
                ttl=60,
                factory=factory,
                timeout=0.01,
            )

        assert await cache.get("template", "fragment") is None


class TestRedisCache:
    def test_requires_the_optional_cache_dependency(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def missing_redis(_: str) -> None:
            raise ImportError("redis is not installed")

        monkeypatch.setattr(
            "wybra.cache.capabilities.importlib.import_module", missing_redis
        )

        with pytest.raises(ConfigurationError, match=r"Install wybra\[cache\]"):
            RedisCache("redis://cache")

    @pytest.mark.anyio
    async def test_uses_binary_redis_values(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class FakeRedis:
            def __init__(self) -> None:
                self.values: dict[str, bytes] = {}

            async def get(self, key: str) -> bytes | None:
                return self.values.get(key)

            async def set(self, key: str, value: bytes, *, px: int) -> None:
                assert px == 60_000
                self.values[key] = value

            async def delete(self, key: str) -> None:
                self.values.pop(key, None)

            async def aclose(self) -> None:
                return None

        client = FakeRedis()
        monkeypatch.setattr(
            "wybra.cache.capabilities.importlib.import_module",
            lambda _: SimpleNamespace(
                Redis=SimpleNamespace(from_url=lambda *_args, **_kwargs: client)
            ),
        )
        cache = RedisCache("redis://cache")

        await cache.set("template", "bytecode", b"compiled", ttl=60)

        assert await cache.get("template", "bytecode") == b"compiled"
        await cache.delete("template", "bytecode")
        assert await cache.get("template", "bytecode") is None


class TestCacheModule:
    @pytest.mark.anyio
    async def test_module_registers_cache_capability(self) -> None:
        site = await start(
            FastAPI(),
            config_source=MappingConfigSource(
                {"app": {"modules": ("wybra.cache",)}, "cache": {}}
            ),
        )

        assert isinstance(site.require_capability(CacheCapability), CacheCapability)
        await site.close()


class TestTemplateFragmentCache:
    @pytest.mark.anyio
    async def test_template_module_resolves_cache_capability_at_render_time(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / "fragment.html").write_text(
            '{% cache "greeting" ttl=60 %}{{ value }}{% endcache %}',
            encoding="utf-8",
        )
        site = await start(
            FastAPI(),
            config_source=MappingConfigSource(
                {
                    "app": {"modules": ("wybra.template", "wybra.cache")},
                    "app.templates": {"root": str(tmp_path)},
                    "cache": {},
                }
            ),
        )
        templates = site.require_capability(TemplateCapability)

        assert await templates.render_template("fragment.html", {"value": "first"}) == (
            "first"
        )
        assert await templates.render_template(
            "fragment.html", {"value": "second"}
        ) == ("first")
        await site.close()

    @pytest.mark.anyio
    async def test_caches_fragments_when_a_cache_provider_is_available(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / "fragment.html").write_text(
            '{% cache "greeting" ttl=60 vary_by=locale %}{{ value }}{% endcache %}',
            encoding="utf-8",
        )
        cache = InMemoryCache()
        templates = DefaultTemplateCapability(
            template_root=tmp_path,
            cache_provider=lambda: cache,
        )

        assert (
            await templates.render_template(
                "fragment.html", {"locale": "en-AU", "value": "first"}
            )
            == "first"
        )
        assert (
            await templates.render_template(
                "fragment.html", {"locale": "en-AU", "value": "second"}
            )
            == "first"
        )
        assert (
            await templates.render_template(
                "fragment.html", {"locale": "fr", "value": "troisième"}
            )
            == "troisième"
        )

    @pytest.mark.anyio
    async def test_isolates_user_scoped_fragments(self, tmp_path: Path) -> None:
        (tmp_path / "fragment.html").write_text(
            '{% cache "greeting" ttl=60 vary_by=request.user.id %}'
            "{{ request.user.name }}{% endcache %}",
            encoding="utf-8",
        )
        cache = InMemoryCache()
        templates = DefaultTemplateCapability(
            template_root=tmp_path,
            cache_provider=lambda: cache,
        )
        first_request = SimpleNamespace(user=SimpleNamespace(id=1, name="Ada"))
        second_request = SimpleNamespace(user=SimpleNamespace(id=2, name="Grace"))

        assert (
            await templates.render_template("fragment.html", {"request": first_request})
            == "Ada"
        )
        assert (
            await templates.render_template(
                "fragment.html", {"request": second_request}
            )
            == "Grace"
        )

    @pytest.mark.anyio
    async def test_fragment_keys_are_isolated_by_template_fingerprint(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / "first.html").write_text(
            '{% cache "summary" ttl=60 %}First {{ value }}{% endcache %}',
            encoding="utf-8",
        )
        (tmp_path / "second.html").write_text(
            '{% cache "summary" ttl=60 %}Second {{ value }}{% endcache %}',
            encoding="utf-8",
        )
        cache = InMemoryCache()
        templates = DefaultTemplateCapability(
            template_root=tmp_path,
            cache_provider=lambda: cache,
        )

        assert await templates.render_template("first.html", {"value": "one"}) == (
            "First one"
        )
        assert await templates.render_template("second.html", {"value": "two"}) == (
            "Second two"
        )

    @pytest.mark.anyio
    async def test_cache_hits_do_not_reload_template_source(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / "fragment.html").write_text(
            '{% cache "greeting" ttl=60 %}{{ value }}{% endcache %}',
            encoding="utf-8",
        )
        cache = InMemoryCache()
        templates = DefaultTemplateCapability(
            template_root=tmp_path,
            cache_provider=lambda: cache,
        )
        loader = templates.environment.loader
        assert loader is not None
        calls = 0
        get_source = loader.get_source

        def counted_get_source(*args: object, **kwargs: object) -> object:
            nonlocal calls
            calls += 1
            return get_source(*args, **kwargs)

        monkeypatch.setattr(loader, "get_source", counted_get_source)

        assert await templates.render_template("fragment.html", {"value": "first"}) == (
            "first"
        )
        calls_after_first_render = calls
        assert await templates.render_template(
            "fragment.html", {"value": "second"}
        ) == ("first")
        assert calls == calls_after_first_render

    @pytest.mark.anyio
    async def test_cache_tag_renders_normally_without_a_cache_provider(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / "fragment.html").write_text(
            '{% cache "greeting" ttl=60 %}{{ value }}{% endcache %}',
            encoding="utf-8",
        )
        templates = DefaultTemplateCapability(template_root=tmp_path)

        assert (
            await templates.render_template("fragment.html", {"value": "first"})
            == "first"
        )
        assert (
            await templates.render_template("fragment.html", {"value": "second"})
            == "second"
        )

    @pytest.mark.anyio
    async def test_template_module_renders_cache_tag_without_cache_module(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / "fragment.html").write_text(
            '{% cache "greeting" ttl=60 %}{{ value }}{% endcache %}',
            encoding="utf-8",
        )
        site = await start(
            FastAPI(),
            config_source=MappingConfigSource(
                {
                    "app": {"modules": ("wybra.template",)},
                    "app.templates": {"root": str(tmp_path)},
                }
            ),
        )
        templates = site.require_capability(TemplateCapability)

        assert await templates.render_template("fragment.html", {"value": "first"}) == (
            "first"
        )
        assert await templates.render_template(
            "fragment.html", {"value": "second"}
        ) == ("second")
        await site.close()
