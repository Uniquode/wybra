from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from wybra.core.composition import AppConfig
from wybra.core.logging import configure_runtime_logging


def configure_cli_logging(
    app_config: AppConfig | None = None,
    *,
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Apply Wybra logging for a command-line entrypoint."""

    return configure_runtime_logging(app_config, config=config)


__all__ = ("configure_cli_logging",)
