import ast
import asyncio
import importlib
import tomllib
from dataclasses import dataclass
from pathlib import Path
from textwrap import dedent

import pytest
from envex import Env
from fastapi import Depends, FastAPI
from fastapi.responses import Response
from fastapi.routing import APIRoute, APIRouter
from fastapi.testclient import TestClient
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
    ConfiguredModuleRouter,
    RouteCompositionError,
    load_configured_module_routes,
    load_module_routes,
    register_module_routes,
    route_prefixes_from_app_config,
)
from wevra.web.routes.discovery import (
    ModuleSurface,
    context_providers_from_modules,
    discover_context_providers,
    discover_module_routers,
    discover_module_surface,
    discover_module_surfaces,
    discover_static_sources,
    discover_template_sources,
    static_sources_from_modules,
    template_sources_from_modules,
)
from wevra.web.security import (
    COOP_HEADER_NAME,
    SecurityHeaderOptions,
    cross_origin_opener_policy,
    register_security_headers,
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
        "wevra.web.errors",
        "wevra.web.forms.security",
        "wevra.core.resources",
        "wevra.web.routes.contracts",
        "wevra.web.routes",
        "wevra.web.security",
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
            module == "host_app"
            or module.startswith("host_app.")
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
        "host_app.app",
        "host_app.routes",
        "host_app.settings",
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


def test_module_surface_default_to_empty_optional_contributions() -> None:
    surface = ModuleSurface(module_name="host_app")

    assert surface.module_name == "host_app"
    assert surface.module_routers == {}
    assert surface.template_sources == ()
    assert surface.static_sources == ()
    assert surface.context_providers == ()


def test_module_surface_accepts_declared_contract_contributions() -> None:
    api_router = APIRouter()
    template_source = PackageResourceSource(package="wevra.auth", directory="templates")
    static_source = PackageResourceSource(package="wevra.auth", directory="static")

    surface = ModuleSurface(
        module_name="wevra.auth",
        module_routers={"api": api_router},
        template_sources=(template_source,),
        static_sources=(static_source,),
    )

    assert surface.module_routers == {"api": api_router}
    assert surface.template_sources == (template_source,)
    assert surface.static_sources == (static_source,)


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


def test_discover_module_routers_accepts_mapping_export(
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


def test_configured_module_routes_and_registration_are_wevra_web_concerns(
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
            static=StaticOptions(url_path="/static/", export_root=Path("static")),
        )

    route_set = load_configured_module_routes(Settings())
    app = FastAPI()

    register_module_routes(app, route_set)

    assert route_prefixes_from_app_config(Settings.app_config) == {
        module_name: {"default": "/site"}
    }
    assert "public:ping" in {getattr(route, "name", None) for route in app.routes}
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


def test_register_module_routes_rejects_route_name_conflicts() -> None:
    first_router = APIRouter()
    second_router = APIRouter()

    @first_router.get("/first", name="shared:home")
    async def first() -> Response:
        return Response()

    @second_router.get("/second", name="shared:home")
    async def second() -> Response:
        return Response()

    with pytest.raises(
        RouteCompositionError,
        match="Route name conflict.*shared:home.*first.*second",
    ):
        register_module_routes(
            FastAPI(),
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


def test_register_module_routes_rejects_method_path_conflicts() -> None:
    first_router = APIRouter()
    second_router = APIRouter()

    @first_router.get("/shared", name="first:shared")
    async def first() -> Response:
        return Response()

    @second_router.get("/shared", name="second:shared")
    async def second() -> Response:
        return Response()

    with pytest.raises(
        RouteCompositionError,
        match="Route method/path conflict.*GET /shared.*first.*second",
    ):
        register_module_routes(
            FastAPI(),
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


def test_load_module_routes_rejects_missing_router_prefix_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package_root = tmp_path / "missing_prefix_route_app"
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
        match="missing router label 'pages'",
    ):
        load_module_routes(
            ("missing_prefix_route_app",),
            route_prefixes={"missing_prefix_route_app": {}},
        )


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


def test_load_app_config_reads_modules_from_app_toml(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "app.toml"
    config_path.write_text(
        """
        modules = ["host_app", "wevra.auth"]

        [routes."wevra.auth"]
        account = "/account"
        api = ""

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
    assert config.modules == ("host_app", "wevra.auth")
    assert config.routes == RouteOptions(
        prefixes={"wevra.auth": {"account": "/account", "api": ""}},
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
        default_modules=("host_app", "wevra.web"),
    )

    assert modules == ("host_app", "wevra.web")


def test_load_app_config_allows_reserved_auth_table(tmp_path: Path) -> None:
    config_path = tmp_path / "app.toml"
    config_path.write_text(
        """
        modules = ["host_app"]

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
    assert config.modules == ("host_app",)
    assert config.static.export_root == Path("static")
    assert not hasattr(config, "auth")


def test_framework_repository_does_not_ship_host_app_config() -> None:
    project_root = Path(__file__).resolve().parents[1]

    assert not (project_root / "app.toml").exists()
    assert load_app_config_modules(
        project_root=project_root,
        default_modules=("wevra.web",),
    ) == ("wevra.web",)


def test_load_app_config_uses_app_config_environment_override(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "configured" / "app.toml"
    config_path.parent.mkdir()
    config_path.write_text(
        """
        modules = ["host_app"]

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
    assert config.modules == ("host_app",)


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
        modules = ["host_app"]

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
    assert config.modules == ("host_app",)
