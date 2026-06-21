from __future__ import annotations

from wybra.forms import context as _context  # noqa: F401
from wybra.forms.capabilities import DefaultFormsCapability, FormsCapability
from wybra.forms.middleware import register_forms_response_finalisation_middleware
from wybra.forms.settings import FormsSettings
from wybra.site import Site


async def setup_site(site: Site) -> None:
    settings = FormsSettings.load_settings(site.config)
    csrf = settings.protector()
    site.app.state.csrf = csrf
    site.provide_capability(FormsCapability, DefaultFormsCapability(csrf))
    register_forms_response_finalisation_middleware(site)


__all__ = ("setup_site",)
