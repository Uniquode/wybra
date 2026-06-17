import pytest

from wybra.tools.app_startup import (
    normalise_cli_config_source,
    normalise_config_source,
)
from wybra.tools.project import ProjectToolConfigurationError


def test_normalise_config_source_rejects_blank_value_with_neutral_message() -> None:
    with pytest.raises(
        ProjectToolConfigurationError,
        match="Configuration source must not be blank",
    ):
        normalise_config_source("   ")


def test_normalise_cli_config_source_rejects_blank_config_option() -> None:
    with pytest.raises(
        ProjectToolConfigurationError, match="--config must not be blank"
    ):
        normalise_cli_config_source("   ")


def test_normalise_config_source_strips_value() -> None:
    assert normalise_config_source(" app.toml ") == "app.toml"
