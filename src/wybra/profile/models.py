from __future__ import annotations

import uuid

from tortoise import fields
from tortoise.indexes import Index
from tortoise.models import Model


class UserProfile(Model):
    """App-facing profile data linked one-to-one with an auth user."""

    id = fields.UUIDField(primary_key=True, default=uuid.uuid4)
    user_id = fields.UUIDField(unique=True)
    profile_picture_media_id = fields.UUIDField(null=True)
    preferred_name = fields.CharField(max_length=120, null=True)
    display_name = fields.CharField(max_length=200, null=True)
    bio = fields.TextField(null=True)
    first_name = fields.CharField(max_length=120, null=True)
    last_name = fields.CharField(max_length=120, null=True)
    pronouns = fields.JSONField(null=True)
    phone_number = fields.CharField(max_length=48, null=True)
    website_links = fields.JSONField(null=True)
    country_region = fields.CharField(max_length=120, null=True)
    city = fields.CharField(max_length=120, null=True)
    postal_code = fields.CharField(max_length=24, null=True)
    job_title = fields.CharField(max_length=160, null=True)
    company = fields.CharField(max_length=200, null=True)
    company_industry = fields.CharField(max_length=160, null=True)
    department = fields.CharField(max_length=160, null=True)
    date_time_format = fields.CharField(max_length=64, null=True)
    theme = fields.CharField(max_length=32, null=True)
    notification_preferences = fields.JSONField(null=True)
    profile_visibility = fields.CharField(max_length=16, default="public")
    marketing_consent = fields.BooleanField(default=False)
    terms_accepted_at = fields.FloatField(null=True)
    data_deletion_requested = fields.BooleanField(default=False)

    class Meta:
        table = "profile_user_profile"


class UserPhoneContact(Model):
    """Per-user phone contact with per-number verification state."""

    id = fields.UUIDField(primary_key=True, default=uuid.uuid4)
    user_id = fields.UUIDField(db_index=True)
    country_code = fields.CharField(max_length=2)
    subdivision_code = fields.CharField(max_length=16, null=True)
    normalised_number = fields.CharField(max_length=32)
    number_type = fields.CharField(max_length=32)
    sms_capable = fields.BooleanField(default=False)
    # Unix timestamp seconds when this phone contact was verified.
    verified_at = fields.FloatField(null=True)

    class Meta:
        table = "profile_phone_contact"
        indexes = (Index(fields=("normalised_number",)),)


__all__ = ("UserPhoneContact", "UserProfile")
