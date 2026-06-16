from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import dateparser

TIMESTAMP_HELP = (
    "Timestamp options parse numeric input as Unix seconds before date parsing; "
    "use separated calendar forms such as 2025-01-01 for dates."
)


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
        raise ValueError(f"Invalid timestamp value: {value}")

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
