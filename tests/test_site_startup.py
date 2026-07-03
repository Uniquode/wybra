from __future__ import annotations

import json
from pathlib import Path
from typing import Protocol

import pytest
from fastapi import FastAPI
from starlette.exceptions import HTTPException as StarletteHTTPException

from wybra import Site, SiteCapabilityError, SiteCapabilityProxy, get_site, start_site
from wybra.config import (
    ConfigService,
    ConfigSourceError,
    ConfigSourceResult,
    MappingConfigSource,
)
from wybra.core.composition import (
    APP_CONFIG_ENV,
    APP_ROOT_ENV,
    AppConfig,
    AssetOptions,
    CompositionError,
    RouteOptions,
    TemplateOptions,
)
from wybra.core.config import ENV_APP_ENV
from wybra.core.exceptions import ConfigurationError, Http404
from wybra.core.routes import inspect_route_tree
from wybra.db.config import ENV_DATABASE_URL
from wybra.errors.capabilities import ErrorHandlingCapability
from wybra.errors.handlers import EmptyBodyResponseException
from wybra.site import start
from wybra.site_config import app_config_from_site


class ExampleCapability:
    def label(self) -> str:
        return "example"


class OtherCapability:
    pass


class UnsupportedCapability(Protocol):
    pass


class ExampleImplementation(ExampleCapability):
    def __init__(self) -> None:
        self.calls = 0

    def label(self) -> str:
        self.calls += 1
        return "implementation"


class LateCapability:
    def label(self) -> str:
        return "late"


class ClosingCapability:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class SyncClosingCapability:
    def close(self) -> None:
        pass


def _write_app_config(
    path: Path,
    *,
    modules: tuple[str, ...],
    asset_root: str | None = None,
    deployment_environment: str | None = None,
) -> Path:
    asset_root_config = f'        root = "{asset_root}"\n' if asset_root else ""
    deployment_config = (
        f'        deployment_environment = "{deployment_environment}"\n'
        if deployment_environment is not None
        else ""
    )
    path.write_text(
        f"""
        [app]
        modules = {json.dumps(list(modules))}
        database_url = "sqlite+aiosqlite:///app.sqlite3"
{deployment_config.rstrip()}

        [app.routes]

        [app.templates]
        auto_reload = true
        cache_size = 0

        [app.assets]
        url_path = "/static/"
{asset_root_config.rstrip()}
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


@pytest.mark.anyio
async def test_start_registers_configured_routes_without_web_module(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package_root = tmp_path / "core_route_app"
    package_root.mkdir()
    (package_root / "__init__.py").write_text("", encoding="utf-8")
    (package_root / "routes.py").write_text(
        "from fastapi import APIRouter\n"
        "router = APIRouter()\n"
        "@router.get('/status', name='status')\n"
        "async def status():\n"
        "    return {'ok': True}\n"
        "module_routers = {'default': router}\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))

    app = FastAPI()
    await start(
        app,
        config_source=MappingConfigSource(
            {
                "app": {
                    "config_path": tmp_path / "app.toml",
                    "project_root": tmp_path,
                    "modules": ("core_route_app",),
                },
                "app.routes": {"prefixes": {"core_route_app": {"default": "/api"}}},
            }
        ),
    )

    paths = {route.path for route in inspect_route_tree(app).routes}

    assert "/api/status" in paths


@pytest.mark.anyio
async def test_start_requires_api_capability_for_routes_declaring_api_semantics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package_root = tmp_path / "api_route_app"
    package_root.mkdir()
    (package_root / "__init__.py").write_text("", encoding="utf-8")
    (package_root / "routes.py").write_text(
        "from fastapi import APIRouter\n"
        "from wybra.core.routes import RouteType, route\n"
        "router = APIRouter()\n"
        "@router.get('/status', name='status')\n"
        "async def status():\n"
        "    return {'ok': True}\n"
        "status = route('/status', RouteType.API)(status)\n"
        "module_routers = {'default': router}\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))

    with pytest.raises(SiteCapabilityError, match="ApiCapability"):
        await start(
            FastAPI(),
            config_source=MappingConfigSource(
                {
                    "app": {
                        "config_path": tmp_path / "app.toml",
                        "project_root": tmp_path,
                        "modules": ("api_route_app",),
                    },
                    "app.routes": {"prefixes": {"api_route_app": {"default": "/api"}}},
                }
            ),
        )


@pytest.mark.anyio
async def test_start_accepts_api_semantics_when_api_module_is_configured(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package_root = tmp_path / "configured_api_route_app"
    package_root.mkdir()
    (package_root / "__init__.py").write_text("", encoding="utf-8")
    (package_root / "routes.py").write_text(
        "from fastapi import APIRouter\n"
        "from wybra.core.routes import RouteType, route\n"
        "router = APIRouter()\n"
        "@router.get('/status', name='status')\n"
        "async def status():\n"
        "    return {'ok': True}\n"
        "status = route('/status', RouteType.API)(status)\n"
        "module_routers = {'default': router}\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))

    app = FastAPI()
    site = await start(
        app,
        config_source=MappingConfigSource(
            {
                "app": {
                    "config_path": tmp_path / "app.toml",
                    "project_root": tmp_path,
                    "modules": ("wybra.api", "configured_api_route_app"),
                },
                "app.routes": {
                    "prefixes": {"configured_api_route_app": {"default": "/api"}}
                },
            }
        ),
    )

    assert site.has_module("wybra.api")
    assert "/api/status" in {route.path for route in inspect_route_tree(app).routes}


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
    config_path = _write_app_config(
        tmp_path / "app.toml",
        modules=("wybra.assets",),
    )

    site = await start(app, config_source=str(config_path))

    assert isinstance(site, Site)
    assert site.app is app
    assert site.modules == ("wybra.assets",)
    assert site.has_module("wybra.assets") is True
    assert site.has_module("wybra.auth") is False


@pytest.mark.anyio
async def test_start_accepts_relative_file_source_string(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_app_config(
        tmp_path / "app.toml",
        modules=("wybra.assets",),
    )
    monkeypatch.chdir(tmp_path)

    site = await start(FastAPI(), config_source="app.toml")

    assert site.modules == ("wybra.assets",)


@pytest.mark.anyio
async def test_start_uses_app_root_environment_for_app_config(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    _write_app_config(
        project_root / "app.toml",
        modules=("wybra.assets",),
    )

    site = await start(
        FastAPI(),
        environ={
            APP_ROOT_ENV: project_root.as_posix(),
            APP_CONFIG_ENV: "app.toml",
        },
    )

    app_config = app_config_from_site(site)
    assert app_config.project_root == project_root.resolve()
    assert app_config.config_path == (project_root / "app.toml").resolve()


@pytest.mark.anyio
async def test_start_app_config_does_not_override_project_root(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "project"
    config_root = project_root / "config"
    config_root.mkdir(parents=True)
    _write_app_config(
        config_root / "app.toml",
        modules=("wybra.assets",),
    )

    site = await start(
        FastAPI(),
        environ={
            APP_ROOT_ENV: project_root.as_posix(),
            APP_CONFIG_ENV: "config/app.toml",
        },
    )

    app_config = app_config_from_site(site)
    assert app_config.project_root == project_root.resolve()
    assert app_config.config_path == (config_root / "app.toml").resolve()


@pytest.mark.anyio
async def test_start_relative_config_source_uses_supplied_app_root(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "project"
    config_root = project_root / "config"
    config_root.mkdir(parents=True)
    _write_app_config(
        config_root / "app.toml",
        modules=("wybra.assets",),
    )

    site = await start(
        FastAPI(),
        config_source="config/app.toml",
        environ={APP_ROOT_ENV: project_root.as_posix()},
    )

    app_config = app_config_from_site(site)
    assert app_config.project_root == project_root.resolve()
    assert app_config.config_path == (config_root / "app.toml").resolve()


@pytest.mark.anyio
async def test_start_environment_overrides_database_url_and_sets_deployment_fallback(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    _write_app_config(project_root / "app.toml", modules=("wybra.db",))

    site = await start(
        FastAPI(),
        environ={
            APP_ROOT_ENV: project_root.as_posix(),
            APP_CONFIG_ENV: "app.toml",
            ENV_DATABASE_URL: "sqlite+aiosqlite:///override.sqlite3",
            ENV_APP_ENV: "staging",
        },
    )

    try:
        app_config = app_config_from_site(site)
        assert app_config.database_url == "sqlite+aiosqlite:///override.sqlite3"
        assert site.deployment_environment == "staging"
        assert app_config.deployment_environment == "staging"
    finally:
        await site.close()


@pytest.mark.anyio
async def test_start_config_deployment_environment_precedes_app_env(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    _write_app_config(
        project_root / "app.toml",
        modules=("wybra.assets",),
        deployment_environment="production",
    )

    site = await start(
        FastAPI(),
        environ={
            APP_ROOT_ENV: project_root.as_posix(),
            APP_CONFIG_ENV: "app.toml",
            ENV_APP_ENV: "staging",
        },
    )

    try:
        app_config = app_config_from_site(site)
        assert site.deployment_environment == "production"
        assert app_config.deployment_environment == "production"
    finally:
        await site.close()


@pytest.mark.anyio
async def test_start_defaults_deployment_environment_to_local() -> None:
    site = await start(
        FastAPI(),
        config_source=MappingConfigSource({"app": {"modules": ("wybra.assets",)}}),
        environ={},
    )

    try:
        assert site.deployment_environment == "local"
    finally:
        await site.close()


@pytest.mark.anyio
async def test_start_rejects_invalid_effective_deployment_environment(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    _write_app_config(project_root / "app.toml", modules=("wybra.assets",))

    with pytest.raises(ConfigurationError, match="Deployment environment"):
        await start(
            FastAPI(),
            environ={
                APP_ROOT_ENV: project_root.as_posix(),
                APP_CONFIG_ENV: "app.toml",
                ENV_APP_ENV: "dev",
            },
        )


@pytest.mark.anyio
async def test_start_accepts_file_uri_source_string(tmp_path: Path) -> None:
    config_path = _write_app_config(
        tmp_path / "app.toml",
        modules=("wybra.assets",),
    )

    site = await start(FastAPI(), config_source=config_path.as_uri())

    assert site.modules == ("wybra.assets",)


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
async def test_site_creates_capability_proxy_before_provider_exists() -> None:
    site = await start(
        FastAPI(),
        config_source=MappingConfigSource({"app": {"modules": ()}}),
    )

    proxy = site.capability_proxy(ExampleCapability)

    assert isinstance(proxy, SiteCapabilityProxy)
    assert proxy.available() is False


@pytest.mark.anyio
async def test_site_capability_proxy_binds_on_first_required_use() -> None:
    site = await start(
        FastAPI(),
        config_source=MappingConfigSource({"app": {"modules": ()}}),
    )
    proxy = site.capability_proxy(ExampleCapability)
    capability = ExampleImplementation()
    site.provide_capability(ExampleCapability, capability)

    assert proxy.available() is True
    assert proxy.label() == "implementation"
    assert proxy.require() is capability
    assert proxy.label() == "implementation"
    assert capability.calls == 2


@pytest.mark.anyio
async def test_site_capability_proxy_is_immutable_after_binding() -> None:
    site = await start(
        FastAPI(),
        config_source=MappingConfigSource({"app": {"modules": ()}}),
    )
    capability = ExampleImplementation()
    proxy = site.capability_proxy(ExampleCapability)
    site.provide_capability(ExampleCapability, capability)

    assert proxy.label() == "implementation"

    del site._capabilities[ExampleCapability]

    assert proxy.label() == "implementation"
    assert proxy.require() is capability


@pytest.mark.anyio
async def test_site_capability_proxy_reports_missing_required_capability() -> None:
    site = await start(
        FastAPI(),
        config_source=MappingConfigSource({"app": {"modules": ()}}),
    )
    proxy = site.capability_proxy(ExampleCapability)

    with pytest.raises(SiteCapabilityError, match="Missing capability"):
        proxy.label()


@pytest.mark.anyio
async def test_site_capability_proxy_finalises_required_capability() -> None:
    site = await start(
        FastAPI(),
        config_source=MappingConfigSource({"app": {"modules": ()}}),
    )
    proxy = site.capability_proxy(ExampleCapability)
    capability = ExampleImplementation()
    site.provide_capability(ExampleCapability, capability)

    assert proxy.finalise_required() is capability
    assert proxy.require() is capability
    assert proxy.finalised is True
    assert proxy.unavailable is False


@pytest.mark.anyio
async def test_site_capability_proxy_reuses_required_finalisation() -> None:
    site = await start(
        FastAPI(),
        config_source=MappingConfigSource({"app": {"modules": ()}}),
    )
    proxy = site.capability_proxy(ExampleCapability)
    capability = ExampleImplementation()
    site.provide_capability(ExampleCapability, capability)

    assert proxy.finalise_required() is capability
    del site._capabilities[ExampleCapability]

    assert proxy.finalise_required() is capability
    assert proxy.finalise_optional() is capability
    assert proxy.finalised is True
    assert proxy.unavailable is False


@pytest.mark.anyio
async def test_site_capability_proxy_reports_missing_required_finalisation() -> None:
    site = await start(
        FastAPI(),
        config_source=MappingConfigSource({"app": {"modules": ()}}),
    )
    proxy = site.capability_proxy(ExampleCapability)

    with pytest.raises(SiteCapabilityError, match="Missing capability"):
        proxy.finalise_required()


@pytest.mark.anyio
async def test_site_capability_proxy_finalises_optional_capability() -> None:
    site = await start(
        FastAPI(),
        config_source=MappingConfigSource({"app": {"modules": ()}}),
    )
    proxy = site.capability_proxy(ExampleCapability)
    capability = ExampleImplementation()
    site.provide_capability(ExampleCapability, capability)

    assert proxy.finalise_optional() is capability
    assert proxy.optional() is capability
    assert proxy.finalised is True
    assert proxy.unavailable is False


@pytest.mark.anyio
async def test_site_capability_proxy_records_unavailable_optional_capability() -> None:
    site = await start(
        FastAPI(),
        config_source=MappingConfigSource({"app": {"modules": ()}}),
    )
    proxy = site.capability_proxy(ExampleCapability)

    assert proxy.finalise_optional() is None
    assert proxy.optional() is None
    assert proxy.finalised is True
    assert proxy.unavailable is True

    with pytest.raises(SiteCapabilityError, match="Missing capability"):
        proxy.label()


@pytest.mark.anyio
async def test_site_capability_proxy_reuses_unavailable_optional_finalisation() -> None:
    site = await start(
        FastAPI(),
        config_source=MappingConfigSource({"app": {"modules": ()}}),
    )
    proxy = site.capability_proxy(ExampleCapability)

    assert proxy.finalise_optional() is None
    site.provide_capability(ExampleCapability, ExampleImplementation())

    assert proxy.finalise_optional() is None
    with pytest.raises(SiteCapabilityError, match="Missing capability"):
        proxy.finalise_required()
    assert proxy.finalised is True
    assert proxy.unavailable is True


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
        modules=("wybra.assets",),
        routes=RouteOptions(prefixes={}),
        templates=TemplateOptions(auto_reload=True, cache_size=0),
        assets=AssetOptions(url_path="/static/"),
    )

    site = await start(FastAPI(), config_source=app_config)

    assert site.modules == ("wybra.assets",)
    assert site.has_module("wybra.assets") is True


@pytest.mark.anyio
async def test_start_accepts_config_source_object() -> None:
    site = await start(
        FastAPI(),
        config_source=MappingConfigSource(
            {"app": {"modules": ("wybra.assets",)}},
            source="test",
        ),
    )

    assert site.modules == ("wybra.assets",)


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
async def test_start_invokes_post_setup_site_hooks_after_all_setup_hooks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.syspath_prepend(str(tmp_path))
    _write_module(tmp_path, "post_setup_recorder", "calls = []\n")
    _write_module(
        tmp_path,
        "first_post_module",
        "from post_setup_recorder import calls\n"
        'async def setup_site(site):\n    calls.append("first.setup")\n'
        'async def post_setup_site(site):\n    calls.append("first.post")\n',
    )
    _write_module(
        tmp_path,
        "second_post_module",
        "from post_setup_recorder import calls\n"
        'async def setup_site(site):\n    calls.append("second.setup")\n'
        'async def post_setup_site(site):\n    calls.append("second.post")\n',
    )

    await start(
        FastAPI(),
        config_source=MappingConfigSource(
            {"app": {"modules": ("first_post_module", "second_post_module")}}
        ),
    )

    from post_setup_recorder import calls

    assert calls == [
        "first.setup",
        "second.setup",
        "first.post",
        "second.post",
    ]


@pytest.mark.anyio
async def test_errors_module_registers_error_handling_capability() -> None:
    app = FastAPI()

    site = await start(
        app,
        config_source=MappingConfigSource({"app": {"modules": ("wybra.errors",)}}),
    )

    assert site.has_capability(ErrorHandlingCapability)
    assert StarletteHTTPException in app.exception_handlers
    assert EmptyBodyResponseException in app.exception_handlers


@pytest.mark.anyio
async def test_errors_module_uses_configured_module_error_mappings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.syspath_prepend(str(tmp_path))
    _write_module(
        tmp_path,
        "mapped_error_app",
        "from jinja2.exceptions import TemplateNotFound\n"
        "from wybra.core.exceptions import Http404\n"
        "from wybra.errors.mappings import ErrorMapping\n"
        "error_mappings = (\n"
        "    ErrorMapping(TemplateNotFound, Http404, 'Missing page.'),\n"
        ")\n",
    )
    app = FastAPI()

    await start(
        app,
        config_source=MappingConfigSource(
            {"app": {"modules": ("mapped_error_app", "wybra.errors")}}
        ),
    )

    from jinja2.exceptions import TemplateNotFound

    @app.get("/missing")
    async def missing() -> None:
        raise TemplateNotFound("missing.html")

    from fastapi.testclient import TestClient

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/missing")

    assert response.status_code == Http404.default_status_code


@pytest.mark.anyio
async def test_start_ignores_modules_without_post_setup_site(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.syspath_prepend(str(tmp_path))
    _write_module(tmp_path, "post_setup_missing_recorder", "calls = []\n")
    _write_module(
        tmp_path,
        "plain_module",
        "from post_setup_missing_recorder import calls\n"
        'async def setup_site(site):\n    calls.append("setup")\n',
    )

    await start(
        FastAPI(),
        config_source=MappingConfigSource({"app": {"modules": ("plain_module",)}}),
    )

    from post_setup_missing_recorder import calls

    assert calls == ["setup"]


@pytest.mark.anyio
async def test_post_setup_site_can_finalise_late_capability_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.syspath_prepend(str(tmp_path))
    _write_module(
        tmp_path,
        "late_dependency_types",
        'class LateCapability:\n    def label(self):\n        return "late"\n',
    )
    _write_module(
        tmp_path,
        "dependent_module",
        "from late_dependency_types import LateCapability\n"
        "proxy = None\n"
        "async def setup_site(site):\n"
        "    global proxy\n"
        "    proxy = site.capability_proxy(LateCapability)\n"
        "async def post_setup_site(site):\n"
        "    proxy.finalise_required()\n",
    )
    _write_module(
        tmp_path,
        "provider_module",
        "from late_dependency_types import LateCapability\n"
        "async def setup_site(site):\n"
        "    site.provide_capability(LateCapability, LateCapability())\n",
    )

    site = await start(
        FastAPI(),
        config_source=MappingConfigSource(
            {"app": {"modules": ("dependent_module", "provider_module")}}
        ),
    )

    capability_type = __import__("late_dependency_types").LateCapability

    assert site.require_capability(capability_type).label() == "late"


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


@pytest.mark.anyio
async def test_start_rejects_non_callable_post_setup_site(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.syspath_prepend(str(tmp_path))
    _write_module(tmp_path, "invalid_post_module", "post_setup_site = object()\n")

    with pytest.raises(
        SiteCapabilityError,
        match="module=invalid_post_module.*attribute=post_setup_site",
    ):
        await start(
            FastAPI(),
            config_source=MappingConfigSource(
                {"app": {"modules": ("invalid_post_module",)}}
            ),
        )


@pytest.mark.anyio
async def test_start_rejects_sync_post_setup_site(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.syspath_prepend(str(tmp_path))
    _write_module(
        tmp_path,
        "sync_post_module",
        'def post_setup_site(site):\n    raise RuntimeError("should not be called")\n',
    )

    with pytest.raises(
        SiteCapabilityError,
        match="module=sync_post_module.*expected=async_callable",
    ):
        await start(
            FastAPI(),
            config_source=MappingConfigSource(
                {"app": {"modules": ("sync_post_module",)}}
            ),
        )


@pytest.mark.anyio
async def test_start_reports_post_setup_site_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.syspath_prepend(str(tmp_path))
    _write_module(
        tmp_path,
        "failing_post_module",
        'async def post_setup_site(site):\n    raise RuntimeError("boom")\n',
    )

    with pytest.raises(
        SiteCapabilityError,
        match="module=failing_post_module.*attribute=post_setup_site.*RuntimeError",
    ):
        await start(
            FastAPI(),
            config_source=MappingConfigSource(
                {"app": {"modules": ("failing_post_module",)}}
            ),
        )
