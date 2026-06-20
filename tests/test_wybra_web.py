import ast
import asyncio
import importlib
import logging
import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from textwrap import dedent
from types import MappingProxyType, SimpleNamespace
from typing import Any, cast

import click
import pytest
from envex import Env
from fastapi import Depends, FastAPI, Request
from fastapi.responses import Response
from fastapi.routing import APIRoute, APIRouter
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient
from jinja2 import Environment, select_autoescape
from jinja2.exceptions import TemplateNotFound

import wybra.template.capabilities as template_capabilities_module
import wybra.web as web_module
from wybra.assets import (
    ComposedStaticFiles,
    StaticAssetCapability,
    StaticAssetDuplicate,
    StaticCollectionError,
    StaticCollectionStatus,
    asset_url,
    collect_configured_static_assets,
    collect_static_assets,
    discover_static_sources,
    static_app_from_config,
    static_asset_response,
    static_sources_from_modules,
)
from wybra.assets.config import _path_value, _url_path_value
from wybra.config import ConfigService, ConfigSourceError, MappingConfigSource
from wybra.core.composition import (
    AppConfig,
    AssetCorsOptions,
    AssetCorsPolicy,
    AssetExportMode,
    AssetOptions,
    CompositionError,
    RouteOptions,
    TemplateOptions,
    load_app_config,
    load_app_config_modules,
    load_modules,
)
from wybra.core.exceptions import ConfigurationError, InputValidationError
from wybra.core.resources import PackageResourceSource, read_text_resource
from wybra.core.settings import (
    EnvironmentSetting,
    SettingsLoadError,
    load_composed_settings,
    values_from_env_settings,
)
from wybra.site import Site, SiteCapabilityError, start
from wybra.template import (
    DefaultTemplateCapability,
    TemplateCapability,
    TemplateSettings,
    build_template_loader,
    render_page,
)
from wybra.template.context import (
    TemplateContext,
    resolve_context_providers,
    set_request_context,
)
from wybra.template.discovery import (
    context_providers_from_modules,
    discover_context_providers,
    discover_template_sources,
    template_sources_from_modules,
)
from wybra.tools import collect as collect_tool
from wybra.web.forms.csrf import CsrfProtector
from wybra.web.routes import (
    ConfiguredModuleRouter,
    RouteCompositionError,
    load_configured_module_routes,
    load_module_routes,
    register_module_routes,
    route_prefixes_from_app_config,
)
from wybra.web.routes.discovery import (
    ModuleSurface,
    discover_module_routers,
    discover_module_surface,
    discover_module_surfaces,
)
from wybra.web.security import (
    COOP_HEADER_NAME,
    SecurityHeaderOptions,
    cross_origin_opener_policy,
    register_security_headers,
)
from wybra.widgets.config import WidgetsSettings


def test_wybra_web_package_imports() -> None:
    package = importlib.import_module("wybra.web")

    assert package.__name__ == "wybra.web"


def test_wybra_widgets_package_imports() -> None:
    package = importlib.import_module("wybra.widgets")

    assert package.__name__ == "wybra.widgets"


def test_wybra_template_package_imports() -> None:
    package = importlib.import_module("wybra.template")

    assert package.__name__ == "wybra.template"


def test_template_settings_load_from_config_service(tmp_path: Path) -> None:
    service = ConfigService(
        [
            MappingConfigSource(
                {
                    "app": {
                        "project_root": tmp_path,
                        "modules": ("wybra.template",),
                    },
                    "app.templates": {
                        "root": "templates",
                        "auto_reload": True,
                        "cache_size": 0,
                    },
                }
            )
        ]
    )

    settings = TemplateSettings.load_settings(service)

    assert settings.project_root == tmp_path.resolve()
    assert settings.root == (tmp_path / "templates").resolve()
    assert settings.auto_reload is True
    assert settings.cache_size == 0


@pytest.mark.anyio
async def test_wybra_template_setup_provides_template_capability(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package_root = tmp_path / "template_capability_app"
    template_root = package_root / "templates"
    template_root.mkdir(parents=True)
    (package_root / "__init__.py").write_text("", encoding="utf-8")
    (template_root / "page.html").write_text(
        "<main>{{ title }}</main>",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()
    app = FastAPI()

    site = await start(
        app,
        config_source=MappingConfigSource(
            {
                "app": {
                    "config_path": tmp_path / "app.toml",
                    "project_root": tmp_path,
                    "modules": ("template_capability_app", "wybra.template"),
                },
                "app.templates": {"auto_reload": True, "cache_size": 0},
            }
        ),
    )

    capability = site.require_capability(TemplateCapability)
    request = Request({"type": "http", "app": app, "headers": []})
    response = capability.render_page(request, "page.html", {"title": "Hello"})

    assert response.body == b"<main>Hello</main>"


@pytest.mark.anyio
async def test_wybra_template_renders_without_assets_when_asset_url_unused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package_root = tmp_path / "template_without_assets_app"
    template_root = package_root / "templates"
    template_root.mkdir(parents=True)
    (package_root / "__init__.py").write_text("", encoding="utf-8")
    (template_root / "page.html").write_text("<main>plain</main>", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()
    app = FastAPI()

    site = await start(
        app,
        config_source=MappingConfigSource(
            {
                "app": {
                    "config_path": tmp_path / "app.toml",
                    "project_root": tmp_path,
                    "modules": ("template_without_assets_app", "wybra.template"),
                },
                "app.templates": {"auto_reload": True, "cache_size": 0},
            }
        ),
    )

    capability = site.require_capability(TemplateCapability)
    request = Request({"type": "http", "app": app, "headers": []})

    assert (
        capability.render_page(request, "page.html", {}).body == b"<main>plain</main>"
    )


@pytest.mark.anyio
async def test_wybra_template_asset_url_fails_lazily_without_assets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package_root = tmp_path / "template_missing_assets_app"
    template_root = package_root / "templates"
    template_root.mkdir(parents=True)
    (package_root / "__init__.py").write_text("", encoding="utf-8")
    (template_root / "page.html").write_text(
        "{{ asset_url('styles/app.css') }}",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()
    app = FastAPI()

    site = await start(
        app,
        config_source=MappingConfigSource(
            {
                "app": {
                    "config_path": tmp_path / "app.toml",
                    "project_root": tmp_path,
                    "modules": ("template_missing_assets_app", "wybra.template"),
                },
                "app.templates": {"auto_reload": True, "cache_size": 0},
            }
        ),
    )

    capability = site.require_capability(TemplateCapability)
    request = Request({"type": "http", "app": app, "headers": []})
    with pytest.raises(SiteCapabilityError, match="Missing static asset capability"):
        capability.render_page(request, "page.html", {})


def test_template_render_context_protects_framework_values(
    tmp_path: Path,
) -> None:
    template_root = tmp_path / "templates"
    template_root.mkdir()
    renderer = DefaultTemplateCapability(
        template_root=template_root,
        csrf=FixedTemplateCsrfProtector(),
    )
    request = Request(
        {
            "type": "http",
            "app": FastAPI(),
            "headers": [],
            "route": SimpleNamespace(name="framework_route"),
        }
    )
    set_request_context(
        request,
        TemplateContext.from_mapping(
            {
                "asset_url": "provider_asset_url",
                "csrf_field_name": "provider_csrf_field_name",
                "csrf_header_name": "provider_csrf_header_name",
                "csrf_token": "provider_csrf_token",
                "request": "provider_request",
                "route_name": "provider_route",
            }
        ),
    )

    context = renderer._template_context(
        request,
        {
            "asset_url": "caller_asset_url",
            "csrf_field_name": "caller_csrf_field_name",
            "csrf_header_name": "caller_csrf_header_name",
            "csrf_token": "caller_csrf_token",
            "request": "caller_request",
            "route_name": "caller_route",
        },
    )

    assert callable(context["asset_url"])
    assert context["csrf_field_name"] == "csrf_token"
    assert context["csrf_header_name"] == "x-csrf-token"
    assert context["csrf_token"] == "framework_csrf_token"
    assert context["request"] is request
    assert context["route_name"] == "framework_route"


class FixedTemplateCsrfProtector:
    def token_context(self, request: Request) -> dict[str, Any]:
        del request
        return {
            "csrf_field_name": "csrf_token",
            "csrf_header_name": "x-csrf-token",
            "csrf_token": "framework_csrf_token",
        }

    def set_cookie(self, request: Request, response: Response) -> None:
        del request, response


def test_template_partial_rendering_does_not_set_csrf_cookie(tmp_path: Path) -> None:
    template_root = tmp_path / "templates"
    template_root.mkdir()
    (template_root / "fragment.html").write_text("<span>ok</span>", encoding="utf-8")

    class CountingCsrfProtector:
        cookie_sets = 0

        def token_context(self, request: Request) -> dict[str, Any]:
            del request
            return {}

        def set_cookie(self, request: Request, response: Response) -> None:
            del request, response
            self.cookie_sets += 1

    csrf = CountingCsrfProtector()
    renderer = DefaultTemplateCapability(template_root=template_root, csrf=csrf)
    request = Request({"type": "http", "app": FastAPI(), "headers": []})

    partial_response = renderer.render_partial(request, "fragment.html", {})

    assert partial_response.body == b"<span>ok</span>"
    assert csrf.cookie_sets == 0

    renderer.render_page(request, "fragment.html", {})

    assert csrf.cookie_sets == 1


def test_wybra_widgets_config_defaults_to_theme_and_login_features() -> None:
    config = MappingConfigSource({"app": {"modules": ("wybra.widgets",)}})
    service = ConfigService([config])

    assert WidgetsSettings.load_settings(
        {
            "features": "theme,login",
            "metadata": {"nested": "option"},
        }
    ).features == ("theme", "login")
    assert WidgetsSettings.load_settings(service).features == ("theme", "login")


def test_wybra_widgets_settings_rejects_non_mapping_config_source() -> None:
    with pytest.raises(ConfigSourceError, match="must be a mapping or ConfigService"):
        WidgetsSettings.section_values(object(), "wybra.widgets")  # type: ignore[arg-type]


def test_wybra_widgets_settings_transform_error_reports_field_without_value() -> None:
    with pytest.raises(ConfigSourceError) as exc_info:
        WidgetsSettings.load_settings({"features": "menu"})

    message = str(exc_info.value)
    assert "wybra.widgets.features" in message
    assert "menu" not in message


def test_wybra_widgets_config_rejects_unknown_feature() -> None:
    config = MappingConfigSource(
        {
            "app": {"modules": ("wybra.widgets",)},
            "wybra.widgets": {"features": ["theme", "menu"]},
        }
    )

    with pytest.raises(ConfigSourceError, match="unknown widget feature"):
        ConfigService([config])


@pytest.mark.anyio
async def test_wybra_widgets_setup_loads_settings_class(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import wybra.widgets as widgets_module
    from wybra.widgets.features import enabled_features

    observed: list[ConfigService] = []

    @dataclass(frozen=True, slots=True)
    class StubWidgetsSettings:
        features: tuple[str, ...] = ("theme",)

    def load_settings(config: ConfigService) -> StubWidgetsSettings:
        observed.append(config)
        return StubWidgetsSettings()

    monkeypatch.setattr(
        widgets_module.WidgetsSettings,
        "load_settings",
        load_settings,
    )

    site = await start(
        FastAPI(),
        config_source=MappingConfigSource(
            {
                "app": {
                    "config_path": tmp_path / "app.toml",
                    "project_root": tmp_path,
                    "modules": ("wybra.widgets",),
                },
            }
        ),
    )

    assert observed == [site.config]
    assert enabled_features() == ("theme",)


@pytest.mark.anyio
async def test_wybra_web_setup_loads_csrf_settings_class(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import wybra.template.setup as template_setup_module

    protector = CsrfProtector("test-secret")
    observed: list[ConfigService] = []

    class StubCsrfSettings:
        def protector(self) -> CsrfProtector:
            return protector

    def load_settings(config: ConfigService) -> StubCsrfSettings:
        observed.append(config)
        return StubCsrfSettings()

    monkeypatch.setattr(
        template_setup_module.CsrfSettings,
        "load_settings",
        load_settings,
    )
    app = FastAPI()

    site = await start(
        app,
        config_source=MappingConfigSource(
            {
                "app": {
                    "config_path": tmp_path / "app.toml",
                    "project_root": tmp_path,
                    "modules": (
                        "wybra.assets",
                        "wybra.template",
                        "wybra.web",
                    ),
                },
            }
        ),
    )

    assert observed == [site.config]
    assert app.state.csrf is protector


def test_load_app_config_accepts_string_static_serve_from_config_sources(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "app.toml"
    config_path.write_text(
        dedent(
            """
            [app]
            modules = ["wybra.web"]

            [app.templates]
            auto_reload = true
            cache_size = 0

            [app.assets]
            url_path = "/static/"
            root = "static"
            serve = "false"
            """
        ),
        encoding="utf-8",
    )

    config = load_app_config(project_root=tmp_path, config_path=config_path)

    assert config.assets.serve is False


def test_asset_root_transform_error_names_config_key() -> None:
    with pytest.raises(ValueError, match="app.assets.root"):
        _path_value("")


def test_asset_url_path_transform_error_names_config_key() -> None:
    with pytest.raises(ValueError, match="app.assets.url_path"):
        _url_path_value("")


def test_load_app_config_requires_app_assets_without_static_fallback(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "app.toml"
    config_path.write_text(
        dedent(
            """
            [app]
            modules = ["wybra.web"]

            [app.templates]
            auto_reload = true
            cache_size = 0

            [app.static]
            url_path = "/static/"
            root = "static"
            """
        ),
        encoding="utf-8",
    )

    with pytest.raises(
        CompositionError,
        match=r"App config must contain a \[app\.assets\] table\.",
    ):
        load_app_config(project_root=tmp_path, config_path=config_path)


def test_load_app_config_defaults_asset_serving_to_enabled(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "app.toml"
    config_path.write_text(
        dedent(
            """
            [app]
            modules = ["wybra.web"]

            [app.templates]
            auto_reload = true
            cache_size = 0

            [app.assets]
            url_path = "/static/"
            root = "static"
            """
        ),
        encoding="utf-8",
    )

    config = load_app_config(project_root=tmp_path, config_path=config_path)

    assert config.assets.serve is True
    assert config.assets.export_mode is AssetExportMode.NORMAL


def test_load_app_config_loads_supported_asset_export_mode(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "app.toml"
    config_path.write_text(
        dedent(
            """
            [app]
            modules = ["wybra.web"]

            [app.templates]
            auto_reload = true
            cache_size = 0

            [app.assets]
            url_path = "/static/"
            root = "static"
            export_mode = "normal"
            """
        ),
        encoding="utf-8",
    )

    config = load_app_config(project_root=tmp_path, config_path=config_path)

    assert config.assets.export_mode is AssetExportMode.NORMAL


def test_load_app_config_rejects_unknown_asset_export_mode(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "app.toml"
    config_path.write_text(
        dedent(
            """
            [app]
            modules = ["wybra.web"]

            [app.templates]
            auto_reload = true
            cache_size = 0

            [app.assets]
            url_path = "/static/"
            root = "static"
            export_mode = "cloud"
            """
        ),
        encoding="utf-8",
    )

    with pytest.raises(
        CompositionError,
        match="app.assets.export_mode must be one of",
    ):
        load_app_config(project_root=tmp_path, config_path=config_path)


def test_load_app_config_loads_asset_cors_with_path_overrides(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "app.toml"
    config_path.write_text(
        dedent(
            """
            [app]
            modules = ["wybra.web"]

            [app.templates]
            auto_reload = true
            cache_size = 0

            [app.assets]
            url_path = "/static/"
            root = "static"

            [app.assets.cors]
            enabled = true
            allow_origins = ["https://example.com"]
            allow_methods = ["GET", "HEAD"]
            max_age = 120

            [app.assets.cors.paths."/static/private/"]
            allow_origins = ["https://admin.example.com"]
            expose_headers = ["x-static"]
            """
        ),
        encoding="utf-8",
    )

    config = load_app_config(project_root=tmp_path, config_path=config_path)

    assert config.assets.cors == AssetCorsOptions(
        enabled=True,
        allow_origins=("https://example.com",),
        allow_methods=("GET", "HEAD"),
        max_age=120,
        paths={
            "/static/private/": AssetCorsPolicy(
                allow_origins=("https://admin.example.com",),
                allow_methods=("GET", "HEAD"),
                expose_headers=("x-static",),
                max_age=120,
            )
        },
    )
    assert isinstance(config.assets.cors.paths, MappingProxyType)
    mutable_paths = cast(Any, config.assets.cors.paths)
    with pytest.raises(TypeError):
        mutable_paths["/static/admin/"] = AssetCorsPolicy()


def _write_widget_page_app(tmp_path: Path, module_name: str) -> None:
    package_root = tmp_path / module_name
    template_root = package_root / "templates"
    template_root.mkdir(parents=True)
    (package_root / "__init__.py").write_text("", encoding="utf-8")
    (package_root / "routes.py").write_text(
        dedent(
            """
            from fastapi import APIRouter, Request
            from wybra.template import render_page

            router = APIRouter()

            @router.get("/")
            async def home(request: Request):
                return render_page(request, "home.html", {"page_title": "Widgets"})

            module_routers = {"default": router}
            """
        ),
        encoding="utf-8",
    )
    (template_root / "home.html").write_text(
        "{% extends 'layouts/page.html' %}{% block content %}home{% endblock %}",
        encoding="utf-8",
    )


def _write_fake_auth_module(
    tmp_path: Path,
    module_name: str,
    *,
    user_email: str | None = None,
) -> None:
    package_root = tmp_path / module_name
    package_root.mkdir(parents=True)
    user_expression = (
        "None"
        if user_email is None
        else f"SimpleNamespace(id='user-id', email={user_email!r})"
    )
    (package_root / "__init__.py").write_text(
        dedent(
            f"""
            from types import SimpleNamespace

            from wybra.auth import AuthCapability

            from .routes import module_routers


            async def optional_current_user(_request):
                return {user_expression}


            async def login_required(request):
                user = await optional_current_user(request)
                if user is None:
                    raise RuntimeError("not authenticated")
                return user


            async def anonymous_required(_request):
                return None


            class FakeAuthCapability:
                settings = None
                fastapi_users = None

                @property
                def optional_current_user(self):
                    return optional_current_user

                @property
                def login_required(self):
                    return login_required

                @property
                def anonymous_required(self):
                    return anonymous_required


            async def setup_site(site):
                site.provide_capability(AuthCapability, FakeAuthCapability())
            """
        ),
        encoding="utf-8",
    )
    (package_root / "routes.py").write_text(
        dedent(
            """
            from fastapi import APIRouter
            from fastapi.responses import Response

            router = APIRouter()

            @router.get("/login", name="auth:login")
            async def login():
                return Response("login")

            @router.get("/logout", name="auth:logout")
            async def logout():
                return Response("logout")

            module_routers = {"account": router}
            """
        ),
        encoding="utf-8",
    )


@pytest.mark.anyio
async def test_wybra_web_setup_site_registers_routes_renderer_and_static(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package_root = tmp_path / "configured_web_app"
    static_root = package_root / "static" / "styles"
    template_root = package_root / "templates"
    static_root.mkdir(parents=True)
    template_root.mkdir(parents=True)
    (package_root / "__init__.py").write_text("", encoding="utf-8")
    (package_root / "routes.py").write_text(
        dedent(
            """
            from fastapi import APIRouter
            from fastapi.responses import Response

            router = APIRouter()

            @router.get("/ping", name="configured:ping")
            async def ping():
                return Response("pong")

            module_routers = {"default": router}
            """
        ),
        encoding="utf-8",
    )
    (static_root / "app.css").write_text("body {}", encoding="utf-8")
    (template_root / "page.html").write_text("page", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()
    app = FastAPI()

    site = await start(
        app,
        config_source=MappingConfigSource(
            {
                "app": {
                    "config_path": tmp_path / "app.toml",
                    "project_root": tmp_path,
                    "modules": (
                        "configured_web_app",
                        "wybra.assets",
                        "wybra.template",
                        "wybra.web",
                    ),
                },
                "app.routes": {
                    "prefixes": {
                        "configured_web_app": {"default": ""},
                        "wybra.web": {},
                    }
                },
                "app.templates": {"auto_reload": True, "cache_size": 0},
                "app.assets": {"url_path": "/static/"},
            }
        ),
    )

    assert site.app is app
    templates = site.require_capability(TemplateCapability)
    request = Request({"type": "http", "app": app, "headers": []})
    assert templates.render_page(request, "page.html", {}).body == b"page"
    static_route = next(
        route for route in app.routes if getattr(route, "name", None) == "static"
    )
    assert isinstance(static_route.app, ComposedStaticFiles)
    assert TestClient(app).get("/ping").text == "pong"
    assert TestClient(app).get("/static/styles/app.css").text == "body {}"


@pytest.mark.anyio
async def test_wybra_web_setup_can_disable_static_serving(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package_root = tmp_path / "static_disabled_app"
    static_root = package_root / "static"
    static_root.mkdir(parents=True)
    (package_root / "__init__.py").write_text("", encoding="utf-8")
    (static_root / "app.css").write_text("body {}", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()
    app = FastAPI()

    site = await start(
        app,
        config_source=MappingConfigSource(
            {
                "app": {
                    "config_path": tmp_path / "app.toml",
                    "project_root": tmp_path,
                    "modules": ("static_disabled_app", "wybra.assets", "wybra.web"),
                },
                "app.routes": {
                    "prefixes": {
                        "static_disabled_app": {},
                        "wybra.web": {},
                    }
                },
                "app.templates": {"auto_reload": True, "cache_size": 0},
                "app.assets": {
                    "url_path": "/static/",
                    "serve": False,
                },
            }
        ),
    )

    assert site.require_capability(StaticAssetCapability).url_path == "/static"
    assert not any(
        route for route in app.routes if getattr(route, "name", None) == "static"
    )
    assert TestClient(app).get("/static/app.css").status_code == 404


@pytest.mark.anyio
async def test_wybra_assets_setup_provides_static_asset_capability(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package_root = tmp_path / "asset_capability_app"
    static_root = package_root / "static"
    static_root.mkdir(parents=True)
    (package_root / "__init__.py").write_text("", encoding="utf-8")
    (static_root / "app.css").write_text("body {}", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()
    app = FastAPI()

    site = await start(
        app,
        config_source=MappingConfigSource(
            {
                "app": {
                    "config_path": tmp_path / "app.toml",
                    "project_root": tmp_path,
                    "modules": ("asset_capability_app", "wybra.assets"),
                },
                "app.routes": {"prefixes": {"asset_capability_app": {}}},
                "app.templates": {"auto_reload": True, "cache_size": 0},
                "app.assets": {"url_path": "/assets/"},
            }
        ),
    )

    capability = site.require_capability(StaticAssetCapability)

    assert capability.url_path == "/assets"
    assert capability.url("app.css") == "/assets/app.css"
    assert not hasattr(capability, "storage")
    assert capability.sources == (
        PackageResourceSource(package="asset_capability_app", directory="static"),
    )
    static_route = next(
        route for route in app.routes if getattr(route, "name", None) == "static"
    )
    assert isinstance(static_route.app, ComposedStaticFiles)
    assert TestClient(app).get("/assets/app.css").text == "body {}"


@pytest.mark.anyio
async def test_wybra_web_setup_without_static_asset_capability_is_valid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package_root = tmp_path / "plain_web_app"
    template_root = package_root / "templates"
    template_root.mkdir(parents=True)
    (package_root / "__init__.py").write_text("", encoding="utf-8")
    (template_root / "page.html").write_text("<main>plain</main>", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()
    app = FastAPI()

    await start(
        app,
        config_source=MappingConfigSource(
            {
                "app": {
                    "config_path": tmp_path / "app.toml",
                    "project_root": tmp_path,
                    "modules": ("plain_web_app", "wybra.template", "wybra.web"),
                },
                "app.routes": {"prefixes": {"plain_web_app": {}, "wybra.web": {}}},
                "app.templates": {"auto_reload": True, "cache_size": 0},
            }
        ),
    )

    @app.get("/")
    async def home(request: Request) -> Response:
        return render_page(request, "page.html")

    assert TestClient(app).get("/").text == "<main>plain</main>"


@pytest.mark.anyio
async def test_wybra_web_setup_serves_module_static_sources_not_collection_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package_root = tmp_path / "source_static_app"
    source_root = package_root / "static"
    source_root.mkdir(parents=True)
    (package_root / "__init__.py").write_text("", encoding="utf-8")
    (source_root / "app.css").write_text("source {}", encoding="utf-8")
    collection_root = tmp_path / "assets"
    collection_root.mkdir()
    (collection_root / "app.css").write_text("collected {}", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()
    app = FastAPI()

    await start(
        app,
        config_source=MappingConfigSource(
            {
                "app": {
                    "config_path": tmp_path / "app.toml",
                    "project_root": tmp_path,
                    "modules": ("source_static_app", "wybra.assets", "wybra.web"),
                },
                "app.routes": {"prefixes": {"source_static_app": {}, "wybra.web": {}}},
                "app.templates": {"auto_reload": True, "cache_size": 0},
                "app.assets": {
                    "url_path": "/static/",
                    "root": "assets",
                },
            }
        ),
    )

    static_route = next(
        route for route in app.routes if getattr(route, "name", None) == "static"
    )
    assert not isinstance(static_route.app, StaticFiles)
    assert TestClient(app).get("/static/app.css").text == "source {}"


def test_static_app_from_config_rejects_missing_configured_static_root(
    tmp_path: Path,
) -> None:
    with pytest.raises(ConfigurationError, match="does not exist"):
        static_app_from_config(
            project_root=tmp_path,
            static_root=Path("missing-static"),
            static_sources=(),
        )


def test_static_app_from_config_uses_module_assets_when_static_root_absent(
    tmp_path: Path,
) -> None:
    app = static_app_from_config(
        project_root=tmp_path,
        static_root=None,
        static_sources=(
            PackageResourceSource(package="wybra.web", directory="static"),
        ),
    )

    assert isinstance(app, ComposedStaticFiles)


@pytest.mark.anyio
async def test_wybra_web_setup_applies_global_asset_cors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package_root = tmp_path / "cors_static_app"
    static_root = package_root / "static"
    static_root.mkdir(parents=True)
    (package_root / "__init__.py").write_text("", encoding="utf-8")
    (static_root / "app.css").write_text("body {}", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()
    app = FastAPI()

    await start(
        app,
        config_source=MappingConfigSource(
            {
                "app": {
                    "config_path": tmp_path / "app.toml",
                    "project_root": tmp_path,
                    "modules": ("cors_static_app", "wybra.assets", "wybra.web"),
                },
                "app.routes": {"prefixes": {"cors_static_app": {}, "wybra.web": {}}},
                "app.templates": {"auto_reload": True, "cache_size": 0},
                "app.assets": {
                    "url_path": "/static/",
                    "root": "assets",
                },
                "app.assets.cors": {
                    "enabled": True,
                    "allow_origins": ("https://example.com",),
                    "allow_methods": ("GET", "HEAD"),
                },
            }
        ),
    )

    response = TestClient(app).get(
        "/static/app.css",
        headers={"Origin": "https://example.com"},
    )

    assert response.text == "body {}"
    assert response.headers["access-control-allow-origin"] == "https://example.com"


@pytest.mark.anyio
async def test_wybra_web_setup_applies_per_path_asset_cors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package_root = tmp_path / "path_cors_static_app"
    static_root = package_root / "static"
    private_root = static_root / "private"
    private_root.mkdir(parents=True)
    (package_root / "__init__.py").write_text("", encoding="utf-8")
    (static_root / "app.css").write_text("body {}", encoding="utf-8")
    (private_root / "admin.css").write_text("admin {}", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()
    app = FastAPI()

    await start(
        app,
        config_source=MappingConfigSource(
            {
                "app": {
                    "config_path": tmp_path / "app.toml",
                    "project_root": tmp_path,
                    "modules": ("path_cors_static_app", "wybra.assets", "wybra.web"),
                },
                "app.routes": {
                    "prefixes": {"path_cors_static_app": {}, "wybra.web": {}}
                },
                "app.templates": {"auto_reload": True, "cache_size": 0},
                "app.assets": {
                    "url_path": "/static/",
                    "root": "assets",
                },
                "app.assets.cors": {
                    "enabled": True,
                    "allow_origins": ("https://example.com",),
                    "paths": {
                        "/static/private/": {
                            "allow_origins": ("https://admin.example.com",),
                        },
                    },
                },
            }
        ),
    )
    client = TestClient(app)

    public_response = client.get(
        "/static/app.css",
        headers={"Origin": "https://example.com"},
    )
    private_response = client.get(
        "/static/private/admin.css",
        headers={"Origin": "https://admin.example.com"},
    )
    rejected_private_response = client.get(
        "/static/private/admin.css",
        headers={"Origin": "https://example.com"},
    )

    assert public_response.headers["access-control-allow-origin"] == (
        "https://example.com"
    )
    assert private_response.headers["access-control-allow-origin"] == (
        "https://admin.example.com"
    )
    assert "access-control-allow-origin" not in rejected_private_response.headers


@pytest.mark.anyio
async def test_wybra_web_setup_uses_normalised_static_mount_path_for_asset_cors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package_root = tmp_path / "normalised_cors_static_app"
    static_root = package_root / "static"
    private_root = static_root / "private"
    private_root.mkdir(parents=True)
    (package_root / "__init__.py").write_text("", encoding="utf-8")
    (private_root / "admin.css").write_text("admin {}", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()
    app = FastAPI()

    site = await start(
        app,
        config_source=MappingConfigSource(
            {
                "app": {
                    "config_path": tmp_path / "app.toml",
                    "project_root": tmp_path,
                    "modules": (
                        "normalised_cors_static_app",
                        "wybra.assets",
                        "wybra.web",
                    ),
                },
                "app.routes": {
                    "prefixes": {"normalised_cors_static_app": {}, "wybra.web": {}}
                },
                "app.templates": {"auto_reload": True, "cache_size": 0},
                "app.assets": {
                    "url_path": "static",
                    "root": "assets",
                },
                "app.assets.cors": {
                    "enabled": True,
                    "allow_origins": ("https://example.com",),
                    "paths": {
                        "/static/private/": {
                            "allow_origins": ("https://admin.example.com",),
                        },
                    },
                },
            }
        ),
    )

    response = TestClient(app).get(
        "/static/private/admin.css",
        headers={"Origin": "https://admin.example.com"},
    )

    assert site.require_capability(StaticAssetCapability).url_path == "/static"
    assert response.headers["access-control-allow-origin"] == (
        "https://admin.example.com"
    )


@pytest.mark.anyio
async def test_wybra_widgets_publish_theme_selector_when_enabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package_root = tmp_path / "widget_page_app"
    template_root = package_root / "templates"
    template_root.mkdir(parents=True)
    (package_root / "__init__.py").write_text("", encoding="utf-8")
    (package_root / "routes.py").write_text(
        dedent(
            """
            from fastapi import APIRouter, Request
            from wybra.template import render_page

            router = APIRouter()

            @router.get("/")
            async def home(request: Request):
                return render_page(request, "home.html", {"page_title": "Widgets"})

            module_routers = {"default": router}
            """
        ),
        encoding="utf-8",
    )
    (template_root / "home.html").write_text(
        "{% extends 'layouts/page.html' %}{% block content %}home{% endblock %}",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()
    app = FastAPI()

    await start(
        app,
        config_source=MappingConfigSource(
            {
                "app": {
                    "config_path": tmp_path / "app.toml",
                    "project_root": tmp_path,
                    "modules": (
                        "widget_page_app",
                        "wybra.widgets",
                        "wybra.assets",
                        "wybra.template",
                        "wybra.web",
                    ),
                },
                "app.routes": {
                    "prefixes": {
                        "widget_page_app": {"default": ""},
                        "wybra.widgets": {"partials": "", "api": ""},
                        "wybra.web": {},
                    }
                },
                "app.templates": {"auto_reload": True, "cache_size": 0},
                "app.assets": {"url_path": "/static/"},
                "wybra.widgets": {"features": ["theme"]},
            }
        ),
    )

    with TestClient(app) as client:
        page = client.get("/")
        theme = client.get("/api/widgets/theme")
        widgets_css = client.get("/static/styles/widgets.css")

    assert page.status_code == 200
    assert 'id="theme-selector"' in page.text
    assert 'href="/static/styles/widgets.css"' in page.text
    assert theme.json() == {"theme_mode": "auto"}
    assert widgets_css.status_code == 200
    assert ".theme-selector" in widgets_css.text


@pytest.mark.anyio
async def test_wybra_widgets_publish_login_button_when_auth_is_available(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_widget_page_app(tmp_path, "login_widget_page_app")
    _write_fake_auth_module(tmp_path, "login_widget_auth")
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()
    app = FastAPI()

    await start(
        app,
        config_source=MappingConfigSource(
            {
                "app": {
                    "config_path": tmp_path / "app.toml",
                    "project_root": tmp_path,
                    "modules": (
                        "login_widget_page_app",
                        "login_widget_auth",
                        "wybra.widgets",
                        "wybra.assets",
                        "wybra.template",
                        "wybra.web",
                    ),
                },
                "app.routes": {
                    "prefixes": {
                        "login_widget_page_app": {"default": ""},
                        "login_widget_auth": {"account": "/account"},
                        "wybra.widgets": {"partials": "", "api": ""},
                        "wybra.web": {},
                    }
                },
                "app.templates": {"auto_reload": True, "cache_size": 0},
                "app.assets": {"url_path": "/static/"},
            }
        ),
    )

    page = TestClient(app).get("/")

    assert page.status_code == 200
    assert 'class="login-widget login-widget--anonymous"' in page.text
    assert 'href="/account/login"' in page.text
    assert page.text.index("login-widget") < page.text.index("theme-selector")


@pytest.mark.anyio
async def test_wybra_widgets_publish_profile_initial_and_logout_when_authenticated(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_widget_page_app(tmp_path, "authenticated_widget_page_app")
    _write_fake_auth_module(
        tmp_path,
        "authenticated_widget_auth",
        user_email="david@example.com",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()
    app = FastAPI()

    await start(
        app,
        config_source=MappingConfigSource(
            {
                "app": {
                    "config_path": tmp_path / "app.toml",
                    "project_root": tmp_path,
                    "modules": (
                        "authenticated_widget_page_app",
                        "authenticated_widget_auth",
                        "wybra.widgets",
                        "wybra.assets",
                        "wybra.template",
                        "wybra.web",
                    ),
                },
                "app.routes": {
                    "prefixes": {
                        "authenticated_widget_page_app": {"default": ""},
                        "authenticated_widget_auth": {"account": "/account"},
                        "wybra.widgets": {"partials": "", "api": ""},
                        "wybra.web": {},
                    }
                },
                "app.templates": {"auto_reload": True, "cache_size": 0},
                "app.assets": {"url_path": "/static/"},
            }
        ),
    )

    page = TestClient(app).get("/")

    assert page.status_code == 200
    assert 'class="login-widget login-widget--authenticated"' in page.text
    assert 'href="/account/logout"' in page.text
    assert "login-widget__avatar" not in page.text


@pytest.mark.anyio
async def test_wybra_widgets_use_profile_descriptor_when_profile_is_available(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_widget_page_app(tmp_path, "profile_widget_page_app")
    _write_fake_auth_module(
        tmp_path,
        "profile_widget_auth",
        user_email="david@example.com",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()
    app = FastAPI()

    await start(
        app,
        config_source=MappingConfigSource(
            {
                "app": {
                    "config_path": tmp_path / "app.toml",
                    "project_root": tmp_path,
                    "modules": (
                        "profile_widget_page_app",
                        "profile_widget_auth",
                        "wybra.widgets",
                        "wybra.profile",
                        "wybra.assets",
                        "wybra.template",
                        "wybra.web",
                    ),
                },
                "app.routes": {
                    "prefixes": {
                        "profile_widget_page_app": {"default": ""},
                        "profile_widget_auth": {"account": "/account"},
                        "wybra.widgets": {"partials": "", "api": ""},
                        "wybra.profile": {},
                        "wybra.web": {},
                    }
                },
                "app.templates": {"auto_reload": True, "cache_size": 0},
                "app.assets": {"url_path": "/static/"},
            }
        ),
    )

    page = TestClient(app).get("/")

    assert page.status_code == 200
    assert 'class="login-widget login-widget--authenticated"' in page.text
    assert 'href="/account/logout"' in page.text
    assert "login-widget__avatar login-widget__avatar--fallback" in page.text
    assert re.search(
        r'<span[^>]*class="[^"]*login-widget__avatar[^"]*"[^>]*>\s*D\s*</span>',
        page.text,
    )


@pytest.mark.anyio
async def test_wybra_widgets_do_not_publish_login_button_without_auth(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_widget_page_app(tmp_path, "no_auth_widget_page_app")
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()
    app = FastAPI()

    await start(
        app,
        config_source=MappingConfigSource(
            {
                "app": {
                    "config_path": tmp_path / "app.toml",
                    "project_root": tmp_path,
                    "modules": (
                        "no_auth_widget_page_app",
                        "wybra.widgets",
                        "wybra.assets",
                        "wybra.template",
                        "wybra.web",
                    ),
                },
                "app.routes": {
                    "prefixes": {
                        "no_auth_widget_page_app": {"default": ""},
                        "wybra.widgets": {"partials": "", "api": ""},
                        "wybra.web": {},
                    }
                },
                "app.templates": {"auto_reload": True, "cache_size": 0},
                "app.assets": {"url_path": "/static/"},
            }
        ),
    )

    page = TestClient(app).get("/")

    assert page.status_code == 200
    assert "login-widget" not in page.text
    assert 'id="theme-selector"' in page.text


@pytest.mark.anyio
async def test_wybra_widgets_can_disable_login_button_when_auth_is_available(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_widget_page_app(tmp_path, "disabled_login_widget_page_app")
    _write_fake_auth_module(tmp_path, "disabled_login_widget_auth")
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()
    app = FastAPI()

    await start(
        app,
        config_source=MappingConfigSource(
            {
                "app": {
                    "config_path": tmp_path / "app.toml",
                    "project_root": tmp_path,
                    "modules": (
                        "disabled_login_widget_page_app",
                        "disabled_login_widget_auth",
                        "wybra.widgets",
                        "wybra.assets",
                        "wybra.template",
                        "wybra.web",
                    ),
                },
                "app.routes": {
                    "prefixes": {
                        "disabled_login_widget_page_app": {"default": ""},
                        "disabled_login_widget_auth": {"account": "/account"},
                        "wybra.widgets": {"partials": "", "api": ""},
                        "wybra.web": {},
                    }
                },
                "app.templates": {"auto_reload": True, "cache_size": 0},
                "app.assets": {"url_path": "/static/"},
                "wybra.widgets": {"features": ["theme"]},
            }
        ),
    )

    page = TestClient(app).get("/")

    assert page.status_code == 200
    assert "login-widget" not in page.text
    assert 'id="theme-selector"' in page.text


def test_wybra_widgets_css_defines_login_and_mobile_semantics() -> None:
    css = read_text_resource(
        (PackageResourceSource(package="wybra.widgets", directory="static"),),
        "styles/widgets.css",
    )
    web_css = read_text_resource(
        (PackageResourceSource(package="wybra.template", directory="static"),),
        "styles/app.css",
    )

    assert ".login-widget__action" in css
    assert ".login-widget__avatar" in css
    assert ".web-responsive-compact-hidden" in web_css
    assert ".web-responsive-compact-centre" in web_css


@pytest.mark.anyio
async def test_wybra_widgets_can_disable_theme_selector(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package_root = tmp_path / "disabled_widget_page_app"
    template_root = package_root / "templates"
    template_root.mkdir(parents=True)
    (package_root / "__init__.py").write_text("", encoding="utf-8")
    (package_root / "routes.py").write_text(
        dedent(
            """
            from fastapi import APIRouter, Request
            from wybra.template import render_page

            router = APIRouter()

            @router.get("/")
            async def home(request: Request):
                return render_page(request, "home.html", {"page_title": "Widgets"})

            module_routers = {"default": router}
            """
        ),
        encoding="utf-8",
    )
    (template_root / "home.html").write_text(
        "{% extends 'layouts/page.html' %}{% block content %}home{% endblock %}",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()
    app = FastAPI()

    await start(
        app,
        config_source=MappingConfigSource(
            {
                "app": {
                    "config_path": tmp_path / "app.toml",
                    "project_root": tmp_path,
                    "modules": (
                        "disabled_widget_page_app",
                        "wybra.widgets",
                        "wybra.assets",
                        "wybra.template",
                        "wybra.web",
                    ),
                },
                "app.routes": {
                    "prefixes": {
                        "disabled_widget_page_app": {"default": ""},
                        "wybra.web": {},
                    }
                },
                "app.templates": {"auto_reload": True, "cache_size": 0},
                "app.assets": {"url_path": "/static/"},
                "wybra.widgets": {"features": []},
            }
        ),
    )

    with TestClient(app) as client:
        page = client.get("/")
        theme = client.get("/api/widgets/theme")

    assert page.status_code == 200
    assert 'id="theme-selector"' not in page.text
    assert theme.status_code == 404


@pytest.mark.anyio
async def test_application_template_overrides_widget_partial(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package_root = tmp_path / "widget_override_app"
    template_root = package_root / "templates"
    (template_root / "components").mkdir(parents=True)
    (package_root / "__init__.py").write_text("", encoding="utf-8")
    (package_root / "routes.py").write_text(
        dedent(
            """
            from fastapi import APIRouter, Request
            from wybra.template import render_page

            router = APIRouter()

            @router.get("/")
            async def home(request: Request):
                return render_page(request, "home.html", {"page_title": "Override"})

            module_routers = {"default": router}
            """
        ),
        encoding="utf-8",
    )
    (template_root / "home.html").write_text(
        "{% extends 'layouts/page.html' %}{% block content %}home{% endblock %}",
        encoding="utf-8",
    )
    (template_root / "components/theme_selector.html").write_text(
        "application selector",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()
    app = FastAPI()

    await start(
        app,
        config_source=MappingConfigSource(
            {
                "app": {
                    "config_path": tmp_path / "app.toml",
                    "project_root": tmp_path,
                    "modules": (
                        "widget_override_app",
                        "wybra.widgets",
                        "wybra.assets",
                        "wybra.template",
                        "wybra.web",
                    ),
                },
                "app.routes": {
                    "prefixes": {
                        "widget_override_app": {"default": ""},
                        "wybra.widgets": {"partials": "", "api": ""},
                        "wybra.web": {},
                    }
                },
                "app.templates": {"auto_reload": True, "cache_size": 0},
                "app.assets": {"url_path": "/static/"},
            }
        ),
    )

    page = TestClient(app).get("/")

    assert "application selector" in page.text
    assert 'id="theme-selector"' not in page.text


@pytest.mark.anyio
async def test_wybra_web_request_context_is_enabled_by_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package_root = tmp_path / "request_context_app"
    template_root = package_root / "templates"
    template_root.mkdir(parents=True)
    (package_root / "__init__.py").write_text("", encoding="utf-8")
    (package_root / "routes.py").write_text(
        dedent(
            """
            from fastapi import APIRouter, Request
            from wybra.template import render_page

            router = APIRouter()

            @router.get("/request-path")
            async def request_path(request: Request):
                return render_page(request, "request_path.html")

            module_routers = {"default": router}
            """
        ),
        encoding="utf-8",
    )
    (template_root / "request_path.html").write_text(
        "{{ request.url.path if request is defined else 'missing' }}",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()
    app = FastAPI()

    await start(
        app,
        config_source=MappingConfigSource(
            {
                "app": {
                    "config_path": tmp_path / "app.toml",
                    "project_root": tmp_path,
                    "modules": (
                        "request_context_app",
                        "wybra.assets",
                        "wybra.template",
                        "wybra.web",
                    ),
                },
                "app.routes": {
                    "prefixes": {
                        "request_context_app": {"default": ""},
                        "wybra.web": {},
                    }
                },
                "app.templates": {
                    "auto_reload": True,
                    "cache_size": 0,
                    "root": str(template_root),
                },
                "app.assets": {"url_path": "/static/"},
            }
        ),
    )

    assert TestClient(app).get("/request-path").text == "/request-path"


@pytest.mark.anyio
async def test_wybra_web_request_context_can_be_disabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package_root = tmp_path / "disabled_request_context_app"
    template_root = package_root / "templates"
    template_root.mkdir(parents=True)
    (package_root / "__init__.py").write_text("", encoding="utf-8")
    (package_root / "routes.py").write_text(
        dedent(
            """
            from fastapi import APIRouter, Request
            from wybra.template import render_page

            router = APIRouter()

            @router.get("/request-path")
            async def request_path(request: Request):
                return render_page(request, "request_path.html")

            module_routers = {"default": router}
            """
        ),
        encoding="utf-8",
    )
    (template_root / "request_path.html").write_text(
        "{{ request.url.path if request is defined else 'missing' }}",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()
    app = FastAPI()

    await start(
        app,
        config_source=MappingConfigSource(
            {
                "app": {
                    "config_path": tmp_path / "app.toml",
                    "project_root": tmp_path,
                    "modules": (
                        "disabled_request_context_app",
                        "wybra.assets",
                        "wybra.template",
                        "wybra.web",
                    ),
                },
                "app.routes": {
                    "prefixes": {
                        "disabled_request_context_app": {"default": ""},
                        "wybra.web": {},
                    }
                },
                "app.templates": {
                    "auto_reload": True,
                    "cache_size": 0,
                    "root": str(template_root),
                    "request_context_enabled": False,
                },
                "app.assets": {"url_path": "/static/"},
            }
        ),
    )

    assert TestClient(app).get("/request-path").text == "missing"


@pytest.mark.anyio
async def test_wybra_web_setup_is_omitted_when_module_is_not_configured() -> None:
    app = FastAPI()

    await start(
        app,
        config_source=MappingConfigSource({"app": {"modules": ()}}),
    )

    assert not hasattr(app.state, "renderer")
    assert all(getattr(route, "name", None) != "static" for route in app.routes)


@pytest.mark.anyio
async def test_wybra_web_registers_auth_routes_through_module_composition(
    tmp_path: Path,
) -> None:
    app = FastAPI()

    await start(
        app,
        config_source=MappingConfigSource(
            {
                "app": {
                    "config_path": tmp_path / "app.toml",
                    "project_root": tmp_path,
                    "modules": (
                        "wybra.assets",
                        "wybra.template",
                        "wybra.web",
                        "wybra.db",
                        "wybra.auth",
                    ),
                    "database_url": f"sqlite+aiosqlite:///{tmp_path / 'app.sqlite3'}",
                },
                "app.routes": {
                    "prefixes": {
                        "wybra.web": {},
                        "wybra.auth": {"account": "/account", "api": ""},
                    }
                },
                "app.templates": {"auto_reload": True, "cache_size": 0},
                "app.assets": {"url_path": "/static/"},
            }
        ),
    )

    assert TestClient(app).get("/account/login").status_code == 200


def test_wybra_web_package_exposes_expected_submodules() -> None:
    for module_name in (
        "wybra.core.composition",
        "wybra.template.context",
        "wybra.web.forms.csrf",
        "wybra.web.errors",
        "wybra.web.forms.security",
        "wybra.core.resources",
        "wybra.web.routes.contracts",
        "wybra.web.routes",
        "wybra.web.security",
        "wybra.assets",
        "wybra.core.settings",
        "wybra.web.routes",
        "wybra.web.routes.discovery",
        "wybra.template",
        "wybra.web.views",
        "wybra.widgets",
        "wybra.widgets.config",
        "wybra.widgets.context",
        "wybra.widgets.routes",
        "wybra.widgets.theme",
    ):
        assert importlib.import_module(module_name).__name__ == module_name


def test_wybra_web_package_is_independent_from_application_and_auth_modules() -> None:
    source_root = Path(__file__).resolve().parents[1] / "src" / "wybra" / "web"

    for path in source_root.rglob("*.py"):
        tree = ast.parse(path.read_text(), filename=str(path))
        imported_modules = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        } | {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module is not None
        }
        assert not any(
            module == "host_app"
            or module.startswith("host_app.")
            or module == "wybra.auth"
            or module.startswith("wybra.auth.")
            for module in imported_modules
        )


def test_wybra_web_composition_loader_is_cli_safe() -> None:
    path = Path(__file__).resolve().parents[1] / "src/wybra/core/composition.py"
    tree = ast.parse(path.read_text(), filename=str(path))
    imported_modules = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }

    forbidden_modules = {
        "wybra.auth",
        "fastapi",
        "jinja2",
        "host_app.app",
        "host_app.routes",
        "host_app.settings",
    }
    assert not any(
        module == forbidden_module or module.startswith(f"{forbidden_module}.")
        for module in imported_modules
        for forbidden_module in forbidden_modules
    )


def test_wybra_web_package_is_included_in_build_modules() -> None:
    pyproject = tomllib.loads(
        (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text()
    )

    assert "wybra" in pyproject["tool"]["uv"]["build-backend"]["module-name"]


def test_security_headers_apply_default_cross_origin_opener_policy() -> None:
    app = FastAPI()
    register_security_headers(app)

    @app.get("/")
    async def home() -> Response:
        return Response("ok")

    response = TestClient(app).get("/")

    assert response.headers[COOP_HEADER_NAME] == "same-origin"


def test_security_headers_can_disable_default_cross_origin_opener_policy() -> None:
    app = FastAPI()
    register_security_headers(
        app,
        options=SecurityHeaderOptions(cross_origin_opener_policy=None),
    )

    @app.get("/")
    async def home() -> Response:
        return Response("ok")

    response = TestClient(app).get("/")

    assert COOP_HEADER_NAME not in response.headers


def test_security_headers_can_override_cross_origin_opener_policy_per_route() -> None:
    app = FastAPI()
    register_security_headers(app)

    @app.get(
        "/popup",
        dependencies=[
            Depends(cross_origin_opener_policy("same-origin-allow-popups")),
        ],
    )
    async def popup() -> Response:
        return Response("popup")

    response = TestClient(app).get("/popup")

    assert response.headers[COOP_HEADER_NAME] == "same-origin-allow-popups"


def test_security_headers_can_exempt_cross_origin_opener_policy_per_route() -> None:
    app = FastAPI()
    register_security_headers(app)

    @app.get("/embed", dependencies=[Depends(cross_origin_opener_policy(None))])
    async def embed() -> Response:
        return Response("embed")

    response = TestClient(app).get("/embed")

    assert COOP_HEADER_NAME not in response.headers


def test_security_headers_preserve_explicit_response_header() -> None:
    app = FastAPI()
    register_security_headers(app)

    @app.get("/")
    async def home() -> Response:
        return Response("ok", headers={COOP_HEADER_NAME: "unsafe-none"})

    response = TestClient(app).get("/")

    assert response.headers[COOP_HEADER_NAME] == "unsafe-none"


def test_security_header_options_reject_invalid_cross_origin_opener_policy() -> None:
    with pytest.raises(ValueError, match="Cross-Origin-Opener-Policy.*invalid"):
        SecurityHeaderOptions(cross_origin_opener_policy="invalid")  # type: ignore[arg-type]


def test_cross_origin_opener_policy_dependency_rejects_invalid_policy() -> None:
    with pytest.raises(ValueError, match="Cross-Origin-Opener-Policy.*invalid"):
        cross_origin_opener_policy("invalid")  # type: ignore[arg-type]


def test_register_security_headers_is_idempotent_and_updates_options() -> None:
    app = FastAPI()
    register_security_headers(app)
    register_security_headers(
        app,
        options=SecurityHeaderOptions(cross_origin_opener_policy="unsafe-none"),
    )

    @app.get("/")
    async def home() -> Response:
        return Response("ok")

    response = TestClient(app).get("/")

    assert len(app.user_middleware) == 1
    assert response.headers[COOP_HEADER_NAME] == "unsafe-none"


def test_composed_settings_loader_builds_framework_settings(tmp_path: Path) -> None:
    @dataclass(frozen=True, slots=True)
    class FrameworkSettings:
        project_root: Path
        app_name: str = "default-app"
        static_url_path: str = "/static/"
        template_auto_reload: bool | None = None
        template_cache_size: int = 400
        app_config: AppConfig | None = None
        feature_enabled: bool = False
        identity_label: str | None = None

    def load_test_environment(**kwargs) -> Env:
        return Env(
            environ=dict(kwargs.get("environ") or {}),
            readenv=False,
            update=False,
        )

    def identity_values(env: Env) -> dict[str, str]:
        return values_from_env_settings(
            env,
            (EnvironmentSetting("IDENTITY_LABEL", "identity_label"),),
        )

    config_path = tmp_path / "app.toml"
    config_path.write_text(
        dedent(
            """
            [app]
            modules = ["wybra.web"]

            [app.templates]
            auto_reload = true
            cache_size = 0

            [app.assets]
            url_path = "/assets/"
            root = "static"
            """
        ),
        encoding="utf-8",
    )

    settings = load_composed_settings(
        FrameworkSettings,
        environment_loader=load_test_environment,
        env_settings=(
            EnvironmentSetting("APP_NAME", "app_name"),
            EnvironmentSetting("FEATURE_ENABLED", "feature_enabled", "bool"),
        ),
        extra_value_loaders=(identity_values,),
        environ={
            "APP_CONFIG": str(config_path),
            "APP_NAME": "composed-app",
            "FEATURE_ENABLED": "true",
            "IDENTITY_LABEL": "identity",
        },
        project_root=tmp_path,
        read_dotenv=False,
    )

    assert settings.project_root == tmp_path
    assert settings.app_name == "composed-app"
    assert settings.feature_enabled is True
    assert settings.identity_label == "identity"
    assert settings.app_config is not None
    assert settings.app_config.modules == ("wybra.web",)
    assert settings.static_url_path == "/assets/"
    assert settings.template_auto_reload is True
    assert settings.template_cache_size == 0


def test_composed_settings_loader_reports_invalid_typed_env_values() -> None:
    with pytest.raises(SettingsLoadError, match="FEATURE_ENABLED must be a boolean"):
        load_composed_settings(
            lambda **kwargs: kwargs,
            environment_loader=lambda **kwargs: Env(
                environ=dict(kwargs.get("environ") or {}),
                readenv=False,
                update=False,
            ),
            env_settings=(
                EnvironmentSetting("FEATURE_ENABLED", "feature_enabled", "bool"),
            ),
            environ={"FEATURE_ENABLED": "not-a-bool"},
            read_dotenv=False,
        )


def test_composed_settings_loader_can_require_app_config(tmp_path: Path) -> None:
    with pytest.raises(
        SettingsLoadError,
        match="Application config file could not be resolved",
    ):
        load_composed_settings(
            lambda **kwargs: kwargs,
            environment_loader=lambda **kwargs: Env(
                environ=dict(kwargs.get("environ") or {}),
                readenv=False,
                update=False,
            ),
            env_settings=(),
            environ={},
            project_root=tmp_path,
            read_dotenv=False,
            require_app_config=True,
        )


def test_module_surface_default_to_empty_optional_contributions() -> None:
    surface = ModuleSurface(module_name="host_app")

    assert surface.module_name == "host_app"
    assert surface.module_routers == {}


def test_module_surface_accepts_declared_contract_contributions() -> None:
    api_router = APIRouter()

    surface = ModuleSurface(
        module_name="wybra.auth",
        module_routers={"api": api_router},
    )

    assert surface.module_routers == {"api": api_router}


def test_discover_module_surface_treats_missing_optional_surfaces_as_empty(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package_root = tmp_path / "empty_surface_app"
    package_root.mkdir()
    (package_root / "__init__.py").write_text("", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()

    surface = discover_module_surface(
        "empty_surface_app",
        include_routes=True,
    )

    assert surface == ModuleSurface(module_name="empty_surface_app")


def test_discover_module_surfaces_preserves_configured_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for package_name in ("first_surface_app", "second_surface_app"):
        package_root = tmp_path / package_name
        package_root.mkdir()
        (package_root / "__init__.py").write_text("", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()

    surfaces = discover_module_surfaces(("first_surface_app", "second_surface_app"))

    assert tuple(surface.module_name for surface in surfaces) == (
        "first_surface_app",
        "second_surface_app",
    )


def test_discover_module_surface_reports_missing_configured_module() -> None:
    with pytest.raises(
        CompositionError,
        match="Configured module 'missing_surface_app' could not be imported",
    ):
        discover_module_surface("missing_surface_app")


def test_discover_module_routers_reads_module_routers_export(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package_root = tmp_path / "routes_surface_app"
    package_root.mkdir()
    (package_root / "__init__.py").write_text("", encoding="utf-8")
    (package_root / "routes.py").write_text(
        "from fastapi import APIRouter\n"
        "router = APIRouter()\n"
        "module_routers = {'default': router}\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()

    routers = discover_module_routers("routes_surface_app")

    assert tuple(routers) == ("default",)
    assert isinstance(routers["default"], APIRouter)


def test_discover_module_routers_accepts_mapping_static_collect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package_root = tmp_path / "routes_mapping_surface_app"
    package_root.mkdir()
    (package_root / "__init__.py").write_text("", encoding="utf-8")
    (package_root / "routes.py").write_text(
        "from fastapi import APIRouter\n"
        "from types import MappingProxyType\n"
        "router = APIRouter()\n"
        "module_routers = MappingProxyType({'default': router})\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()

    routers = discover_module_routers("routes_mapping_surface_app")

    assert tuple(routers) == ("default",)
    assert isinstance(routers["default"], APIRouter)


def test_load_module_routes_reads_configured_route_surfaces_in_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for package_name, route_name in (
        ("first_routes_app", "first:home"),
        ("second_routes_app", "second:home"),
    ):
        api_route_name = f"{route_name}:api"
        package_root = tmp_path / package_name
        package_root.mkdir()
        (package_root / "__init__.py").write_text("", encoding="utf-8")
        (package_root / "routes.py").write_text(
            dedent(
                f"""
            from fastapi import APIRouter

            router = APIRouter()
            api_router = APIRouter(prefix='/api')

            @router.get('/home', name={route_name!r})
            async def home():
                return 'home'

            @api_router.get('/{package_name}', name={api_route_name!r})
            async def api():
                return {{'ok': True}}

            module_routers = {{'pages': router, 'api': api_router}}
            """
            ),
            encoding="utf-8",
        )
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()

    routes = load_module_routes(
        ("first_routes_app", "second_routes_app"),
        route_prefixes={
            "first_routes_app": {"pages": "/first", "api": ""},
            "second_routes_app": {"pages": "/second", "api": ""},
        },
    )

    assert tuple(
        (route.module_name, route.label, route.prefix) for route in routes
    ) == (
        ("first_routes_app", "pages", "/first"),
        ("first_routes_app", "api", ""),
        ("second_routes_app", "pages", "/second"),
        ("second_routes_app", "api", ""),
    )
    assert tuple(
        route.name
        for configured_router in routes
        for route in configured_router.router.routes
    ) == (
        "first:home",
        "first:home:api",
        "second:home",
        "second:home:api",
    )


def test_configured_module_routes_and_registration_are_wybra_web_concerns(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module_name = "route_host_app"
    package_root = tmp_path / module_name
    package_root.mkdir()
    (package_root / "__init__.py").write_text("", encoding="utf-8")
    (package_root / "routes.py").write_text(
        dedent(
            """
            from fastapi import APIRouter
            from fastapi.responses import Response

            router = APIRouter()

            @router.get("/ping", name="public:ping")
            async def ping():
                return Response("pong")

            module_routers = {"default": router}
            """
        ),
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()

    class Settings:
        modules = (module_name,)
        app_config = AppConfig(
            config_path=Path("app.toml"),
            project_root=Path.cwd(),
            modules=modules,
            routes=RouteOptions(prefixes={module_name: {"default": "/site"}}),
            templates=TemplateOptions(auto_reload=True, cache_size=0),
            assets=AssetOptions(url_path="/static/"),
        )

    route_set = load_configured_module_routes(Settings())
    app = FastAPI()

    register_module_routes(app, route_set)

    assert route_prefixes_from_app_config(Settings.app_config) == {
        module_name: {"default": "/site"}
    }
    assert TestClient(app).get("/site/ping").text == "pong"


def test_register_module_routes_applies_labelled_prefixes() -> None:
    router = APIRouter()

    @router.get("/dashboard", name="admin:dashboard")
    async def dashboard() -> Response:
        return Response("dashboard")

    app = FastAPI()
    register_module_routes(
        app,
        (
            ConfiguredModuleRouter(
                module_name="admin",
                label="pages",
                router=router,
                prefix="/admin",
            ),
        ),
    )

    assert TestClient(app).get("/admin/dashboard").text == "dashboard"


def test_module_router_can_bypass_module_prefix_with_separate_label() -> None:
    account_router = APIRouter()
    callback_router = APIRouter()

    @account_router.get("/login", name="auth:login")
    async def login() -> Response:
        return Response("login")

    @callback_router.get("/oauth/callback", name="auth:oauth-callback")
    async def callback() -> Response:
        return Response("callback")

    app = FastAPI()
    register_module_routes(
        app,
        (
            ConfiguredModuleRouter(
                module_name="auth",
                label="account",
                router=account_router,
                prefix="/account",
            ),
            ConfiguredModuleRouter(
                module_name="auth",
                label="callbacks",
                router=callback_router,
                prefix="",
            ),
        ),
    )

    client = TestClient(app)
    assert client.get("/account/login").text == "login"
    assert client.get("/oauth/callback").text == "callback"
    assert client.get("/account/oauth/callback").status_code == 404


def test_register_module_routes_allows_route_name_conflicts() -> None:
    first_router = APIRouter()
    second_router = APIRouter()

    @first_router.get("/first", name="shared:home")
    async def first() -> Response:
        return Response()

    @second_router.get("/second", name="shared:home")
    async def second() -> Response:
        return Response("second")

    app = FastAPI()
    register_module_routes(
        app,
        (
            ConfiguredModuleRouter(
                module_name="first",
                label="pages",
                router=first_router,
                prefix="",
            ),
            ConfiguredModuleRouter(
                module_name="second",
                label="pages",
                router=second_router,
                prefix="",
            ),
        ),
    )

    assert TestClient(app).get("/second").text == "second"


def test_register_module_routes_skips_later_method_path_conflicts(
    caplog: pytest.LogCaptureFixture,
) -> None:
    first_router = APIRouter()
    second_router = APIRouter()

    @first_router.get("/shared", name="first:shared")
    async def first() -> Response:
        return Response("first")

    @second_router.get("/shared", name="second:shared")
    async def second() -> Response:
        return Response("second")

    app = FastAPI()
    with caplog.at_level(
        logging.WARNING,
        logger="wybra.web.routes.registration",
    ):
        register_module_routes(
            app,
            (
                ConfiguredModuleRouter(
                    module_name="first",
                    label="pages",
                    router=first_router,
                    prefix="",
                ),
                ConfiguredModuleRouter(
                    module_name="second",
                    label="pages",
                    router=second_router,
                    prefix="",
                ),
            ),
        )

    assert TestClient(app).get("/shared").text == "first"
    assert any(
        record.message == "Skipping duplicate configured route."
        and record.route_module == "second"
        and record.route_router == "pages"
        and record.route_method == "GET"
        and record.route_path == "/shared"
        and record.winning_route_module == "first"
        and record.winning_route_router == "pages"
        for record in caplog.records
    )


def test_register_module_routes_existing_app_route_wins_on_conflict(
    caplog: pytest.LogCaptureFixture,
) -> None:
    app = FastAPI()

    @app.get("/shared")
    async def app_shared() -> Response:
        return Response("app")

    router = APIRouter()

    @router.get("/shared", name="module:shared")
    async def module_shared() -> Response:
        return Response("module")

    with caplog.at_level(
        logging.WARNING,
        logger="wybra.web.routes.registration",
    ):
        register_module_routes(
            app,
            (
                ConfiguredModuleRouter(
                    module_name="module",
                    label="pages",
                    router=router,
                    prefix="",
                ),
            ),
        )

    assert TestClient(app).get("/shared").text == "app"
    assert any(
        record.message == "Skipping duplicate configured route."
        and record.route_module == "module"
        and record.route_router == "pages"
        and record.route_method == "GET"
        and record.route_path == "/shared"
        and record.winning_route_module == "app"
        and record.winning_route_router == "existing"
        for record in caplog.records
    )


def test_register_module_routes_rejects_partial_method_path_conflicts() -> None:
    app = FastAPI()

    @app.get("/shared")
    async def app_shared() -> Response:
        return Response("app")

    router = APIRouter()

    @router.api_route("/shared", methods=["GET", "POST"], name="module:shared")
    async def module_shared() -> Response:
        return Response("module")

    with pytest.raises(
        RouteCompositionError,
        match="partial method/path conflict.*GET",
    ):
        register_module_routes(
            app,
            (
                ConfiguredModuleRouter(
                    module_name="module",
                    label="pages",
                    router=router,
                    prefix="",
                ),
            ),
        )


def test_register_module_routes_rejects_non_string_http_methods() -> None:
    router = APIRouter()

    @router.get("/bad-method", name="bad:method")
    async def bad_method() -> Response:
        return Response()

    route = router.routes[0]
    assert isinstance(route, APIRoute)
    route.methods.add(123)  # type: ignore[arg-type]

    with pytest.raises(
        RouteCompositionError,
        match=r"invalid HTTP method 123; methods must be strings",
    ):
        register_module_routes(
            FastAPI(),
            (
                ConfiguredModuleRouter(
                    module_name="bad",
                    label="pages",
                    router=router,
                    prefix="",
                ),
            ),
        )


def test_load_module_routes_omits_unpublished_router_labels(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package_root = tmp_path / "unpublished_route_app"
    package_root.mkdir()
    (package_root / "__init__.py").write_text("", encoding="utf-8")
    (package_root / "routes.py").write_text(
        dedent(
            """
            from fastapi import APIRouter

            public_router = APIRouter()
            admin_router = APIRouter()

            @public_router.get("/public", name="app:public")
            async def public():
                pass

            @admin_router.get("/admin", name="app:admin")
            async def admin():
                pass

            module_routers = {
                "public": public_router,
                "admin": admin_router,
            }
            """
        ),
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()

    routes = load_module_routes(
        ("unpublished_route_app",),
        route_prefixes={"unpublished_route_app": {"public": ""}},
    )

    assert tuple(route.label for route in routes) == ("public",)


def test_load_module_routes_omits_modules_without_route_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package_root = tmp_path / "unpublished_module_route_app"
    package_root.mkdir()
    (package_root / "__init__.py").write_text("", encoding="utf-8")
    (package_root / "routes.py").write_text(
        dedent(
            """
            from fastapi import APIRouter

            router = APIRouter()

            @router.get("/admin", name="app:admin")
            async def admin():
                pass

            module_routers = {"admin": router}
            """
        ),
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()

    assert (
        load_module_routes(
            ("unpublished_module_route_app",),
            route_prefixes={},
        )
        == ()
    )


def test_load_module_routes_rejects_unknown_router_prefix_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package_root = tmp_path / "unknown_prefix_route_app"
    package_root.mkdir()
    (package_root / "__init__.py").write_text("", encoding="utf-8")
    (package_root / "routes.py").write_text(
        "from fastapi import APIRouter\n"
        "router = APIRouter()\n"
        "module_routers = {'pages': router}\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()

    with pytest.raises(
        RouteCompositionError,
        match="unknown router label 'admin'",
    ):
        load_module_routes(
            ("unknown_prefix_route_app",),
            route_prefixes={"unknown_prefix_route_app": {"admin": ""}},
        )


def test_load_module_routes_without_route_prefixes_publishes_all_surfaces(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package_root = tmp_path / "default_prefix_route_app"
    package_root.mkdir()
    (package_root / "__init__.py").write_text("", encoding="utf-8")
    (package_root / "routes.py").write_text(
        dedent(
            """
            from fastapi import APIRouter

            router = APIRouter()

            @router.get('/home', name='default:home')
            async def home():
                return 'home'

            module_routers = {'pages': router}
            """
        ),
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()

    routes = load_module_routes(
        ("default_prefix_route_app",),
    )

    assert tuple(route.prefix for route in routes) == ("",)


def test_load_module_routes_rejects_empty_router_surface(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package_root = tmp_path / "empty_router_surface_app"
    package_root.mkdir()
    (package_root / "__init__.py").write_text("", encoding="utf-8")
    (package_root / "routes.py").write_text(
        "from fastapi import APIRouter\n"
        "router = APIRouter()\n"
        "module_routers = {'pages': router}\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()

    with pytest.raises(
        RouteCompositionError,
        match="did not register any routes.*decorated handler modules",
    ):
        load_module_routes(
            ("empty_router_surface_app",),
            route_prefixes={"empty_router_surface_app": {"pages": ""}},
        )


def test_load_module_routes_rejects_invalid_include_prefixes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package_root = tmp_path / "invalid_prefix_route_app"
    package_root.mkdir()
    (package_root / "__init__.py").write_text("", encoding="utf-8")
    (package_root / "routes.py").write_text(
        "from fastapi import APIRouter\n"
        "router = APIRouter()\n"
        "module_routers = {'pages': router}\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()

    with pytest.raises(RouteCompositionError, match="must start with '/'"):
        load_module_routes(
            ("invalid_prefix_route_app",),
            route_prefixes={"invalid_prefix_route_app": {"pages": "bad"}},
        )


def test_load_module_routes_normalises_trailing_include_prefix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package_root = tmp_path / "trailing_prefix_route_app"
    package_root.mkdir()
    (package_root / "__init__.py").write_text("", encoding="utf-8")
    (package_root / "routes.py").write_text(
        dedent(
            """
            from fastapi import APIRouter

            router = APIRouter()

            @router.get('/home', name='trailing:home')
            async def home():
                return 'home'

            module_routers = {'pages': router}
            """
        ),
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()

    routes = load_module_routes(
        ("trailing_prefix_route_app",),
        route_prefixes={"trailing_prefix_route_app": {"pages": "/account/"}},
    )

    assert tuple(route.prefix for route in routes) == ("/account",)


def test_discover_module_routers_rejects_malformed_present_surface(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package_root = tmp_path / "bad_routes_surface_app"
    package_root.mkdir()
    (package_root / "__init__.py").write_text("", encoding="utf-8")
    (package_root / "routes.py").write_text(
        "module_routers = object()\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()

    with pytest.raises(
        CompositionError,
        match="bad_routes_surface_app.routes.*module_routers",
    ):
        discover_module_routers("bad_routes_surface_app")


def test_discover_module_routers_rejects_blank_router_label(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package_root = tmp_path / "blank_label_app"
    package_root.mkdir()
    (package_root / "__init__.py").write_text("", encoding="utf-8")
    (package_root / "routes.py").write_text(
        "from fastapi import APIRouter\nmodule_routers = {' ': APIRouter()}\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()

    with pytest.raises(CompositionError, match="non-blank string router labels"):
        discover_module_routers("blank_label_app")


def test_discover_module_routers_rejects_non_apirouter_value(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package_root = tmp_path / "non_apirouter_value_app"
    package_root.mkdir()
    (package_root / "__init__.py").write_text("", encoding="utf-8")
    (package_root / "routes.py").write_text(
        "module_routers = {'default': object()}\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()

    with pytest.raises(CompositionError, match="fastapi.APIRouter"):
        discover_module_routers("non_apirouter_value_app")


def test_discover_package_resource_sources_without_route_import(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package_root = tmp_path / "resource_surface_app"
    package_root.mkdir()
    (package_root / "__init__.py").write_text("", encoding="utf-8")
    (package_root / "templates").mkdir()
    (package_root / "static").mkdir()
    (package_root / "routes.py").write_text(
        "raise RuntimeError('route surface should not be imported')\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()

    surface = discover_module_surface("resource_surface_app")

    assert surface.module_routers == {}
    assert discover_template_sources("resource_surface_app") == (
        PackageResourceSource(package="resource_surface_app", directory="templates"),
    )
    assert discover_static_sources("resource_surface_app") == (
        PackageResourceSource(package="resource_surface_app", directory="static"),
    )


def test_template_sources_from_modules_use_configured_module_precedence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for package_name, content in (
        ("override_template_app", "override"),
        ("base_template_app", "base"),
    ):
        package_root = tmp_path / package_name
        template_root = package_root / "templates"
        template_root.mkdir(parents=True)
        (package_root / "__init__.py").write_text("", encoding="utf-8")
        (template_root / "shared.html").write_text(content, encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()

    sources = template_sources_from_modules(
        ("override_template_app", "base_template_app")
    )
    environment = Environment(
        loader=build_template_loader(template_sources=sources),
        autoescape=select_autoescape(("html", "xml")),
    )

    assert sources == (
        PackageResourceSource(package="override_template_app", directory="templates"),
        PackageResourceSource(package="base_template_app", directory="templates"),
    )
    assert read_text_resource(sources, "shared.html") == "override"
    assert environment.get_template("shared.html").render() == "override"


def test_static_sources_from_modules_use_configured_module_precedence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for package_name, content in (
        ("override_static_app", "body { color: red; }"),
        ("base_static_app", "body { color: black; }"),
    ):
        package_root = tmp_path / package_name
        static_root = package_root / "static"
        static_root.mkdir(parents=True)
        (package_root / "__init__.py").write_text("", encoding="utf-8")
        (static_root / "styles.css").write_text(content, encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()

    sources = static_sources_from_modules(("override_static_app", "base_static_app"))
    response = static_asset_response(sources, "styles.css")

    assert sources == (
        PackageResourceSource(package="override_static_app", directory="static"),
        PackageResourceSource(package="base_static_app", directory="static"),
    )
    assert response.status_code == 200
    assert response.body == b"body { color: red; }"
    assert response.media_type == "text/css"


def test_static_response_uses_plain_not_found_for_missing_assets() -> None:
    response = static_asset_response(
        (PackageResourceSource(package="wybra.web", directory="static"),),
        "missing.css",
    )

    assert response.status_code == 404
    assert response.body == b"Not Found"
    assert response.media_type == "text/plain"


def test_composed_static_files_rejects_empty_sources() -> None:
    with pytest.raises(InputValidationError, match="At least one static source"):
        ComposedStaticFiles(())


def test_normal_static_asset_storage_resolves_logical_path_to_static_url() -> None:
    assert asset_url("styles/app.css", url_path="/static/") == "/static/styles/app.css"
    assert asset_url("styles/app.css", url_path="assets") == "/assets/styles/app.css"


def test_asset_url_normalises_dot_segments_in_logical_path() -> None:
    assert (
        asset_url("./styles/./app.css", url_path="/static/") == "/static/styles/app.css"
    )


def test_asset_url_rejects_absolute_logical_paths() -> None:
    with pytest.raises(InputValidationError) as excinfo:
        asset_url("/styles/app.css", url_path="/static/")

    assert str(excinfo.value) == (
        "Logical asset paths must be relative and cannot start with '/'."
    )


@pytest.mark.parametrize(
    "logical_path",
    (" styles/app.css", "styles/app.css "),
)
def test_asset_url_rejects_logical_paths_with_surrounding_whitespace(
    logical_path: str,
) -> None:
    with pytest.raises(InputValidationError) as excinfo:
        asset_url(logical_path, url_path="/static/")

    assert str(excinfo.value) == (
        "Logical asset paths must not have leading or trailing whitespace."
    )


@pytest.mark.parametrize(
    "logical_path",
    ("../styles/app.css", "./../styles/app.css"),
)
def test_asset_url_rejects_parent_directory_logical_paths(
    logical_path: str,
) -> None:
    with pytest.raises(InputValidationError) as excinfo:
        asset_url(logical_path, url_path="/static/")

    assert str(excinfo.value) == (
        "Logical asset paths must not contain '..' segments that escape the namespace."
    )


@pytest.mark.anyio
async def test_asset_url_is_available_in_template_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package_root = tmp_path / "asset_url_template_app"
    template_root = package_root / "templates"
    static_root = package_root / "static" / "styles"
    template_root.mkdir(parents=True)
    static_root.mkdir(parents=True)
    (package_root / "__init__.py").write_text("", encoding="utf-8")
    (template_root / "page.html").write_text(
        '<link href="{{ asset_url("styles/app.css") }}" rel="stylesheet">',
        encoding="utf-8",
    )
    (static_root / "app.css").write_text("body {}", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()
    app = FastAPI()

    await start(
        app,
        config_source=MappingConfigSource(
            {
                "app": {
                    "config_path": tmp_path / "app.toml",
                    "project_root": tmp_path,
                    "modules": (
                        "asset_url_template_app",
                        "wybra.assets",
                        "wybra.template",
                        "wybra.web",
                    ),
                },
                "app.routes": {
                    "prefixes": {
                        "asset_url_template_app": {"default": ""},
                        "wybra.web": {},
                    }
                },
                "app.templates": {"auto_reload": True, "cache_size": 0},
                "app.assets": {"url_path": "/assets/"},
            }
        ),
    )

    @app.get("/")
    async def home(request: Request) -> Response:
        return render_page(request, "page.html")

    assert TestClient(app).get("/").text == (
        '<link href="/assets/styles/app.css" rel="stylesheet">'
    )


@pytest.mark.anyio
async def test_template_context_does_not_expose_static_mount_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package_root = tmp_path / "no_static_mount_context_app"
    template_root = package_root / "templates"
    template_root.mkdir(parents=True)
    (package_root / "__init__.py").write_text("", encoding="utf-8")
    (template_root / "page.html").write_text(
        "{{ static_mount_path is defined }}",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()
    app = FastAPI()

    await start(
        app,
        config_source=MappingConfigSource(
            {
                "app": {
                    "config_path": tmp_path / "app.toml",
                    "project_root": tmp_path,
                    "modules": (
                        "no_static_mount_context_app",
                        "wybra.assets",
                        "wybra.template",
                        "wybra.web",
                    ),
                },
                "app.routes": {
                    "prefixes": {
                        "no_static_mount_context_app": {"default": ""},
                        "wybra.web": {},
                    }
                },
                "app.templates": {"auto_reload": True, "cache_size": 0},
            }
        ),
    )

    @app.get("/")
    async def home(request: Request) -> Response:
        return render_page(request, "page.html")

    assert TestClient(app).get("/").text == "False"


def test_template_rendering_requires_site_for_render_page() -> None:
    class AppWithoutState:
        pass

    request = Request({"type": "http", "app": AppWithoutState()})
    with pytest.raises(
        SiteCapabilityError,
        match="Missing template capability provider",
    ):
        render_page(request, "page.html")


def test_template_renderer_requires_static_asset_capability() -> None:
    site = Site(
        app=FastAPI(),
        config=ConfigService([MappingConfigSource({"app": {"modules": ()}})]),
    )
    site.app.state.site = site
    renderer = DefaultTemplateCapability(
        assets=site.capability_proxy(StaticAssetCapability),
    )
    asset_url_helper = renderer._resolve_asset_url()

    with pytest.raises(SiteCapabilityError, match="Missing static asset capability"):
        asset_url_helper("styles/main.css")


def test_template_asset_url_helper_resolves_capability_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    site = Site(
        app=FastAPI(),
        config=ConfigService([MappingConfigSource({"app": {"modules": ()}})]),
    )
    site.app.state.site = site

    @dataclass(frozen=True, slots=True)
    class CountingStaticAssetCapability:
        url_path: str = "/assets"
        root: Path = Path("static")
        export_mode: AssetExportMode = AssetExportMode.NORMAL
        serve: bool = True
        sources: tuple[PackageResourceSource, ...] = ()

        def url(self, logical_path: str) -> str:
            return f"{self.url_path}/{logical_path}"

    capability = CountingStaticAssetCapability()
    site.provide_capability(StaticAssetCapability, capability)
    renderer = DefaultTemplateCapability(
        assets=site.capability_proxy(StaticAssetCapability),
    )
    require_calls = 0
    original_require = template_capabilities_module.require_static_asset_capability

    def count_require(proxy):
        nonlocal require_calls
        require_calls += 1
        return original_require(proxy)

    monkeypatch.setattr(
        template_capabilities_module,
        "require_static_asset_capability",
        count_require,
    )
    asset_url_helper = renderer._resolve_asset_url()

    assert asset_url_helper("styles/one.css") == "/assets/styles/one.css"
    assert asset_url_helper("styles/two.css") == "/assets/styles/two.css"
    assert require_calls == 1


def test_optional_static_mount_path_normalises_capability_url_path() -> None:
    site = Site(
        app=FastAPI(),
        config=ConfigService([MappingConfigSource({"app": {"modules": ()}})]),
    )

    @dataclass(frozen=True, slots=True)
    class UnnormalisedStaticAssetCapability:
        url_path: str = "static//"
        root: Path = Path("static")
        export_mode: AssetExportMode = AssetExportMode.NORMAL
        serve: bool = True
        sources: tuple[PackageResourceSource, ...] = ()

        def url(self, logical_path: str) -> str:
            return f"/static/{logical_path}"

    site.provide_capability(StaticAssetCapability, UnnormalisedStaticAssetCapability())

    assert (
        web_module._optional_static_mount_path(
            site.capability_proxy(StaticAssetCapability)
        )
        == "/static"
    )


def test_wybra_owned_templates_use_asset_url_for_static_references() -> None:
    template_paths = (
        Path(__file__).resolve().parents[1]
        / "src/wybra/template/templates/layouts/page.html",
        Path(__file__).resolve().parents[1]
        / "src/wybra/template/templates/errors/base.html",
        Path(__file__).resolve().parents[1]
        / "src/wybra/widgets/templates/layouts/page.html",
    )

    for template_path in template_paths:
        template = template_path.read_text(encoding="utf-8")
        assert "asset_url(" in template
        assert "static_mount_path }}/" not in template


def test_static_collect_writes_winning_assets_and_reports_duplicates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for package_name, content in (
        ("base_collect_static_app", "base"),
        ("application_collect_static_app", "application"),
    ):
        package_root = tmp_path / package_name
        static_root = package_root / "static" / "styles"
        static_root.mkdir(parents=True)
        (package_root / "__init__.py").write_text("", encoding="utf-8")
        (static_root / "app.css").write_text(content, encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()

    sources = static_sources_from_modules(
        ("application_collect_static_app", "base_collect_static_app")
    )
    result = collect_static_assets(sources, root=tmp_path / "collected")

    collected_asset = tmp_path / "collected" / "styles" / "app.css"
    assert collected_asset.read_text(encoding="utf-8") == "application"
    assert tuple(asset.logical_path for asset in result.collected_assets) == (
        "styles/app.css",
    )
    assert result.duplicates == (
        StaticAssetDuplicate(
            logical_path="styles/app.css",
            winner=PackageResourceSource(
                package="application_collect_static_app",
                directory="static",
            ),
            shadowed=PackageResourceSource(
                package="base_collect_static_app",
                directory="static",
            ),
        ),
    )


def test_static_collect_skips_reserved_manifest_asset(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package_root = tmp_path / "manifest_collision_collect_app"
    static_root = package_root / "static"
    static_root.mkdir(parents=True)
    (package_root / "__init__.py").write_text("", encoding="utf-8")
    (static_root / ".wybra-static-collect.json").write_text(
        "not a collect manifest",
        encoding="utf-8",
    )
    (static_root / "styles.css").write_text("body {}", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()

    result = collect_static_assets(
        static_sources_from_modules(("manifest_collision_collect_app",)),
        root=tmp_path / "collected",
    )

    manifest = tmp_path / "collected" / ".wybra-static-collect.json"
    assert '"assets": [' in manifest.read_text(encoding="utf-8")
    assert tuple(asset.logical_path for asset in result.collected_assets) == (
        "styles.css",
    )
    assert tuple(asset.logical_path for asset in result.skipped_assets) == (
        ".wybra-static-collect.json",
    )
    assert tuple(asset.reason for asset in result.skipped_assets) == ("reserved",)


@pytest.mark.parametrize(
    ("manifest_assets", "expected_message"),
    (
        ("[42]", "contains a non-text path at index 0: 42"),
        (
            '["../styles/app.css"]',
            "contains an invalid path at index 0: '../styles/app.css'",
        ),
    ),
)
def test_static_collect_reports_invalid_manifest_entry(
    tmp_path: Path,
    manifest_assets: str,
    expected_message: str,
) -> None:
    collected_root = tmp_path / "collected"
    collected_root.mkdir()
    (collected_root / ".wybra-static-collect.json").write_text(
        f'{{"version": 1, "assets": {manifest_assets}}}',
        encoding="utf-8",
    )

    with pytest.raises(StaticCollectionError) as excinfo:
        collect_static_assets((), root=collected_root)

    assert expected_message in str(excinfo.value)


def test_configured_static_collect_uses_app_toml_without_route_import(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package_root = tmp_path / "configured_collect_static_app"
    static_root = package_root / "static" / "styles"
    static_root.mkdir(parents=True)
    (package_root / "__init__.py").write_text("", encoding="utf-8")
    (package_root / "routes.py").write_text(
        "raise RuntimeError('route surface should not be imported')\n",
        encoding="utf-8",
    )
    (static_root / "app.css").write_text(":root {}", encoding="utf-8")
    config_path = tmp_path / "app.toml"
    config_path.write_text(
        """
        [app]
        modules = ["configured_collect_static_app"]

        [app.templates]
        auto_reload = true
        cache_size = 0

        [app.assets]
        url_path = "/static/"
        root = "collected-static"
        """,
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()

    result = collect_configured_static_assets(
        project_root=tmp_path,
        config_path=config_path,
    )

    collected_asset = tmp_path / "collected-static" / "styles" / "app.css"
    assert result.root == (tmp_path / "collected-static").resolve()
    assert collected_asset.read_text(encoding="utf-8") == ":root {}"
    assert result.duplicates == ()


def test_static_collect_skips_unchanged_assets_and_updates_changed_assets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package_root = tmp_path / "incremental_collect_app"
    static_root = package_root / "static" / "styles"
    static_root.mkdir(parents=True)
    (package_root / "__init__.py").write_text("", encoding="utf-8")
    source_asset = static_root / "app.css"
    source_asset.write_text("body {}", encoding="utf-8")
    source_mtime = 1_700_000_000
    os.utime(source_asset, (source_mtime, source_mtime))
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()
    sources = static_sources_from_modules(("incremental_collect_app",))

    first_result = collect_static_assets(sources, root=tmp_path / "collected")
    destination = tmp_path / "collected" / "styles" / "app.css"
    manifest = tmp_path / "collected" / ".wybra-static-collect.json"
    first_destination_mtime = destination.stat().st_mtime_ns
    first_manifest_mtime = manifest.stat().st_mtime_ns

    second_result = collect_static_assets(sources, root=tmp_path / "collected")

    assert tuple(asset.status for asset in first_result.collected_assets) == (
        StaticCollectionStatus.COPIED,
    )
    assert tuple(asset.status for asset in second_result.collected_assets) == (
        StaticCollectionStatus.UNCHANGED,
    )
    assert destination.stat().st_mtime_ns == first_destination_mtime
    assert manifest.stat().st_mtime_ns == first_manifest_mtime

    source_asset.write_text("body { color: red; }", encoding="utf-8")
    updated_mtime = source_mtime + 60
    os.utime(source_asset, (updated_mtime, updated_mtime))

    third_result = collect_static_assets(sources, root=tmp_path / "collected")

    assert destination.read_text(encoding="utf-8") == "body { color: red; }"
    assert tuple(asset.status for asset in third_result.collected_assets) == (
        StaticCollectionStatus.UPDATED,
    )
    assert int(destination.stat().st_mtime) == updated_mtime


def test_static_collect_updates_same_device_same_metadata_different_content(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_mtime = 1_700_000_000
    for package_name, content in (
        ("metadata_collect_app_a", "body{a}"),
        ("metadata_collect_app_b", "body{b}"),
    ):
        package_root = tmp_path / package_name
        static_root = package_root / "static" / "styles"
        static_root.mkdir(parents=True)
        (package_root / "__init__.py").write_text("", encoding="utf-8")
        source_asset = static_root / "app.css"
        source_asset.write_text(content, encoding="utf-8")
        os.utime(source_asset, (source_mtime, source_mtime))
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()
    destination_root = tmp_path / "collected"

    collect_static_assets(
        static_sources_from_modules(("metadata_collect_app_a",)),
        root=destination_root,
    )
    destination = destination_root / "styles" / "app.css"

    result = collect_static_assets(
        static_sources_from_modules(("metadata_collect_app_b",)),
        root=destination_root,
    )

    assert destination.read_text(encoding="utf-8") == "body{b}"
    assert tuple(asset.status for asset in result.collected_assets) == (
        StaticCollectionStatus.UPDATED,
    )


def test_static_collect_deletes_stale_assets_by_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package_root = tmp_path / "stale_collect_app"
    static_root = package_root / "static" / "styles"
    static_root.mkdir(parents=True)
    (package_root / "__init__.py").write_text("", encoding="utf-8")
    (static_root / "app.css").write_text("body {}", encoding="utf-8")
    stale_source = static_root / "old.css"
    stale_source.write_text("old", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()
    destination_root = tmp_path / "collected"

    collect_static_assets(
        static_sources_from_modules(("stale_collect_app",)),
        root=destination_root,
    )
    stale_source.unlink()
    stale_asset = destination_root / "styles" / "old.css"
    unmanaged_asset = destination_root / "uploads" / "user.txt"
    unmanaged_asset.parent.mkdir(parents=True)
    unmanaged_asset.write_text("user data", encoding="utf-8")
    result = collect_static_assets(
        static_sources_from_modules(("stale_collect_app",)),
        root=destination_root,
    )

    assert not stale_asset.exists()
    assert unmanaged_asset.exists()
    assert tuple(asset.logical_path for asset in result.deleted_assets) == (
        "styles/old.css",
    )


def test_static_collect_can_leave_stale_assets_in_place(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package_root = tmp_path / "no_delete_collect_app"
    static_root = package_root / "static" / "styles"
    static_root.mkdir(parents=True)
    (package_root / "__init__.py").write_text("", encoding="utf-8")
    (static_root / "app.css").write_text("body {}", encoding="utf-8")
    stale_source = static_root / "old.css"
    stale_source.write_text("old", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()
    destination_root = tmp_path / "collected"

    collect_static_assets(
        static_sources_from_modules(("no_delete_collect_app",)),
        root=destination_root,
    )
    stale_source.unlink()
    stale_asset = destination_root / "styles" / "old.css"
    result = collect_static_assets(
        static_sources_from_modules(("no_delete_collect_app",)),
        root=destination_root,
        delete=False,
    )

    assert stale_asset.exists()
    assert result.deleted_assets == ()


def test_static_collect_command_config_overrides_ambient_app_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_static_collect_app(tmp_path, "ambient_collect_app", "ambient")
    _write_static_collect_app(tmp_path, "selected_collect_app", "selected")
    ambient_config = _write_static_collect_config(
        tmp_path,
        "ambient.toml",
        module_name="ambient_collect_app",
        root="ambient-assets",
    )
    selected_config = _write_static_collect_config(
        tmp_path,
        "selected.toml",
        module_name="selected_collect_app",
        root="selected-assets",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setenv("APP_CONFIG", ambient_config.as_posix())
    monkeypatch.setattr(collect_tool, "runtime_project_root", lambda: tmp_path)
    importlib.invalidate_caches()

    exit_code = collect_tool.main(["--config", selected_config.as_posix()])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "static collection: ok" in captured.out
    assert (tmp_path / "selected-assets" / "styles" / "app.css").read_text(
        encoding="utf-8"
    ) == "selected"
    assert not (tmp_path / "ambient-assets" / "styles" / "app.css").exists()


def test_static_collect_command_dest_overrides_app_assets_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_static_collect_app(tmp_path, "dest_collect_app", "body {}")
    config_path = _write_static_collect_config(
        tmp_path,
        "app.toml",
        module_name="dest_collect_app",
        root="configured-assets",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(collect_tool, "runtime_project_root", lambda: tmp_path)
    importlib.invalidate_caches()

    exit_code = collect_tool.main(
        [
            "--config",
            config_path.as_posix(),
            "--dest",
            "override-assets",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert f"- root: {(tmp_path / 'override-assets').resolve()}" in captured.out
    assert (tmp_path / "override-assets" / "styles" / "app.css").read_text(
        encoding="utf-8"
    ) == "body {}"
    assert not (tmp_path / "configured-assets" / "styles" / "app.css").exists()


def test_static_collect_command_no_delete_preserves_stale_assets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_static_collect_app(tmp_path, "cli_no_delete_collect_app", "body {}")
    config_path = _write_static_collect_config(
        tmp_path,
        "app.toml",
        module_name="cli_no_delete_collect_app",
        root="collected-assets",
    )
    stale_asset = tmp_path / "collected-assets" / "styles" / "old.css"
    stale_asset.parent.mkdir(parents=True)
    stale_asset.write_text("old", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(collect_tool, "runtime_project_root", lambda: tmp_path)
    importlib.invalidate_caches()

    exit_code = collect_tool.main(["--config", config_path.as_posix(), "--no-delete"])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "- deleted: 0" in captured.out
    assert stale_asset.exists()


def test_static_collect_command_rejects_unimplemented_manifest_export_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_static_collect_app(tmp_path, "manifest_collect_app", "body {}")
    config_path = _write_static_collect_config(
        tmp_path,
        "app.toml",
        module_name="manifest_collect_app",
        root="collected-assets",
        export_mode="manifest",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(collect_tool, "runtime_project_root", lambda: tmp_path)
    importlib.invalidate_caches()

    exit_code = collect_tool.main(["--config", config_path.as_posix()])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "configuration: failed" in captured.err
    assert "app.assets.export_mode must be one of: 'normal'" in captured.err


def test_static_collect_command_reports_destination_creation_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_static_collect_app(tmp_path, "root_failure_collect_app", "body {}")
    config_path = _write_static_collect_config(
        tmp_path,
        "app.toml",
        module_name="root_failure_collect_app",
        root="collected-assets",
    )
    blocked_parent = tmp_path / "not-a-directory"
    blocked_parent.write_text("file", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(collect_tool, "runtime_project_root", lambda: tmp_path)
    importlib.invalidate_caches()

    exit_code = collect_tool.main(
        [
            "--config",
            config_path.as_posix(),
            "--dest",
            (blocked_parent / "static").as_posix(),
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "static collection: failed" in captured.err
    assert "create failed for static collection root" in captured.err


def test_static_collect_main_handles_click_abort_without_traceback(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def aborting_main(**_kwargs: object) -> int:
        raise click.exceptions.Abort()

    monkeypatch.setattr(collect_tool.collect_command, "main", aborting_main)

    exit_code = collect_tool.main(["--config", "app.toml"])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert captured.err == "Aborted!\n"


def _write_static_collect_app(tmp_path: Path, package_name: str, content: str) -> None:
    package_root = tmp_path / package_name
    static_root = package_root / "static" / "styles"
    static_root.mkdir(parents=True)
    (package_root / "__init__.py").write_text("", encoding="utf-8")
    (static_root / "app.css").write_text(content, encoding="utf-8")


def _write_static_collect_config(
    tmp_path: Path,
    file_name: str,
    *,
    module_name: str,
    root: str,
    export_mode: str = "normal",
) -> Path:
    config_path = tmp_path / file_name
    config_path.write_text(
        dedent(
            f"""
            [app]
            modules = ["{module_name}"]

            [app.templates]
            auto_reload = true
            cache_size = 0

            [app.assets]
            url_path = "/static/"
            root = "{root}"
            export_mode = "{export_mode}"
            """
        ),
        encoding="utf-8",
    )
    return config_path


def test_static_collect_reports_asset_path_for_copy_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package_root = tmp_path / "failed_collect_app"
    static_root = package_root / "static" / "styles"
    static_root.mkdir(parents=True)
    (package_root / "__init__.py").write_text("", encoding="utf-8")
    (static_root / "app.css").write_text("body {}", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()
    destination_root = tmp_path / "collected"
    destination_root.mkdir()
    (destination_root / "styles").write_text("not a directory", encoding="utf-8")

    with pytest.raises(
        StaticCollectionError,
        match="copy failed for static asset 'styles/app.css'",
    ):
        collect_static_assets(
            static_sources_from_modules(("failed_collect_app",)),
            root=destination_root,
        )


def test_composed_template_loader_uses_standard_missing_template_behaviour(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package_root = tmp_path / "template_missing_app"
    (package_root / "templates").mkdir(parents=True)
    (package_root / "__init__.py").write_text("", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()

    sources = template_sources_from_modules(("template_missing_app",))
    environment = Environment(
        loader=build_template_loader(template_sources=sources),
        autoescape=select_autoescape(("html", "xml")),
    )

    with pytest.raises(TemplateNotFound, match="missing.html"):
        environment.get_template("missing.html")


def test_discover_context_providers_imports_context_registration_module(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package_root = tmp_path / "context_surface_app"
    package_root.mkdir()
    (package_root / "__init__.py").write_text("", encoding="utf-8")
    (package_root / "context.py").write_text(
        "from wybra.template.context import add_to_context\n"
        "\n"
        "def site_context(request, context):\n"
        "    del request\n"
        "    return context.with_values(site_name='Test app')\n"
        "\n"
        "add_to_context({'app_mode': 'test'})\n"
        "add_to_context(site_context)\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()

    providers = discover_context_providers("context_surface_app")

    assert len(providers) == 2
    assert providers[0](object(), TemplateContext()).as_dict() == {"app_mode": "test"}
    assert providers[1](object(), TemplateContext()).as_dict() == {
        "site_name": "Test app"
    }


def test_add_to_context_registers_callable_provider_module_through_wrapper(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package_root = tmp_path / "wrapped_context_app"
    package_root.mkdir()
    (package_root / "__init__.py").write_text("", encoding="utf-8")
    (package_root / "helpers.py").write_text(
        "from wybra.template.context import add_to_context\n"
        "\n"
        "def register_provider(provider):\n"
        "    return add_to_context(provider)\n",
        encoding="utf-8",
    )
    (package_root / "context.py").write_text(
        "from wrapped_context_app.helpers import register_provider\n"
        "\n"
        "def site_context(request, context):\n"
        "    del request\n"
        "    return context.with_values(site_name='Wrapped app')\n"
        "\n"
        "register_provider(site_context)\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()

    providers = discover_context_providers("wrapped_context_app")

    assert len(providers) == 1
    assert providers[0](object(), TemplateContext()).as_dict() == {
        "site_name": "Wrapped app"
    }


def test_context_providers_from_modules_preserves_configured_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for package_name, key in (
        ("first_context_app", "first"),
        ("second_context_app", "second"),
    ):
        package_root = tmp_path / package_name
        package_root.mkdir()
        (package_root / "__init__.py").write_text("", encoding="utf-8")
        (package_root / "context.py").write_text(
            "from wybra.template.context import add_to_context\n"
            f"add_to_context({{{key!r}: True}})\n",
            encoding="utf-8",
        )
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()

    providers = context_providers_from_modules(
        ("first_context_app", "second_context_app")
    )

    assert tuple(
        provider(object(), TemplateContext()).as_dict() for provider in providers
    ) == (
        {"first": True},
        {"second": True},
    )


def test_resolve_context_providers_accumulates_immutable_context_in_order() -> None:
    def sync_provider(
        request: object,
        context: TemplateContext,
    ) -> TemplateContext:
        del request
        return context.with_values(site_name="Test app")

    async def async_provider(
        request: object,
        context: TemplateContext,
    ) -> TemplateContext:
        del request
        return context.with_values(authenticated=False)

    context = asyncio.run(
        resolve_context_providers((sync_provider, async_provider), object())
    )

    assert context.as_dict() == {"site_name": "Test app", "authenticated": False}


def test_resolve_context_providers_allows_provider_owned_request_key() -> None:
    def provider(
        request: object,
        context: TemplateContext,
    ) -> TemplateContext:
        del request
        return context.with_values(request="caller-controlled")

    context = asyncio.run(resolve_context_providers((provider,), object()))

    assert context.as_dict() == {"request": "caller-controlled"}


def test_resolve_context_providers_preserves_empty_initial_context() -> None:
    initial_context = TemplateContext()
    observed: dict[str, TemplateContext] = {}

    def provider(
        request: object,
        context: TemplateContext,
    ) -> TemplateContext:
        del request
        observed["context"] = context
        return context.with_values(app_name="wybra")

    context = asyncio.run(
        resolve_context_providers(
            (provider,),
            object(),
            initial_context=initial_context,
        )
    )

    assert observed["context"] is initial_context
    assert context.as_dict() == {"app_name": "wybra"}


def test_template_context_layer_overrides_parent_without_mutating_parent() -> None:
    original_request = object()
    provider_request = object()
    parent = TemplateContext.from_mapping({"request": original_request})

    context = parent.with_values(request=provider_request)

    assert context.as_dict() == {"request": provider_request}
    assert parent.as_dict() == {"request": original_request}
    assert context.as_dict()["request"] is not original_request


def test_resolve_context_providers_layers_existing_keys_newest_first() -> None:
    def first_provider(
        request: object,
        context: TemplateContext,
    ) -> TemplateContext:
        del request
        return context.with_values(shared="first")

    def second_provider(
        request: object,
        context: TemplateContext,
    ) -> TemplateContext:
        del request
        return context.with_values(shared="second")

    context = asyncio.run(
        resolve_context_providers((first_provider, second_provider), object())
    )

    assert context.as_dict() == {"shared": "second"}


def test_template_context_resolves_unmatched_keys_from_parent_layers() -> None:
    context = TemplateContext.from_mapping({"page_title": "Parent"})
    child_context = context.with_values(section_title="Child")

    assert child_context["page_title"] == "Parent"
    assert child_context["section_title"] == "Child"
    assert child_context.as_dict() == {
        "page_title": "Parent",
        "section_title": "Child",
    }


def test_template_context_can_be_constructed_from_multiple_layers() -> None:
    context = TemplateContext.from_layers(
        {"page_title": "Child"},
        {"page_title": "Parent", "section_title": "Section"},
    )

    assert context["page_title"] == "Child"
    assert context["section_title"] == "Section"
    assert context.as_dict() == {
        "page_title": "Child",
        "section_title": "Section",
    }


def test_load_modules_imports_explicit_modules_in_configured_order() -> None:
    modules = load_modules(("wybra.core.resources", "wybra.web.routes"))

    assert tuple(module.__name__ for module in modules) == (
        "wybra.core.resources",
        "wybra.web.routes",
    )


def test_load_modules_fails_clearly_for_missing_module() -> None:
    with pytest.raises(
        CompositionError,
        match="Configured module cannot be imported: wybra.web.missing",
    ):
        load_modules(("wybra.web.missing",))


def test_load_app_config_reads_modules_from_app_toml(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "app.toml"
    config_path.write_text(
        """
        [app]
        modules = ["host_app", "wybra.auth"]

        [app.routes]
        wybra-auth = { account = "/account", api = "" }

        [app.templates]
        auto_reload = false
        cache_size = 400

        [app.assets]
        url_path = "/assets/"
        root = "build/assets"
        """,
        encoding="utf-8",
    )

    config = load_app_config(project_root=tmp_path, config_path=config_path)

    assert isinstance(config, AppConfig)
    assert config.config_path == config_path.resolve()
    assert config.modules == ("host_app", "wybra.auth")
    assert config.routes == RouteOptions(
        prefixes={"wybra.auth": {"account": "/account", "api": ""}},
    )
    assert config.templates == TemplateOptions(
        auto_reload=False,
        cache_size=400,
    )
    assert config.assets == AssetOptions(
        url_path="/assets/",
        root=Path("build/assets"),
    )


def test_load_app_config_rejects_duplicate_route_module_aliases(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "app.toml"
    config_path.write_text(
        """
        [app]
        modules = ["wybra.auth"]

        [app.routes]
        wybra-auth = { account = "/account" }
        "wybra.auth" = { api = "" }

        [app.templates]
        auto_reload = true
        cache_size = 0

        [app.assets]
        url_path = "/static/"
        root = "static"
        """,
        encoding="utf-8",
    )

    with pytest.raises(CompositionError, match="duplicate route entries"):
        load_app_config(config_path=config_path)


@pytest.mark.parametrize(
    "module_name",
    [
        " wybra-auth",
        "wybra-auth ",
        "wybra auth",
    ],
    ids=("leading", "trailing", "embedded"),
)
def test_load_app_config_rejects_route_module_aliases_with_whitespace(
    tmp_path: Path,
    module_name: str,
) -> None:
    config_path = tmp_path / "app.toml"
    config_path.write_text(
        f'''
        [app]
        modules = ["wybra.auth"]

        [app.routes]
        "{module_name}" = {{ account = "/account" }}

        [app.templates]
        auto_reload = true
        cache_size = 0

        [app.assets]
        url_path = "/static/"
        root = "static"
        ''',
        encoding="utf-8",
    )

    with pytest.raises(CompositionError, match="must not contain whitespace"):
        load_app_config(config_path=config_path)


def test_load_app_config_rejects_malformed_toml(tmp_path: Path) -> None:
    config_path = tmp_path / "app.toml"
    config_path.write_text("[app", encoding="utf-8")

    with pytest.raises(CompositionError, match="App config file is invalid"):
        load_app_config(project_root=tmp_path, config_path=config_path)


def test_load_app_config_modules_requires_config_source(
    tmp_path: Path,
) -> None:
    with pytest.raises(
        CompositionError,
        match="Application config file could not be resolved",
    ):
        load_app_config_modules(project_root=tmp_path)


def test_load_app_config_loads_auth_table(tmp_path: Path) -> None:
    config_path = tmp_path / "app.toml"
    config_path.write_text(
        """
        [app]
        database_url = "sqlite+aiosqlite:///app.sqlite3"
        modules = ["host_app"]

        [app.templates]
        auto_reload = true
        cache_size = 0

        [app.assets]
        url_path = "/static/"

        [auth]
        account_creation_policy = "public-signup"
        """,
        encoding="utf-8",
    )

    config = load_app_config(project_root=tmp_path, config_path=config_path)

    assert config.config_path == config_path.resolve()
    assert config.modules == ("host_app",)
    assert config.database_url == "sqlite+aiosqlite:///app.sqlite3"
    assert config.assets.root == Path("static")
    assert config.auth == {"account_creation_policy": "public-signup"}


def test_load_app_config_uses_app_config_environment_override(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "configured" / "app.toml"
    config_path.parent.mkdir()
    config_path.write_text(
        """
        [app]
        modules = ["host_app"]

        [app.templates]
        auto_reload = true
        cache_size = 0

        [app.assets]
        url_path = "/static/"
        root = "static"
        """,
        encoding="utf-8",
    )

    config = load_app_config(
        project_root=tmp_path,
        environ={"APP_CONFIG": str(config_path)},
    )

    assert config.config_path == config_path.resolve()
    assert config.modules == ("host_app",)


def test_load_app_config_rejects_blank_app_config_environment(
    tmp_path: Path,
) -> None:
    with pytest.raises(CompositionError, match="APP_CONFIG must not be blank"):
        load_app_config(
            project_root=tmp_path,
            environ={"APP_CONFIG": "   "},
        )


def test_load_app_config_explicit_path_overrides_app_config_environment(
    tmp_path: Path,
) -> None:
    env_config_path = tmp_path / "env-app.toml"
    explicit_config_path = tmp_path / "explicit-app.toml"
    env_config_path.write_text(
        """
        [app]
        modules = ["wybra.auth"]

        [app.templates]
        auto_reload = true
        cache_size = 0

        [app.assets]
        url_path = "/static/"
        root = "static"
        """,
        encoding="utf-8",
    )
    explicit_config_path.write_text(
        """
        [app]
        modules = ["host_app"]

        [app.templates]
        auto_reload = true
        cache_size = 0

        [app.assets]
        url_path = "/static/"
        root = "static"
        """,
        encoding="utf-8",
    )

    config = load_app_config(
        project_root=tmp_path,
        config_path=explicit_config_path,
        environ={"APP_CONFIG": str(env_config_path)},
    )

    assert config.config_path == explicit_config_path.resolve()
    assert config.modules == ("host_app",)
