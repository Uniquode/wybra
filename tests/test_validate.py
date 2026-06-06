import ast
from dataclasses import dataclass
from pathlib import Path
from textwrap import dedent
from types import SimpleNamespace

import click
import pytest

import wevra.tools.validate as validate_module
import wevra.web
from wevra.core.composition import (
    AppConfig,
    RouteOptions,
    StaticOptions,
    TemplateOptions,
)
from wevra.tools.project import ProjectToolConfigurationError, runtime_project_root
from wevra.tools.validate import main as validate_main
from wevra.tools.validation.core import ValidationResult
from wevra.tools.validation.registry import (
    ValidationDiscoveryError,
    discover_validation_targets,
)
from wevra.web.validation import _contains_post_form, validate_web


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
    uses_filesystem_template_root: bool = False
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


def _app_config(tmp_path: Path, modules: tuple[str, ...]) -> AppConfig:
    return AppConfig(
        config_path=tmp_path / "app.toml",
        project_root=tmp_path,
        modules=modules,
        routes=RouteOptions(prefixes={}),
        templates=TemplateOptions(auto_reload=True, cache_size=0),
        static=StaticOptions(url_path="/static/", export_root=Path("static")),
    )


def _web_settings(
    tmp_path: Path,
    modules: tuple[str, ...] = ("wevra.web",),
) -> WebSettings:
    wevra_web_root = Path(wevra.web.__file__).resolve().parent
    return WebSettings(
        project_root=tmp_path,
        modules=modules,
        template_root=wevra_web_root / "templates",
        static_root=wevra_web_root / "static",
        app_config=_app_config(tmp_path, modules),
    )


def test_tools_modules_do_not_import_auth_or_host_runtime_startup() -> None:
    project_root = Path(__file__).resolve().parents[1]
    forbidden_modules = (
        "wevra.auth",
        "host_app",
    )
    tools_files = sorted((project_root / "src/wevra/tools").rglob("*.py"))

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
    forbidden_modules = ("wevra.auth", "host_app")
    validation_files = sorted(
        (project_root / "src/wevra/tools/validation").rglob("*.py")
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


def test_validate_web_checks_framework_web_foundation(tmp_path: Path) -> None:
    result = validate_web(_web_settings(tmp_path))

    assert result.is_ok
    assert result.name == "web"
    assert any(
        check.description == "configured module surfaces load: wevra.web"
        for check in result.checks
    )


def test_validate_command_reports_missing_host_adapter_configuration(capsys) -> None:
    exit_code = validate_main(["web"])

    captured = capsys.readouterr()

    assert exit_code == 1
    assert captured.out == ""
    assert "configuration: failed" in captured.err
    assert "[tool.wevra].settings_loader" in captured.err


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
        validate_module.main(["web"])


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
        from wevra.tools.validation.core import ValidationResult

        def validate_first(settings):
            return ValidationResult(name="first", errors=())

        validation_targets = {"first": validate_first}
        """,
    )
    _write_validation_module(
        tmp_path,
        "second_validation_module",
        """
        from wevra.tools.validation.core import ValidationResult

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


def test_unlisted_module_validation_targets_are_not_discovered(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_validation_module(
        tmp_path,
        "listed_validation_module",
        """
        from wevra.tools.validation.core import ValidationResult

        def validate_listed(settings):
            return ValidationResult(name="listed", errors=())

        validation_targets = {"listed": validate_listed}
        """,
    )
    _write_validation_module(
        tmp_path,
        "unlisted_validation_module",
        """
        from wevra.tools.validation.core import ValidationResult

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
        from wevra.tools.validation.core import ValidationResult

        def validate_command_target(settings):
            return ValidationResult(name="command-target", errors=())

        validation_targets = {"command-target": validate_command_target}
        """,
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(
        validate_module,
        "_build_settings",
        lambda _overrides: SimpleNamespace(modules=("command_validation_module",)),
    )

    exit_code = validate_main([])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.out == "command-target: ok\n"
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
        lambda _overrides: SimpleNamespace(modules=("wevra.web",)),
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


def test_validate_web_omitting_wevra_web_does_not_use_default_static_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package_root = tmp_path / "staticless_app"
    package_root.mkdir()
    (package_root / "__init__.py").write_text("", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))

    result = validate_web(_web_settings(tmp_path, ("staticless_app",)))

    assert not result.is_ok
    assert "Missing static asset: styles/app.css" in result.errors


def test_validate_post_form_detection_accepts_html_attribute_variants() -> None:
    assert _contains_post_form('<form method="post"></form>')
    assert _contains_post_form('<form class="x" method="POST"></form>')
    assert _contains_post_form('<form method=" post "></form>')
    assert not _contains_post_form('<form method="get"></form>')
    assert not _contains_post_form("<form></form>")


def test_validate_web_rejects_post_form_missing_csrf_field(
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
    (package_root / "routes.py").write_text(
        dedent(
            """
            from fastapi.responses import Response
            from wevra.web.routes import HtmlRouteDefinition, ModuleRoutes

            class View:
                template_name = "missing_csrf.html"

                async def render(self, request, renderer):
                    del request, renderer
                    return Response()

            module_routes = ModuleRoutes(
                page_routes=(
                    HtmlRouteDefinition(
                        path="/form",
                        name="form",
                        methods=("GET",),
                        surface="page",
                        view=View(),
                    ),
                ),
            )
            """
        ),
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))

    result = validate_web(_web_settings(tmp_path, ("csrf_missing_app", "wevra.web")))

    assert not result.is_ok
    assert "POST form template must include CSRF field: missing_csrf.html" in (
        result.errors
    )


def test_validate_web_rejects_stylesheet_missing_theme_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package_root = tmp_path / "theme_contract_app"
    stylesheet_root = package_root / "static/styles"
    stylesheet_root.mkdir(parents=True)
    (package_root / "__init__.py").write_text("", encoding="utf-8")
    (stylesheet_root / "app.css").write_text(":root {}", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))

    result = validate_web(_web_settings(tmp_path, ("theme_contract_app",)))

    assert not result.is_ok
    assert any("Missing theme token" in error for error in result.errors)
    assert any("Missing theme selector" in error for error in result.errors)


def test_validate_web_reports_missing_configured_module(tmp_path: Path) -> None:
    result = validate_web(_web_settings(tmp_path, ("missing_validation_app",)))

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

    exit_code = validate_main(["web"])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert captured.out == ""
    assert "configuration: failed" in captured.err
    assert "host adapter is invalid" in captured.err
