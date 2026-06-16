import pytest

from wybra.core.asgi import load_asgi_app
from wybra.core.exceptions import ConfigurationError


def test_load_asgi_app_returns_created_app() -> None:
    app = object()

    assert load_asgi_app(lambda: app) is app


def test_load_asgi_app_reports_configuration_errors_without_traceback(
    capsys: pytest.CaptureFixture[str],
) -> None:
    def raise_configuration_error() -> object:
        raise ConfigurationError("APP_ENV must be local, staging, or production.")

    with pytest.raises(SystemExit) as excinfo:
        load_asgi_app(raise_configuration_error)

    message = (
        "Application configuration failed: "
        "APP_ENV must be local, staging, or production."
    )
    captured = capsys.readouterr()
    assert str(excinfo.value) == message
    assert captured.err.strip() == message
