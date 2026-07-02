from __future__ import annotations

import csv
import json
import sys
from datetime import UTC, datetime
from typing import Any

from wybra.auth.admin.management import (
    ERROR_CYCLIC_GROUP_MEMBERSHIP,
    ERROR_FINAL_SUPERUSER,
    ERROR_GROUP_HAS_MEMBERSHIPS,
    ERROR_INVALID_TIMEZONE,
    ERROR_INVALID_USER_ID,
    ERROR_NO_ACTIVE_PASSKEY,
    ERROR_NO_ACTIVE_TOTP,
    ERROR_NO_CHANGES,
    ERROR_NOT_FOUND,
    ERROR_SCOPE_IN_USE,
    ERROR_SUPERUSER_PROTECTED,
    ERROR_UNSUPPORTED_ORDER,
    USER_RECORD_FIELDS,
    USER_TIMESTAMP_FIELDS,
)
from wybra.auth.result import (
    ERROR_ALREADY_EXISTS,
    ERROR_INVALID_EMAIL,
    ERROR_INVALID_PASSWORD,
)

TIMESTAMP_FIELDS: frozenset[str] = frozenset(USER_TIMESTAMP_FIELDS)
PASSKEY_TIMESTAMP_FIELDS: frozenset[str] = frozenset(
    {"created_at", "last_used_at", "revoked_at"}
)
USER_NESTED_RECORD_FIELDS: tuple[str, ...] = ("passkeys",)


def _print_failure(error_type: str | None, message: str | None) -> int:
    fallback_messages = {
        ERROR_ALREADY_EXISTS: "User already exists.",
        ERROR_CYCLIC_GROUP_MEMBERSHIP: "Nested group membership would create a cycle.",
        ERROR_FINAL_SUPERUSER: "Cannot remove the final superuser flag.",
        ERROR_GROUP_HAS_MEMBERSHIPS: "Group still has memberships.",
        ERROR_INVALID_EMAIL: "Email address is invalid.",
        ERROR_INVALID_PASSWORD: "Password is invalid.",
        ERROR_INVALID_TIMEZONE: "Preferred timezone is invalid.",
        ERROR_INVALID_USER_ID: "User target must be an email address or valid user ID.",
        ERROR_NO_ACTIVE_PASSKEY: "User does not have active passkeys.",
        ERROR_NO_CHANGES: "No user changes were requested.",
        ERROR_NO_ACTIVE_TOTP: "User does not have active TOTP.",
        ERROR_NOT_FOUND: "No matching user was found.",
        ERROR_SCOPE_IN_USE: "Scope is assigned to one or more groups.",
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

    for record in cleaned_records:
        _print_human_user_record(record)


def _print_records(
    records: list[dict[str, Any]],
    *,
    field_names: tuple[str, ...],
    json_output: bool,
    csv_output: bool,
) -> None:
    cleaned_records = [
        _record_without_nulls_for_fields(record, field_names) for record in records
    ]
    if json_output:
        print(json.dumps(cleaned_records))
        return

    formatted_records = [
        {
            field_name: _format_record_value(value)
            for field_name, value in record.items()
        }
        for record in cleaned_records
    ]
    if csv_output:
        writer = csv.DictWriter(sys.stdout, fieldnames=list(field_names))
        writer.writeheader()
        writer.writerows(formatted_records)
        return

    for record in formatted_records:
        print(
            " ".join(
                f"{field_name}={record.get(field_name)}"
                for field_name in field_names
                if field_name in record
            )
        )


def _print_single_record(record: dict[str, Any], *, json_output: bool) -> None:
    if json_output:
        print(json.dumps(_record_without_nulls_for_fields(record, tuple(record))))
        return

    for field_name, value in _record_without_nulls_for_fields(
        record,
        tuple(record),
    ).items():
        print(f"{field_name}={_format_record_value(value)}")


def _record_without_nulls_for_fields(
    record: dict[str, Any],
    field_names: tuple[str, ...],
) -> dict[str, Any]:
    return {
        field_name: record[field_name]
        for field_name in field_names
        if field_name in record and record[field_name] is not None
    }


def _format_record_value(value: Any) -> str:
    if isinstance(value, str | int | float | bool) or value is None:
        return str(value)
    try:
        return json.dumps(value)
    except TypeError:
        return str(value)


def _record_without_nulls(record: dict[str, Any]) -> dict[str, Any]:
    return {
        field_name: record[field_name]
        for field_name in (*USER_RECORD_FIELDS, *USER_NESTED_RECORD_FIELDS)
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
    if (
        field_name in TIMESTAMP_FIELDS or field_name in PASSKEY_TIMESTAMP_FIELDS
    ) and isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), UTC).isoformat()
        except OverflowError:
            return value
        except OSError:
            return value
        except ValueError:
            return value

    return value


def _print_human_user_record(record: dict[str, Any]) -> None:
    human_record = _records_for_human_output([record])[0]
    print(
        " ".join(
            [
                str(human_record.get("email", "<unknown>")),
                f"id={human_record.get('id', '<unknown>')}",
                f"admin={human_record.get('is_admin', False)}",
                f"superuser={human_record.get('is_superuser', False)}",
                f"active={human_record.get('effective_active', False)}",
                f"verified={human_record.get('is_verified', False)}",
            ]
        )
    )
    passkeys = record.get("passkeys")
    if not isinstance(passkeys, list):
        return
    for passkey in passkeys:
        if isinstance(passkey, dict):
            _print_human_passkey_record(passkey)


def _print_human_passkey_record(record: dict[str, Any]) -> None:
    fields = (
        "id",
        "credential_id",
        "label",
        "created_at",
        "last_used_at",
        "user_verified",
    )
    parts = [
        f"{field_name}={_format_human_value(field_name, record[field_name])}"
        for field_name in fields
        if field_name in record and record[field_name] is not None
    ]
    print(f"  passkey {' '.join(parts)}")


def _csv_fieldnames() -> list[str]:
    return list(USER_RECORD_FIELDS)
