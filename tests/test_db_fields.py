from __future__ import annotations

import uuid

from wybra.db import fields
from wybra.db.indexes import Index
from wybra.db.models import Model
from wybra.db.query import Manager, Q, QuerySet


def test_wybra_uuid_primary_key_field_defaults_to_uuid7() -> None:
    field = fields.UUIDField(primary_key=True)

    assert field.default is uuid.uuid7
    assert field.to_python_value(field.default()).version == 7


def test_wybra_database_facades_expose_model_declaration_primitives() -> None:
    class Article(Model):
        id = fields.UUIDField(primary_key=True)
        title = fields.CharField(max_length=200)

        class Meta:
            table = "test_facade_article"
            indexes = (Index(fields=("title",)),)

    assert Article._meta.pk_attr == "id"
    assert isinstance(Q(title="Example"), Q)


def test_wybra_database_facade_supports_default_and_named_managers() -> None:
    class VisibleManager(Manager):
        def get_queryset(self) -> QuerySet:
            return super().get_queryset().filter(deleted=False)

    class Article(Model):
        id = fields.UUIDField(primary_key=True)
        deleted = fields.BooleanField(default=False)
        all_records = Manager()

        class Meta:
            table = "test_facade_managed_article"
            manager = VisibleManager()

    assert isinstance(Article._meta.manager, VisibleManager)
    assert Article.all_records._model is Article
