# ruff: noqa: E501, I001
from tortoise import fields, migrations
from tortoise.migrations import operations as ops

from wybra.db import VersionField


class Migration(migrations.Migration):
    initial = True

    operations = [
        ops.CreateModel(
            name="FormAddress",
            fields=[
                ("id", fields.IntField(primary_key=True, unique=True)),
                ("street", fields.CharField(max_length=120)),
            ],
            options={
                "table": "test_form_address",
                "app": "tests_support_form_binding",
                "pk_attr": "id",
            },
            bases=["Model"],
        ),
        ops.CreateModel(
            name="FormPhone",
            fields=[
                ("id", fields.IntField(primary_key=True, unique=True)),
                ("number", fields.CharField(max_length=120)),
            ],
            options={
                "table": "test_form_phone",
                "app": "tests_support_form_binding",
                "pk_attr": "id",
            },
            bases=["Model"],
        ),
        ops.CreateModel(
            name="FormContact",
            fields=[
                ("id", fields.IntField(primary_key=True, unique=True)),
                (
                    "address",
                    fields.ForeignKeyField(
                        "tests_support_form_binding.FormAddress",
                        on_delete=fields.CASCADE,
                    ),
                ),
                (
                    "phone",
                    fields.ForeignKeyField(
                        "tests_support_form_binding.FormPhone",
                        on_delete=fields.CASCADE,
                    ),
                ),
                ("name", fields.CharField(max_length=120, unique=True)),
            ],
            options={
                "table": "test_form_contact",
                "app": "tests_support_form_binding",
                "pk_attr": "id",
            },
            bases=["Model"],
        ),
        ops.CreateModel(
            name="FormLabel",
            fields=[
                ("id", fields.IntField(primary_key=True, unique=True)),
                ("name", fields.CharField(max_length=120)),
            ],
            options={
                "table": "test_form_label",
                "app": "tests_support_form_binding",
                "pk_attr": "id",
            },
            bases=["Model"],
        ),
        ops.CreateModel(
            name="FormDocument",
            fields=[
                ("id", fields.IntField(primary_key=True, unique=True)),
                (
                    "labels",
                    fields.ManyToManyField(
                        "tests_support_form_binding.FormLabel",
                    ),
                ),
            ],
            options={
                "table": "test_form_document",
                "app": "tests_support_form_binding",
                "pk_attr": "id",
            },
            bases=["Model"],
        ),
        ops.CreateModel(
            name="FormVersionedRecord",
            fields=[
                ("id", fields.IntField(primary_key=True, unique=True)),
                ("data", fields.CharField(max_length=120)),
                ("enabled", fields.BooleanField(default=True)),
                ("version", VersionField()),
            ],
            options={
                "table": "test_form_versioned_record",
                "app": "tests_support_form_binding",
                "pk_attr": "id",
            },
            bases=["Model"],
        ),
    ]
