from __future__ import annotations

import argparse
import asyncio
import csv
import getpass
import json
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import dateparser

from auth_ext.configuration import ConfigurationError
from auth_ext.database import close_database, create_database, session_scope
from auth_ext.management import (
    ERROR_FINAL_SUPERUSER,
    ERROR_INVALID_TIMEZONE,
    ERROR_INVALID_USER_ID,
    ERROR_NO_CHANGES,
    ERROR_NOT_FOUND,
    ERROR_SUPERUSER_PROTECTED,
    ERROR_UNSUPPORTED_ORDER,
    USER_RECORD_FIELDS,
    USER_TIMESTAMP_FIELDS,
    create_local_user_for_management,
    deactivate_local_user_for_management,
    delete_local_user_for_management,
    list_local_users_for_management,
    resolve_user_target,
    target_error_message,
    update_local_user_for_management,
    user_record,
)
from auth_ext.result import (
    ERROR_ALREADY_EXISTS,
    ERROR_INVALID_EMAIL,
    ERROR_INVALID_PASSWORD,
    Result,
)
from auth_ext.settings import load_auth_settings

TIMESTAMP_FIELDS: frozenset[str] = frozenset(USER_TIMESTAMP_FIELDS)
PASSWORD_SOURCE_STDIN = "-"
PASSWORD_SOURCE_PROMPT = "prompt"
TIMESTAMP_HELP = (
    "Timestamp options parse numeric input as Unix seconds before date parsing; "
    "use separated calendar forms such as 2025-01-01 for dates."
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="usermgr",
        description="Manage local identity users through configured services.",
        epilog=TIMESTAMP_HELP,
    )
    parser.add_argument(
        "--config",
        help="Path to auth.toml. Defaults to AUTH_CONFIG or ./auth.toml when present.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    create = subparsers.add_parser("create", help="Create a local user.")
    create.add_argument("email")
    create.add_argument(
        "--password",
        nargs="?",
        const=PASSWORD_SOURCE_PROMPT,
        default=PASSWORD_SOURCE_PROMPT,
        choices=(PASSWORD_SOURCE_STDIN, PASSWORD_SOURCE_PROMPT),
    )
    create.add_argument("--admin", action="store_true")
    create.add_argument("--superuser", action="store_true")
    create.add_argument("--unverified", action="store_true")
    create.add_argument("--display-name")
    create.add_argument("--preferred-name")
    create.add_argument("--timezone", dest="preferred_timezone")
    create.add_argument("--expires-at", type=parse_timestamp_filter)

    update = subparsers.add_parser("update", help="Update a local user.")
    update.add_argument("target")
    _add_boolean_pair(update, "--admin", "--no-admin", "is_admin")
    _add_boolean_pair(update, "--superuser", "--no-superuser", "is_superuser")
    _add_boolean_pair(update, "--verify", "--no-verify", "is_verified")
    update.add_argument(
        "--password",
        nargs="?",
        const=PASSWORD_SOURCE_PROMPT,
        choices=(PASSWORD_SOURCE_STDIN, PASSWORD_SOURCE_PROMPT),
    )
    update.add_argument("--no-revoke", action="store_true")
    display_name_group = update.add_mutually_exclusive_group()
    display_name_group.add_argument("--display-name")
    display_name_group.add_argument(
        "--no-display-name",
        dest="clear_display_name",
        action="store_true",
    )
    preferred_name_group = update.add_mutually_exclusive_group()
    preferred_name_group.add_argument("--preferred-name")
    preferred_name_group.add_argument(
        "--no-preferred-name",
        dest="clear_preferred_name",
        action="store_true",
    )
    timezone_group = update.add_mutually_exclusive_group()
    timezone_group.add_argument("--timezone", dest="preferred_timezone")
    timezone_group.add_argument(
        "--no-timezone",
        dest="clear_preferred_timezone",
        action="store_true",
    )
    expires_group = update.add_mutually_exclusive_group()
    expires_group.add_argument("--expires-at", type=parse_timestamp_filter)
    expires_group.add_argument("--no-expires-at", action="store_true")

    delete = subparsers.add_parser("delete", help="Delete a local user.")
    delete.add_argument("target")
    delete.add_argument("--force", action="store_true")

    deactivate = subparsers.add_parser("deactivate", help="Deactivate a local user.")
    deactivate.add_argument("target")
    deactivate.add_argument("--force", action="store_true")

    list_parser = subparsers.add_parser("list", help="List local users.")
    output_group = list_parser.add_mutually_exclusive_group()
    output_group.add_argument("--json", action="store_true", dest="json_output")
    output_group.add_argument("--csv", action="store_true", dest="csv_output")
    list_parser.add_argument("--email", "-e", dest="email_pattern")
    list_parser.add_argument("--domain", "-d", dest="domain_pattern")
    _add_boolean_pair(list_parser, "--admin", "--non-admin", "is_admin")
    _add_boolean_pair(list_parser, "--superuser", "--non-superuser", "is_superuser")
    _add_boolean_pair(list_parser, "--active", "--inactive", "effective_active")
    _add_boolean_pair(list_parser, "--verified", "--unverified", "is_verified")
    list_parser.add_argument(
        "--since-created-at",
        "-C",
        type=parse_timestamp_filter,
    )
    list_parser.add_argument(
        "--before-created-at",
        "-c",
        type=parse_timestamp_filter,
    )
    list_parser.add_argument(
        "--since-modified-at",
        "-M",
        type=parse_timestamp_filter,
    )
    list_parser.add_argument(
        "--before-modified-at",
        "-m",
        type=parse_timestamp_filter,
    )
    list_parser.add_argument(
        "--since-last-login-at",
        "-L",
        type=parse_timestamp_filter,
    )
    list_parser.add_argument(
        "--before-last-login-at",
        "-l",
        type=parse_timestamp_filter,
    )
    _add_boolean_pair(
        list_parser,
        "--never-logged-in",
        "--logged-in",
        "never_logged_in",
    )
    list_parser.add_argument(
        "--order",
        choices=("email", "email-domain", "created-at", "modified-at", "last-login-at"),
        default="email",
    )
    list_parser.add_argument("--direction", choices=("asc", "desc"))

    password = subparsers.add_parser("password", help="Change a local user's password.")
    password.add_argument("target")
    password.add_argument(
        "--password",
        nargs="?",
        const=PASSWORD_SOURCE_PROMPT,
        default=PASSWORD_SOURCE_PROMPT,
        choices=(PASSWORD_SOURCE_STDIN, PASSWORD_SOURCE_PROMPT),
    )
    password.add_argument("--no-revoke", action="store_true")

    return parser


def _add_boolean_pair(
    parser: argparse.ArgumentParser,
    positive: str,
    negative: str,
    destination: str,
) -> None:
    group = parser.add_mutually_exclusive_group()
    group.add_argument(positive, dest=destination, action="store_true", default=None)
    group.add_argument(negative, dest=destination, action="store_false")


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return asyncio.run(_main_async(args))
    except ConfigurationError as exc:
        print(f"configuration: {exc}", file=sys.stderr)
        return 1


async def _main_async(args: argparse.Namespace) -> int:
    settings = load_auth_settings(config_path=args.config)
    database = create_database(settings.database_url)
    try:
        async with session_scope(database.session_factory) as session:
            match args.command:
                case "create":
                    password = _read_password(args.password)
                    result = await create_local_user_for_management(
                        session,
                        settings.identity_options,
                        email=args.email,
                        password=password,
                        is_admin=args.admin,
                        is_superuser=args.superuser,
                        is_verified=not args.unverified,
                        display_name=args.display_name,
                        preferred_name=args.preferred_name,
                        preferred_timezone=args.preferred_timezone,
                        expires_at=args.expires_at,
                    )
                    if result.is_failure():
                        return _print_failure(result.error_type, result.message)

                    value = result.value or {}
                    print(f"created user: {value.get('email', args.email)}")
                    return 0
                case "update":
                    password = (
                        _read_password(args.password)
                        if args.password is not None
                        else None
                    )
                    result = await update_local_user_for_management(
                        session,
                        settings.identity_options,
                        target=args.target,
                        is_admin=args.is_admin,
                        is_superuser=args.is_superuser,
                        is_verified=args.is_verified,
                        password=password,
                        revoke_sessions=not args.no_revoke,
                        display_name=args.display_name,
                        clear_display_name=args.clear_display_name,
                        preferred_name=args.preferred_name,
                        clear_preferred_name=args.clear_preferred_name,
                        preferred_timezone=args.preferred_timezone,
                        clear_preferred_timezone=args.clear_preferred_timezone,
                        expires_at=args.expires_at,
                        clear_expires_at=args.no_expires_at,
                    )
                    if result.is_failure():
                        return _print_failure(result.error_type, result.message)

                    value = result.value or {}
                    print(f"updated user: {value.get('email', args.target)}")
                    return 0
                case "delete":
                    if not args.force:
                        target_result = await _resolve_target_record(
                            session, args.target
                        )
                        if target_result.is_failure():
                            return _print_failure(
                                target_result.error_type,
                                target_result.message,
                            )
                        target_record = target_result.value or {}
                        if not _confirm_destructive("delete", target_record):
                            return 1

                    result = await delete_local_user_for_management(
                        session,
                        target=args.target,
                    )
                    if result.is_failure():
                        return _print_failure(result.error_type, result.message)

                    value = result.value or {}
                    print(f"deleted user: {value.get('email', args.target)}")
                    return 0
                case "deactivate":
                    if not args.force:
                        target_result = await _resolve_target_record(
                            session, args.target
                        )
                        if target_result.is_failure():
                            return _print_failure(
                                target_result.error_type,
                                target_result.message,
                            )
                        target_record = target_result.value or {}
                        if not _confirm_destructive("deactivate", target_record):
                            return 1

                    result = await deactivate_local_user_for_management(
                        session,
                        target=args.target,
                    )
                    if result.is_failure():
                        return _print_failure(result.error_type, result.message)

                    value = result.value or {}
                    print(f"deactivated user: {value.get('email', args.target)}")
                    return 0
                case "list":
                    result = await list_local_users_for_management(
                        session,
                        email_pattern=args.email_pattern,
                        domain_pattern=args.domain_pattern,
                        is_admin=args.is_admin,
                        is_superuser=args.is_superuser,
                        effective_active=args.effective_active,
                        is_verified=args.is_verified,
                        since_created_at=args.since_created_at,
                        before_created_at=args.before_created_at,
                        since_modified_at=args.since_modified_at,
                        before_modified_at=args.before_modified_at,
                        since_last_login_at=args.since_last_login_at,
                        before_last_login_at=args.before_last_login_at,
                        never_logged_in=args.never_logged_in,
                        order=args.order,
                        direction=args.direction,
                    )
                    if result.is_failure():
                        return _print_failure(result.error_type, result.message)

                    records = (result.value or {}).get("users", [])
                    _print_user_records(
                        records,
                        json_output=args.json_output,
                        csv_output=args.csv_output,
                    )
                    return 0
                case "password":
                    password = _read_password(args.password)
                    result = await update_local_user_for_management(
                        session,
                        settings.identity_options,
                        target=args.target,
                        password=password,
                        revoke_sessions=not args.no_revoke,
                    )
                    if result.is_failure():
                        return _print_failure(result.error_type, result.message)

                    value = result.value or {}
                    print(f"changed password: {value.get('email', args.target)}")
                    return 0
                case _:
                    print(f"{args.command}: not implemented", file=sys.stderr)
                    return 1
    finally:
        await close_database(database)


def _read_password(value: str) -> str:
    if value == PASSWORD_SOURCE_STDIN:
        if sys.stdin.isatty():
            raise ConfigurationError(
                "Refusing to read password from interactive stdin; "
                "pipe a password or omit --password for a hidden prompt."
            )
        line = sys.stdin.readline()
        if line == "":
            raise ConfigurationError("No password received on stdin.")

        password = line.rstrip("\r\n")
        if sys.stdin.read(1):
            raise ConfigurationError(
                "Password stdin input must contain exactly one line."
            )
        return password

    if value == PASSWORD_SOURCE_PROMPT:
        password = getpass.getpass("Password: ")
        confirmation = getpass.getpass("Confirm password: ")
        if password != confirmation:
            raise ConfigurationError("Password confirmation does not match.")
        return password

    raise ConfigurationError(f"Unsupported password source: {value!r}")


def parse_timestamp_filter(value: str) -> float:
    """Parse CLI timestamp input.

    Numeric input is intentionally interpreted first as Unix seconds. Use a
    separated date form such as ``2025-01-01`` for calendar dates.
    """

    try:
        return float(value)
    except ValueError:
        pass

    parsed = dateparser.parse(
        value,
        settings=_timestamp_parser_settings(),
    )
    if parsed is None:
        raise argparse.ArgumentTypeError(f"Invalid timestamp value: {value}")

    return parsed.astimezone(UTC).timestamp()


def _timestamp_parser_settings() -> dict[str, object]:
    return {
        "DATE_ORDER": "DMY",
        "DEFAULT_LANGUAGES": ["en"],
        "RETURN_AS_TIMEZONE_AWARE": True,
        "TIMEZONE": _local_timezone_name(),
        "TO_TIMEZONE": "UTC",
    }


def _local_timezone_name() -> str:
    local_timezone = datetime.now().astimezone().tzinfo
    return _timezone_name_from_tzinfo(local_timezone)


def _timezone_name_from_tzinfo(local_timezone: Any) -> str:
    if local_timezone is None:
        return "UTC"

    timezone_name = getattr(local_timezone, "key", None)
    if isinstance(timezone_name, str) and timezone_name:
        return timezone_name

    timezone_name = getattr(local_timezone, "zone", None)
    if isinstance(timezone_name, str) and timezone_name:
        return timezone_name

    return "UTC"


async def _resolve_target_record(
    session,
    target: str,
) -> Result[dict[str, Any]]:
    user, target_error = await resolve_user_target(session, target)
    if target_error is not None:
        return Result.failure(target_error, target_error_message(target_error))

    if user is None:
        return Result.failure(ERROR_NOT_FOUND)

    return Result.ok(user_record(user))


def _confirm_destructive(action: str, record: dict[str, Any]) -> bool:
    answer = input(
        f"{action} {record['email']} ({record['id']})? Type 'yes' to continue: "
    )
    return answer == "yes"


def _print_failure(error_type: str | None, message: str | None) -> int:
    fallback_messages = {
        ERROR_ALREADY_EXISTS: "User already exists.",
        ERROR_FINAL_SUPERUSER: "Cannot remove the final superuser flag.",
        ERROR_INVALID_EMAIL: "Email address is invalid.",
        ERROR_INVALID_PASSWORD: "Password is invalid.",
        ERROR_INVALID_TIMEZONE: "Preferred timezone is invalid.",
        ERROR_INVALID_USER_ID: "User target must be an email address or valid user ID.",
        ERROR_NO_CHANGES: "No user changes were requested.",
        ERROR_NOT_FOUND: "No matching user was found.",
        ERROR_SUPERUSER_PROTECTED: "Superuser accounts are protected.",
        ERROR_UNSUPPORTED_ORDER: "Requested ordering is not supported.",
    }
    fallback_message = (
        fallback_messages.get(error_type) if error_type is not None else None
    )
    print(
        message or fallback_message or "User management failed.",
        file=sys.stderr,
    )
    return 1


def _print_user_records(
    records: list[dict[str, Any]],
    *,
    json_output: bool,
    csv_output: bool,
) -> None:
    cleaned_records = [_record_without_nulls(record) for record in records]
    if json_output:
        # Contract: JSON output omits unset optional fields instead of emitting nulls.
        print(json.dumps(cleaned_records))
        return

    if csv_output:
        writer = csv.DictWriter(sys.stdout, fieldnames=_csv_fieldnames())
        writer.writeheader()
        writer.writerows(_records_for_human_output(cleaned_records))
        return

    for record in _records_for_human_output(cleaned_records):
        print(
            " ".join(
                [
                    str(record.get("email", "<unknown>")),
                    f"id={record.get('id', '<unknown>')}",
                    f"admin={record.get('is_admin', False)}",
                    f"superuser={record.get('is_superuser', False)}",
                    f"active={record.get('effective_active', False)}",
                    f"verified={record.get('is_verified', False)}",
                ]
            )
        )


def _record_without_nulls(record: dict[str, Any]) -> dict[str, Any]:
    return {
        field_name: record[field_name]
        for field_name in USER_RECORD_FIELDS
        if field_name in record and record[field_name] is not None
    }


def _records_for_human_output(
    records: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    return [
        {
            field_name: _format_human_value(field_name, record.get(field_name))
            for field_name in USER_RECORD_FIELDS
            if field_name in record
        }
        for record in records
    ]


def _format_human_value(field_name: str, value: Any) -> Any:
    if field_name in TIMESTAMP_FIELDS and isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), UTC).isoformat()
        except OverflowError:
            return value
        except OSError:
            return value
        except ValueError:
            return value

    return value


def _csv_fieldnames() -> list[str]:
    return list(USER_RECORD_FIELDS)
