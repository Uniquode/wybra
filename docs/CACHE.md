# Cache

`wybra.cache` provides an optional, capability-backed cache for application
code and Jinja template fragments. Cache values are opaque bytes; callers own
serialisation and cache-key variation.

## Configuration

Install Redis support when using the Redis backend:

```sh
uv add 'wybra[cache]'
```

Configure the module and selected backend in the host application configuration:

```toml
[app]
modules = [
  "wybra.template",
  "wybra.cache",
]

[cache]
backend = "redis"
url = "redis://localhost:6379/0"
```

`backend = "memory"` is the default. It is process-local, has no size bound,
and removes expired values only when their keys are accessed. Use it for local
development or small, bounded workloads; use Redis when cache state must be
shared across workers or instances.

Every cache operation requires an owner and a logical key. Owners must be
non-blank and cannot contain `:`; the owner prefixes the backend key and keeps
independent cache domains separate. Cache entries always have an explicit,
positive TTL.

## Template fragments

`wybra.template` always recognises the cache tag, even when `wybra.cache` is
not configured. Without a cache capability, the tag simply renders its body.

```jinja
{% cache "profile-card" ttl=300 vary_by=(request.user.id, locale) %}
  <h2>{{ request.user.display_name }}</h2>
{% endcache %}
```

The explicit name, template generation, and `vary_by` values identify a
fragment. Include every value that can change the rendered body in `vary_by`.
For personalised output this normally includes a stable user or request
identity, and may also include locale, permissions, tenant, or feature state.

`vary_by` accepts JSON-compatible values: `None`, booleans, finite numbers,
strings, mappings with string keys, and lists or tuples containing those values.
Mappings are ordered by key; lists and tuples retain their order; sets are
ordered deterministically. Do not pass a model, request, datetime, enum, or
another arbitrary object directly: its display representation is not a safe
cache identity.

Use the template `cache_key()` helper when a fragment varies by several named
conditions or includes an application type. It produces a canonical key value:

```jinja
{% cache "profile-card" ttl=300
   vary_by=cache_key(user=request.user, locale=locale, permissions=permissions) %}
  <h2>{{ request.user.display_name }}</h2>
{% endcache %}
```

Register normalisers for application-specific values on the template
capability. A normaliser must return only JSON-compatible values; for models,
prefer an explicit stable type identifier and primary key:

```python
templates.register_cache_key_normaliser(
    User,
    lambda user: {"type": "accounts.user", "pk": user.pk},
)
```

After registering a normaliser, `cache_key(user=request.user)` and
`vary_by=request.user` both use it. Prefer `cache_key()` for named, readable
variation conditions in templates.

Never cache CSRF tokens, password-reset links, one-time codes, or other
per-request secrets inside a fragment. Keep those values outside the cached
body, or use a design that deliberately separates the per-request value from
the reusable markup.

The fragment cache stores rendered markup as UTF-8 bytes. It does not cache
querysets, serialise structured Python values, or invalidate reverse proxies
or CDNs.
