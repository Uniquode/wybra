# Content Types

`wybra.content_types` provides site-local metadata for configured Tortoise
models. It is a runtime registry, not a database table.

Configure it with the database module and the modules that provide model
surfaces:

```toml
[app]
modules = ["wybra.db", "wybra.content_types", "example.articles"]
```

After Tortoise finalises the configured models, Wybra derives one content type
for every non-abstract model. Application modules do not import models for
registration, use decorators, or call a registration API.

```python
from wybra.content_types import ContentTypesCapability
from wybra.site import get_site

content_types = get_site(request.app).require_capability(ContentTypesCapability)
article_type = content_types.for_model(Article)
article_model = content_types.for_identifier(article_type.identifier).model
```

Identifiers derive from Tortoise's finalised app and database-table identity,
including schema where applicable. They are independent of Python class names,
labels, and routes. Renaming an app, schema, or table is therefore a semantic
identifier migration when the identifier is stored in application data.
Schema and table names must not contain dots.

## Model metadata defaults

By default, the model class name produces a title-case source singular label
while preserving acronym runs (`APIKey` becomes `API Key`), and `inflect`
derives the English plural. Standard CRUD actions are all enabled:
`list`, `view`, `create`, `update`, and `delete`.

Use `Model.Meta` only to adjust presentation or actions:

```python
class Person(Model):
    class Meta:
        verbose_name_plural = "People"
        content_exclude = {"delete"}
```

`content_actions` selects an action set, and `content_exclude` subtracts from
that selected set or from the default. Unknown actions fail during startup.

The labels are canonical source labels. Later localisation translates them for
display; it does not infer translated words from model class names.

## Generic views, routes, and policy

Content types supply model metadata to generic views but never create or choose
routes. A generic view remains an ordinary explicitly decorated route, owned by
the application. Content types also do not carry scopes or policy rules; the
scope-decorators change will own that behaviour.

There is deliberately no generic foreign-key support, multi-tenant registry,
or persisted `content_type` table in this first implementation.
