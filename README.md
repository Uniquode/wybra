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
```

## Development Notes

Use `uv` for dependency and command execution. Runtime dependencies should be
added with `uv add`; development dependencies should be added with `uv add
--dev` or the appropriate dependency group option.

Run project validation:

```sh
uv run validate
uv run validate --verbose
uv run validate --verbose web persistence
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
uv run alembic upgrade head
```

PostgreSQL environments must provide the database, users, roles, and privileges
before application startup. Alembic handles application schema migrations only.
