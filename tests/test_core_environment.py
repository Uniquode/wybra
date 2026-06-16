from pathlib import Path

import pytest

from wybra.core.environment import load_environment
from wybra.core.exceptions import ConfigurationError


def test_load_environment_reads_dotenv_without_mutating_process_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("APP_RELOAD", raising=False)
    (tmp_path / ".env").write_text("APP_RELOAD=true\n", encoding="utf-8")

    env = load_environment(environ={}, project_root=tmp_path)

    assert env.get("APP_RELOAD") == "true"
    assert not env.is_set("UNCONFIGURED_VALUE")


def test_load_environment_wraps_loader_failures_without_raw_detail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def raise_sensitive_error(**_kwargs: object) -> None:
        raise RuntimeError("DATABASE_URL=postgresql://user:secret@example/app")

    monkeypatch.setattr("wybra.core.environment.Env", raise_sensitive_error)

    with pytest.raises(
        ConfigurationError,
        match="Environment loader failed while initialising envex",
    ) as excinfo:
        load_environment(environ={}, read_dotenv=False)

    assert "RuntimeError" in str(excinfo.value)
    assert "secret" not in str(excinfo.value)
    assert "DATABASE_URL" not in str(excinfo.value)
