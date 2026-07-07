# ruff: noqa: E501, I001
from tortoise import migrations
from tortoise.migrations import operations as ops
import functools
from json import dumps, loads
from uuid import uuid4
from tortoise import fields
from tortoise.indexes import Index


class Migration(migrations.Migration):
    initial = True

    operations = [
        ops.CreateModel(
            name="UserPhoneContact",
            fields=[
                (
                    "id",
                    fields.UUIDField(
                        primary_key=True, default=uuid4, unique=True, db_index=True
                    ),
                ),
                ("user_id", fields.UUIDField(db_index=True)),
                ("country_code", fields.CharField(max_length=2)),
                ("subdivision_code", fields.CharField(null=True, max_length=16)),
                ("normalised_number", fields.CharField(max_length=32)),
                ("number_type", fields.CharField(max_length=32)),
                ("sms_capable", fields.BooleanField(default=False)),
                ("verified_at", fields.FloatField(null=True)),
            ],
            options={
                "table": "profile_phone_contact",
                "app": "wybra_profile",
                "indexes": [Index(fields=["normalised_number"])],
                "pk_attr": "id",
                "table_description": "Per-user phone contact with per-number verification state.",
            },
            bases=["Model"],
        ),
        ops.CreateModel(
            name="UserProfile",
            fields=[
                (
                    "id",
                    fields.UUIDField(
                        primary_key=True, default=uuid4, unique=True, db_index=True
                    ),
                ),
                ("user_id", fields.UUIDField(unique=True)),
                ("profile_picture_media_id", fields.UUIDField(null=True)),
                ("preferred_name", fields.CharField(null=True, max_length=120)),
                ("display_name", fields.CharField(null=True, max_length=200)),
                ("bio", fields.TextField(null=True, unique=False)),
                ("first_name", fields.CharField(null=True, max_length=120)),
                ("last_name", fields.CharField(null=True, max_length=120)),
                (
                    "pronouns",
                    fields.JSONField(
                        null=True,
                        encoder=functools.partial(dumps, separators=(",", ":")),
                        decoder=loads,
                    ),
                ),
                ("phone_number", fields.CharField(null=True, max_length=48)),
                (
                    "website_links",
                    fields.JSONField(
                        null=True,
                        encoder=functools.partial(dumps, separators=(",", ":")),
                        decoder=loads,
                    ),
                ),
                ("country_region", fields.CharField(null=True, max_length=120)),
                ("city", fields.CharField(null=True, max_length=120)),
                ("postal_code", fields.CharField(null=True, max_length=24)),
                ("job_title", fields.CharField(null=True, max_length=160)),
                ("company", fields.CharField(null=True, max_length=200)),
                ("company_industry", fields.CharField(null=True, max_length=160)),
                ("department", fields.CharField(null=True, max_length=160)),
                ("date_time_format", fields.CharField(null=True, max_length=64)),
                ("theme", fields.CharField(null=True, max_length=32)),
                (
                    "notification_preferences",
                    fields.JSONField(
                        null=True,
                        encoder=functools.partial(dumps, separators=(",", ":")),
                        decoder=loads,
                    ),
                ),
                (
                    "profile_visibility",
                    fields.CharField(default="public", max_length=16),
                ),
                ("marketing_consent", fields.BooleanField(default=False)),
                ("terms_accepted_at", fields.FloatField(null=True)),
                ("data_deletion_requested", fields.BooleanField(default=False)),
            ],
            options={
                "table": "profile_user_profile",
                "app": "wybra_profile",
                "pk_attr": "id",
                "table_description": "App-facing profile data linked one-to-one with an auth user.",
            },
            bases=["Model"],
        ),
    ]
