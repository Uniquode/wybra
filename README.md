# uniquode.io

`uniquode` is the FastAPI-based web application for `uniquode.io`.

The application is currently an early server-rendered FastAPI site with local
identity support.

## Current Foundations

- FastAPI/Starlette ASGI application with `uniquode.asgi:app` as the stable app
  import path.
- Jinja2 server-rendered pages with `htmx` used only for progressive
  enhancement.
- Shared static assets and templates under `src/static/` and `src/templates/`.
- SQLAlchemy async persistence with Alembic migrations.
- Local account support using FastAPI Users, including password sign-in,
  database-backed browser sessions, password reset hooks, and email verification
  hooks.
- Account pages for sign in, sign out, account status, password reset, and email
  verification.

## Local Commands

Run the development server:

```sh
uv run runserver
uv run runserver --host 127.0.0.1 --port 8000
uv run runserver --reload
APP_RELOAD=1 uv run runserver
```

## Configuration

Runtime configuration is loaded through `envex`, including local `.env` files.
`DATABASE_URL` is the database connection string. App settings use concise names
such as `APP_ENV`, `APP_NAME`, `CSRF_SECRET`, `CSRF_SECURE`, `RESET_SECRET`,
`VERIFICATION_SECRET`, `SESSION_COOKIE`, `SESSION_SECURE`, `SESSION_LIFETIME`,
`OAUTH_LINKING`, `ADVANCED_AUTH`, and `APP_RELOAD`.

Local `.env` files are for development only and are ignored by Git. Deployment
environments should inject secrets through their secret manager or environment
configuration.

## Development Notes

Use `uv` for dependency and command execution. Runtime dependencies should be
added with `uv add`; development dependencies should be added with `uv add
--dev` or the appropriate dependency group option.

Run project validation:

```sh
uv run validate
uv run validate --verbose
uv run validate --verbose environment web persistence
```

Verbose validation lists the concrete checks performed for each target. Database
URLs printed by validation are redacted when credentials are embedded, for
example `postgresql+asyncpg://***:***@host.example/app`.

Run the main checks:

```sh
uv run ruff format --check
uv run ruff check
uv run ty check src/
uv run pytest
```

Initialise or update the local SQLite development database:

```sh
uv run migrate upgrade
```

Use `--database-url` to target an explicit database for one migration command:

```sh
uv run migrate --database-url sqlite+aiosqlite:///scratch.sqlite3 upgrade
```

PostgreSQL environments must provide the database, users, roles, and privileges
before application startup. Alembic handles application schema migrations only.

Manage local identity users with the operator CLI:

```sh
uv run usermgr create person@example.com
uv run usermgr create admin@example.com --admin
uv run usermgr list
uv run usermgr list --json
uv run usermgr password person@example.com
uv run usermgr delete person@example.com --force
```

`usermgr` timestamp arguments accept Unix seconds directly, such as
`--expires-at 4102444800`, or supported date/time strings parsed by
`dateparser`. Numeric input is interpreted first as Unix seconds, so use a
separated form such as `2025-01-01` for calendar dates.

`usermgr` is owned by the reusable authentication package and loads generic
auth configuration from `--config`, `AUTH_CONFIG`, or `./auth.toml` when
present. The file uses an `[auth]` table and may be shared by the host
application:

```toml
[auth]
database_url = "sqlite+aiosqlite:///uniquode.sqlite3"
session_cookie_secure = false

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

`AUTH_DATABASE_URL` can override the configured database for automation.
`usermgr` talks to the configured identity database directly. It is not an
API-backed remote administration client; that mode is deferred until
administrative API tokens and scopes exist. Passwords are entered through hidden
prompts by default, or read from stdin with `--password -` for operator
automation. Password changes revoke existing sessions unless `--no-revoke` is
supplied. Password writes use the configured `auth_ext` password policy, which
provides server-side validation and strength feedback for future UI use. The
committed [auth.toml.example](auth.toml.example) shows the supported generic
auth configuration shape.

The CLI distinguishes application admins from superusers. `--admin` marks an
account for elevated application administration, while `--superuser` is the
absolute FastAPI Users privilege flag. Superusers cannot be deleted or
deactivated, and the final superuser cannot be demoted. A user's preferred
timezone is stored only when explicitly supplied; otherwise presentation falls
back to the current server/application timezone at runtime.
