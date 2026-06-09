import asyncio
import csv
import importlib
import importlib.metadata
import io
import json
import logging
import pkgutil
import sqlite3
import sys
import tempfile
import tomllib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from textwrap import dedent
from time import time
from types import SimpleNamespace

import click
import pytest
from click.testing import CliRunner
from fastapi import FastAPI, Request
from fastapi_users.exceptions import UserAlreadyExists, UserNotExists
from sqlalchemy import inspect as sqlalchemy_inspect
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

import wevra.auth.admin.management as identity_management
import wevra.auth.cli.authmgr as identitymgr
import wevra.auth.cli.authmgr.groups as authmgr_groups
import wevra.auth.cli.authmgr.output as authmgr_output
import wevra.auth.cli.authmgr.passwords as authmgr_passwords
import wevra.auth.cli.authmgr.runtime as authmgr_runtime
import wevra.auth.cli.authmgr.schema as authmgr_schema
import wevra.auth.cli.authmgr.scopes as authmgr_scopes
import wevra.auth.cli.authmgr.timestamps as authmgr_timestamps
import wevra.auth.cli.authmgr.users as authmgr_users
import wevra.auth.sessions as identity_sessions
import wevra.db.migrate as migrate_module
import wevra.db.persistence as db_persistence
from wevra.auth import ERROR_INACTIVE_USER
from wevra.auth.accounts.manager import UserManager, create_user_manager
from wevra.auth.accounts.schemas import UserCreate
from wevra.auth.configuration import ConfigurationError
from wevra.auth.models import (
    Base,
    Group,
    GroupGroup,
    GroupScope,
    GroupUser,
    IdentityUserEmail,
    Scope,
    User,
)
from wevra.auth.options import IdentityOptions
from wevra.auth.persistence import create_database_strategy, create_user_database
from wevra.auth.persistence.database import (
    SQLITE_MEMORY_DATABASE_URL,
    parse_sqlite_database_url,
    resolve_database_url,
)
from wevra.auth.settings import load_auth_settings
from wevra.core.composition import (
    AppConfig,
    RouteOptions,
    StaticOptions,
    TemplateOptions,
    load_app_config,
)
from wevra.db.persistence import (
    close_database,
    create_database,
    create_database_engine,
    create_session_factory,
    session_scope,
)

STRONG_TEST_PASSWORD = "Correct horse 42!"
UPDATED_STRONG_TEST_PASSWORD = "New correct horse 42!"


@dataclass(frozen=True, slots=True)
class AuthTestSettings:
    database_url: str
    identity_options: IdentityOptions = field(default_factory=IdentityOptions)


@dataclass(frozen=True, slots=True)
class MigrationTestSettings:
    database_url: str
    alembic_config: Path
    migrations_root: Path | None = None
    app_config: None = None

    @property
    def modules(self) -> tuple[str, ...]:
        return ("wevra.auth",)


@dataclass(slots=True)
class CaptureDelivery:
    verification_tokens: list[tuple[str, str]]

    async def send_verification_token(
        self,
        user: User,
        token: str,
        request: Request | None = None,
    ) -> None:
        self.verification_tokens.append((user.email, token))


@dataclass(slots=True)
class TimestampVisibleVerificationDelivery:
    session_factory: object
    verification_tokens: list[tuple[str, str]]
    visible_timestamp: float | None = None

    async def send_verification_token(
        self,
        user: User,
        token: str,
        request: Request | None = None,
    ) -> None:
        async with session_scope(self.session_factory) as session:  # type: ignore[arg-type]
            refreshed_user = (
                await session.execute(select(User).where(User.email == user.email))
            ).scalar_one()
            self.visible_timestamp = refreshed_user.email_verification_sent_at

        self.verification_tokens.append((user.email, token))


def sqlite_file_url(path: Path) -> str:
    return f"sqlite+aiosqlite:///{path.resolve().as_posix()}"


def create_auth_test_app(
    *,
    database_url: str = SQLITE_MEMORY_DATABASE_URL,
    identity_options: IdentityOptions | None = None,
) -> FastAPI:
    options = identity_options or IdentityOptions()
    settings = AuthTestSettings(database_url=database_url, identity_options=options)
    app = FastAPI()
    app.state.settings = settings
    app.state.identity_options = options
    app.state.database = create_database(settings)
    return app


def run_auth_migration(argv: list[str]) -> int:
    alembic_config = tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        suffix=".ini",
        delete=False,
    )
    alembic_config.write(
        dedent(
            """
        [alembic]

        [loggers]
        keys = root,sqlalchemy,alembic

        [handlers]
        keys = console

        [formatters]
        keys = generic

        [logger_root]
        level = WARNING
        handlers = console
        qualname =

        [logger_sqlalchemy]
        level = WARNING
        handlers =
        qualname = sqlalchemy.engine

        [logger_alembic]
        level = INFO
        handlers =
        qualname = alembic

        [handler_console]
        class = StreamHandler
        args = (sys.stderr,)
        level = NOTSET
        formatter = generic

        [formatter_generic]
        format = %(levelname)-5.5s [%(name)s] %(message)s
        datefmt = %H:%M:%S
        """
        )
    )
    alembic_config.close()
    alembic_config_path = Path(alembic_config.name)

    def load_settings(database_url: str | None) -> MigrationTestSettings:
        if database_url is None:
            raise migrate_module.MigrationConfigurationError(
                "Test database URL is required."
            )

        return MigrationTestSettings(
            database_url=database_url,
            alembic_config=alembic_config_path,
        )

    try:
        command = migrate_module.create_migrate_command(load_settings)
        return migrate_module.run_migrate_command(command, argv)
    finally:
        alembic_config_path.unlink(missing_ok=True)


def write_auth_app_toml(
    config_path: Path,
    *auth_lines: str,
    database_url: str = "sqlite+aiosqlite:///auth.sqlite3",
) -> Path:
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        "\n".join(
            [
                "[app]",
                f'database_url = "{database_url}"',
                'modules = ["wevra.auth"]',
                "",
                "[app.templates]",
                "auto_reload = true",
                "cache_size = 0",
                "",
                "[app.static]",
                'url_path = "/static/"',
                'export_root = "static"',
                "",
                "[auth]",
                *auth_lines,
            ]
        ),
        encoding="utf-8",
    )
    return config_path


def load_auth_test_app_config(
    config_path: Path,
    *auth_lines: str,
    database_url: str = "sqlite+aiosqlite:///auth.sqlite3",
) -> AppConfig:
    return load_app_config(
        project_root=config_path.parent,
        config_path=write_auth_app_toml(
            config_path,
            *auth_lines,
            database_url=database_url,
        ),
    )


@pytest.fixture(autouse=True)
def _identitymgr_app_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = write_auth_app_toml(tmp_path / "app.toml")
    monkeypatch.setenv("APP_CONFIG", str(config_path))
    monkeypatch.delenv("AUTH_CONFIG", raising=False)


def set_identitymgr_database_url(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    database_url: str,
) -> None:
    config_path = write_auth_app_toml(tmp_path / "app.toml", database_url=database_url)
    monkeypatch.setenv("APP_CONFIG", str(config_path))


def initialise_identity_database(database_url: str) -> None:
    settings = AuthTestSettings(database_url=database_url)
    engine = create_database_engine(settings)

    async def initialise() -> None:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

    try:
        asyncio.run(initialise())
    finally:
        asyncio.run(close_database(engine))


def initialise_legacy_identity_database(database_path: Path) -> None:
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            CREATE TABLE identity_user (
                id CHAR(32) NOT NULL PRIMARY KEY,
                email VARCHAR(320) NOT NULL,
                hashed_password VARCHAR(1024) NOT NULL,
                is_active BOOLEAN NOT NULL,
                is_superuser BOOLEAN NOT NULL,
                is_verified BOOLEAN NOT NULL
            )
            """
        )


def identity_users_from_database(database_url: str) -> list[User]:
    settings = AuthTestSettings(database_url=database_url)
    engine = create_database_engine(settings)
    session_factory = create_session_factory(engine)

    async def load_users() -> list[User]:
        async with session_scope(session_factory) as session:
            return list((await session.execute(select(User))).scalars().all())

    try:
        return asyncio.run(load_users())
    finally:
        asyncio.run(close_database(engine))


def identity_user_from_database(database_url: str, email: str) -> User | None:
    settings = AuthTestSettings(database_url=database_url)
    engine = create_database_engine(settings)
    session_factory = create_session_factory(engine)

    async def load_user() -> User | None:
        async with session_scope(session_factory) as session:
            return (
                await session.execute(select(User).where(User.email == email))
            ).scalar_one_or_none()

    try:
        return asyncio.run(load_user())
    finally:
        asyncio.run(close_database(engine))


def identity_user_emails_from_database(database_url: str) -> list[IdentityUserEmail]:
    settings = AuthTestSettings(database_url=database_url)
    engine = create_database_engine(settings)
    session_factory = create_session_factory(engine)

    async def load_user_emails() -> list[IdentityUserEmail]:
        async with session_scope(session_factory) as session:
            return list(
                (await session.execute(select(IdentityUserEmail))).scalars().all()
            )

    try:
        return asyncio.run(load_user_emails())
    finally:
        asyncio.run(close_database(engine))


def access_tokens_from_database(database_url: str) -> list[str]:
    from wevra.auth.models import AccessToken

    settings = AuthTestSettings(database_url=database_url)
    engine = create_database_engine(settings)
    session_factory = create_session_factory(engine)

    async def load_tokens() -> list[str]:
        async with session_scope(session_factory) as session:
            return [
                token.token
                for token in (await session.execute(select(AccessToken)))
                .scalars()
                .all()
            ]

    try:
        return asyncio.run(load_tokens())
    finally:
        asyncio.run(close_database(engine))


def scopes_from_database(database_url: str) -> list[Scope]:
    settings = AuthTestSettings(database_url=database_url)
    engine = create_database_engine(settings)
    session_factory = create_session_factory(engine)

    async def load_scopes() -> list[Scope]:
        async with session_scope(session_factory) as session:
            return list((await session.execute(select(Scope))).scalars().all())

    try:
        return asyncio.run(load_scopes())
    finally:
        asyncio.run(close_database(engine))


def group_from_database(database_url: str, abbrev: str) -> Group:
    settings = AuthTestSettings(database_url=database_url)
    engine = create_database_engine(settings)
    session_factory = create_session_factory(engine)

    async def load_group() -> Group:
        async with session_scope(session_factory) as session:
            return (
                await session.execute(select(Group).where(Group.abbrev == abbrev))
            ).scalar_one()

    try:
        return asyncio.run(load_group())
    finally:
        asyncio.run(close_database(engine))


def group_scopes_from_database(database_url: str, abbrev: str) -> list[str]:
    settings = AuthTestSettings(database_url=database_url)
    engine = create_database_engine(settings)
    session_factory = create_session_factory(engine)

    async def load_group_scopes() -> list[str]:
        async with session_scope(session_factory) as session:
            group = (
                await session.execute(select(Group).where(Group.abbrev == abbrev))
            ).scalar_one()
            return list(
                (
                    await session.execute(
                        select(GroupScope.scope).where(GroupScope.group_id == group.id)
                    )
                )
                .scalars()
                .all()
            )

    try:
        return asyncio.run(load_group_scopes())
    finally:
        asyncio.run(close_database(engine))


def user_group_abbrevs_from_database(database_url: str, email: str) -> list[str]:
    settings = AuthTestSettings(database_url=database_url)
    engine = create_database_engine(settings)
    session_factory = create_session_factory(engine)

    async def load_user_groups() -> list[str]:
        async with session_scope(session_factory) as session:
            user = (
                await session.execute(select(User).where(User.email == email))
            ).scalar_one()
            return sorted(
                (
                    await session.execute(
                        select(Group.abbrev)
                        .join(GroupUser, GroupUser.group_id == Group.id)
                        .where(GroupUser.user_id == user.id)
                    )
                )
                .scalars()
                .all()
            )

    try:
        return asyncio.run(load_user_groups())
    finally:
        asyncio.run(close_database(engine))


def create_session_token_for_user(database_url: str, email: str) -> str:
    settings = AuthTestSettings(database_url=database_url)
    engine = create_database_engine(settings)
    session_factory = create_session_factory(engine)

    async def create_token() -> str:
        async with session_scope(session_factory) as session:
            user = (
                await session.execute(select(User).where(User.email == email))
            ).scalar_one()
            strategy = create_database_strategy(session, settings.identity_options)
            return await strategy.write_token(user)

    try:
        return asyncio.run(create_token())
    finally:
        asyncio.run(close_database(engine))


def update_user_fields(database_url: str, email: str, **values: object) -> None:
    settings = AuthTestSettings(database_url=database_url)
    engine = create_database_engine(settings)
    session_factory = create_session_factory(engine)

    async def update_user() -> None:
        async with session_scope(session_factory) as session:
            user = (
                await session.execute(select(User).where(User.email == email))
            ).scalar_one()
            for field_name, value in values.items():
                setattr(user, field_name, value)
            await session.commit()

    try:
        asyncio.run(update_user())
    finally:
        asyncio.run(close_database(engine))


def test_identitymgr_project_script_is_defined() -> None:
    data = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))

    assert data["project"]["scripts"]["wevra-authmgr"] == "wevra.auth.cli.authmgr:main"
    assert "wevra-identitymgr" not in data["project"]["scripts"]
    assert "identitymgr" not in data["project"]["scripts"]
    assert "usermgr" not in data["project"]["scripts"]


def test_identitymgr_uses_shared_database_runtime_helpers() -> None:
    assert authmgr_runtime.create_database is db_persistence.create_database
    assert authmgr_runtime.session_scope is db_persistence.session_scope
    assert authmgr_runtime.close_database is db_persistence.close_database


def test_identitymgr_command_registration_is_explicit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_discovery(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("auth manager command registration used discovery")

    monkeypatch.setattr(importlib.metadata, "entry_points", fail_discovery)
    monkeypatch.setattr(pkgutil, "iter_modules", fail_discovery)

    import wevra.auth.cli.authmgr.cli as authmgr_cli

    reloaded_cli = importlib.reload(authmgr_cli)
    importlib.reload(identitymgr)

    assert {"user", "scope", "group"} <= set(reloaded_cli.authmgr_command.commands)


def test_identitymgr_create_positional_is_email() -> None:
    result = CliRunner().invoke(
        identitymgr.authmgr_command,
        ["user", "create", "--help"],
    )

    assert result.exit_code == 0
    assert "EMAIL" in result.output
    assert "TARGET" not in result.output


def test_identitymgr_update_positional_is_target() -> None:
    result = CliRunner().invoke(
        identitymgr.authmgr_command,
        ["user", "update", "--help"],
    )

    assert result.exit_code == 0
    assert "TARGET" in result.output
    assert "EMAIL" not in result.output


def test_dateparser_runtime_dependency_is_defined() -> None:
    data = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))

    assert any(
        dependency.startswith("dateparser")
        for dependency in data["project"]["dependencies"]
    )


def test_identitymgr_loads_app_auth_configuration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "auth.sqlite3"
    database_url = sqlite_file_url(database_path)
    initialise_identity_database(database_url)
    config_path = write_auth_app_toml(
        tmp_path / "configured" / "app.toml",
        'session_cookie_name = "auth_session"',
        database_url=database_url,
    )
    monkeypatch.setenv("APP_CONFIG", str(config_path))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr(sys, "stdin", io.StringIO(f"{STRONG_TEST_PASSWORD}\n"))

    assert (
        identitymgr.main(
            [
                "user",
                "create",
                "configured@example.com",
                "--password",
                "-",
            ]
        )
        == 0
    )

    [user] = identity_users_from_database(database_url)
    assert user.email == "configured@example.com"


def test_identitymgr_loads_project_app_toml_configuration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "project-auth.sqlite3"
    database_url = sqlite_file_url(database_path)
    initialise_identity_database(database_url)
    write_auth_app_toml(tmp_path / "app.toml", database_url=database_url)
    (tmp_path / "pyproject.toml").write_text("[tool.wevra]\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("APP_CONFIG", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr(sys, "stdin", io.StringIO(f"{STRONG_TEST_PASSWORD}\n"))

    assert (
        identitymgr.main(
            ["user", "create", "project-config@example.com", "--password", "-"]
        )
        == 0
    )

    [user] = identity_users_from_database(database_url)
    assert user.email == "project-config@example.com"


def test_identitymgr_rejects_missing_app_config_even_when_auth_toml_exists(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    case_root = tmp_path / "auth-only"
    case_root.mkdir()
    (case_root / "auth.toml").write_text(
        "\n".join(
            [
                "[auth]",
                'database_url = "sqlite+aiosqlite:///auth.sqlite3"',
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.chdir(case_root)
    monkeypatch.delenv("APP_CONFIG", raising=False)
    monkeypatch.setenv("AUTH_CONFIG", str(case_root / "auth.toml"))
    stdin = io.StringIO(f"{STRONG_TEST_PASSWORD}\n")
    monkeypatch.setattr(sys, "stdin", stdin)

    exit_code = identitymgr.main(
        ["user", "create", "missing-config@example.com", "--password", "-"]
    )

    assert exit_code == 1
    assert stdin.tell() == 0
    captured = capsys.readouterr()
    assert "App config file does not exist" in captured.err
    assert "set APP_CONFIG" in captured.err


@pytest.mark.parametrize(
    ("environ_template", "expected_url"),
    [
        pytest.param(
            {"DATABASE_URL": "database_env"},
            "database_env",
            id="database-env-used",
        ),
        pytest.param(
            {"AUTH_DATABASE_URL": "auth_env"},
            "config",
            id="auth-env-ignored",
        ),
        pytest.param(
            {"DATABASE_URL": "   "},
            "config",
            id="blank-database-env-falls-back-to-config",
        ),
    ],
)
def test_app_database_url_precedence(
    tmp_path: Path,
    environ_template: dict[str, str],
    expected_url: str,
) -> None:
    urls = {
        "auth_env": sqlite_file_url(tmp_path / "auth-env.sqlite3"),
        "config": sqlite_file_url(tmp_path / "auth.sqlite3"),
        "database_env": sqlite_file_url(tmp_path / "database-env.sqlite3"),
    }
    app_config = load_auth_test_app_config(
        tmp_path / "app.toml",
        database_url=urls["config"],
    )
    environ = {
        key: urls[value] if value in urls else value
        for key, value in environ_template.items()
    }

    settings = load_auth_settings(
        app_config=app_config,
        environ=environ,
    )

    assert settings.database_url == urls[expected_url]


def test_app_database_url_resolves_relative_sqlite_path_from_config_directory(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config" / "app.toml"
    app_config = load_auth_test_app_config(
        config_path,
        database_url="sqlite+aiosqlite:///relative-auth.sqlite3",
    )

    settings = load_auth_settings(app_config=app_config, environ={})

    assert settings.database_url == sqlite_file_url(
        config_path.parent / "relative-auth.sqlite3"
    )


def test_app_database_url_error_names_app_config_section(tmp_path: Path) -> None:
    app_config = AppConfig(
        config_path=tmp_path / "app.toml",
        project_root=tmp_path,
        modules=("wevra.auth",),
        routes=RouteOptions(),
        templates=TemplateOptions(auto_reload=True, cache_size=0),
        static=StaticOptions(url_path="/static/", export_root=Path("static")),
    )

    with pytest.raises(ConfigurationError, match=r"\[app\]\.database_url"):
        load_auth_settings(app_config=app_config, environ={})


def test_app_auth_config_rejects_unknown_auth_options(tmp_path: Path) -> None:
    app_config = load_auth_test_app_config(
        tmp_path / "app.toml",
        "session_lifetme_seconds = 3600",
    )

    with pytest.raises(ConfigurationError, match="session_lifetme_seconds"):
        load_auth_settings(app_config=app_config, environ={})


def test_app_auth_config_rejects_stale_auth_database_url(tmp_path: Path) -> None:
    app_config = load_auth_test_app_config(
        tmp_path / "app.toml",
        'database_url = "sqlite+aiosqlite:///stale-auth.sqlite3"',
    )

    with pytest.raises(ConfigurationError, match="database_url"):
        load_auth_settings(app_config=app_config, environ={})


def test_app_auth_config_applies_identity_env_overrides(tmp_path: Path) -> None:
    app_config = load_auth_test_app_config(
        tmp_path / "app.toml",
        "provider_enabled = true",
        "totp_enabled = false",
        "passkey_enabled = true",
    )

    settings = load_auth_settings(
        app_config=app_config,
        environ={
            "PROVIDER_ENABLED": "false",
            "TOTP_ENABLED": "true",
            "PASSKEY_ENABLED": "false",
        },
    )

    assert settings.identity_options.provider_enabled is False
    assert settings.identity_options.totp_enabled is True
    assert settings.identity_options.passkey_enabled is False


def test_app_auth_configures_default_password_policy(tmp_path: Path) -> None:
    app_config = load_auth_test_app_config(
        tmp_path / "app.toml",
        "session_cookie_force_secure = true",
        "",
        "[auth.password.policy]",
        "minimum_length = 8",
        "minimum_strength = 0.25",
        "minimum_character_categories = 1",
        'common_fragments = ["example"]',
    )

    settings = load_auth_settings(app_config=app_config, environ={})

    assert settings.identity_options.session_cookie_force_secure is True
    policy = settings.identity_options.resolved_password_policy()
    assert policy.minimum_length == 8
    assert policy.minimum_score == 0.25
    assert policy.minimum_character_categories == 1
    assert policy.common_fragments == ("example",)


@pytest.mark.parametrize(
    "common_fragments_config",
    [
        "common_fragments = 123",
        'common_fragments = ["example", 123]',
    ],
)
def test_app_auth_rejects_invalid_password_common_fragments(
    tmp_path: Path,
    common_fragments_config: str,
) -> None:
    app_config = load_auth_test_app_config(
        tmp_path / "app.toml",
        "",
        "[auth.password.policy]",
        common_fragments_config,
    )

    with pytest.raises(ConfigurationError, match="common fragments"):
        load_auth_settings(app_config=app_config, environ={})


def test_app_auth_rejects_unknown_password_policy_options(tmp_path: Path) -> None:
    app_config = load_auth_test_app_config(
        tmp_path / "app.toml",
        "",
        "[auth.password.policy]",
        "minimum_strenth = 0.25",
    )

    with pytest.raises(ConfigurationError, match="minimum_strenth"):
        load_auth_settings(app_config=app_config, environ={})


@pytest.mark.parametrize("command", ["group", "scope", "user"])
def test_identitymgr_root_help_exposes_resource_command_groups(command: str) -> None:
    result = CliRunner().invoke(identitymgr.authmgr_command, ["--help"])

    assert result.exit_code == 0
    assert command in result.output


@pytest.mark.parametrize(
    "command",
    ["create", "update", "delete", "deactivate", "list", "password"],
)
def test_identitymgr_user_help_exposes_user_commands(command: str) -> None:
    result = CliRunner().invoke(identitymgr.authmgr_command, ["user", "--help"])

    assert result.exit_code == 0
    assert command in result.output


@pytest.mark.parametrize(
    ("help_suffix_args", "help_option_args"),
    [
        pytest.param(["help"], ["--help"], id="root"),
        pytest.param(["help", "user"], ["user", "--help"], id="root-user-group"),
        pytest.param(["help", "scope"], ["scope", "--help"], id="root-scope-group"),
        pytest.param(["help", "group"], ["group", "--help"], id="root-group-command"),
        pytest.param(
            ["help", "user", "create"],
            ["user", "create", "--help"],
            id="root-user-create",
        ),
        pytest.param(
            ["help", "scope", "create"],
            ["scope", "create", "--help"],
            id="root-scope-create",
        ),
        pytest.param(["user", "help"], ["user", "--help"], id="user-group"),
        pytest.param(
            ["user", "help", "create"],
            ["user", "create", "--help"],
            id="user-create",
        ),
        pytest.param(["scope", "help"], ["scope", "--help"], id="scope-group"),
        pytest.param(
            ["scope", "help", "create"],
            ["scope", "create", "--help"],
            id="scope-create",
        ),
        pytest.param(["group", "help"], ["group", "--help"], id="group-command"),
    ],
)
def test_identitymgr_help_suffix_matches_help_option(
    help_suffix_args: list[str],
    help_option_args: list[str],
) -> None:
    runner = CliRunner()

    suffix_result = runner.invoke(identitymgr.authmgr_command, help_suffix_args)
    option_result = runner.invoke(identitymgr.authmgr_command, help_option_args)

    assert suffix_result.exit_code == option_result.exit_code == 0
    assert suffix_result.output == option_result.output


@pytest.mark.parametrize(
    ("argv", "usage"),
    [
        pytest.param(
            ["help", "group", "create"],
            "Usage: wevra-authmgr group create <abbrev>",
            id="root-group-create",
        ),
        pytest.param(
            ["group", "help", "create"],
            "Usage: wevra-authmgr group create <abbrev>",
            id="group-create",
        ),
        pytest.param(
            ["group", "help", "project", "update"],
            "Usage: wevra-authmgr group <group> update",
            id="group-target-update",
        ),
    ],
)
def test_identitymgr_help_path_shows_raw_group_operation_usage(
    argv: list[str],
    usage: str,
) -> None:
    result = CliRunner().invoke(identitymgr.authmgr_command, argv)

    assert result.exit_code == 0
    assert usage in result.output


def test_identitymgr_preserves_help_as_option_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_args: list[identitymgr.AuthmgrArgs] = []

    def capture_args(_ctx: click.Context, args: identitymgr.AuthmgrArgs) -> None:
        captured_args.append(args)

    monkeypatch.setattr(authmgr_users, "_run_authmgr", capture_args)

    result = CliRunner().invoke(
        identitymgr.authmgr_command,
        ["user", "update", "alice@example.com", "--display-name", "help"],
    )

    assert result.exit_code == 0
    assert captured_args[0].display_name == "help"


@pytest.mark.parametrize(
    ("argv", "expected_field", "expected_value"),
    [
        pytest.param(
            ["scope", "create", "help"],
            "scope",
            "help",
            id="scope-name",
        ),
        pytest.param(
            ["group", "create", "admins", "--description", "help"],
            "description",
            "help",
            id="group-description",
        ),
    ],
)
def test_identitymgr_preserves_help_as_command_value(
    monkeypatch: pytest.MonkeyPatch,
    argv: list[str],
    expected_field: str,
    expected_value: str,
) -> None:
    captured_args: list[identitymgr.AuthmgrArgs] = []

    def capture_args(_ctx: click.Context, args: identitymgr.AuthmgrArgs) -> None:
        captured_args.append(args)

    target_module = authmgr_scopes if argv[0] == "scope" else authmgr_groups
    monkeypatch.setattr(target_module, "_run_authmgr", capture_args)

    result = CliRunner().invoke(identitymgr.authmgr_command, argv)

    assert result.exit_code == 0
    assert getattr(captured_args[0], expected_field) == expected_value


def test_identitymgr_group_create_accepts_dash_prefixed_abbrev_after_terminator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_args: list[identitymgr.AuthmgrArgs] = []

    def capture_args(_ctx: click.Context, args: identitymgr.AuthmgrArgs) -> None:
        captured_args.append(args)

    monkeypatch.setattr(authmgr_groups, "_run_authmgr", capture_args)

    result = CliRunner().invoke(
        identitymgr.authmgr_command,
        ["group", "create", "--", "-admins"],
    )

    assert result.exit_code == 0
    assert captured_args[0].command == "group-create"
    assert captured_args[0].group_target == "-admins"


@pytest.mark.parametrize(
    "command",
    ["create", "update", "delete", "deactivate", "list", "password"],
)
def test_identitymgr_rejects_top_level_user_action_commands(command: str) -> None:
    result = CliRunner().invoke(identitymgr.authmgr_command, [command])

    assert result.exit_code == 2
    assert f"No such command '{command}'" in result.output


def test_identitymgr_rejects_unknown_command() -> None:
    result = CliRunner().invoke(identitymgr.authmgr_command, ["unknown"])

    assert result.exit_code == 2
    assert "No such command 'unknown'" in result.output


def test_identitymgr_main_treats_falsy_click_exception_as_failure(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class FalsyExitClickException(click.ClickException):
        exit_code = 0

    def raise_click_exception(*_args, **_kwargs) -> None:
        raise FalsyExitClickException("invalid usage")

    monkeypatch.setattr(identitymgr.authmgr_command, "main", raise_click_exception)

    assert identitymgr.main([]) == 1

    captured = capsys.readouterr()
    assert "invalid usage" in captured.err


@pytest.mark.parametrize(
    "argv",
    [
        ["user", "create", "person@example.com", "--password", "secret"],
        ["user", "password", "person@example.com", "--password", "secret"],
        ["user", "update", "person@example.com", "--password", "secret"],
    ],
)
def test_identitymgr_rejects_plain_command_line_password(argv: list[str]) -> None:
    result = CliRunner().invoke(
        identitymgr.authmgr_command,
        argv,
    )

    assert result.exit_code == 2
    assert "must be '-' or omitted" in result.output
    assert "--password" in result.output


@pytest.mark.parametrize("expires_at", ["4102444800", "0"])
def test_identitymgr_rejects_conflicting_expiry_update_options(expires_at: str) -> None:
    result = CliRunner().invoke(
        identitymgr.authmgr_command,
        [
            "user",
            "update",
            "person@example.com",
            "--expires-at",
            expires_at,
            "--no-expires-at",
        ],
    )

    assert result.exit_code == 2
    assert "not allowed with option '--expires-at'" in result.output


def test_identitymgr_rejects_conflicting_display_name_update_with_empty_value() -> None:
    result = CliRunner().invoke(
        identitymgr.authmgr_command,
        [
            "user",
            "update",
            "person@example.com",
            "--display-name",
            "",
            "--no-display-name",
        ],
    )

    assert result.exit_code == 2
    assert "not allowed with option '--display-name'" in result.output


def test_identitymgr_accepts_flexible_expiry_timestamp_values() -> None:
    assert (
        authmgr_timestamps.parse_timestamp_filter("2100-01-01T00:00:00Z")
        == 4102444800.0
    )
    assert authmgr_timestamps.parse_timestamp_filter("4102444800") == 4102444800.0
    assert authmgr_timestamps.parse_timestamp_filter("20250101") == 20250101.0


def test_identitymgr_timestamp_parse_error_identifies_option() -> None:
    result = CliRunner().invoke(
        identitymgr.authmgr_command,
        ["user", "list", "--since-created-at", "not-a-date"],
    )

    assert result.exit_code == 2
    assert "Invalid value for '--since-created-at'" in result.output
    assert "Invalid timestamp value: not-a-date" in result.output


def test_identitymgr_help_documents_numeric_timestamp_precedence() -> None:
    result = CliRunner().invoke(identitymgr.authmgr_command, ["--help"])

    assert result.exit_code == 0
    assert "numeric input as Unix seconds before date parsing" in result.output


def test_user_model_exposes_management_metadata_columns() -> None:
    user_columns = set(User.__table__.columns.keys())
    user_indexes = {index.name for index in User.__table__.indexes}

    assert {
        "is_admin",
        "created_at",
        "modified_at",
        "last_login_at",
        "expires_at",
        "email_verification_sent_at",
        "display_name",
        "preferred_name",
        "preferred_timezone",
    }.issubset(user_columns)
    assert {
        "ix_identity_user_is_active_expires_at",
        "ix_identity_user_last_login_at",
        "ix_identity_user_created_at",
        "ix_identity_user_modified_at",
        "ix_identity_user_is_admin",
        "ix_identity_user_is_superuser",
    }.issubset(user_indexes)


def test_user_model_updates_modified_at_on_orm_update() -> None:
    assert User.__table__.c.modified_at.onupdate is not None


def test_user_management_metadata_defaults() -> None:
    settings = AuthTestSettings(database_url=SQLITE_MEMORY_DATABASE_URL)
    engine = create_database_engine(settings)
    session_factory = create_session_factory(engine)

    async def assert_defaults() -> None:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

        async with session_scope(session_factory) as session:
            manager = create_user_manager(session, settings.identity_options)
            await manager.create(
                UserCreate(
                    email="metadata@example.com",
                    password=STRONG_TEST_PASSWORD,
                ),
                safe=True,
            )

        async with session_scope(session_factory) as session:
            user = (
                await session.execute(
                    select(User).where(User.email == "metadata@example.com")
                )
            ).scalar_one()

            assert user.is_admin is False
            assert isinstance(user.created_at, float)
            assert isinstance(user.modified_at, float)
            assert user.created_at > 0
            assert user.modified_at >= user.created_at
            assert user.last_login_at is None
            assert user.expires_at is None
            assert user.email_verification_sent_at is None
            assert user.display_name is None
            assert user.preferred_name is None
            assert user.preferred_timezone is None

    try:
        asyncio.run(assert_defaults())
    finally:
        asyncio.run(close_database(engine))


def test_user_manager_get_by_email_resolves_secondary_emails(tmp_path: Path) -> None:
    database_url = sqlite_file_url(tmp_path / "secondary-email.sqlite3")
    initialise_identity_database(database_url)
    web_app = create_auth_test_app(database_url=database_url)

    async def assert_secondary_email_lookup() -> None:
        async with web_app.state.database.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

        async with session_scope(web_app.state.database.session_factory) as session:
            manager = create_user_manager(
                session,
                web_app.state.settings.identity_options,
            )
            user = await manager.create(
                UserCreate(
                    email="primary@example.com",
                    password=STRONG_TEST_PASSWORD,
                ),
                safe=True,
            )
            session.add(
                IdentityUserEmail(
                    user_id=user.id,
                    email="alias@example.com",
                    is_primary=False,
                    is_verified=True,
                )
            )
            await session.commit()

            primary_user = await manager.get_by_email("Primary@Example.com")
            alias_user = await manager.get_by_email("Alias@Example.com")

            assert primary_user is not None
            assert alias_user is not None
            assert primary_user.id == user.id
            assert alias_user.id == user.id

    try:
        asyncio.run(assert_secondary_email_lookup())
    finally:
        asyncio.run(close_database(web_app.state.database))


def test_resolve_user_target_uses_secondary_email_addresses(
    tmp_path: Path,
) -> None:
    database_url = sqlite_file_url(tmp_path / "secondary-target.sqlite3")
    initialise_identity_database(database_url)
    web_app = create_auth_test_app(database_url=database_url)

    async def assert_secondary_target_resolution() -> None:
        async with web_app.state.database.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

        async with session_scope(web_app.state.database.session_factory) as session:
            manager = create_user_manager(
                session,
                web_app.state.settings.identity_options,
            )
            user = await manager.create(
                UserCreate(
                    email="target@example.com",
                    password=STRONG_TEST_PASSWORD,
                ),
                safe=True,
            )
            session.add(
                IdentityUserEmail(
                    user_id=user.id,
                    email="linked@example.com",
                    is_primary=False,
                    is_verified=True,
                )
            )
            await session.commit()

            resolved_user, target_error = await identity_management.resolve_user_target(
                session,
                "linked@example.com",
            )
            assert target_error is None
            assert resolved_user is not None
            assert resolved_user.id == user.id

    try:
        asyncio.run(assert_secondary_target_resolution())
    finally:
        asyncio.run(close_database(web_app.state.database))


def test_user_manager_create_rollback_when_after_register_fails(
    tmp_path: Path,
) -> None:
    database_url = sqlite_file_url(tmp_path / "create-after-register-fail.sqlite3")
    initialise_identity_database(database_url)
    web_app = create_auth_test_app(database_url=database_url)

    class _FailingPostRegisterManager(UserManager):
        async def on_after_register(
            self,
            user: User,
            request: Request | None = None,
        ) -> None:
            raise RuntimeError("post-register hook failed")

    async def assert_rollback_when_hook_fails() -> None:
        async with web_app.state.database.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

        async with session_scope(web_app.state.database.session_factory) as session:
            manager = _FailingPostRegisterManager(
                create_user_database(session),
                web_app.state.settings.identity_options,
            )
            with pytest.raises(RuntimeError, match="post-register hook failed"):
                await manager.create(
                    UserCreate(
                        email="rollback-hook@example.com",
                        password=STRONG_TEST_PASSWORD,
                    ),
                    safe=True,
                )

    try:
        asyncio.run(assert_rollback_when_hook_fails())
        assert identity_users_from_database(database_url) == []
        assert identity_user_emails_from_database(database_url) == []
    finally:
        asyncio.run(close_database(web_app.state.database))


def test_user_manager_duplicate_secondary_email_maps_to_user_already_exists(
    tmp_path: Path,
) -> None:
    database_url = sqlite_file_url(
        tmp_path / "create-duplicate-secondary-email.sqlite3"
    )
    initialise_identity_database(database_url)
    web_app = create_auth_test_app(database_url=database_url)

    class _NoLookupManager(UserManager):
        async def get_by_email(self, user_email: str) -> User:
            del user_email
            raise UserNotExists()

    async def assert_duplicate_secondary_email_returns_user_already_exists() -> None:
        async with web_app.state.database.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

        async with session_scope(web_app.state.database.session_factory) as session:
            manager = create_user_manager(
                session,
                web_app.state.settings.identity_options,
            )
            primary_user = await manager.create(
                UserCreate(
                    email="primary@example.com",
                    password=STRONG_TEST_PASSWORD,
                ),
                safe=True,
            )
            session.add(
                IdentityUserEmail(
                    user=primary_user,
                    email="Alias@example.com",
                    is_primary=False,
                    is_verified=True,
                )
            )
            await session.commit()

            racing_manager = _NoLookupManager(
                create_user_database(session),
                web_app.state.settings.identity_options,
            )
            with pytest.raises(UserAlreadyExists):
                await racing_manager.create(
                    UserCreate(
                        email="ALIAS@example.com",
                        password=STRONG_TEST_PASSWORD,
                    ),
                    safe=True,
                )

            users = list((await session.execute(select(User))).scalars())
            emails = list((await session.execute(select(IdentityUserEmail))).scalars())
            assert len(users) == 1
            assert users[0].email == "primary@example.com"
            assert len(emails) == 2

    try:
        asyncio.run(assert_duplicate_secondary_email_returns_user_already_exists())
    finally:
        asyncio.run(close_database(web_app.state.database))


def test_migrate_upgrade_creates_user_management_metadata_columns(
    tmp_path: Path,
) -> None:
    database_url = f"sqlite+aiosqlite:///{(tmp_path / 'metadata.sqlite3').as_posix()}"

    assert run_auth_migration(["--database-url", database_url, "init"]) == 0
    exit_code = run_auth_migration(["--database-url", database_url, "upgrade"])

    assert exit_code == 0

    from sqlalchemy import create_engine

    engine = create_engine(f"sqlite:///{tmp_path / 'metadata.sqlite3'}")
    try:
        inspector = sqlalchemy_inspect(engine)
        columns = {column["name"] for column in inspector.get_columns("identity_user")}

        assert {
            "is_admin",
            "created_at",
            "modified_at",
            "last_login_at",
            "expires_at",
            "email_verification_sent_at",
            "display_name",
            "preferred_name",
            "preferred_timezone",
        }.issubset(columns)
        indexes = {index["name"] for index in inspector.get_indexes("identity_user")}
        assert {
            "ix_identity_user_is_active_expires_at",
            "ix_identity_user_last_login_at",
            "ix_identity_user_created_at",
            "ix_identity_user_modified_at",
            "ix_identity_user_is_admin",
            "ix_identity_user_is_superuser",
        }.issubset(indexes)
    finally:
        engine.dispose()


def test_migrate_upgrade_creates_authorisation_group_tables(
    tmp_path: Path,
) -> None:
    database_url = f"sqlite+aiosqlite:///{(tmp_path / 'groups.sqlite3').as_posix()}"

    assert run_auth_migration(["--database-url", database_url, "init"]) == 0
    exit_code = run_auth_migration(["--database-url", database_url, "upgrade"])

    assert exit_code == 0

    from sqlalchemy import create_engine

    engine = create_engine(f"sqlite:///{tmp_path / 'groups.sqlite3'}")
    try:
        inspector = sqlalchemy_inspect(engine)
        table_names = set(inspector.get_table_names())

        assert {
            "identity_group",
            "identity_scope",
            "identity_group_scope",
            "identity_group_user",
            "identity_group_group",
        }.issubset(table_names)
        assert {
            column["name"] for column in inspector.get_columns("identity_group")
        } == {
            "id",
            "abbrev",
            "description",
        }
        assert {
            column["name"] for column in inspector.get_columns("identity_scope")
        } == {
            "scope",
            "description",
        }
        group_indexes = {
            index["name"] for index in inspector.get_indexes("identity_group")
        }
        assert "ix_identity_group_abbrev" in group_indexes
        group_group_checks = {
            check["name"]
            for check in inspector.get_check_constraints("identity_group_group")
        }
        assert "ck_identity_group_group_no_self_membership" in group_group_checks
        group_scope_foreign_keys = {
            tuple(foreign_key["constrained_columns"]): foreign_key["options"].get(
                "ondelete"
            )
            for foreign_key in inspector.get_foreign_keys("identity_group_scope")
        }
        group_user_foreign_keys = {
            tuple(foreign_key["constrained_columns"]): foreign_key["options"].get(
                "ondelete"
            )
            for foreign_key in inspector.get_foreign_keys("identity_group_user")
        }
        group_group_foreign_keys = {
            tuple(foreign_key["constrained_columns"]): foreign_key["options"].get(
                "ondelete"
            )
            for foreign_key in inspector.get_foreign_keys("identity_group_group")
        }
        assert group_scope_foreign_keys == {
            ("group_id",): "RESTRICT",
            ("scope",): "RESTRICT",
        }
        assert group_user_foreign_keys == {
            ("group_id",): "RESTRICT",
            ("user_id",): "CASCADE",
        }
        assert group_group_foreign_keys == {
            ("parent_group_id",): "RESTRICT",
            ("child_group_id",): "RESTRICT",
        }
    finally:
        engine.dispose()


def test_migrate_upgrade_creates_identity_user_email_table(
    tmp_path: Path,
) -> None:
    database_url = f"sqlite+aiosqlite:///{(tmp_path / 'user-email.sqlite3').as_posix()}"

    assert run_auth_migration(["--database-url", database_url, "init"]) == 0
    exit_code = run_auth_migration(["--database-url", database_url, "upgrade"])

    assert exit_code == 0

    from sqlalchemy import create_engine

    engine = create_engine(f"sqlite:///{tmp_path / 'user-email.sqlite3'}")
    try:
        inspector = sqlalchemy_inspect(engine)
        assert "identity_user_email" in set(inspector.get_table_names())

        user_email_columns = {
            column["name"] for column in inspector.get_columns("identity_user_email")
        }
        assert {"id", "user_id", "email", "is_primary", "is_verified"}.issubset(
            user_email_columns
        )

        user_email_indexes = {
            index["name"] for index in inspector.get_indexes("identity_user_email")
        }
        assert "ix_identity_user_email_user_id" in user_email_indexes
        assert "uq_identity_user_email_primary_per_user" in user_email_indexes

        user_email_uniques = {
            unique["name"]: set(unique["column_names"])
            for unique in inspector.get_unique_constraints("identity_user_email")
        }
        assert user_email_uniques["uq_identity_user_email_email"] == {"email"}

        user_email_foreign_keys = {
            tuple(foreign_key["constrained_columns"]): foreign_key["options"].get(
                "ondelete"
            )
            for foreign_key in inspector.get_foreign_keys("identity_user_email")
        }
        assert user_email_foreign_keys == {
            ("user_id",): "CASCADE",
        }
    finally:
        engine.dispose()


def test_identitymgr_reports_outdated_identity_schema_before_reading_password(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database_path = tmp_path / "legacy-identity.sqlite3"
    initialise_legacy_identity_database(database_path)
    set_identitymgr_database_url(monkeypatch, tmp_path, sqlite_file_url(database_path))
    stdin = io.StringIO(f"{STRONG_TEST_PASSWORD}\n")
    monkeypatch.setattr(sys, "stdin", stdin)

    exit_code = identitymgr.main(
        ["user", "create", "legacy@example.com", "--password", "-"]
    )

    assert exit_code == 1
    assert stdin.tell() == 0
    captured = capsys.readouterr()
    assert "Auth database schema is not up to date" in captured.err
    assert "uv run wevra-migrate init" in captured.err
    assert "uv run wevra-migrate upgrade" in captured.err
    assert "APP_CONFIG" in captured.err
    assert "explicit auth database" not in captured.err
    assert "is_admin" in captured.err


def test_identitymgr_reports_missing_group_tables_before_reading_password(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database_path = tmp_path / "users-only.sqlite3"
    database_url = sqlite_file_url(database_path)
    assert run_auth_migration(["--database-url", database_url, "init"]) == 0
    assert (
        run_auth_migration(["--database-url", database_url, "upgrade", "b7f8c3b4b2a1"])
        == 0
    )
    set_identitymgr_database_url(monkeypatch, tmp_path, database_url)
    stdin = io.StringIO(f"{STRONG_TEST_PASSWORD}\n")
    monkeypatch.setattr(sys, "stdin", stdin)

    exit_code = identitymgr.main(
        ["user", "create", "missing-groups@example.com", "--password", "-"]
    )

    assert exit_code == 1
    assert stdin.tell() == 0
    captured = capsys.readouterr()
    assert "Auth database schema is not up to date" in captured.err
    assert "Missing identity_group table" in captured.err


def test_identitymgr_reports_missing_identity_table_before_reading_password(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database_path = tmp_path / "missing-identity.sqlite3"
    set_identitymgr_database_url(monkeypatch, tmp_path, sqlite_file_url(database_path))
    stdin = io.StringIO(f"{STRONG_TEST_PASSWORD}\n")
    monkeypatch.setattr(sys, "stdin", stdin)

    exit_code = identitymgr.main(
        ["user", "create", "missing@example.com", "--password", "-"]
    )

    assert exit_code == 1
    assert stdin.tell() == 0
    captured = capsys.readouterr()
    assert "Auth database schema is not up to date" in captured.err
    assert "Missing identity_user table" in captured.err
    assert "Missing identity_user columns" not in captured.err


def test_identitymgr_identity_schema_error_uses_qualified_table_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class MissingTableSession:
        async def run_sync(self, _function):
            return authmgr_schema.IdentitySchemaStatus(
                primary_table_name="identity_user",
                table_exists=False,
                missing_columns=(),
            )

    monkeypatch.setattr(User.__table__, "schema", "auth")

    with pytest.raises(ConfigurationError) as exc_info:
        asyncio.run(authmgr_schema._verify_identity_schema(MissingTableSession()))  # type: ignore[arg-type]

    assert "Missing auth.identity_user table" in str(exc_info.value)


def test_identitymgr_identity_schema_missing_columns_are_table_aware(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class MissingColumnSession:
        async def run_sync(self, _function):
            return authmgr_schema.IdentitySchemaStatus(
                primary_table_name="identity_user",
                table_exists=True,
                missing_columns=("identity_group.description",),
            )

    with pytest.raises(ConfigurationError) as exc_info:
        asyncio.run(authmgr_schema._verify_identity_schema(MissingColumnSession()))  # type: ignore[arg-type]

    message = str(exc_info.value)
    assert "Missing identity schema columns: identity_group.description" in message
    assert "Missing identity_user columns" not in message


def test_identitymgr_identity_schema_status_normalises_column_name_case(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tables_by_name = {
        table.name: table
        for table in (
            User.__table__,
            Group.__table__,
            Scope.__table__,
            GroupScope.__table__,
            GroupUser.__table__,
            GroupGroup.__table__,
            IdentityUserEmail.__table__,
        )
    }

    class FakeInspector:
        def has_table(self, table_name: str, *, schema: str | None = None) -> bool:
            assert table_name in tables_by_name
            assert schema == tables_by_name[table_name].schema
            return True

        def get_columns(
            self,
            table_name: str,
            *,
            schema: str | None = None,
        ) -> list[dict[str, str]]:
            assert table_name in tables_by_name
            assert schema == tables_by_name[table_name].schema
            return [
                {"name": str(column.name).upper()}
                for column in tables_by_name[table_name].columns
            ]

    class FakeSession:
        def get_bind(self) -> object:
            return object()

    monkeypatch.setattr(
        authmgr_schema, "sqlalchemy_inspect", lambda _bind: FakeInspector()
    )

    status = authmgr_schema._identity_schema_status(FakeSession())  # type: ignore[arg-type]

    assert status.table_exists is True
    assert status.missing_columns == ()


def test_identitymgr_reports_schema_inspection_error_without_leaking_context(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class FailingSession:
        async def run_sync(self, _function):
            raise SQLAlchemyError("database is locked")

    with caplog.at_level(logging.DEBUG, logger="wevra.auth.cli.authmgr"):
        with pytest.raises(ConfigurationError) as exc_info:
            asyncio.run(authmgr_schema._verify_identity_schema(FailingSession()))  # type: ignore[arg-type]

    message = str(exc_info.value)
    assert "Auth database schema could not be inspected" in message
    assert "SQLAlchemyError" not in message
    assert "database is locked" not in message
    assert "SQLAlchemyError" in caplog.text
    assert "database is locked" in caplog.text


def test_authentication_finalisation_updates_last_login_timestamp() -> None:
    web_app = create_auth_test_app()

    async def assert_last_login_update() -> None:
        async with web_app.state.database.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

        async with session_scope(web_app.state.database.session_factory) as session:
            manager = create_user_manager(
                session,
                web_app.state.settings.identity_options,
            )
            user = await manager.create(
                UserCreate(
                    email="login-time@example.com",
                    password=STRONG_TEST_PASSWORD,
                ),
                safe=True,
            )
            assert user.last_login_at is None

        result = await identity_sessions.complete_authentication_ceremony(
            Request({"type": "http", "app": web_app}),
            user,
        )

        assert result.is_ok() is True

        async with session_scope(web_app.state.database.session_factory) as session:
            refreshed_user = await session.get(User, user.id)
            assert refreshed_user is not None
            assert isinstance(refreshed_user.last_login_at, float)
            assert refreshed_user.last_login_at > 0

    try:
        asyncio.run(assert_last_login_update())
    finally:
        asyncio.run(close_database(web_app.state.database))


def test_expired_user_is_rejected_during_authentication_finalisation() -> None:
    web_app = create_auth_test_app()

    async def assert_expired_user_rejected() -> None:
        async with web_app.state.database.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

        async with session_scope(web_app.state.database.session_factory) as session:
            manager = create_user_manager(
                session,
                web_app.state.settings.identity_options,
            )
            user = await manager.create(
                UserCreate(
                    email="expired-login@example.com",
                    password=STRONG_TEST_PASSWORD,
                ),
                safe=True,
            )
            user.expires_at = time() - 60
            await session.commit()

        result = await identity_sessions.complete_authentication_ceremony(
            Request({"type": "http", "app": web_app}),
            user,
        )

        assert result.is_failure() is True
        assert result.error_type == ERROR_INACTIVE_USER

        async with session_scope(web_app.state.database.session_factory) as session:
            refreshed_user = await session.get(User, user.id)
            assert refreshed_user is not None
            assert refreshed_user.last_login_at is None

    try:
        asyncio.run(assert_expired_user_rejected())
    finally:
        asyncio.run(close_database(web_app.state.database))


def test_inactive_user_is_rejected_during_authentication_finalisation() -> None:
    web_app = create_auth_test_app()

    async def assert_inactive_user_rejected() -> None:
        async with web_app.state.database.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

        async with session_scope(web_app.state.database.session_factory) as session:
            manager = create_user_manager(
                session,
                web_app.state.settings.identity_options,
            )
            user = await manager.create(
                UserCreate(
                    email="inactive-login@example.com",
                    password=STRONG_TEST_PASSWORD,
                ),
                safe=True,
            )
            user.is_active = False
            await session.commit()

        result = await identity_sessions.complete_authentication_ceremony(
            Request({"type": "http", "app": web_app}),
            user,
        )

        assert result.is_failure() is True
        assert result.error_type == ERROR_INACTIVE_USER

        async with session_scope(web_app.state.database.session_factory) as session:
            refreshed_user = await session.get(User, user.id)
            assert refreshed_user is not None
            assert refreshed_user.last_login_at is None

    try:
        asyncio.run(assert_inactive_user_rejected())
    finally:
        asyncio.run(close_database(web_app.state.database))


def test_is_user_effectively_active_uses_exclusive_expiry_boundary() -> None:
    now = 200.0
    user = User(email="boundary@example.com", hashed_password="hash")
    user.is_active = True

    user.expires_at = now
    assert identity_management.is_user_effectively_active(user, now=now) is False

    user.expires_at = now + 0.001
    assert identity_management.is_user_effectively_active(user, now=now) is True


def test_request_verification_records_email_verification_sent_timestamp() -> None:
    web_app = create_auth_test_app()
    web_app.state.identity_delivery = CaptureDelivery(verification_tokens=[])

    async def seed_user() -> None:
        async with web_app.state.database.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

        async with session_scope(web_app.state.database.session_factory) as session:
            manager = create_user_manager(
                session,
                web_app.state.settings.identity_options,
            )
            await manager.create(
                UserCreate(
                    email="verify-time@example.com",
                    password=STRONG_TEST_PASSWORD,
                ),
                safe=True,
            )

    async def assert_verification_timestamp() -> None:
        await identity_sessions.request_verification(
            Request({"type": "http", "app": web_app}),
            "verify-time@example.com",
        )

        async with session_scope(web_app.state.database.session_factory) as session:
            user = (
                await session.execute(
                    select(User).where(User.email == "verify-time@example.com")
                )
            ).scalar_one()
            assert isinstance(user.email_verification_sent_at, float)
            assert user.email_verification_sent_at > 0

    try:
        asyncio.run(seed_user())
        asyncio.run(assert_verification_timestamp())
    finally:
        asyncio.run(close_database(web_app.state.database))


def test_request_verification_commits_timestamp_before_delivery() -> None:
    web_app = create_auth_test_app()
    delivery = TimestampVisibleVerificationDelivery(
        session_factory=web_app.state.database.session_factory,
        verification_tokens=[],
    )
    web_app.state.identity_delivery = delivery

    async def seed_user() -> None:
        async with web_app.state.database.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

        async with session_scope(web_app.state.database.session_factory) as session:
            manager = create_user_manager(
                session,
                web_app.state.settings.identity_options,
            )
            await manager.create(
                UserCreate(
                    email="verify-atomic@example.com",
                    password=STRONG_TEST_PASSWORD,
                ),
                safe=True,
            )

    async def assert_timestamp_visible_to_delivery() -> None:
        await identity_sessions.request_verification(
            Request({"type": "http", "app": web_app}),
            "verify-atomic@example.com",
        )

        assert len(delivery.verification_tokens) == 1
        assert delivery.verification_tokens[0][0] == "verify-atomic@example.com"
        assert isinstance(delivery.visible_timestamp, float)
        assert delivery.visible_timestamp > 0

    try:
        asyncio.run(seed_user())
        asyncio.run(assert_timestamp_visible_to_delivery())
    finally:
        asyncio.run(close_database(web_app.state.database))


def test_request_verification_ignores_missing_users_without_modifying_rows() -> None:
    web_app = create_auth_test_app()
    web_app.state.identity_delivery = CaptureDelivery(verification_tokens=[])

    async def assert_missing_user_is_ignored() -> None:
        async with web_app.state.database.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

        await identity_sessions.request_verification(
            Request({"type": "http", "app": web_app}),
            "missing@example.com",
        )

        async with session_scope(web_app.state.database.session_factory) as session:
            users = (await session.execute(select(User))).scalars().all()
            assert users == []
            assert web_app.state.identity_delivery.verification_tokens == []

    try:
        asyncio.run(assert_missing_user_is_ignored())
    finally:
        asyncio.run(close_database(web_app.state.database))


@pytest.mark.parametrize(
    ("field_name", "field_value"),
    [
        ("is_active", False),
        ("expires_at", time() - 60),
    ],
)
def test_request_verification_does_not_overwrite_ineligible_user_timestamp(
    field_name: str,
    field_value: object,
) -> None:
    web_app = create_auth_test_app()
    web_app.state.identity_delivery = CaptureDelivery(verification_tokens=[])

    async def assert_ineligible_user_timestamp_is_preserved() -> None:
        async with web_app.state.database.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

        async with session_scope(web_app.state.database.session_factory) as session:
            manager = create_user_manager(
                session,
                web_app.state.settings.identity_options,
            )
            user = await manager.create(
                UserCreate(
                    email=f"{field_name.replace('_', '-')}@example.com",
                    password=STRONG_TEST_PASSWORD,
                ),
                safe=True,
            )
            setattr(user, field_name, field_value)
            user.email_verification_sent_at = 123.0
            await session.commit()
            email = user.email

        await identity_sessions.request_verification(
            Request({"type": "http", "app": web_app}),
            email,
        )

        async with session_scope(web_app.state.database.session_factory) as session:
            refreshed_user = (
                await session.execute(select(User).where(User.email == email))
            ).scalar_one()
            assert refreshed_user.email_verification_sent_at == 123.0
            assert web_app.state.identity_delivery.verification_tokens == []

    try:
        asyncio.run(assert_ineligible_user_timestamp_is_preserved())
    finally:
        asyncio.run(close_database(web_app.state.database))


def test_identitymgr_create_user_with_metadata_from_stdin_password(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = sqlite_file_url(tmp_path / "users.sqlite3")
    initialise_identity_database(database_url)
    set_identitymgr_database_url(monkeypatch, tmp_path, database_url)
    monkeypatch.setattr(sys, "stdin", io.StringIO(f"{STRONG_TEST_PASSWORD}\n"))

    exit_code = identitymgr.main(
        [
            "user",
            "create",
            "operator@example.com",
            "--password",
            "-",
            "--admin",
            "--superuser",
            "--unverified",
            "--display-name",
            "Operator Example",
            "--preferred-name",
            "Operator",
            "--timezone",
            "Australia/Melbourne",
            "--expires-at",
            "4102444800",
        ]
    )

    assert exit_code == 0

    [user] = identity_users_from_database(database_url)
    assert user.email == "operator@example.com"
    assert user.hashed_password != STRONG_TEST_PASSWORD
    assert user.is_admin is True
    assert user.is_superuser is True
    assert user.is_verified is False
    assert user.display_name == "Operator Example"
    assert user.preferred_name == "Operator"
    assert user.preferred_timezone == "Australia/Melbourne"
    assert user.expires_at == 4102444800.0


def test_identitymgr_scope_commands_manage_scope_records(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database_url = sqlite_file_url(tmp_path / "scope-cli.sqlite3")
    initialise_identity_database(database_url)
    set_identitymgr_database_url(monkeypatch, tmp_path, database_url)

    assert (
        identitymgr.main(
            [
                "scope",
                "create",
                "document:read",
                "--description",
                "Read documents.",
            ]
        )
        == 0
    )
    assert (
        identitymgr.main(
            [
                "scope",
                "update",
                "document:read",
                "--description",
                "Read published documents.",
            ]
        )
        == 0
    )
    assert identitymgr.main(["scope", "list", "--json"]) == 0
    listed = json.loads(capsys.readouterr().out.splitlines()[-1])

    assert listed == [
        {
            "scope": "document:read",
            "description": "Read published documents.",
        }
    ]

    assert identitymgr.main(["scope", "delete", "document:read"]) == 0

    assert scopes_from_database(database_url) == []


def test_identitymgr_scope_delete_rejects_used_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database_url = sqlite_file_url(tmp_path / "used-scope-cli.sqlite3")
    initialise_identity_database(database_url)
    set_identitymgr_database_url(monkeypatch, tmp_path, database_url)

    assert identitymgr.main(["scope", "create", "admin:read"]) == 0
    assert identitymgr.main(["group", "create", "admins", "--scope", "admin:read"]) == 0

    assert identitymgr.main(["scope", "delete", "admin:read"]) == 1

    assert "Scope is assigned to one or more groups." in capsys.readouterr().err
    assert [scope.scope for scope in scopes_from_database(database_url)] == [
        "admin:read"
    ]


def test_identitymgr_group_target_first_commands_manage_group(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database_url = sqlite_file_url(tmp_path / "group-cli.sqlite3")
    initialise_identity_database(database_url)
    set_identitymgr_database_url(monkeypatch, tmp_path, database_url)

    assert identitymgr.main(["scope", "create", "project:read"]) == 0
    assert identitymgr.main(["scope", "create", "project:write"]) == 0
    assert (
        identitymgr.main(
            [
                "group",
                "create",
                "project",
                "--description",
                "Project access",
                "--scope",
                "project:read",
            ]
        )
        == 0
    )
    assert (
        identitymgr.main(
            [
                "group",
                "project",
                "update",
                "--description",
                "Project operators",
                "--scope",
                "project:write",
                "--rm-scope",
                "project:read",
            ]
        )
        == 0
    )
    assert identitymgr.main(["group", "project", "show", "--json"]) == 0
    shown = json.loads(capsys.readouterr().out.splitlines()[-1])

    assert shown["abbrev"] == "project"
    assert shown["description"] == "Project operators"
    assert shown["scopes"] == ["project:write"]
    assert group_scopes_from_database(database_url, "project") == ["project:write"]

    assert identitymgr.main(["group", "project", "delete", "--force"]) == 0


def test_identitymgr_group_membership_commands_manage_users_and_child_groups(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database_url = sqlite_file_url(tmp_path / "group-membership-cli.sqlite3")
    initialise_identity_database(database_url)
    set_identitymgr_database_url(monkeypatch, tmp_path, database_url)
    monkeypatch.setattr(sys, "stdin", io.StringIO(f"{STRONG_TEST_PASSWORD}\n"))

    assert (
        identitymgr.main(["user", "create", "member@example.com", "--password", "-"])
        == 0
    )
    assert identitymgr.main(["group", "create", "parent"]) == 0
    assert identitymgr.main(["group", "create", "child"]) == 0
    assert identitymgr.main(["group", "parent", "add-user", "member@example.com"]) == 0
    assert identitymgr.main(["group", "parent", "add-group", "child"]) == 0
    assert identitymgr.main(["group", "parent", "show", "--json"]) == 0
    shown = json.loads(capsys.readouterr().out.splitlines()[-1])

    assert shown["users"] == ["member@example.com"]
    assert shown["child_groups"] == ["child"]

    assert (
        identitymgr.main(["group", "parent", "remove-user", "member@example.com"]) == 0
    )
    assert identitymgr.main(["group", "parent", "remove-group", "child"]) == 0
    assert identitymgr.main(["group", "parent", "delete", "--force"]) == 0


def test_identitymgr_group_parser_disambiguates_user_and_group_targets() -> None:
    ctx = click.Context(identitymgr.authmgr_command, obj={})

    user_args = authmgr_groups._target_group_args(
        ctx,
        ("parent", "add-user", "member@example.com"),
    )
    group_args = authmgr_groups._target_group_args(
        ctx, ("parent", "add-group", "child")
    )

    assert user_args.user_target == "member@example.com"
    assert user_args.child_group_target == ""
    assert group_args.user_target == ""
    assert group_args.child_group_target == "child"


def test_identitymgr_create_and_update_user_group_memberships(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database_url = sqlite_file_url(tmp_path / "user-groups-cli.sqlite3")
    initialise_identity_database(database_url)
    set_identitymgr_database_url(monkeypatch, tmp_path, database_url)
    monkeypatch.setattr(
        sys,
        "stdin",
        io.StringIO(f"{STRONG_TEST_PASSWORD}\n"),
    )

    for abbrev in ("alpha", "beta", "gamma"):
        assert identitymgr.main(["group", "create", abbrev]) == 0
    assert (
        identitymgr.main(
            [
                "user",
                "create",
                "grouped@example.com",
                "--password",
                "-",
                "--group",
                "alpha",
                "--group",
                "beta",
            ]
        )
        == 0
    )
    assert user_group_abbrevs_from_database(database_url, "grouped@example.com") == [
        "alpha",
        "beta",
    ]

    assert (
        identitymgr.main(
            [
                "user",
                "update",
                "grouped@example.com",
                "--rm-group",
                "alpha",
                "--add-group",
                "gamma",
            ]
        )
        == 0
    )
    assert "updated user: grouped@example.com" in capsys.readouterr().out
    assert user_group_abbrevs_from_database(database_url, "grouped@example.com") == [
        "beta",
        "gamma",
    ]

    assert (
        identitymgr.main(
            [
                "user",
                "update",
                "grouped@example.com",
                "--set-group",
                "alpha",
            ]
        )
        == 0
    )
    assert user_group_abbrevs_from_database(database_url, "grouped@example.com") == [
        "alpha"
    ]


def test_identitymgr_create_with_missing_group_does_not_create_user(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = sqlite_file_url(tmp_path / "missing-create-group-cli.sqlite3")
    initialise_identity_database(database_url)
    set_identitymgr_database_url(monkeypatch, tmp_path, database_url)
    stdin = io.StringIO(f"{STRONG_TEST_PASSWORD}\n")
    monkeypatch.setattr(sys, "stdin", stdin)

    assert (
        identitymgr.main(
            [
                "user",
                "create",
                "missing-create-group@example.com",
                "--password",
                "-",
                "--group",
                "missing",
            ]
        )
        == 1
    )

    assert stdin.tell() == 0
    assert (
        identity_user_from_database(database_url, "missing-create-group@example.com")
        is None
    )


def test_identitymgr_set_group_validates_targets_before_replacing_memberships(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = sqlite_file_url(tmp_path / "user-groups-invalid-cli.sqlite3")
    initialise_identity_database(database_url)
    set_identitymgr_database_url(monkeypatch, tmp_path, database_url)
    monkeypatch.setattr(sys, "stdin", io.StringIO(f"{STRONG_TEST_PASSWORD}\n"))

    assert identitymgr.main(["group", "create", "alpha"]) == 0
    assert identitymgr.main(["group", "create", "beta"]) == 0
    assert (
        identitymgr.main(
            [
                "user",
                "create",
                "invalid-set@example.com",
                "--password",
                "-",
                "--group",
                "alpha",
            ]
        )
        == 0
    )

    assert (
        identitymgr.main(
            [
                "user",
                "update",
                "invalid-set@example.com",
                "--set-group",
                "beta",
                "--set-group",
                "missing",
            ]
        )
        == 1
    )

    assert user_group_abbrevs_from_database(
        database_url, "invalid-set@example.com"
    ) == ["alpha"]


def test_identitymgr_update_rejects_group_replacement_shortcut() -> None:
    result = CliRunner().invoke(
        identitymgr.authmgr_command,
        ["user", "update", "user@example.com", "--group", "admins"],
    )

    assert result.exit_code == 2
    assert "use --set-group for replacement" in result.output


@pytest.mark.parametrize("incremental_option", ["--add-group", "--rm-group"])
def test_identitymgr_update_rejects_group_replacement_with_incremental_edits(
    incremental_option: str,
) -> None:
    result = CliRunner().invoke(
        identitymgr.authmgr_command,
        [
            "user",
            "update",
            "user@example.com",
            "--set-group",
            "admins",
            incremental_option,
            "editors",
        ],
    )

    assert result.exit_code == 2
    assert "--set-group cannot be used with --add-group or --rm-group." in result.output


def test_identitymgr_record_formatting_json_encodes_nested_values(
    capsys: pytest.CaptureFixture[str],
) -> None:
    record = {
        "email": "nested@example.com",
        "groups": [{"abbrev": "admins", "scopes": ["read", "write"]}],
    }

    assert authmgr_output._format_record_value(record["groups"]) == (
        '[{"abbrev": "admins", "scopes": ["read", "write"]}]'
    )

    authmgr_output._print_records(
        [record],
        field_names=("email", "groups"),
        json_output=False,
        csv_output=True,
    )
    rows = list(csv.DictReader(io.StringIO(capsys.readouterr().out)))

    assert rows == [
        {
            "email": "nested@example.com",
            "groups": '[{"abbrev": "admins", "scopes": ["read", "write"]}]',
        }
    ]


def test_identitymgr_group_effective_scopes_reports_folded_scopes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database_url = sqlite_file_url(tmp_path / "effective-scopes-cli.sqlite3")
    initialise_identity_database(database_url)
    set_identitymgr_database_url(monkeypatch, tmp_path, database_url)
    monkeypatch.setattr(sys, "stdin", io.StringIO(f"{STRONG_TEST_PASSWORD}\n"))

    assert identitymgr.main(["scope", "create", "project:read"]) == 0
    assert (
        identitymgr.main(["group", "create", "readers", "--scope", "project:read"]) == 0
    )
    assert (
        identitymgr.main(["user", "create", "reader@example.com", "--password", "-"])
        == 0
    )
    assert identitymgr.main(["group", "readers", "add-user", "reader@example.com"]) == 0

    assert (
        identitymgr.main(["group", "effective-scopes", "reader@example.com", "--json"])
        == 0
    )
    effective_scopes = json.loads(capsys.readouterr().out.splitlines()[-1])

    assert effective_scopes["scopes"] == ["project:read"]
    assert effective_scopes["groups"] == ["readers"]
    assert effective_scopes["user"]["email"] == "reader@example.com"


def test_identitymgr_create_rejects_invalid_timezone_without_creating_user(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database_url = sqlite_file_url(tmp_path / "invalid-create-timezone.sqlite3")
    initialise_identity_database(database_url)
    set_identitymgr_database_url(monkeypatch, tmp_path, database_url)
    monkeypatch.setattr(sys, "stdin", io.StringIO(f"{STRONG_TEST_PASSWORD}\n"))

    exit_code = identitymgr.main(
        [
            "user",
            "create",
            "invalid-timezone@example.com",
            "--password",
            "-",
            "--timezone",
            "Not/AZone",
        ]
    )

    assert exit_code == 1
    assert "Preferred timezone is invalid." in capsys.readouterr().err
    assert identity_users_from_database(database_url) == []


def test_identitymgr_update_rejects_invalid_timezone_without_updating_user(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database_url = sqlite_file_url(tmp_path / "invalid-update-timezone.sqlite3")
    initialise_identity_database(database_url)
    set_identitymgr_database_url(monkeypatch, tmp_path, database_url)
    monkeypatch.setattr(sys, "stdin", io.StringIO(f"{STRONG_TEST_PASSWORD}\n"))
    assert (
        identitymgr.main(
            [
                "user",
                "create",
                "invalid-update-timezone@example.com",
                "--password",
                "-",
                "--timezone",
                "UTC",
            ]
        )
        == 0
    )
    capsys.readouterr()

    exit_code = identitymgr.main(
        [
            "user",
            "update",
            "invalid-update-timezone@example.com",
            "--timezone",
            "Not/AZone",
        ]
    )

    assert exit_code == 1
    assert "Preferred timezone is invalid." in capsys.readouterr().err
    [user] = identity_users_from_database(database_url)
    assert user.preferred_timezone == "UTC"


def test_identitymgr_password_from_stdin_trims_crlf(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO("correct horse\r\n"))

    assert authmgr_passwords._read_password("-") == "correct horse"


def test_identitymgr_password_from_stdin_rejects_extra_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO("correct horse\nextra\n"))

    with pytest.raises(authmgr_passwords.PasswordSourceError, match="exactly one line"):
        authmgr_passwords._read_password("-")


def test_identitymgr_password_from_stdin_preserves_whitespace_and_strips_newline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO("  spacey  \n"))

    assert authmgr_passwords._read_password("-") == "  spacey  "


def test_identitymgr_password_from_stdin_rejects_empty_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))

    with pytest.raises(
        authmgr_passwords.PasswordSourceError, match="No password received"
    ):
        authmgr_passwords._read_password("-")


def test_identitymgr_password_from_stdin_rejects_tty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stdin = io.StringIO("correct horse\n")
    stdin.isatty = lambda: True  # type: ignore[method-assign]
    monkeypatch.setattr(sys, "stdin", stdin)

    with pytest.raises(
        authmgr_passwords.PasswordSourceError, match="interactive stdin"
    ):
        authmgr_passwords._read_password("-")


def test_identitymgr_read_password_rejects_invalid_source() -> None:
    with pytest.raises(
        authmgr_passwords.PasswordSourceError, match="Unsupported password source"
    ):
        authmgr_passwords._read_password("invalid")


def test_identitymgr_create_rejects_duplicate_email(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database_url = sqlite_file_url(tmp_path / "duplicate.sqlite3")
    initialise_identity_database(database_url)
    set_identitymgr_database_url(monkeypatch, tmp_path, database_url)

    monkeypatch.setattr(sys, "stdin", io.StringIO(f"{STRONG_TEST_PASSWORD}\n"))
    assert (
        identitymgr.main(["user", "create", "duplicate@example.com", "--password", "-"])
        == 0
    )

    monkeypatch.setattr(sys, "stdin", io.StringIO(f"{STRONG_TEST_PASSWORD}\n"))
    exit_code = identitymgr.main(
        ["user", "create", "duplicate@example.com", "--password", "-"]
    )

    assert exit_code == 1
    assert "already exists" in capsys.readouterr().err
    assert len(identity_users_from_database(database_url)) == 1


def test_identitymgr_create_rejects_duplicate_secondary_email(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database_url = sqlite_file_url(tmp_path / "duplicate-secondary.sqlite3")
    initialise_identity_database(database_url)
    web_app = create_auth_test_app(database_url=database_url)

    async def seed_secondary_email_user() -> None:
        async with web_app.state.database.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

        async with session_scope(web_app.state.database.session_factory) as session:
            manager = create_user_manager(
                session,
                web_app.state.settings.identity_options,
            )
            user = await manager.create(
                UserCreate(
                    email="primary@example.com",
                    password=STRONG_TEST_PASSWORD,
                ),
                safe=True,
            )
            session.add(
                IdentityUserEmail(
                    user_id=user.id,
                    email="linked@example.com",
                    is_primary=False,
                    is_verified=True,
                )
            )
            await session.commit()

    try:
        asyncio.run(seed_secondary_email_user())
        set_identitymgr_database_url(monkeypatch, tmp_path, database_url)
        monkeypatch.setattr(sys, "stdin", io.StringIO(f"{STRONG_TEST_PASSWORD}\n"))

        exit_code = identitymgr.main(
            ["user", "create", "linked@example.com", "--password", "-"]
        )

        assert exit_code == 1
        assert "already exists" in capsys.readouterr().err
    finally:
        asyncio.run(close_database(web_app.state.database))


def test_identitymgr_list_json_omits_null_fields(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database_url = sqlite_file_url(tmp_path / "list-json.sqlite3")
    initialise_identity_database(database_url)
    set_identitymgr_database_url(monkeypatch, tmp_path, database_url)
    monkeypatch.setattr(sys, "stdin", io.StringIO(f"{STRONG_TEST_PASSWORD}\n"))
    assert (
        identitymgr.main(
            [
                "user",
                "create",
                "listed@example.com",
                "--password",
                "-",
                "--display-name",
                "Listed User",
            ]
        )
        == 0
    )
    capsys.readouterr()

    exit_code = identitymgr.main(["user", "list", "--json"])

    assert exit_code == 0
    [record] = json.loads(capsys.readouterr().out)
    assert record["email"] == "listed@example.com"
    assert record["display_name"] == "Listed User"
    assert "preferred_name" not in record
    assert "preferred_timezone" not in record
    assert "hashed_password" not in record


def test_identitymgr_update_resolves_id_and_updates_user_fields(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = sqlite_file_url(tmp_path / "update.sqlite3")
    initialise_identity_database(database_url)
    set_identitymgr_database_url(monkeypatch, tmp_path, database_url)
    monkeypatch.setattr(sys, "stdin", io.StringIO(f"{STRONG_TEST_PASSWORD}\n"))
    assert (
        identitymgr.main(["user", "create", "update@example.com", "--password", "-"])
        == 0
    )
    [created_user] = identity_users_from_database(database_url)

    exit_code = identitymgr.main(
        [
            "user",
            "update",
            str(created_user.id),
            "--admin",
            "--superuser",
            "--no-verify",
            "--display-name",
            "Updated User",
            "--preferred-name",
            "Updated",
            "--timezone",
            "UTC",
            "--expires-at",
            "4102444800",
        ]
    )

    assert exit_code == 0
    [user] = identity_users_from_database(database_url)
    assert user.is_admin is True
    assert user.is_superuser is True
    assert user.is_verified is False
    assert user.display_name == "Updated User"
    assert user.preferred_name == "Updated"
    assert user.preferred_timezone == "UTC"
    assert user.expires_at == 4102444800.0


def test_identitymgr_update_no_expires_at_without_existing_expiry_is_noop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database_url = sqlite_file_url(tmp_path / "no-expiry-noop.sqlite3")
    initialise_identity_database(database_url)
    set_identitymgr_database_url(monkeypatch, tmp_path, database_url)
    monkeypatch.setattr(sys, "stdin", io.StringIO(f"{STRONG_TEST_PASSWORD}\n"))
    assert (
        identitymgr.main(["user", "create", "no-expiry@example.com", "--password", "-"])
        == 0
    )
    [created_user] = identity_users_from_database(database_url)
    capsys.readouterr()

    exit_code = identitymgr.main(
        ["user", "update", "no-expiry@example.com", "--no-expires-at"]
    )

    assert exit_code == 1
    assert "No user changes" in capsys.readouterr().err
    [user] = identity_users_from_database(database_url)
    assert user.expires_at is None
    assert user.modified_at == created_user.modified_at


def test_identitymgr_update_can_clear_optional_string_fields(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = sqlite_file_url(tmp_path / "clear-optional-fields.sqlite3")
    initialise_identity_database(database_url)
    set_identitymgr_database_url(monkeypatch, tmp_path, database_url)
    monkeypatch.setattr(sys, "stdin", io.StringIO(f"{STRONG_TEST_PASSWORD}\n"))
    assert (
        identitymgr.main(["user", "create", "clear@example.com", "--password", "-"])
        == 0
    )
    assert (
        identitymgr.main(
            [
                "user",
                "update",
                "clear@example.com",
                "--display-name",
                "Clear Example",
                "--preferred-name",
                "Clear",
                "--timezone",
                "UTC",
            ]
        )
        == 0
    )

    exit_code = identitymgr.main(
        [
            "user",
            "update",
            "clear@example.com",
            "--no-display-name",
            "--no-preferred-name",
            "--no-timezone",
        ]
    )

    assert exit_code == 0
    [user] = identity_users_from_database(database_url)
    assert user.display_name is None
    assert user.preferred_name is None
    assert user.preferred_timezone is None


@pytest.mark.parametrize(
    ("target", "expected_message"),
    [
        ("not-a-user-id", "valid user ID"),
        ("not-an-email@", "email address is invalid"),
    ],
)
def test_identitymgr_update_reports_malformed_targets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    target: str,
    expected_message: str,
) -> None:
    database_url = sqlite_file_url(tmp_path / "malformed-target.sqlite3")
    initialise_identity_database(database_url)
    set_identitymgr_database_url(monkeypatch, tmp_path, database_url)

    exit_code = identitymgr.main(["user", "update", target, "--admin"])

    assert exit_code == 1
    assert expected_message in capsys.readouterr().err


def test_identitymgr_update_rejects_final_superuser_demotion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database_url = sqlite_file_url(tmp_path / "final-superuser.sqlite3")
    initialise_identity_database(database_url)
    set_identitymgr_database_url(monkeypatch, tmp_path, database_url)
    monkeypatch.setattr(sys, "stdin", io.StringIO(f"{STRONG_TEST_PASSWORD}\n"))
    assert (
        identitymgr.main(
            ["user", "create", "root@example.com", "--password", "-", "--superuser"]
        )
        == 0
    )

    exit_code = identitymgr.main(
        ["user", "update", "root@example.com", "--no-superuser"]
    )

    assert exit_code == 1
    assert "final superuser" in capsys.readouterr().err
    [user] = identity_users_from_database(database_url)
    assert user.is_superuser is True


def test_identitymgr_delete_and_deactivate_protect_superusers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database_url = sqlite_file_url(tmp_path / "protect-superuser.sqlite3")
    initialise_identity_database(database_url)
    set_identitymgr_database_url(monkeypatch, tmp_path, database_url)
    monkeypatch.setattr(sys, "stdin", io.StringIO(f"{STRONG_TEST_PASSWORD}\n"))
    assert (
        identitymgr.main(
            [
                "user",
                "create",
                "protected@example.com",
                "--password",
                "-",
                "--superuser",
            ]
        )
        == 0
    )

    assert identitymgr.main(["user", "delete", "protected@example.com", "--force"]) == 1
    assert (
        identitymgr.main(["user", "deactivate", "protected@example.com", "--force"])
        == 1
    )

    captured = capsys.readouterr()
    assert "superuser" in captured.err
    [user] = identity_users_from_database(database_url)
    assert user.is_active is True


def test_identitymgr_delete_protects_non_final_superuser(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database_url = sqlite_file_url(tmp_path / "protect-non-final-superuser.sqlite3")
    initialise_identity_database(database_url)
    set_identitymgr_database_url(monkeypatch, tmp_path, database_url)
    for email in ("first-root@example.com", "second-root@example.com"):
        monkeypatch.setattr(sys, "stdin", io.StringIO(f"{STRONG_TEST_PASSWORD}\n"))
        assert (
            identitymgr.main(
                ["user", "create", email, "--password", "-", "--superuser"]
            )
            == 0
        )

    exit_code = identitymgr.main(
        ["user", "delete", "first-root@example.com", "--force"]
    )

    assert exit_code == 1
    assert "cannot be deleted" in capsys.readouterr().err
    assert {user.email for user in identity_users_from_database(database_url)} == {
        "first-root@example.com",
        "second-root@example.com",
    }


def test_identitymgr_delete_and_deactivate_normal_users_with_force(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = sqlite_file_url(tmp_path / "delete-deactivate.sqlite3")
    initialise_identity_database(database_url)
    set_identitymgr_database_url(monkeypatch, tmp_path, database_url)
    monkeypatch.setattr(sys, "stdin", io.StringIO(f"{STRONG_TEST_PASSWORD}\n"))
    assert (
        identitymgr.main(["user", "create", "delete@example.com", "--password", "-"])
        == 0
    )
    monkeypatch.setattr(sys, "stdin", io.StringIO(f"{STRONG_TEST_PASSWORD}\n"))
    assert (
        identitymgr.main(
            ["user", "create", "deactivate@example.com", "--password", "-"]
        )
        == 0
    )
    delete_token = create_session_token_for_user(database_url, "delete@example.com")
    deactivate_token = create_session_token_for_user(
        database_url,
        "deactivate@example.com",
    )
    assert set(access_tokens_from_database(database_url)) == {
        delete_token,
        deactivate_token,
    }

    assert identitymgr.main(["user", "delete", "delete@example.com", "--force"]) == 0
    assert access_tokens_from_database(database_url) == [deactivate_token]

    assert (
        identitymgr.main(["user", "deactivate", "deactivate@example.com", "--force"])
        == 0
    )

    [remaining_user] = identity_users_from_database(database_url)
    assert remaining_user.email == "deactivate@example.com"
    assert remaining_user.is_active is False
    assert access_tokens_from_database(database_url) == []


def test_identitymgr_deactivate_only_revokes_target_user_sessions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = sqlite_file_url(tmp_path / "deactivate-target-sessions.sqlite3")
    initialise_identity_database(database_url)
    set_identitymgr_database_url(monkeypatch, tmp_path, database_url)
    for email in ("alice@example.com", "bob@example.com"):
        monkeypatch.setattr(sys, "stdin", io.StringIO(f"{STRONG_TEST_PASSWORD}\n"))
        assert identitymgr.main(["user", "create", email, "--password", "-"]) == 0

    alice_token = create_session_token_for_user(database_url, "alice@example.com")
    bob_token = create_session_token_for_user(database_url, "bob@example.com")
    assert set(access_tokens_from_database(database_url)) == {alice_token, bob_token}

    assert identitymgr.main(["user", "deactivate", "alice@example.com", "--force"]) == 0

    assert access_tokens_from_database(database_url) == [bob_token]


def test_identitymgr_delete_confirmation_identifies_resolved_user(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database_url = sqlite_file_url(tmp_path / "delete-confirm.sqlite3")
    initialise_identity_database(database_url)
    set_identitymgr_database_url(monkeypatch, tmp_path, database_url)
    monkeypatch.setattr(sys, "stdin", io.StringIO(f"{STRONG_TEST_PASSWORD}\n"))
    assert (
        identitymgr.main(["user", "create", "confirm@example.com", "--password", "-"])
        == 0
    )
    [user] = identity_users_from_database(database_url)
    monkeypatch.setattr("builtins.input", lambda prompt: print(prompt) or "no")
    capsys.readouterr()

    exit_code = identitymgr.main(["user", "delete", str(user.id)])

    assert exit_code == 1
    assert "confirm@example.com" in capsys.readouterr().out
    assert len(identity_users_from_database(database_url)) == 1


def test_identitymgr_password_revokes_sessions_by_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = sqlite_file_url(tmp_path / "password-revoke.sqlite3")
    initialise_identity_database(database_url)
    set_identitymgr_database_url(monkeypatch, tmp_path, database_url)
    monkeypatch.setattr(sys, "stdin", io.StringIO(f"{STRONG_TEST_PASSWORD}\n"))
    assert (
        identitymgr.main(["user", "create", "password@example.com", "--password", "-"])
        == 0
    )
    token = create_session_token_for_user(database_url, "password@example.com")
    assert access_tokens_from_database(database_url) == [token]

    monkeypatch.setattr(sys, "stdin", io.StringIO(f"{UPDATED_STRONG_TEST_PASSWORD}\n"))
    exit_code = identitymgr.main(
        ["user", "password", "password@example.com", "--password", "-"]
    )

    assert exit_code == 0
    assert access_tokens_from_database(database_url) == []


def test_identitymgr_update_password_revokes_sessions_by_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = sqlite_file_url(tmp_path / "update-password-revoke.sqlite3")
    initialise_identity_database(database_url)
    set_identitymgr_database_url(monkeypatch, tmp_path, database_url)
    monkeypatch.setattr(sys, "stdin", io.StringIO(f"{STRONG_TEST_PASSWORD}\n"))
    assert (
        identitymgr.main(
            ["user", "create", "update-password@example.com", "--password", "-"]
        )
        == 0
    )
    token = create_session_token_for_user(database_url, "update-password@example.com")
    assert access_tokens_from_database(database_url) == [token]

    monkeypatch.setattr(sys, "stdin", io.StringIO(f"{UPDATED_STRONG_TEST_PASSWORD}\n"))
    exit_code = identitymgr.main(
        ["user", "update", "update-password@example.com", "--password", "-"]
    )

    assert exit_code == 0
    assert access_tokens_from_database(database_url) == []


def test_identitymgr_password_can_preserve_sessions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = sqlite_file_url(tmp_path / "password-preserve.sqlite3")
    initialise_identity_database(database_url)
    set_identitymgr_database_url(monkeypatch, tmp_path, database_url)
    monkeypatch.setattr(sys, "stdin", io.StringIO(f"{STRONG_TEST_PASSWORD}\n"))
    assert (
        identitymgr.main(["user", "create", "preserve@example.com", "--password", "-"])
        == 0
    )
    token = create_session_token_for_user(database_url, "preserve@example.com")

    monkeypatch.setattr(sys, "stdin", io.StringIO(f"{UPDATED_STRONG_TEST_PASSWORD}\n"))
    exit_code = identitymgr.main(
        ["user", "password", "preserve@example.com", "--password", "-", "--no-revoke"]
    )

    assert exit_code == 0
    assert access_tokens_from_database(database_url) == [token]


def test_identitymgr_interactive_password_mismatch_aborts_when_input_ends(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = sqlite_file_url(tmp_path / "password-mismatch.sqlite3")
    initialise_identity_database(database_url)
    set_identitymgr_database_url(monkeypatch, tmp_path, database_url)

    result = CliRunner().invoke(
        identitymgr.authmgr_command,
        ["user", "create", "mismatch@example.com"],
        input="first password\nsecond password\n",
    )

    assert result.exit_code == 1
    assert result.stdout == ""
    assert "The two entered values do not match" in result.stderr
    assert "Aborted" in result.stderr
    assert identity_users_from_database(database_url) == []


def test_identitymgr_interactive_password_prompt_retries_after_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = sqlite_file_url(tmp_path / "password-retry.sqlite3")
    initialise_identity_database(database_url)
    set_identitymgr_database_url(monkeypatch, tmp_path, database_url)

    result = CliRunner().invoke(
        identitymgr.authmgr_command,
        ["user", "create", "retry@example.com"],
        input=(
            "first password\n"
            "second password\n"
            f"{STRONG_TEST_PASSWORD}\n"
            f"{STRONG_TEST_PASSWORD}\n"
        ),
    )

    assert result.exit_code == 0
    assert result.stdout == "created user: retry@example.com\n"
    assert "Password:" in result.stderr
    assert "The two entered values do not match" in result.stderr
    [user] = identity_users_from_database(database_url)
    assert user.email == "retry@example.com"


@pytest.mark.parametrize("password_source", ["-", "stdin"])
def test_identitymgr_create_with_stdin_password_does_not_prompt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    password_source: str,
) -> None:
    source_name = "dash" if password_source == "-" else password_source
    email = f"{source_name}@example.com"
    database_url = sqlite_file_url(tmp_path / f"stdin-password-{source_name}.sqlite3")
    initialise_identity_database(database_url)
    set_identitymgr_database_url(monkeypatch, tmp_path, database_url)

    result = CliRunner().invoke(
        identitymgr.authmgr_command,
        ["user", "create", email, "--password", password_source],
        input=f"{STRONG_TEST_PASSWORD}\n",
    )

    assert result.exit_code == 0
    assert result.stdout == f"created user: {email}\n"
    assert "Password:" not in result.stderr
    [user] = identity_users_from_database(database_url)
    assert user.email == email


def test_identitymgr_create_with_empty_stdin_password_reports_password_option(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = sqlite_file_url(tmp_path / "stdin-password-empty.sqlite3")
    initialise_identity_database(database_url)
    set_identitymgr_database_url(monkeypatch, tmp_path, database_url)

    result = CliRunner().invoke(
        identitymgr.authmgr_command,
        ["user", "create", "empty-stdin@example.com", "--password", "-"],
        input="",
    )

    assert result.exit_code == 2
    assert "Invalid value for '--password'" in result.output
    assert "No password received on stdin." in result.output
    assert identity_users_from_database(database_url) == []


def test_identitymgr_password_command_prompts_by_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = sqlite_file_url(tmp_path / "password-default-prompt.sqlite3")
    initialise_identity_database(database_url)
    set_identitymgr_database_url(monkeypatch, tmp_path, database_url)
    monkeypatch.setattr(sys, "stdin", io.StringIO(f"{STRONG_TEST_PASSWORD}\n"))
    assert (
        identitymgr.main(
            ["user", "create", "default-prompt@example.com", "--password", "-"]
        )
        == 0
    )

    result = CliRunner().invoke(
        identitymgr.authmgr_command,
        ["user", "password", "default-prompt@example.com"],
        input=f"{UPDATED_STRONG_TEST_PASSWORD}\n{UPDATED_STRONG_TEST_PASSWORD}\n",
    )

    assert result.exit_code == 0
    assert result.stdout == "changed password: default-prompt@example.com\n"
    assert "Password:" in result.stderr


def test_identitymgr_list_filters_by_email_domain_flags_and_effective_activity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database_url = sqlite_file_url(tmp_path / "list-filters.sqlite3")
    initialise_identity_database(database_url)
    set_identitymgr_database_url(monkeypatch, tmp_path, database_url)
    for email in ("alpha@example.com", "beta@example.org", "gamma@example.com"):
        monkeypatch.setattr(sys, "stdin", io.StringIO(f"{STRONG_TEST_PASSWORD}\n"))
        assert identitymgr.main(["user", "create", email, "--password", "-"]) == 0
    assert identitymgr.main(["user", "update", "alpha@example.com", "--admin"]) == 0
    assert identitymgr.main(["user", "deactivate", "beta@example.org", "--force"]) == 0
    update_user_fields(database_url, "gamma@example.com", expires_at=time() - 60)
    capsys.readouterr()

    assert (
        identitymgr.main(
            ["user", "list", "--json", "--domain", "example.com", "--admin"]
        )
        == 0
    )
    [admin_record] = json.loads(capsys.readouterr().out)
    assert admin_record["email"] == "alpha@example.com"

    assert identitymgr.main(["user", "list", "--json", "--inactive"]) == 0
    inactive_emails = {
        record["email"] for record in json.loads(capsys.readouterr().out)
    }
    assert inactive_emails == {"beta@example.org", "gamma@example.com"}


def test_identitymgr_list_uses_shared_effective_active_timestamp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database_url = sqlite_file_url(tmp_path / "list-now.sqlite3")
    initialise_identity_database(database_url)
    set_identitymgr_database_url(monkeypatch, tmp_path, database_url)
    monkeypatch.setattr(sys, "stdin", io.StringIO(f"{STRONG_TEST_PASSWORD}\n"))
    assert (
        identitymgr.main(["user", "create", "boundary@example.com", "--password", "-"])
        == 0
    )
    update_user_fields(database_url, "boundary@example.com", expires_at=200.0)
    capsys.readouterr()

    clock_values = iter([100.0, 300.0])
    monkeypatch.setattr(
        "wevra.auth.admin.management.current_timestamp",
        lambda: next(clock_values),
    )

    assert identitymgr.main(["user", "list", "--json", "--active"]) == 0

    [record] = json.loads(capsys.readouterr().out)
    assert record["email"] == "boundary@example.com"
    assert record["effective_active"] is True


def test_identitymgr_active_filter_uses_exclusive_expiry_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database_url = sqlite_file_url(tmp_path / "active-expiry-boundary.sqlite3")
    initialise_identity_database(database_url)
    set_identitymgr_database_url(monkeypatch, tmp_path, database_url)
    monkeypatch.setattr(sys, "stdin", io.StringIO(f"{STRONG_TEST_PASSWORD}\n"))
    assert (
        identitymgr.main(["user", "create", "boundary@example.com", "--password", "-"])
        == 0
    )
    update_user_fields(database_url, "boundary@example.com", expires_at=200.0)
    monkeypatch.setattr("wevra.auth.admin.management.current_timestamp", lambda: 200.0)
    capsys.readouterr()

    assert identitymgr.main(["user", "list", "--json"]) == 0
    [boundary_record] = json.loads(capsys.readouterr().out)
    assert boundary_record["email"] == "boundary@example.com"
    assert boundary_record["effective_active"] is False

    assert identitymgr.main(["user", "list", "--json", "--active"]) == 0
    assert json.loads(capsys.readouterr().out) == []

    assert identitymgr.main(["user", "list", "--json", "--inactive"]) == 0
    [record] = json.loads(capsys.readouterr().out)
    assert record["email"] == "boundary@example.com"
    assert record["effective_active"] is False


def test_identitymgr_list_timestamp_filters_and_ordering(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database_url = sqlite_file_url(tmp_path / "list-timestamps.sqlite3")
    initialise_identity_database(database_url)
    set_identitymgr_database_url(monkeypatch, tmp_path, database_url)
    for email in ("first@z.example", "second@y.example", "third@y.example"):
        monkeypatch.setattr(sys, "stdin", io.StringIO(f"{STRONG_TEST_PASSWORD}\n"))
        assert identitymgr.main(["user", "create", email, "--password", "-"]) == 0

    update_user_fields(
        database_url,
        "first@z.example",
        created_at=100.0,
        modified_at=150.0,
        last_login_at=200.0,
    )
    update_user_fields(
        database_url,
        "second@y.example",
        created_at=300.0,
        modified_at=350.0,
        last_login_at=400.0,
    )
    update_user_fields(
        database_url,
        "third@y.example",
        created_at=500.0,
        modified_at=550.0,
        last_login_at=600.0,
    )
    capsys.readouterr()

    assert (
        identitymgr.main(
            [
                "user",
                "list",
                "--json",
                "--since-created-at",
                "250",
                "--order",
                "email-domain",
            ]
        )
        == 0
    )
    records = json.loads(capsys.readouterr().out)
    assert [record["email"] for record in records] == [
        "second@y.example",
        "third@y.example",
    ]

    assert identitymgr.main(["user", "list", "--json", "-l", "450"]) == 0
    records = json.loads(capsys.readouterr().out)
    assert {record["email"] for record in records} == {
        "first@z.example",
        "second@y.example",
    }


def test_identitymgr_last_login_order_keeps_nulls_last(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database_url = sqlite_file_url(tmp_path / "last-login-order.sqlite3")
    initialise_identity_database(database_url)
    set_identitymgr_database_url(monkeypatch, tmp_path, database_url)
    for email in ("never@example.com", "recent@example.com"):
        monkeypatch.setattr(sys, "stdin", io.StringIO(f"{STRONG_TEST_PASSWORD}\n"))
        assert identitymgr.main(["user", "create", email, "--password", "-"]) == 0

    update_user_fields(database_url, "recent@example.com", last_login_at=100.0)
    capsys.readouterr()

    assert identitymgr.main(["user", "list", "--json", "--order", "last-login-at"]) == 0

    records = json.loads(capsys.readouterr().out)
    assert [record["email"] for record in records] == [
        "recent@example.com",
        "never@example.com",
    ]


def test_identitymgr_email_domain_order_rejects_unsupported_dialect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = sqlite_file_url(tmp_path / "email-domain-unsupported.sqlite3")
    initialise_identity_database(database_url)
    settings = AuthTestSettings(database_url=database_url)
    engine = create_database_engine(settings)
    session_factory = create_session_factory(engine)
    monkeypatch.setattr(
        identity_management,
        "EMAIL_DOMAIN_ORDER_DIALECTS",
        frozenset(),
    )

    async def assert_unsupported_order() -> None:
        async with session_scope(session_factory) as session:
            result = await identity_management.list_local_users_for_management(
                session,
                order="email-domain",
            )

        assert result.is_failure() is True
        assert result.error_type == identity_management.ERROR_UNSUPPORTED_ORDER
        assert result.message
        message = result.message.lower()
        assert "email-domain" in message
        assert "sqlite" in message

    try:
        asyncio.run(assert_unsupported_order())
    finally:
        asyncio.run(close_database(engine))


def test_identitymgr_list_filters_by_login_presence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database_url = sqlite_file_url(tmp_path / "login-presence.sqlite3")
    initialise_identity_database(database_url)
    set_identitymgr_database_url(monkeypatch, tmp_path, database_url)
    for email in ("never@example.com", "recent@example.com"):
        monkeypatch.setattr(sys, "stdin", io.StringIO(f"{STRONG_TEST_PASSWORD}\n"))
        assert identitymgr.main(["user", "create", email, "--password", "-"]) == 0

    update_user_fields(database_url, "recent@example.com", last_login_at=100.0)
    capsys.readouterr()

    assert identitymgr.main(["user", "list", "--json", "--never-logged-in"]) == 0
    [never_record] = json.loads(capsys.readouterr().out)
    assert never_record["email"] == "never@example.com"

    assert identitymgr.main(["user", "list", "--json", "--logged-in"]) == 0
    [logged_in_record] = json.loads(capsys.readouterr().out)
    assert logged_in_record["email"] == "recent@example.com"


def test_identitymgr_timestamp_parser_handles_numeric_iso_and_natural_values() -> None:
    assert authmgr_timestamps.parse_timestamp_filter("4102444800") == 4102444800.0
    assert authmgr_timestamps.parse_timestamp_filter("20250101") == 20250101.0
    assert (
        authmgr_timestamps.parse_timestamp_filter("2100-01-01T00:00:00Z")
        == 4102444800.0
    )
    assert isinstance(authmgr_timestamps.parse_timestamp_filter("1 June 2030"), float)


def test_identitymgr_timestamp_parser_rejects_invalid_values() -> None:
    with pytest.raises(ValueError, match="Invalid timestamp value"):
        authmgr_timestamps.parse_timestamp_filter("not-a-date")


def test_identitymgr_timestamp_parser_uses_day_month_year_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(authmgr_timestamps, "_local_timezone_name", lambda: "UTC")

    assert (
        authmgr_timestamps.parse_timestamp_filter("01/02/2030")
        == datetime(
            2030,
            2,
            1,
            tzinfo=UTC,
        ).timestamp()
    )


@pytest.mark.parametrize(
    ("tzinfo", "expected"),
    [
        (SimpleNamespace(key="Australia/Melbourne"), "Australia/Melbourne"),
        (SimpleNamespace(zone="Europe/London"), "Europe/London"),
        (SimpleNamespace(tzname=lambda _: "CET"), "UTC"),
        (SimpleNamespace(key="", zone="", tzname=lambda _: ""), "UTC"),
        (None, "UTC"),
    ],
)
def test_identitymgr_timezone_name_uses_available_tzinfo_name(
    tzinfo: object,
    expected: str,
) -> None:
    assert authmgr_timestamps._timezone_name_from_tzinfo(tzinfo) == expected


def test_auth_database_url_parser_handles_relative_and_absolute_sqlite_paths(
    tmp_path: Path,
) -> None:
    relative_url = parse_sqlite_database_url("sqlite+aiosqlite:///relative.db")
    absolute_url = parse_sqlite_database_url("sqlite+aiosqlite:////tmp/auth.db")

    assert relative_url is not None
    assert relative_url.path == Path("relative.db")
    assert absolute_url is not None
    assert absolute_url.path == Path("/tmp/auth.db")
    assert (
        resolve_database_url("sqlite+aiosqlite:///relative.db", tmp_path)
        == f"sqlite+aiosqlite:///{(tmp_path / 'relative.db').resolve().as_posix()}"
    )
    assert (
        resolve_database_url("sqlite+aiosqlite:////tmp/auth.db", tmp_path)
        == f"sqlite+aiosqlite:///{Path('/tmp/auth.db').resolve().as_posix()}"
    )


def test_auth_database_url_rejects_sqlite_authority_form(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="authority forms"):
        resolve_database_url("sqlite+aiosqlite://host/auth.db", tmp_path)


def test_auth_database_url_rejects_blank_url(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="blank"):
        resolve_database_url("", tmp_path)


def test_auth_database_url_rejects_unsupported_scheme(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="unsupported scheme"):
        resolve_database_url("mysql+aiomysql://localhost/auth", tmp_path)


def test_identitymgr_human_output_formats_only_known_timestamp_fields() -> None:
    assert (
        authmgr_output._format_human_value("created_at", 4102444800.0)
        == "2100-01-01T00:00:00+00:00"
    )
    assert (
        authmgr_output._format_human_value("created_at", 4102444800)
        == "2100-01-01T00:00:00+00:00"
    )
    assert authmgr_output._format_human_value("quota", 1.5) == 1.5


def test_identitymgr_timestamp_fields_are_centralised() -> None:
    assert authmgr_output.USER_RECORD_FIELDS is identity_management.USER_RECORD_FIELDS
    assert (
        authmgr_output.USER_TIMESTAMP_FIELDS
        is identity_management.USER_TIMESTAMP_FIELDS
    )
    assert authmgr_output.TIMESTAMP_FIELDS == frozenset(
        authmgr_output.USER_TIMESTAMP_FIELDS
    )
    assert set(authmgr_output.USER_TIMESTAMP_FIELDS).issubset(
        authmgr_output.USER_RECORD_FIELDS
    )


@pytest.mark.parametrize(
    ("pattern", "expected"),
    [
        ("", ""),
        ("*", "%"),
        ("foo", "foo"),
        ("foo*bar", "foo%bar"),
        (r"\*", "*"),
        (r"foo\*bar", "foo*bar"),
        ("%", r"\%"),
        ("_", r"\_"),
        (r"\%", r"\%"),
        (r"\_", r"\_"),
        (r"foo\bar", r"foo\\bar"),
        (r"foo\\*bar", r"foo\\%bar"),
        ("foo\\", r"foo\\"),
        (r"foo\*", "foo*"),
        (r"foo\%", r"foo\%"),
        (r"foo\\*", r"foo\\%"),
        (r"foo\\", r"foo\\"),
        (r"foo\_", r"foo\_"),
    ],
)
def test_identitymgr_sql_wildcard_pattern_examples(
    pattern: str,
    expected: str,
) -> None:
    assert identity_management._sql_wildcard_pattern(pattern) == expected


def test_identitymgr_human_output_handles_missing_record_fields(
    capsys: pytest.CaptureFixture[str],
) -> None:
    authmgr_output._print_user_records(
        [{"email": "partial@example.com", "id": "user-1"}],
        json_output=False,
        csv_output=False,
    )

    assert capsys.readouterr().out == (
        "partial@example.com id=user-1 admin=False "
        "superuser=False active=False verified=False\n"
    )


def test_identitymgr_csv_output_uses_iso_timestamp_strings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database_url = sqlite_file_url(tmp_path / "list-csv.sqlite3")
    initialise_identity_database(database_url)
    set_identitymgr_database_url(monkeypatch, tmp_path, database_url)
    monkeypatch.setattr(sys, "stdin", io.StringIO(f"{STRONG_TEST_PASSWORD}\n"))
    assert (
        identitymgr.main(["user", "create", "csv@example.com", "--password", "-"]) == 0
    )
    update_user_fields(database_url, "csv@example.com", created_at=4102444800.0)
    capsys.readouterr()

    assert identitymgr.main(["user", "list", "--csv"]) == 0

    [record] = csv.DictReader(io.StringIO(capsys.readouterr().out)).__iter__()
    assert record["email"] == "csv@example.com"
    assert record["created_at"] == "2100-01-01T00:00:00+00:00"


def test_identitymgr_csv_fieldnames_are_stable() -> None:
    assert authmgr_output._csv_fieldnames() == list(authmgr_output.USER_RECORD_FIELDS)
