import ast
from dataclasses import dataclass, replace
from pathlib import Path
from textwrap import dedent
from types import SimpleNamespace

import click
import pytest

import wybra.assets.validation as asset_validation
import wybra.template.validation as template_validation
import wybra.tools.validate as validate_module
import wybra.web
from wybra.api.validation import validate_api
from wybra.assets.validation import validate_assets
from wybra.config import AppConfigSource, ConfigService
from wybra.core.composition import (
    AppConfig,
    AssetOptions,
    RouteOptions,
    TemplateOptions,
)
from wybra.core.config import RUNTIME_CONFIG_DEF
from wybra.core.routes.validation import validate_routes
from wybra.security.validation import validate_security
from wybra.template.validation import _contains_post_form, validate_template
from wybra.tools.project import ProjectToolConfigurationError, runtime_project_root
from wybra.tools.settings import ProjectSettings
from wybra.tools.validate import main as validate_main
from wybra.tools.validation.core import ValidationResult
from wybra.tools.validation.registry import (
    ValidationDiscoveryError,
    discover_validation_target_details,
    discover_validation_targets,
)
from wybra.widgets.validation import validate_widgets


@dataclass(frozen=True, slots=True)
class WebSettings:
    project_root: Path
    modules: tuple[str, ...]
    template_root: Path
    static_root: Path
    static_url_path: str = "/static/"
    template_auto_reload: bool | None = None
    template_cache_size: int = 400
    app_config: AppConfig | None = None
    config: ConfigService | None = None
    uses_filesystem_template_root: bool = False
    uses_filesystem_static_root: bool = False


@dataclass(frozen=True, slots=True)
class AssetValidationOnlySettings:
    project_root: Path
    modules: tuple[str, ...]
    static_root: Path | None
    app_config: AppConfig | None
    uses_filesystem_static_root: bool = False


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imported_modules = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }
    imported_modules.update(
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    )
    return imported_modules


def _write_validation_module(
    root: Path,
    module_name: str,
    validation_body: str,
) -> None:
    module_root = root / module_name
    module_root.mkdir()
    (module_root / "__init__.py").write_text("", encoding="utf-8")
    (module_root / "validation.py").write_text(
        dedent(validation_body),
        encoding="utf-8",
    )


def _write_replacement_security_module(root: Path, module_name: str) -> None:
    module_root = root / module_name
    module_root.mkdir()
    (module_root / "__init__.py").write_text(
        dedent(
            """
            provides_security_capability = True
            """
        ),
        encoding="utf-8",
    )


def _write_replacement_api_module(root: Path, module_name: str) -> None:
    module_root = root / module_name
    module_root.mkdir()
    (module_root / "__init__.py").write_text(
        dedent(
            """
            provides_api_capability = True
            """
        ),
        encoding="utf-8",
    )


def _app_config(tmp_path: Path, modules: tuple[str, ...]) -> AppConfig:
    route_prefixes = {
        "wybra.web": {},
    }
    return AppConfig(
        config_path=tmp_path / "app.toml",
        project_root=tmp_path,
        modules=modules,
        routes=RouteOptions(
            prefixes={
                module_name: route_prefixes[module_name]
                for module_name in modules
                if module_name in route_prefixes
            }
        ),
        templates=TemplateOptions(auto_reload=True, cache_size=0),
        assets=AssetOptions(url_path="/static/"),
    )


def _web_settings(
    tmp_path: Path,
    modules: tuple[str, ...] = ("wybra.web",),
    raw_config: dict[str, dict[str, object]] | None = None,
) -> WebSettings:
    wybra_web_root = Path(wybra.web.__file__).resolve().parent
    app_config = _app_config(tmp_path, modules)
    if raw_config is not None:
        app_config = replace(app_config, raw_config=raw_config)
    config_defs = [RUNTIME_CONFIG_DEF]
    if "wybra.security" in modules:
        from wybra.security import module_config as security_module_config

        config_defs.append(security_module_config)
    if "wybra.api" in modules:
        from wybra.api import module_config as api_module_config

        config_defs.append(api_module_config)
    config = ConfigService(
        [AppConfigSource(app_config)],
        config_defs=tuple(config_defs),
        discover_module_config=False,
    )
    return WebSettings(
        project_root=tmp_path,
        modules=modules,
        template_root=wybra_web_root / "templates",
        static_root=wybra_web_root / "static",
        app_config=app_config,
        config=config,
    )


def test_tools_modules_do_not_import_auth_or_host_runtime_startup() -> None:
    project_root = Path(__file__).resolve().parents[1]
    forbidden_modules = (
        "wybra.auth",
        "host_app",
    )
    tools_files = sorted((project_root / "src/wybra/tools").rglob("*.py"))

    assert tools_files
    for path in tools_files:
        imported_modules = _imported_modules(path)
        assert not any(
            module == forbidden_module or module.startswith(f"{forbidden_module}.")
            for module in imported_modules
            for forbidden_module in forbidden_modules
        )


def test_tools_validation_modules_do_not_import_host_or_auth_packages() -> None:
    project_root = Path(__file__).resolve().parents[1]
    forbidden_modules = ("wybra.auth", "host_app")
    validation_files = sorted(
        (project_root / "src/wybra/tools/validation").rglob("*.py")
    )

    assert validation_files
    for path in validation_files:
        imported_modules = _imported_modules(path)
        assert not any(
            module == forbidden_module or module.startswith(f"{forbidden_module}.")
            for module in imported_modules
            for forbidden_module in forbidden_modules
        )


def test_runtime_project_root_uses_invoking_project(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "host_project"
    child = project_root / "src/host_app"
    child.mkdir(parents=True)
    (project_root / "pyproject.toml").write_text(
        '[project]\nname = "host-project"\n',
        encoding="utf-8",
    )
    monkeypatch.chdir(child)

    assert runtime_project_root() == project_root.resolve()


def test_runtime_project_root_reports_malformed_pyproject(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "host_project"
    child = project_root / "src/host_app"
    child.mkdir(parents=True)
    pyproject_path = project_root / "pyproject.toml"
    pyproject_path.write_text("[project\n", encoding="utf-8")
    monkeypatch.chdir(child)

    with pytest.raises(ProjectToolConfigurationError, match=str(pyproject_path)):
        runtime_project_root()


def test_validate_routes_checks_configured_route_modules(tmp_path: Path) -> None:
    result = validate_routes(_web_settings(tmp_path))

    assert result.is_ok
    assert result.name == "routes"
    assert any(
        check.description == "configured route modules load: wybra.web"
        for check in result.checks
    )


def test_validate_command_reports_missing_project_configuration(capsys) -> None:
    exit_code = validate_main(["routes"])

    captured = capsys.readouterr()

    assert exit_code == 1
    assert captured.out == ""
    assert "configuration: failed" in captured.err
    assert "Application config file could not be resolved" in captured.err


def test_validate_command_help_returns_cleanly(capsys) -> None:
    exit_code = validate_main(["--help"])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "Run project validation checks" in captured.out
    assert captured.err == ""


def test_validate_command_does_not_mask_unrelated_value_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def raise_unrelated_value_error(_args: object) -> object:
        raise ValueError("programmer error")

    monkeypatch.setattr(validate_module, "_build_settings", raise_unrelated_value_error)

    with pytest.raises(ValueError, match="programmer error"):
        validate_module.main(["routes"])


def test_resolve_targets_raises_domain_error_for_unknown_targets() -> None:
    with pytest.raises(
        validate_module.UnknownValidationTargetError,
        match="Unknown validation target\\(s\\): foo",
    ):
        validate_module._resolve_targets(("foo",), ("web", "environment"))


def test_validation_targets_are_discovered_from_configured_modules(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_validation_module(
        tmp_path,
        "first_validation_module",
        """
        from wybra.tools.validation.core import ValidationResult

        def validate_first(settings):
            return ValidationResult(name="first", errors=())

        validation_targets = {"first": validate_first}
        """,
    )
    _write_validation_module(
        tmp_path,
        "second_validation_module",
        """
        from wybra.tools.validation.core import ValidationResult

        def validate_second(settings):
            return ValidationResult(name="second", errors=())

        validation_targets = {"second": validate_second}
        """,
    )
    monkeypatch.syspath_prepend(str(tmp_path))

    targets = discover_validation_targets(
        ("first_validation_module", "second_validation_module")
    )

    assert tuple(targets) == ("first", "second")
    assert isinstance(targets["first"](object()), ValidationResult)


def test_validation_target_details_include_origins(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_validation_module(
        tmp_path,
        "origin_validation_module",
        """
        from wybra.tools.validation.core import ValidationResult

        def validate_origin(settings):
            return ValidationResult(name="origin", errors=())

        validation_targets = {"origin": validate_origin}
        """,
    )
    monkeypatch.syspath_prepend(str(tmp_path))

    details = discover_validation_target_details(("origin_validation_module",))

    assert tuple(details.targets) == ("origin",)
    assert details.origins == {"origin": "origin_validation_module.validation"}


def test_unlisted_module_validation_targets_are_not_discovered(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_validation_module(
        tmp_path,
        "listed_validation_module",
        """
        from wybra.tools.validation.core import ValidationResult

        def validate_listed(settings):
            return ValidationResult(name="listed", errors=())

        validation_targets = {"listed": validate_listed}
        """,
    )
    _write_validation_module(
        tmp_path,
        "unlisted_validation_module",
        """
        from wybra.tools.validation.core import ValidationResult

        def validate_unlisted(settings):
            return ValidationResult(name="unlisted", errors=())

        validation_targets = {"unlisted": validate_unlisted}
        """,
    )
    monkeypatch.syspath_prepend(str(tmp_path))

    targets = discover_validation_targets(("listed_validation_module",))

    assert tuple(targets) == ("listed",)
    assert "unlisted" not in targets


def test_malformed_validation_surface_fails_clearly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_validation_module(
        tmp_path,
        "malformed_validation_module",
        """
        validation_targets = {"broken": object()}
        """,
    )
    monkeypatch.syspath_prepend(str(tmp_path))

    with pytest.raises(ValidationDiscoveryError, match="must be callable"):
        discover_validation_targets(("malformed_validation_module",))


def test_validate_command_runs_discovered_module_targets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_validation_module(
        tmp_path,
        "command_validation_module",
        """
        from wybra.tools.validation.core import ValidationResult

        def validate_command_target(settings):
            return ValidationResult(name="command-target", errors=())

        validation_targets = {"command-target": validate_command_target}
        """,
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    settings = _web_settings(tmp_path, ("command_validation_module",))
    monkeypatch.setattr(
        validate_module,
        "_build_settings",
        lambda _overrides: settings,
    )

    exit_code = validate_main([])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.out == (
        "api: ok\nassets: ok\nforms: ok\nroutes: ok\nsecurity: ok\ntemplate: ok\n"
        "command-target: ok\n"
    )
    assert captured.err == ""


def test_validate_security_accepts_omitted_security_module(tmp_path: Path) -> None:
    result = validate_security(_web_settings(tmp_path, ("wybra.web",)))

    assert result.is_ok
    assert any(
        check.description == "security module is not configured"
        for check in result.checks
    )


def test_validate_security_loads_configured_security_module(tmp_path: Path) -> None:
    result = validate_security(
        _web_settings(
            tmp_path,
            ("wybra.security", "wybra.web"),
            raw_config={
                "app.security": {
                    "cross_origin_opener_policy": "same-origin-allow-popups",
                },
                "app.assets.cors": {
                    "enabled": True,
                    "allow_origins": ("https://example.com",),
                },
            },
        )
    )

    assert result.is_ok
    assert any("security settings load" in check.description for check in result.checks)


def test_validate_security_accepts_replacement_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_replacement_security_module(tmp_path, "replacement_security")
    monkeypatch.syspath_prepend(str(tmp_path))

    result = validate_security(
        _web_settings(tmp_path, ("replacement_security", "wybra.web"))
    )

    assert result.is_ok
    assert any(
        check.description == "replacement security capability provider is configured"
        for check in result.checks
    )


def test_validate_command_rejects_module_target_conflicting_with_builtin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_validation_module(
        tmp_path,
        "conflicting_validation_module",
        """
        from wybra.tools.validation.core import ValidationResult

        def validate_assets(settings):
            return ValidationResult(name="assets", errors=())

        validation_targets = {"assets": validate_assets}
        """,
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    settings = _web_settings(tmp_path, ("conflicting_validation_module",))
    monkeypatch.setattr(
        validate_module,
        "_build_settings",
        lambda _overrides: settings,
    )

    exit_code = validate_main([])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert captured.out == ""
    assert "configuration: failed" in captured.err
    assert "assets from conflicting_validation_module.validation" in captured.err
    assert "Built-in validation targets cannot be overridden" in captured.err


def test_validate_api_accepts_omitted_api_module(tmp_path: Path) -> None:
    result = validate_api(_web_settings(tmp_path, ("wybra.web",)))

    assert result.is_ok
    assert any(
        check.description == "api module is not configured" for check in result.checks
    )


def test_validate_api_loads_configured_api_module(tmp_path: Path) -> None:
    result = validate_api(
        _web_settings(
            tmp_path,
            ("wybra.api", "wybra.web"),
            raw_config={
                "app.api": {
                    "path_prefix": "/service",
                    "paging_link_mode": "request_path",
                },
            },
        )
    )

    assert result.is_ok
    assert any("API settings load" in check.description for check in result.checks)
    assert any(
        check.description == "API path prefix is configured" and check.passed
        for check in result.checks
    )


def test_validate_api_accepts_replacement_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_replacement_api_module(tmp_path, "replacement_api")
    monkeypatch.syspath_prepend(str(tmp_path))

    result = validate_api(_web_settings(tmp_path, ("replacement_api", "wybra.web")))

    assert result.is_ok
    assert any(
        check.description == "replacement API capability provider is configured"
        for check in result.checks
    )


def test_validate_command_exposes_builtin_api_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings = _web_settings(tmp_path, ("wybra.api", "wybra.web"))
    monkeypatch.setattr(validate_module, "_build_settings", lambda _overrides: settings)

    exit_code = validate_main(["api"])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.out == "api: ok\n"
    assert captured.err == ""


def test_validate_command_reports_malformed_validation_surface(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_validation_module(
        tmp_path,
        "command_malformed_validation_module",
        """
        validation_targets = ["not", "a", "mapping"]
        """,
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(
        validate_module,
        "_build_settings",
        lambda _overrides: SimpleNamespace(
            modules=("command_malformed_validation_module",),
        ),
    )

    exit_code = validate_main([])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert captured.out == ""
    assert "validation discovery: failed" in captured.err
    assert "must expose `validation_targets` as a mapping" in captured.err


def test_validate_command_unknown_target_returns_usage_error(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        validate_module,
        "_build_settings",
        lambda _overrides: SimpleNamespace(modules=("wybra.web",)),
    )

    exit_code = validate_main(["foo"])

    captured = capsys.readouterr()
    assert exit_code == 2
    assert captured.out == ""
    assert "Unknown validation target(s): foo" in captured.err


def test_validate_main_treats_falsy_click_exception_as_failure(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class FalsyExitClickException(click.ClickException):
        exit_code = 0

    def raise_click_exception(*_args, **_kwargs) -> None:
        raise FalsyExitClickException("invalid usage")

    monkeypatch.setattr(validate_module.validate_command, "main", raise_click_exception)

    assert validate_main([]) == 1

    captured = capsys.readouterr()
    assert "invalid usage" in captured.err


def test_project_settings_do_not_treat_asset_root_as_runtime_static_root(
    tmp_path: Path,
) -> None:
    config = ConfigService([AppConfigSource(_app_config(tmp_path, ("wybra.web",)))])

    settings = ProjectSettings.load_settings(
        config,
        app_config=_app_config(tmp_path, ("wybra.web",)),
    )

    assert settings.static_root is None
    assert not settings.uses_filesystem_static_root


def test_validate_assets_accepts_missing_creatable_default_asset_root(
    tmp_path: Path,
) -> None:
    result = validate_assets(_web_settings(tmp_path, ("wybra.assets", "wybra.web")))

    assert result.is_ok
    assert result.name == "assets"
    assert not (tmp_path / "static").exists()
    assert any(
        check.description
        == f"static asset collection root is usable: {(tmp_path / 'static').resolve()}"
        and check.passed
        for check in result.checks
    )


def test_validate_assets_reads_static_url_path_from_app_config(
    tmp_path: Path,
) -> None:
    settings = AssetValidationOnlySettings(
        project_root=tmp_path,
        modules=("wybra.assets",),
        static_root=None,
        app_config=_app_config(tmp_path, ("wybra.assets",)),
    )

    result = validate_assets(settings)

    assert result.is_ok
    assert any(
        check.description == "static URL path is configured: /static/" and check.passed
        for check in result.checks
    )


def test_validate_assets_reports_blank_app_config_static_url_path(
    tmp_path: Path,
) -> None:
    app_config = replace(
        _app_config(tmp_path, ("wybra.assets",)),
        assets=AssetOptions(url_path=""),
    )
    settings = AssetValidationOnlySettings(
        project_root=tmp_path,
        modules=("wybra.assets",),
        static_root=None,
        app_config=app_config,
    )

    result = validate_assets(settings)

    assert not result.is_ok
    assert "Static URL path must not be empty." in result.errors


def test_validate_assets_reports_asset_root_createability_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _web_settings(tmp_path, ("wybra.assets", "wybra.web"))
    monkeypatch.setattr(asset_validation.os, "access", lambda *_args: False)

    result = validate_assets(settings)

    assert not result.is_ok
    assert any(
        error
        == (
            "Static asset collection root cannot be created because parent is "
            f"not writable: {tmp_path.resolve()}"
        )
        for error in result.errors
    )


def test_validate_assets_reports_asset_root_existing_file(
    tmp_path: Path,
) -> None:
    app_config = replace(
        _app_config(tmp_path, ("wybra.assets", "wybra.web")),
        assets=AssetOptions(url_path="/static/", root=Path("asset-file")),
    )
    (tmp_path / "asset-file").write_text("not a directory", encoding="utf-8")
    settings = replace(
        _web_settings(tmp_path, ("wybra.assets", "wybra.web")),
        app_config=app_config,
    )

    result = validate_assets(settings)

    assert not result.is_ok
    assert (
        "Static asset collection root is not a directory: "
        f"{(tmp_path / 'asset-file').resolve()}"
    ) in result.errors


def test_validate_assets_accepts_web_without_asset_provider(tmp_path: Path) -> None:
    result = validate_assets(_web_settings(tmp_path, ("wybra.web",)))

    assert result.is_ok
    assert any(
        check.description == "static asset capability provider is not required"
        and check.passed
        for check in result.checks
    )


def test_validate_assets_accepts_provider_after_web(tmp_path: Path) -> None:
    result = validate_assets(_web_settings(tmp_path, ("wybra.web", "wybra.assets")))

    assert result.is_ok
    assert any(
        check.description == "static asset capability provider is configured"
        and check.passed
        for check in result.checks
    )


def test_validate_assets_accepts_marked_provider_before_web(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module_root = tmp_path / "custom_asset_provider"
    module_root.mkdir()
    (module_root / "__init__.py").write_text(
        "provides_static_asset_capability = True\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))

    result = validate_assets(
        _web_settings(tmp_path, ("custom_asset_provider", "wybra.web"))
    )

    assert result.is_ok


def test_validate_command_exposes_builtin_assets_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings = _web_settings(tmp_path, ("wybra.assets", "wybra.web"))
    monkeypatch.setattr(validate_module, "_build_settings", lambda _overrides: settings)

    exit_code = validate_main(["assets"])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.out == "assets: ok\n"
    assert captured.err == ""


def test_validate_post_form_detection_accepts_html_attribute_variants() -> None:
    assert _contains_post_form('<form method="post"></form>')
    assert _contains_post_form('<form class="x" method="POST"></form>')
    assert _contains_post_form('<form method=" post "></form>')
    assert not _contains_post_form('<form method="get"></form>')
    assert not _contains_post_form("<form></form>")


def test_validate_template_rejects_post_form_missing_csrf_field_from_module_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package_root = tmp_path / "csrf_missing_app"
    template_root = package_root / "templates"
    template_root.mkdir(parents=True)
    (package_root / "__init__.py").write_text("", encoding="utf-8")
    (template_root / "missing_csrf.html").write_text(
        '<form method="post"></form>',
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))

    result = validate_template(
        _web_settings(tmp_path, ("csrf_missing_app", "wybra.template"))
    )

    assert not result.is_ok
    assert result.name == "template"
    assert "POST form template must include CSRF field: missing_csrf.html" in (
        result.errors
    )


def test_validate_template_rejects_post_form_missing_csrf_field(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package_root = tmp_path / "template_csrf_missing_app"
    template_root = package_root / "templates"
    template_root.mkdir(parents=True)
    (package_root / "__init__.py").write_text("", encoding="utf-8")
    (template_root / "missing_csrf.html").write_text(
        '<form method="post"></form>',
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))

    result = validate_template(
        _web_settings(tmp_path, ("template_csrf_missing_app", "wybra.template"))
    )

    assert not result.is_ok
    assert result.name == "template"
    assert "POST form template must include CSRF field: missing_csrf.html" in (
        result.errors
    )


def test_validate_template_accepts_omitted_template_provider(
    tmp_path: Path,
) -> None:
    result = validate_template(_web_settings(tmp_path, ("wybra.web",)))

    assert result.is_ok
    assert result.name == "template"


def test_validate_template_accepts_replacement_template_provider_sources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package_root = tmp_path / "replacement_template_provider"
    template_root = package_root / "templates"
    template_root.mkdir(parents=True)
    (package_root / "__init__.py").write_text("", encoding="utf-8")
    (template_root / "page.html").write_text("<main>ok</main>", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))

    result = validate_template(
        _web_settings(tmp_path, ("replacement_template_provider",))
    )

    assert result.is_ok
    assert any(
        check.description == "template loads: page.html" for check in result.checks
    )


def test_validate_template_reuses_renderer_for_all_template_loads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package_root = tmp_path / "multi_template_provider"
    template_root = package_root / "templates"
    template_root.mkdir(parents=True)
    (package_root / "__init__.py").write_text("", encoding="utf-8")
    (template_root / "first.html").write_text("<main>first</main>", encoding="utf-8")
    (template_root / "second.html").write_text("<main>second</main>", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    original_template_renderer = template_validation._template_renderer
    renderer_calls = 0

    def count_template_renderer(settings, template_sources):
        nonlocal renderer_calls
        renderer_calls += 1
        return original_template_renderer(settings, template_sources)

    monkeypatch.setattr(
        template_validation,
        "_template_renderer",
        count_template_renderer,
    )

    result = validate_template(_web_settings(tmp_path, ("multi_template_provider",)))

    assert result.is_ok
    assert renderer_calls == 1
    assert any(
        check.description == "template loads: first.html" for check in result.checks
    )
    assert any(
        check.description == "template loads: second.html" for check in result.checks
    )


def test_validate_template_does_not_require_framework_stylesheet(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package_root = tmp_path / "unstyled_template_app"
    template_root = package_root / "templates"
    template_root.mkdir(parents=True)
    (package_root / "__init__.py").write_text("", encoding="utf-8")
    (template_root / "page.html").write_text("<main>ok</main>", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))

    result = validate_template(_web_settings(tmp_path, ("unstyled_template_app",)))

    assert result.is_ok
    assert not any("styles/app.css" in error for error in result.errors)
    assert not any("styles/app.css" in check.description for check in result.checks)


def test_validate_widgets_checks_theme_resources(tmp_path: Path) -> None:
    result = validate_widgets(_web_settings(tmp_path, ("wybra.widgets",)))

    assert result.is_ok
    assert any(
        check.description == "widget template exists: components/theme_selector.html"
        for check in result.checks
    )
    assert any(
        check.description == "widget static asset exists: styles/widgets.css"
        for check in result.checks
    )


def test_validate_widgets_checks_login_resources_when_enabled(tmp_path: Path) -> None:
    result = validate_widgets(
        _web_settings(
            tmp_path,
            ("wybra.widgets",),
            raw_config={"wybra.widgets": {"features": ["login"]}},
        )
    )

    assert result.is_ok
    assert any(
        check.description == "widget template exists: components/login_control.html"
        for check in result.checks
    )


def test_validate_widgets_accepts_absent_widgets_module(tmp_path: Path) -> None:
    result = validate_widgets(_web_settings(tmp_path, ("wybra.web",)))

    assert result.is_ok
    assert any(
        check.description == "wybra.widgets is not configured"
        for check in result.checks
    )


def test_validate_routes_reports_missing_configured_module(tmp_path: Path) -> None:
    result = validate_routes(_web_settings(tmp_path, ("missing_validation_app",)))

    assert not result.is_ok
    assert any(
        "Configured module 'missing_validation_app' could not be imported" in error
        for error in result.errors
    )


def test_validate_command_reports_host_configuration_error(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def raise_configuration_error(_overrides: object) -> object:
        raise ProjectToolConfigurationError("host adapter is invalid")

    monkeypatch.setattr(validate_module, "_build_settings", raise_configuration_error)

    exit_code = validate_main(["routes"])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert captured.out == ""
    assert "configuration: failed" in captured.err
    assert "host adapter is invalid" in captured.err
