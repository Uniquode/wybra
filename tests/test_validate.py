from pathlib import Path

import pytest

from uniquode.settings import Settings
from uniquode.validate import _contains_post_form
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


def test_validate_command_default_runs_registered_targets(capsys) -> None:
    exit_code = validate_main([])

    captured = capsys.readouterr()

    assert exit_code == 0
    assert "web: ok" in captured.out
    assert "persistence: ok" in captured.out


def test_validate_command_verbose_lists_registered_checks(capsys) -> None:
    exit_code = validate_main(["--verbose"])

    captured = capsys.readouterr()

    assert exit_code == 0
    assert "web: ok" in captured.out
    assert "persistence: ok" in captured.out
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
    assert "Static URL path must not be empty." in captured.out


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


def test_validate_command_rejects_unsupported_database_url(capsys) -> None:
    exit_code = validate_main(["persistence", "--database-url", "sqlite://:memory:"])

    captured = capsys.readouterr()

    assert exit_code == 1
    assert "Database URL must use sqlite+aiosqlite:// or postgresql+asyncpg://" in (
        captured.out
    )


def test_validate_command_rejects_empty_database_url_override(capsys) -> None:
    exit_code = validate_main(["persistence", "--database-url", ""])

    captured = capsys.readouterr()

    assert exit_code == 1
    assert "Database URL must not be empty." in captured.out


def test_validate_command_verbose_lists_failed_checks(capsys) -> None:
    exit_code = validate_main(
        ["persistence", "--verbose", "--database-url", "sqlite://:memory:"]
    )

    captured = capsys.readouterr()

    assert exit_code == 1
    assert "persistence: failed" in captured.out
    assert "ok: database URL is configured: sqlite://:memory:" in captured.out
    assert "failed: database URL uses supported async SQLAlchemy driver" in (
        captured.out
    )
    assert "Database URL must use sqlite+aiosqlite:// or postgresql+asyncpg://" in (
        captured.out
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
    assert "Missing Alembic config" in captured.out
    assert "Missing Alembic migrations root" in captured.out


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
    assert "Missing template" in captured.out


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
    assert "Unable to read" in captured.out
    assert "identity/pages/login.html" in captured.out


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
    assert "Missing theme token" in captured.out
    assert "Missing theme selector" in captured.out
