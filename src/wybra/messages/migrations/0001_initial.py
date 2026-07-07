# ruff: noqa: E501, I001
from tortoise import migrations
from tortoise.migrations import operations as ops
from time import time
from tortoise import fields
from tortoise.indexes import Index


class Migration(migrations.Migration):
    initial = True

    operations = [
        ops.CreateModel(
            name="MessageAlert",
            fields=[
                (
                    "id",
                    fields.IntField(
                        generated=True, primary_key=True, unique=True, db_index=True
                    ),
                ),
                ("queue_key", fields.CharField(max_length=255)),
                ("severity", fields.CharField(max_length=16)),
                ("message", fields.TextField(unique=False)),
                ("created_at", fields.FloatField(default=time)),
                ("expires_at", fields.FloatField(null=True, db_index=True)),
            ],
            options={
                "table": "messages_alert",
                "app": "wybra_messages",
                "indexes": [Index(fields=["queue_key", "id"])],
                "pk_attr": "id",
                "table_description": "Queued user-facing alert stored by the database messages backend.",
            },
            bases=["Model"],
        ),
    ]
