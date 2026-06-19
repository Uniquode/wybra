from wybra.core import (
    DataValidationError,
    InputValidationError,
    InvalidConfigurationError,
)
from wybra.core.exceptions import ConfigurationError


def test_data_validation_error_is_value_error() -> None:
    assert issubclass(DataValidationError, ValueError)


def test_input_validation_error_is_data_validation_error() -> None:
    assert issubclass(InputValidationError, DataValidationError)


def test_invalid_configuration_error_is_data_validation_error() -> None:
    assert issubclass(InvalidConfigurationError, DataValidationError)


def test_data_validation_branches_are_separate_from_configuration_error() -> None:
    assert not issubclass(DataValidationError, ConfigurationError)
    assert not issubclass(InputValidationError, ConfigurationError)
    assert not issubclass(InvalidConfigurationError, ConfigurationError)
