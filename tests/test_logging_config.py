from __future__ import annotations

import logging
import re
from pathlib import Path

import pytest

import wybra.core.logging as logging_module
from wybra.core.composition import load_app_config
from wybra.core.logging import (
    DEFAULT_LOG_DATE_FORMAT,
    DEFAULT_LOG_FORMAT,
    configure_runtime_logging,
    default_logging_config,
    logging_config_from_app_config,
    merge_logging_config,
)


@pytest.fixture(autouse=True)
def restore_root_logging():
    root = logging.getLogger()
    original_handlers = list(root.handlers)
    original_level = root.level
    original_disabled = logging.root.manager.disable
    yield
    for handler in list(root.handlers):
        root.removeHandler(handler)
        if handler not in original_handlers:
            handler.close()
    root.handlers[:] = original_handlers
    root.setLevel(original_level)
    logging.disable(original_disabled)


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
    assert config["loggers"]["tortoise"]["level"] == "INFO"
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
    assert "tortoise" not in config


def test_merge_logging_config_authoritative_injects_required_version() -> None:
    config = merge_logging_config(
        {
            "disable_existing_loggers": True,
            "handlers": {},
            "root": {"level": "ERROR", "handlers": []},
        }
    )

    assert config["version"] == 1
    assert config["disable_existing_loggers"] is True
    assert "formatters" not in config
    assert "tortoise" not in config


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

        [app.assets]
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


def test_default_logging_config_uses_iso_timestamp_level_logger_and_message() -> None:
    config = default_logging_config()

    assert config["formatters"]["simple"]["format"] == DEFAULT_LOG_FORMAT
    assert config["formatters"]["simple"]["datefmt"] == DEFAULT_LOG_DATE_FORMAT


def test_configure_runtime_logging_emits_default_format(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_runtime_logging()

    logging.getLogger("wybra.tests.logging").warning("runtime ready")

    output = capsys.readouterr().err.strip()
    assert re.match(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{4} "
        r"WARNING wybra\.tests\.logging runtime ready",
        output,
    )
    assert not output.startswith("WARNING:wybra.tests.logging:")


def test_configure_runtime_logging_replaces_fallback_handlers(
    capsys: pytest.CaptureFixture[str],
) -> None:
    fallback_records: list[str] = []

    class FallbackHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            fallback_records.append(record.getMessage())

    logging.getLogger().addHandler(FallbackHandler())

    configure_runtime_logging()
    configure_runtime_logging()
    logging.getLogger("wybra.tests.logging").warning("single record")

    output = capsys.readouterr().err
    assert output.count("single record") == 1
    assert fallback_records == []
    assert len(logging.getLogger().handlers) == 1


def test_core_logging_does_not_embed_uvicorn_details() -> None:
    source = Path(logging_module.__file__).read_text(encoding="utf-8")

    assert "uvicorn" not in source.lower()
