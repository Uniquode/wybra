from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from typing import Any, cast
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi_users.exceptions import (
    InvalidPasswordException,
    UserAlreadyExists,
)
from pydantic import ValidationError
from sqlalchemy import Select, delete, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from wybra.auth.accounts.manager import (
    create_user_manager,
    public_password_failure_message,
)
from wybra.auth.accounts.schemas import UserCreate
from wybra.auth.authorisation import (
    effective_scope_sets_for_user,
    is_user_effectively_active,
)
from wybra.auth.delivery import IdentityDelivery
from wybra.auth.emails import (
    normalise_email_target,
    resolve_user_by_normalised_email,
)
from wybra.auth.ids import parse_uuid
from wybra.auth.mfa.recovery import generate_recovery_codes
from wybra.auth.mfa.storage import (
    WEBAUTHN_ACTIVE_STATUS,
    WEBAUTHN_REVOKED_STATUS,
    SqlAlchemyRecoveryCodeStore,
    SqlAlchemyTOTPCredentialStore,
)
from wybra.auth.mfa.totp import generate_totp_secret, totp_auth_uri
from wybra.auth.models import (
    AccessToken,
    Group,
    GroupGroup,
    GroupScope,
    GroupUser,
    IdentityWebAuthnCredential,
    Scope,
    User,
)
from wybra.auth.options import IdentityOptions
from wybra.auth.result import (
    ERROR_ALREADY_EXISTS,
    ERROR_INVALID_EMAIL,
    ERROR_INVALID_PASSWORD,
    Result,
    ResultErrorType,
)
from wybra.auth.timestamps import current_timestamp

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
EMAIL_DOMAIN_ORDER_DIALECTS = frozenset({"postgresql", "sqlite"})
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


async def group_record(session: AsyncSession, group: Group) -> dict[str, Any]:
    scopes = (
        (
            await session.execute(
                select(GroupScope.scope)
                .where(GroupScope.group_id == group.id)
                .order_by(GroupScope.scope)
            )
        )
        .scalars()
        .all()
    )
    users = (
        (
            await session.execute(
                select(User.__table__.c.email)
                .join(GroupUser, GroupUser.user_id == User.id)
                .where(GroupUser.group_id == group.id)
                .order_by(User.email)
            )
        )
        .scalars()
        .all()
    )
    child_groups = await _related_group_abbrevs(
        session,
        GroupGroup.child_group_id,
        GroupGroup.parent_group_id == group.id,
    )
    parent_groups = await _related_group_abbrevs(
        session,
        GroupGroup.parent_group_id,
        GroupGroup.child_group_id == group.id,
    )
    return _group_record_from_parts(
        group,
        scopes=list(scopes),
        users=list(users),
        child_groups=child_groups,
        parent_groups=parent_groups,
    )


async def group_records(
    session: AsyncSession, groups: list[Group]
) -> list[dict[str, Any]]:
    if not groups:
        return []

    group_ids = [group.id for group in groups]
    scopes_by_group: dict[UUID, list[str]] = defaultdict(list)
    users_by_group: dict[UUID, list[str]] = defaultdict(list)
    children_by_group: dict[UUID, list[str]] = defaultdict(list)
    parents_by_group: dict[UUID, list[str]] = defaultdict(list)

    scope_rows = (
        await session.execute(
            select(GroupScope.group_id, GroupScope.scope)
            .where(GroupScope.group_id.in_(group_ids))
            .order_by(GroupScope.scope)
        )
    ).all()
    for group_id, scope in scope_rows:
        scopes_by_group[group_id].append(scope)

    user_rows = (
        await session.execute(
            select(GroupUser.group_id, User.__table__.c.email)
            .join(User, GroupUser.user_id == User.id)
            .where(GroupUser.group_id.in_(group_ids))
            .order_by(User.email)
        )
    ).all()
    for group_id, email in user_rows:
        users_by_group[group_id].append(email)

    child_rows = (
        await session.execute(
            select(GroupGroup.parent_group_id, Group.abbrev)
            .join(Group, Group.id == GroupGroup.child_group_id)
            .where(GroupGroup.parent_group_id.in_(group_ids))
            .order_by(Group.abbrev)
        )
    ).all()
    for group_id, abbrev in child_rows:
        children_by_group[group_id].append(abbrev)

    parent_rows = (
        await session.execute(
            select(GroupGroup.child_group_id, Group.abbrev)
            .join(Group, Group.id == GroupGroup.parent_group_id)
            .where(GroupGroup.child_group_id.in_(group_ids))
            .order_by(Group.abbrev)
        )
    ).all()
    for group_id, abbrev in parent_rows:
        parents_by_group[group_id].append(abbrev)

    records = []
    for group in groups:
        records.append(
            _group_record_from_parts(
                group,
                scopes=scopes_by_group[group.id],
                users=users_by_group[group.id],
                child_groups=children_by_group[group.id],
                parent_groups=parents_by_group[group.id],
            )
        )

    return records


async def create_scope_for_management(
    session: AsyncSession,
    *,
    scope: str,
    description: str | None = None,
) -> Result[dict[str, Any]]:
    existing_scope = await session.get(Scope, scope)
    if existing_scope is not None:
        return Result.failure(ERROR_ALREADY_EXISTS, "Scope already exists.")

    scope_record_model = Scope(scope=scope, description=description)
    session.add(scope_record_model)
    await session.commit()
    await session.refresh(scope_record_model)
    return Result.ok(scope_record(scope_record_model))


async def update_scope_for_management(
    session: AsyncSession,
    *,
    scope: str,
    description: str | None = None,
) -> Result[dict[str, Any]]:
    scope_record_model = await session.get(Scope, scope)
    if scope_record_model is None:
        return Result.failure(ERROR_NOT_FOUND, "No matching scope was found.")

    scope_record_model.description = description
    await session.commit()
    await session.refresh(scope_record_model)
    return Result.ok(scope_record(scope_record_model))


async def list_scopes_for_management(session: AsyncSession) -> Result[dict[str, Any]]:
    scope_records = (
        (await session.execute(select(Scope).order_by(Scope.scope))).scalars().all()
    )
    return Result.ok({"scopes": [scope_record(scope) for scope in scope_records]})


async def delete_scope_for_management(
    session: AsyncSession,
    *,
    scope: str,
) -> Result[dict[str, Any]]:
    scope_record_model = await session.get(Scope, scope)
    if scope_record_model is None:
        return Result.failure(ERROR_NOT_FOUND, "No matching scope was found.")

    assignment_count = await session.scalar(
        select(func.count()).select_from(GroupScope).where(GroupScope.scope == scope)
    )
    if assignment_count:
        return Result.failure(
            ERROR_SCOPE_IN_USE,
            "Scope is assigned to one or more groups.",
        )

    record = scope_record(scope_record_model)
    await session.delete(scope_record_model)
    await session.commit()
    return Result.ok(record)


async def provision_totp_for_management(
    session: AsyncSession,
    identity_options: IdentityOptions,
    *,
    user: User,
    issuer: str = DEFAULT_MANAGEMENT_TOTP_ISSUER,
) -> Result[dict[str, Any]]:
    secret = generate_totp_secret()
    credential_store = SqlAlchemyTOTPCredentialStore(session)
    credential_id = await credential_store.create_pending_totp_credential(
        str(user.id),
        secret,
    )
    # Activation is the replacement boundary: the store disables any existing
    # active credential for this user before marking the new credential active.
    await credential_store.activate_totp_credential(credential_id)

    recovery_codes = generate_recovery_codes()
    recovery_store = SqlAlchemyRecoveryCodeStore(session)
    await recovery_store.replace_recovery_codes(
        str(user.id),
        credential_id,
        recovery_codes,
    )
    await session.commit()

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
    session: AsyncSession,
    *,
    user: User,
) -> Result[dict[str, Any]]:
    credential_store = SqlAlchemyTOTPCredentialStore(session)
    active_credential_id = await credential_store.get_active_totp_credential(
        str(user.id)
    )
    if active_credential_id is None:
        return Result.failure(ERROR_NO_ACTIVE_TOTP, "User does not have active TOTP.")

    await credential_store.disable_totp_credential(active_credential_id)
    await session.commit()
    return Result.ok({"user": user_record(user)})


async def rotate_totp_recovery_codes_for_management(
    session: AsyncSession,
    *,
    user: User,
) -> Result[dict[str, Any]]:
    credential_store = SqlAlchemyTOTPCredentialStore(session)
    active_credential_id = await credential_store.get_active_totp_credential(
        str(user.id)
    )
    if active_credential_id is None:
        return Result.failure(ERROR_NO_ACTIVE_TOTP, "User does not have active TOTP.")

    recovery_codes = generate_recovery_codes()
    recovery_store = SqlAlchemyRecoveryCodeStore(session)
    await recovery_store.replace_recovery_codes(
        str(user.id),
        active_credential_id,
        recovery_codes,
    )
    await session.commit()
    return Result.ok(
        {
            "user": user_record(user),
            "totp": {
                "recovery_codes": recovery_codes,
            },
        }
    )


async def revoke_passkeys_for_management(
    session: AsyncSession,
    *,
    user: User,
    credential: str | None = None,
) -> Result[dict[str, Any]]:
    credentials = await _active_passkeys_for_revoke(
        session,
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

    await session.commit()
    return Result.ok(
        {
            "user": user_record(user),
            "passkeys": [passkey_record(passkey) for passkey in credentials],
        }
    )


async def _active_passkeys_for_revoke(
    session: AsyncSession,
    *,
    user: User,
    credential: str | None,
) -> list[IdentityWebAuthnCredential]:
    query = (
        select(IdentityWebAuthnCredential)
        .where(
            IdentityWebAuthnCredential.user_id == user.id,
            IdentityWebAuthnCredential.status == WEBAUTHN_ACTIVE_STATUS,
        )
        .order_by(IdentityWebAuthnCredential.created_at.desc())
        .with_for_update()
    )
    if credential is not None:
        credential_uuid = parse_uuid(credential)
        credential_match = IdentityWebAuthnCredential.credential_id == credential
        if credential_uuid is not None:
            query = query.where(
                or_(IdentityWebAuthnCredential.id == credential_uuid, credential_match)
            )
        else:
            query = query.where(credential_match)

    return list((await session.execute(query)).scalars().all())


def _no_active_passkey_message(credential: str | None) -> str:
    if credential is None:
        return "User does not have active passkeys."
    return "No matching active passkey was found for the user."


async def create_group_for_management(
    session: AsyncSession,
    *,
    abbrev: str,
    description: str,
) -> Result[dict[str, Any]]:
    existing_group = (
        await session.execute(select(Group).where(Group.abbrev == abbrev))
    ).scalar_one_or_none()
    if existing_group is not None:
        return Result.failure(
            ERROR_ALREADY_EXISTS, "Group abbreviation already exists."
        )

    group = Group(abbrev=abbrev, description=description)
    session.add(group)
    await session.commit()
    await session.refresh(group)
    return Result.ok(await group_record(session, group))


async def resolve_group_target(
    session: AsyncSession,
    target: str,
) -> tuple[Group | None, ResultErrorType | None]:
    group = (
        await session.execute(select(Group).where(Group.abbrev == target))
    ).scalar_one_or_none()
    if group is not None:
        return group, None

    try:
        group_id = UUID(target)
    except ValueError:
        return None, ERROR_INVALID_GROUP_ID

    group = await session.get(Group, group_id)
    return (group, None) if group is not None else (None, ERROR_NOT_FOUND)


async def get_group_for_management(
    session: AsyncSession,
    *,
    target: str,
) -> Result[dict[str, Any]]:
    group, target_error = await resolve_group_target(session, target)
    if target_error is not None:
        return Result.failure(target_error, group_target_error_message(target_error))
    if group is None:
        return Result.failure(ERROR_NOT_FOUND, "No matching group was found.")

    return Result.ok(await group_record(session, group))


async def update_group_for_management(
    session: AsyncSession,
    *,
    target: str,
    description: str,
) -> Result[dict[str, Any]]:
    group, target_error = await resolve_group_target(session, target)
    if target_error is not None:
        return Result.failure(target_error, group_target_error_message(target_error))
    if group is None:
        return Result.failure(ERROR_NOT_FOUND, "No matching group was found.")

    group.description = description
    await session.commit()
    await session.refresh(group)
    return Result.ok(await group_record(session, group))


async def list_groups_for_management(session: AsyncSession) -> Result[dict[str, Any]]:
    groups = (
        (await session.execute(select(Group).order_by(Group.abbrev))).scalars().all()
    )
    return Result.ok({"groups": await group_records(session, list(groups))})


async def delete_group_for_management(
    session: AsyncSession,
    *,
    target: str,
) -> Result[dict[str, Any]]:
    group, target_error = await resolve_group_target(session, target)
    if target_error is not None:
        return Result.failure(target_error, group_target_error_message(target_error))
    if group is None:
        return Result.failure(ERROR_NOT_FOUND, "No matching group was found.")

    if await _group_has_memberships(session, group):
        return Result.failure(
            ERROR_GROUP_HAS_MEMBERSHIPS,
            "Group still has user, child group, or parent group memberships.",
        )

    record = await group_record(session, group)
    await session.execute(delete(GroupScope).where(GroupScope.group_id == group.id))
    await session.delete(group)
    await session.commit()
    return Result.ok(record)


async def add_scope_to_group_for_management(
    session: AsyncSession,
    *,
    group_target: str,
    scope: str,
) -> Result[dict[str, Any]]:
    group_result = await _resolve_group_result(session, group_target)
    if group_result.is_failure():
        return group_result

    scope_record_model = await session.get(Scope, scope)
    if scope_record_model is None:
        return Result.failure(ERROR_NOT_FOUND, "No matching scope was found.")

    group = _group_from_result(group_result)
    existing = (
        await session.execute(
            select(GroupScope).where(
                GroupScope.group_id == group.id,
                GroupScope.scope == scope,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return Result.failure(ERROR_ALREADY_EXISTS, "Group already has scope.")

    session.add(GroupScope(group_id=group.id, scope=scope))
    await session.commit()
    return Result.ok(await group_record(session, group))


async def remove_scope_from_group_for_management(
    session: AsyncSession,
    *,
    group_target: str,
    scope: str,
) -> Result[dict[str, Any]]:
    group_result = await _resolve_group_result(session, group_target)
    if group_result.is_failure():
        return group_result

    group = _group_from_result(group_result)
    existing = (
        await session.execute(
            select(GroupScope).where(
                GroupScope.group_id == group.id,
                GroupScope.scope == scope,
            )
        )
    ).scalar_one_or_none()
    if existing is None:
        return Result.failure(ERROR_NOT_FOUND, "Group scope assignment was not found.")

    await session.execute(
        delete(GroupScope).where(
            GroupScope.group_id == group.id,
            GroupScope.scope == scope,
        )
    )

    await session.commit()
    return Result.ok(await group_record(session, group))


async def add_user_to_group_for_management(
    session: AsyncSession,
    *,
    group_target: str,
    user_target: str,
) -> Result[dict[str, Any]]:
    group_result = await _resolve_group_result(session, group_target)
    if group_result.is_failure():
        return group_result

    user, target_error = await resolve_user_target(session, user_target)
    if target_error is not None:
        return Result.failure(target_error, target_error_message(target_error))
    if user is None:
        return Result.failure(ERROR_NOT_FOUND, "No matching user was found.")

    group = _group_from_result(group_result)
    existing = (
        await session.execute(
            select(GroupUser).where(
                GroupUser.group_id == group.id,
                GroupUser.user_id == user.id,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return Result.failure(ERROR_ALREADY_EXISTS, "User is already in group.")

    session.add(GroupUser(group_id=group.id, user_id=user.id))
    await session.commit()
    return Result.ok(await group_record(session, group))


async def remove_user_from_group_for_management(
    session: AsyncSession,
    *,
    group_target: str,
    user_target: str,
) -> Result[dict[str, Any]]:
    group_result = await _resolve_group_result(session, group_target)
    if group_result.is_failure():
        return group_result

    user, target_error = await resolve_user_target(session, user_target)
    if target_error is not None:
        return Result.failure(target_error, target_error_message(target_error))
    if user is None:
        return Result.failure(ERROR_NOT_FOUND, "No matching user was found.")

    group = _group_from_result(group_result)
    existing = (
        await session.execute(
            select(GroupUser).where(
                GroupUser.group_id == group.id,
                GroupUser.user_id == user.id,
            )
        )
    ).scalar_one_or_none()
    if existing is None:
        return Result.failure(ERROR_NOT_FOUND, "User group membership was not found.")

    await session.execute(
        delete(GroupUser).where(
            GroupUser.group_id == group.id,
            GroupUser.user_id == user.id,
        )
    )

    await session.commit()
    return Result.ok(await group_record(session, group))


async def add_child_group_to_group_for_management(
    session: AsyncSession,
    *,
    parent_target: str,
    child_target: str,
) -> Result[dict[str, Any]]:
    parent_result = await _resolve_group_result(session, parent_target)
    if parent_result.is_failure():
        return parent_result
    child_result = await _resolve_group_result(session, child_target)
    if child_result.is_failure():
        return child_result

    parent = _group_from_result(parent_result)
    child = _group_from_result(child_result)
    if parent.id == child.id or await _group_reaches(session, child.id, parent.id):
        return Result.failure(
            ERROR_CYCLIC_GROUP_MEMBERSHIP,
            "Nested group membership would create a cycle.",
        )

    existing = (
        await session.execute(
            select(GroupGroup).where(
                GroupGroup.parent_group_id == parent.id,
                GroupGroup.child_group_id == child.id,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return Result.failure(ERROR_ALREADY_EXISTS, "Child group is already assigned.")

    session.add(GroupGroup(parent_group_id=parent.id, child_group_id=child.id))
    await session.commit()
    return Result.ok(await group_record(session, parent))


async def remove_child_group_from_group_for_management(
    session: AsyncSession,
    *,
    parent_target: str,
    child_target: str,
) -> Result[dict[str, Any]]:
    parent_result = await _resolve_group_result(session, parent_target)
    if parent_result.is_failure():
        return parent_result
    child_result = await _resolve_group_result(session, child_target)
    if child_result.is_failure():
        return child_result

    parent = _group_from_result(parent_result)
    child = _group_from_result(child_result)
    existing = (
        await session.execute(
            select(GroupGroup).where(
                GroupGroup.parent_group_id == parent.id,
                GroupGroup.child_group_id == child.id,
            )
        )
    ).scalar_one_or_none()
    if existing is None:
        return Result.failure(ERROR_NOT_FOUND, "Nested group membership was not found.")

    await session.execute(
        delete(GroupGroup).where(
            GroupGroup.parent_group_id == parent.id,
            GroupGroup.child_group_id == child.id,
        )
    )

    await session.commit()
    return Result.ok(await group_record(session, parent))


async def list_candidate_child_groups_for_management(
    session: AsyncSession,
    *,
    parent_target: str,
) -> Result[dict[str, Any]]:
    parent_result = await _resolve_group_result(session, parent_target)
    if parent_result.is_failure():
        return parent_result

    parent = _group_from_result(parent_result)
    reachable_from_parent = await _reachable_group_ids(session, parent.id)
    reachable_to_parent = await _group_ids_reaching(session, parent.id)
    groups = (
        (await session.execute(select(Group).order_by(Group.abbrev))).scalars().all()
    )
    candidate_groups = [
        group
        for group in groups
        if group.id != parent.id
        and group.id not in reachable_from_parent
        and group.id not in reachable_to_parent
    ]
    candidates = await group_records(session, list(candidate_groups))
    return Result.ok({"groups": candidates})


async def effective_scopes_for_user_for_management(
    session: AsyncSession,
    *,
    user_target: str,
) -> Result[dict[str, Any]]:
    user, target_error = await resolve_user_target(session, user_target)
    if target_error is not None:
        return Result.failure(target_error, target_error_message(target_error))
    if user is None:
        return Result.failure(ERROR_NOT_FOUND, "No matching user was found.")

    scope_values, group_values = await effective_scope_sets_for_user(session, user.id)
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
    session: AsyncSession,
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

    manager = create_user_manager(session, options, delivery)
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

    user.is_admin = is_admin
    user.preferred_timezone = preferred_timezone
    user.expires_at = expires_at
    user.modified_at = current_timestamp()
    await session.commit()
    await session.refresh(user)
    return Result.ok(user_record(user))


async def list_local_users_for_management(
    session: AsyncSession,
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
    if order == "email-domain":
        dialect_name = _session_dialect_name(session)
        if dialect_name not in EMAIL_DOMAIN_ORDER_DIALECTS:
            return Result.failure(
                ERROR_UNSUPPORTED_ORDER,
                "Email-domain ordering is not supported for SQL dialect "
                f"{dialect_name!r}; use --order email instead.",
            )

    query = _list_users_query(
        session,
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
        now=now,
    )
    ordered_users = (await session.execute(query)).scalars().all()
    records = [user_record(user, now=now) for user in ordered_users]
    if include_passkeys:
        passkeys_by_user = await _active_passkeys_by_user(session, ordered_users)
        for user, record in zip(ordered_users, records, strict=True):
            record["passkeys"] = passkeys_by_user.get(user.id, [])
    return Result.ok({"users": records})


async def _active_passkeys_by_user(
    session: AsyncSession,
    users: Sequence[User],
) -> dict[UUID, list[dict[str, Any]]]:
    user_ids = [user.id for user in users]
    if not user_ids:
        return {}

    credentials = (
        (
            await session.execute(
                select(IdentityWebAuthnCredential)
                .where(
                    IdentityWebAuthnCredential.user_id.in_(user_ids),
                    IdentityWebAuthnCredential.status == WEBAUTHN_ACTIVE_STATUS,
                )
                .order_by(
                    IdentityWebAuthnCredential.user_id,
                    IdentityWebAuthnCredential.created_at.desc(),
                )
            )
        )
        .scalars()
        .all()
    )
    passkeys_by_user: dict[UUID, list[dict[str, Any]]] = defaultdict(list)
    for credential in credentials:
        passkeys_by_user[credential.user_id].append(passkey_record(credential))
    return passkeys_by_user


async def resolve_user_target(
    session: AsyncSession,
    target: str,
) -> tuple[User | None, ResultErrorType | None]:
    if "@" in target:
        normalised_target = normalise_email_target(target)
        if normalised_target is None:
            return None, ERROR_INVALID_EMAIL

        return await resolve_user_by_normalised_email(session, normalised_target), None

    try:
        user_id = UUID(target)
    except ValueError:
        return None, ERROR_INVALID_USER_ID

    return await session.get(User, user_id), None


def target_error_message(error_type: ResultErrorType) -> str:
    if error_type == ERROR_INVALID_EMAIL:
        return "User target email address is invalid."

    if error_type == ERROR_INVALID_USER_ID:
        return "User target must be an email address or valid user ID."

    return "User target is invalid."


async def update_local_user_for_management(
    session: AsyncSession,
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
    user, target_error = await resolve_user_target(session, target)
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
        has_changes = True

    if is_superuser is not None:
        if (
            not is_superuser
            and user.is_superuser
            and await _sole_superuser(session, user)
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
        manager = create_user_manager(session, options, delivery)
        try:
            await manager.validate_password(password, user)
        except InvalidPasswordException as exc:
            return Result.failure(
                ERROR_INVALID_PASSWORD,
                _invalid_password_message(exc),
            )

        user.hashed_password = manager.password_helper.hash(password)
        if revoke_sessions:
            await _delete_user_sessions(session, user)
        has_changes = True

    if not has_changes:
        return Result.failure(ERROR_NO_CHANGES, "No user changes were requested.")

    user.modified_at = current_timestamp()
    await session.commit()
    await session.refresh(user)
    return Result.ok(user_record(user))


async def delete_local_user_for_management(
    session: AsyncSession,
    *,
    target: str,
) -> Result[dict[str, Any]]:
    user, target_error = await resolve_user_target(session, target)
    if target_error is not None:
        return Result.failure(target_error, target_error_message(target_error))
    if user is None:
        return Result.failure(ERROR_NOT_FOUND, "No matching user was found.")

    if user.is_superuser:
        return Result.failure(
            ERROR_SUPERUSER_PROTECTED,
            "superuser accounts cannot be deleted.",
        )

    record = user_record(user)
    await _delete_user_sessions(session, user)
    await session.delete(user)
    await session.commit()
    return Result.ok(record)


async def deactivate_local_user_for_management(
    session: AsyncSession,
    *,
    target: str,
) -> Result[dict[str, Any]]:
    user, target_error = await resolve_user_target(session, target)
    if target_error is not None:
        return Result.failure(target_error, target_error_message(target_error))
    if user is None:
        return Result.failure(ERROR_NOT_FOUND, "No matching user was found.")

    if user.is_superuser:
        return Result.failure(
            ERROR_SUPERUSER_PROTECTED,
            "superuser accounts cannot be deactivated.",
        )

    user.is_active = False
    user.modified_at = current_timestamp()
    await _delete_user_sessions(session, user)
    await session.commit()
    await session.refresh(user)
    return Result.ok(user_record(user))


async def _sole_superuser(session: AsyncSession, user: User) -> bool:
    """Return whether ``user`` is the only superuser.

    Dialects that honour ``SELECT ... FOR UPDATE`` serialise concurrent
    superuser demotions before the caller mutates ``user.is_superuser``. SQLite
    ignores that row-locking hint, so this is best-effort in local development;
    production deployments that need concurrent superuser administration should
    use a database/isolation strategy with reliable row locks.
    """

    count = await session.scalar(
        select(func.count())
        .select_from(User.__table__)
        .where(User.__table__.c.is_superuser.is_(True))
        .with_for_update()
    )
    return bool(user.is_superuser and count == 1)


async def _delete_user_sessions(session: AsyncSession, user: User) -> None:
    await session.execute(delete(AccessToken).where(AccessToken.user_id == user.id))


async def _resolve_group_result(
    session: AsyncSession,
    target: str,
) -> Result[Any]:
    group, target_error = await resolve_group_target(session, target)
    if target_error is not None:
        return Result.failure(target_error, group_target_error_message(target_error))
    if group is None:
        return Result.failure(ERROR_NOT_FOUND, "No matching group was found.")
    return Result.ok(group)


def _group_from_result(result: Result[Any]) -> Group:
    return cast(Group, result.value)


async def _related_group_abbrevs(
    session: AsyncSession, group_id_column, predicate
) -> list[str]:
    return list(
        (
            await session.execute(
                select(Group.abbrev)
                .join_from(GroupGroup, Group, Group.id == group_id_column)
                .where(predicate)
                .order_by(Group.abbrev)
            )
        )
        .scalars()
        .all()
    )


async def _group_has_memberships(session: AsyncSession, group: Group) -> bool:
    checks = (
        select(exists().where(GroupUser.group_id == group.id)),
        select(exists().where(GroupGroup.parent_group_id == group.id)),
        select(exists().where(GroupGroup.child_group_id == group.id)),
    )
    for query in checks:
        if await session.scalar(query):
            return True
    return False


async def _group_reaches(
    session: AsyncSession,
    start_group_id: UUID,
    target_group_id: UUID,
) -> bool:
    return target_group_id in await _reachable_group_ids(session, start_group_id)


async def _reachable_group_ids(
    session: AsyncSession,
    start_group_id: UUID,
) -> set[UUID]:
    visited: set[UUID] = set()
    pending = {start_group_id}
    while pending:
        current_ids = pending - visited
        if not current_ids:
            break
        visited.update(current_ids)
        child_ids = set(
            (
                await session.execute(
                    select(GroupGroup.child_group_id).where(
                        GroupGroup.parent_group_id.in_(current_ids)
                    )
                )
            )
            .scalars()
            .all()
        )
        pending = child_ids - visited

    visited.discard(start_group_id)
    return visited


async def _group_ids_reaching(
    session: AsyncSession,
    target_group_id: UUID,
) -> set[UUID]:
    visited: set[UUID] = set()
    pending = {target_group_id}
    while pending:
        current_ids = pending - visited
        if not current_ids:
            break
        visited.update(current_ids)
        parent_ids = set(
            (
                await session.execute(
                    select(GroupGroup.parent_group_id).where(
                        GroupGroup.child_group_id.in_(current_ids)
                    )
                )
            )
            .scalars()
            .all()
        )
        pending = parent_ids - visited

    visited.discard(target_group_id)
    return visited


def _list_users_query(
    session: AsyncSession,
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
    order: str,
    direction: str | None,
    now: float | None = None,
) -> Select[tuple[User]]:
    reference_now = current_timestamp() if now is None else now
    user_table = User.__table__
    query = select(User)
    email_column = user_table.c.email

    if email_pattern is not None:
        query = query.where(
            func.lower(email_column).like(
                _sql_wildcard_pattern(email_pattern.lower()),
                escape="\\",
            )
        )

    if domain_pattern is not None:
        query = query.where(
            func.lower(email_column).like(
                _sql_wildcard_pattern(f"*@{domain_pattern}".lower()),
                escape="\\",
            )
        )

    if is_admin is not None:
        query = query.where(user_table.c.is_admin.is_(is_admin))

    if is_superuser is not None:
        query = query.where(user_table.c.is_superuser.is_(is_superuser))

    if effective_active is not None:
        query = query.where(
            _effective_active_expression(reference_now, effective_active)
        )

    if is_verified is not None:
        query = query.where(user_table.c.is_verified.is_(is_verified))

    query = _apply_timestamp_range(
        query,
        user_table.c.created_at,
        since_created_at,
        before_created_at,
    )
    query = _apply_timestamp_range(
        query,
        user_table.c.modified_at,
        since_modified_at,
        before_modified_at,
    )
    if never_logged_in is True:
        query = query.where(user_table.c.last_login_at.is_(None))
    else:
        if never_logged_in is False:
            query = query.where(user_table.c.last_login_at.is_not(None))
        query = _apply_timestamp_range(
            query,
            user_table.c.last_login_at,
            since_last_login_at,
            before_last_login_at,
        )
    return query.order_by(*_list_ordering(session, order, direction))


def _effective_active_expression(reference_now: float, effective_active: bool):
    user_table = User.__table__
    expires_active = or_(
        user_table.c.expires_at.is_(None),
        user_table.c.expires_at > reference_now,
    )
    return (
        user_table.c.is_active.is_(True) & expires_active
        if effective_active
        else or_(user_table.c.is_active.is_(False), ~expires_active)
    )


def _apply_timestamp_range(
    query: Select[tuple[User]],
    column,
    since_value: float | None,
    before_value: float | None,
) -> Select[tuple[User]]:
    if since_value is not None:
        query = query.where(column >= since_value)

    if before_value is not None:
        query = query.where(column < before_value)

    return query


def _list_ordering(
    session: AsyncSession,
    order: str,
    direction: str | None,
):
    user_table = User.__table__
    match order:
        case "email-domain":
            expressions = (_email_domain_expression(session), user_table.c.email)
        case "created-at":
            expressions = (user_table.c.created_at, user_table.c.email)
        case "modified-at":
            expressions = (user_table.c.modified_at, user_table.c.email)
        case "last-login-at":
            expressions = (user_table.c.last_login_at, user_table.c.email)
        case _:
            expressions = (user_table.c.email,)

    reverse = _reverse_order(order, direction)
    ordered_expressions = []
    for index, expression in enumerate(expressions):
        ordered_expression = expression.desc() if reverse else expression.asc()
        if order == "last-login-at" and index == 0:
            ordered_expression = ordered_expression.nulls_last()
        ordered_expressions.append(ordered_expression)
    return tuple(ordered_expressions)


def _email_domain_expression(session: AsyncSession):
    email_column = User.__table__.c.email
    dialect_name = _session_dialect_name(session)
    if dialect_name == "postgresql":
        return func.lower(func.split_part(email_column, "@", 2))

    if dialect_name == "sqlite":
        return func.lower(func.substr(email_column, func.instr(email_column, "@") + 1))

    raise RuntimeError(
        f"Email-domain ordering is not supported for SQL dialect {dialect_name!r}."
    )


def _session_dialect_name(session: AsyncSession) -> str:
    return session.get_bind().dialect.name


def _invalid_password_message(exc: InvalidPasswordException) -> str:
    return public_password_failure_message(exc)


def _sql_wildcard_pattern(pattern: str) -> str:
    r"""Translate user wildcards into an escaped SQL LIKE pattern.

    Unescaped ``*`` is the application wildcard. Escaped ``\*`` is a literal
    asterisk, and SQL LIKE metacharacters are escaped for literal matching.
    Backslash is a one-character lookahead state: it escapes ``*``, ``%``,
    ``_``, or another backslash when followed by those characters; otherwise it
    is emitted as an escaped literal backslash and the following character is
    processed normally.

    Canonical examples are covered in tests. In Python string notation,
    ``"*"`` returns ``"%"``, ``r"\*"`` returns ``"*"``, ``"%"`` returns
    ``r"\%"``, ``"_"`` returns ``r"\_"``, ``r"foo\bar"`` returns
    ``r"foo\\bar"``, and ``r"*\\"`` returns ``r"%\\"``. The final example
    means SQL LIKE sees an application wildcard followed by an escaped literal
    backslash.
    """

    escaped_chars: list[str] = []
    index = 0
    length = len(pattern)
    while index < length:
        char = pattern[index]
        if char == "\\" and index + 1 < length:
            next_char = pattern[index + 1]
            if next_char == "*":
                escaped_chars.append("*")
                index += 2
                continue
            if next_char in {"%", "_", "\\"}:
                escaped_chars.append(f"\\{next_char}")
                index += 2
                continue

            escaped_chars.append("\\\\")
            index += 1
            continue

        if char == "*":
            escaped_chars.append("%")
        elif char in {"%", "_", "\\"}:
            escaped_chars.append(f"\\{char}")
        else:
            escaped_chars.append(char)

        index += 1

    return "".join(escaped_chars)


def _reverse_order(order: str, direction: str | None) -> bool:
    if direction is not None:
        return direction == "desc"

    return order in {"created-at", "modified-at", "last-login-at"}


def _valid_timezone(value: str) -> bool:
    try:
        ZoneInfo(value)
    except ZoneInfoNotFoundError:
        return False

    return True
