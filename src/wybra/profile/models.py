from __future__ import annotations

from typing import TYPE_CHECKING

from wybra.db import VersionField, fields
from wybra.db.indexes import Index
from wybra.db.models import Model

if TYPE_CHECKING:
    import uuid


class UserProfile(Model):
    """App-facing profile data linked one-to-one with an auth user."""

    if TYPE_CHECKING:
        user_id: uuid.UUID
        profile_picture_media_id: uuid.UUID | None

    id = fields.UUIDField(primary_key=True)
    user = fields.OneToOneField(
        "wybra_auth.User",
        related_name=False,
        on_delete=fields.CASCADE,
    )
    profile_picture_media = fields.ForeignKeyField(
        "wybra_media.MediaItem",
        related_name=False,
        on_delete=fields.SET_NULL,
        null=True,
    )
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
    version = VersionField()

    class Meta:
        table = "profile_user_profile"


class UserPhoneContact(Model):
    """Per-user phone contact with per-number verification state."""

    if TYPE_CHECKING:
        user_id: uuid.UUID

    id = fields.UUIDField(primary_key=True)
    user = fields.ForeignKeyField(
        "wybra_auth.User",
        related_name=False,
        on_delete=fields.CASCADE,
        db_index=True,
    )
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
