from __future__ import annotations

import json
from pathlib import Path
from typing import Protocol

import pytest
from fastapi import FastAPI

from wevra import Site, SiteCapabilityError, get_site, start_site
from wevra.config import (
    ConfigService,
    ConfigSourceError,
    ConfigSourceResult,
    MappingConfigSource,
)
from wevra.core.composition import (
    AppConfig,
    CompositionError,
    RouteOptions,
    StaticOptions,
    TemplateOptions,
)
from wevra.site import start
from wevra.site_config import app_config_from_site


class ExampleCapability:
    pass


class OtherCapability:
    pass


class UnsupportedCapability(Protocol):
    pass


class ExampleImplementation(ExampleCapability):
    pass


class ClosingCapability:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class SyncClosingCapability:
    def close(self) -> None:
        pass


def _write_app_config(path: Path, *, modules: tuple[str, ...]) -> Path:
    path.write_text(
        f"""
        [app]
        modules = {json.dumps(list(modules))}
        database_url = "sqlite+aiosqlite:///app.sqlite3"

        [app.routes]

        [app.templates]
        auto_reload = true
        cache_size = 0

        [app.static]
        url_path = "/static/"
        export_root = "static"
        """,
        encoding="utf-8",
    )
    return path


def _site_from_mapping(values: dict[str, dict[str, object]]) -> Site:
    return Site(
        app=FastAPI(),
        config=ConfigService(
            [MappingConfigSource(values)],
            discover_module_config=False,
        ),
    )


def test_app_config_from_site_rejects_non_mapping_route_prefixes() -> None:
    site = _site_from_mapping(
        {
            "app": {"modules": ("host_app",)},
            "app.routes": {"prefixes": []},
        }
    )

    with pytest.raises(
        CompositionError,
        match=r"'app\.routes' prefixes must be a mapping",
    ):
        app_config_from_site(site)


def test_app_config_from_site_rejects_non_mapping_module_route_prefixes() -> None:
    site = _site_from_mapping(
        {
            "app": {"modules": ("host_app",)},
            "app.routes": {"prefixes": {"host_app": ""}},
        }
    )

    with pytest.raises(
        CompositionError,
        match=r"prefixes for 'host_app' must be a mapping",
    ):
        app_config_from_site(site)


def test_app_config_from_site_rejects_non_string_route_prefix() -> None:
    site = _site_from_mapping(
        {
            "app": {"modules": ("host_app",)},
            "app.routes": {"prefixes": {"host_app": {"admin": 123}}},
        }
    )

    with pytest.raises(
        CompositionError,
        match=r"prefix for 'host_app' router 'admin' must be a string",
    ):
        app_config_from_site(site)


def test_app_config_from_site_rejects_malformed_scalar_options() -> None:
    site = _site_from_mapping(
        {
            "app": {"modules": ("host_app",)},
            "app.templates": {"cache_size": -1},
        }
    )

    with pytest.raises(
        CompositionError,
        match=r"cache_size' must be a non-negative integer",
    ):
        app_config_from_site(site)


@pytest.mark.anyio
async def test_start_composes_existing_fastapi_app_from_file_source(
    tmp_path: Path,
) -> None:
    app = FastAPI(title="Host app")
    config_path = _write_app_config(tmp_path / "app.toml", modules=("wevra.web",))

    site = await start(app, config_source=str(config_path))

    assert isinstance(site, Site)
    assert site.app is app
    assert site.modules == ("wevra.web",)
    assert site.has_module("wevra.web") is True
    assert site.has_module("wevra.auth") is False


@pytest.mark.anyio
async def test_start_accepts_relative_file_source_string(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_app_config(tmp_path / "app.toml", modules=("wevra.web",))
    monkeypatch.chdir(tmp_path)

    site = await start(FastAPI(), config_source="app.toml")

    assert site.modules == ("wevra.web",)


@pytest.mark.anyio
async def test_start_accepts_file_uri_source_string(tmp_path: Path) -> None:
    config_path = _write_app_config(
        tmp_path / "app.toml",
        modules=("wevra.web",),
    )

    site = await start(FastAPI(), config_source=config_path.as_uri())

    assert site.modules == ("wevra.web",)


@pytest.mark.anyio
async def test_start_rejects_blank_config_source_string() -> None:
    with pytest.raises(ConfigSourceError, match="must not be blank"):
        await start(FastAPI(), config_source="   ")


@pytest.mark.anyio
async def test_start_rejects_unsupported_config_source_uri_scheme() -> None:
    with pytest.raises(ConfigSourceError, match="Unsupported config source URI scheme"):
        await start(FastAPI(), config_source="https://example.test/app.toml")


@pytest.mark.anyio
async def test_start_treats_windows_absolute_source_string_as_file_path() -> None:
    with pytest.raises(ConfigSourceError, match="file: App config file"):
        await start(FastAPI(), config_source=r"C:\config\app.toml")


@pytest.mark.anyio
async def test_start_rejects_invalid_config_source_object() -> None:
    with pytest.raises(ConfigSourceError, match="string, AppConfig, or ConfigSource"):
        await start(FastAPI(), config_source=object())  # type: ignore[arg-type]


@pytest.mark.anyio
async def test_start_rejects_config_source_object_with_invalid_metadata() -> None:
    class InvalidConfigSource:
        metadata = object()

        def load(self) -> ConfigSourceResult:
            return ConfigSourceResult()

    with pytest.raises(ConfigSourceError, match="string, AppConfig, or ConfigSource"):
        await start(
            FastAPI(),
            config_source=InvalidConfigSource(),  # type: ignore[arg-type]
        )


@pytest.mark.anyio
async def test_site_provides_and_requires_type_keyed_capability() -> None:
    site = await start(
        FastAPI(),
        config_source=MappingConfigSource({"app": {"modules": ()}}),
    )
    capability = ExampleImplementation()

    site.provide_capability(ExampleCapability, capability)

    assert site.has_capability(ExampleCapability) is True
    assert site.require_capability(ExampleCapability) is capability


@pytest.mark.anyio
async def test_site_reports_missing_required_capability() -> None:
    site = await start(
        FastAPI(),
        config_source=MappingConfigSource({"app": {"modules": ()}}),
    )

    with pytest.raises(SiteCapabilityError, match="Missing capability"):
        site.require_capability(ExampleCapability)


@pytest.mark.anyio
async def test_site_rejects_duplicate_capability_provider() -> None:
    site = await start(
        FastAPI(),
        config_source=MappingConfigSource({"app": {"modules": ()}}),
    )
    site.provide_capability(ExampleCapability, ExampleImplementation())

    with pytest.raises(SiteCapabilityError, match="already provided"):
        site.provide_capability(ExampleCapability, ExampleImplementation())


@pytest.mark.anyio
async def test_site_rejects_capability_value_with_wrong_runtime_type() -> None:
    site = await start(
        FastAPI(),
        config_source=MappingConfigSource({"app": {"modules": ()}}),
    )

    with pytest.raises(SiteCapabilityError, match="invalid type"):
        site.provide_capability(ExampleCapability, OtherCapability())


@pytest.mark.anyio
async def test_site_rejects_capability_type_without_runtime_validation() -> None:
    site = await start(
        FastAPI(),
        config_source=MappingConfigSource({"app": {"modules": ()}}),
    )

    with pytest.raises(SiteCapabilityError, match="cannot be runtime-validated"):
        site.provide_capability(UnsupportedCapability, object())


@pytest.mark.anyio
async def test_site_close_closes_async_capabilities() -> None:
    site = await start(
        FastAPI(),
        config_source=MappingConfigSource({"app": {"modules": ()}}),
    )
    capability = ClosingCapability()
    site.provide_capability(ClosingCapability, capability)

    await site.close()

    assert capability.closed is True
    assert site.has_capability(ClosingCapability) is False

    await site.close()


@pytest.mark.anyio
async def test_site_close_reports_invalid_hooks_after_closing_valid_capabilities() -> (
    None
):
    site = await start(
        FastAPI(),
        config_source=MappingConfigSource({"app": {"modules": ()}}),
    )
    closing = ClosingCapability()
    site.provide_capability(ClosingCapability, closing)
    site.provide_capability(SyncClosingCapability, SyncClosingCapability())

    with pytest.raises(SiteCapabilityError, match="error_count=1"):
        await site.close()

    assert closing.closed is True
    assert site.has_capability(ClosingCapability) is False
    assert site.has_capability(SyncClosingCapability) is False


@pytest.mark.anyio
async def test_start_site_returns_fastapi_lifespan_and_stores_site() -> None:
    app = FastAPI()
    lifespan = start_site(
        config_source=MappingConfigSource({"app": {"modules": ()}}),
    )

    async with lifespan(app):
        assert isinstance(app.state.site, Site)
        assert app.state.site.app is app
        assert get_site(app) is app.state.site


@pytest.mark.anyio
async def test_get_site_rejects_missing_site() -> None:
    with pytest.raises(SiteCapabilityError, match="attribute=site"):
        get_site(FastAPI())


@pytest.mark.anyio
async def test_start_accepts_loaded_app_config(tmp_path: Path) -> None:
    app_config = AppConfig(
        config_path=tmp_path / "app.toml",
        project_root=tmp_path,
        modules=("wevra.web",),
        routes=RouteOptions(prefixes={}),
        templates=TemplateOptions(auto_reload=True, cache_size=0),
        static=StaticOptions(
            url_path="/static/", root=None, export_root=Path("static")
        ),
    )

    site = await start(FastAPI(), config_source=app_config)

    assert site.modules == ("wevra.web",)
    assert site.has_module("wevra.web") is True


@pytest.mark.anyio
async def test_start_accepts_config_source_object() -> None:
    site = await start(
        FastAPI(),
        config_source=MappingConfigSource(
            {"app": {"modules": ("wevra.web",)}},
            source="test",
        ),
    )

    assert site.modules == ("wevra.web",)


@pytest.mark.anyio
async def test_start_reports_missing_required_config_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigSourceError, match="file: App config file"):
        await start(FastAPI(), config_source=str(tmp_path / "missing.toml"))


def _write_module(root: Path, name: str, body: str) -> None:
    module_path = root / f"{name}.py"
    module_path.write_text(body, encoding="utf-8")


@pytest.mark.anyio
async def test_start_invokes_setup_site_hooks_in_configured_module_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.syspath_prepend(str(tmp_path))
    _write_module(tmp_path, "site_setup_recorder", "calls = []\n")
    _write_module(
        tmp_path,
        "first_module",
        "from site_setup_recorder import calls\n"
        'async def setup_site(site):\n    calls.append("first")\n',
    )
    _write_module(
        tmp_path,
        "second_module",
        "from site_setup_recorder import calls\n"
        'async def setup_site(site):\n    calls.append("second")\n',
    )

    site = await start(
        FastAPI(),
        config_source=MappingConfigSource(
            {"app": {"modules": ("first_module", "second_module")}}
        ),
    )

    from site_setup_recorder import calls

    assert isinstance(site, Site)
    assert calls == ["first", "second"]


@pytest.mark.anyio
async def test_start_ignores_modules_without_setup_site(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.syspath_prepend(str(tmp_path))
    _write_module(tmp_path, "plain_module", "")

    site = await start(
        FastAPI(),
        config_source=MappingConfigSource({"app": {"modules": ("plain_module",)}}),
    )

    assert site.modules == ("plain_module",)


@pytest.mark.anyio
async def test_start_rejects_non_callable_setup_site(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.syspath_prepend(str(tmp_path))
    _write_module(tmp_path, "invalid_module", "setup_site = object()\n")

    with pytest.raises(
        SiteCapabilityError,
        match="module=invalid_module.*attribute_type=object",
    ):
        await start(
            FastAPI(),
            config_source=MappingConfigSource(
                {"app": {"modules": ("invalid_module",)}}
            ),
        )


@pytest.mark.anyio
async def test_start_rejects_sync_setup_site(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.syspath_prepend(str(tmp_path))
    _write_module(
        tmp_path,
        "sync_module",
        'def setup_site(site):\n    raise RuntimeError("should not be called")\n',
    )

    with pytest.raises(
        SiteCapabilityError,
        match="module=sync_module.*expected=async_callable",
    ):
        await start(
            FastAPI(),
            config_source=MappingConfigSource({"app": {"modules": ("sync_module",)}}),
        )


@pytest.mark.anyio
async def test_start_reports_setup_site_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.syspath_prepend(str(tmp_path))
    _write_module(
        tmp_path,
        "failing_module",
        'async def setup_site(site):\n    raise RuntimeError("boom")\n',
    )

    with pytest.raises(
        SiteCapabilityError,
        match="module=failing_module.*error_type=RuntimeError",
    ):
        await start(
            FastAPI(),
            config_source=MappingConfigSource(
                {"app": {"modules": ("failing_module",)}}
            ),
        )
