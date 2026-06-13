# Wevra

[![Build Status](https://img.shields.io/github/actions/workflow/status/Uniquode/wevra/tests.yml?branch=main&label=tests&logo=github)](https://github.com/Uniquode/wevra/actions/workflows/tests.yml)
[![Security](https://img.shields.io/github/actions/workflow/status/Uniquode/wevra/codeql.yml?branch=main&label=security&logo=github)](https://github.com/Uniquode/wevra/security/code-scanning)
[![Maintenance](https://img.shields.io/badge/maintenance-active-brightgreen.svg)](https://github.com/Uniquode/wevra)
[![PyPI version](https://img.shields.io/pypi/v/wevra.svg?logo=pypi&logoColor=white)](https://pypi.org/project/wevra/)
[![PyPI downloads](https://img.shields.io/pypi/dm/wevra.svg?logo=pypi&logoColor=white)](https://pypi.org/project/wevra/)
[![Python versions](https://img.shields.io/pypi/pyversions/wevra.svg?logo=python&logoColor=white)](https://pypi.org/project/wevra/)

<p align="center">
  <img src="logo.svg" alt="wevra" width="160"/>
</p>

`wevra` is a reusable async FastAPI framework layer. It provides web
composition, database and migration helpers, project command adapters, and
reusable local authentication building blocks.

Repository: <https://github.com/Uniquode/wevra>

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
  `wevra-authmgr` operator CLI.

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
application through the host project's `[tool.wevra]` metadata and `app.toml`.

## Application Startup

Host applications own their FastAPI instance and product routes. Wevra owns the
common engine setup behind the FastAPI lifespan hook:

```python
from fastapi import FastAPI
import wevra

app = FastAPI(
    title="example",
    lifespan=wevra.start_site(config_source="app.toml"),
)
```

Configured modules expose one async setup hook at their package root:

```python
from wevra import Site


async def setup_site(site: Site) -> None:
    ...
```

Startup calls configured module hooks in `app.toml` order. Modules use
type-keyed capabilities for shared services rather than importing another
module's implementation details:

```python
from wevra.db import DatabaseCapability


async def setup_site(site: Site) -> None:
    database = site.require_capability(DatabaseCapability)
    async with database.transaction() as session:
        ...
```

Auth is exposed through `AuthCapability`, so applications can depend on public
helpers rather than auth internals:

```python
from fastapi import Depends
from wevra.auth import login_required


@router.get("/admin", dependencies=[Depends(login_required)])
async def admin_page():
    ...
```

App-side Wevra database, auth, route, template, static, or runtime-state setup
is not supported. Configure modules and settings once, then let
`wevra.start_site(...)` initialise the Wevra-owned concerns.

## Project Commands

Wevra publishes prefixed console scripts to avoid collisions with host
application or environment-specific tooling:

- `wevra-runserver`: start the configured ASGI application with Uvicorn.
- `wevra-migrate`: run Alembic migrations for the configured application.
- `wevra-routes`: inspect the configured application's installed route tree.
- `wevra-validate`: run configured project validation targets.
- `wevra-authmgr`: manage local identity users, scopes, and groups.

Host applications may add their own short aliases when appropriate, but the
portable package-owned command names are the `wevra-*` commands.

## Migration Workflow

Provision a first-time managed database and initialise Alembic state
explicitly:

```sh
uv run wevra-migrate init
```

`init` stops after infrastructure and migration-state setup. After migration
state exists, apply schema revisions with:

```sh
uv run wevra-migrate upgrade
```

For PostgreSQL, `init` provisions the database, user, role, and privileges.
Provide administrative connection details with `--admin-database-url` or the
dbscripts-compatible `SA_DATABASE_URL` environment variable.

Inspect migration state without mutating the database:

```sh
uv run wevra-migrate current
```

Create module-owned Alembic revisions through the project command:

```sh
uv run wevra-migrate revision --module wevra.auth --autogenerate -m "add identity field"
```

Revision files are placed in the selected configured module's conventional
`migrations/versions/` directory. The normal roll-forward order is to upgrade
the working database to the current head, update the owning module's models,
generate the revision, review generated operations plus `down_revision` and
`depends_on`, run `wevra-migrate upgrade`, then validate.

## Route Inspection

Inspect the installed route tree:

```sh
uv run wevra-routes
uv run wevra-routes --graph
uv run wevra-routes --mermaid
uv run wevra-routes --json
uv run wevra-routes --check
uv run wevra-routes --check --quiet
```

The route-tree command imports the configured ASGI app target and reports the
final installed FastAPI/Starlette route graph. Use it for route review and for
explicit route smoke checks. Use `--check --quiet` when only the exit status is
needed. It is separate from `wevra-validate`, which remains the broad
project-structure validation command.

## Auth Configuration

Wevra-hosted applications configure auth through the host application's
`app.toml`. `wevra-authmgr` resolves the same host application config as the
other package-owned project commands, then reads `[auth]` from that file:

```toml
[app]
database_url = "sqlite+aiosqlite:///app.sqlite3"
modules = ["wevra.web", "wevra.auth"]

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
