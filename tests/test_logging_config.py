from __future__ import annotations

import pytest

from wybra.core.composition import load_app_config
from wybra.core.logging import (
    default_logging_config,
    logging_config_from_app_config,
    merge_logging_config,
)


def test_merge_logging_config_adds_to_defaults_when_not_authoritative() -> None:
    config = merge_logging_config(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {
                "simple": {"format": "%(levelname)s %(name)s: %(message)s"},
                "detailed": {"format": "%(asctime)s %(message)s"},
            },
            "handlers": {
                "file": {
                    "class": "logging.handlers.RotatingFileHandler",
                    "level": "DEBUG",
                    "formatter": "detailed",
                    "filename": "app.log",
                    "maxBytes": 1048576,
                    "backupCount": 3,
                    "encoding": "utf-8",
                },
            },
            "root": {"level": "INFO", "handlers": ["console", "file"]},
            "loggers": {
                "urllib3": {
                    "level": "WARNING",
                    "handlers": ["console"],
                    "propagate": False,
                }
            },
        }
    )

    assert config["disable_existing_loggers"] is False
    assert config["handlers"]["console"]["class"] == "logging.StreamHandler"
    assert config["handlers"]["file"]["filename"] == "app.log"
    assert config["formatters"]["simple"]["format"] == (
        "%(levelname)s %(name)s: %(message)s"
    )
    assert config["loggers"]["alembic"]["level"] == "INFO"
    assert config["loggers"]["urllib3"]["propagate"] is False
    assert config["root"]["handlers"] == ["console", "file"]


def test_merge_logging_config_authoritative_with_disable_existing_loggers() -> None:
    config = merge_logging_config(
        {
            "version": 1,
            "disable_existing_loggers": True,
            "handlers": {},
            "root": {"level": "ERROR", "handlers": []},
        }
    )

    assert config == {
        "version": 1,
        "disable_existing_loggers": True,
        "handlers": {},
        "root": {"level": "ERROR", "handlers": []},
    }
    assert "formatters" not in config
    assert "alembic" not in config


def test_merge_logging_config_rejects_non_table() -> None:
    with pytest.raises(ValueError, match=r"\[log\] must be a table"):
        merge_logging_config("not-a-table")


def test_logging_config_from_app_config_reads_log_table(tmp_path) -> None:
    config_path = tmp_path / "app.toml"
    config_path.write_text(
        """
        [app]
        modules = ["wybra.db"]
        database_url = "sqlite+aiosqlite:///app.sqlite3"

        [app.templates]
        auto_reload = true
        cache_size = 0

        [app.static]
        url_path = "/static/"

        [app.runserver]
        asgi_app = "test_app:app"
        reload_env = "APP_RELOAD"

        [log]
        version = 1
        disable_existing_loggers = false

        [log.formatters.simple]
        format = "%(levelname)s %(name)s: %(message)s"

        [log.handlers.console]
        class = "logging.StreamHandler"
        level = "INFO"
        formatter = "simple"
        stream = "ext://sys.stderr"

        [log.root]
        level = "INFO"
        handlers = ["console"]

        [log.loggers."urllib3"]
        level = "WARNING"
        handlers = ["console"]
        propagate = false
        """,
        encoding="utf-8",
    )

    app_config = load_app_config(project_root=tmp_path, config_path=config_path)
    config = logging_config_from_app_config(app_config)

    assert config["formatters"]["simple"]["format"] == (
        "%(levelname)s %(name)s: %(message)s"
    )
    assert config["loggers"]["urllib3"]["level"] == "WARNING"
    assert config["loggers"]["urllib3"]["propagate"] is False


def test_default_logging_config_returns_independent_copy() -> None:
    first = default_logging_config()
    second = default_logging_config()

    first["handlers"]["console"]["level"] = "DEBUG"

    assert second["handlers"]["console"]["level"] == "INFO"
