# uniquode.io

`uniquode` is the FastAPI-based web application for `uniquode.io`.

The application is currently an early server-rendered FastAPI site with local
identity support.

## Current Foundations

- FastAPI/Starlette ASGI application with `uniquode.asgi:app` as the stable app
  import path.
- Jinja2 server-rendered pages with `htmx` used only for progressive
  enhancement.
- Package-owned static assets and templates under configured modules, including
  reusable web foundation defaults in `src/wevra/web/templates/` and
  `src/wevra/web/static/`, application-owned public page templates in
  `src/uniquode/templates/`, and identity defaults in
  `src/wevra/auth/templates/`.
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
uv run runserver --no-reload
APP_RELOAD=1 uv run runserver
```

Additional Uvicorn arguments can be passed after `--`, for example to trust
forwarded headers from a local TLS-terminating proxy:

```sh
uv run runserver --host 127.0.0.1 --port 8000 -- --proxy-headers --forwarded-allow-ips 127.0.0.1
```

See [WEB-SECURITY.md](WEB-SECURITY.md) for reverse-proxy HTTPS setup and secure
session-cookie guidance.

## Configuration

Runtime configuration is loaded through `envex`, including local `.env` files.
`DATABASE_URL` is the database connection string. App settings use concise names
such as `APP_ENV`, `APP_NAME`, `CSRF_SECRET`, `CSRF_SECURE`, `RESET_SECRET`,
`VERIFICATION_SECRET`, `SESSION_COOKIE`, `SESSION_FORCE_SECURE`,
`SESSION_LIFETIME`, `OAUTH_LINKING`, `ADVANCED_AUTH`, and `APP_RELOAD`.
`wevra.core` owns the reusable envex/app.toml settings-loading mechanics, while
`uniquode.settings` owns this application's concrete settings fields, defaults,
deployment policy, CSRF policy, and identity policy adapter.

Application composition is loaded from [app.toml](app.toml) in the project root,
or from the path named by `APP_CONFIG`. This file is the shared source for
configured modules and web resource defaults used by runtime startup, Alembic,
validation, and future project tooling. `wevra.db` discovers model metadata
from `<module>.models` and Alembic version locations from
`<module>/migrations/versions/` when those surfaces exist; it also owns the
reusable database URL parsing and async SQLAlchemy engine/session helpers. The
project `migrate` entry point is a `wevra.tools.migrate` adapter that loads the
configured host settings adapter from `[tool.wevra]` and passes those settings
into the generic `wevra.db` migration command factory.
Page, partial, and API routes are discovered and registered through `wevra.web`
from `<module>.routes` through a `module_routes` export, and template context
providers are registered from `<module>.context` with `add_to_context`.
Validation targets are discovered from
`<module>.validation` through a `validation_targets` mapping. Runtime template
and static serving resolve configured module package sources directly, so an
earlier configured module can override a later module by providing the same
logical template or static path. Static defaults from `wevra.web` are available
only when `wevra.web` is configured, unless an explicit filesystem `STATIC_ROOT`
is supplied. Static collection is only needed when exporting assets for an
external static server such as Nginx, and the reusable static export boundary
writes the composed logical static namespace to `[static].export_root`.

```toml
modules = [
  "uniquode",
  "wevra.web",
  "wevra.auth",
]

[routes]
"wevra.auth" = "/"

[templates]
auto_reload = true
cache_size = 0

[static]
url_path = "/static/"
export_root = "static"
```

`app.toml` is not a secrets or deployment-policy file. Keep secrets in the
environment or deployment secret manager. The default auth configuration path
remains `auth.toml`; `app.toml` may reserve compatible auth directives for a
future unification change, but this application does not currently load auth
settings from it.

The current identity browser surface is published by `wevra.auth.routes`, default
identity templates live under `src/wevra/auth/templates/identity/`, and safe
identity template state is provided by `wevra.auth.context`. Identity model
metadata and migration revisions are bundled with `wevra.auth` alongside those
models. Reusable layout, theme, error, form, and stylesheet defaults are
published by `wevra.web`; host applications can omit `wevra.web` or override its
logical template/static paths from earlier configured modules.
Application-specific navigation and product policy remain application-owned.

Local `.env` files are for development only and are ignored by Git. Deployment
environments should inject secrets through their secret manager or environment
configuration.

Browser session cookies derive their `Secure` attribute from the request scheme:
plain HTTP responses use non-secure cookies, and HTTPS responses use secure
cookies. Prefer trusted proxy-header normalisation for TLS termination; set
`SESSION_FORCE_SECURE=1` for non-local deployments and any deployment where
browser traffic is HTTPS but the ASGI request scheme cannot be made reliable.
See [WEB-SECURITY.md](WEB-SECURITY.md) for Nginx and Apache examples. Non-local
deployments must explicitly configure identity token secrets and force secure
session cookies.

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

Project command wrappers such as `runserver` and `validate` live in
`wevra.tools`. The current application remains the configured command target
where appropriate, for example `runserver` starts `uniquode.asgi:app` through
the `[tool.wevra]` adapter metadata.

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
uv run identitymgr user create person@example.com
uv run identitymgr user create admin@example.com --admin
uv run identitymgr user create reader@example.com --group readers
uv run identitymgr user update reader@example.com --add-group editors
uv run identitymgr user update reader@example.com --rm-group readers
uv run identitymgr user update reader@example.com --set-group operators
uv run identitymgr user list
uv run identitymgr user list --json
uv run identitymgr user password person@example.com
uv run identitymgr user delete person@example.com --force
```

Manage local authorisation scopes and groups with the same CLI:

```sh
uv run identitymgr scope create document:read --description "Read documents"
uv run identitymgr scope update document:read --description "Read published documents"
uv run identitymgr scope list --json
uv run identitymgr scope delete document:read

uv run identitymgr group create readers --description "Readers" --scope document:read
uv run identitymgr group readers update --scope document:write --rm-scope document:read
uv run identitymgr group readers add-user person@example.com
uv run identitymgr group readers add-group staff
uv run identitymgr group readers show --json
uv run identitymgr group effective-scopes person@example.com --json
uv run identitymgr group readers remove-user person@example.com
uv run identitymgr group readers remove-group staff
uv run identitymgr group readers delete --force
```

`identitymgr` timestamp arguments accept Unix seconds directly, such as
`--expires-at 4102444800`, or supported date/time strings parsed by
`dateparser`. Numeric input is interpreted first as Unix seconds, so use a
separated form such as `2025-01-01` for calendar dates.

`identitymgr` is owned by the reusable authentication package and loads generic
auth configuration from `--config`, `AUTH_CONFIG`, or `./auth.toml` when
present. The file uses an `[auth]` table. The `uniquode` web application still
loads its runtime settings from envex environment configuration; a host may
choose to share `auth.toml`, but only if it explicitly loads that source.

```toml
[auth]
database_url = "sqlite+aiosqlite:///uniquode.sqlite3"
# Local default. Non-local deployments should force secure cookies.
# Prefer trusted proxy-header normalisation for TLS termination as well.
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

Database selection precedence for auth configuration is
`AUTH_DATABASE_URL`, then generic `DATABASE_URL`, then `[auth].database_url`.
This lets reusable `wevra.auth` tooling share a host application's database
environment without requiring a host-specific wrapper, while still allowing an
auth-specific override for automation.

`identitymgr` talks to the configured identity database directly. It is not an
API-backed remote administration client; that mode is deferred until
administrative API tokens and scopes exist. Passwords are entered through hidden
prompts by default, or read from stdin with `--password -` for operator
automation. Password changes revoke existing sessions unless `--no-revoke` is
supplied. Groups are the local authorisation mechanism: scopes are assigned to
groups, users are assigned to groups, and effective scopes are resolved through
direct and nested group membership. Scope deletion is refused while any group
uses that scope, and group deletion is refused while users, child groups, or
parent groups still reference that group. Password writes use the configured
`wevra.auth` password policy, which
provides server-side validation and strength feedback for future UI use. The
committed [auth.toml.example](auth.toml.example) shows the supported generic
auth configuration shape.

The CLI distinguishes application admins from superusers. `--admin` marks an
account for elevated application administration, while `--superuser` is the
absolute FastAPI Users privilege flag. Superusers cannot be deleted or
deactivated, and the final superuser cannot be demoted. A user's preferred
timezone is stored only when explicitly supplied; otherwise presentation falls
back to the current server/application timezone at runtime.
