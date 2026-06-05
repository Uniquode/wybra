# Data Core Migration Environment

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

The default development database is the project-root SQLite file
`uniquode.sqlite3`, supplied by this repository's Alembic configuration. It is
ignored by Git.

Initialise or update the local schema with:

```sh
uv run migrate upgrade
```

Use direct Alembic commands only when you need Alembic-specific flags that the
project migration command does not expose.

Use explicit in-memory SQLite only for tests or deliberately ephemeral runs:

```text
sqlite+aiosqlite:///:memory:
```

## PostgreSQL

PostgreSQL database, user, role, and privilege provisioning happens outside
application startup. Staging and production environments must provide an
already-created database and login role with the required privileges before the
application or migrations connect.

Configured model modules own table/index/constraint migrations through Alembic.
The application does not create or destroy PostgreSQL databases, users, roles,
or privileges during ordinary startup.
