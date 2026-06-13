from __future__ import annotations


def structured_error(message: str, **fields: object) -> str:
    if not fields:
        return message
    details = ", ".join(
        f"{key}={_format_error_value(value)}" for key, value in fields.items()
    )
    return f"{message}: {details}."


def type_name(value: object) -> str:
    name = getattr(value, "__name__", None)
    if isinstance(name, str) and name:
        return name
    return type(value).__name__


def _format_error_value(value: object) -> str:
    if isinstance(value, str | int | float | bool):
        return str(value)
    return type_name(value)
