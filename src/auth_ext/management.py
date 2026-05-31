from __future__ import annotations

from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi_users.exceptions import (
    InvalidPasswordException,
    UserAlreadyExists,
)
from pydantic import EmailStr, TypeAdapter, ValidationError
from sqlalchemy import Select, delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from auth_ext.delivery import IdentityDelivery
from auth_ext.manager import create_user_manager, public_password_failure_message
from auth_ext.models import AccessToken, User
from auth_ext.options import IdentityOptions
from auth_ext.result import (
    ERROR_ALREADY_EXISTS,
    ERROR_INVALID_EMAIL,
    ERROR_INVALID_PASSWORD,
    Result,
    ResultErrorType,
)
from auth_ext.schemas import UserCreate
from auth_ext.timestamps import current_timestamp

ERROR_INVALID_TIMEZONE = "invalid_timezone"
ERROR_NO_CHANGES = "no_changes"
ERROR_NOT_FOUND = "not_found"
ERROR_SUPERUSER_PROTECTED = "superuser_protected"
ERROR_FINAL_SUPERUSER = "final_superuser"
ERROR_INVALID_USER_ID = "invalid_user_id"
ERROR_UNSUPPORTED_ORDER = "unsupported_order"
EMAIL_DOMAIN_ORDER_DIALECTS = frozenset({"postgresql", "sqlite"})
EMAIL_TARGET_ADAPTER = TypeAdapter(EmailStr)
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
    "display_name",
    "preferred_name",
    "preferred_timezone",
)


def is_user_effectively_active(user: User, *, now: float | None = None) -> bool:
    """Return whether the account is active at ``now``.

    ``expires_at`` is an exclusive upper bound: accounts are inactive at or
    after that Unix timestamp.
    """

    if not user.is_active:
        return False

    expires_at = user.expires_at
    if expires_at is None:
        return True

    reference_now = current_timestamp() if now is None else now
    return expires_at > reference_now


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
        "display_name": user.display_name,
        "preferred_name": user.preferred_name,
        "preferred_timezone": user.preferred_timezone,
    }
    return {field_name: record.get(field_name) for field_name in USER_RECORD_FIELDS}


async def create_local_user_for_management(
    session: AsyncSession,
    options: IdentityOptions,
    *,
    email: str,
    password: str,
    is_admin: bool = False,
    is_superuser: bool = False,
    is_verified: bool = True,
    display_name: str | None = None,
    preferred_name: str | None = None,
    preferred_timezone: str | None = None,
    expires_at: float | None = None,
    delivery: IdentityDelivery | None = None,
) -> Result[dict[str, Any]]:
    if preferred_timezone is not None and not _valid_timezone(preferred_timezone):
        return Result.failure(
            ERROR_INVALID_TIMEZONE,
            f"Unknown preferred timezone: {preferred_timezone}",
        )

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
    user.display_name = display_name
    user.preferred_name = preferred_name
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
    return Result.ok({"users": records})


async def resolve_user_target(
    session: AsyncSession,
    target: str,
) -> tuple[User | None, ResultErrorType | None]:
    if "@" in target:
        email = _normalise_email_target(target)
        if email is None:
            return None, ERROR_INVALID_EMAIL

        return (
            (
                await session.execute(
                    select(User).where(User.__table__.c.email == email)
                )
            ).scalar_one_or_none(),
            None,
        )

    try:
        user_id = UUID(target)
    except ValueError:
        return None, ERROR_INVALID_USER_ID

    return await session.get(User, user_id), None


def _normalise_email_target(target: str) -> str | None:
    try:
        return str(EMAIL_TARGET_ADAPTER.validate_python(target))
    except ValidationError:
        return None


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
    display_name: str | None = None,
    clear_display_name: bool = False,
    preferred_name: str | None = None,
    clear_preferred_name: bool = False,
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
        return Result.failure(
            ERROR_INVALID_TIMEZONE,
            f"Unknown preferred timezone: {preferred_timezone}",
        )

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

    for field_name, field_value, clear_field in (
        ("display_name", display_name, clear_display_name),
        ("preferred_name", preferred_name, clear_preferred_name),
        ("preferred_timezone", preferred_timezone, clear_preferred_timezone),
    ):
        if clear_field and getattr(user, field_name) is not None:
            setattr(user, field_name, None)
            has_changes = True
            continue

        if field_value is not None:
            setattr(user, field_name, field_value)
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
