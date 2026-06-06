# Wevra

`wevra` is a reusable async FastAPI framework layer. It provides web
composition, database and migration helpers, project command adapters, and
reusable local authentication building blocks.

Repository: <https://github.com/Uniquode/wevra>

The package is currently developed beside its host application:

```text
wevra-workspace/
  app/
  wevra/
```

Host applications can depend on this project through an editable `../wevra`
path source while developing framework and application code together.

## Package Areas

- `wevra.core`: module composition, package resource helpers, settings loading,
  diagnostics, and shared conventions.
- `wevra.web`: route composition, template rendering, static assets, CSRF,
  theme defaults, error handling, views, and web validation.
- `wevra.db`: SQLAlchemy metadata conventions, async database helpers, database
  URL handling, and Alembic command/configuration support.
- `wevra.tools`: generic project command adapters and validation target
  discovery. Host applications provide concrete settings loaders through their
  own `[tool.wevra]` metadata.
- `wevra.auth`: local identity models, FastAPI Users integration, browser auth
  routes, auth templates, password policy, group/scope administration, and the
  `identitymgr` operator CLI.

## Local Development

Use `uv` for dependency management and command execution:

```sh
uv sync
uv run pytest
uv run ruff format --check src tests
uv run ruff check src tests
uv run ty check src/
uv build
```

The framework project does not contain host application settings, `app.toml`,
or change-management artifacts. Host-facing commands such as migration,
validation, and runserver adapters are configured by the application that
exposes them.

## Auth Configuration

The standalone `identitymgr` CLI loads generic auth configuration from
`--config`, `AUTH_CONFIG`, or `./auth.toml` when present. The file uses an
`[auth]` table:

```toml
[auth]
database_url = "sqlite+aiosqlite:///app.sqlite3"
session_cookie_force_secure = false

[auth.password.policy]
minimum_length = 12
minimum_character_categories = 2
minimum_strength = 0.45
common_fragments = [
  "admin",
  "changeme",
  "changeit",
  "letmein",
  "p4ssw0rd",
  "pass",
  "password",
  "qwerty",
  "test",
  "tester",
  "welcome",
]
```

Database selection precedence for auth configuration is `AUTH_DATABASE_URL`,
then `DATABASE_URL`, then `[auth].database_url`.
