# Database Configuration

Wybra supports two database configuration styles:

- Structured `[app.database]` configuration for production and managed
  deployments.
- `database_url` / `DATABASE_URL` for local development, simple SQLite setups,
  and explicit command-line overrides.

For production, prefer `[app.database]` so credentials can come from a secret
source instead of being stored inside a URL.

## Precedence

Wybra resolves the effective database connection in this order:

1. A command-line/startup database URL override.
2. `DATABASE_URL`.
3. `[app.database]`.
4. `[app].database_url`.

If `[app.database]` and `[app].database_url` are both configured,
`[app.database]` is used. Wybra logs an info-level message telling you that
`[app].database_url` is overridden and should be removed.

`AUTH_DATABASE_URL` is not supported. Authentication uses the same application
database configuration as the rest of the app.

## Local SQLite

For local development, a URL is still acceptable:

```toml
[app]
database_url = "sqlite:///local.sqlite3"
```

Relative SQLite file paths are resolved from the effective project root, not
from the directory containing the configuration file and not from the process
working directory. This remains true when the active config file is supplied
from another location with `--config`, `APP_CONFIG`, or equivalent startup
configuration.

The structured equivalent is:

```toml
[app.database]
backend = "sqlite"
database = "local.sqlite3"
```

When constructing settings directly from a database URL, use an absolute
SQLite URL. Relative SQLite URLs require the application project root and are
only accepted through the application configuration path.

## PostgreSQL With Secret-Backed Credentials

Use structured config for PostgreSQL so the username and password can be read
from a configured secret source:

```toml
[app.database]
backend = "postgresql"
host = "db.internal.example"
port = 5432
database = "uniquode"
credential_source = "keychain"
user_key = "database/uniquode/app/user"
password_key = "database/uniquode/app/password"
```

`credential_source` can use the same sources as the Wybra secrets subsystem,
including `environment`, `keychain`, `kms`, and `vault` when those sources are
available and configured.

## Environment Credential Source

When `credential_source = "environment"`, the credential keys are environment
variable names:

```toml
[app.database]
backend = "postgresql"
host = "db.internal.example"
database = "uniquode"
credential_source = "environment"
user_key = "UNIQUODE_DB_USER"
password_key = "UNIQUODE_DB_PASSWORD"
```

This is explicit credential lookup, not general TOML interpolation.

## PostgreSQL Unix Sockets

For PostgreSQL deployments that use a Unix socket, set `host` to the socket
directory:

```toml
[app.database]
backend = "postgresql"
host = "/var/run/postgresql"
database = "uniquode"
credential_source = "keychain"
user_key = "database/uniquode/app/user"
password_key = "database/uniquode/app/password"
```

Wybra passes the socket path to the database backend without converting it into
a TCP host name.

## Backend Dependencies

SQLite support is available by default. Other database backends require the
matching Wybra optional dependency, such as `wybra[postgresql]`,
`wybra[psycopg]`, `wybra[mysql]`, `wybra[mssql]`, or `wybra[oracle]`.
