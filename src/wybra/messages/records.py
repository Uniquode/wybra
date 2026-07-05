from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, Literal, Self, cast

from wybra.messages.exceptions import InvalidAlertError

SUCCESS_ALERT: Final = "success"
WARNING_ALERT: Final = "warning"
ERROR_ALERT: Final = "error"
ALERT_SEVERITIES: Final = frozenset((SUCCESS_ALERT, WARNING_ALERT, ERROR_ALERT))
type AlertSeverity = Literal["success", "warning", "error"]
type AlertPayload = dict[str, object]


@dataclass(frozen=True, slots=True)
class AlertRecord:
    severity: AlertSeverity
    message: str
    created_at: float

    @classmethod
    def create(
        cls,
        severity: str,
        message: object,
        *,
        max_message_length: int,
        created_at: float | None = None,
    ) -> Self:
        return cls(
            severity=normalise_alert_severity(severity),
            message=normalise_alert_message(
                message,
                max_message_length=max_message_length,
            ),
            created_at=normalise_created_at(
                time.time() if created_at is None else created_at
            ),
        )

    @classmethod
    def from_payload(
        cls,
        payload: Mapping[str, object],
        *,
        max_message_length: int,
    ) -> Self:
        severity = _payload_string(payload, "severity")
        created_at = _payload_timestamp(payload, "created_at")
        return cls.create(
            severity,
            _payload_value(payload, "message"),
            created_at=created_at,
            max_message_length=max_message_length,
        )

    def to_payload(self) -> AlertPayload:
        return {
            "severity": self.severity,
            "message": self.message,
            "created_at": self.created_at,
        }


def normalise_alert_severity(value: object) -> AlertSeverity:
    if not isinstance(value, str):
        raise InvalidAlertError("Alert severity must be a string.")
    severity = value.strip().lower()
    if severity not in ALERT_SEVERITIES:
        allowed = ", ".join(sorted(ALERT_SEVERITIES))
        raise InvalidAlertError(f"Alert severity must be one of: {allowed}.")
    return cast(AlertSeverity, severity)


def normalise_alert_message(value: object, *, max_message_length: int) -> str:
    if not isinstance(value, str):
        raise InvalidAlertError("Alert message must be text.")
    message = value.strip()
    if not message:
        raise InvalidAlertError("Alert message must not be blank.")
    if len(message) > max_message_length:
        raise InvalidAlertError(
            "Alert message exceeds maximum length: "
            f"length={len(message)}, max_length={max_message_length}."
        )
    return message


def normalise_created_at(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InvalidAlertError("Alert created_at must be a Unix timestamp float.")
    created_at = float(value)
    if created_at <= 0:
        raise InvalidAlertError("Alert created_at must be positive.")
    return created_at


def _payload_value(payload: Mapping[str, object], key: str) -> object:
    try:
        return payload[key]
    except KeyError as exc:
        raise InvalidAlertError(f"Stored alert is missing {key!r}.") from exc


def _payload_string(payload: Mapping[str, object], key: str) -> str:
    value = _payload_value(payload, key)
    if not isinstance(value, str):
        raise InvalidAlertError(f"Stored alert {key!r} must be a string.")
    return value


def _payload_timestamp(payload: Mapping[str, object], key: str) -> float:
    value = _payload_value(payload, key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InvalidAlertError(f"Stored alert {key!r} must be a timestamp.")
    return float(value)


__all__ = (
    "ALERT_SEVERITIES",
    "ERROR_ALERT",
    "SUCCESS_ALERT",
    "WARNING_ALERT",
    "AlertPayload",
    "AlertRecord",
    "AlertSeverity",
    "normalise_alert_message",
    "normalise_alert_severity",
    "normalise_created_at",
)
