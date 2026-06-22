# Wybra

[![Build Status](https://img.shields.io/github/actions/workflow/status/Uniquode/wybra/tests.yml?branch=main&label=tests&logo=github)](https://github.com/Uniquode/wybra/actions/workflows/tests.yml)
[![Security](https://img.shields.io/github/actions/workflow/status/Uniquode/wybra/codeql.yml?branch=main&label=security&logo=github)](https://github.com/Uniquode/wybra/security/code-scanning)
[![Maintenance](https://img.shields.io/badge/maintenance-active-brightgreen.svg)](https://github.com/Uniquode/wybra)
[![PyPI version](https://img.shields.io/pypi/v/wybra.svg?logo=pypi&logoColor=white)](https://pypi.org/project/wybra/)
[![PyPI downloads](https://img.shields.io/pypi/dm/wybra.svg?logo=pypi&logoColor=white)](https://pypi.org/project/wybra/)
[![Python versions](https://img.shields.io/pypi/pyversions/wybra.svg?logo=python&logoColor=white)](https://pypi.org/project/wybra/)

<p align="center">
  <img src="logo.svg" alt="wybra" width="160"/>
</p>

`wybra` is a reusable async FastAPI framework layer. It provides web
composition, database and migration helpers, project command adapters, and
reusable local authentication building blocks.

The name follows attested Bundjalung and neighbouring dialect forms including
`wybra`, `wibra`, `wybera`, and `waybara`, associated with fire, firewood, or
wood.

Repository: <https://github.com/Uniquode/wybra>

## Package Areas

- `wybra.core`: module composition, route management, package resource helpers,
  settings loading, diagnostics, and shared conventions.
- `wybra.views`: developer-facing view base classes, plain HTML view helpers,
  template view helpers, API view helpers, and paging/result helper types.
- `wybra.assets`: static asset settings, source discovery, runtime serving,
  URL resolution, collection, and validation.
- `wybra.template`: template settings, source discovery, rendering capability,
  context construction, and template validation.
- `wybra.forms`: form settings, CSRF protection, request form parsing, form
  safety helpers, form response finalisation, and forms validation.
- `wybra.security`: web-facing security policy, COOP/security headers, CORS
  policy data, middleware setup, and security validation.
- `wybra.errors`: exception handler registration, error classification,
  safe fallback responses, renderer coordination, and error validation.
- `wybra.api`: API request classification, response formatting, error payloads,
  HATEOAS-style paging metadata, streaming responses, and API validation.
- `wybra.db`: SQLAlchemy metadata conventions, async database helpers, database
  URL handling, and Alembic command/configuration support.
- `wybra.tools`: generic project command adapters and validation target
  discovery. Host applications provide concrete runtime settings through their
  app config.
- `wybra.auth`: local identity models, FastAPI Users integration, browser auth
  routes, auth templates, password policy, group/scope administration, and the
  `wybra-authmgr` operator CLI.

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
or change-management artifacts. Host-facing commands resolve the configured
application through the host app config file selected by `--config`,
`APP_CONFIG`, or the default `app.toml`.

## Application Startup

Host applications own their FastAPI instance. Wybra owns the common engine setup,
including configured route registration, behind the FastAPI lifespan hook:

```python
from fastapi import FastAPI
import wybra

app = FastAPI(
    title="example",
    lifespan=wybra.start_site(config_source="app.toml"),
)
```

Configured modules expose one async setup hook at their package root:

```python
from wybra import Site


async def setup_site(site: Site) -> None:
    ...
```

Startup calls configured module hooks in `app.toml` order. Modules use
type-keyed capabilities for shared services rather than importing another
module's implementation details. When a module depends on a capability that may
be provided later in the configured module list, keep the setup order
independent by storing a capability proxy in `setup_site(...)` and finalising it
in `post_setup_site(...)`:

```python
from wybra.db import DatabaseCapability


async def setup_site(site: Site) -> None:
    database = site.capability_proxy(DatabaseCapability)
    ...


async def post_setup_site(site: Site) -> None:
    database.finalise_required()
```

`post_setup_site(...)` is an optional async hook that runs only after every
configured module has completed `setup_site(...)`. Use it for final composition
checks: hard dependencies bind to real capabilities or fail startup, while soft
dependencies can be finalised with `finalise_optional()` and handled by the
consuming module's fallback behaviour.

Current hard dependencies include auth, media, and profile data access, auth on
forms for protected browser form routes, widgets on templates, widgets on forms
for theme form routes, and routes that explicitly require template rendering.
Soft dependencies include profile images on media, templates on assets for
`asset_url(...)`, and widgets on auth/profile enrichment.

Auth is exposed through `AuthCapability`, so applications can depend on public
helpers rather than auth internals:

```python
from fastapi import Depends
from wybra.auth import login_required


@router.get("/admin", dependencies=[Depends(login_required)])
async def admin_page():
    ...
```

App-side Wybra database, auth, route, template, static, or runtime-state setup
is not supported. Configure modules and settings once, then use
`wybra.start_site(...)` to initialise the Wybra-owned concerns.

## Project Commands

Wybra publishes prefixed console scripts to avoid collisions with host
application or environment-specific tooling:

- `wybra-runserver`: start the configured ASGI application with Uvicorn.
- `wybra-migrate`: run Alembic migrations for the configured application.
- `wybra-collect`: collect configured module static assets for deployment.
- `wybra-routes`: inspect the configured application's installed route tree.
- `wybra-validate`: run configured project validation targets.
- `wybra-authmgr`: manage local identity users, scopes, and groups.

Host applications may add their own short aliases when appropriate, but the
portable package-owned command names are the `wybra-*` commands.

`wybra-runserver` reads the configured Uvicorn app target from
`[app.runserver].asgi_app` in the selected app config file. The reload
environment variable is configured with `[app.runserver].reload_env`.

By default, Wybra uses the current project root and `app.toml` in that project
root for application startup. Runtime overrides are passed through the same
startup configuration channel used by ASGI startup:

- `--project` sets `APP_ROOT` and is the only CLI option that changes the
  effective project root.
- `--config` sets `APP_CONFIG` and selects the application config file without
  changing the project root.
- `--database-url` sets `DATABASE_URL` for database, auth, validation, and
  migration consumers.
- `--deploy` sets `APP_ENV` for deployment-policy consumers.

Precedence is CLI override, then environment variable, then app config default.
Relative config paths and relative SQLite database paths are resolved from the
effective project root.

```toml
[app.runserver]
asgi_app = "example_app.asgi:app"
reload_env = "APP_RELOAD"
```

## Static Asset Collection

`wybra.assets` owns static asset settings, source discovery, runtime serving,
URL resolution, collection, and validation. Wybra can collect the static assets
for the configured application into the filesystem tree configured by
`[app.assets].root`. Collection output is deployment/export output; it does not
become the runtime source of app-served static files:

```sh
uv run wybra-collect --config config/app.toml
```

Collection uses the same configured module order and static asset precedence as
runtime serving. Runtime app-served static handling still serves the configured
module static sources directly, so local development sees the source assets
rather than a previously collected tree. Unchanged files are skipped, copied
files preserve metadata, and Wybra-managed stale files under the asset root are
deleted by default so the collected tree matches the configured asset set.

Use `--dest` for a one-off collection destination:

```sh
uv run wybra-collect --config config/app.toml --dest build/static
```

Use `--no-delete` when stale files should be retained for a diagnostic or
staged deployment run:

```sh
uv run wybra-collect --config config/app.toml --no-delete
```

During local development, keep app-served static handling enabled so Wybra
serves the configured module static sources:

```toml
[app.assets]
url_path = "/static/"
root = "static"
export_mode = "normal"
serve = true
```

For deployments where nginx or another front end serves collected assets
directly, keep the URL path aligned and disable the ASGI static mount:

```toml
[app.assets]
url_path = "/static/"
root = "static"
export_mode = "normal"
serve = false
```

`export_mode = "normal"` is the default and performs a direct collection to the
configured asset root. Manifest collection is a separate mode and backend.

When nginx serves collected assets directly, Wybra runtime middleware cannot
apply asset CORS headers. Configure `wybra.security` and ask collection to write
an nginx CORS include for the same asset-serving policy:

```sh
uv run wybra-collect --config config/app.toml --nginx-cors deploy/asset-cors.conf
```

## Template Context

`wybra.template` composes template context as read-only mapping layers. Adding
context creates a newer layer in front of existing parents; lookup uses the
first matching key from newest to oldest, and parent mappings are not mutated.
At the render boundary Wybra flattens the layered context to a plain mapping for
the configured template engine.

Ordinary caller context can override request or provider context by occupying a
newer ordinary layer. Framework-owned render values are applied as a protected
final layer so page data cannot shadow runtime helpers. Reserved render names
are `asset_url`, `request`, `route_name`, `csrf_field_name`,
`csrf_header_name`, and `csrf_token`.

## Migration Workflow

Provision a first-time managed database and initialise Alembic state
explicitly:

```sh
uv run wybra-migrate init
uv run wybra-migrate --config config/app.toml init
```

`init` stops after infrastructure and migration-state setup. After migration
state exists, apply schema revisions with:

```sh
uv run wybra-migrate upgrade
```

For PostgreSQL, `init` provisions the database, user, role, and privileges.
Provide administrative connection details with `--admin-database-url` or the
dbscripts-compatible `SA_DATABASE_URL` environment variable.

Inspect migration state without mutating the database:

```sh
uv run wybra-migrate current
uv run wybra-migrate --config config/app.toml current
```

Create module-owned Alembic revisions through the project command:

```sh
uv run wybra-migrate revision --module wybra.auth --autogenerate -m "add identity field"
```

Revision files are placed in the selected configured module's conventional
`migrations/versions/` directory. The normal roll-forward order is to upgrade
the working database to the current head, update the owning module's models,
generate the revision, review generated operations plus `down_revision` and
`depends_on`, run `wybra-migrate upgrade`, then validate.

## Route Inspection

Inspect the installed route tree:

```sh
uv run wybra-routes
uv run wybra-routes --config config/app.toml
uv run wybra-routes --graph
uv run wybra-routes --mermaid
uv run wybra-routes --json
uv run wybra-routes --check
uv run wybra-routes --check --quiet
```

The route-tree command imports the configured ASGI app target and reports the
final installed FastAPI/Starlette route graph. Use it for route review and for
explicit route smoke checks. Use `--check --quiet` when only the exit status is
needed. It is separate from `wybra-validate`, which remains the broad
project-structure validation command.

## Auth Configuration

Wybra-hosted applications configure auth through the host application's
`app.toml`. `wybra-authmgr` resolves the same host application config as the
other package-owned project commands, then reads `[auth]` from that file. Use
`--config <path>` to select a specific app config for one invocation:

```toml
[app]
database_url = "sqlite+aiosqlite:///app.sqlite3"
modules = [
    "wybra.assets",
    "wybra.security",
    "wybra.forms",
    "wybra.errors",
    "wybra.api",
    "wybra.template",
    "wybra.auth",
]

[app.templates]
auto_reload = true
cache_size = 0

[app.assets]
url_path = "/static/"
root = "static"

[auth]
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

Database selection precedence for auth configuration is `DATABASE_URL`, then
`[app].database_url`.

```sh
uv run wybra-authmgr --config config/app.toml user list
```
