from __future__ import annotations

from wybra.diagnostics.middleware import register_diagnostics_middleware
from wybra.diagnostics.settings import DiagnosticsSettings
from wybra.site import Site

DIAGNOSTICS_SETTINGS_STATE_ATTRIBUTE = "wybra_diagnostics_settings"


def setup_core_diagnostics(site: Site) -> DiagnosticsSettings:
    settings = DiagnosticsSettings.load_settings(site.config)
    setattr(site.app.state, DIAGNOSTICS_SETTINGS_STATE_ATTRIBUTE, settings)
    if settings.enabled:
        register_diagnostics_middleware(site, settings)
    return settings


__all__ = (
    "DIAGNOSTICS_SETTINGS_STATE_ATTRIBUTE",
    "setup_core_diagnostics",
)
