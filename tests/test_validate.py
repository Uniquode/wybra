import ast
from pathlib import Path
from shutil import copytree
from textwrap import dedent

import click
import pytest
from click.testing import CliRunner

import uniquode.validation.environment as environment_validation
import wevra.tools.validate as validate_module
from uniquode.configuration import ConfigurationError
from uniquode.settings import Settings
from uniquode.validation.persistence import validate_persistence
from wevra.core.composition import (
    AppConfig,
    RouteOptions,
    StaticOptions,
    TemplateOptions,
)
from wevra.tools.validate import main as validate_main
from wevra.tools.validation.core import ValidationResult
from wevra.tools.validation.registry import (
    ValidationDiscoveryError,
    discover_validation_targets,
)
from wevra.web.validation import _contains_post_form, validate_web


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


def test_tools_modules_do_not_import_auth_or_runtime_startup() -> None:
    project_root = Path(__file__).resolve().parents[1]
    forbidden_modules = (
        "wevra.auth",
        "uniquode.app",
        "uniquode.asgi",
        "uniquode.routes",
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


def test_tools_validation_modules_do_not_import_application_or_auth_packages() -> None:
    project_root = Path(__file__).resolve().parents[1]
    forbidden_modules = ("wevra.auth", "uniquode")
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


def test_validate_command_checks_web_foundation(capsys) -> None:
    exit_code = validate_main(["web"])

    captured = capsys.readouterr()

    assert exit_code == 0
    assert "web: ok" in captured.out


def test_validate_command_checks_persistence_foundation(capsys) -> None:
    exit_code = validate_main(["persistence"])

    captured = capsys.readouterr()

    assert exit_code == 0
    assert "persistence: ok" in captured.out


def test_validate_persistence_allows_no_model_composition(tmp_path: Path) -> None:
    result = validate_persistence(
        Settings(app_config=_app_config(tmp_path, ("uniquode", "wevra.web")))
    )

    assert result.is_ok
    assert "At least one Alembic migration revision is required." not in result.errors


def test_validate_command_checks_environment_configuration(capsys) -> None:
    exit_code = validate_main(["environment"])

    captured = capsys.readouterr()

    assert exit_code == 0
    assert "environment: ok" in captured.out


def test_validate_command_default_runs_registered_targets(capsys) -> None:
    exit_code = validate_main([])

    captured = capsys.readouterr()

    assert exit_code == 0
    assert "environment: ok" in captured.out
    assert "web: ok" in captured.out
    assert "persistence: ok" in captured.out


def test_validate_command_help_returns_cleanly(capsys) -> None:
    exit_code = validate_main(["--help"])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "Run project validation checks" in captured.out
    assert captured.err == ""


def test_validate_command_verbose_lists_registered_checks(capsys) -> None:
    exit_code = validate_main(["--verbose"])

    captured = capsys.readouterr()

    assert exit_code == 0
    assert "environment: ok" in captured.out
    assert "web: ok" in captured.out
    assert "persistence: ok" in captured.out
    assert "ok: supported environment variable name is valid: APP_ENV" in captured.out
    assert (
        "ok: supported environment variable name is valid: APP_RELOAD" in captured.out
    )
    assert "ok: supported environment variable names are unique" in captured.out
    assert "ok: environment loader returns an envex Env instance" in captured.out
    assert "ok: template root exists:" in captured.out
    assert (
        "ok: configured module surfaces load: uniquode, wevra.web, wevra.auth"
        in captured.out
    )
    assert "ok: template context providers validate" in captured.out
    assert "ok: module routes compose" in captured.out
    assert "ok: route template exists: public:home -> public/pages/home.html" in (
        captured.out
    )
    assert "ok: route template exists: identity:login -> identity/pages/login.html" in (
        captured.out
    )
    assert "ok: POST form CSRF field exists: identity/pages/login.html" in (
        captured.out
    )
    assert "ok: static asset exists: styles/app.css" in captured.out
    assert "ok: theme token present: --web-core-colour-page-bg" in captured.out
    assert "ok: default database URL uses persistent SQLite file:" in captured.out
    assert "ok: database URL uses supported async SQLAlchemy driver" in captured.out
    assert "ok: Alembic config exists:" in captured.out
    assert "ok: Alembic config does not force in-memory SQLite" in captured.out
    assert "ok: Alembic migration file exists: env.py" in captured.out
    assert "ok: module migration version locations exist:" in captured.out
    assert "ok: Alembic migration revision exists" in captured.out
    assert "ok: Alembic migration creates table: identity_user" in captured.out
    assert "ok: development database initialisation command is available:" in (
        captured.out
    )
    assert "uv run migrate upgrade" in captured.out


def test_validate_environment_verbose_redacts_database_url_password(
    capsys,
    monkeypatch,
) -> None:
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql+asyncpg://user:password@host.example/app",
    )

    exit_code = validate_main(["environment", "--verbose"])

    captured = capsys.readouterr()

    assert exit_code == 0
    assert "postgresql+asyncpg://***:***@host.example/app" in captured.out
    assert "postgresql+asyncpg://user:password@host.example/app" not in captured.out
    assert "password" not in captured.out


def test_validate_command_reports_invalid_environment_configuration(
    capsys,
    monkeypatch,
) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.delenv("CSRF_SECRET", raising=False)
    monkeypatch.delenv("RESET_SECRET", raising=False)
    monkeypatch.delenv("VERIFICATION_SECRET", raising=False)

    exit_code = validate_main(["environment"])

    captured = capsys.readouterr()

    assert exit_code == 1
    assert captured.out == ""
    assert "configuration: failed" in captured.err
    assert "Non-local deployments must configure identity reset" in captured.err


def test_validate_command_does_not_mask_unrelated_value_errors(monkeypatch) -> None:
    def raise_unrelated_value_error(_args: object) -> Settings:
        raise ValueError("programmer error")

    monkeypatch.setattr(validate_module, "_build_settings", raise_unrelated_value_error)

    with pytest.raises(ValueError, match="programmer error"):
        validate_module.main(["environment"])


def test_validate_environment_loader_error_does_not_emit_exception_detail(
    monkeypatch,
) -> None:
    def raise_sensitive_error(**_kwargs: object) -> None:
        raise ConfigurationError(
            "Environment loader failed while initialising envex (RuntimeError)."
        )

    monkeypatch.setattr(
        environment_validation, "load_environment", raise_sensitive_error
    )

    result = environment_validation.validate_environment(Settings())

    assert not result.is_ok
    assert result.errors == (
        "Environment loader failed while initialising envex (RuntimeError).",
    )
    assert "secret" not in "\n".join(result.errors)
    assert "DATABASE_URL" not in "\n".join(result.errors)


def test_validate_environment_reports_wrong_loader_return_type(monkeypatch) -> None:
    monkeypatch.setattr(
        environment_validation,
        "load_environment",
        lambda **_kwargs: object(),
    )

    result = environment_validation.validate_environment(Settings())

    assert not result.is_ok
    assert result.errors == ("Environment loader returned object; expected envex Env.",)


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
    assert isinstance(targets["first"](Settings()), ValidationResult)


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
    capsys,
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
        lambda _overrides: Settings(
            project_root=tmp_path,
            app_config=_app_config(tmp_path, ("command_validation_module",)),
        ),
    )

    exit_code = validate_main([])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.out == "command-target: ok\n"
    assert captured.err == ""


def test_validate_command_reports_malformed_validation_surface(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
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
        lambda _overrides: Settings(
            project_root=tmp_path,
            app_config=_app_config(tmp_path, ("command_malformed_validation_module",)),
        ),
    )

    exit_code = validate_main([])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert captured.out == ""
    assert "validation discovery: failed" in captured.err
    assert "must expose `validation_targets` as a mapping" in captured.err


def test_validate_command_unknown_target_returns_usage_error(capsys) -> None:
    exit_code = validate_main(["foo"])

    captured = capsys.readouterr()
    assert exit_code == 2
    assert captured.out == ""
    assert "Unknown validation target(s): foo" in captured.err


def test_validate_click_command_reports_unknown_target() -> None:
    result = CliRunner().invoke(validate_module.validate_command, ["foo"])

    assert result.exit_code == 2
    assert "Unknown validation target(s): foo" in result.output


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


def test_validate_command_accepts_normalisable_static_url_path(capsys) -> None:
    exit_code = validate_main(["web", "--static-url-path", "static"])

    captured = capsys.readouterr()

    assert exit_code == 0
    assert "web: ok" in captured.out


def test_validate_command_rejects_blank_static_url_path(capsys) -> None:
    exit_code = validate_main(["web", "--static-url-path", "   "])

    captured = capsys.readouterr()

    assert exit_code == 1
    assert captured.out == ""
    assert "Static URL path must not be empty." in captured.err


def test_validate_web_omitting_wevra_web_does_not_use_default_static_root(
    tmp_path: Path,
) -> None:
    result = validate_web(
        Settings(
            project_root=tmp_path,
            app_config=_app_config(tmp_path, ("uniquode",)),
        )
    )

    assert not result.is_ok
    assert "Missing static asset: styles/app.css" in result.errors


def test_validate_post_form_detection_accepts_html_attribute_variants() -> None:
    assert _contains_post_form("<form METHOD='POST' action='/login'>")
    assert _contains_post_form('<form action="/login" method = "post">')
    assert _contains_post_form("<form action=/login method=post>")
    assert _contains_post_form(
        """
        <form
          action="/login"
          method = " POST "
        >
        """
    )
    assert not _contains_post_form('<form method="get" action="/login">')
    assert not _contains_post_form('<form method="postish" action="/login">')
    assert not _contains_post_form('<form data-method="post" action="/login">')
    assert not _contains_post_form('<form method="post"')


def test_validate_web_rejects_post_form_missing_csrf_field(tmp_path) -> None:
    source_root = Path(__file__).resolve().parents[1]
    template_root = tmp_path / "templates"
    static_root = tmp_path / "static"
    copytree(source_root / "src/wevra/web/templates", template_root)
    copytree(source_root / "src/wevra/web/static", static_root)
    (template_root / "public/pages").mkdir(parents=True)
    (template_root / "public/pages/home.html").write_text(
        (source_root / "src/uniquode/templates/public/pages/home.html").read_text(
            encoding="utf-8"
        ),
        encoding="utf-8",
    )
    (template_root / "identity/pages").mkdir(parents=True)
    (template_root / "identity/pages/login.html").write_text(
        """
        <form method="post" action="/login">
          <input name="email" type="email">
          <button type="submit">Sign in</button>
        </form>
        """,
        encoding="utf-8",
    )

    result = validate_web(
        Settings(
            template_root=template_root,
            static_root=static_root,
        )
    )

    assert not result.is_ok
    assert any(
        "POST form template must include CSRF field" in error for error in result.errors
    )


def test_validate_web_reports_missing_configured_module(tmp_path) -> None:
    settings = Settings(
        project_root=tmp_path,
        app_config=AppConfig(
            config_path=tmp_path / "app.toml",
            project_root=tmp_path,
            modules=("missing_validation_app",),
            routes=RouteOptions(prefixes={}),
            templates=TemplateOptions(auto_reload=True, cache_size=0),
            static=StaticOptions(url_path="/static/", export_root=Path("static")),
        ),
    )

    result = validate_web(settings)

    assert not result.is_ok
    assert result.errors == (
        "Configured module surface validation failed: Configured module "
        "'missing_validation_app' could not be imported.",
    )


def test_validate_command_rejects_unsupported_database_url(capsys) -> None:
    exit_code = validate_main(["persistence", "--database-url", "sqlite://:memory:"])

    captured = capsys.readouterr()

    assert exit_code == 1
    assert captured.out == ""
    assert "Database URL must use sqlite+aiosqlite:// or postgresql+asyncpg://" in (
        captured.err
    )


def test_validate_command_rejects_empty_database_url_override(capsys) -> None:
    exit_code = validate_main(["persistence", "--database-url", ""])

    captured = capsys.readouterr()

    assert exit_code == 1
    assert captured.out == ""
    assert "Database URL must not be empty." in captured.err


def test_validate_command_verbose_lists_failed_checks(capsys) -> None:
    exit_code = validate_main(
        ["persistence", "--verbose", "--database-url", "sqlite://:memory:"]
    )

    captured = capsys.readouterr()

    assert exit_code == 1
    assert captured.out == ""
    assert "persistence: failed" in captured.err
    assert "ok: database URL is configured: sqlite://:memory:" in captured.err
    assert "failed: database URL uses supported async SQLAlchemy driver" in (
        captured.err
    )
    assert "Database URL must use sqlite+aiosqlite:// or postgresql+asyncpg://" in (
        captured.err
    )


def test_validate_command_redacts_database_url_password(capsys) -> None:
    exit_code = validate_main(
        [
            "persistence",
            "--verbose",
            "--database-url",
            "postgresql+asyncpg://user:password@host.example/app",
        ]
    )

    captured = capsys.readouterr()

    assert exit_code == 0
    assert "postgresql+asyncpg://***:***@host.example/app" in captured.out
    assert "postgresql+asyncpg://user:password@host.example/app" not in captured.out
    assert "password" not in captured.out


def test_validate_command_redacts_database_url_username_without_password(
    capsys,
) -> None:
    exit_code = validate_main(
        [
            "persistence",
            "--verbose",
            "--database-url",
            "postgresql+asyncpg://alice@host.example/app",
        ]
    )

    captured = capsys.readouterr()

    assert exit_code == 0
    assert "postgresql+asyncpg://***@host.example/app" in captured.out
    assert "postgresql+asyncpg://alice@host.example/app" not in captured.out
    assert "alice" not in captured.out


def test_validate_command_reports_missing_alembic_structure(tmp_path, capsys) -> None:
    exit_code = validate_main(
        [
            "persistence",
            "--migrations-root",
            str(tmp_path / "missing-migrations"),
            "--alembic-config",
            str(tmp_path / "missing-alembic.ini"),
        ]
    )

    captured = capsys.readouterr()

    assert exit_code == 1
    assert captured.out == ""
    assert "Missing Alembic config" in captured.err
    assert "Missing Alembic migrations root" in captured.err


def test_validate_command_reports_missing_templates(tmp_path, capsys) -> None:
    settings = Settings(
        template_root=tmp_path / "templates",
        static_root=tmp_path / "static",
    )
    settings.static_root.mkdir()

    exit_code = validate_main(
        [
            "web",
            "--template-root",
            str(settings.template_root),
            "--static-root",
            str(settings.static_root),
        ]
    )

    captured = capsys.readouterr()

    assert exit_code == 1
    assert captured.out == ""
    assert "Missing template" in captured.err


def test_validate_command_reports_template_decode_errors(tmp_path, capsys) -> None:
    template_root = tmp_path / "templates"
    static_root = tmp_path / "static"
    (template_root / "identity/pages").mkdir(parents=True)
    static_root.mkdir()
    (template_root / "identity/pages/login.html").write_bytes(b"\xff")

    exit_code = validate_main(
        [
            "web",
            "--template-root",
            str(template_root),
            "--static-root",
            str(static_root),
        ]
    )

    captured = capsys.readouterr()

    assert exit_code == 1
    assert captured.out == ""
    assert "Unable to read" in captured.err
    assert "identity/pages/login.html" in captured.err


def test_validate_command_reports_missing_theme_tokens(tmp_path, capsys) -> None:
    template_root = tmp_path / "templates"
    static_root = tmp_path / "static"
    (template_root / "public/pages").mkdir(parents=True)
    (template_root / "public/partials").mkdir(parents=True)
    (template_root / "identity/pages").mkdir(parents=True)
    (template_root / "layouts").mkdir(parents=True)
    (template_root / "components").mkdir(parents=True)
    (template_root / "errors").mkdir(parents=True)
    (static_root / "styles").mkdir(parents=True)

    source_root = Path(__file__).resolve().parents[1]
    template_paths = (
        "public/pages/home.html",
        "layouts/page.html",
        "components/theme_switcher.html",
        "components/theme_selector.html",
        "errors/base.html",
    )
    for template_path in template_paths:
        source_base = (
            source_root / "src/uniquode/templates"
            if template_path.startswith("public/")
            else source_root / "src/wevra/web/templates"
        )
        source = source_base / template_path
        destination = template_root / template_path
        destination.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    identity_template_paths = (
        "identity/pages/account.html",
        "identity/pages/login.html",
        "identity/pages/logout.html",
        "identity/pages/password_reset.html",
        "identity/pages/signup.html",
        "identity/pages/verify.html",
    )
    for template_path in identity_template_paths:
        source = source_root / "src/wevra/auth/templates" / template_path
        destination = template_root / template_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(source.read_text())

    (static_root / "styles/app.css").write_text(":root {}")

    exit_code = validate_main(
        [
            "web",
            "--template-root",
            str(template_root),
            "--static-root",
            str(static_root),
        ]
    )

    captured = capsys.readouterr()

    assert exit_code == 1
    assert captured.out == ""
    assert "Missing theme token" in captured.err
    assert "Missing theme selector" in captured.err
