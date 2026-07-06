# Wybra Debugging

Wybra debug mode is for local development and diagnosis. It controls
FastAPI/Starlette debug responses through Wybra configuration, and it can be
paired with Wybra diagnostics for structured request, SQL, template, and
backend-operation visibility.

Debug mode must not be enabled in staging or production.

## FastAPI Debug Responses

Enable debug responses in local configuration:

```toml
[app]
deployment_environment = "local"
debug = true
```

You can also use the environment variable:

```sh
APP_DEBUG=true
```

Or the local runserver override:

```sh
uv run wybra-runserver --debug
```

To force debug off for a runserver invocation:

```sh
uv run wybra-runserver --no-debug
```

Precedence is:

1. `wybra-runserver --debug` or `--no-debug`
2. `APP_DEBUG`
3. `[app] debug`
4. default `false`

When debug is enabled and an unhandled exception occurs before the response has
started, Starlette's traceback response is returned instead of the normal Wybra
500 page. When debug is disabled, Wybra's configured error handling remains the
default.

If a response has already started, neither Starlette debug responses nor Wybra
error pages can replace that response.

## Diagnostics

Diagnostics are separate from ordinary operational logging. They collect
structured event records and summary statistics. File and console logs are not
the primary diagnostics collector.

Enable diagnostics explicitly:

```toml
[wybra.diagnostics]
enabled = true
level = "debug"
logging_bridge = false
slow_sql_threshold_seconds = 0.5
```

Wybra initialises diagnostics from `[wybra.diagnostics]` during site startup.
That single startup path registers request diagnostics middleware and enables
the built-in SQL, template, session, and message instrumentation. There are no
separate environment variables or per-backend switches for those built-in
collectors.

Diagnostics levels are:

- `info`: request summaries and notable events
- `debug`: backend operations and additional diagnostic events
- `trace`: high-volume per-operation details

Diagnostics can collect:

- request method, path, route name, status, exception state, and duration
- SQL query count and total SQL duration
- slow SQL metadata without SQL parameter values
- template names and render durations without rendered content
- selected Wybra backend operation names, result state, and duration

Diagnostics do not collect request bodies, cookies, authorisation headers,
session payloads, CSRF tokens, SQL parameter values, rendered template content,
or secret values.

## Logging Bridge

To mirror diagnostics into Python logging, enable the bridge:

```toml
[wybra.diagnostics]
enabled = true
level = "trace"
logging_bridge = true

[log.loggers."wybra.diagnostics"]
level = "TRACE"
handlers = ["console"]
propagate = false
```

The bridge emits through the dedicated `wybra.diagnostics` logger. Normal
operational logging remains configured separately through `[log]`.

## TRACE Logging

Wybra registers a `TRACE` logging level at numeric level `5`. It is available
for explicit logging configuration and diagnostics bridge output. Enabling
`app.debug` does not lower the root logger or Wybra loggers to `TRACE`.
