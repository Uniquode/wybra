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

- `wybra.core`: module composition, package resource helpers, settings loading,
  diagnostics, and shared conventions.
- `wybra.web`: route composition, template rendering, static assets, CSRF,
  theme defaults, error handling, views, and web validation.
- `wybra.db`: SQLAlchemy metadata conventions, async database helpers, database
  URL handling, and Alembic command/configuration support.
- `wybra.tools`: generic project command adapters and validation target
  discovery. Host applications provide concrete settings loaders through their
  own `[tool.wybra]` metadata.
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
application through the host project's `[tool.wybra]` metadata and `app.toml`.

## Application Startup

Host applications own their FastAPI instance and application routes. Wybra owns the
common engine setup behind the FastAPI lifespan hook:

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
module's implementation details:

```python
from wybra.db import DatabaseCapability


async def setup_site(site: Site) -> None:
    database = site.require_capability(DatabaseCapability)
    async with database.transaction() as session:
        ...
```

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
- `wybra-routes`: inspect the configured application's installed route tree.
- `wybra-validate`: run configured project validation targets.
- `wybra-authmgr`: manage local identity users, scopes, and groups.

Host applications may add their own short aliases when appropriate, but the
portable package-owned command names are the `wybra-*` commands.

`wybra-runserver` reads the configured Uvicorn app target from
`[tool.wybra].runserver_app`. By default, Wybra uses the current project root
and `app.toml` in that project root for application startup. Runtime overrides
are passed through the same startup configuration channel used by ASGI startup:

- `--project` sets `APP_ROOT` and is the only CLI option that changes the
  effective project root.
- `--config` sets `APP_CONFIG` and selects the application config file without
  changing the project root.
- `--database-url` sets `DATABASE_URL` for database, auth, validation, and
  migration consumers.
- `--deploy` sets `APP_ENV` for deployment-policy consumers.

Precedence is CLI override, then environment variable, then default. Relative
config paths and relative SQLite database paths are resolved from the effective
project root.

## Migration Workflow

Provision a first-time managed database and initialise Alembic state
explicitly:

```sh
uv run wybra-migrate init
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
other package-owned project commands, then reads `[auth]` from that file:

```toml
[app]
database_url = "sqlite+aiosqlite:///app.sqlite3"
modules = ["wybra.web", "wybra.auth"]

[app.templates]
auto_reload = true
cache_size = 0

[app.static]
url_path = "/static/"
export_root = "static"

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
