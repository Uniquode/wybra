# Streams

`wybra.streams` provides optional, provider-neutral live fan-out for opaque
byte payloads. It is independent of `wybra.cache`, `wybra.tasks`, and
`wybra.events`: configure it when an application needs live notifications.

Streams are not a durable transport. They provide no replay, acknowledgement,
consumer position, retry, queueing, result, or scheduling guarantee. Use
`wybra.tasks` when a consumer must recover work or observe delivery attempts.

## Configuration

Memory is the default provider and is suitable for one process or deterministic
tests:

```toml
[app]
modules = ["wybra.streams"]

[streams]
backend = "memory"
```

Install network providers before selecting Redis or NATS:

```sh
uv add 'wybra[streams]'
```

Redis uses one independently configured connection and an isolated namespace:

```toml
[app]
modules = ["wybra.secrets", "wybra.streams"]

[streams]
backend = "redis"
url_source = "keychain"
namespace = "website_live"
```

The derived keychain URL key is `streams/redis/url`. An explicit environment
source can choose another variable with `url_key`; alternatively,
`WYBRA_STREAMS_URL` supplies the endpoint directly. A credential-free Redis
URL may remain in configuration and receive separately resolved credentials:

```toml
[streams]
backend = "redis"
url = "rediss://cache.internal:6380/0"
credentials_source = "keychain"
namespace = "website_live"
```

The derived credential key is `streams/redis/credentials`; credentials use
`username:password` form, with `:password` valid for password-only Redis
authentication. A URL source supplies a complete URL and cannot be combined
with `url` or `credentials_source`. Settings and diagnostics never expose
resolved URLs or credentials.

Use core NATS live subjects when the application already has a NATS service:

```toml
[streams]
backend = "nats"
servers = ["nats://nats-1.internal:4222", "nats://nats-2.internal:4222"]
namespace = "website_live"
```

Every configured NATS server is passed to the NATS client as its seed-server
pool. The client connects to one usable server and retains the remaining
servers for reconnection and failover; Wybra does not connect to every broker
at once. Do not mix WebSocket (`ws` or `wss`) and TCP (`nats` or `tls`) server
URLs in the same pool.

NATS authentication is optional. When required, configure an explicit mode and
resolve its value through the keychain or environment rather than embedding
credentials in a server URL:

```toml
[app]
modules = ["wybra.secrets", "wybra.streams"]

[streams]
backend = "nats"
servers = ["tls://nats-1.internal:4222", "tls://nats-2.internal:4222"]
authentication = "token"
authentication_source = "keychain"
namespace = "website_live"
```

The derived token key is `streams/nats/token`. Set
`authentication = "user-password"` to resolve `username:password` credentials
instead; its derived key is `streams/nats/credentials`. Environment sources
use `WYBRA_STREAMS_NATS_TOKEN` or `WYBRA_STREAMS_NATS_CREDENTIALS` by default,
or select a distinct key with `authentication_key`. NKey, JWT credentials-file,
and client-certificate authentication are not supported by the initial streams
module.

NATS streams do not require JetStream. The memory provider is process-local;
Redis and NATS deliver between connected application instances. Redis and NATS
provider connections are started during streams setup and closed during site
shutdown. Their order in `[app].modules` does not depend on cache or tasks
configuration.

Safe diagnostics identify the configured provider, namespace, and secret
reference source and key. They never expose resolved endpoints or credentials.

## Publishing and subscribing

Resolve `StreamsCapability` from the site. Owners and topics are non-blank
application identifiers; their pair is isolated inside the configured
namespace. Payloads are opaque `bytes` and are limited to 65,536 bytes before
provider I/O.

```python
from wybra.streams import StreamsCapability

streams = site.require_capability(StreamsCapability)
subscription = await streams.subscribe("notifications", "updates")
try:
    await streams.publish("notifications", "updates", b"refresh")
    message = await subscription.receive(timeout=10)
finally:
    await subscription.close()
```

Subscriptions also support async iteration. Always close a subscription when
it is no longer needed; site shutdown closes any remaining provider resources.
Messages published before subscription or after a subscriber disconnects may
be missed by design.

The process-local provider uses bounded per-subscription buffers. A subscriber
that cannot keep up is closed and its unread live messages are discarded rather
than allowing unbounded memory growth. Reconnect and use a durable task or
stream transport when every message must be recovered.
