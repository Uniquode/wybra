import ast
import asyncio
import importlib
import tomllib
from dataclasses import dataclass
from pathlib import Path
from textwrap import dedent
from typing import Any, get_type_hints

import pytest
from envex import Env
from fastapi import FastAPI, Request
from fastapi.responses import Response
from fastapi.routing import APIRouter
from jinja2 import Environment, select_autoescape
from jinja2.exceptions import TemplateNotFound

from wevra.core.composition import (
    AppConfig,
    CompositionError,
    RouteOptions,
    StaticOptions,
    TemplateOptions,
    load_app_config,
    load_app_config_modules,
    load_modules,
)
from wevra.core.resources import PackageResourceSource, read_text_resource
from wevra.core.settings import (
    EnvironmentSetting,
    SettingsLoadError,
    load_composed_settings,
    values_from_env_settings,
)
from wevra.web.context import ContextProviderError, resolve_context_providers
from wevra.web.routes import (
    HtmlRouteDefinition,
    HtmlSurface,
    HtmlView,
    ModuleRoutes,
    RouteCompositionError,
    compose_module_routes,
    load_configured_module_routes,
    load_module_routes,
    register_module_routes,
    route_prefixes_from_app_config,
)
from wevra.web.routes.discovery import (
    ModuleSurface,
    context_providers_from_modules,
    discover_context_providers,
    discover_module_routes,
    discover_module_surface,
    discover_module_surfaces,
    discover_static_sources,
    discover_template_sources,
    static_sources_from_modules,
    template_sources_from_modules,
)
from wevra.web.staticfiles import (
    StaticAssetDuplicate,
    export_configured_static_assets,
    export_static_assets,
    static_asset_response,
)
from wevra.web.templating import build_template_loader


def test_wevra_web_package_imports() -> None:
    package = importlib.import_module("wevra.web")

    assert package.__name__ == "wevra.web"


def test_wevra_web_package_exposes_expected_submodules() -> None:
    for module_name in (
        "wevra.core.composition",
        "wevra.web.context",
        "wevra.web.forms.csrf",
        "wevra.web.routes.dispatcher",
        "wevra.web.errors",
        "wevra.web.forms.security",
        "wevra.core.resources",
        "wevra.web.routes.contracts",
        "wevra.web.routes",
        "wevra.core.settings",
        "wevra.web.routes",
        "wevra.web.style_contract",
        "wevra.web.routes.discovery",
        "wevra.web.staticfiles",
        "wevra.web.templating",
        "wevra.web.theme",
        "wevra.web.views",
    ):
        assert importlib.import_module(module_name).__name__ == module_name


def test_wevra_web_package_is_independent_from_application_and_auth_modules() -> None:
    source_root = Path(__file__).resolve().parents[1] / "src" / "wevra" / "web"

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
            module == "uniquode"
            or module.startswith("uniquode.")
            or module == "wevra.auth"
            or module.startswith("wevra.auth.")
            for module in imported_modules
        )


def test_wevra_web_composition_loader_is_cli_safe() -> None:
    path = Path(__file__).resolve().parents[1] / "src/wevra/core/composition.py"
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
        "wevra.auth",
        "fastapi",
        "jinja2",
        "uniquode.app",
        "uniquode.routes",
        "uniquode.settings",
    }
    assert not any(
        module == forbidden_module or module.startswith(f"{forbidden_module}.")
        for module in imported_modules
        for forbidden_module in forbidden_modules
    )


def test_wevra_web_package_is_included_in_build_modules() -> None:
    pyproject = tomllib.loads(
        (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text()
    )

    assert "wevra" in pyproject["tool"]["uv"]["build-backend"]["module-name"]


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

    (tmp_path / "app.toml").write_text(
        dedent(
            """
            modules = ["wevra.web"]

            [templates]
            auto_reload = true
            cache_size = 0

            [static]
            url_path = "/assets/"
            export_root = "static"
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
    assert settings.app_config.modules == ("wevra.web",)
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


def test_html_route_definition_captures_route_contract() -> None:
    class View:
        async def render(self, request: Request, renderer: object) -> Response:
            del request, renderer
            return Response()

    definition = HtmlRouteDefinition(
        path="/account",
        name="identity:account",
        methods=("GET",),
        surface="page",
        view=View(),
    )

    assert definition.path == "/account"
    assert definition.name == "identity:account"
    assert definition.methods == ("GET",)
    assert definition.surface == "page"
    assert isinstance(definition.view, HtmlView)


def test_html_route_definition_rejects_unknown_surfaces_at_type_boundary() -> None:
    hints = get_type_hints(HtmlRouteDefinition)

    assert hints["surface"] == HtmlSurface


def test_html_view_contract_uses_fastapi_engine_types() -> None:
    hints = get_type_hints(HtmlView.render)

    assert hints["request"] is Request
    assert hints["renderer"] is Any
    assert hints["return"] is Response


def test_module_routes_default_to_empty_web_contributions() -> None:
    routes = ModuleRoutes()

    assert routes.page_routes == ()
    assert routes.partial_routes == ()
    assert routes.api_routers == ()


def test_module_surface_default_to_empty_optional_contributions() -> None:
    surface = ModuleSurface(module_name="uniquode")

    assert surface.module_name == "uniquode"
    assert surface.routes == ModuleRoutes()
    assert surface.template_sources == ()
    assert surface.static_sources == ()
    assert surface.context_providers == ()


def test_module_surface_accepts_declared_contract_contributions() -> None:
    api_router = APIRouter()
    template_source = PackageResourceSource(package="wevra.auth", directory="templates")
    static_source = PackageResourceSource(package="wevra.auth", directory="static")
    routes = ModuleRoutes(api_routers=(api_router,))

    surface = ModuleSurface(
        module_name="wevra.auth",
        routes=routes,
        template_sources=(template_source,),
        static_sources=(static_source,),
    )

    assert surface.routes.api_routers == (api_router,)
    assert surface.template_sources == (template_source,)
    assert surface.static_sources == (static_source,)


class _RouteCompositionView:
    async def render(self, request: Request, renderer: object) -> Response:
        del request, renderer
        return Response()


def _route_definition(
    path: str,
    name: str,
    methods: tuple[str, ...] = ("GET",),
    surface: HtmlSurface = "page",
) -> HtmlRouteDefinition:
    return HtmlRouteDefinition(
        path=path,
        name=name,
        methods=methods,
        surface=surface,
        view=_RouteCompositionView(),
    )


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
        include_context=True,
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


def test_discover_module_routes_reads_module_routes_export(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package_root = tmp_path / "routes_surface_app"
    package_root.mkdir()
    (package_root / "__init__.py").write_text("", encoding="utf-8")
    (package_root / "routes.py").write_text(
        "from wevra.web.routes import ModuleRoutes\nmodule_routes = ModuleRoutes()\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()

    assert discover_module_routes("routes_surface_app") == ModuleRoutes()


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
            from fastapi.responses import Response
            from wevra.web.routes import HtmlRouteDefinition, ModuleRoutes

            class View:
                async def render(self, request, renderer):
                    del request, renderer
                    return Response()

            router = APIRouter()

            @router.get('/{package_name}/api', name={api_route_name!r})
            async def api():
                return {{'ok': True}}

            module_routes = ModuleRoutes(
                page_routes=(HtmlRouteDefinition(
                    path='home',
                    name={route_name!r},
                    methods=('GET',),
                    surface='page',
                    view=View(),
                ),),
                api_routers=(router,),
            )
            """
            ),
            encoding="utf-8",
        )
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()

    routes = load_module_routes(
        ("first_routes_app", "second_routes_app"),
        route_prefixes={
            "first_routes_app": "/first",
            "second_routes_app": "/second",
        },
    )

    assert tuple(route.name for route in routes.page_routes) == (
        "first:home",
        "second:home",
    )
    assert tuple(route.path for route in routes.page_routes) == (
        "/first/home",
        "/second/home",
    )
    assert tuple(router.routes[0].name for router in routes.api_routers) == (
        "first:home:api",
        "second:home:api",
    )


def test_configured_module_routes_and_registration_are_wevra_web_concerns() -> None:
    class Settings:
        modules = ("uniquode",)
        app_config = AppConfig(
            config_path=Path("app.toml"),
            project_root=Path.cwd(),
            modules=modules,
            routes=RouteOptions(prefixes={"uniquode": "/site"}),
            templates=TemplateOptions(auto_reload=True, cache_size=0),
            static=StaticOptions(url_path="/static/", export_root=Path("static")),
        )

    class Dispatcher:
        def __init__(self) -> None:
            self.registered_routes: tuple[HtmlRouteDefinition, ...] = ()

        def register(self, definitions) -> None:
            self.registered_routes = self.registered_routes + tuple(definitions)

    route_set = load_configured_module_routes(Settings())
    app = FastAPI()
    dispatcher = Dispatcher()

    register_module_routes(app, dispatcher, route_set)  # type: ignore[arg-type]

    assert route_prefixes_from_app_config(Settings.app_config) == {"uniquode": "/site"}
    assert tuple(route.name for route in dispatcher.registered_routes) == (
        "public:home",
    )
    assert tuple(route.path for route in dispatcher.registered_routes) == ("/",)
    assert "public:home" in {getattr(route, "name", None) for route in app.routes}


def test_compose_module_routes_applies_prefixes_to_relative_paths() -> None:
    routes = compose_module_routes(
        (
            (
                "admin",
                ModuleRoutes(
                    page_routes=(_route_definition("dashboard", "admin:dashboard"),),
                    partial_routes=(
                        _route_definition(
                            "summary",
                            "admin:partial:summary",
                            surface="partial",
                        ),
                    ),
                ),
            ),
        ),
        route_prefixes={"admin": "/admin/"},
    )

    assert tuple(route.path for route in routes.page_routes) == ("/admin/dashboard",)
    assert tuple(route.path for route in routes.partial_routes) == ("/admin/summary",)


def test_compose_module_routes_preserves_absolute_paths() -> None:
    routes = compose_module_routes(
        (
            (
                "identity",
                ModuleRoutes(
                    page_routes=(_route_definition("/login", "identity:login"),),
                ),
            ),
        ),
        route_prefixes={"identity": "/identity"},
    )

    assert tuple(route.path for route in routes.page_routes) == ("/login",)


def test_compose_module_routes_rejects_route_name_conflicts() -> None:
    with pytest.raises(
        RouteCompositionError,
        match="Route name conflict.*shared:home.*first.*second",
    ):
        compose_module_routes(
            (
                (
                    "first",
                    ModuleRoutes(
                        page_routes=(_route_definition("/first", "shared:home"),),
                    ),
                ),
                (
                    "second",
                    ModuleRoutes(
                        page_routes=(_route_definition("/second", "shared:home"),),
                    ),
                ),
            ),
            route_prefixes={},
        )


def test_compose_module_routes_rejects_html_api_method_path_conflicts() -> None:
    api_router = APIRouter()

    @api_router.get("/shared", name="api:shared")
    async def shared() -> dict[str, bool]:
        return {"ok": True}

    with pytest.raises(
        RouteCompositionError,
        match="Route method/path conflict.*GET /shared.*page.*api",
    ):
        compose_module_routes(
            (
                (
                    "page",
                    ModuleRoutes(
                        page_routes=(_route_definition("/shared", "page:shared"),),
                    ),
                ),
                ("api", ModuleRoutes(api_routers=(api_router,))),
            ),
            route_prefixes={},
        )


def test_compose_module_routes_rejects_non_string_http_methods() -> None:
    invalid_route = _route_definition(
        "/bad-method",
        "bad:method",
        methods=("GET", 123),  # type: ignore[arg-type]
    )

    with pytest.raises(
        RouteCompositionError,
        match=r"invalid HTTP method 123; methods must be strings",
    ):
        compose_module_routes(
            (("bad", ModuleRoutes(page_routes=(invalid_route,))),),
            route_prefixes={},
        )


def test_discover_module_routes_rejects_malformed_present_surface(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package_root = tmp_path / "bad_routes_surface_app"
    package_root.mkdir()
    (package_root / "__init__.py").write_text("", encoding="utf-8")
    (package_root / "routes.py").write_text(
        "module_routes = object()\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()

    with pytest.raises(
        CompositionError,
        match="bad_routes_surface_app.routes.*module_routes",
    ):
        discover_module_routes("bad_routes_surface_app")


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

    assert surface.template_sources == (
        PackageResourceSource(package="resource_surface_app", directory="templates"),
    )
    assert surface.static_sources == (
        PackageResourceSource(package="resource_surface_app", directory="static"),
    )
    assert discover_template_sources("resource_surface_app") == surface.template_sources
    assert discover_static_sources("resource_surface_app") == surface.static_sources


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
        (PackageResourceSource(package="wevra.web", directory="static"),),
        "missing.css",
    )

    assert response.status_code == 404
    assert response.body == b"Not Found"
    assert response.media_type == "text/plain"


def test_static_export_writes_winning_assets_and_reports_duplicates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for package_name, content in (
        ("base_export_static_app", "base"),
        ("application_export_static_app", "application"),
    ):
        package_root = tmp_path / package_name
        static_root = package_root / "static" / "styles"
        static_root.mkdir(parents=True)
        (package_root / "__init__.py").write_text("", encoding="utf-8")
        (static_root / "app.css").write_text(content, encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()

    sources = static_sources_from_modules(
        ("application_export_static_app", "base_export_static_app")
    )
    result = export_static_assets(sources, export_root=tmp_path / "collected")

    exported_asset = tmp_path / "collected" / "styles" / "app.css"
    assert exported_asset.read_text(encoding="utf-8") == "application"
    assert tuple(asset.logical_path for asset in result.exported_assets) == (
        "styles/app.css",
    )
    assert result.duplicates == (
        StaticAssetDuplicate(
            logical_path="styles/app.css",
            winner=PackageResourceSource(
                package="application_export_static_app",
                directory="static",
            ),
            shadowed=PackageResourceSource(
                package="base_export_static_app",
                directory="static",
            ),
        ),
    )


def test_configured_static_export_uses_app_toml_without_route_import(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package_root = tmp_path / "configured_export_static_app"
    static_root = package_root / "static" / "styles"
    static_root.mkdir(parents=True)
    (package_root / "__init__.py").write_text("", encoding="utf-8")
    (package_root / "routes.py").write_text(
        "raise RuntimeError('route surface should not be imported')\n",
        encoding="utf-8",
    )
    (static_root / "app.css").write_text(":root {}", encoding="utf-8")
    (tmp_path / "app.toml").write_text(
        """
        modules = ["configured_export_static_app"]

        [templates]
        auto_reload = true
        cache_size = 0

        [static]
        url_path = "/static/"
        export_root = "exported-static"
        """,
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()

    result = export_configured_static_assets(project_root=tmp_path)

    exported_asset = tmp_path / "exported-static" / "styles" / "app.css"
    assert result.export_root == (tmp_path / "exported-static").resolve()
    assert exported_asset.read_text(encoding="utf-8") == ":root {}"
    assert result.duplicates == ()


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
        "from wevra.web.context import add_to_context\n"
        "\n"
        "def site_context(request):\n"
        "    del request\n"
        "    return {'site_name': 'Test app'}\n"
        "\n"
        "add_to_context({'app_mode': 'test'})\n"
        "add_to_context(site_context)\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()

    providers = discover_context_providers("context_surface_app")

    assert len(providers) == 2
    assert providers[0](object()) == {"app_mode": "test"}
    assert providers[1](object()) == {"site_name": "Test app"}


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
            "from wevra.web.context import add_to_context\n"
            f"add_to_context({{{key!r}: True}})\n",
            encoding="utf-8",
        )
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()

    providers = context_providers_from_modules(
        ("first_context_app", "second_context_app")
    )

    assert tuple(provider(object()) for provider in providers) == (
        {"first": True},
        {"second": True},
    )


def test_resolve_context_providers_merges_async_context_in_order() -> None:
    def sync_provider(request: object) -> dict[str, object]:
        del request
        return {"site_name": "Test app"}

    async def async_provider(request: object) -> dict[str, object]:
        del request
        return {"authenticated": False}

    context = asyncio.run(
        resolve_context_providers((sync_provider, async_provider), object())
    )

    assert context == {"site_name": "Test app", "authenticated": False}


def test_resolve_context_providers_rejects_reserved_keys() -> None:
    def provider(request: object) -> dict[str, object]:
        del request
        return {"request": "caller-controlled"}

    with pytest.raises(
        ContextProviderError,
        match="reserved template context keys: request",
    ):
        asyncio.run(
            resolve_context_providers(
                (provider,),
                object(),
                reserved_keys=frozenset({"request"}),
            )
        )


def test_resolve_context_providers_rejects_provider_key_collisions() -> None:
    def first_provider(request: object) -> dict[str, object]:
        del request
        return {"shared": "first"}

    def second_provider(request: object) -> dict[str, object]:
        del request
        return {"shared": "second"}

    with pytest.raises(
        ContextProviderError,
        match="collides with existing template context keys: shared",
    ):
        asyncio.run(
            resolve_context_providers((first_provider, second_provider), object())
        )


def test_load_modules_imports_explicit_modules_in_configured_order() -> None:
    modules = load_modules(("wevra.core.resources", "wevra.web.routes"))

    assert tuple(module.__name__ for module in modules) == (
        "wevra.core.resources",
        "wevra.web.routes",
    )


def test_load_modules_fails_clearly_for_missing_module() -> None:
    with pytest.raises(
        CompositionError,
        match="Configured module cannot be imported: wevra.web.missing",
    ):
        load_modules(("wevra.web.missing",))


def test_wevra_web_dispatcher_owns_route_contracts() -> None:
    from wevra.web.routes import dispatcher

    assert dispatcher.HtmlRouteDefinition is HtmlRouteDefinition
    assert dispatcher.HtmlView is HtmlView


def test_load_app_config_reads_modules_from_app_toml(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "app.toml"
    config_path.write_text(
        """
        modules = ["uniquode", "wevra.auth"]

        [routes]
        "wevra.auth" = "/"

        [templates]
        auto_reload = false
        cache_size = 400

        [static]
        url_path = "/assets/"
        export_root = "build/assets"
        """,
        encoding="utf-8",
    )

    config = load_app_config(project_root=tmp_path)

    assert isinstance(config, AppConfig)
    assert config.config_path == config_path.resolve()
    assert config.modules == ("uniquode", "wevra.auth")
    assert config.routes == RouteOptions(
        prefixes={"wevra.auth": "/"},
    )
    assert config.templates == TemplateOptions(
        auto_reload=False,
        cache_size=400,
    )
    assert config.static == StaticOptions(
        url_path="/assets/",
        export_root=Path("build/assets"),
    )


def test_load_app_config_rejects_malformed_toml(tmp_path: Path) -> None:
    config_path = tmp_path / "app.toml"
    config_path.write_text("[app", encoding="utf-8")

    with pytest.raises(CompositionError, match="App config file is invalid"):
        load_app_config(project_root=tmp_path)


def test_load_app_config_modules_uses_defaults_when_app_toml_is_absent(
    tmp_path: Path,
) -> None:
    modules = load_app_config_modules(
        project_root=tmp_path,
        default_modules=("uniquode", "wevra.web"),
    )

    assert modules == ("uniquode", "wevra.web")


def test_load_app_config_allows_reserved_auth_table(tmp_path: Path) -> None:
    config_path = tmp_path / "app.toml"
    config_path.write_text(
        """
        modules = ["uniquode"]

        [templates]
        auto_reload = true
        cache_size = 0

        [static]
        url_path = "/static/"

        [auth]
        account_creation_policy = "public-signup"
        """,
        encoding="utf-8",
    )

    config = load_app_config(project_root=tmp_path)

    assert config.config_path == config_path.resolve()
    assert config.modules == ("uniquode",)
    assert config.static.export_root == Path("static")
    assert not hasattr(config, "auth")


def test_repository_app_toml_preserves_current_default_modules() -> None:
    project_root = Path(__file__).resolve().parents[1]

    config = load_app_config(project_root=project_root)

    assert config.modules == ("uniquode", "wevra.web", "wevra.auth")
    assert config.routes == RouteOptions(prefixes={"wevra.auth": "/"})
    assert config.templates == TemplateOptions(
        auto_reload=True,
        cache_size=0,
    )
    assert config.static == StaticOptions(
        url_path="/static/",
        export_root=Path("static"),
    )


def test_load_app_config_uses_app_config_environment_override(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "configured" / "app.toml"
    config_path.parent.mkdir()
    config_path.write_text(
        """
        modules = ["uniquode"]

        [templates]
        auto_reload = true
        cache_size = 0

        [static]
        url_path = "/static/"
        export_root = "static"
        """,
        encoding="utf-8",
    )

    config = load_app_config(
        project_root=tmp_path,
        environ={"APP_CONFIG": str(config_path)},
    )

    assert config.config_path == config_path.resolve()
    assert config.modules == ("uniquode",)


def test_load_app_config_explicit_path_overrides_app_config_environment(
    tmp_path: Path,
) -> None:
    env_config_path = tmp_path / "env-app.toml"
    explicit_config_path = tmp_path / "explicit-app.toml"
    env_config_path.write_text(
        """
        modules = ["wevra.auth"]

        [templates]
        auto_reload = true
        cache_size = 0

        [static]
        url_path = "/static/"
        export_root = "static"
        """,
        encoding="utf-8",
    )
    explicit_config_path.write_text(
        """
        modules = ["uniquode"]

        [templates]
        auto_reload = true
        cache_size = 0

        [static]
        url_path = "/static/"
        export_root = "static"
        """,
        encoding="utf-8",
    )

    config = load_app_config(
        project_root=tmp_path,
        config_path=explicit_config_path,
        environ={"APP_CONFIG": str(env_config_path)},
    )

    assert config.config_path == explicit_config_path.resolve()
    assert config.modules == ("uniquode",)
