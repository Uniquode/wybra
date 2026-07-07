from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, cast
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import ValidationError
from tortoise.backends.base.client import BaseDBAsyncClient
from tortoise.exceptions import IntegrityError

from wybra.auth.accounts.manager import (
    InvalidPasswordException,
    UserAlreadyExists,
    create_user_manager,
    public_password_failure_message,
)
from wybra.auth.accounts.schemas import UserCreate
from wybra.auth.authorisation import (
    effective_scope_sets_for_user,
    is_user_effectively_active,
)
from wybra.auth.delivery import IdentityDelivery
from wybra.auth.email_normalisation import normalise_email_target
from wybra.auth.emails import resolve_user_by_normalised_email
from wybra.auth.ids import parse_uuid
from wybra.auth.mfa.recovery import generate_recovery_codes
from wybra.auth.mfa.storage import (
    WEBAUTHN_ACTIVE_STATUS,
    WEBAUTHN_REVOKED_STATUS,
    TortoiseRecoveryCodeStore,
    TortoiseTOTPCredentialStore,
)
from wybra.auth.mfa.totp import generate_totp_secret, totp_auth_uri
from wybra.auth.models import (
    AccessToken,
    ExternalIdentityLink,
    Group,
    GroupGroup,
    GroupScope,
    GroupUser,
    IdentityAuthenticationChallenge,
    IdentityProvider,
    IdentityTotpCredential,
    IdentityTotpRecoveryCode,
    IdentityUserEmail,
    IdentityWebAuthnCredential,
    Scope,
    User,
)
from wybra.auth.options import IdentityOptions
from wybra.auth.persistence.transactions import auth_savepoint
from wybra.auth.result import (
    ERROR_ALREADY_EXISTS,
    ERROR_INVALID_EMAIL,
    ERROR_INVALID_PASSWORD,
    Result,
    ResultErrorType,
)
from wybra.auth.timestamps import current_timestamp
from wybra.services.crypto import SecretEnvelopeService

ERROR_INVALID_TIMEZONE = "invalid_timezone"
ERROR_NO_CHANGES = "no_changes"
ERROR_NOT_FOUND = "not_found"
ERROR_SUPERUSER_PROTECTED = "superuser_protected"
ERROR_FINAL_SUPERUSER = "final_superuser"
ERROR_INVALID_USER_ID = "invalid_user_id"
ERROR_NO_ACTIVE_TOTP = "no_active_totp"
ERROR_NO_ACTIVE_PASSKEY = "no_active_passkey"
ERROR_UNSUPPORTED_ORDER = "unsupported_order"
ERROR_INVALID_GROUP_ID = "invalid_group_id"
ERROR_GROUP_HAS_MEMBERSHIPS = "group_has_memberships"
ERROR_CYCLIC_GROUP_MEMBERSHIP = "cyclic_group_membership"
ERROR_SCOPE_IN_USE = "scope_in_use"
USER_TIMESTAMP_FIELDS: tuple[str, ...] = (
    "created_at",
    "modified_at",
    "last_login_at",
    "expires_at",
    "email_verification_sent_at",
)
USER_RECORD_FIELDS: tuple[str, ...] = (
    "id",
    "email",
    "is_active",
    "effective_active",
    "is_admin",
    "is_superuser",
    "is_verified",
    *USER_TIMESTAMP_FIELDS,
    "preferred_timezone",
)
SCOPE_RECORD_FIELDS: tuple[str, ...] = ("scope", "description")
GROUP_RECORD_FIELDS: tuple[str, ...] = (
    "id",
    "abbrev",
    "description",
    "scopes",
    "users",
    "child_groups",
    "parent_groups",
)
PASSKEY_RECORD_FIELDS: tuple[str, ...] = (
    "id",
    "credential_id",
    "status",
    "label",
    "created_at",
    "last_used_at",
    "revoked_at",
    "user_verified",
    "credential_device_type",
    "credential_backed_up",
    "transports",
    "aaguid",
    "attestation_format",
)
DEFAULT_MANAGEMENT_TOTP_ISSUER = "Wybra"


@dataclass(frozen=True, slots=True)
class TortoiseAuthManagementStore:
    """Tortoise-backed implementation of auth management workflows."""

    connection: BaseDBAsyncClient
    secret_service: SecretEnvelopeService | None = None

    async def resolve_user_record(self, target: str) -> Result[dict[str, Any]]:
        user, target_error = await resolve_user_target(self.connection, target)
        if target_error is not None:
            return Result.failure(target_error, target_error_message(target_error))
        if user is None:
            return Result.failure(ERROR_NOT_FOUND)
        return Result.ok(user_record(user))

    async def validate_group_targets(
        self,
        group_targets: tuple[str, ...],
    ) -> Result[dict[str, Any]]:
        return await _resolve_group_targets_for_set(self.connection, group_targets)

    async def update_user_groups(
        self,
        *,
        target: str,
        add_group_targets: tuple[str, ...] = (),
        remove_group_targets: tuple[str, ...] = (),
        set_group_targets: tuple[str, ...] = (),
    ) -> Result[dict[str, Any]]:
        user, target_error = await resolve_user_target(self.connection, target)
        if target_error is not None:
            return Result.failure(target_error, target_error_message(target_error))
        if user is None:
            return Result.failure(ERROR_NOT_FOUND, "No matching user was found.")

        if set_group_targets:
            replacement_group_result = await _resolve_group_targets_for_set(
                self.connection,
                set_group_targets,
            )
            if replacement_group_result.is_failure():
                return replacement_group_result
            replacement_group_ids = cast(
                list[UUID],
                (replacement_group_result.value or {}).get("group_ids", []),
            )

            await (
                GroupUser.filter(user_id=user.id)
                .using_db(self.connection)
                .select_for_update()
            )
            await GroupUser.filter(user_id=user.id).using_db(self.connection).delete()
            await GroupUser.bulk_create(
                [
                    GroupUser(group_id=group_id, user_id=user.id)
                    for group_id in dict.fromkeys(replacement_group_ids)
                ],
                using_db=self.connection,
            )

        for group_target in add_group_targets:
            result = await add_user_to_group_for_management(
                self.connection,
                group_target=group_target,
                user_target=target,
            )
            if result.is_failure():
                return result

        for group_target in remove_group_targets:
            result = await remove_user_from_group_for_management(
                self.connection,
                group_target=group_target,
                user_target=target,
            )
            if result.is_failure():
                return result

        return Result.ok(user_record(user))

    async def create_local_user(
        self,
        options: object,
        *,
        email: str,
        password: str,
        is_admin: bool = False,
        is_superuser: bool = False,
        is_verified: bool = True,
        preferred_timezone: str | None = None,
        expires_at: float | None = None,
    ) -> Result[dict[str, Any]]:
        return await create_local_user_for_management(
            self.connection,
            cast(IdentityOptions, options),
            email=email,
            password=password,
            is_admin=is_admin,
            is_superuser=is_superuser,
            is_verified=is_verified,
            preferred_timezone=preferred_timezone,
            expires_at=expires_at,
        )

    async def update_local_user(
        self,
        options: object,
        *,
        target: str,
        is_admin: bool | None = None,
        is_superuser: bool | None = None,
        is_verified: bool | None = None,
        password: str | None = None,
        revoke_sessions: bool = True,
        preferred_timezone: str | None = None,
        clear_preferred_timezone: bool = False,
        expires_at: float | None = None,
        clear_expires_at: bool = False,
    ) -> Result[dict[str, Any]]:
        return await update_local_user_for_management(
            self.connection,
            cast(IdentityOptions, options),
            target=target,
            is_admin=is_admin,
            is_superuser=is_superuser,
            is_verified=is_verified,
            password=password,
            revoke_sessions=revoke_sessions,
            preferred_timezone=preferred_timezone,
            clear_preferred_timezone=clear_preferred_timezone,
            expires_at=expires_at,
            clear_expires_at=clear_expires_at,
        )

    async def delete_local_user(self, *, target: str) -> Result[dict[str, Any]]:
        return await delete_local_user_for_management(self.connection, target=target)

    async def deactivate_local_user(self, *, target: str) -> Result[dict[str, Any]]:
        return await deactivate_local_user_for_management(
            self.connection,
            target=target,
        )

    async def list_local_users(
        self,
        *,
        email_pattern: str | None = None,
        domain_pattern: str | None = None,
        is_admin: bool | None = None,
        is_superuser: bool | None = None,
        effective_active: bool | None = None,
        is_verified: bool | None = None,
        since_created_at: float | None = None,
        before_created_at: float | None = None,
        since_modified_at: float | None = None,
        before_modified_at: float | None = None,
        since_last_login_at: float | None = None,
        before_last_login_at: float | None = None,
        never_logged_in: bool | None = None,
        order: str = "email",
        direction: str | None = None,
        include_passkeys: bool = False,
    ) -> Result[dict[str, Any]]:
        return await list_local_users_for_management(
            self.connection,
            email_pattern=email_pattern,
            domain_pattern=domain_pattern,
            is_admin=is_admin,
            is_superuser=is_superuser,
            effective_active=effective_active,
            is_verified=is_verified,
            since_created_at=since_created_at,
            before_created_at=before_created_at,
            since_modified_at=since_modified_at,
            before_modified_at=before_modified_at,
            since_last_login_at=since_last_login_at,
            before_last_login_at=before_last_login_at,
            never_logged_in=never_logged_in,
            order=order,
            direction=direction,
            include_passkeys=include_passkeys,
        )

    async def provision_totp(
        self,
        options: object,
        *,
        target: str,
    ) -> Result[dict[str, Any]]:
        user_result = await _resolve_user_for_management_operation(
            self.connection,
            target,
        )
        if user_result.is_failure():
            return user_result
        return await provision_totp_for_management(
            self.connection,
            cast(IdentityOptions, options),
            user=cast(User, user_result.value),
            secret_service=self.secret_service,
        )

    async def disable_totp(self, *, target: str) -> Result[dict[str, Any]]:
        user_result = await _resolve_user_for_management_operation(
            self.connection,
            target,
        )
        if user_result.is_failure():
            return user_result
        return await disable_totp_for_management(
            self.connection,
            user=cast(User, user_result.value),
            secret_service=self.secret_service,
        )

    async def rotate_totp_recovery_codes(
        self,
        *,
        target: str,
    ) -> Result[dict[str, Any]]:
        user_result = await _resolve_user_for_management_operation(
            self.connection,
            target,
        )
        if user_result.is_failure():
            return user_result
        return await rotate_totp_recovery_codes_for_management(
            self.connection,
            user=cast(User, user_result.value),
            secret_service=self.secret_service,
        )

    async def revoke_passkeys(
        self,
        *,
        target: str,
        credential: str | None = None,
    ) -> Result[dict[str, Any]]:
        user_result = await _resolve_user_for_management_operation(
            self.connection,
            target,
        )
        if user_result.is_failure():
            return user_result
        return await revoke_passkeys_for_management(
            self.connection,
            user=cast(User, user_result.value),
            credential=credential,
        )

    async def create_scope(
        self,
        *,
        scope: str,
        description: str | None = None,
    ) -> Result[dict[str, Any]]:
        return await create_scope_for_management(
            self.connection,
            scope=scope,
            description=description,
        )

    async def update_scope(
        self,
        *,
        scope: str,
        description: str | None = None,
    ) -> Result[dict[str, Any]]:
        return await update_scope_for_management(
            self.connection,
            scope=scope,
            description=description,
        )

    async def delete_scope(self, *, scope: str) -> Result[dict[str, Any]]:
        return await delete_scope_for_management(self.connection, scope=scope)

    async def list_scopes(self) -> Result[dict[str, Any]]:
        return await list_scopes_for_management(self.connection)

    async def create_group(
        self,
        *,
        abbrev: str,
        description: str,
    ) -> Result[dict[str, Any]]:
        return await create_group_for_management(
            self.connection,
            abbrev=abbrev,
            description=description,
        )

    async def update_group(
        self,
        *,
        target: str,
        description: str,
    ) -> Result[dict[str, Any]]:
        return await update_group_for_management(
            self.connection,
            target=target,
            description=description,
        )

    async def delete_group(self, *, target: str) -> Result[dict[str, Any]]:
        return await delete_group_for_management(self.connection, target=target)

    async def get_group(self, *, target: str) -> Result[dict[str, Any]]:
        return await get_group_for_management(self.connection, target=target)

    async def list_groups(self) -> Result[dict[str, Any]]:
        return await list_groups_for_management(self.connection)

    async def add_scope_to_group(
        self,
        *,
        group_target: str,
        scope: str,
    ) -> Result[dict[str, Any]]:
        return await add_scope_to_group_for_management(
            self.connection,
            group_target=group_target,
            scope=scope,
        )

    async def remove_scope_from_group(
        self,
        *,
        group_target: str,
        scope: str,
    ) -> Result[dict[str, Any]]:
        return await remove_scope_from_group_for_management(
            self.connection,
            group_target=group_target,
            scope=scope,
        )

    async def add_user_to_group(
        self,
        *,
        group_target: str,
        user_target: str,
    ) -> Result[dict[str, Any]]:
        return await add_user_to_group_for_management(
            self.connection,
            group_target=group_target,
            user_target=user_target,
        )

    async def remove_user_from_group(
        self,
        *,
        group_target: str,
        user_target: str,
    ) -> Result[dict[str, Any]]:
        return await remove_user_from_group_for_management(
            self.connection,
            group_target=group_target,
            user_target=user_target,
        )

    async def add_child_group_to_group(
        self,
        *,
        parent_target: str,
        child_target: str,
    ) -> Result[dict[str, Any]]:
        return await add_child_group_to_group_for_management(
            self.connection,
            parent_target=parent_target,
            child_target=child_target,
        )

    async def remove_child_group_from_group(
        self,
        *,
        parent_target: str,
        child_target: str,
    ) -> Result[dict[str, Any]]:
        return await remove_child_group_from_group_for_management(
            self.connection,
            parent_target=parent_target,
            child_target=child_target,
        )

    async def effective_scopes_for_user(
        self,
        *,
        user_target: str,
    ) -> Result[dict[str, Any]]:
        return await effective_scopes_for_user_for_management(
            self.connection,
            user_target=user_target,
        )


def user_record(user: User, *, now: float | None = None) -> dict[str, Any]:
    record = {
        "id": str(user.id),
        "email": user.email,
        "is_active": user.is_active,
        "effective_active": is_user_effectively_active(user, now=now),
        "is_admin": user.is_admin,
        "is_superuser": user.is_superuser,
        "is_verified": user.is_verified,
        "created_at": user.created_at,
        "modified_at": user.modified_at,
        "last_login_at": user.last_login_at,
        "expires_at": user.expires_at,
        "email_verification_sent_at": user.email_verification_sent_at,
        "preferred_timezone": user.preferred_timezone,
    }
    return {field_name: record.get(field_name) for field_name in USER_RECORD_FIELDS}


def scope_record(scope: Scope) -> dict[str, Any]:
    return {
        "scope": scope.scope,
        "description": scope.description,
    }


def passkey_record(credential: IdentityWebAuthnCredential) -> dict[str, Any]:
    record = {
        "id": str(credential.id),
        "credential_id": credential.credential_id,
        "status": credential.status,
        "label": credential.label,
        "created_at": credential.created_at,
        "last_used_at": credential.last_used_at,
        "revoked_at": credential.revoked_at,
        "user_verified": credential.user_verified,
        "credential_device_type": credential.credential_device_type,
        "credential_backed_up": credential.credential_backed_up,
        "transports": credential.transports,
        "aaguid": credential.aaguid,
        "attestation_format": credential.attestation_format,
    }
    return {
        field_name: record[field_name]
        for field_name in PASSKEY_RECORD_FIELDS
        if record.get(field_name) is not None
    }


def _group_record_from_parts(
    group: Group,
    *,
    scopes: list[str],
    users: list[str],
    child_groups: list[str],
    parent_groups: list[str],
) -> dict[str, Any]:
    record = {
        "id": str(group.id),
        "abbrev": group.abbrev,
        "description": group.description,
        "scopes": scopes,
        "users": users,
        "child_groups": child_groups,
        "parent_groups": parent_groups,
    }
    return {field_name: record.get(field_name) for field_name in GROUP_RECORD_FIELDS}


async def group_record(
    connection: BaseDBAsyncClient,
    group: Group,
) -> dict[str, Any]:
    scopes = cast(
        list[str],
        list(
            await GroupScope.filter(group_id=group.id)
            .using_db(connection)
            .order_by("scope")
            .values_list("scope", flat=True)
        ),
    )
    users = await _group_user_emails(connection, {group.id})
    child_groups = await _related_group_abbrevs(
        connection,
        source_ids={group.id},
        source_field="parent_group_id",
        related_field="child_group_id",
    )
    parent_groups = await _related_group_abbrevs(
        connection,
        source_ids={group.id},
        source_field="child_group_id",
        related_field="parent_group_id",
    )
    return _group_record_from_parts(
        group,
        scopes=scopes,
        users=users[group.id],
        child_groups=child_groups[group.id],
        parent_groups=parent_groups[group.id],
    )


async def group_records(
    connection: BaseDBAsyncClient,
    groups: list[Group],
) -> list[dict[str, Any]]:
    if not groups:
        return []

    group_ids = {group.id for group in groups}
    scopes_by_group: dict[UUID, list[str]] = defaultdict(list)
    scope_rows = cast(
        list[tuple[UUID, str]],
        await GroupScope.filter(group_id__in=group_ids)
        .using_db(
            connection,
        )
        .order_by("scope")
        .values_list("group_id", "scope"),
    )
    for group_id, scope in scope_rows:
        scopes_by_group[group_id].append(scope)

    users_by_group = await _group_user_emails(connection, group_ids)
    children_by_group = await _related_group_abbrevs(
        connection,
        source_ids=group_ids,
        source_field="parent_group_id",
        related_field="child_group_id",
    )
    parents_by_group = await _related_group_abbrevs(
        connection,
        source_ids=group_ids,
        source_field="child_group_id",
        related_field="parent_group_id",
    )

    return [
        _group_record_from_parts(
            group,
            scopes=scopes_by_group[group.id],
            users=users_by_group[group.id],
            child_groups=children_by_group[group.id],
            parent_groups=parents_by_group[group.id],
        )
        for group in groups
    ]


async def create_scope_for_management(
    connection: BaseDBAsyncClient,
    *,
    scope: str,
    description: str | None = None,
) -> Result[dict[str, Any]]:
    existing_scope = await Scope.get_or_none(scope=scope, using_db=connection)
    if existing_scope is not None:
        return Result.failure(ERROR_ALREADY_EXISTS, "Scope already exists.")

    try:
        async with auth_savepoint(connection) as savepoint:
            scope_record_model = await Scope.create(
                scope=scope,
                description=description,
                using_db=savepoint,
            )
    except IntegrityError:
        return Result.failure(ERROR_ALREADY_EXISTS, "Scope already exists.")
    return Result.ok(scope_record(scope_record_model))


async def update_scope_for_management(
    connection: BaseDBAsyncClient,
    *,
    scope: str,
    description: str | None = None,
) -> Result[dict[str, Any]]:
    scope_record_model = await Scope.get_or_none(scope=scope, using_db=connection)
    if scope_record_model is None:
        return Result.failure(ERROR_NOT_FOUND, "No matching scope was found.")

    cast(Any, scope_record_model).description = description
    await scope_record_model.save(using_db=connection)
    return Result.ok(scope_record(scope_record_model))


async def list_scopes_for_management(
    connection: BaseDBAsyncClient,
) -> Result[dict[str, Any]]:
    scope_records = await Scope.all().using_db(connection).order_by("scope")
    return Result.ok({"scopes": [scope_record(scope) for scope in scope_records]})


async def delete_scope_for_management(
    connection: BaseDBAsyncClient,
    *,
    scope: str,
) -> Result[dict[str, Any]]:
    scope_record_model = await Scope.get_or_none(scope=scope, using_db=connection)
    if scope_record_model is None:
        return Result.failure(ERROR_NOT_FOUND, "No matching scope was found.")

    assignment_count = await GroupScope.filter(scope=scope).using_db(connection).count()
    if assignment_count:
        return Result.failure(
            ERROR_SCOPE_IN_USE,
            "Scope is assigned to one or more groups.",
        )

    record = scope_record(scope_record_model)
    await scope_record_model.delete(using_db=connection)
    return Result.ok(record)


async def provision_totp_for_management(
    connection: BaseDBAsyncClient,
    identity_options: IdentityOptions,
    *,
    user: User,
    issuer: str = DEFAULT_MANAGEMENT_TOTP_ISSUER,
    secret_service: SecretEnvelopeService | None = None,
) -> Result[dict[str, Any]]:
    secret = generate_totp_secret()
    credential_store = TortoiseTOTPCredentialStore(connection, secret_service)
    credential_id = await credential_store.create_pending_totp_credential(
        str(user.id),
        secret,
    )
    # Activation is the replacement boundary: the store disables any existing
    # active credential for this user before marking the new credential active.
    await credential_store.activate_totp_credential(credential_id)

    recovery_codes = generate_recovery_codes()
    recovery_store = TortoiseRecoveryCodeStore(connection, secret_service)
    await recovery_store.replace_recovery_codes(
        str(user.id),
        credential_id,
        recovery_codes,
    )

    return Result.ok(
        {
            "user": user_record(user),
            "totp": {
                "secret": secret,
                "provisioning_uri": totp_auth_uri(
                    account_name=user.email,
                    secret=secret,
                    issuer=issuer,
                    period=identity_options.totp_period_seconds,
                ),
                "recovery_codes": recovery_codes,
            },
        }
    )


async def disable_totp_for_management(
    connection: BaseDBAsyncClient,
    *,
    user: User,
    secret_service: SecretEnvelopeService | None = None,
) -> Result[dict[str, Any]]:
    credential_store = TortoiseTOTPCredentialStore(connection, secret_service)
    active_credential_id = await credential_store.get_active_totp_credential(
        str(user.id)
    )
    if active_credential_id is None:
        return Result.failure(ERROR_NO_ACTIVE_TOTP, "User does not have active TOTP.")

    await credential_store.disable_totp_credential(active_credential_id)
    return Result.ok({"user": user_record(user)})


async def rotate_totp_recovery_codes_for_management(
    connection: BaseDBAsyncClient,
    *,
    user: User,
    secret_service: SecretEnvelopeService | None = None,
) -> Result[dict[str, Any]]:
    credential_store = TortoiseTOTPCredentialStore(connection, secret_service)
    active_credential_id = await credential_store.get_active_totp_credential(
        str(user.id)
    )
    if active_credential_id is None:
        return Result.failure(ERROR_NO_ACTIVE_TOTP, "User does not have active TOTP.")

    recovery_codes = generate_recovery_codes()
    recovery_store = TortoiseRecoveryCodeStore(connection, secret_service)
    await recovery_store.replace_recovery_codes(
        str(user.id),
        active_credential_id,
        recovery_codes,
    )
    return Result.ok(
        {
            "user": user_record(user),
            "totp": {
                "recovery_codes": recovery_codes,
            },
        }
    )


async def revoke_passkeys_for_management(
    connection: BaseDBAsyncClient,
    *,
    user: User,
    credential: str | None = None,
) -> Result[dict[str, Any]]:
    credentials = await _active_passkeys_for_revoke(
        connection,
        user=user,
        credential=credential,
    )
    if not credentials:
        return Result.failure(
            ERROR_NO_ACTIVE_PASSKEY,
            _no_active_passkey_message(credential),
        )

    revoked_at = current_timestamp()
    for webauthn_credential in credentials:
        webauthn_credential.status = WEBAUTHN_REVOKED_STATUS
        webauthn_credential.revoked_at = revoked_at
        await webauthn_credential.save(using_db=connection)

    return Result.ok(
        {
            "user": user_record(user),
            "passkeys": [passkey_record(passkey) for passkey in credentials],
        }
    )


async def _active_passkeys_for_revoke(
    connection: BaseDBAsyncClient,
    *,
    user: User,
    credential: str | None,
) -> list[IdentityWebAuthnCredential]:
    query = (
        IdentityWebAuthnCredential.filter(
            user_id=user.id,
            status=WEBAUTHN_ACTIVE_STATUS,
        )
        .using_db(connection)
        .select_for_update()
        .order_by("-created_at")
    )
    if credential is not None:
        credential_uuid = parse_uuid(credential)
        credentials = await query
        return [
            candidate
            for candidate in credentials
            if candidate.credential_id == credential or candidate.id == credential_uuid
        ]
    return list(await query)


def _no_active_passkey_message(credential: str | None) -> str:
    if credential is None:
        return "User does not have active passkeys."
    return "No matching active passkey was found for the user."


async def create_group_for_management(
    connection: BaseDBAsyncClient,
    *,
    abbrev: str,
    description: str,
) -> Result[dict[str, Any]]:
    existing_group = await Group.get_or_none(abbrev=abbrev, using_db=connection)
    if existing_group is not None:
        return Result.failure(
            ERROR_ALREADY_EXISTS, "Group abbreviation already exists."
        )

    try:
        async with auth_savepoint(connection) as savepoint:
            group = await Group.create(
                abbrev=abbrev,
                description=description,
                using_db=savepoint,
            )
    except IntegrityError:
        return Result.failure(
            ERROR_ALREADY_EXISTS, "Group abbreviation already exists."
        )
    return Result.ok(await group_record(connection, group))


async def resolve_group_target(
    connection: BaseDBAsyncClient,
    target: str,
) -> tuple[Group | None, ResultErrorType | None]:
    group = await Group.get_or_none(abbrev=target, using_db=connection)
    if group is not None:
        return group, None

    group_id = parse_uuid(target)
    if group_id is None:
        return None, ERROR_INVALID_GROUP_ID

    group = await Group.get_or_none(id=group_id, using_db=connection)
    return (group, None) if group is not None else (None, ERROR_NOT_FOUND)


async def get_group_for_management(
    connection: BaseDBAsyncClient,
    *,
    target: str,
) -> Result[dict[str, Any]]:
    group, target_error = await resolve_group_target(connection, target)
    if target_error is not None:
        return Result.failure(target_error, group_target_error_message(target_error))
    if group is None:
        return Result.failure(ERROR_NOT_FOUND, "No matching group was found.")

    return Result.ok(await group_record(connection, group))


async def update_group_for_management(
    connection: BaseDBAsyncClient,
    *,
    target: str,
    description: str,
) -> Result[dict[str, Any]]:
    group, target_error = await resolve_group_target(connection, target)
    if target_error is not None:
        return Result.failure(target_error, group_target_error_message(target_error))
    if group is None:
        return Result.failure(ERROR_NOT_FOUND, "No matching group was found.")

    group.description = description
    await group.save(using_db=connection)
    return Result.ok(await group_record(connection, group))


async def list_groups_for_management(
    connection: BaseDBAsyncClient,
) -> Result[dict[str, Any]]:
    groups = list(await Group.all().using_db(connection).order_by("abbrev"))
    return Result.ok({"groups": await group_records(connection, groups)})


async def delete_group_for_management(
    connection: BaseDBAsyncClient,
    *,
    target: str,
) -> Result[dict[str, Any]]:
    group, target_error = await resolve_group_target(connection, target)
    if target_error is not None:
        return Result.failure(target_error, group_target_error_message(target_error))
    if group is None:
        return Result.failure(ERROR_NOT_FOUND, "No matching group was found.")

    if await _group_has_memberships(connection, group):
        return Result.failure(
            ERROR_GROUP_HAS_MEMBERSHIPS,
            "Group still has user, child group, or parent group memberships.",
        )

    record = await group_record(connection, group)
    await GroupScope.filter(group_id=group.id).using_db(connection).delete()
    await group.delete(using_db=connection)
    return Result.ok(record)


async def add_scope_to_group_for_management(
    connection: BaseDBAsyncClient,
    *,
    group_target: str,
    scope: str,
) -> Result[dict[str, Any]]:
    group_result = await _resolve_group_result(connection, group_target)
    if group_result.is_failure():
        return group_result

    scope_record_model = await Scope.get_or_none(scope=scope, using_db=connection)
    if scope_record_model is None:
        return Result.failure(ERROR_NOT_FOUND, "No matching scope was found.")

    group = _group_from_result(group_result)
    existing = await GroupScope.get_or_none(
        group_id=group.id,
        scope=scope,
        using_db=connection,
    )
    if existing is not None:
        return Result.failure(ERROR_ALREADY_EXISTS, "Group already has scope.")

    try:
        async with auth_savepoint(connection) as savepoint:
            await GroupScope.create(
                group_id=group.id,
                scope=scope,
                using_db=savepoint,
            )
    except IntegrityError:
        return Result.failure(ERROR_ALREADY_EXISTS, "Group already has scope.")
    return Result.ok(await group_record(connection, group))


async def remove_scope_from_group_for_management(
    connection: BaseDBAsyncClient,
    *,
    group_target: str,
    scope: str,
) -> Result[dict[str, Any]]:
    group_result = await _resolve_group_result(connection, group_target)
    if group_result.is_failure():
        return group_result

    group = _group_from_result(group_result)
    existing = await GroupScope.get_or_none(
        group_id=group.id,
        scope=scope,
        using_db=connection,
    )
    if existing is None:
        return Result.failure(ERROR_NOT_FOUND, "Group scope assignment was not found.")

    await existing.delete(using_db=connection)
    return Result.ok(await group_record(connection, group))


async def add_user_to_group_for_management(
    connection: BaseDBAsyncClient,
    *,
    group_target: str,
    user_target: str,
) -> Result[dict[str, Any]]:
    group_result = await _resolve_group_result(connection, group_target)
    if group_result.is_failure():
        return group_result

    user, target_error = await resolve_user_target(connection, user_target)
    if target_error is not None:
        return Result.failure(target_error, target_error_message(target_error))
    if user is None:
        return Result.failure(ERROR_NOT_FOUND, "No matching user was found.")

    group = _group_from_result(group_result)
    existing = await GroupUser.get_or_none(
        group_id=group.id,
        user_id=user.id,
        using_db=connection,
    )
    if existing is not None:
        return Result.failure(ERROR_ALREADY_EXISTS, "User is already in group.")

    try:
        async with auth_savepoint(connection) as savepoint:
            await GroupUser.create(
                group_id=group.id,
                user_id=user.id,
                using_db=savepoint,
            )
    except IntegrityError:
        return Result.failure(ERROR_ALREADY_EXISTS, "User is already in group.")
    return Result.ok(await group_record(connection, group))


async def remove_user_from_group_for_management(
    connection: BaseDBAsyncClient,
    *,
    group_target: str,
    user_target: str,
) -> Result[dict[str, Any]]:
    group_result = await _resolve_group_result(connection, group_target)
    if group_result.is_failure():
        return group_result

    user, target_error = await resolve_user_target(connection, user_target)
    if target_error is not None:
        return Result.failure(target_error, target_error_message(target_error))
    if user is None:
        return Result.failure(ERROR_NOT_FOUND, "No matching user was found.")

    group = _group_from_result(group_result)
    existing = await GroupUser.get_or_none(
        group_id=group.id,
        user_id=user.id,
        using_db=connection,
    )
    if existing is None:
        return Result.failure(ERROR_NOT_FOUND, "User group membership was not found.")

    await existing.delete(using_db=connection)
    return Result.ok(await group_record(connection, group))


async def add_child_group_to_group_for_management(
    connection: BaseDBAsyncClient,
    *,
    parent_target: str,
    child_target: str,
) -> Result[dict[str, Any]]:
    parent_result = await _resolve_group_result(connection, parent_target)
    if parent_result.is_failure():
        return parent_result
    child_result = await _resolve_group_result(connection, child_target)
    if child_result.is_failure():
        return child_result

    parent = _group_from_result(parent_result)
    child = _group_from_result(child_result)
    if parent.id == child.id or await _group_reaches(connection, child.id, parent.id):
        return Result.failure(
            ERROR_CYCLIC_GROUP_MEMBERSHIP,
            "Nested group membership would create a cycle.",
        )

    existing = await GroupGroup.get_or_none(
        parent_group_id=parent.id,
        child_group_id=child.id,
        using_db=connection,
    )
    if existing is not None:
        return Result.failure(ERROR_ALREADY_EXISTS, "Child group is already assigned.")

    try:
        async with auth_savepoint(connection) as savepoint:
            await GroupGroup.create(
                parent_group_id=parent.id,
                child_group_id=child.id,
                using_db=savepoint,
            )
    except IntegrityError:
        return Result.failure(ERROR_ALREADY_EXISTS, "Child group is already assigned.")
    return Result.ok(await group_record(connection, parent))


async def remove_child_group_from_group_for_management(
    connection: BaseDBAsyncClient,
    *,
    parent_target: str,
    child_target: str,
) -> Result[dict[str, Any]]:
    parent_result = await _resolve_group_result(connection, parent_target)
    if parent_result.is_failure():
        return parent_result
    child_result = await _resolve_group_result(connection, child_target)
    if child_result.is_failure():
        return child_result

    parent = _group_from_result(parent_result)
    child = _group_from_result(child_result)
    existing = await GroupGroup.get_or_none(
        parent_group_id=parent.id,
        child_group_id=child.id,
        using_db=connection,
    )
    if existing is None:
        return Result.failure(ERROR_NOT_FOUND, "Nested group membership was not found.")

    await existing.delete(using_db=connection)
    return Result.ok(await group_record(connection, parent))


async def list_candidate_child_groups_for_management(
    connection: BaseDBAsyncClient,
    *,
    parent_target: str,
) -> Result[dict[str, Any]]:
    parent_result = await _resolve_group_result(connection, parent_target)
    if parent_result.is_failure():
        return parent_result

    parent = _group_from_result(parent_result)
    reachable_from_parent = await _reachable_group_ids(connection, parent.id)
    reachable_to_parent = await _group_ids_reaching(connection, parent.id)
    groups = list(await Group.all().using_db(connection).order_by("abbrev"))
    candidate_groups = [
        group
        for group in groups
        if group.id != parent.id
        and group.id not in reachable_from_parent
        and group.id not in reachable_to_parent
    ]
    candidates = await group_records(connection, candidate_groups)
    return Result.ok({"groups": candidates})


async def effective_scopes_for_user_for_management(
    connection: BaseDBAsyncClient,
    *,
    user_target: str,
) -> Result[dict[str, Any]]:
    user, target_error = await resolve_user_target(connection, user_target)
    if target_error is not None:
        return Result.failure(target_error, target_error_message(target_error))
    if user is None:
        return Result.failure(ERROR_NOT_FOUND, "No matching user was found.")

    scope_values, group_values = await effective_scope_sets_for_user(
        connection,
        user.id,
    )
    return Result.ok(
        {
            "user": user_record(user),
            "scopes": list(scope_values),
            "groups": list(group_values),
        }
    )


def group_target_error_message(error_type: ResultErrorType) -> str:
    if error_type == ERROR_INVALID_GROUP_ID:
        return "Group target must be a valid group ID or abbreviation."

    if error_type == ERROR_NOT_FOUND:
        return "Group not found."

    return "Group target is invalid."


async def create_local_user_for_management(
    connection: BaseDBAsyncClient,
    options: IdentityOptions,
    *,
    email: str,
    password: str,
    is_admin: bool = False,
    is_superuser: bool = False,
    is_verified: bool = True,
    preferred_timezone: str | None = None,
    expires_at: float | None = None,
    delivery: IdentityDelivery | None = None,
) -> Result[dict[str, Any]]:
    if preferred_timezone is not None and not _valid_timezone(preferred_timezone):
        return Result.failure(ERROR_INVALID_TIMEZONE)

    manager = create_user_manager(connection, options, delivery)
    try:
        user = await manager.create(
            UserCreate(
                email=email,
                password=password,
                is_superuser=is_superuser,
                is_verified=is_verified,
            ),
            safe=False,
        )
    except ValidationError:
        return Result.failure(ERROR_INVALID_EMAIL, "Email address is invalid.")
    except InvalidPasswordException as exc:
        return Result.failure(ERROR_INVALID_PASSWORD, _invalid_password_message(exc))
    except UserAlreadyExists:
        return Result.failure(ERROR_ALREADY_EXISTS, "User already exists.")

    user = cast(User, user)
    user.is_admin = is_admin
    user.preferred_timezone = preferred_timezone
    user.expires_at = expires_at
    user.modified_at = current_timestamp()
    await user.save(using_db=connection)
    return Result.ok(user_record(user))


async def list_local_users_for_management(
    connection: BaseDBAsyncClient,
    *,
    email_pattern: str | None = None,
    domain_pattern: str | None = None,
    is_admin: bool | None = None,
    is_superuser: bool | None = None,
    effective_active: bool | None = None,
    is_verified: bool | None = None,
    since_created_at: float | None = None,
    before_created_at: float | None = None,
    since_modified_at: float | None = None,
    before_modified_at: float | None = None,
    since_last_login_at: float | None = None,
    before_last_login_at: float | None = None,
    never_logged_in: bool | None = None,
    order: str = "email",
    direction: str | None = None,
    include_passkeys: bool = False,
) -> Result[dict[str, Any]]:
    now = current_timestamp()
    users = list(await User.all().using_db(connection))
    ordered_users = _sort_users(
        [
            user
            for user in users
            if _matches_user_filters(
                user,
                email_pattern=email_pattern,
                domain_pattern=domain_pattern,
                is_admin=is_admin,
                is_superuser=is_superuser,
                effective_active=effective_active,
                is_verified=is_verified,
                since_created_at=since_created_at,
                before_created_at=before_created_at,
                since_modified_at=since_modified_at,
                before_modified_at=before_modified_at,
                since_last_login_at=since_last_login_at,
                before_last_login_at=before_last_login_at,
                never_logged_in=never_logged_in,
                now=now,
            )
        ],
        order=order,
        direction=direction,
    )
    records = [user_record(user, now=now) for user in ordered_users]
    if include_passkeys:
        passkeys_by_user = await _active_passkeys_by_user(connection, ordered_users)
        for user, record in zip(ordered_users, records, strict=True):
            record["passkeys"] = passkeys_by_user.get(user.id, [])
    return Result.ok({"users": records})


async def _active_passkeys_by_user(
    connection: BaseDBAsyncClient,
    users: Sequence[User],
) -> dict[UUID, list[dict[str, Any]]]:
    user_ids = [user.id for user in users]
    if not user_ids:
        return {}

    credentials = (
        await IdentityWebAuthnCredential.filter(
            user_id__in=tuple(user_ids),
            status=WEBAUTHN_ACTIVE_STATUS,
        )
        .using_db(connection)
        .order_by("user_id", "-created_at")
    )
    passkeys_by_user: dict[UUID, list[dict[str, Any]]] = defaultdict(list)
    for credential in credentials:
        passkeys_by_user[credential.user_id].append(passkey_record(credential))
    return passkeys_by_user


async def resolve_user_target(
    connection: BaseDBAsyncClient,
    target: str,
) -> tuple[User | None, ResultErrorType | None]:
    if "@" in target:
        normalised_target = normalise_email_target(target)
        if normalised_target is None:
            return None, ERROR_INVALID_EMAIL

        return await resolve_user_by_normalised_email(
            connection,
            normalised_target,
        ), None

    user_id = parse_uuid(target)
    if user_id is None:
        return None, ERROR_INVALID_USER_ID

    return await User.get_or_none(id=user_id, using_db=connection), None


async def _resolve_user_for_management_operation(
    connection: BaseDBAsyncClient,
    target: str,
) -> Result[Any]:
    user, target_error = await resolve_user_target(connection, target)
    if target_error is not None:
        return Result.failure(target_error, target_error_message(target_error))
    if user is None:
        return Result.failure(ERROR_NOT_FOUND, "No matching user was found.")
    return Result.ok(user)


async def _resolve_group_targets_for_set(
    connection: BaseDBAsyncClient,
    group_targets: tuple[str, ...],
) -> Result[dict[str, Any]]:
    unique_targets = tuple(dict.fromkeys(group_targets))
    groups_by_abbrev = {
        group.abbrev: group
        for group in await Group.filter(abbrev__in=unique_targets).using_db(connection)
    }
    parsed_ids: dict[str, UUID] = {}
    invalid_targets: set[str] = set()
    for target in unique_targets:
        if target in groups_by_abbrev:
            continue
        group_id = parse_uuid(target)
        if group_id is None:
            invalid_targets.add(target)
        else:
            parsed_ids[target] = group_id

    groups_by_id: dict[UUID, Group] = {}
    if parsed_ids:
        groups_by_id = {
            group.id: group
            for group in await Group.filter(id__in=tuple(parsed_ids.values())).using_db(
                connection
            )
        }

    group_ids = []
    for target in group_targets:
        if target in groups_by_abbrev:
            group_ids.append(groups_by_abbrev[target].id)
            continue

        if target in invalid_targets:
            return Result.failure(
                ERROR_INVALID_GROUP_ID,
                group_target_error_message(ERROR_INVALID_GROUP_ID),
            )

        group_id = parsed_ids[target]
        group = groups_by_id.get(group_id)
        if group is None:
            return Result.failure(ERROR_NOT_FOUND, "No matching group was found.")
        group_ids.append(group.id)

    return Result.ok({"group_ids": group_ids})


def target_error_message(error_type: ResultErrorType) -> str:
    if error_type == ERROR_INVALID_EMAIL:
        return "User target email address is invalid."

    if error_type == ERROR_INVALID_USER_ID:
        return "User target must be an email address or valid user ID."

    return "User target is invalid."


async def update_local_user_for_management(
    connection: BaseDBAsyncClient,
    options: IdentityOptions,
    *,
    target: str,
    is_admin: bool | None = None,
    is_superuser: bool | None = None,
    is_verified: bool | None = None,
    password: str | None = None,
    revoke_sessions: bool = True,
    preferred_timezone: str | None = None,
    clear_preferred_timezone: bool = False,
    expires_at: float | None = None,
    clear_expires_at: bool = False,
    delivery: IdentityDelivery | None = None,
) -> Result[dict[str, Any]]:
    user, target_error = await resolve_user_target(connection, target)
    if target_error is not None:
        return Result.failure(target_error, target_error_message(target_error))
    if user is None:
        return Result.failure(ERROR_NOT_FOUND, "No matching user was found.")

    if preferred_timezone is not None and not _valid_timezone(preferred_timezone):
        return Result.failure(ERROR_INVALID_TIMEZONE)

    has_changes = False
    if is_admin is not None:
        user.is_admin = is_admin
        has_changes = True

    if is_verified is not None:
        user.is_verified = is_verified
        await (
            IdentityUserEmail.filter(
                user_id=user.id,
                is_primary=True,
            )
            .using_db(connection)
            .update(is_verified=is_verified)
        )
        has_changes = True

    if is_superuser is not None:
        if (
            not is_superuser
            and user.is_superuser
            and await _sole_superuser(connection, user)
        ):
            return Result.failure(
                ERROR_FINAL_SUPERUSER,
                "Cannot remove the final superuser flag.",
            )
        user.is_superuser = is_superuser
        has_changes = True

    if clear_preferred_timezone and user.preferred_timezone is not None:
        user.preferred_timezone = None
        has_changes = True
    elif preferred_timezone is not None:
        user.preferred_timezone = preferred_timezone
        has_changes = True

    if clear_expires_at and user.expires_at is not None:
        user.expires_at = None
        has_changes = True
    elif expires_at is not None:
        user.expires_at = expires_at
        has_changes = True

    if password is not None:
        manager = create_user_manager(connection, options, delivery)
        try:
            await manager.validate_password(password, user)
        except InvalidPasswordException as exc:
            return Result.failure(
                ERROR_INVALID_PASSWORD,
                _invalid_password_message(exc),
            )

        user.hashed_password = manager.password_helper.hash(password)
        if revoke_sessions:
            await _delete_user_sessions(connection, user)
        has_changes = True

    if not has_changes:
        return Result.failure(ERROR_NO_CHANGES, "No user changes were requested.")

    user.modified_at = current_timestamp()
    await user.save(using_db=connection)
    return Result.ok(user_record(user))


async def delete_local_user_for_management(
    connection: BaseDBAsyncClient,
    *,
    target: str,
) -> Result[dict[str, Any]]:
    user, target_error = await resolve_user_target(connection, target)
    if target_error is not None:
        return Result.failure(target_error, target_error_message(target_error))
    if user is None:
        return Result.failure(ERROR_NOT_FOUND, "No matching user was found.")

    if user.is_superuser:
        return Result.failure(
            ERROR_SUPERUSER_PROTECTED,
            "superuser accounts cannot be deleted.",
        )

    locked_user = (
        await User.filter(id=user.id).using_db(connection).select_for_update().first()
    )
    if locked_user is None:
        return Result.failure(ERROR_NOT_FOUND, "No matching user was found.")
    if locked_user.is_superuser:
        return Result.failure(
            ERROR_SUPERUSER_PROTECTED,
            "superuser accounts cannot be deleted.",
        )
    user = locked_user
    record = user_record(user)
    await _delete_user_persistence(connection, user)
    await user.delete(using_db=connection)
    return Result.ok(record)


async def deactivate_local_user_for_management(
    connection: BaseDBAsyncClient,
    *,
    target: str,
) -> Result[dict[str, Any]]:
    user, target_error = await resolve_user_target(connection, target)
    if target_error is not None:
        return Result.failure(target_error, target_error_message(target_error))
    if user is None:
        return Result.failure(ERROR_NOT_FOUND, "No matching user was found.")

    if user.is_superuser:
        return Result.failure(
            ERROR_SUPERUSER_PROTECTED,
            "superuser accounts cannot be deactivated.",
        )

    locked_user = (
        await User.filter(id=user.id).using_db(connection).select_for_update().first()
    )
    if locked_user is None:
        return Result.failure(ERROR_NOT_FOUND, "No matching user was found.")
    if locked_user.is_superuser:
        return Result.failure(
            ERROR_SUPERUSER_PROTECTED,
            "superuser accounts cannot be deactivated.",
        )
    user = locked_user
    user.is_active = False
    user.modified_at = current_timestamp()
    await _delete_user_sessions(connection, user)
    await user.save(using_db=connection)
    return Result.ok(user_record(user))


async def _sole_superuser(connection: BaseDBAsyncClient, user: User) -> bool:
    superuser_ids = (
        await User.filter(is_superuser=True)
        .using_db(connection)
        .select_for_update()
        .values_list("id", flat=True)
    )
    return bool(user.is_superuser and len(superuser_ids) == 1)


async def _delete_user_sessions(connection: BaseDBAsyncClient, user: User) -> None:
    await AccessToken.filter(user_id=user.id).using_db(connection).delete()


async def _delete_user_persistence(
    connection: BaseDBAsyncClient,
    user: User,
) -> None:
    await _delete_user_sessions(connection, user)
    await IdentityUserEmail.filter(user_id=user.id).using_db(connection).delete()
    await GroupUser.filter(user_id=user.id).using_db(connection).delete()
    await (
        IdentityAuthenticationChallenge.filter(user_id=user.id)
        .using_db(
            connection,
        )
        .delete()
    )
    await (
        IdentityWebAuthnCredential.filter(user_id=user.id)
        .using_db(
            connection,
        )
        .delete()
    )

    totp_credential_ids = tuple(
        await IdentityTotpCredential.filter(user_id=user.id)
        .using_db(connection)
        .values_list("id", flat=True)
    )
    if totp_credential_ids:
        await (
            IdentityTotpRecoveryCode.filter(
                credential_id__in=totp_credential_ids,
            )
            .using_db(connection)
            .delete()
        )
    await IdentityTotpCredential.filter(user_id=user.id).using_db(connection).delete()

    provider_ids = tuple(
        await ExternalIdentityLink.filter(user_id=user.id)
        .using_db(connection)
        .values_list("provider_id", flat=True)
    )
    await ExternalIdentityLink.filter(user_id=user.id).using_db(connection).delete()
    if provider_ids:
        await IdentityProvider.filter(id__in=provider_ids).using_db(connection).delete()


async def _resolve_group_result(
    connection: BaseDBAsyncClient,
    target: str,
) -> Result[Any]:
    group, target_error = await resolve_group_target(connection, target)
    if target_error is not None:
        return Result.failure(target_error, group_target_error_message(target_error))
    if group is None:
        return Result.failure(ERROR_NOT_FOUND, "No matching group was found.")
    return Result.ok(group)


def _group_from_result(result: Result[Any]) -> Group:
    return cast(Group, result.value)


async def _group_user_emails(
    connection: BaseDBAsyncClient,
    group_ids: set[UUID],
) -> dict[UUID, list[str]]:
    users_by_group: dict[UUID, list[str]] = defaultdict(list)
    if not group_ids:
        return users_by_group

    user_rows = cast(
        list[tuple[UUID, UUID]],
        await GroupUser.filter(group_id__in=group_ids)
        .using_db(
            connection,
        )
        .values_list("group_id", "user_id"),
    )
    users_by_id = {
        user.id: user
        for user in await User.filter(
            id__in=tuple(user_id for _group_id, user_id in user_rows),
        ).using_db(connection)
    }
    for group_id, user_id in sorted(
        user_rows,
        key=lambda row: users_by_id[row[1]].email if row[1] in users_by_id else "",
    ):
        user = users_by_id.get(user_id)
        if user is not None:
            users_by_group[group_id].append(user.email)
    return users_by_group


async def _related_group_abbrevs(
    connection: BaseDBAsyncClient,
    *,
    source_ids: set[UUID],
    source_field: str,
    related_field: str,
) -> dict[UUID, list[str]]:
    related_by_source: dict[UUID, list[str]] = defaultdict(list)
    if not source_ids:
        return related_by_source

    filters = {f"{source_field}__in": source_ids}
    relation_rows = cast(
        list[tuple[UUID, UUID]],
        await GroupGroup.filter(**filters)
        .using_db(connection)
        .values_list(
            source_field,
            related_field,
        ),
    )
    groups_by_id = {
        group.id: group
        for group in await Group.filter(
            id__in=tuple(related_id for _source_id, related_id in relation_rows),
        ).using_db(connection)
    }
    for source_id, related_id in sorted(
        relation_rows,
        key=lambda row: groups_by_id[row[1]].abbrev if row[1] in groups_by_id else "",
    ):
        group = groups_by_id.get(related_id)
        if group is not None:
            related_by_source[source_id].append(group.abbrev)
    return related_by_source


async def _group_has_memberships(
    connection: BaseDBAsyncClient,
    group: Group,
) -> bool:
    return bool(
        await GroupUser.filter(group_id=group.id).using_db(connection).exists()
        or await GroupGroup.filter(parent_group_id=group.id)
        .using_db(connection)
        .exists()
        or await GroupGroup.filter(child_group_id=group.id)
        .using_db(connection)
        .exists()
    )


async def _group_reaches(
    connection: BaseDBAsyncClient,
    start_group_id: UUID,
    target_group_id: UUID,
) -> bool:
    return target_group_id in await _reachable_group_ids(connection, start_group_id)


async def _reachable_group_ids(
    connection: BaseDBAsyncClient,
    start_group_id: UUID,
) -> set[UUID]:
    visited: set[UUID] = set()
    pending = {start_group_id}
    while pending:
        current_ids = pending - visited
        if not current_ids:
            break
        visited.update(current_ids)
        child_ids = cast(
            set[UUID],
            set(
                await GroupGroup.filter(parent_group_id__in=current_ids)
                .using_db(connection)
                .values_list("child_group_id", flat=True)
            ),
        )
        pending = child_ids - visited

    visited.discard(start_group_id)
    return visited


async def _group_ids_reaching(
    connection: BaseDBAsyncClient,
    target_group_id: UUID,
) -> set[UUID]:
    visited: set[UUID] = set()
    pending = {target_group_id}
    while pending:
        current_ids = pending - visited
        if not current_ids:
            break
        visited.update(current_ids)
        parent_ids = cast(
            set[UUID],
            set(
                await GroupGroup.filter(child_group_id__in=current_ids)
                .using_db(connection)
                .values_list("parent_group_id", flat=True)
            ),
        )
        pending = parent_ids - visited

    visited.discard(target_group_id)
    return visited


def _matches_user_filters(
    user: User,
    *,
    email_pattern: str | None,
    domain_pattern: str | None,
    is_admin: bool | None,
    is_superuser: bool | None,
    effective_active: bool | None,
    is_verified: bool | None,
    since_created_at: float | None,
    before_created_at: float | None,
    since_modified_at: float | None,
    before_modified_at: float | None,
    since_last_login_at: float | None,
    before_last_login_at: float | None,
    never_logged_in: bool | None,
    now: float,
) -> bool:
    if email_pattern is not None and not _wildcard_matches(
        user.email.lower(),
        email_pattern.lower(),
    ):
        return False

    if domain_pattern is not None and not _wildcard_matches(
        user.email.lower(),
        f"*@{domain_pattern}".lower(),
    ):
        return False

    if is_admin is not None and user.is_admin is not is_admin:
        return False

    if is_superuser is not None and user.is_superuser is not is_superuser:
        return False

    if effective_active is not None and (
        is_user_effectively_active(user, now=now) is not effective_active
    ):
        return False

    if is_verified is not None and user.is_verified is not is_verified:
        return False

    if not _timestamp_in_range(user.created_at, since_created_at, before_created_at):
        return False

    if not _timestamp_in_range(user.modified_at, since_modified_at, before_modified_at):
        return False

    if never_logged_in is True:
        return user.last_login_at is None

    if never_logged_in is False and user.last_login_at is None:
        return False

    return _timestamp_in_range(
        user.last_login_at,
        since_last_login_at,
        before_last_login_at,
    )


def _timestamp_in_range(
    value: float | None,
    since_value: float | None,
    before_value: float | None,
) -> bool:
    if since_value is not None and (value is None or value < since_value):
        return False

    if before_value is not None and (value is None or value >= before_value):
        return False

    return True


def _sort_users(
    users: list[User],
    *,
    order: str,
    direction: str | None,
) -> list[User]:
    reverse = _reverse_order(order, direction)

    def key(user: User) -> tuple[object, ...]:
        match order:
            case "email-domain":
                return (_email_domain(user.email), user.email)
            case "created-at":
                return (user.created_at, user.email)
            case "modified-at":
                return (user.modified_at, user.email)
            case "last-login-at":
                return (
                    _nullable_timestamp_sort_key(user.last_login_at, reverse),
                    user.email,
                )
            case _:
                return (user.email,)

    return sorted(users, key=key, reverse=reverse)


def _email_domain(email: str) -> str:
    _local, separator, domain = email.partition("@")
    return domain.lower() if separator else ""


def _nullable_timestamp_sort_key(value: float | None, reverse: bool) -> float:
    if value is not None:
        return value
    return float("-inf") if reverse else float("inf")


def _wildcard_matches(value: str, pattern: str) -> bool:
    return re.fullmatch(_wildcard_pattern_regex(pattern), value) is not None


def _wildcard_pattern_regex(pattern: str) -> str:
    escaped_chars: list[str] = []
    index = 0
    length = len(pattern)
    while index < length:
        char = pattern[index]
        if char == "\\" and index + 1 < length:
            next_char = pattern[index + 1]
            if next_char == "*":
                escaped_chars.append(re.escape("*"))
                index += 2
                continue
            if next_char in {"%", "_", "\\"}:
                escaped_chars.append(re.escape(next_char))
                index += 2
                continue

            escaped_chars.append(re.escape("\\"))
            index += 1
            continue

        if char == "*":
            escaped_chars.append(".*")
        else:
            escaped_chars.append(re.escape(char))

        index += 1

    return "".join(escaped_chars)


def _reverse_order(order: str, direction: str | None) -> bool:
    if direction is not None:
        return direction == "desc"

    return order in {"created-at", "modified-at", "last-login-at"}


def _invalid_password_message(exc: InvalidPasswordException) -> str:
    return public_password_failure_message(exc)


def _valid_timezone(value: str) -> bool:
    try:
        ZoneInfo(value)
    except (ValueError, ZoneInfoNotFoundError):
        return False

    return True
