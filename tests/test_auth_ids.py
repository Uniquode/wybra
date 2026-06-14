from uuid import UUID

from wevra.auth.ids import log_safe_line, log_safe_uuid, parse_uuid


def test_parse_uuid_returns_uuid_for_valid_values() -> None:
    value = UUID("12345678-1234-5678-1234-567812345678")

    assert parse_uuid(value) == value
    assert parse_uuid(str(value)) == value


def test_log_safe_uuid_rejects_invalid_values() -> None:
    assert log_safe_uuid("not-a-uuid") == "<invalid-uuid>"


def test_log_safe_line_strips_line_breaks() -> None:
    assert log_safe_line("first\r\nsecond\nthird\rfourth") == "firstsecondthirdfourth"
