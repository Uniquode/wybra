from __future__ import annotations

import copy
import logging.config
from collections.abc import Mapping
from typing import Any, Final

from wybra.core.composition import AppConfig

DICT_CONFIG_VERSION = 1
DEFAULT_LOG_FORMAT: Final = "%(asctime)s %(levelname)s %(name)s %(message)s"
DEFAULT_LOG_DATE_FORMAT: Final = "%Y-%m-%dT%H:%M:%S%z"


class LoggingConfigurationError(ValueError):
    """Raised when runtime logging configuration cannot be applied."""


DEFAULT_LOGGING_CONFIG: Final[dict[str, Any]] = {
    "version": DICT_CONFIG_VERSION,
    "disable_existing_loggers": False,
    "formatters": {
        "simple": {
            "format": DEFAULT_LOG_FORMAT,
            "datefmt": DEFAULT_LOG_DATE_FORMAT,
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "level": "INFO",
            "formatter": "simple",
            "stream": "ext://sys.stderr",
        },
    },
    "root": {
        "level": "WARNING",
        "handlers": ["console"],
    },
    "loggers": {
        "alembic": {
            "level": "INFO",
        },
        "sqlalchemy.engine": {
            "level": "WARNING",
        },
    },
}


def logging_config_from_app_config(app_config: AppConfig | None) -> dict[str, Any]:
    if app_config is None:
        return default_logging_config()

    return merge_logging_config(app_config.raw_config.get("log"))


def default_logging_config() -> dict[str, Any]:
    return copy.deepcopy(DEFAULT_LOGGING_CONFIG)


def merge_logging_config(config: object) -> dict[str, Any]:
    if config is None:
        return default_logging_config()
    if not isinstance(config, Mapping):
        raise ValueError("[log] must be a table.")

    configured = _plain_dict(config)
    if configured.get("disable_existing_loggers") is True:
        configured.setdefault("version", DICT_CONFIG_VERSION)
        return configured

    return _deep_merge(default_logging_config(), configured)


def configure_logging(config: Mapping[str, Any]) -> None:
    try:
        logging.config.dictConfig(_plain_dict(config))
    except (AttributeError, ImportError, TypeError, ValueError) as exc:
        raise LoggingConfigurationError("Logging configuration is invalid.") from exc


def configure_runtime_logging(
    app_config: AppConfig | None = None,
    *,
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    logging_config = (
        _plain_dict(config)
        if config is not None
        else logging_config_from_app_config(app_config)
    )
    configure_logging(logging_config)
    return logging_config


def _deep_merge(base: dict[str, Any], overrides: Mapping[Any, Any]) -> dict[str, Any]:
    for key, value in overrides.items():
        if isinstance(value, Mapping) and isinstance(base.get(key), dict):
            base[key] = _deep_merge(base[key], value)
        else:
            base[key] = _plain_value(value)
    return base


def _plain_dict(config: Mapping[Any, Any]) -> dict[str, Any]:
    return {str(key): _plain_value(value) for key, value in config.items()}


def _plain_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return _plain_dict(value)
    if isinstance(value, list):
        return [_plain_value(item) for item in value]
    return copy.deepcopy(value)


__all__ = (
    "DEFAULT_LOGGING_CONFIG",
    "DEFAULT_LOG_DATE_FORMAT",
    "DEFAULT_LOG_FORMAT",
    "LoggingConfigurationError",
    "configure_logging",
    "configure_runtime_logging",
    "default_logging_config",
    "logging_config_from_app_config",
    "merge_logging_config",
)
