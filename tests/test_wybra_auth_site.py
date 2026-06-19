from pathlib import Path

import pytest
from fastapi import FastAPI

from wybra.auth import AuthCapability, anonymous_required, login_required
from wybra.auth.delivery import NullIdentityDelivery
from wybra.config import MappingConfigSource
from wybra.db import DatabaseCapability
from wybra.site import SiteCapabilityError, start


def _site_config_source(
    tmp_path: Path,
    *,
    modules: tuple[str, ...] = ("wybra.db", "wybra.auth"),
) -> MappingConfigSource:
    return MappingConfigSource(
        {
            "app": {
                "config_path": tmp_path / "app.toml",
                "project_root": tmp_path,
                "modules": modules,
                "database_url": f"sqlite+aiosqlite:///{tmp_path / 'app.sqlite3'}",
            },
            "app.routes": {
                "prefixes": {
                    "wybra.auth": {"account": "/account", "api": ""},
                }
            },
            "app.templates": {"auto_reload": True, "cache_size": 0},
            "app.assets": {"url_path": "/static/", "root": Path("static")},
        }
    )


@pytest.mark.anyio
async def test_wybra_auth_setup_site_registers_auth_capability(
    tmp_path: Path,
) -> None:
    app = FastAPI()
    site = await start(app, config_source=_site_config_source(tmp_path))

    auth = site.require_capability(AuthCapability)

    assert site.has_capability(AuthCapability) is True
    assert isinstance(auth, AuthCapability)
    assert auth.settings is app.state.auth_settings
    assert auth.fastapi_users is app.state.fastapi_users
    assert isinstance(app.state.identity_delivery, NullIdentityDelivery)
    assert callable(auth.optional_current_user)
    assert callable(auth.login_required)
    assert callable(auth.anonymous_required)
    assert callable(login_required)
    assert callable(anonymous_required)


@pytest.mark.anyio
async def test_wybra_auth_setup_site_requires_database_capability(
    tmp_path: Path,
) -> None:
    with pytest.raises(SiteCapabilityError, match="Missing capability"):
        await start(
            FastAPI(),
            config_source=_site_config_source(tmp_path, modules=("wybra.auth",)),
        )


@pytest.mark.anyio
async def test_wybra_auth_setup_is_omitted_when_module_is_not_configured(
    tmp_path: Path,
) -> None:
    site = await start(
        FastAPI(),
        config_source=_site_config_source(tmp_path, modules=("wybra.db",)),
    )

    assert site.has_capability(DatabaseCapability) is True
    assert site.has_capability(AuthCapability) is False
    assert all(route.path != "/account/login" for route in site.app.routes)
