from tortoise import migrations
from tortoise.migrations import operations as ops
import functools
from json import dumps, loads
from tortoise.fields.base import OnDelete
from uuid import uuid4
from wybra.auth.models import current_datetime
from wybra.auth.timestamps import current_timestamp
from tortoise import fields
from tortoise.indexes import Index

class Migration(migrations.Migration):
    initial = True

    operations = [
        ops.CreateModel(
            name='Group',
            fields=[
                ('id', fields.UUIDField(primary_key=True, default=uuid4, unique=True, db_index=True)),
                ('abbrev', fields.CharField(unique=True, max_length=120)),
                ('description', fields.TextField(default='', unique=False)),
            ],
            options={'table': 'identity_group', 'app': 'wybra_auth', 'pk_attr': 'id', 'table_description': 'Authorisation group used to collect reusable scopes.'},
            bases=['Model'],
        ),
        ops.CreateModel(
            name='GroupGroup',
            fields=[
                ('id', fields.IntField(generated=True, primary_key=True, unique=True, db_index=True)),
                ('parent_group', fields.ForeignKeyField('wybra_auth.Group', source_field='parent_group_id', db_index=True, db_constraint=True, to_field='id', related_name=False, on_delete=OnDelete.CASCADE)),
                ('child_group', fields.ForeignKeyField('wybra_auth.Group', source_field='child_group_id', db_index=True, db_constraint=True, to_field='id', related_name=False, on_delete=OnDelete.CASCADE)),
            ],
            options={'table': 'identity_group_group', 'app': 'wybra_auth', 'unique_together': (('parent_group_id', 'child_group_id'),), 'pk_attr': 'id', 'table_description': 'Nested group membership in an authorisation group tree.'},
            bases=['Model'],
        ),
        ops.CreateModel(
            name='GroupScope',
            fields=[
                ('id', fields.IntField(generated=True, primary_key=True, unique=True, db_index=True)),
                ('group', fields.ForeignKeyField('wybra_auth.Group', source_field='group_id', db_index=True, db_constraint=True, to_field='id', related_name=False, on_delete=OnDelete.CASCADE)),
                ('scope', fields.CharField(db_index=True, max_length=255)),
            ],
            options={'table': 'identity_group_scope', 'app': 'wybra_auth', 'unique_together': (('group_id', 'scope'),), 'pk_attr': 'id', 'table_description': 'Scope assignment on an authorisation group.'},
            bases=['Model'],
        ),
        ops.CreateModel(
            name='IdentityProvider',
            fields=[
                ('id', fields.UUIDField(primary_key=True, default=uuid4, unique=True, db_index=True)),
                ('provider_name', fields.CharField(max_length=100)),
                ('provider_subject', fields.CharField(max_length=320)),
                ('crypt_access_token', fields.CharField(max_length=1024)),
                ('expires_at', fields.FloatField(null=True)),
                ('crypt_refresh_token', fields.CharField(null=True, max_length=1024)),
                ('account_email', fields.CharField(max_length=320)),
                ('provider_enabled', fields.BooleanField(default=True)),
                ('provider_metadata', fields.JSONField(null=True, encoder=functools.partial(dumps, separators=(',', ':')), decoder=loads)),
            ],
            options={'table': 'identity_provider', 'app': 'wybra_auth', 'unique_together': (('provider_name', 'provider_subject'),), 'indexes': [Index(fields=['provider_name']), Index(fields=['provider_subject']), Index(fields=['provider_enabled'])], 'pk_attr': 'id', 'table_description': 'Canonical provider identity row used by external login flows.'},
            bases=['Model'],
        ),
        ops.CreateModel(
            name='InitialAdminBootstrap',
            fields=[
                ('id', fields.IntField(generated=True, primary_key=True, unique=True, db_index=True)),
            ],
            options={'table': 'identity_initial_admin_bootstrap', 'app': 'wybra_auth', 'pk_attr': 'id', 'table_description': 'Serialises initial admin bootstrap state.'},
            bases=['Model'],
        ),
        ops.CreateModel(
            name='Scope',
            fields=[
                ('scope', fields.CharField(primary_key=True, unique=True, db_index=True, max_length=255)),
                ('description', fields.TextField(null=True, unique=False)),
            ],
            options={'table': 'identity_scope', 'app': 'wybra_auth', 'pk_attr': 'scope', 'table_description': 'Authorisation scope assignable to groups.'},
            bases=['Model'],
        ),
        ops.CreateModel(
            name='User',
            fields=[
                ('id', fields.UUIDField(primary_key=True, default=uuid4, unique=True, db_index=True)),
                ('email', fields.CharField(unique=True, db_index=True, max_length=320)),
                ('hashed_password', fields.CharField(null=True, max_length=1024)),
                ('is_active', fields.BooleanField(default=True)),
                ('is_superuser', fields.BooleanField(default=False)),
                ('is_verified', fields.BooleanField(default=False)),
                ('password_login_enabled', fields.BooleanField(default=True)),
                ('is_admin', fields.BooleanField(default=False)),
                ('created_at', fields.FloatField(default=current_timestamp)),
                ('modified_at', fields.FloatField(default=current_timestamp)),
                ('last_login_at', fields.FloatField(null=True)),
                ('expires_at', fields.FloatField(null=True)),
                ('email_verification_sent_at', fields.FloatField(null=True)),
                ('preferred_timezone', fields.CharField(null=True, max_length=64)),
            ],
            options={'table': 'identity_user', 'app': 'wybra_auth', 'indexes': [Index(fields=['is_active', 'expires_at']), Index(fields=['last_login_at']), Index(fields=['created_at']), Index(fields=['modified_at']), Index(fields=['is_admin']), Index(fields=['is_superuser'])], 'pk_attr': 'id', 'table_description': 'Canonical local user account.'},
            bases=['Model'],
        ),
        ops.CreateModel(
            name='AccessToken',
            fields=[
                ('token', fields.CharField(primary_key=True, unique=True, db_index=True, max_length=128)),
                ('created_at', fields.DatetimeField(default=current_datetime, db_index=True, auto_now=False, auto_now_add=False)),
                ('user', fields.ForeignKeyField('wybra_auth.User', source_field='user_id', db_index=True, db_constraint=True, to_field='id', related_name=False, on_delete=OnDelete.CASCADE)),
            ],
            options={'table': 'identity_access_token', 'app': 'wybra_auth', 'pk_attr': 'token', 'table_description': 'Server-side browser session token.'},
            bases=['Model'],
        ),
        ops.CreateModel(
            name='ExternalIdentityLink',
            fields=[
                ('id', fields.IntField(generated=True, primary_key=True, unique=True, db_index=True)),
                ('user', fields.ForeignKeyField('wybra_auth.User', source_field='user_id', db_constraint=True, to_field='id', related_name=False, on_delete=OnDelete.CASCADE)),
                ('provider', fields.OneToOneField('wybra_auth.IdentityProvider', source_field='provider_id', db_constraint=True, to_field='id', related_name=False, on_delete=OnDelete.CASCADE)),
            ],
            options={'table': 'identity_external_identity_link', 'app': 'wybra_auth', 'unique_together': (('user_id', 'provider_id'),), 'indexes': [Index(fields=['user_id'])], 'pk_attr': 'id', 'table_description': 'Link row between a local user and one provider identity.'},
            bases=['Model'],
        ),
        ops.CreateModel(
            name='GroupUser',
            fields=[
                ('id', fields.IntField(generated=True, primary_key=True, unique=True, db_index=True)),
                ('group', fields.ForeignKeyField('wybra_auth.Group', source_field='group_id', db_index=True, db_constraint=True, to_field='id', related_name=False, on_delete=OnDelete.CASCADE)),
                ('user', fields.ForeignKeyField('wybra_auth.User', source_field='user_id', db_index=True, db_constraint=True, to_field='id', related_name=False, on_delete=OnDelete.CASCADE)),
            ],
            options={'table': 'identity_group_user', 'app': 'wybra_auth', 'unique_together': (('group_id', 'user_id'),), 'pk_attr': 'id', 'table_description': 'Direct user membership in an authorisation group.'},
            bases=['Model'],
        ),
        ops.CreateModel(
            name='IdentityAuthenticationChallenge',
            fields=[
                ('id', fields.CharField(primary_key=True, unique=True, db_index=True, max_length=32)),
                ('user', fields.ForeignKeyField('wybra_auth.User', source_field='user_id', db_index=True, db_constraint=True, to_field='id', related_name=False, on_delete=OnDelete.CASCADE)),
                ('kind', fields.CharField(max_length=16)),
                ('expires_at', fields.FloatField(db_index=True)),
                ('metadata_payload', fields.JSONField(source_field='metadata', null=True, encoder=functools.partial(dumps, separators=(',', ':')), decoder=loads)),
            ],
            options={'table': 'identity_authentication_challenge', 'app': 'wybra_auth', 'pk_attr': 'id', 'table_description': 'Transient authentication challenge metadata.'},
            bases=['Model'],
        ),
        ops.CreateModel(
            name='IdentityTotpCredential',
            fields=[
                ('id', fields.UUIDField(primary_key=True, default=uuid4, unique=True, db_index=True)),
                ('user', fields.ForeignKeyField('wybra_auth.User', source_field='user_id', db_index=True, db_constraint=True, to_field='id', related_name=False, on_delete=OnDelete.CASCADE)),
                ('crypt_secret', fields.CharField(max_length=1024)),
                ('status', fields.CharField(db_index=True, max_length=16)),
                ('created_at', fields.FloatField(db_index=True)),
                ('activated_at', fields.FloatField(null=True)),
                ('disabled_at', fields.FloatField(null=True)),
                ('last_used_counter', fields.IntField(null=True)),
            ],
            options={'table': 'identity_totp_credential', 'app': 'wybra_auth', 'pk_attr': 'id', 'table_description': 'A TOTP secret and its current lifecycle state.'},
            bases=['Model'],
        ),
        ops.CreateModel(
            name='IdentityTotpRecoveryCode',
            fields=[
                ('id', fields.UUIDField(primary_key=True, default=uuid4, unique=True, db_index=True)),
                ('credential', fields.ForeignKeyField('wybra_auth.IdentityTotpCredential', source_field='credential_id', db_index=True, db_constraint=True, to_field='id', related_name=False, on_delete=OnDelete.CASCADE)),
                ('code_verifier', fields.CharField(max_length=256)),
                ('consumed_at', fields.FloatField(null=True, db_index=True)),
                ('created_at', fields.FloatField()),
            ],
            options={'table': 'identity_totp_recovery_code', 'app': 'wybra_auth', 'unique_together': (('credential_id', 'code_verifier'),), 'pk_attr': 'id', 'table_description': 'Single-use TOTP recovery codes linked to a TOTP credential.'},
            bases=['Model'],
        ),
        ops.CreateModel(
            name='IdentityUserEmail',
            fields=[
                ('id', fields.UUIDField(primary_key=True, default=uuid4, unique=True, db_index=True)),
                ('user', fields.ForeignKeyField('wybra_auth.User', source_field='user_id', db_index=True, db_constraint=True, to_field='id', related_name=False, on_delete=OnDelete.CASCADE)),
                ('email', fields.CharField(unique=True, max_length=320)),
                ('is_primary', fields.BooleanField(default=True)),
                ('is_verified', fields.BooleanField(default=False)),
            ],
            options={'table': 'identity_user_email', 'app': 'wybra_auth', 'pk_attr': 'id', 'table_description': 'Additional email addresses for local user accounts.'},
            bases=['Model'],
        ),
        ops.CreateModel(
            name='IdentityWebAuthnCredential',
            fields=[
                ('id', fields.UUIDField(primary_key=True, default=uuid4, unique=True, db_index=True)),
                ('user', fields.ForeignKeyField('wybra_auth.User', source_field='user_id', db_index=True, db_constraint=True, to_field='id', related_name=False, on_delete=OnDelete.CASCADE)),
                ('credential_id', fields.CharField(unique=True, max_length=1024)),
                ('public_key', fields.BinaryField()),
                ('sign_count', fields.IntField(default=0)),
                ('status', fields.CharField(db_index=True, max_length=16)),
                ('label', fields.CharField(null=True, max_length=120)),
                ('created_at', fields.FloatField(db_index=True)),
                ('last_used_at', fields.FloatField(null=True)),
                ('revoked_at', fields.FloatField(null=True)),
                ('user_verified', fields.BooleanField(default=False)),
                ('credential_device_type', fields.CharField(null=True, max_length=32)),
                ('credential_backed_up', fields.BooleanField(default=False)),
                ('transports', fields.JSONField(null=True, encoder=functools.partial(dumps, separators=(',', ':')), decoder=loads)),
                ('aaguid', fields.CharField(null=True, max_length=64)),
                ('attestation_format', fields.CharField(null=True, max_length=64)),
            ],
            options={'table': 'identity_webauthn_credential', 'app': 'wybra_auth', 'indexes': [Index(fields=['user_id', 'status'])], 'pk_attr': 'id', 'table_description': 'A WebAuthn public-key credential linked to a local account.'},
            bases=['Model'],
        ),
    ]
