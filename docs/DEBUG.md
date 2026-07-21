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
the primary diagnostics collectors.

Enable diagnostics explicitly:

```toml
[wybra.diagnostics]
events_enabled = true
level = "debug"
logging_bridge = false
slow_sql_threshold_seconds = 0.5
```

Wybra initialises diagnostics from `[wybra.diagnostics]` during site startup.
`events_enabled` registers request diagnostics middleware and enables
collection. `event_scopes` selects the dot-notation namespaces to retain; its
default is `sql`, `template`, and `events.errors`. A parent selector includes
all of its child topics, so `sql` includes `sql.statement`. Wildcard selectors
such as `.*` are not supported.

Diagnostics levels are:

- `info`: request summaries and notable events
- `debug`: backend operations and additional diagnostic events
- `trace`: high-volume per-operation details

Diagnostics can collect:

- request method, route name, status, exception state, and duration
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
events_enabled = true
level = "trace"
logging_bridge = true

[log.loggers."wybra.diagnostics"]
level = "TRACE"
handlers = ["console"]
propagate = false
```

The bridge emits through the dedicated `wybra.diagnostics` logger. Normal
operational logging remains configured separately through `[log]`.

## Debug WebSocket

The interactive control plane is separately disabled by default. Enable it
only for a local or intentionally controlled development endpoint:

```toml
[wybra.diagnostics]
events_enabled = true
debug_enabled = true
debug_allowed_hosts = ["localhost", "127.0.0.1", "::1"]
level = "trace"
```

It registers `/__debug/ws` and accepts JSON-RPC 2.0 requests. A connection has
no subscriptions initially. Use `diagnostics.scopes` to discover available
scopes, `diagnostics.snapshot` to retrieve retained data, and
`diagnostics.subscribe` / `diagnostics.unsubscribe` to control only that
connection's notifications. The protocol cannot enable collection, broaden the
server's collector filter, or otherwise modify server state.

`debug_allowed_hosts` authorises the effective client peer, not the untrusted
`Host` request header. The default entries resolve only to loopback addresses.
For remote or container development, add the intended client address explicitly.
When a trusted reverse proxy such as Nginx fronts Wybra, configure Uvicorn with
its proxy address in `--forwarded-allow-ips` (and `--proxy-headers`); Uvicorn
then safely normalises the ASGI peer from proxy-inserted forwarded headers.
Wybra never reads forwarded headers itself, so a direct client cannot spoof its
peer identity.

Browser connections must also have an `Origin` matching the requested WebSocket
host. Command-line clients may omit `Origin`. This prevents another local web
page from reading diagnostics through the developer's browser.

The endpoint provides process-local, bounded history; it is not an authenticated
production monitoring service. It never exposes request bodies, cookies,
credentials, sessions, CSRF values, rendered template content, SQL parameter
values, or concrete request paths. Content-type mappings are snapshot-only;
they cannot be subscribed to. Bounded live subscription queues report a
`dropped: true` marker on the next delivered notification after overflow.

### Debug CLI

`wybra-debug` is a small client for this already-enabled endpoint. It neither
enables diagnostics nor changes the server's collection filters or any other
client's subscription. Its required positional argument is the complete
WebSocket URL, including the exposed path:

```sh
wybra-debug ws://127.0.0.1:8000/__debug/ws --list-scopes
wybra-debug ws://127.0.0.1:8000/__debug/ws --scope sql --scope request
wybra-debug wss://debug.example.test/__debug/ws --scope events.errors
```

`--list-scopes` invokes `diagnostics.scopes`, writes its original JSON-RPC
response message to standard output, and exits. Streaming requires at least
one repeatable `--scope`; the command sends one `diagnostics.subscribe` request
for all selected scopes and then writes only received
`diagnostics.notification` JSON-RPC messages to standard output. Each message
is emitted unchanged on one line, making it suitable for redirection or a
downstream JSON processor.

Connection, access-control, malformed-message, and JSON-RPC errors are written
to standard error with a non-zero status. Use `Ctrl-C` to close a live stream
cleanly. The command supplies no authentication or access-control bypass: the
target application's `debug_enabled` and `debug_allowed_hosts` configuration
remain authoritative, including the proxy guidance above.

## TRACE Logging

Wybra registers a `TRACE` logging level at numeric level `5`. It is available
for explicit logging configuration and diagnostics bridge output. Enabling
`app.debug` does not lower the root logger or Wybra loggers to `TRACE`.
