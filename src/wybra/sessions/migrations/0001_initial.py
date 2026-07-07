# ruff: noqa: E501, I001
from tortoise import migrations
from tortoise.migrations import operations as ops
from tortoise import fields


class Migration(migrations.Migration):
    initial = True

    operations = [
        ops.CreateModel(
            name="SessionRecordModel",
            fields=[
                (
                    "id",
                    fields.CharField(
                        primary_key=True, unique=True, db_index=True, max_length=128
                    ),
                ),
                ("data", fields.TextField(unique=False)),
                ("created_at", fields.FloatField()),
                ("updated_at", fields.FloatField()),
                ("expires_at", fields.FloatField(db_index=True)),
            ],
            options={
                "table": "sessions_session",
                "app": "wybra_sessions",
                "pk_attr": "id",
                "table_description": "Server-side request session persisted by the database backend.",
            },
            bases=["Model"],
        ),
    ]
