from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from tortoise import fields
from tortoise.indexes import Index
from tortoise.models import Model

from wybra.auth.email_normalisation import normalise_email
from wybra.auth.session_tokens import SESSION_TOKEN_MAX_LENGTH
from wybra.auth.timestamps import current_timestamp


def current_datetime() -> datetime:
    return datetime.now(UTC)


class InitialAdminBootstrap(Model):
    """Serialises initial admin bootstrap state."""

    id = fields.IntField(primary_key=True)

    class Meta:
        table = "identity_initial_admin_bootstrap"


class IdentityProvider(Model):
    """Canonical provider identity row used by external login flows."""

    id = fields.UUIDField(primary_key=True, default=uuid.uuid4)
    provider_name = fields.CharField(max_length=100)
    provider_subject = fields.CharField(max_length=320)
    crypt_access_token = fields.CharField(max_length=1024)
    expires_at = fields.FloatField(null=True)
    crypt_refresh_token = fields.CharField(max_length=1024, null=True)
    account_email = fields.CharField(max_length=320)
    provider_enabled = fields.BooleanField(default=True)
    provider_metadata = fields.JSONField(null=True)

    class Meta:
        table = "identity_provider"
        unique_together = (("provider_name", "provider_subject"),)
        indexes = (
            Index(fields=("provider_name",)),
            Index(fields=("provider_subject",)),
            Index(fields=("provider_enabled",)),
        )


class ExternalIdentityLink(Model):
    """Link row between a local user and one provider identity."""

    if TYPE_CHECKING:
        user_id: uuid.UUID
        provider_id: uuid.UUID

    user = fields.ForeignKeyField(
        "wybra_auth.User",
        related_name=False,
        on_delete=fields.CASCADE,
    )
    provider = fields.OneToOneField(
        "wybra_auth.IdentityProvider",
        related_name=False,
        on_delete=fields.CASCADE,
    )

    class Meta:
        table = "identity_external_identity_link"
        unique_together = (("user_id", "provider_id"),)
        indexes = (Index(fields=("user_id",)),)


class IdentityUserEmail(Model):
    """Additional email addresses for local user accounts."""

    if TYPE_CHECKING:
        user_id: uuid.UUID

    id = fields.UUIDField(primary_key=True, default=uuid.uuid4)
    user = fields.ForeignKeyField(
        "wybra_auth.User",
        related_name=False,
        on_delete=fields.CASCADE,
        db_index=True,
    )
    email = fields.CharField(max_length=320, unique=True)
    is_primary = fields.BooleanField(default=True)
    is_verified = fields.BooleanField(default=False)

    async def save(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
        self.email = normalise_email(self.email)
        await super().save(*args, **kwargs)

    class Meta:
        table = "identity_user_email"


class User(Model):
    """Canonical local user account."""

    # Store Unix timestamps as `float` for cross-database consistency.
    id = fields.UUIDField(primary_key=True, default=uuid.uuid4)
    email = fields.CharField(max_length=320, unique=True, db_index=True)
    hashed_password = fields.CharField(max_length=1024, null=True)
    is_active = fields.BooleanField(default=True)
    is_superuser = fields.BooleanField(default=False)
    is_verified = fields.BooleanField(default=False)
    password_login_enabled = fields.BooleanField(default=True)
    is_admin = fields.BooleanField(default=False)
    created_at = fields.FloatField(default=current_timestamp)
    modified_at = fields.FloatField(default=current_timestamp)
    last_login_at = fields.FloatField(null=True)
    expires_at = fields.FloatField(null=True)
    email_verification_sent_at = fields.FloatField(null=True)
    preferred_timezone = fields.CharField(max_length=64, null=True)

    class Meta:
        table = "identity_user"
        indexes = (
            Index(fields=("is_active", "expires_at")),
            Index(fields=("last_login_at",)),
            Index(fields=("created_at",)),
            Index(fields=("modified_at",)),
            Index(fields=("is_admin",)),
            Index(fields=("is_superuser",)),
        )


if TYPE_CHECKING:

    class LocalUser(User):
        """Local account view for password-backed authentication paths."""

        hashed_password: str

else:
    LocalUser = User


class IdentityTotpCredential(Model):
    """A TOTP secret and its current lifecycle state."""

    if TYPE_CHECKING:
        user_id: uuid.UUID

    id = fields.UUIDField(primary_key=True, default=uuid.uuid4)
    user = fields.ForeignKeyField(
        "wybra_auth.User",
        related_name=False,
        on_delete=fields.CASCADE,
        db_index=True,
    )
    crypt_secret = fields.CharField(max_length=1024)
    status = fields.CharField(max_length=16, db_index=True)
    created_at = fields.FloatField(db_index=True)
    activated_at = fields.FloatField(null=True)
    disabled_at = fields.FloatField(null=True)
    last_used_counter = fields.IntField(null=True)

    class Meta:
        table = "identity_totp_credential"


class IdentityAuthenticationChallenge(Model):
    """Transient authentication challenge metadata."""

    if TYPE_CHECKING:
        user_id: uuid.UUID

    id = fields.CharField(max_length=32, primary_key=True)
    user = fields.ForeignKeyField(
        "wybra_auth.User",
        related_name=False,
        on_delete=fields.CASCADE,
        db_index=True,
    )
    kind = fields.CharField(max_length=16)
    expires_at = fields.FloatField(db_index=True)
    metadata_payload = fields.JSONField(null=True, source_field="metadata")

    class Meta:
        table = "identity_authentication_challenge"


class IdentityWebAuthnCredential(Model):
    """A WebAuthn public-key credential linked to a local account."""

    if TYPE_CHECKING:
        user_id: uuid.UUID

    id = fields.UUIDField(primary_key=True, default=uuid.uuid4)
    user = fields.ForeignKeyField(
        "wybra_auth.User",
        related_name=False,
        on_delete=fields.CASCADE,
        db_index=True,
    )
    credential_id = fields.CharField(max_length=1024, unique=True)
    public_key = fields.BinaryField()
    sign_count = fields.IntField(default=0)
    status = fields.CharField(max_length=16, db_index=True)
    label = fields.CharField(max_length=120, null=True)
    created_at = fields.FloatField(db_index=True)
    last_used_at = fields.FloatField(null=True)
    revoked_at = fields.FloatField(null=True)
    user_verified = fields.BooleanField(default=False)
    credential_device_type = fields.CharField(max_length=32, null=True)
    credential_backed_up = fields.BooleanField(default=False)
    transports = fields.JSONField(null=True)
    aaguid = fields.CharField(max_length=64, null=True)
    attestation_format = fields.CharField(max_length=64, null=True)

    class Meta:
        table = "identity_webauthn_credential"
        indexes = (Index(fields=("user_id", "status")),)


class IdentityTotpRecoveryCode(Model):
    """Single-use TOTP recovery codes linked to a TOTP credential."""

    if TYPE_CHECKING:
        credential_id: uuid.UUID

    id = fields.UUIDField(primary_key=True, default=uuid.uuid4)
    credential = fields.ForeignKeyField(
        "wybra_auth.IdentityTotpCredential",
        related_name=False,
        on_delete=fields.CASCADE,
        db_index=True,
    )
    code_verifier = fields.CharField(max_length=256)
    consumed_at = fields.FloatField(null=True, db_index=True)
    created_at = fields.FloatField()

    class Meta:
        table = "identity_totp_recovery_code"
        unique_together = (("credential_id", "code_verifier"),)


class Group(Model):
    """Authorisation group used to collect reusable scopes."""

    id = fields.UUIDField(primary_key=True, default=uuid.uuid4)
    abbrev = fields.CharField(max_length=120, unique=True)
    description = fields.TextField(default="")

    class Meta:
        table = "identity_group"


class Scope(Model):
    """Authorisation scope assignable to groups."""

    scope = fields.CharField(max_length=255, primary_key=True)
    description = fields.TextField(null=True)

    class Meta:
        table = "identity_scope"


class GroupScope(Model):
    """Scope assignment on an authorisation group."""

    if TYPE_CHECKING:
        group_id: uuid.UUID

    group = fields.ForeignKeyField(
        "wybra_auth.Group",
        related_name=False,
        on_delete=fields.CASCADE,
        db_index=True,
    )
    scope = fields.CharField(max_length=255, db_index=True)

    class Meta:
        table = "identity_group_scope"
        unique_together = (("group_id", "scope"),)


class GroupUser(Model):
    """Direct user membership in an authorisation group."""

    if TYPE_CHECKING:
        group_id: uuid.UUID
        user_id: uuid.UUID

    group = fields.ForeignKeyField(
        "wybra_auth.Group",
        related_name=False,
        on_delete=fields.CASCADE,
        db_index=True,
    )
    user = fields.ForeignKeyField(
        "wybra_auth.User",
        related_name=False,
        on_delete=fields.CASCADE,
        db_index=True,
    )

    class Meta:
        table = "identity_group_user"
        unique_together = (("group_id", "user_id"),)


class GroupGroup(Model):
    """Nested group membership in an authorisation group tree."""

    if TYPE_CHECKING:
        parent_group_id: uuid.UUID
        child_group_id: uuid.UUID

    parent_group = fields.ForeignKeyField(
        "wybra_auth.Group",
        related_name=False,
        on_delete=fields.CASCADE,
        db_index=True,
    )
    child_group = fields.ForeignKeyField(
        "wybra_auth.Group",
        related_name=False,
        on_delete=fields.CASCADE,
        db_index=True,
    )

    class Meta:
        table = "identity_group_group"
        unique_together = (("parent_group_id", "child_group_id"),)


class AccessToken(Model):
    """Server-side browser session token."""

    if TYPE_CHECKING:
        user_id: uuid.UUID

    token = fields.CharField(max_length=SESSION_TOKEN_MAX_LENGTH, primary_key=True)
    created_at = fields.DatetimeField(default=current_datetime, db_index=True)
    user = fields.ForeignKeyField(
        "wybra_auth.User",
        related_name=False,
        on_delete=fields.CASCADE,
        db_index=True,
    )

    class Meta:
        table = "identity_access_token"


__all__ = (
    "AccessToken",
    "ExternalIdentityLink",
    "IdentityAuthenticationChallenge",
    "IdentityTotpCredential",
    "IdentityTotpRecoveryCode",
    "IdentityUserEmail",
    "IdentityProvider",
    "LocalUser",
    "Group",
    "GroupGroup",
    "GroupScope",
    "GroupUser",
    "InitialAdminBootstrap",
    "Scope",
    "User",
)
