from __future__ import annotations

from wybra.events import EventsCapability
from wybra.sessions.cleanup import session_cleanup_registry_from_site
from wybra.sessions.exceptions import SessionsConfigurationError
from wybra.sessions.middleware import (
    SessionMiddlewareContext,
    register_session_middleware,
)
from wybra.sessions.settings import SessionsSettings
from wybra.sessions.storage import SessionStorage, storage_from_settings
from wybra.site import Site

SESSION_SETTINGS_STATE_ATTRIBUTE = "wybra_session_settings"
SESSION_STORAGE_STATE_ATTRIBUTE = "wybra_session_storage"


async def setup_core_sessions(site: Site) -> None:
    _reject_starlette_session_middleware(site)
    settings = SessionsSettings.load_settings(site.config)
    cleanup_registry = session_cleanup_registry_from_site(site)
    storage = site.optional_capability(SessionStorage)
    if storage is None:
        storage = storage_from_settings(
            site,
            settings,
            cleanup_registry=cleanup_registry,
        )
        site.provide_capability(SessionStorage, storage)
    await storage.validate()
    setattr(site.app.state, SESSION_SETTINGS_STATE_ATTRIBUTE, settings)
    setattr(site.app.state, SESSION_STORAGE_STATE_ATTRIBUTE, storage)
    register_session_middleware(
        site.app,
        SessionMiddlewareContext(
            settings=settings,
            storage=storage,
            events=site.optional_capability(EventsCapability),
            cleanup_registry=cleanup_registry,
        ),
    )


def session_settings_from_site(site: Site) -> SessionsSettings:
    settings = getattr(site.app.state, SESSION_SETTINGS_STATE_ATTRIBUTE, None)
    if not isinstance(settings, SessionsSettings):
        raise SessionsConfigurationError("Wybra session settings are unavailable.")
    return settings


def session_storage_from_site(site: Site) -> SessionStorage:
    storage = getattr(site.app.state, SESSION_STORAGE_STATE_ATTRIBUTE, None)
    if storage is None:
        raise SessionsConfigurationError("Wybra session storage is unavailable.")
    return storage


def _reject_starlette_session_middleware(site: Site) -> None:
    for middleware in site.app.user_middleware:
        middleware_class = middleware.cls
        if (
            getattr(middleware_class, "__module__", "")
            == "starlette.middleware.sessions"
            and getattr(middleware_class, "__name__", "") == "SessionMiddleware"
        ):
            raise SessionsConfigurationError(
                "Starlette SessionMiddleware must not be installed with Wybra sessions."
            )


__all__ = (
    "SESSION_SETTINGS_STATE_ATTRIBUTE",
    "SESSION_STORAGE_STATE_ATTRIBUTE",
    "session_settings_from_site",
    "session_storage_from_site",
    "setup_core_sessions",
)
