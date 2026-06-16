# Wybra DB Migration Environment

Generic Alembic environment files for the project migration command live here.
Host applications inject their settings through the command adapter and Alembic
configuration options; this package does not import the host application
settings module.

Revision files do not live in this directory. Modules that own SQLAlchemy models
bundle their own revision history alongside those models, under a conventional
`migrations/versions/` directory. The migration command discovers version
locations from configured modules and composes them into one database-wide
Alembic migration graph.

## Local Development

Host projects supply the concrete database URL and configured module list.
Provision a first-time host database and initialise Alembic migration state
with:

```sh
uv run wybra-migrate init
```

`init` does not apply application schema revisions. Update an already
initialised host schema with:

```sh
uv run wybra-migrate upgrade
```

Inspect migration state without mutating the database:

```sh
uv run wybra-migrate current
```

Create module-owned revisions through the project command:

```sh
uv run wybra-migrate revision --module <module> --autogenerate -m "change summary"
```

Revision files are placed in the selected configured module's conventional
`migrations/versions/` directory. The normal roll-forward order is to upgrade
the working database to the current head, update the owning module's models,
generate the revision, review generated operations plus `down_revision` and
`depends_on`, run `wybra-migrate upgrade`, then validate.

Use explicit in-memory SQLite only for tests or deliberately ephemeral runs:

```text
sqlite+aiosqlite:///:memory:
```

## PostgreSQL

PostgreSQL database, user, role, and privilege provisioning happens during
explicit `wybra-migrate init`, not during application startup or routine
`wybra-migrate upgrade`. Provide an administrative database URL with
`--admin-database-url` or through the dbscripts-compatible `SA_DATABASE_URL`
environment variable.

Configured model modules own table/index/constraint migrations through Alembic.
The application does not create or destroy PostgreSQL databases, users, roles,
or privileges during ordinary startup.
