from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import FastAPI

from wevra import Site, SiteCapabilityError, start
from wevra.config import ConfigSourceError, ConfigSourceResult, MappingConfigSource
from wevra.core.composition import (
    AppConfig,
    RouteOptions,
    StaticOptions,
    TemplateOptions,
)


class ExampleCapability:
    pass


class OtherCapability:
    pass


class ExampleImplementation(ExampleCapability):
    pass


def _write_app_config(path: Path, *, modules: tuple[str, ...]) -> Path:
    path.write_text(
        f"""
        [app]
        modules = {json.dumps(list(modules))}
        database_url = "sqlite+aiosqlite:///app.sqlite3"

        [app.routes]

        [app.templates]
        auto_reload = true
        cache_size = 0

        [app.static]
        url_path = "/static/"
        export_root = "static"
        """,
        encoding="utf-8",
    )
    return path


def test_start_composes_existing_fastapi_app_from_file_source(tmp_path: Path) -> None:
    app = FastAPI(title="Host app")
    config_path = _write_app_config(tmp_path / "app.toml", modules=("wevra.web",))

    site = start(app, config_source=str(config_path))

    assert isinstance(site, Site)
    assert site.app is app
    assert site.modules == ("wevra.web",)
    assert site.has_module("wevra.web") is True
    assert site.has_module("wevra.auth") is False


def test_start_accepts_relative_file_source_string(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_app_config(tmp_path / "app.toml", modules=("wevra.web",))
    monkeypatch.chdir(tmp_path)

    site = start(FastAPI(), config_source="app.toml")

    assert site.modules == ("wevra.web",)


def test_start_accepts_file_uri_source_string(tmp_path: Path) -> None:
    config_path = _write_app_config(
        tmp_path / "app.toml",
        modules=("wevra.web",),
    )

    site = start(FastAPI(), config_source=config_path.as_uri())

    assert site.modules == ("wevra.web",)


def test_start_rejects_blank_config_source_string() -> None:
    with pytest.raises(ConfigSourceError, match="must not be blank"):
        start(FastAPI(), config_source="   ")


def test_start_rejects_unsupported_config_source_uri_scheme() -> None:
    with pytest.raises(ConfigSourceError, match="Unsupported config source URI scheme"):
        start(FastAPI(), config_source="https://example.test/app.toml")


def test_start_treats_windows_absolute_source_string_as_file_path() -> None:
    with pytest.raises(ConfigSourceError, match="file: App config file"):
        start(FastAPI(), config_source=r"C:\config\app.toml")


def test_start_rejects_invalid_config_source_object() -> None:
    with pytest.raises(ConfigSourceError, match="string, AppConfig, or ConfigSource"):
        start(FastAPI(), config_source=object())  # type: ignore[arg-type]


def test_start_rejects_config_source_object_with_invalid_metadata() -> None:
    class InvalidConfigSource:
        metadata = object()

        def load(self) -> ConfigSourceResult:
            return ConfigSourceResult()

    with pytest.raises(ConfigSourceError, match="string, AppConfig, or ConfigSource"):
        start(FastAPI(), config_source=InvalidConfigSource())  # type: ignore[arg-type]


def test_site_provides_and_requires_type_keyed_capability() -> None:
    site = start(
        FastAPI(),
        config_source=MappingConfigSource({"app": {"modules": ()}}),
    )
    capability = ExampleImplementation()

    site.provide_capability(ExampleCapability, capability)

    assert site.has_capability(ExampleCapability) is True
    assert site.require_capability(ExampleCapability) is capability


def test_site_reports_missing_required_capability() -> None:
    site = start(
        FastAPI(),
        config_source=MappingConfigSource({"app": {"modules": ()}}),
    )

    with pytest.raises(SiteCapabilityError, match="Missing capability"):
        site.require_capability(ExampleCapability)


def test_site_rejects_duplicate_capability_provider() -> None:
    site = start(
        FastAPI(),
        config_source=MappingConfigSource({"app": {"modules": ()}}),
    )
    site.provide_capability(ExampleCapability, ExampleImplementation())

    with pytest.raises(SiteCapabilityError, match="already provided"):
        site.provide_capability(ExampleCapability, ExampleImplementation())


def test_site_rejects_capability_value_with_wrong_runtime_type() -> None:
    site = start(
        FastAPI(),
        config_source=MappingConfigSource({"app": {"modules": ()}}),
    )

    with pytest.raises(SiteCapabilityError, match="invalid type"):
        site.provide_capability(ExampleCapability, OtherCapability())


def test_start_accepts_loaded_app_config(tmp_path: Path) -> None:
    app_config = AppConfig(
        config_path=tmp_path / "app.toml",
        project_root=tmp_path,
        modules=("wevra.web", "wevra.auth"),
        routes=RouteOptions(prefixes={}),
        templates=TemplateOptions(auto_reload=True, cache_size=0),
        static=StaticOptions(url_path="/static/", export_root=Path("static")),
    )

    site = start(FastAPI(), config_source=app_config)

    assert site.modules == ("wevra.web", "wevra.auth")
    assert site.has_module("wevra.auth") is True


def test_start_accepts_config_source_object() -> None:
    site = start(
        FastAPI(),
        config_source=MappingConfigSource(
            {"app": {"modules": ("wevra.web",)}},
            source="test",
        ),
    )

    assert site.modules == ("wevra.web",)


def test_start_reports_missing_required_config_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigSourceError, match="file: App config file"):
        start(FastAPI(), config_source=str(tmp_path / "missing.toml"))


def _write_module(root: Path, name: str, body: str) -> None:
    module_path = root / f"{name}.py"
    module_path.write_text(body, encoding="utf-8")


def test_start_invokes_setup_site_hooks_in_configured_module_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.syspath_prepend(str(tmp_path))
    _write_module(tmp_path, "site_setup_recorder", "calls = []\n")
    _write_module(
        tmp_path,
        "first_module",
        "from site_setup_recorder import calls\n"
        'def setup_site(site):\n    calls.append("first")\n',
    )
    _write_module(
        tmp_path,
        "second_module",
        "from site_setup_recorder import calls\n"
        'def setup_site(site):\n    calls.append("second")\n',
    )

    site = start(
        FastAPI(),
        config_source=MappingConfigSource(
            {"app": {"modules": ("first_module", "second_module")}}
        ),
    )

    from site_setup_recorder import calls

    assert isinstance(site, Site)
    assert calls == ["first", "second"]


def test_start_ignores_modules_without_setup_site(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.syspath_prepend(str(tmp_path))
    _write_module(tmp_path, "plain_module", "")

    site = start(
        FastAPI(),
        config_source=MappingConfigSource({"app": {"modules": ("plain_module",)}}),
    )

    assert site.modules == ("plain_module",)


def test_start_rejects_non_callable_setup_site(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.syspath_prepend(str(tmp_path))
    _write_module(tmp_path, "invalid_module", "setup_site = object()\n")

    with pytest.raises(SiteCapabilityError, match="setup_site"):
        start(
            FastAPI(),
            config_source=MappingConfigSource(
                {"app": {"modules": ("invalid_module",)}}
            ),
        )


def test_start_reports_setup_site_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.syspath_prepend(str(tmp_path))
    _write_module(
        tmp_path,
        "failing_module",
        'def setup_site(site):\n    raise RuntimeError("boom")\n',
    )

    with pytest.raises(SiteCapabilityError, match="failing_module"):
        start(
            FastAPI(),
            config_source=MappingConfigSource(
                {"app": {"modules": ("failing_module",)}}
            ),
        )
