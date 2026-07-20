from __future__ import annotations

from wybra.diagnostics.capabilities import (
    DiagnosticsCapability,
    activate_process_diagnostics,
)
from wybra.diagnostics.debug import register_debug_websocket
from wybra.diagnostics.event_projection import register_event_projection
from wybra.diagnostics.middleware import register_diagnostics_middleware
from wybra.diagnostics.settings import DiagnosticsSettings
from wybra.events import EventsCapability
from wybra.site import Site

DIAGNOSTICS_SETTINGS_STATE_ATTRIBUTE = "wybra_diagnostics_settings"


def setup_core_diagnostics(site: Site) -> DiagnosticsSettings:
    settings = DiagnosticsSettings.load_settings(site.config)
    setattr(site.app.state, DIAGNOSTICS_SETTINGS_STATE_ATTRIBUTE, settings)
    if settings.events_enabled:
        capability = DiagnosticsCapability(
            retention_limit=settings.retention_limit,
            subscription_queue_limit=settings.subscription_queue_limit,
            allowed_scopes=settings.event_scopes,
            level=settings.level,
        )
        site.provide_capability(DiagnosticsCapability, capability)
        activate_process_diagnostics(capability)
        register_event_projection(
            site.require_capability(EventsCapability),
            settings.event_scopes,
        )
        register_diagnostics_middleware(site, settings, capability)
        register_debug_websocket(site, settings, capability)
    return settings


__all__ = (
    "DIAGNOSTICS_SETTINGS_STATE_ATTRIBUTE",
    "setup_core_diagnostics",
)
