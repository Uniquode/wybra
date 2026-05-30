from pathlib import Path
from shutil import copytree

import pytest

import uniquode.validate as validate_module
import uniquode.validation.environment as environment_validation
from uniquode.configuration import ConfigurationError
from uniquode.settings import Settings
from uniquode.validate import _contains_post_form, validate_web
from uniquode.validate import main as validate_main


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
    assert "ok: theme token present: --u-colour-page-bg" in captured.out
    assert "ok: default database URL uses persistent SQLite file:" in captured.out
    assert "ok: database URL uses supported async SQLAlchemy driver" in captured.out
    assert "ok: Alembic config exists:" in captured.out
    assert "ok: Alembic config does not force in-memory SQLite" in captured.out
    assert "ok: Alembic migration file exists: env.py" in captured.out
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


def test_validate_command_unknown_target_raises_system_exit(capsys) -> None:
    with pytest.raises(
        SystemExit, match="Unknown validation target\\(s\\): foo"
    ) as excinfo:
        validate_main(["foo"])

    captured = capsys.readouterr()
    assert "foo" in str(excinfo.value)
    assert captured.out == ""
    assert captured.err == ""


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
    copytree(source_root / "src/templates", template_root)
    copytree(source_root / "src/static", static_root)
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
        "identity/pages/account.html",
        "identity/pages/login.html",
        "identity/pages/logout.html",
        "identity/pages/password_reset.html",
        "identity/pages/verify.html",
        "layouts/page.html",
        "components/theme_switcher.html",
        "components/theme_selector.html",
        "errors/base.html",
    )
    for template_path in template_paths:
        source = source_root / "src/templates" / template_path
        destination = template_root / template_path
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
