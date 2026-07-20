# Events

`wybra.events` is a core, in-process observation service. It is available to
configured modules during setup without appearing in `[app].modules`, but
delivery is disabled by default.

```toml
[wybra.events]
enabled = true
```

Set `WYBRA_EVENT_DELIVERY_ENABLED=true` for an environment override. When
disabled, subscriptions and publications are harmless no-ops. Producers never
check diagnostics settings; hot producer boundaries may skip constructing an
observation when delivery is disabled. Event publication or handler failures
never change the operation being observed.

## Subscribing

Subscribe an asynchronous handler to a typed scope from `wybra.events`.
A selector includes all child topics: `EVT_SQL` selects `sql.statement.*` and
`sql.transaction.*`. Selectors use dot-separated names; wildcard syntax is not
supported.

```python
from wybra.events import EVT_CACHE, Event, EventsCapability


async def observe(event: Event) -> None:
    print(event.scope)


events: EventsCapability = ...
events.subscribe(EVT_CACHE, observe)
```

Handlers execute sequentially in the application event loop. A failing handler
is logged and recorded under `events.errors`; it cannot veto or alter the
operation that emitted its event.

## Public producer scopes

- `sql`: database connections, statements, transactions, and savepoints.
- `request`, `route`, `view`: HTTP lifecycle, dispatch, and generic-view
  outcomes.
- `template`, `cache`, `form`: rendering, cache, validation, and persistence.
- `account`, `credential`: account lifecycle separately from authentication
  and credential changes.
- `session`, `security`: session lifecycle plus policy and denial outcomes.
- `site`: module hooks, capability registration/resolution, startup, and
  shutdown.
- `events` and `events.errors`: event delivery diagnostics and handler
  failures.

Within an HTTP operation, observations begin with `request.started`, then any
lower-level database, cache, template, form, route, or view observations at
their existing boundaries, followed by `request.completed`. Site/module events
occur during composition and shutdown, outside an HTTP request. This ordering
is observational only; it does not impose a new execution model.

Event payloads are typed, immutable, and deliberately allowlisted. They never
include request bodies, uploaded content, field values, rendered output,
headers, cookies, session identifiers or payloads, credentials, tokens,
challenge identifiers, provider identities, SQL statement or parameter values, or raw framework
objects. User UUIDs may appear as opaque internal traceability identifiers;
email values are masked.

Capability-registration events are emitted at site composition boundaries,
including after the optional diagnostics capability has been registered. A
runtime capability registration is not itself an asynchronous dispatch point;
applications that need to observe it should register through their own
explicit asynchronous composition boundary.

## Diagnostics is optional

Events and diagnostics are independent. Enabling `[wybra.events]` delivers to
application subscribers even when diagnostics is absent or disabled.
`[wybra.diagnostics].events_enabled` installs a passive subscriber that selects
and retains configured event scopes for `/__debug/ws`; it cannot enable event
delivery, change producer behaviour, or alter another subscriber.

Database events are operation observations from Wybra's isolated Tortoise
adapter. They do not replace or translate `wybra.db.signals`, which remains a
direct namespace for native Tortoise lifecycle hooks and their controlling
pre-save semantics.
