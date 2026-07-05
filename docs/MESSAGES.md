# Wybra Messages

`wybra.messages` provides queued user-facing alerts for outcomes that need to
survive a redirect and display on the next rendered page.

## Enable The Module

Add `wybra.messages` to the configured modules list. For the default widget
layout, keep `wybra.template`, `wybra.assets`, and `wybra.widgets` configured
as usual.

```toml
[app]
modules = [
  "wybra.assets",
  "wybra.template",
  "wybra.widgets",
  "wybra.messages",
]
```

## Storage Backends

The default storage backend is `session`.

```toml
[wybra.messages]
storage_backend = "session"
```

Session storage uses the current request's `request.session` mapping, which is
provided by Wybra's core session middleware. If alert code runs without a
request session, Wybra raises a clear messages error instead of silently losing
alerts.

Cache storage stores alerts in a configured cache queue. `memory://` is useful
for tests and local experiments; Redis URLs are supported when the optional
Redis package is installed.

```toml
[wybra.messages]
storage_backend = "cache"
cache_url = "redis://localhost:6379/0"
cache_key_prefix = "wybra:messages:"
```

Database storage stores alerts in Wybra-managed persistence. Configure
`wybra.db` and run migrations before using it.

```toml
[wybra.messages]
storage_backend = "database"
database_connection_name = "default"
```

## Queue Settings

```toml
[wybra.messages]
queue_depth = 20
message_max_length = 1000
message_ttl_seconds = 86400
```

`queue_depth` limits stored alerts per request queue. When the queue exceeds
that depth, the oldest alerts are discarded. `message_ttl_seconds` controls how
long cache and database alerts remain eligible for display.

## Severities

Supported alert severities are:

- `success`
- `warning`
- `error`

Messages are stored and rendered as plain text. Raw HTML alert content is not
supported.

## Adding Alerts

Route handlers should use the messages capability instead of touching storage
keys directly.

```python
from wybra.messages import MessagesCapability
from wybra.site import get_site


messages = get_site(request.app).require_capability(MessagesCapability)
await messages.success(request, "Settings saved.")
```

Convenience helpers are available for `success`, `warning`, and `error`, or use
`add_alert(request, severity, message)` for a dynamic severity.

## Form Post Messages

Form post handlers can declare success and failure messages instead of looking
up the optional messages capability in every route.

```python
from wybra.forms import FormPostHandler


class SettingsPostHandler(FormPostHandler[SettingsForm]):
    success_message = "Settings saved."
    failure_message = "Settings could not be saved."

    async def commit(self, request, form):
        await save_settings(form.values)
```

After a valid form is committed without validation errors, the handler queues a
`success` alert when `wybra.messages` is configured. When form validation fails,
the handler queues an `error` alert. If `wybra.messages` is not configured, the
same handler continues without adding alerts.

Override `get_success_message()` or `get_failure_message()` when the message
needs to be computed from the submitted form or request.

## Template Context

When `wybra.messages` is configured, template context includes:

- `messages_enabled`: `True` when the module is configured.
- `alerts`: an iterable collection of alert records.
- `has_alerts`: `True` when alerts are available.

Each alert record exposes:

- `severity`
- `message`
- `created_at`

Template context peeks at queued alerts without immediately removing them. The
alerts are acknowledged and removed only when the alert collection is rendered
or otherwise inspected by the template. Routes that redirect, return JSON, serve
files, or render fragments without touching alert context leave the queued
alerts available for the next page that renders them.

## Templates And Styles

The default component is:

```jinja
{% include "components/alerts.html" ignore missing %}
```

The default stylesheet is:

```jinja
<link href="{{ asset_url('styles/messages.css') }}" rel="stylesheet">
```

Applications can override `components/alerts.html` and `styles/messages.css`
through normal module template/static precedence. CSS hooks are provided for
header, under-header, footer, sticky, closable, and timed display variants.
