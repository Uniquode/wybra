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
```

`credential_source` can use the same sources as the Wybra secrets subsystem,
including `environment`, `keychain`, `kms`, and `vault` when those sources are
available and configured.

For non-environment secret sources, Wybra derives default database credential
keys from the configured database name:

| Credential | Default key |
| --- | --- |
| Runtime username | `database/<database>/app/user` |
| Runtime password | `database/<database>/app/password` |
| Service-account username | `database/<database>/service-account/user` |
| Service-account password | `database/<database>/service-account/password` |

For the example above, the runtime keys are
`database/uniquode/app/user` and `database/uniquode/app/password`.
Configure `user_key`, `password_key`, `sa_user_key`, or `sa_password_key` only
when a deployment needs to override those defaults.

Default key derivation accepts Unicode database names, but the database name
must be safe as one key-path segment. If the configured database name contains
path separators, whitespace, or control characters, configure explicit
credential keys instead.

Service-account credentials are used for database lifecycle work such as
`wybra-migrate init`, `wybra-migrate destroy`, and schema-changing migration
commands. Runtime credentials are for application access and should not need
broad DDL permissions.

PostgreSQL lifecycle work first connects to a service-account database because
the target application database may not exist yet. Wybra defaults that
maintenance database to `postgres`. Override it only when the service account
cannot connect to that database. The service-account database must be different
from the target application database:

```toml
[app.database]
backend = "postgresql"
database = "uniquode"
sa_database = "postgres"
credential_source = "keychain"
```

PostgreSQL migrations are executed through the service-account connection so
created schema objects remain owned by the service account. Provisioning and
maintenance then apply grants and default privileges for the runtime
application role. Use an application-dedicated runtime role; Wybra will not
drop a runtime role when it is also the service-account role or when it has
dependencies outside the configured target database.

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
```

Wybra passes the socket path to the database backend without converting it into
a TCP host name.

## Backend Dependencies

SQLite support is available by default. Other database backends require the
matching Wybra optional dependency, such as `wybra[postgresql]`,
`wybra[psycopg]`, `wybra[mysql]`, `wybra[mariadb]`, `wybra[mssql]`, or
`wybra[oracle]`.
